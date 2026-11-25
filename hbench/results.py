"""Writing the contract output of a run: `<output_dir>/<run_id>/run.json` and `trials.jsonl`, and what `run --resume`
needs: reopening a run directory and the heartbeat that says whether a run is still alive (ADR 0010).

Files hold references only (log path prefixes), never prompts, agent output or diffs.
"""

import dataclasses
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from hbench.core.models import ContractError, Costs, RunInfo, TrialResult


def _atomic_write(path: Path, text: str) -> None:
    """Write via a private temp file in the same directory, then replace, so readers never see a partial file.

    The file keeps the permissions of the one it replaces; a new file is 0644 like other result files."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        if path.exists():
            shutil.copymode(path, tmp)
        else:
            tmp.chmod(0o644)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


RUN_ID = re.compile(r"[0-9a-f]{32}")  # `new_run_id()`: a UUID4 in hex


class ResumeError(ValueError):
    """A run cannot be reopened: it does not exist, or its files are not valid contract data."""


class RunWriter:
    """Owns one run directory. `run.json` exists from construction; `close` adds the finish time.

    `RunWriter.open_existing` reopens the directory of an interrupted run (`run --resume`)."""

    def __init__(self, out_dir: Path, info: RunInfo, *, existing: bool = False) -> None:
        self.info = info
        self.run_dir = out_dir / info.run_id
        self.trials_path = self.run_dir / "trials.jsonl"
        if not existing:
            self.run_dir.mkdir(parents=True)
            self.trials_path.touch()
            self._write_info()

    @classmethod
    def open_existing(cls, out_dir: Path, run_id: str) -> Self:
        """The writer of the run `run_id` in `out_dir`, appending to its `trials.jsonl`. A last line cut short by a
        crash is dropped, so the next trial starts on a line of its own. Raises `ResumeError` for an id that is
        not a run id, a run that does not exist, or files that are not valid."""
        if RUN_ID.fullmatch(run_id) is None:
            raise ResumeError(f"{run_id!r} is not a run id (32 lowercase hex characters, the name of a run directory)")
        info_path = out_dir / run_id / "run.json"
        try:
            info = RunInfo.from_dict(json.loads(info_path.read_text()))
        except FileNotFoundError as e:
            raise ResumeError(f"no run {run_id} in {out_dir}: {info_path} does not exist") from e
        except ValueError as e:  # ContractError and json.JSONDecodeError are both ValueErrors
            raise ResumeError(f"{info_path} is not valid run data: {e}") from e
        writer = cls(out_dir, info, existing=True)
        writer._drop_partial_last_line()
        return writer

    def _drop_partial_last_line(self) -> None:
        text = self.trials_path.read_text() if self.trials_path.exists() else ""
        if text and not text.endswith("\n"):
            _atomic_write(self.trials_path, text[: text.rfind("\n") + 1])

    def trials(self) -> list[TrialResult]:
        """The trials written so far. Raises `ResumeError` when `trials.jsonl` holds anything else."""
        try:
            lines = self.trials_path.read_text().splitlines() if self.trials_path.exists() else []
            return [TrialResult.from_dict(json.loads(line)) for line in lines if line.strip()]
        except ValueError as e:
            raise ResumeError(f"{self.trials_path} is not valid trial data: {e}") from e

    def finished_trial_ids(self) -> set[str]:
        return {trial.trial_id for trial in self.trials()}

    def append(self, trial: TrialResult) -> None:
        """Add a trial and make it durable before returning: `fsync`, so a crash right after cannot lose it."""
        with self.trials_path.open("a") as f:
            f.write(json.dumps(trial.to_dict()) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def close(self, finished_at: datetime) -> None:
        self.info = dataclasses.replace(self.info, finished_at=finished_at)
        self._write_info()

    def _write_info(self) -> None:
        _atomic_write(self.run_dir / "run.json", json.dumps(self.info.to_dict(), indent=2) + "\n")


def read_trials(out_dir: Path, warn: Callable[[str], None] = print) -> list[TrialResult]:
    """Every trial of every run directory in `out_dir`, oldest file name first. A file that is not valid contract
    data is skipped with a `warn` message."""
    trials: list[TrialResult] = []
    for path in sorted(out_dir.glob("*/trials.jsonl")):
        try:
            trials += [
                TrialResult.from_dict(json.loads(line)) for line in path.read_text().splitlines() if line.strip()
            ]
        except ValueError as e:  # ContractError and json.JSONDecodeError are both ValueErrors
            warn(f"warning: {path} skipped, not valid trial data: {e}")
    return trials


def _with_gateway_cost(trial: TrialResult, cost: float) -> TrialResult:
    costs = dataclasses.replace(trial.usage.costs, gateway=cost)
    return dataclasses.replace(trial, usage=dataclasses.replace(trial.usage, costs=costs))


def _rewritten(path: Path, costs_by_tag: Mapping[str, float]) -> tuple[list[TrialResult], int]:
    """The trials of `path` with gateway costs applied, and how many were updated."""
    trials = [TrialResult.from_dict(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    rewritten, matched = [], 0
    for trial in trials:
        cost = costs_by_tag.get(trial.trace_id) if trial.trace_id else None
        if cost is None:
            rewritten.append(trial)
        else:
            rewritten.append(_with_gateway_cost(trial, cost))
            matched += 1
    return rewritten, matched


def _valid_costs(costs_by_tag: Mapping[str, float]) -> dict[str, float]:
    """The spends the contract accepts; a bad one (negative, NaN: it comes from the gateway) is skipped."""
    valid = {}
    for tag, cost in costs_by_tag.items():
        try:
            Costs(gateway=cost)
        except ContractError as e:
            print(f"warning: gateway spend for tag {tag!r} ignored: {e}")
        else:
            valid[tag] = cost
    return valid


def apply_gateway_costs(out_dir: Path, costs_by_tag: Mapping[str, float]) -> int:
    """Set `usage.costs.gateway` on every trial whose `trace_id` is in `costs_by_tag`, in all run dirs.

    Returns the number of trials updated. A file is rewritten only when it has a match. A file that is not valid
    contract data (corrupt, or written by another tool) is left untouched with a warning."""
    valid = _valid_costs(costs_by_tag)
    updated = 0
    for path in sorted(out_dir.glob("*/trials.jsonl")):
        try:
            trials, matched = _rewritten(path, valid)
        except ValueError as e:  # ContractError and json.JSONDecodeError are both ValueErrors
            print(f"warning: {path} skipped, not valid trial data: {e}")
            continue
        if matched:
            _atomic_write(path, "".join(json.dumps(t.to_dict()) + "\n" for t in trials))
            updated += matched
    return updated


# ---------------------------------------------------------------- heartbeat

HEARTBEAT_FILE = "heartbeat.json"
HEARTBEAT_INTERVAL_S = 30.0
HEARTBEAT_FRESH_S = 90.0


class Heartbeat:
    """`<run_dir>/heartbeat.json`: who is running the run and what it is doing, rewritten atomically at each phase
    change and every `interval` seconds by a daemon thread, so `--resume` can tell a live run from a killed one.
    Fields: `pid`, `hostname`, `trial_id`, `phase`, `updated_at` and the `extra` ones the caller adds. Use it as a
    context manager: it starts on entry and the file is removed on exit (a run that is gone leaves none)."""

    def __init__(
        self, path: Path, extra: Mapping[str, str] | None = None, interval: float = HEARTBEAT_INTERVAL_S
    ) -> None:
        self.path = path
        self.interval = interval
        self._state: dict[str, Any] = {"trial_id": None, "phase": "start", **(extra or {})}
        self._lock = threading.Lock()
        self._warned = False
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="heartbeat", daemon=True)

    def beat(self, trial_id: str | None, phase: str) -> None:
        """Record the phase now (and rewrite the file at once)."""
        with self._lock:
            self._state.update(trial_id=trial_id, phase=phase)
            self._write()

    def _write(self) -> None:
        """Write the file; the caller holds the lock. A failure (disk full, a dir gone) is warned about once and never
        raised: a run must not die because its heartbeat could not be written, and the next beat may succeed."""
        state = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            **self._state,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        try:
            _atomic_write(self.path, json.dumps(state, indent=2) + "\n")
        except OSError as e:
            if not self._warned:
                self._warned = True
                print(f"warning: heartbeat {self.path} not written (will keep trying): {e}", file=sys.stderr)

    def _loop(self) -> None:
        while not self._stopped.wait(self.interval):
            with self._lock:
                self._write()

    def start(self) -> None:
        """Write the first beat and start the thread."""
        with self._lock:
            self._write()
        self._thread.start()

    def stop(self) -> None:
        """Stop the thread and remove the file: a run that is gone leaves none."""
        self._stopped.set()
        self._thread.join()
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.stop()


def read_heartbeat(path: Path) -> dict[str, Any] | None:
    """The heartbeat file as a dict, or None when it is missing or not a JSON object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # it exists, and belongs to another user
        return True
    return True


def heartbeat_is_live(path: Path, now: datetime | None = None, max_age_s: float = HEARTBEAT_FRESH_S) -> bool:
    """Whether the run behind this heartbeat file may still be running: it is fresh (younger than `max_age_s`) and
    its pid is alive on this host. A fresh heartbeat of another host cannot be checked, so it counts as alive. A
    missing, malformed or stale file means the run is gone."""
    data = read_heartbeat(path)
    if data is None:
        return False
    pid, host, updated = data.get("pid"), data.get("hostname"), data.get("updated_at")
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and isinstance(host, str)):
        return False
    try:
        age = ((now or datetime.now(UTC)) - datetime.fromisoformat(str(updated))).total_seconds()
    except (ValueError, TypeError):  # not a timestamp, or a naive one
        return False
    if age >= max_age_s:
        return False
    return _pid_alive(pid) if host == socket.gethostname() else True

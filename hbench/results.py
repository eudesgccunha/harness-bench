"""Writing the contract output of a run: `<output_dir>/<run_id>/run.json` and `trials.jsonl`, and what `run --resume`
needs: reopening a run directory and the heartbeat that says whether a run is still alive (ADR 0010).

Files hold references only (log path prefixes), never prompts, agent output or diffs.
"""

import contextlib
import dataclasses
import fcntl
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Self, TextIO

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


@contextlib.contextmanager
def _exclusive(f: IO[Any]) -> Iterator[None]:
    """An exclusive `flock` on the open file, released when the block ends."""
    fcntl.flock(f, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)


def locked_append(path: Path, text: str) -> None:
    """Append `text` to `path` under an exclusive `flock`: write, flush, `fsync`, unlock. Workers on one run directory
    take turns, so no line is ever torn or interleaved (ADR 0012; it needs a filesystem where `flock` works). Under the
    lock no live writer is mid-write, so a last line without its newline was cut short by a crash: it is dropped."""
    with path.open("a+b") as f, _exclusive(f):
        size = os.fstat(f.fileno()).st_size
        if size:
            f.seek(size - 1)
            if f.read(1) != b"\n":
                f.seek(0)
                data = f.read()
                f.truncate(data.rfind(b"\n") + 1)
        f.write(text.encode())
        f.flush()
        os.fsync(f.fileno())


@dataclass
class LogCursor:
    """What a reader of a line file keeps between calls of `locked_log`: where it stopped, which file that was, and
    what the caller learned from the lines (`ids`). `fresh` says the last read began at the start of the file."""

    inode: int | None = None
    offset: int = 0
    fresh: bool = True
    ids: set[str] = field(default_factory=set)


@contextlib.contextmanager
def locked_log(path: Path, cursor: LogCursor) -> Iterator[tuple[TextIO, str]]:
    """Open the line file `path` for reading and appending under an exclusive `flock` and yield it with its complete
    lines. Under the lock no live writer is mid-write, so anything after the last newline was cut short by a crash and
    is dropped first. The caller writes its line (one `write`, then `flush` and `fsync`).

    Only the lines added since the `cursor`'s last read come back, so a long file is not read again for every line.
    A file that shrank or was replaced (another inode) is read from the start: `cursor.fresh` is True and
    `cursor.ids` is emptied; the caller refills it from the lines it gets."""
    with path.open("a+") as f, _exclusive(f):
        stat = os.fstat(f.fileno())
        start = cursor.offset if cursor.inode == stat.st_ino and cursor.offset <= stat.st_size else 0
        f.seek(start)
        text = f.read()
        complete = text[: text.rfind("\n") + 1]
        end = start + len(complete.encode())
        if len(complete) != len(text):
            f.truncate(end)
        if start == 0:
            cursor.ids.clear()
        cursor.fresh, cursor.inode, cursor.offset = start == 0, stat.st_ino, end
        yield f, complete


@contextlib.contextmanager
def trial_lock(run_dir: Path, trial_id: str) -> Iterator[None]:
    """An exclusive `flock` on `<run_dir>/.locks/<trial_id>.lock`, held for the whole trial: two workers given the same
    spec never run it at once (the second waits). The lock goes when its holder dies, however it dies."""
    lock_dir = run_dir / ".locks"
    lock_dir.mkdir(exist_ok=True)
    with (lock_dir / f"{trial_id}.lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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
        self._cursor = LogCursor()  # the `trial_id`s `append_once` has seen, so it reads only what was added since
        if not existing:
            self.run_dir.mkdir(parents=True)
            self.trials_path.touch()
            self._write_info()

    @classmethod
    def open_existing(cls, out_dir: Path, run_id: str, *, repair: bool = True) -> Self:
        """The writer of the run `run_id` in `out_dir`, appending to its `trials.jsonl`. With `repair`, a last line
        cut short by a crash is dropped, so the next trial starts on a line of its own; a worker that shares the run
        with others passes `repair=False` (a line that looks cut short may be another worker's write in progress:
        `append_once` repairs it under the lock). Raises `ResumeError` for an id that is not a run id, a run that
        does not exist, or files that are not valid."""
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
        if repair:
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

    def append_once(self, trial: TrialResult) -> TrialResult:
        """Add a trial and make it durable before returning (`fsync`), unless a trial with this `trial_id` is already in
        the file: then nothing is written and the stored trial is returned (`trial` itself when it was written). Check
        and write happen under one lock, so two workers that ran the same trial (an orchestrator retry) leave one line.
        Under the lock no other writer is mid-write, so a last line cut short belongs to a crashed one and is dropped
        first. Only the lines added since the last call are read."""
        with locked_log(self.trials_path, self._cursor) as (f, complete):
            self._cursor.ids |= {i for i in map(_line_trial_id, complete.splitlines()) if isinstance(i, str)}
            if trial.trial_id in self._cursor.ids:
                f.seek(0)
                return next(
                    TrialResult.from_dict(json.loads(line))
                    for line in f.read().splitlines()
                    if _line_trial_id(line) == trial.trial_id
                )
            f.write(json.dumps(trial.to_dict()) + "\n")
            f.flush()
            os.fsync(f.fileno())
            self._cursor.ids.add(trial.trial_id)
            return trial

    def close(self, finished_at: datetime) -> None:
        self.info = dataclasses.replace(self.info, finished_at=finished_at)
        self._write_info()

    def _write_info(self) -> None:
        _atomic_write(self.run_dir / "run.json", json.dumps(self.info.to_dict(), indent=2) + "\n")


def _line_trial_id(line: str) -> object:
    """The `trial_id` of one line of `trials.jsonl`, or None for a line that holds no JSON object with one."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data.get("trial_id") if isinstance(data, dict) else None


def read_run(run_dir: Path) -> tuple[RunInfo, list[TrialResult]]:
    """The run in `run_dir` as it is on disk, without changing a file (`export` may read a run that is still going):
    its `run.json` and its trials. A last line without a newline is a write in progress or cut short by a crash,
    so it is left out. Raises `ResumeError` for a directory that is not a run or holds invalid contract data."""
    try:
        info = RunInfo.from_dict(json.loads((run_dir / "run.json").read_text()))
        text = (run_dir / "trials.jsonl").read_text()
        lines = text.splitlines() if text.endswith("\n") else text.splitlines()[:-1]
        return info, [TrialResult.from_dict(json.loads(line)) for line in lines if line.strip()]
    except FileNotFoundError as e:
        raise ResumeError(f"{run_dir} is not a run directory: {e.filename} does not exist") from e
    except ValueError as e:  # ContractError and json.JSONDecodeError are both ValueErrors
        raise ResumeError(f"{run_dir} is not valid run data: {e}") from e


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

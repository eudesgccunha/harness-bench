"""Writing the contract output of a run: `<output_dir>/<run_id>/run.json` and `trials.jsonl`.

Files hold references only (log path prefixes), never prompts, agent output or diffs.
"""

import dataclasses
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

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


class RunWriter:
    """Owns one run directory. `run.json` exists from construction; `close` adds the finish time."""

    def __init__(self, out_dir: Path, info: RunInfo) -> None:
        self.info = info
        self.run_dir = out_dir / info.run_id
        self.run_dir.mkdir(parents=True)
        self.trials_path = self.run_dir / "trials.jsonl"
        self.trials_path.touch()
        self._write_info()

    def append(self, trial: TrialResult) -> None:
        with self.trials_path.open("a") as f:
            f.write(json.dumps(trial.to_dict()) + "\n")
            f.flush()

    def close(self, finished_at: datetime) -> None:
        self.info = dataclasses.replace(self.info, finished_at=finished_at)
        self._write_info()

    def _write_info(self) -> None:
        _atomic_write(self.run_dir / "run.json", json.dumps(self.info.to_dict(), indent=2) + "\n")


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

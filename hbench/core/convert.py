"""Convert a legacy `results.jsonl` row into a contract `TrialResult`.

The legacy row stays the resume/reconcile record; the contract is derived from it. Rows written before
Phase 1 lack some keys (`parse_ok`, `sandboxed`, `cost_*`), so every optional key has a documented default.
"""

import math
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from hbench.core.ids import trial_id
from hbench.core.models import (
    ContractError,
    Costs,
    describe_value,
    GraderResult,
    Target,
    TrialResult,
    TrialStatus,
    Usage,
)

_TAG_RE = re.compile(r"^bench-\d{14}-(.+)$")
_EPOCH = datetime.fromtimestamp(0, UTC)
# Run id of trials rebuilt from `results.jsonl` for reporting: they belong to no `run.json`.
LEGACY_RUN_ID = "legacy"
_NUMBER_KEYS = ("input", "output", "reasoning", "cache_read", "cache_write", "turns")
_COST_KEYS = ("cost_litellm", "cost_computed", "cost_reported")


def _started_at(row: Mapping[str, Any]) -> datetime:
    """`ts` (ISO 8601; naive means UTC), else `t_start` (epoch seconds), else the epoch."""
    try:
        ts = datetime.fromisoformat(row["ts"])
        return ts if ts.utcoffset() is not None else ts.replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError):
        pass
    if isinstance(row.get("t_start"), int | float):
        return datetime.fromtimestamp(row["t_start"], UTC)
    return _EPOCH


def _status(passed: bool, timed_out: bool) -> TrialStatus:
    if passed:
        return TrialStatus.PASSED
    return TrialStatus.TIMEOUT if timed_out else TrialStatus.FAILED


def _count(name: str, value: Any) -> int:
    """A token or turn count from untrusted parser output: a whole number >= 0 (a float is fine if integral)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ContractError(f"{name} must be a whole number, got {describe_value(value)}")
    if isinstance(value, float) and not (math.isfinite(value) and value.is_integer()):
        raise ContractError(f"{name} must be a whole number, got {value!r}")
    if value < 0:
        raise ContractError(f"{name} must be a whole number >= 0, got {value!r}")
    return int(value)


def _artifact_uri(run_tag: str | None) -> str | None:
    """Log path prefix relative to the output dir (`logs/<tag>`), or None for a tag of another shape."""
    match = _TAG_RE.match(run_tag or "")
    return f"logs/{match.group(1)}" if match else None


def trial_from_row(
    row: Mapping[str, Any],
    *,
    run_id: str,
    suite_version: str,
    harness_version: str = "unknown",
    test_timed_out: bool = False,
) -> TrialResult:
    target = Target(harness=row["harness"], harness_version=harness_version, model=row["model"], params={})
    passed = bool(row["passed"])
    usage = Usage(
        input_tokens=_count("input_tokens", row["input"]),
        output_tokens=_count("output_tokens", row["output"]),
        reasoning_tokens=_count("reasoning_tokens", row.get("reasoning", 0)),
        cache_read_tokens=_count("cache_read_tokens", row["cache_read"]),
        cache_write_tokens=_count("cache_write_tokens", row["cache_write"]),
        turns=_count("turns", row["turns"]),
        parsed=bool(row.get("parse_ok", True)),
        costs=Costs(
            gateway=row.get("cost_litellm"), price_table=row.get("cost_computed"), harness=row.get("cost_reported")
        ),
    )
    return TrialResult(
        run_id=run_id,
        trial_id=trial_id(run_id, row["task"], target.target_id, row["rep"]),
        task_id=row["task"],
        suite_version=suite_version,
        target=target,
        rep=row["rep"],
        status=_status(passed, bool(row.get("agent_timed_out")) or test_timed_out),
        graders=(GraderResult(grader="test", grader_version="1", passed=passed, timed_out=test_timed_out),),
        usage=usage,
        latency_s=row["wall_s"],
        started_at=_started_at(row),
        agent_exit_code=row.get("agent_rc"),
        sandboxed=bool(row.get("sandboxed", False)),
        trace_id=row.get("run_tag"),
        artifact_uri=_artifact_uri(row.get("run_tag")),
    )


def _label(row: Mapping[str, Any]) -> str:
    return str(row.get("run_tag") or f"{row.get('task')}/{row.get('harness')}/{row.get('model')} rep {row.get('rep')}")


def _sanitized(row: Mapping[str, Any], warn: Callable[[str], None] | None) -> dict[str, Any]:
    """A copy of `row` without its bad fields. A bad cost becomes unknown (the other costs are kept); a bad
    count zeroes all counts, drops the price-table cost computed from them and marks the row as unparsed.
    Every bad field is reported with `warn`."""
    clean = dict(row)
    bad_count = False
    for key in _NUMBER_KEYS:
        try:
            _count(key, row.get(key, 0))
        except ContractError as e:
            bad_count = True
            if warn:
                warn(f"warning: {_label(row)}: {e}; counts dropped, trial marked as unparsed")
    if bad_count:
        clean.update(dict.fromkeys(_NUMBER_KEYS, 0), parse_ok=False)
        if clean.get("cost_computed") is not None:
            # The price-table cost was derived from these counts, so it is as wrong as they are.
            clean["cost_computed"] = None
            if warn:
                warn(f"warning: {_label(row)}: price-table cost dropped because the counts are invalid")
    for key in _COST_KEYS:
        try:
            Costs(harness=clean.get(key))
        except ContractError as e:
            clean[key] = None
            if warn:
                warn(f"warning: {_label(row)}: {key}: {e}; cost treated as unknown")
    return clean


def trial_from_row_lenient(
    row: Mapping[str, Any],
    *,
    run_id: str,
    suite_version: str,
    harness_version: str = "unknown",
    test_timed_out: bool = False,
    warn: Callable[[str], None] | None = None,
) -> TrialResult:
    """Like `trial_from_row`, but fields that break the contract (negative, NaN, bool, fractional: parser output
    is untrusted) are dropped one by one instead of raising. See `_sanitized`."""
    kwargs = {"run_id": run_id, "suite_version": suite_version, "harness_version": harness_version}
    try:
        return trial_from_row(row, test_timed_out=test_timed_out, **kwargs)
    except ContractError:
        return trial_from_row(_sanitized(row, warn), test_timed_out=test_timed_out, **kwargs)


def trials_from_rows(rows: Iterable[Mapping[str, Any]], warn: Callable[[str], None] | None = None) -> list[TrialResult]:
    """Trials for reporting over legacy rows. A hand-written row with rep 0 is read as rep 1."""
    return [
        trial_from_row_lenient(
            {**row, "rep": max(row["rep"], 1)}, run_id=LEGACY_RUN_ID, suite_version="unknown", warn=warn
        )
        for row in rows
    ]

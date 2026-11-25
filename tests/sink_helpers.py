"""Builders of contract objects for the sink tests: a run and trials with known ids, times and costs."""

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

from hbench.core.ids import trial_id
from hbench.core.models import Costs, GraderResult, RunInfo, Target, TrialResult, TrialStatus, Usage

RUN_ID = "0123456789abcdef0123456789abcdef"
STARTED = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)


def make_info(run_id: str = RUN_ID, finished: bool = True, **overrides: Any) -> RunInfo:
    fields: dict[str, Any] = {
        "run_id": run_id,
        "started_at": STARTED,
        "finished_at": STARTED + timedelta(minutes=5) if finished else None,
        "harness_bench_version": "0.0.0",
        "config_sha256": "c" * 64,
        "suite_version": "demo@1",
        "python": "3.12.0",
        "platform": "linux",
        "sandboxed": True,
        "targets": (Target(harness="claude", harness_version="1.0", model="fix", params={}),),
        "sandbox": "local-tmp",
    }
    return RunInfo(**{**fields, **overrides})


def make_trial(
    task_id: str = "fizzbuzz",
    harness: str = "claude",
    model: str = "fix",
    rep: int = 1,
    status: TrialStatus = TrialStatus.PASSED,
    cost: float | None = 0.5,
    run_id: str = RUN_ID,
    **overrides: Any,
) -> TrialResult:
    """A trial; `cost=None` is an unknown cost. The trial id is the one the engine computes."""
    target = Target(harness=harness, harness_version="1.0", model=model, params={})
    fields: dict[str, Any] = {
        "run_id": run_id,
        "trial_id": trial_id(run_id, task_id, f"{harness}/{model}", rep),
        "task_id": task_id,
        "suite_version": "demo@1",
        "target": target,
        "rep": rep,
        "status": status,
        "graders": (GraderResult(grader="hidden_tests", grader_version="1", passed=status is TrialStatus.PASSED),),
        "usage": Usage(
            input_tokens=100,
            output_tokens=20,
            cache_read_tokens=40,
            turns=2,
            parsed=True,
            costs=Costs(gateway=cost),
            tool_calls=3,
        ),
        "latency_s": 12.5,
        "started_at": STARTED + timedelta(seconds=30 * rep),
        "agent_exit_code": 0,
        "sandboxed": True,
        "trace_id": f"bench-20261124120000-{task_id}__{harness}__{model}__r{rep}",
    }
    return TrialResult(**{**fields, **overrides})


def with_usage(trial: TrialResult, **changes: Any) -> TrialResult:
    return dataclasses.replace(trial, usage=dataclasses.replace(trial.usage, **changes))

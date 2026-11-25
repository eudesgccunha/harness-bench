#!/usr/bin/env python3
"""Write a SQLite database of synthetic trials through the `sqlite` sink, for the example dashboard (ADR 0011).

Fixed seed: the same command writes the same rows (ids, outcomes, costs). The data is invented: three targets, six
tasks, three reps each; one target never reports a cost, so the dashboard shows an unknown cost as unknown.

Run from the repository root: python3 -m scripts.synthetic_db OUT.sqlite
"""

import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hbench.core.ids import trial_id
from hbench.core.models import Costs, GraderResult, RunInfo, Target, TrialResult, TrialStatus, Usage
from hbench.sinks.sqlite import SqliteSink

SEED = 7
RUN_ID = "5eed5eed5eed5eed5eed5eed5eed5eed"
STARTED = datetime(2026, 11, 1, 9, 0, 0, tzinfo=UTC)
TASKS = ("fizzbuzz", "path-traversal-fix", "extract-function", "dependency-upgrade", "html-report", "csv-parser")
REPS = 3
# harness, model, chance to solve a task, cost of a trial in USD (None: the harness reports no cost)
TARGETS: tuple[tuple[str, str, float, float | None], ...] = (
    ("claude", "large", 0.9, 0.80),
    ("opencode", "mid", 0.7, 0.30),
    ("cursor", "small", 0.5, None),
)


def _uniform(seed: int, *key: object) -> float:
    """A number in [0, 1) that depends only on the seed and the key: the same data on every run and every machine."""
    digest = hashlib.sha256("|".join(map(str, (seed, *key))).encode()).digest()
    return int.from_bytes(digest[:8]) / 2**64


def synthetic_trials(seed: int) -> list[TrialResult]:
    trials = []
    clock = STARTED
    for harness, model, solve_rate, cost in TARGETS:
        for task in TASKS:
            for rep in range(1, REPS + 1):
                key = (harness, model, task, rep)
                passed = _uniform(seed, *key, "passed") < solve_rate
                tokens = 20_000 + int(_uniform(seed, *key, "tokens") * 100_000)
                spread = 0.5 + _uniform(seed, *key, "cost")
                latency = round(30 + _uniform(seed, *key, "latency") * 570, 1)
                trials.append(
                    TrialResult(
                        run_id=RUN_ID,
                        trial_id=trial_id(RUN_ID, task, f"{harness}/{model}", rep),
                        task_id=task,
                        suite_version="synthetic@1.0.0",
                        target=Target(harness=harness, harness_version="1.0", model=model, params={}),
                        rep=rep,
                        status=TrialStatus.PASSED if passed else TrialStatus.FAILED,
                        graders=(GraderResult(grader="hidden_tests", grader_version="1", passed=passed),),
                        usage=Usage(
                            input_tokens=tokens,
                            output_tokens=tokens // 10,
                            turns=3 + int(_uniform(seed, *key, "turns") * 23),
                            parsed=cost is not None,
                            costs=Costs(gateway=None if cost is None else round(cost * spread, 4)),
                        ),
                        latency_s=latency,
                        started_at=clock,
                        agent_exit_code=0,
                        sandboxed=True,
                    )
                )
                clock += timedelta(seconds=latency)
    return trials


def write_database(path: Path, seed: int = SEED) -> int:
    """Write the synthetic run to `path` (a new database, or the same rows again in an existing one). Returns the
    number of trials."""
    trials = synthetic_trials(seed)
    info = RunInfo(
        run_id=RUN_ID,
        started_at=STARTED,
        finished_at=max(t.started_at + timedelta(seconds=t.latency_s) for t in trials),
        harness_bench_version="0.0.0",
        config_sha256="0" * 64,
        suite_version="synthetic@1.0.0",
        python="3.12",
        platform="synthetic",
        sandboxed=True,
        targets=tuple(t.target for t in {x.target.target_id: x for x in trials}.values()),
        sandbox="synthetic",
    )
    sink = SqliteSink(path)
    sink.open(info)
    for trial in trials:
        sink.write(trial)
    sink.close(info)
    return len(trials)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 -m scripts.synthetic_db OUT.sqlite", file=sys.stderr)
        return 2
    print(f"wrote {write_database(Path(argv[1]))} synthetic trials to {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

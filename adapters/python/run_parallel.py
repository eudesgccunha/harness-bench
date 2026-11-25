"""Run a benchmark with a pool of worker processes: the Python adapter of ADR 0012.

    PYTHONPATH=. python3 adapters/python/run_parallel.py --config config.json --workers 4 [--allow-unsandboxed]

It is `plan`, `run_trial` for each spec in a worker process, then `finish_run`. A worker gets the spec as text and
loads the config itself: nothing but the name of a trial crosses the process boundary. Packaged orchestrators (Airflow,
Prefect, Dagster, Temporal, Ray) wrap `run_one` the same way.
"""

import argparse
import json
import multiprocessing
import sys
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from hbench import api
from hbench.core.models import TrialResult


def run_one(config_path: str, spec_json: str, allow_unsandboxed: bool) -> str:
    """Run the trial one spec names and return its result as JSON. This is what a worker does."""
    config = api.load_config(config_path)
    spec = api.parse_spec(spec_json)
    return json.dumps(api.run_trial(config, spec, allow_unsandboxed=allow_unsandboxed).to_dict())


def run_parallel(
    config_path: Path | str,
    *,
    workers: int,
    only: Sequence[str] | None = None,
    reps: int | None = None,
    allow_unsandboxed: bool = False,
) -> list[TrialResult]:
    """Plan the run, run every trial in `workers` processes, finish the run and return the results in plan order."""
    config = api.load_config(config_path)
    run = api.start_run(config, only=only, reps=reps)
    specs = api.plan(config, run, only=only, reps=reps)
    # `spawn`: the engine starts processes and threads, which a forked worker would inherit half way through.
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = [
            pool.submit(run_one, str(config_path), json.dumps(spec.to_dict()), allow_unsandboxed) for spec in specs
        ]
        results = [TrialResult.from_dict(json.loads(future.result())) for future in futures]
    api.finish_run(config, run.run_id)
    return results


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run a harness-bench run with a pool of worker processes.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--reps", type=int)
    parser.add_argument("--only", nargs="*", help="only these harness names")
    parser.add_argument("--allow-unsandboxed", action="store_true", help="only inside a disposable VM")
    args = parser.parse_args(argv)
    try:
        results = run_parallel(
            args.config,
            workers=args.workers,
            only=args.only,
            reps=args.reps,
            allow_unsandboxed=args.allow_unsandboxed,
        )
    except api.ApiError as e:
        sys.exit(f"run_parallel: {e}")
    print(f"{sum(r.passed for r in results)} of {len(results)} trials passed")


if __name__ == "__main__":
    main()

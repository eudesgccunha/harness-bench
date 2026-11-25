# Integrating with an orchestrator

This guide assumes no orchestrator in particular. It covers how to run the trials of one benchmark run on many workers (processes, CI jobs, pods, tasks of Airflow, Prefect, Dagster, Temporal or Ray) and get the results `python3 -m hbench run` would give. Decisions: [ADR 0012](adr/0012-plan-and-run-trial-api.md).

## The three calls

An orchestrator only ever calls three things, as Python functions of [`hbench/api.py`](../hbench/api.py) or as CLI commands:

| Step | Python | CLI |
|---|---|---|
| Plan | `config = load_config(path)`, `run = start_run(config)`, `specs = plan(config, run)` | `python3 -m hbench plan --config C` prints one `TrialSpec` JSON per line |
| Run, once per spec, on any worker | `run_trial(config, spec)` returns the `TrialResult` | `python3 -m hbench run-trial --config C --spec -` reads the spec on stdin and prints the result |
| Finish | `finish_run(config, run.run_id)` | `python3 -m hbench finish --config C --run RUN_ID` |

`plan` is pure apart from `start_run`, which creates `<output_dir>/<run_id>/run.json` and `trials.jsonl`. The specs come in the order `run` executes them (pairs, tasks, repetitions). `plan --run RUN_ID` (CLI) or `open_run` (API) plans an existing run again. `finish` sets the finish time, closes the sinks with the finished run (the run span of the OTLP sink) and writes `summary.md` and `summary.csv`; `--no-report` skips them.

## What a spec is

```json
{"schema_version": "1.4.0", "run_id": "…32 hex…", "trial_id": "…16 hex…", "task_id": "fizzbuzz",
 "harness": "claude", "model": "fix", "rep": 1, "config_sha256": "…64 hex…"}
```

A spec names a trial. It carries **no command, prompt, path, env or secret**, so a spec that comes from a queue, a ConfigMap or a CI artifact cannot inject a command: the worker resolves everything from **its own copy of the config**. The JSON Schema of a spec is in `python3 -m hbench schema export` (`trial_spec`).

Before anything runs, `run_trial` checks that the spec fits the config and the run, and raises `SpecError` (the CLI exits with a message) when it does not, with nothing run and nothing written:

- `run_id`, `trial_id` and `config_sha256` have the right shape (a path-like `run_id` is refused), and there are no extra keys;
- `config_sha256` equals the hash of the worker's config (the same rule as `run --resume`), so every worker must use the config of the plan;
- the task, the harness and the pair exist in the config, and the `trial_id` recomputed from the config equals the spec's;
- the run directory exists and was started with this config;
- the trial is in the plan of the run: its target (harness, version, model) is one of the run's targets, so a harness left out with `--only` is refused, and its `rep` is at most the `reps` the run recorded in `run.json` (a run of an older version did not record them: the config's `reps` applies).

## Retries and idempotency

`run_trial` is idempotent by `trial_id`. If the trial is already in `trials.jsonl`, it returns that result and runs nothing, so an orchestrator may retry a spec freely. Each trial runs under a lock (`<run_dir>/.locks/<trial_id>.lock`, an `flock` held for the whole trial). If two workers get the same spec at once (a retry that raced the original), the second waits, finds the recorded result and returns it: the agent runs once. If the first died without recording, the second runs it. The append is also checked under the file lock, so a trial is never recorded twice. A worker killed in the middle leaves no record; run the spec again. Orphan work dirs and containers of a killed worker are in its own private dirs (a temp dir named `harness-bench-work-*` and `harness-bench-git-*`); a retry reclaims the container of the trial.

## A killed worker

A worker killed with SIGKILL (an OOM kill, a node loss) cannot clean up. It leaves a temp dir `harness-bench-work-*` (with the 0600 env file of the agent, which holds secrets, unless `work_dir` is set) and one `harness-bench-git-*` in its `TMPDIR`, and the lock file `<run_dir>/.locks/<trial_id>.lock` (empty, harmless: the lock itself goes with the process). `run --resume` cleans only what its own heartbeat names, so nothing does this for a worker. Give each worker its own `TMPDIR` (a pod `emptyDir`, a CI runner) so the leftovers go with it, or delete the `harness-bench-*` dirs of the temp dir when no worker is running. A retry of the spec reclaims the container of the trial.

## One trial, one process

Each `run_trial` makes its own private work dir and baseline git dir, its own sandbox session and its own sink feed (open, write, close), and removes them when it returns or raises. It is the same code as `run`, so every defense applies unchanged: the workdir outside the repository and without git history, the hardened git, the env allowlist, the protected paths, the audit diff, redacted logs, and the sandbox rule: **without a configured sandbox, `run_trial` refuses unless `allow_unsandboxed=True`** (`--allow-unsandboxed`), on every worker. The run heartbeat of `run --resume` belongs to `run`; a worker writes none.

## Several workers on one run directory

- The workers share `<output_dir>`: the appends to `trials.jsonl`, `results.jsonl` and the OTLP trace file take an exclusive `flock` (write, flush, `fsync`, unlock), so no line is torn. The SQLite sink waits up to 60 s for a locked database.
- **Workers on other machines need a shared filesystem where `flock` works** (a local disk, or NFS with locking). Without one, give each worker its own `output_dir`, then concatenate the `trials.jsonl` lines of the trials into the run directory (and the `results.jsonl` rows into `results.jsonl`) before `finish`: every `trial_id` is unique, so plain concatenation is enough. There is no merge tool. [`adapters/github-actions/benchmark.yml`](../adapters/github-actions/benchmark.yml) does exactly this.
- Run `finish` only after every trial has ended: it sets the finish time and writes the report from what is on disk. Run `reconcile` only after `finish` (it replaces `trials.jsonl` atomically, so a worker still appending would lose its line). Never run `reconcile` or `run --resume` on a run while workers are writing it.

## Input size

`run-trial` reads at most 64 KiB of spec (a spec is a few hundred bytes) and refuses more.

## Secrets

Secrets come from each worker's environment (`${VAR}` in the config), never from a spec. The LiteLLM spend key and the sink header values stay out of the agent's environment and out of the logs, as in `run`. Give a worker only the variables its config names.

## Examples

All are small and add no dependency to the engine. Run them from the repository root (they call `python3 -m hbench`).

| Adapter | What it does |
|---|---|
| [`adapters/shell/run.sh`](../adapters/shell/run.sh) | `plan`, then `xargs -P N` over the lines, each line to `run-trial` on stdin, then `finish`. |
| [`adapters/python/run_parallel.py`](../adapters/python/run_parallel.py) | The API with `concurrent.futures.ProcessPoolExecutor`: `PYTHONPATH=. python3 adapters/python/run_parallel.py --config config.json --workers 4`. Tested. |
| [`adapters/github-actions/benchmark.yml`](../adapters/github-actions/benchmark.yml) | A `plan` job, a matrix of `run-trial` jobs (the spec in the matrix, the run directory as an artifact), a `finish` job that merges the trial files. Copy it to `.github/workflows/`. |
| [`adapters/kubernetes/indexed-job.yaml`](../adapters/kubernetes/indexed-job.yaml) | An Indexed Job: pod N runs line N of the specs (a ConfigMap), results on a shared volume. |

Adapters for packaged orchestrators are a few lines over the same API and live outside this repository. The pattern:

```python
import json

from hbench import api


def plan_task(config_path: str) -> list[str]:  # one task: returns the specs as JSON texts
    config = api.load_config(config_path)
    run = api.start_run(config)
    return [json.dumps(spec.to_dict()) for spec in api.plan(config, run)]


def trial_task(config_path: str, spec_json: str) -> str:  # mapped over the specs, retried freely
    config = api.load_config(config_path)
    return json.dumps(api.run_trial(config, api.parse_spec(spec_json)).to_dict())


def finish_task(config_path: str, run_id: str) -> None:  # runs after every trial task
    api.finish_run(api.load_config(config_path), run_id)
```

`api.ApiError` is the base of every error the API raises (`ConfigError`, `SpecError`, `RunError`, `SandboxRequiredError`); the messages hold no secret.

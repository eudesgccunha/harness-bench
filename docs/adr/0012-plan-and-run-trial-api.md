# ADR 0012: `plan` / `run_trial` API for orchestrators (Phase 8)

- Status: accepted
- Date: 2026-11-25

## Context

Phase 8 of the [evolution plan](../evolution-plan.md) asks for a stable Python API,
`plan(...) -> list[TrialSpec]` and `run_trial(spec) -> TrialResult`, a serializable
`TrialSpec` that any worker can run, example adapters that are only a shell over
`run_trial`, and a guide that assumes no orchestrator in particular. Today `_run_matrix`
in `harness_bench.py` plans and runs every trial in one process, sharing a private work
dir, a private git dir, the run writer, the heartbeat, the sandbox and the sinks. The
runtime is the standard library only ([ADR 0001](0001-stdlib-runtime.md)), so adapters
cannot pull orchestrator packages into the engine.

## Decision

1. **Module `hbench/api.py`** is the stable surface (documented, typed, covered by tests
   that pin the signatures):
   - `load_config(path: Path) -> Config` (the expanded, validated config and its base dir)
   - `start_run(config: Config, *, only=None, reps=None) -> RunInfo`: creates
     `<output_dir>/<run_id>/run.json` and `trials.jsonl`, as `run` does today.
   - `plan(config: Config, run: RunInfo, *, only=None, reps=None) -> list[TrialSpec]`:
     pure, same order as `run` (pairs, tasks, reps).
   - `run_trial(config: Config, spec: TrialSpec, *, allow_unsandboxed=False) -> TrialResult`
   - `finish_run(config: Config, run_id: str, *, report=True) -> RunInfo`: sets
     `finished_at`, closes the sinks with the finished run, writes the report.
   The plan's `plan(suite, targets, reps)` becomes `plan(config, run, ...)`: suites and
   targets are already config keys (`suites`, `matrix`), so the config is the one input.
2. **`TrialSpec`** (contract object in `hbench/core/models.py`, frozen, JSON round-trip,
   in the JSON Schema; contract `1.4.0`, additive): `schema_version`, `run_id`,
   `trial_id`, `task_id`, `harness`, `model`, `rep`, `config_sha256`. It names a trial;
   it carries **no** command, prompt, path, env or secret. A worker resolves everything
   from its own copy of the config, so a spec from a queue cannot inject a command.
3. **`run_trial` validates the spec before anything runs:** `run_id` and `trial_id` match
   their patterns; the config hash equals `config_sha256` (same rule as `--resume`); the
   task, harness and pair exist in the config; the `trial_id` recomputed from the config
   equals the spec's; the run dir exists. Any mismatch is a `SpecError` (CLI: exit with
   a message), nothing runs.
4. **Idempotent by `trial_id`:** if the trial is already in `trials.jsonl`, `run_trial`
   returns that result and runs nothing (an orchestrator retry is safe).
5. **One trial, one process, its own private dirs:** `run_trial` makes its own private
   work dir and baseline git dir (with the owner token), its own sandbox session and its
   own sink feed (open, write, close per trial; the run span is written by `finish_run`).
   Every defense of `_run_trial` applies unchanged: `_run_trial` is the single
   implementation. Amended during implementation: `run` keeps its own loop (shared work
   dir, run heartbeat, one sink feed, `--keep`, `work_dir`, resume cleanup) and calls the
   same `_run_trial` and the same planning helpers; it is not routed through
   `api.run_trial`. An acceptance test and a smoke case check that `run`, the CLI, a
   script over the API and the Python adapter record the same trials.
   A spec must also be in the run's plan: its target is one of `RunInfo.targets` (which
   reflects `--only`) and its `rep` is at most the run's `reps` (recorded in `run.json`).
   A per-trial lock (`<run_dir>/.locks/<trial_id>.lock`, `flock`) is held for the whole
   trial, so two workers given the same spec at once never run it twice; the second waits
   and returns the recorded result. Leftover containers are reclaimed only under that
   lock.
6. **Concurrent workers on one run dir:** appends to `trials.jsonl` and `results.jsonl`
   take an exclusive `fcntl.flock` on the file (write, flush, fsync, unlock); the SQLite
   sink uses a busy timeout; the OTLP file append takes the same lock. Workers on other
   machines need a shared filesystem with working `flock`, or each writes to its own
   output dir and the user merges (documented; no merge tool in this phase). The run
   heartbeat stays a single-process `run`/`--resume` feature: `run_trial` writes a
   per-trial heartbeat only to its log, not the run heartbeat.
7. **CLI:** `python3 -m hbench plan --config C [--only ...] [--reps N] [--run RUN_ID]`
   starts the run (or reuses `RUN_ID`) and prints one `TrialSpec` JSON per line;
   `python3 -m hbench run-trial --config C [--allow-unsandboxed] (--spec FILE | -)` runs
   one spec and prints the `TrialResult` JSON; `python3 -m hbench finish --config C
   --run RUN_ID [--no-report]`.
8. **Adapters** in `adapters/`, none with a dependency: `adapters/shell/` (`xargs -P`
   over `plan` output), `adapters/github-actions/` (a workflow that runs `plan`, fans out
   a matrix of `run-trial` jobs with the specs as artifacts, then `finish`),
   `adapters/kubernetes/` (an Indexed Job manifest where each pod runs the spec at its
   index) and `adapters/python/` (a `concurrent.futures` example over the API, tested).
   Adapters for packaged orchestrators (Airflow, Prefect, Dagster, Temporal, Ray) are a
   few lines over the same API and live outside this repository; the guide shows the
   pattern.
9. **Guide:** `docs/orchestrator-integration.md`: the three calls, idempotency, the spec
   carries no command, the shared-filesystem requirement, secrets come from each
   worker's env, the sandbox rule applies per worker.

## Consequences

- An orchestrator only ever calls `plan`, `run_trial`, `finish_run` (or the three CLI
  commands); `run` is the same code in one process, so results do not depend on the
  caller (the acceptance).
- The engine never imports an orchestrator.

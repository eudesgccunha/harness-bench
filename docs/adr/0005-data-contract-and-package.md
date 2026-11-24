# ADR 0005: Data contract, `hbench` package and single CLI

- Status: accepted
- Date: 2026-11-24

## Context

Phase 1 of the [evolution plan](../evolution-plan.md) adds a versioned data contract
(`core/`), structured output per run, a Markdown report derived from that contract, a
single CLI and a JSON Schema export. Constraints: stdlib runtime
([ADR 0001](0001-stdlib-runtime.md)), incremental extraction with the two scripts kept as
entry points ([ADR 0002](0002-incremental-extraction.md)), golden tests unchanged.

## Decision

1. **Package name `hbench/`, not `harness_bench/`.** A directory `harness_bench/` next to
   `harness_bench.py` shadows the module on import (packages win in the same path
   entry), which would break the shim, the tests (`import harness_bench as hb`) and the
   wrapper. The engine stays in `harness_bench.py` in Phase 1; only new code goes into
   `hbench/`.
2. **Contract in `hbench/core/`.** Frozen, `kw_only` dataclasses validated in
   `__post_init__`; `to_dict()` / `from_dict()` reject unknown fields. `SCHEMA_VERSION =
   "1.0.0"`. `run_id` is a UUID4 hex; `trial_id` is the first 16 hex chars of
   `sha256(run_id|task_id|target_id|rep)`.
3. **Statuses.** `passed` when the grader passed; `timeout` when the agent or the test
   timed out (and it did not pass); `failed` otherwise. `error` exists in the contract
   for infrastructure failures, but Phase 1 has no producer yet: the engine does not
   check the setup step today, and M6 (failure breakdown) owns that classification.
4. **Costs.** A trial stores every known cost (`gateway`, `price_table`, `harness`;
   `None` when unknown) plus the picked one (`cost_usd`, `cost_source`, order
   gateway > price_table > harness). The report can still force one source
   (`--cost-source`).
5. **Files.** Each `run` invocation writes `<output_dir>/<run_id>/run.json` and
   `<output_dir>/<run_id>/trials.jsonl`. The legacy `<output_dir>/results.jsonl` is still
   appended: resume (`done_keys`), `reconcile` and old result folders keep working.
   `reconcile` also writes gateway costs into every `trials.jsonl` it matches by
   `trace_id` (the LiteLLM run tag).
6. **Report from the contract.** `report` converts each legacy row into a `TrialResult`
   and aggregates over `TrialResult` objects. The golden `summary.md`/`summary.csv` stay
   byte-identical.
7. **No raw content in results.** Trials keep references only: `artifact_uri` is the log
   path prefix relative to the output dir (`logs/<tag>`).
8. **Single CLI** in `hbench/cli.py`, run as `python3 -m hbench`
   (`run`, `report`, `reconcile`, `schema export`). `--litellm` applies the LiteLLM
   profile (drop `parser: cursor`, force LiteLLM cost, wait and reconcile after `run`).
   `harness_bench.py` and `harness_bench_litellm.py` keep their commands and flags and
   delegate to it. No console-script entry point: uv runs the project with
   `package = false`, and the runtime installs nothing.
9. **JSON Schema written by hand** in `hbench/core/schema.py`, with a small stdlib
   validator covering the keywords the schema uses. A test fails if the schema and the
   dataclass fields drift apart.

## Consequences

- Data consumers read `trials.jsonl` + `run.json` and validate them with
  `python3 -m hbench schema export`.
- `results.jsonl` is legacy and is removed only in a later phase with a migration note.
- Module moves from `harness_bench.py` into `hbench/` continue in later phases
  (runners and graders in Phase 3).

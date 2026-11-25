# ADR 0011: Sinks and telemetry (Phase 7)

- Status: accepted
- Date: 2026-11-24

## Context

Phase 7 of the [evolution plan](../evolution-plan.md) asks for result sinks (`jsonl` and
`markdown` by default; `postgres`, `s3`, `parquet` as adapters), a reference SQL schema
with scorecard queries, optional OpenTelemetry spans without prompt or response content,
the `run_id` on the gateway call, and an example dashboard with synthetic data. Roadmap M13
(trace export) is scheduled here. The runtime is the Python 3.12 standard library only
([ADR 0001](0001-stdlib-runtime.md)), so a Postgres driver, an S3 client, a Parquet writer
and the OpenTelemetry SDK cannot be runtime dependencies. `TrialResult.trace_id` already
exists in the contract and is always `None` today.

## Decision

1. **`trials.jsonl` stays the source of truth** and is always written, before any sink,
   as today ([ADR 0010](0010-pluggable-sandbox-and-resume.md) §8). The markdown report
   is derived from it as today. These two are the default "sinks" and do not move behind
   the new protocol.
2. **`Sink` protocol** in `hbench/sinks/base.py`: `open(info: RunInfo) -> None`,
   `write(trial: TrialResult) -> None`, `close(info: RunInfo) -> None`. A sink receives
   only contract objects, which never hold artifact content, agent output or the judge's
   `reason` (AGENTS.md), so no sink can leak them. Every write must be idempotent by
   `trial_id` (resume and replay send a trial again).
3. **Built-in sinks** (stdlib only):
   - `sqlite` (`sqlite3`): tables `runs`, `trials`, `grader_results` from
     `docs/sql/schema.sql`; upsert by `run_id` / `trial_id`. It is the reference
     database sink and the one the acceptance test uses.
   - `otlp`: OpenTelemetry traces in the OTLP/JSON encoding, written to a file
     (`traces.otlp.jsonl` in the run directory, one `ExportTraceServiceRequest` per line)
     and, only when `endpoint` is set, also sent as `POST <endpoint>/v1/traces` with
     `Content-Type: application/json` (OTLP/HTTP). No OpenTelemetry package is used, so
     there is nothing to install and nothing is a no-op by accident.
   - `postgres`, `s3`, `parquet` and any other sink are third-party plugins through the
     entry-point group `harness_bench.sinks` (`hbench/plugins.py`). The repository ships
     none of them: they need drivers. `docs/sql/schema.sql` is portable SQL (it runs on
     SQLite and PostgreSQL), so a Postgres plugin can reuse it.
4. **Config:** `sinks: [{"type": "sqlite", "path": "results/bench.sqlite"},
   {"type": "otlp", "endpoint": "https://collector:4318", "headers": {"x-key": "${VAR}"},
   "timeout": 10}]`. Paths are relative to the config. Header values that carry a secret
   must be exactly `${VAR}` (same rule as `judge.api_key`), so the redaction knows them.
   Unknown keys and unknown types are config errors.
5. **Engine wiring:** sinks are opened after `run.json`, each trial is written to every
   sink right after it is appended (and fsynced) to `trials.jsonl`, and sinks are closed
   with the run. A sink that raises does not abort the run and does not change any
   result: the engine warns once per sink, stops using that sink for the run, and the
   user replays later. `python3 -m hbench export --config C --run <run_dir>` replays a
   finished or interrupted run from `run.json` and `trials.jsonl` into the configured
   sinks (idempotent).
6. **Spans:** `trace_id` = the `run_id` (32 hex, already the right size), the run span id
   = the first 16 hex of `sha256("run:" + run_id)`, each trial span id = the `trial_id`
   (16 hex, already the right size), parent = the run span. Amended during implementation:
   `TrialResult.trace_id` keeps the run tag it already carries (`reconcile` matches spend
   logs by it), and is not replaced by the `run_id`; the OTLP trace id is the `run_id`,
   which every trial already records.
   Attributes: `eval.run_id`, `eval.trial_id`, `eval.target_id` (`harness/model`),
   `eval.task_id`, `eval.rep`, `eval.status`, `eval.passed`, `eval.suite_version`,
   `eval.sandbox`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
   `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens` when known, and
   `eval.cost_usd` when known (unknown stays absent, never 0). **No prompt, response,
   transcript, artifact or log content** goes into a span, ever (there is no option to
   turn it on in this phase). Span start and end come from `started_at` and `latency_s`.
   Status `ERROR` for a trial whose status is `error`, else `UNSET`.
7. **Network rule, opt-in:** the engine may also `POST <otlp.endpoint>/v1/traces`, only when
   an `otlp` sink has an `endpoint`. Same URL rule as `check_litellm_url` (https, or http
   only for localhost), the opener that refuses redirects, a timeout, a 1 MB cap on the
   reply that is read and then discarded. AGENTS.md is updated.
8. **`run_id` on the gateway call:** the engine already exports `BENCH_TAG` (the run tag)
   and the harnesses send it in `x-litellm-tags`; the trial and the sinks carry the run
   tag, so spend logs join on it. The engine also exports `BENCH_RUN_ID` and
   `BENCH_TRIAL_ID` to the agent environment (allowlisted names, not secrets), so a
   harness config can send them as extra headers; the example harness configs and
   `sandbox/` do not change in this phase, and nothing depends on a harness sending them.
   `docs/sql/scorecard.sql` shows the join on the run tag.
9. **Reference SQL** in `docs/sql/`: `schema.sql` and `scorecard.sql` (pass rate, pass^k
   per target, cost per success per target, unknown cost kept unknown). A test runs both
   on the SQLite sink output.
10. **Example dashboard:** `docs/dashboards/grafana-scorecard.json` for Grafana with the
    SQLite data source plugin, panels driven by the `scorecard.sql` queries, and
    `scripts/synthetic_db.py` that writes a database of synthetic trials (fixed seed). A
    test checks the JSON loads and each panel query runs on the synthetic database.

## Consequences

- No new runtime dependency; a database and traces come from the standard library.
- The network rule widens only when the user configures an OTLP endpoint.
- Postgres, S3 and Parquet are a plugin away and reuse the reference schema.

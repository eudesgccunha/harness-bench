# Phase 7: Sinks and Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `Sink` protocol with stdlib `sqlite` and `otlp` sinks, plugin sinks, an `export` replay command, reference SQL, `BENCH_RUN_ID`/`BENCH_TRIAL_ID` in the agent env, and an example dashboard with synthetic data.

**Architecture:** `hbench/sinks/` (protocol, registry, `sqlite.py`, `otlp.py`); the engine feeds sinks after each `trials.jsonl` append; `hbench/cli.py` gains `export`. Decisions: [ADR 0011](../../adr/0011-sinks-and-telemetry.md).

**Tech Stack:** Python 3.12 stdlib (`sqlite3`, `json`, `hashlib`, `urllib.request`).

**Spec:** [`docs/evolution-plan.md`](../../evolution-plan.md) Phase 7; roadmap M13.

## Global Constraints

- Python 3.12, stdlib only at runtime. No `shell=True`. Unknown cost/usage is `None`/absent, never 0.
- `trials.jsonl` written first and unchanged in format; goldens unchanged; no sink configured means behavior identical to today.
- Network: only the existing GET (reconcile, doctor) and judge POST, plus `POST <otlp.endpoint>/v1/traces` when configured, through `check_litellm_url` and the no-redirect opener, with a total deadline and a 1 MB reply cap.
- No artifact, prompt, response, transcript or log content in any sink or span.
- Every AGENTS.md defense stays; smoke keeps every case; a new defense gets a new case. `sandbox/` untouched.
- Type hints everywhere, mypy strict, ruff ANN, bandit; no new ignores. Coverage `fail_under = 100`.
- Fake secrets built at runtime; gitleaks passes. Targeted Edit only, NEVER read+overwrite a whole file. No commits.

## Review Focus

- A sink that raises (disk full, locked DB, collector 500/timeout/redirect): the run finishes, `trials.jsonl`, `results.jsonl` and the report are identical to a run without that sink; one warning. Test in Task 4.
- Resume and `export` replay twice: each `trial_id` exactly once in SQLite and once per span id in the OTLP file. Test in Tasks 2, 3, 4.
- OTLP endpoint `http://` non-local, or a redirect: refused at config validation / not followed. A header value with a literal secret (not exactly `${VAR}`): config error. The header secret never appears in any log or file. Test in Task 3.
- A trial whose fields hold SQL-like text (task id `x'); DROP TABLE trials;--`): parameterized queries only. Test in Task 2.
- Span content: a test asserts the exact attribute key set and that no attribute value contains the agent's stdout or a grader log line from the fake run. Test in Task 3.

---

### Task 1: Sink protocol, registry, config, plugins

**Files:** Create `hbench/sinks/__init__.py` (`sinks_for(cfg, base) -> list[Sink]`, validation), `hbench/sinks/base.py`; modify `hbench/plugins.py` (group `harness_bench.sinks`), `harness_bench.py` (`validate_config` validates `sinks`). Test `tests/test_sinks.py`.

- [x] Step 1: failing tests — unknown type, unknown key, non-list, plugin sink loads, header not exactly `${VAR}` refused. Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 2: `sqlite` sink and reference SQL

**Files:** Create `hbench/sinks/sqlite.py`, `docs/sql/schema.sql`, `docs/sql/scorecard.sql`. Test `tests/test_sinks.py` (or `tests/test_sqlite_sink.py`).

- [x] Step 1: failing tests — tables created from `schema.sql`; upsert idempotent; parameterized (Review Focus 4); `None` costs stay NULL; every `scorecard.sql` query runs and gives known values on a fixture run (pass rate, pass^k, cost per success; unknown cost not 0). Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 3: `otlp` sink

**Files:** Create `hbench/sinks/otlp.py`; extend `tests/fake_litellm.py` (or a new `tests/fake_collector.py`) with `POST /v1/traces` recording bodies and scripted failures. Test `tests/test_otlp_sink.py`.

- [x] Step 1: failing tests — ids per ADR §6; parent link; attribute key set exact; unknown values absent; status mapping; times from `started_at`/`latency_s`; file one request per line; endpoint POST with headers from env; URL rule and no redirect; timeout; reply cap; Review Focus 3 and 5. Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 4: Engine wiring, `trace_id`, env vars, `export`

**Files:** Modify `harness_bench.py` (open/write/close sinks around the trial append; warn once and drop a failing sink; `TrialResult.trace_id` = run_id when an `otlp` sink is configured; `BENCH_RUN_ID`, `BENCH_TRIAL_ID` in the agent env), `hbench/cli.py` (`export --config C --run DIR`). Tests `tests/test_run.py`, `tests/test_sinks.py`, `tests/test_cli.py`; smoke case: a run with both sinks and the fake collector, `trial_id` joins JSONL, SQLite and spans (the acceptance).

- [x] Step 1: failing tests — Review Focus 1 and 2; acceptance join; env vars present in the agent env and the spend key still absent; `export` replay idempotent; goldens unchanged without sinks. Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 5: Dashboard, synthetic data, docs

**Files:** Create `docs/dashboards/grafana-scorecard.json`, `scripts/synthetic_db.py` (fixed seed, writes a SQLite DB through the `sqlite` sink); tests that the dashboard JSON loads and each panel `rawSql` runs on the synthetic DB. Docs: `AGENTS.md` (network rule, Map, sinks rule), `CONTEXT.md` (sink, span), `scripts/build_md.py` (+ regenerate), `config.example.json` (`_comment` with a `sinks` example), `docs/running-a-benchmark.md`, `docs/architecture.md`, `docs/README.md`, `docs/roadmap.md` (M13), `docs/evolution-plan.md` (tick Phase 7, link plan/ADR).

- [x] Step 1: failing tests. Step 2: FAIL. Step 3: implement. Step 4: docs. Step 5: `make check` green; gitleaks passes.

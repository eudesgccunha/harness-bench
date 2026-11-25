# Phase 8: Orchestrator API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A stable `hbench/api.py` (`load_config`, `start_run`, `plan`, `run_trial`, `finish_run`), a serializable `TrialSpec` (contract 1.4.0), CLI `plan` / `run-trial` / `finish`, `run` rebuilt on the same calls, concurrent-safe appends, dependency-free adapters and an integration guide.

**Architecture:** `_run_trial` in `harness_bench.py` stays the only trial implementation; `hbench/api.py` wraps it with per-trial private dirs; `_run_matrix` becomes start + plan + run each + finish. Decisions: [ADR 0012](../../adr/0012-plan-and-run-trial-api.md).

**Tech Stack:** Python 3.12 stdlib (`fcntl`, `concurrent.futures`, `json`).

**Spec:** [`docs/evolution-plan.md`](../../evolution-plan.md) Phase 8.

## Global Constraints

- Python 3.12, stdlib only at runtime. No `shell=True`. Unknown is `None`, never 0.
- Every AGENTS.md defense stays and applies to `run_trial` exactly as to `run`; smoke keeps every case; a new defense gets a new case.
- `run` output unchanged: goldens unchanged (never `HB_UPDATE_GOLDEN`). `--resume`, heartbeat, sinks, `export`, `suite validate --run` keep working.
- A `TrialSpec` carries no command, prompt, path, env or secret.
- No new network. `sandbox/` untouched. Adapters add no dependency to the engine.
- Type hints everywhere, mypy strict, ruff ANN, bandit; no new ignores. Coverage `fail_under = 100`.
- Fake secrets built at runtime; gitleaks passes. Targeted Edit only, NEVER read+overwrite a whole file. No commits.

## Review Focus

- A forged spec: wrong `config_sha256`, unknown task/harness/pair, `trial_id` that does not match the recomputed one, path-like `run_id`, extra keys, a run dir that does not exist: `SpecError`, nothing runs, no file written. Test in Task 2.
- `run_trial` twice with the same spec: second returns the first result, runs nothing. Test in Task 2.
- Four workers in parallel (processes) on one run dir: every `trial_id` once in `trials.jsonl`, every key once in `results.jsonl`, no torn line. Test in Task 3.
- `run` via the CLI, a script over the API, and the `adapters/python` example give the same trials (masked volatile fields) on the fake agents. Test in Task 4 (the acceptance).
- `run_trial` without a sandbox and without `allow_unsandboxed`: refused. Test in Task 2.

---

### Task 1: `TrialSpec` and contract 1.4.0

**Files:** Modify `hbench/core/models.py`, `hbench/core/schema.py` (`TrialSpec`, `SCHEMA_VERSION = "1.4.0"`, older data loads), tests `tests/test_core_models.py`, `tests/test_core_schema.py`.

- [x] Step 1: failing tests — round-trip, field validation, extra keys refused, schema export contains `TrialSpec`. Step 2: FAIL. Step 3: implement. Step 4: `make check` green; goldens unchanged.

### Task 2: `hbench/api.py` and `run_trial`

**Files:** Create `hbench/api.py`; modify `harness_bench.py` (factor what `_Matrix` needs so one trial can run with its own private work/git dirs, sandbox session and sink feed; `_run_trial` unchanged in behavior). Test `tests/test_api.py`.

- [x] Step 1: failing tests — every Review Focus 1, 2, 5 case; the result equals what `run` records for the same trial; private dirs removed after the trial (also on exception). Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 3: Concurrent-safe appends and `run` on the API

**Files:** Modify `hbench/results.py` (`flock` around appends to `trials.jsonl`), `harness_bench.py` (`results.jsonl` append under `flock`; `_run_matrix` = `start_run`/`open_resumed_run` + `plan` + `run_trial` loop + `finish_run`, keeping resume, heartbeat, sinks, dry run, `done_keys` skip), `hbench/sinks/otlp.py` (file append under the same lock), `hbench/sinks/sqlite.py` (busy timeout). Test `tests/test_api.py` (4 processes, Review Focus 3).

- [x] Step 1: failing tests. Step 2: FAIL. Step 3: implement. Step 4: `make check` green; goldens unchanged.

### Task 4: CLI, adapters, guide

**Files:** Modify `hbench/cli.py` (`plan`, `run-trial`, `finish`); create `adapters/README.md`, `adapters/shell/run.sh` (`set -euo pipefail`, `xargs -P`, no `eval`), `adapters/github-actions/benchmark.yml` (pinned action SHAs as in `.github/workflows/ci.yml`; not under `.github/workflows`, so it does not run here; actionlint it in `make check` if cheap), `adapters/kubernetes/indexed-job.yaml`, `adapters/python/run_parallel.py` (typed, covered by tests); `docs/orchestrator-integration.md`. Docs: `AGENTS.md` (Map, API rule), `CONTEXT.md` (TrialSpec), `scripts/build_md.py` (+ regenerate), `docs/architecture.md`, `docs/README.md`, `docs/running-a-benchmark.md`, `docs/evolution-plan.md` (tick Phase 8, link plan/ADR). Tests `tests/test_cli.py`, `tests/test_api.py` (acceptance: CLI vs API vs adapter), `tests/smoke.py` (case: `plan` | `run-trial` per line | `finish` gives the same solved/runs as `run`).

- [x] Step 1: failing tests. Step 2: FAIL. Step 3: implement. Step 4: docs. Step 5: `make check` green; gitleaks passes; `bash -n adapters/shell/run.sh`.

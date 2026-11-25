# Phase 6: Pluggable Sandbox, Limits, Resume and Doctor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `Sandbox` protocol with `prefix`, `docker` and `local-tmp`; per-trial limits and a no-network default; process-group cleanup; resume by `trial_id` with a heartbeat; image digest in results; a `doctor` command.

**Architecture:** `hbench/sandboxes/` builds argv and manages lifecycle; the engine keeps running processes. Resume and heartbeat live in `hbench/results.py` and the engine loop. Decisions: [ADR 0010](../../adr/0010-pluggable-sandbox-and-resume.md).

**Tech Stack:** Python 3.12 stdlib (`subprocess`, `os.killpg`, `resource`, `threading`, `signal`); Docker for the `docker` sandbox and `make docker-e2e`.

**Spec:** [`docs/evolution-plan.md`](../../evolution-plan.md) Phase 6; roadmap M7, M8, M15.

## Global Constraints

- Python 3.12, stdlib only at runtime. No `shell=True`.
- Every AGENTS.md defense stays; `tests/smoke.py` keeps every case; a new defense gets a new case.
- `sandbox.prefix` configs produce byte-identical argv; goldens unchanged.
- Network: no new destinations; `doctor --network` only GETs the configured LiteLLM.
- Unit tests stay offline and need no Docker: use a fake `docker` executable on `PATH` (in `tests/`) that records argv. `make docker-e2e` is the real-Docker check; run it (Docker 29 is available) and report the result; if the image build needs network that is unavailable, say so.
- Type hints everywhere, mypy strict, ruff ANN, bandit; no new ignores. Coverage `fail_under = 100`.
- Fake secrets built at runtime; gitleaks passes. Targeted Edit only. No commits.

## Review Focus

- Docker image given as a tag (`img:latest`) or a malformed digest: config error before any run. Test in Task 2.
- A process the agent forks into the background (`sleep 600 &`) is gone after the agent step, and cannot touch the workdir during grading. Smoke case in Task 3.
- Kill the run with SIGKILL right after a trial is written to `trials.jsonl` but before its legacy row: `--resume` neither repeats nor loses the trial. Test in Task 4.
- `--resume` while another process holds a fresh heartbeat: refused; `--force` overrides. Test in Task 4.
- `--resume` with a `run_id` that does not exist, or a path-like `run_id` (`../x`): clean error. Test in Task 4.

---

### Task 1: Sandbox protocol, `prefix`, `local-tmp`, plugins

**Files:** Create `hbench/sandboxes/__init__.py` (registry `sandbox_for(cfg)`), `base.py`, `prefix.py`, `local_tmp.py`; modify `hbench/plugins.py` (group `harness_bench.sandboxes`), `harness_bench.py` (`wrap()` delegates to the session; `cmd_run` creates a session per trial and destroys it in `finally`; `is_sandboxed`/`require_sandbox` use the sandbox type). Test `tests/test_sandboxes.py`.

- [x] Step 1: failing tests — `prefix` argv identical to today's `wrap()` for the example config; `local-tmp` refuses without `--allow-unsandboxed`; plugin sandbox loads; `destroy` always called (exception in the agent step).
- [x] Step 2: FAIL. Step 3: implement. Step 4: `make check` green; goldens unchanged.

### Task 2: `docker` sandbox and limits

**Files:** Create `hbench/sandboxes/docker.py`, `tests/fake_docker.py` (+ a tiny launcher on `PATH` in tests); modify `validate_config`. Test `tests/test_sandboxes.py`.

**Interfaces:** argv exactly per ADR 0010 §4; `image_digest()` returns the `sha256:` part; `destroy()` runs `docker rm -f` for the session's container names through the engine's `run_proc` (not a shell).

- [x] Step 1: failing tests — argv for default and full `limits`; `--network none` default; tag image rejected; bad digest rejected; bad limits rejected (`cpus <= 0`, `memory` not matching `^\d+[kmg]$`, `pids < 1`); `destroy` calls `rm -f` with the right names.
- [x] Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 3: Process-group cleanup and local rlimits

**Files:** Modify `harness_bench.py` (`run_proc`: `start_new_session=True`, `os.killpg(pgid, SIGKILL)` after exit and on timeout, ignore `ProcessLookupError`), `hbench/sandboxes/local_tmp.py` (`preexec_fn` setting rlimits from `limits`), `tests/fake_agent.py` (mode `fork-bg`: starts a background child that keeps writing to a file in the workdir), `tests/smoke.py` (case: the background child is dead after the agent step; the file stops changing before grading).

- [x] Step 1: failing smoke/test. Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 4: Resume by `trial_id` and heartbeat

**Files:** Modify `hbench/results.py` (`RunWriter.open_existing(out_dir, run_id) -> RunWriter`, `finished_trial_ids() -> set[str]`, fsync on append; `Heartbeat` class with `start()/beat(trial_id, phase)/stop()`), `harness_bench.py` (`run --resume <run_id> [--force]`; trial appended before the legacy row; cleanup of leftovers), `hbench/cli.py`. Test `tests/test_resume.py` (subprocess run with a slow fake agent, SIGKILL after the first trial line appears, then `--resume`; assert every expected `trial_id` exactly once in `trials.jsonl` and every key once in `results.jsonl`).

- [x] Step 1: failing tests for every Review Focus 3–5 case and the acceptance test.
- [x] Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 5: Image digest and contract 1.3.0

**Files:** Modify `hbench/core/models.py`, `schema.py` (`RunInfo.sandbox: str`, `SCHEMA_VERSION = "1.3.0"`, 1.0–1.2 data loads with `sandbox = "unknown"`), `harness_bench.py` (`TrialResult.image_digest` from the sandbox), tests.

- [x] Step 1: failing tests. Step 2: FAIL. Step 3: implement. Step 4: `make check` green; goldens unchanged (check that `run.jsonl` golden holds no `RunInfo`).

### Task 6: `doctor`, docs and docker-e2e

**Files:** Create `hbench/doctor.py` (`Check(name, ok, detail)`, `run_checks(cfg, base, network: bool) -> list[Check]`); modify `hbench/cli.py` (`doctor`), `config.example.json` (keep `prefix` example; add a commented `docker` alternative under `_comment`, or a new `config.docker.example.json`), `tests/docker_e2e.py` (use the `docker` sandbox type with a digest of the locally built image), `scripts/build_md.py`, `AGENTS.md` (defenses: process-group kill, docker limits and no-network default; Map), `CONTEXT.md`, `docs/machine-setup.md` (internal network for the gateway allowlist), `docs/running-a-benchmark.md`, `docs/roadmap.md` (M7, M8, M15), `docs/evolution-plan.md` (tick Phase 6, link plan/ADR), `docs/README.md`, `docs/architecture.md`. Test `tests/test_doctor.py` (fake docker; fake LiteLLM for `--network`).

- [x] Step 1: failing tests — each check ok/fail; `--network` does exactly one health GET and one spend-logs GET; exit code. Step 2: FAIL. Step 3: implement. Step 4: docs. Step 5: `make check` green; `make docker-e2e` result reported.

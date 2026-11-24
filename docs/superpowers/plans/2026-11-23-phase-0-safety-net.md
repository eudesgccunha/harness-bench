# Phase 0 Safety Net Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze current engine behavior with golden tests and add CI, lint and secret scanning, so later phases can refactor safely.

**Architecture:** Characterization tests drive the existing CLI as a subprocess, the same way `tests/smoke.py` does, and compare outputs to committed golden files. Tool config lives in a `pyproject.toml` without runtime dependencies. CI is one GitHub Actions workflow.

**Tech Stack:** Python 3.12 standard library (`unittest`, `subprocess`, `json`, `csv`), GitHub Actions, ruff, mypy, bandit, gitleaks, pre-commit (dev only).

**Spec:** [`docs/evolution-plan.md`](../../evolution-plan.md) Phase 0, PRs 1–2 of section 5. Decisions: [`docs/adr/`](../../adr/). Current structure: [`docs/architecture.md`](../../architecture.md).

> **Note (2026-11-24):** dev tooling moved to uv, `uv.lock` and a `Makefile` per [ADR 0004](../../adr/0004-uv-dev-tooling.md). Where this plan says "scratchpad venv" or `pip install`, read `make sync` and `make check`.

## Global Constraints

- Work on branch `develop`. **Do not commit** unless the user explicitly orders it. Where a task ends, stop and report the changed files instead.
- Python 3.12; runtime imports only the standard library ([ADR 0001](../../adr/0001-stdlib-runtime.md)).
- New tests are `unittest` in `tests/test_*.py` ([ADR 0003](../../adr/0003-test-runner.md)).
- No change to engine behavior in this plan. `harness_bench.py` changes only in Task 5 (formatting), and the goldens must stay identical.
- No `shell=True`. Everything in English. Unknown cost is `None`, never `0`.
- No Claude attribution lines in any commit, if commits are ordered.
- Done when: `python3 tests/smoke.py` ends with `PASS`, `python3 -m unittest discover -s tests -p 'test_*.py'` is OK, and `python3 scripts/build_md.py` regenerated `HARNESS_BENCH.md`.

## Review Focus

1. Wall time and timestamps make goldens flaky: `summary.md` line 1 and run fields `ts`, `wall_s`, `run_tag`, `t_start`, `t_end` must be masked; the report golden uses a fixed fixture, so `avg_wall_s` is stable there.
2. `tests/fake_agent.py` raises `KeyError` when `BENCH_TAG` is set and `FAKE_TAGS_FILE` is not: the golden run config must pass `FAKE_TAGS_FILE` in each harness `env`.
3. CI runners need `git` with no global identity; the engine passes `-c user.name=...` already, but test helpers that commit must do the same.
4. bandit skips must not hide a real `shell=True`: skip only B404, B603, B310, never B602. Accepted deviation: B607 is also skipped, because only `git` is called by name (PATH is operator-controlled, not the agent's).
5. gitleaks flags test keys (`sk-test-agent-key-0123456789` in `tests/smoke.py`): allowlist by path `tests/`, not by rule.

---

### Task 1: Golden report test

**Files:**
- Create: `tests/test_golden.py`
- Create: `tests/golden/results.jsonl`, `tests/golden/summary.md`, `tests/golden/summary.csv`

**Interfaces:**
- Produces: `tests/test_golden.py` with helpers `run_cli(tmp: Path, script: str, *args: str, config: str = "config.json") -> subprocess.CompletedProcess`, `mask_summary(md: str) -> str` (replaces line 1 with `# Harness x model — <masked>`), `assert_golden(self, name: str, actual: str) -> None` (writes `tests/golden/<name>` when env `HB_UPDATE_GOLDEN=1`, else `assertEqual`).

- [x] **Step 1: Write fixture `tests/golden/results.jsonl`** with 6 rows over 3 targets and 2 tasks. Required row fields (the ones `aggregate` reads): `task, harness, model, rep, passed, input, output, reasoning, cache_read, cache_write, turns, wall_s, parse_ok, cost_reported, cost_computed`, plus `cost_litellm` on some rows. Cases: one target with `cost_litellm` on every row; one cursor target with `parse_ok: false` and every cost `null`; one target with 0 solved (so `cost_per_solved` is `inf`).

- [x] **Step 2: Write test `GoldenReport.test_summary_matches_golden`**

```python
def test_summary_matches_golden(self):
    tmp = make_tmp(self)  # copies harness_bench.py; writes config.json = {"output_dir": "results"}
    shutil.copy(GOLDEN / "results.jsonl", tmp / "results" / "results.jsonl")
    p = run_cli(tmp, "harness_bench.py", "report")
    self.assertEqual(p.returncode, 0, p.stderr)
    self.assert_golden("summary.md", mask_summary((tmp / "results/summary.md").read_text()))
    self.assert_golden("summary.csv", (tmp / "results/summary.csv").read_text())
```

- [x] **Step 3: Run it, expect FAIL** (golden files missing): `python3 -m unittest tests.test_golden -v`

- [x] **Step 4: Generate goldens** with `HB_UPDATE_GOLDEN=1 python3 -m unittest tests.test_golden`, then read `tests/golden/summary.md` and check by hand: cursor row shows `n/a` cost, the 0-solved target shows `inf`, sort order is by cost per solved with `n/a` last.

- [x] **Step 5: Run again without the env var, expect PASS**, and run `python3 tests/smoke.py`, expect `PASS`.

- [x] **Step 6: Stop.** Report files; no commit unless ordered.

### Task 2: Golden run test

**Files:**
- Modify: `tests/test_golden.py`
- Create: `tests/golden/run.jsonl`

**Interfaces:**
- Consumes: `run_cli`, `assert_golden` from Task 1.
- Produces: `mask_row(row: dict) -> dict` that sets `ts`, `wall_s`, `run_tag`, `t_start`, `t_end` to `"<masked>"`.

- [x] **Step 1: Write test `GoldenRun.test_results_match_golden`.** Config like `tests/smoke.py` (`harness()` helper, fizzbuzz task, the same 5-entry matrix and `prices`), without `litellm`. Each harness `env` sets `FAKE_TAGS_FILE` to a file in `tmp`. Run `harness_bench.py run --allow-unsandboxed`. Golden = each row of `results/results.jsonl` passed through `mask_row`, dumped with `json.dumps(row, sort_keys=True)`, one per line.

- [x] **Step 2: Run, expect FAIL** (golden missing). Generate with `HB_UPDATE_GOLDEN=1`. Check by hand: 5 rows; `claude` passed, `opencode` not; `cursor` has `parse_ok: false` and `cost_computed: null`; `codex` `cost_computed` is `0.0735`.

- [x] **Step 3: Run 3 times without the env var, expect PASS each time** (proves masks cover every volatile field).

- [x] **Step 4: Stop.** Report; no commit unless ordered.

### Task 3: Tool config and CI

**Files:**
- Create: `pyproject.toml`, `.github/workflows/ci.yml`, `.gitleaks.toml`

**Interfaces:**
- Produces: CI job `test` that later phases extend.

- [x] **Step 1: Write `pyproject.toml`** with only tool sections, no `[project]` dependencies:
  - `[tool.ruff]` `target-version = "py312"`, `line-length = 120`; `[tool.ruff.lint]` `select = ["E", "F", "W", "B", "UP"]`.
  - `[tool.mypy]` `python_version = "3.12"`, `files = ["harness_bench.py", "harness_bench_litellm.py"]`, non-strict; comment: new modules get `[[tool.mypy.overrides]] strict = true`.
  - `[tool.bandit]` `skips = ["B404", "B603", "B310"]` (accepted deviation: `B607` also skipped, only `git` called by name) with a comment per id: subprocess import is the engine's job; list-argv calls without shell; urllib only to the configured LiteLLM, https enforced by `check_litellm_url`.

- [x] **Step 2: Run each tool locally** (install in a throwaway venv in the scratchpad, never in the repo): `ruff check .`, `mypy`, `bandit -c pyproject.toml -r harness_bench.py harness_bench_litellm.py scripts`. Fix findings only by config or by changes that keep goldens identical. Record any rule disabled and why in `pyproject.toml` comments.

- [x] **Step 3: Write `.gitleaks.toml`** extending the default config with `[allowlist] paths = ['''^tests/''']`. Run `gitleaks detect --source . --config .gitleaks.toml`, expect no leaks.

- [x] **Step 4: Write `.github/workflows/ci.yml`**: trigger on `push` and `pull_request`; `ubuntu-latest`; `actions/checkout` with `fetch-depth: 0`; `actions/setup-python` with `3.12`; steps in order: `python3 tests/smoke.py`; `python3 -m unittest discover -s tests -p 'test_*.py'`; `python3 scripts/build_md.py && git diff --exit-code HARNESS_BENCH.md`; `pip install` of pinned `ruff`, `mypy`, `bandit` versions; `ruff check .`; `mypy`; bandit as in Step 2; gitleaks action pinned by commit SHA. Pin every action by commit SHA.

- [x] **Step 5: Validate the workflow** with `actionlint` from the scratchpad venv tools, else review by hand. Do not push.

- [x] **Step 6: Stop.** Report; no commit unless ordered.

### Task 4: pre-commit

**Files:**
- Create: `.pre-commit-config.yaml`

- [x] **Step 1: Write hooks:** `ruff` (lint) and `gitleaks` from their upstream repos with pinned `rev`; one `local` hook `unittest` running `python3 -m unittest discover -s tests -p 'test_*.py'`, `language: system`, `pass_filenames: false`.
- [x] **Step 2: Run `pre-commit run --all-files`** from the scratchpad venv, expect all hooks pass.
- [x] **Step 3: Stop.** Report; no commit unless ordered.

### Task 5: Apply ruff format

**Files:**
- Modify: every `*.py` in the repo; regenerated `HARNESS_BENCH.md`
- Modify: `.github/workflows/ci.yml`, `.pre-commit-config.yaml`

- [x] **Step 1: Run `ruff format .`.**
- [x] **Step 2: Run goldens, smoke and `ruff check .`**, expect all green with goldens **unchanged** (no `HB_UPDATE_GOLDEN`).
- [x] **Step 3: Run `python3 scripts/build_md.py`** to regenerate `HARNESS_BENCH.md`.
- [x] **Step 4: Add `ruff format --check .`** to CI after `ruff check`, and `ruff-format` to pre-commit.
- [x] **Step 5: Stop.** Keep this change separate from Tasks 1–4 when commits are ordered (formatting-only diff).

### Task 6: Docs

**Files:**
- Modify: `AGENTS.md` ("Done when", Map), `scripts/build_md.py`, `docs/evolution-plan.md`

- [x] **Step 1: AGENTS.md "Done when"** adds `python3 -m unittest discover -s tests -p 'test_*.py'` and the goldens rule: "regenerate goldens with `HB_UPDATE_GOLDEN=1` only for an intended behavior change, and say so in the PR". Map adds `tests/test_golden.py`, `tests/golden/`, `pyproject.toml`, `.github/workflows/ci.yml`.
- [x] **Step 2: `scripts/build_md.py`** bundles `tests/test_golden.py` next to the other tests; regenerate `HARNESS_BENCH.md`.
- [x] **Step 3: Tick Phase 0 boxes** in `docs/evolution-plan.md`.
- [x] **Step 4: Final verification:** smoke `PASS`, unittest OK, `git diff --exit-code HARNESS_BENCH.md` after regenerate. Stop and report.

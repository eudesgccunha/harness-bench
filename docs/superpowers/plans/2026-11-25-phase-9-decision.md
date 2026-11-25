# Phase 9: From Benchmark to Decision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `compare`, `recommend` (with `--write-target`), `gate`, a `TargetDecision` contract object (1.5.0), and an offline Exercism importer (`suite import exercism`), with docs.

**Architecture:** `hbench/decide.py` (pure, over `report.aggregate`), `hbench/importers/` (registry + `exercism.py`, plugin group `harness_bench.importers`), thin CLI in `hbench/cli.py`. Decisions: [ADR 0013](../../adr/0013-compare-recommend-gate-import.md).

**Tech Stack:** Python 3.12 stdlib (`json`, `pathlib`, `os`, `shutil`).

**Spec:** [`docs/evolution-plan.md`](../../evolution-plan.md) Phase 9; roadmap M12.

## Global Constraints

- Python 3.12, stdlib only at runtime. No `shell=True`. Unknown is `None`, never 0; an unknown cost never meets a cost constraint.
- No new network access. The importer reads a local checkout only.
- Every AGENTS.md defense stays; smoke keeps every case; a new defense gets a new case. `sandbox/` untouched.
- `run`/`report` output unchanged: goldens unchanged (never `HB_UPDATE_GOLDEN`).
- No third-party task content in the repository: importer tests build a synthetic Exercism-like tree at runtime.
- Type hints everywhere, mypy strict, ruff ANN, bandit; no new ignores. Coverage `fail_under = 100`.
- Fake secrets built at runtime; gitleaks passes. Targeted Edit only, NEVER read+overwrite a whole file. No commits.

## Review Focus

- `recommend`: unknown cost with `--max-cost-*` excluded (reason `unknown cost`), never ranked as 0; null control never picked; no eligible target exits 1; tie warning. Test in Task 2.
- `gate` fails closed: unfinished run, absent target, zero suite trials, invalid/forged target file (extra keys, wrong schema version), `error` trials counted as failed. Test in Task 2.
- `compare`: regression needs `b > c` and `p < alpha` on pairs present in both runs; disjoint tasks/reps, targets only in one run, comparability warnings; deterministic bytes. Test in Task 3.
- Target file: same run gives the same bytes; no command/env/path; symlink at the destination refused; atomic replace. Test in Task 2.
- Importer: symlink in the source, `..` or absolute name in `config.json`, file over 1 MB, bad slug, missing `LICENSE`, non-https `--source-url`, existing `--out`: error, nothing left behind; the imported suite passes `suite validate` (and `--run` in a test) and every hidden test has the marker. Test in Task 4.
- Acceptance: two fake runs where the second regresses a target: `compare` reports it, `recommend --write-target` picks the other target, `gate` with that file passes and with the regressed target fails. Test in Task 3 and a smoke case.

---

### Task 1: `TargetDecision` and contract 1.5.0

**Files:** Modify `hbench/core/models.py`, `hbench/core/schema.py` (`TargetDecision`, `SCHEMA_VERSION = "1.5.0"`, older data loads), tests `tests/test_core_models.py`, `tests/test_core_schema.py`, `tests/test_contract_output.py` if it pins the version.

- [x] Step 1: failing tests — round-trip, field validation (rates in [0, 1], non-negative costs or None), extra keys refused, schema export contains `TargetDecision`. Step 2: FAIL. Step 3: implement. Step 4: `make check` green; goldens unchanged.

### Task 2: `hbench/decide.py`: `recommend`, target file, `gate`

**Files:** Create `hbench/decide.py`; test `tests/test_decide.py` (trials built in memory, as in `tests/test_report.py`).

- [x] Step 1: failing tests — Review Focus 1, 2, 4. Step 2: FAIL. Step 3: implement (`recommend(info, trials, constraints, cost_source) -> Recommendation`, `decision_for(...) -> TargetDecision`, `write_target(path, decision)`, `gate(info, trials, harness, model, suite, min_pass_rate) -> GateResult`; rendering to Markdown/JSON kept separate from the logic). Step 4: `make check` green.

### Task 3: `compare` and the CLI

**Files:** Modify `hbench/decide.py` (`compare(a, b, cost_source, alpha) -> Comparison`, render), `hbench/cli.py` (`compare`, `recommend`, `gate`; `--litellm` forces the LiteLLM cost source as for `report`); create `adapters/github-actions/regression-gate.yml` (pinned SHAs as in `.github/workflows/ci.yml`; actionlinted by `make check` if the adapters workflow already is). Tests `tests/test_decide.py`, `tests/test_cli.py` (acceptance), `tests/smoke.py` (case: run, regressed run, `compare --fail-on-regression` exit 1, `recommend --write-target`, `gate` pass and fail).

- [x] Step 1: failing tests — Review Focus 3 and 6. Step 2: FAIL. Step 3: implement. Step 4: `make check` green; goldens unchanged.

### Task 4: Importers and `suite import exercism`

**Files:** Create `hbench/importers/__init__.py` (registry, `ImportError`-style `ImporterError`), `hbench/importers/exercism.py`; modify `hbench/plugins.py` (group `harness_bench.importers`), `hbench/cli.py` (`suite import`). Test `tests/test_importers.py` (synthetic tree at runtime; one test runs `suite validate --run --allow-unsandboxed` on the imported suite).

- [x] Step 1: failing tests — Review Focus 5; plugin importer loads. Step 2: FAIL. Step 3: implement (reads files through the same no-symlink/regular-file/size checks as `hbench/graders/_files.py`, reusing it where it fits; writes with `safe_copy_into`-style care into the fresh `--out`). Step 4: `make check` green.

### Task 5: Docs

**Files:** Create `docs/from-benchmark-to-decision.md`. Modify `AGENTS.md` (Map: `decide.py`, `importers/`, new commands; rule: unknown cost never meets a cost constraint, gate fails closed, importers never use the network), `CONTEXT.md` (target decision, regression, gate, importer), `scripts/build_md.py` (+ regenerate), `docs/architecture.md`, `docs/README.md`, `docs/running-a-benchmark.md`, `docs/roadmap.md` (M12: Exercism done, Terminal-Bench deferred with the reason), `adapters/README.md`, `docs/evolution-plan.md` (tick Phase 9 items, Terminal-Bench marked deferred, link plan/ADR).

- [x] Step 1: docs. Step 2: `make check` green; gitleaks passes.

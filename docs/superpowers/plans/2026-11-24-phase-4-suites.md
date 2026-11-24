# Phase 4: Suites as Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Versioned JSON suites, `suite list` / `suite validate [--run]`, three synthetic example tasks plus a regression suite, capability/regression split in the report, and an automated contamination test.

**Architecture:** `hbench/suites.py` loads and validates suites and expands them into engine task dicts; the engine and CLI consume them. Behavioral validation reuses the engine's workdir and `run_step` path. Decisions: [ADR 0008](../../adr/0008-suites-as-data.md).

**Tech Stack:** Python 3.12 stdlib; dev tools via uv.

**Spec:** [`docs/evolution-plan.md`](../../evolution-plan.md) Phase 4; roadmap M2 and M12.

## Global Constraints

- Python 3.12, stdlib only at runtime; the example tasks are stdlib only and run offline.
- Only public or synthetic code in `suites/` (written for this repo, MIT). No third-party code copied.
- Every AGENTS.md defense unchanged; `suite validate --run` goes through the same `wrap()`/env/redaction path as `run` and follows the same sandbox rule.
- Type hints everywhere; mypy strict on the whole repo; ruff ANN; no new ignores.
- `make check` green after each task; coverage `fail_under = 100`.
- Goldens unchanged (configs without `suites` behave as before).
- New files in `scripts/build_md.py` (and the mypy `files` covers them); `HARNESS_BENCH.md` regenerated.
- Targeted Edit only on existing files. No commits.

## Review Focus

- A suite path that escapes its directory (`"repo": "../../etc"`, absolute path, symlink in the suite dir pointing outside): rejected by `load_suite`. Test in Task 1.
- Two suites with the same task id, or a suite task id equal to a config task id: config error naming both. Test in Task 2.
- `suite validate --run` on a task whose untouched repo already passes: reported as `trivially passing`, exit 1. Test in Task 3.
- `suite validate --run` without sandbox and without `--allow-unsandboxed`: refuses, same message as `run`. Test in Task 3.
- Task id with `/` in log and workdir names: `safe()` keeps every file inside `logs_dir`/`work_root`. Test in Task 2.

---

### Task 1: Suite format and loader

**Files:** Create `hbench/suites.py`; Test `tests/test_suites.py`.

**Interfaces:**
- `SUITE_SCHEMA_VERSION = "1.0.0"`; `class SuiteError(ValueError)`.
- `@dataclass(frozen=True, kw_only=True) class Provenance: source: str; license: str; url: str | None = None`.
- `@dataclass(frozen=True, kw_only=True) class SuiteTask: id: str; data: Mapping[str, Any]` (validated task keys; paths already absolute); `tags: tuple[str, ...]`; `solution: Path | None`; `provenance: Provenance`.
- `@dataclass(frozen=True, kw_only=True) class Suite: name: str; version: str; kind: Literal["capability", "regression"]; description: str; path: Path; tasks: tuple[SuiteTask, ...]`.
- `load_suite(path: Path) -> Suite`; `suite_tasks(suite: Suite) -> list[dict[str, Any]]` (id `<name>/<task id>`, adds `suite`, `suite_kind`, `tags` keys the engine ignores).

- [x] Step 1: failing tests — valid suite loads; unknown top-level or task key rejected naming it; bad `name`, `kind`, `version`, `schema_version` major rejected; relative paths resolved against the suite dir; escapes rejected (Review Focus 1); git URL repos allowed with `ref`; duplicate task ids in one suite rejected.
- [x] Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 2: Config integration and engine

**Files:** Modify `harness_bench.py` (`validate_config`/config loading expands `suites`; `make_run_info` records suites), `hbench/report.py` (`## By suite kind` section only when trials carry more than one kind), `hbench/core/convert.py` if the trial needs the suite name (prefer reading it from `task_id` prefix; no contract change). Test `tests/test_run.py`, `tests/test_report.py`.

- [x] Step 1: failing tests — config with `suites` runs the suite tasks with fake agents; duplicate ids error (Review Focus 2); `RunInfo.suite_version` lists `name@version`; `safe()` on `/` ids (Review Focus 5); report section appears with two kinds and flags a failed regression task; absent otherwise (goldens unchanged).
- [x] Step 2: FAIL. Step 3: implement. Step 4: `make check` green; `git diff --exit-code tests/golden`.

### Task 3: `suite list` and `suite validate [--run]`

**Files:** Modify `hbench/cli.py` (subcommand `suite` with `list` and `validate`), `harness_bench.py` (a `validate_task_behavior(cfg, task, base, work_root) -> TaskCheck` that reuses `prepare_workdir`, `run_step`, graders and `rmtree_inside`; copies `solution` over the repo for the second check). Test `tests/test_cli.py`, `tests/test_suites.py`.

**Interfaces:** `@dataclass(frozen=True) class TaskCheck: task_id: str; untouched_passed: bool; solution_passed: bool` with property `ok = not untouched_passed and solution_passed`. Exit code 1 when any check is not ok; output one line per task: `ok`, `trivially passing`, or `solution fails`.

- [x] Step 1: failing tests — `suite list` output for the example suites; static `validate` ok and error cases (exit 1, message); `--run` ok case, `trivially passing`, `solution fails` (Review Focus 3); sandbox refusal (Review Focus 4).
- [x] Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 4: Example suites

**Files:** Create `suites/examples-capability/suite.json` and per task `repo/`, `hidden_tests/`, `solution/`: `dependency-upgrade`, `path-traversal-fix`, `extract-function`; create `suites/regression/suite.json` with `fizzbuzz` (move `tasks/fizzbuzz` to `suites/regression/fizzbuzz` with `git mv`, add `solution/`); update `config.example.json`, `config.litellm.example.json` to use `"suites": ["suites/regression", "suites/examples-capability"]`; update every test/fixture/smoke path that pointed at `tasks/fizzbuzz` (golden configs must still produce identical goldens — keep the golden fixture task definition inline so its ids do not change); `.gitignore`/`.dockerignore` only if needed. Test `tests/test_suites.py`.

- [x] Step 1: failing test — `suite validate --run --allow-unsandboxed` on both example suites reports `ok` for every task (runs the real hidden tests offline).
- [x] Step 2: FAIL. Step 3: write the tasks (small, stdlib only, deterministic; hidden tests fail on the untouched repo and pass on the solution; `provenance.source = "synthetic"`, `license = "MIT"`). Step 4: `make check` green; goldens unchanged.

### Task 5: Contamination guard and docs

**Files:** Modify `tests/fake_agent.py` (mode `probe-hidden`: walks the workdir, its parents up to `/` (non-recursive above the workdir parent, recursive at most 3 levels below each), `$HOME` and `tempfile.gettempdir()` (depth-limited) looking for hidden test file names and the marker string `HIDDEN-TEST-MARKER` that every example hidden test file contains as a comment; exits 7 when found), `tests/smoke.py` (case: probe finds nothing), the example hidden tests (add the marker comment). Docs: `scripts/build_md.py` (suite format, `suite` commands, example suites, by-suite-kind section), `docs/running-a-benchmark.md`, `AGENTS.md` (Map: `suites/`, `hbench/suites.py`; "Adding a task" now means adding to a suite), `CONTEXT.md` (Suite, capability, regression), `docs/roadmap.md` (M2 done, M12 → Phase 9), `docs/evolution-plan.md` (tick Phase 4, link plan and ADR), `docs/README.md`.

- [x] Step 1: failing smoke/test for the probe. Step 2: FAIL. Step 3: implement. Step 4: docs. Step 5: `make check` green; goldens unchanged.

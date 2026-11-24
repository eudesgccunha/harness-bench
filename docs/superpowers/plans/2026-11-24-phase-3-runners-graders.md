# Phase 3: Runner and Grader Protocols Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harnesses become runner adapters, grading becomes pluggable graders with composition and partial credit, plugins load through entry points, and usage records tool calls.

**Architecture:** `hbench/runners/` and `hbench/graders/` hold protocols, registries and built-ins; the engine keeps all process execution and defenses and injects a `run` callable into graders. Decisions: [ADR 0007](../../adr/0007-runner-and-grader-protocols.md).

**Tech Stack:** Python 3.12 stdlib (`typing.Protocol`, `importlib.metadata`, `fnmatch`, `re`); dev tools via uv.

**Spec:** [`docs/evolution-plan.md`](../../evolution-plan.md) Phase 3; roadmap M9 and M10 in [`docs/roadmap.md`](../../roadmap.md).

## Global Constraints

- Python 3.12, stdlib only at runtime. No `shell=True`. Unknown values are `None`, never `0`.
- Every AGENTS.md defense stays in the engine; runners and graders never start processes.
- English; type hints everywhere; `hbench/` mypy strict.
- `make check` green after every task; coverage `fail_under = 100`; no new ignores.
- Goldens unchanged until Task 6. In Task 6, regenerate `tests/golden/run.jsonl`, `summary.md`, `summary.csv`, `summary_reps.md` once with `HB_UPDATE_GOLDEN=1`; verify the diff only adds `tool_calls` and the two report columns.
- New files go into `scripts/build_md.py` and the mypy list; `HARNESS_BENCH.md` regenerated.
- Edit existing files with targeted replacements only; never rewrite a whole existing file.
- No commits.

## Review Focus

- A task without `graders` behaves byte-for-byte as before (goldens until Task 6 prove it).
- `diff_constraints` with a path like `../x`, an absolute path, or a rename in the diff: paths are normalized relative to the workdir; anything outside counts as forbidden. Test in Task 3.
- A `SCORE:` line printed by agent code before the real one: last line wins; an out-of-range or malformed value is ignored (score falls back to pass/fail) with a warning. Test in Task 3.
- Unknown grader or runner type with no plugin installed: config validation exits with a message naming the type and the entry-point group, before any agent runs. Test in Task 5.
- Plugin that raises on load or returns the wrong type: clear error naming the plugin, no traceback from the engine. Test in Task 5.

---

### Task 1: Runner protocol and adapters

**Files:**
- Create: `hbench/runners/__init__.py`, `hbench/runners/base.py`, `hbench/runners/_usage.py` (`num`, `json_lines`, `empty_usage`), `hbench/runners/{claude,opencode,pi,cursor,codex,null}.py`
- Modify: `harness_bench.py` (remove parser bodies; `from hbench.runners import ...` re-exporting `parse_claude`, ..., `PARSERS`, `num`, `json_lines`, `empty_usage`; `build_cmd` delegates to the runner)
- Test: `tests/test_runners.py`; existing `tests/test_parsers.py` must pass unchanged

**Interfaces:**
- `class HarnessRunner(Protocol)`: `name: str`; `def prepare(self, workdir: Path) -> None`; `def command(self, model: str, prompt: str) -> list[str]`; `def parse_usage(self, stdout: str) -> dict[str, Any] | None`; `def version(self) -> str`.
- `class TemplateRunner`: implements it from a harness config (`command`, `parser`, optional `version`, default `"unknown"`); `command` keeps today's `build_cmd` semantics exactly.
- `PARSERS: Mapping[str, Callable[[str], dict[str, Any] | None]]` (built-ins).
- `runner_for(harness_cfg: Mapping[str, Any]) -> HarnessRunner` (built-ins now; plugins in Task 5).

- [x] Step 1: failing tests — `TemplateRunner` builds the same argv as `build_cmd` for `provider/model` and plain model; `version()` default and configured; `runner_for` with every built-in parser; unknown parser raises `KeyError` (wrapped into a config error in Task 5).
- [x] Step 2: FAIL. Step 3: move parsers verbatim (no behavior change), wire the engine. Step 4: `make check` green, goldens untouched.

### Task 2: Grader protocol and composition

**Files:**
- Create: `hbench/graders/__init__.py`, `hbench/graders/base.py`, `hbench/graders/compose.py`
- Test: `tests/test_graders.py`

**Interfaces:**
- `@dataclass(frozen=True) class Change: path: str; added: int; deleted: int` (path POSIX, relative to workdir).
- `@dataclass(frozen=True) class GradeContext: workdir: Path; task: Mapping[str, Any]; base: Path; changes: tuple[Change, ...]; run: Callable[[str, int, str], tuple[int | None, bool, str]]` (`run(command, timeout, log_name) -> (rc, timed_out, output)`).
- `class Grader(Protocol)`: `name: str`; `version: str`; `def grade(self, ctx: GradeContext) -> GraderResult`.
- `parse_score(output: str) -> float | None` (last `^SCORE:\s*([0-9.]+)\s*$` line; outside `[0, 1]` or malformed → `None`).
- `compose(results: Sequence[GraderResult], mode: str, weights: Sequence[float], threshold: float | None) -> tuple[bool, float]`.

- [x] Step 1: failing tests — `parse_score` last-wins, ignores `1.5`, `abc`, `-0.1`; `compose` all/any/weighted known cases (`[pass 1.0, fail 0.0]` → all `(False, 0.5)`, any `(True, 1.0)`, weighted w=[3,1] t=0.7 → `(True, 0.75)`); weighted without threshold raises `ValueError`; empty results raise `ValueError`.
- [x] Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 3: Bundled graders

**Files:**
- Create: `hbench/graders/hidden_tests.py`, `hbench/graders/command.py`, `hbench/graders/diff_constraints.py`
- Test: `tests/test_graders.py`

**Interfaces:**
- `HiddenTestsGrader(cfg)`: copies `base / task["hidden_tests"]` into `workdir` (same `copytree(..., dirs_exist_ok=True)` as today) when set, then `ctx.run(task["test"], task.get("test_timeout", 900), "<log>")`; `passed = rc == 0 and not timed_out`; score per ADR 0007 §5; `timed_out` set.
- `CommandGrader(cfg)`: `cfg["command"]`, `cfg.get("timeout", 900)`; same pass/score rule.
- `DiffConstraintsGrader(cfg)`: `allowed: list[str] = []` (empty = everything allowed), `forbidden: list[str] = []`, `max_changed_lines: int | None`; fails listing violations in `details_uri`-free form (details go to the log via the engine; the result carries only `passed`/`score` 1.0 or 0.0).
- Log names: `<tag>.test.log` for `hidden_tests` (unchanged), `<tag>.grader-<index>-<type>.log` for others. The engine passes `tag`-prefixed names; graders receive the suffix only.

- [x] Step 1: failing tests with a fake `run` and fake `changes`: hidden tests copied before the command runs; timeout → `timed_out=True`, not passed; SCORE parsed; `diff_constraints` allowed/forbidden/max lines, `../x` and absolute paths forbidden.
- [x] Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 4: Engine integration

**Files:**
- Modify: `harness_bench.py` (`cmd_run`: capture `changes` right after the agent run with `git diff --numstat` against the baseline through `git_bin()`; build graders from the task; `run` callable = `run_step` bound to cfg/workdir/env_file/secrets/logs; compose; legacy row `passed` = composed verdict; trial gets all `GraderResult`s), `hbench/core/convert.py` (`trial_from_row(..., graders: Sequence[GraderResult] | None = None)`), `validate_config` (grader list shape, `grade` mode/threshold)
- Modify: `tests/smoke.py` (new cases: a `command` grader that fails makes the run fail; a `diff_constraints` forbidding `run_tests.py` fails the `hack` agent even if tests pass), `tests/fake_agent.py` only if needed
- Test: `tests/test_run.py`, `tests/test_contract_output.py`

- [x] Step 1: failing tests — a task with `graders: [hidden_tests, command]` writes 2 `GraderResult`s to `trials.jsonl`; `grade: any` passes when one passes; legacy row has no new keys; `changes` captured before restore (an agent edit to a protected file still shows up in `changes`).
- [x] Step 2: FAIL. Step 3: implement; every defense unchanged. Step 4: `make check` green, goldens untouched, smoke PASS.

### Task 5: Entry-point plugins

**Files:**
- Create: `hbench/plugins.py`
- Modify: `hbench/runners/__init__.py`, `hbench/graders/__init__.py` (lookup falls back to plugins), `harness_bench.py` (`validate_config` resolves every runner and grader type up front)
- Test: `tests/test_plugins.py` (builds a throwaway distribution in a temp dir: `demo_plugin-0.1.dist-info/METADATA` + `entry_points.txt` + `demo_plugin.py`, prepended to `sys.path`; restored after)

**Interfaces:**
- `RUNNER_GROUP = "harness_bench.runners"`, `GRADER_GROUP = "harness_bench.graders"`.
- `load_plugin(group: str, name: str) -> Callable[[Mapping[str, Any]], object]`; raises `PluginError(ValueError)` for: not found (message names group and name), built-in name clash, load failure (wraps the exception text), factory result not matching the protocol (`isinstance` with `runtime_checkable` protocols).
- Prints `plugin <group>:<name> from <dist> <version>` once per process.

- [x] Step 1: failing tests — demo grader plugin grades a task end to end through `cmd_run` with fake agents (acceptance criterion: no core change); unknown type exits before any agent runs; clash, load error and wrong type each give `PluginError`; a plain config never calls `entry_points` (patch and assert not called).
- [x] Step 2: FAIL. Step 3: implement. Step 4: `make check` green.

### Task 6: Tool calls (M10) and contract 1.1.0

**Files:**
- Modify: `hbench/runners/*.py` (count tool calls where the format exposes them; check `tests/fake_agent.py` formats and the AGENTS.md semantics; unknown → `None`), `hbench/runners/_usage.py` (`empty_usage()["tool_calls"] = None`), `hbench/core/models.py` (`Usage.tool_calls: int | None = None`, `SCHEMA_VERSION = "1.1.0"`), `hbench/core/schema.py`, `hbench/core/convert.py`, `hbench/report.py` (append `tool_calls_per_run`, `tool_calls_per_solved` keys and `Tool calls/run`, `Tool calls/solved` columns; `None` when any trial of the pair is unknown), `tests/fake_agent.py` (emit tool-call events in each format), goldens (regenerate once), `AGENTS.md` (usage semantics per harness: how tool calls are counted), `scripts/build_md.py`, `docs/roadmap.md`, `docs/evolution-plan.md`, `docs/README.md`, `CONTEXT.md`, `docs/architecture.md`
- Test: `tests/test_parsers.py`, `tests/test_core_models.py`, `tests/test_core_schema.py`, `tests/test_report.py`

- [x] Step 1: failing tests — per parser, tool calls counted from a fixture line set, `None` when the format has none; a 1.0.0 trial dict without `tool_calls` loads; report columns `n/a` when unknown.
- [x] Step 2: FAIL. Step 3: implement.
- [x] Step 4: regenerate goldens once; verify diff only adds `tool_calls` and the two columns/CSV keys.
- [x] Step 5: docs; tick Phase 3 in the evolution plan, M9/M10 in the roadmap. `make check` green.

# Architecture

Current structure of the engine, mapped before refactoring (Phase 0 of the
[evolution plan](evolution-plan.md)). Line numbers refer to `harness_bench.py` at commit
`e44c25c`.

## Files

This table maps the code as it was at `e44c25c`. The test suite and tooling added in Phase 0 (`tests/fixtures.py`, `tests/test_*.py`, `tests/golden/`, `Makefile`, `scripts/docs_check.py`, `pyproject.toml`, CI) are listed in the Map in [`AGENTS.md`](../AGENTS.md).

| File | Role |
|---|---|
| `harness_bench.py` | Engine and CLI: `run`, `reconcile`, `report`, one parser per harness. |
| `harness_bench_litellm.py` | Filters the config (drops `parser: cursor`), forces LiteLLM cost, chains `run` → wait → `reconcile` → `report`. |
| `scripts/build_md.py` | Bundles docs and every source file into `HARNESS_BENCH.md`. |
| `tests/smoke.py` | End-to-end test with fake agents and a fake LiteLLM. One case per defense. |
| `tests/fake_agent.py` | Prints each CLI's JSON shape; modes `fix`, `nofix`, `hack`; probes for leaks. |
| `tests/fake_litellm.py` | Serves `/spend/logs/v2` from the tags the fake agents wrote. |

## Responsibilities inside `harness_bench.py`

| Concern (plan module) | Current code |
|---|---|
| Parsers (`runners/`) | moved to `hbench/runners/` in Phase 3 (re-exported here) |
| Cost (`cost/`) | `computed_cost` (204–212), cost pick in `aggregate.run_cost` (672–679) |
| Safety (`sandbox/`) | `BASE_ENV`, `base_env`, `redact`, `validate_config`, `rmtree_inside`, `wrap`, `write_env_file` (220–303) |
| Workdir (`sandbox/`) | `prepare_workdir`, `hardened_git`, `make_baseline` (git dir outside the workdir), `snapshot_protected`, `restore_protected`, `save_diff`, `nested_git_entries` |
| Runner (`runners/`) | `run_proc` (308); `build_cmd` delegates to `hbench.runners.base.build_argv` |
| Planner and resume | loop in `cmd_run` (453–546), `done_keys` (412) |
| Grader (`graders/`) | `grade_trial` runs the task's graders (Phase 3); `collect_changes` feeds `diff_constraints` |
| Result writing (`sinks/`) | row dict appended to `results.jsonl` (528–540) |
| Gateway (`cost/`) | `check_litellm_url`, `litellm_get`, `litellm_rows`, `row_tags`, `cmd_reconcile` (555–642) |
| Stats (`stats/`) | `aggregate` (659–706) |
| Report (`sinks/`) | `cmd_report` writes `summary.csv` and `summary.md` (709–758) |
| CLI (`cli/`) | `main` (763–784) |

## Couplings to break

1. **`cmd_run` does everything.** One function plans trials, prepares the workdir, builds
   the env, runs the agent, parses usage, grades, computes cost and writes the row. The
   inner closure `step` captures `env_file`, `workdir`, `logs_dir` and `secrets`.
2. **Implicit row schema.** The row is a dict literal with usage fields spread in
   (`**usage`). No `schema_version`, no `run_id`, no harness or image version.
3. **No error status.** An agent crash (`agent_rc != 0`) or timeout is only visible in
   extra fields; `passed` is the only verdict, so infrastructure errors count as agent
   failures.
4. **`reconcile` rewrites results in place.** It loads every row, adds `cost_litellm*`
   fields and rewrites `results.jsonl`, then calls `cmd_report`.
5. **Stats and rendering are mixed.** `cmd_report` computes the table, writes CSV and
   builds Markdown in one function. `summary.md` starts with a timestamp line and shows
   `avg_wall_s`, so it is not byte-stable between runs.
6. **The wrapper depends on internals.** `harness_bench_litellm.py` calls
   `cmd_run/cmd_reconcile/cmd_report(args, cfg, base)` and sets `args.cost_source` and
   `args.no_report`. Changing those signatures breaks it.
7. **Defenses live in `cmd_run`.** Every defense in AGENTS.md (workdir outside the repo,
   no git history, env allowlist, protected paths, audit diff, redaction,
   `rmtree_inside`, no `shell=True`, `wrap()`) must keep its `tests/smoke.py` case while
   code moves.

## What Phase 0 freezes

- `summary.md` and `summary.csv` from a fixed `results.jsonl` (first line masked).
- `results.jsonl` from a fake run, with `ts`, `wall_s`, `run_tag`, `t_start` and `t_end`
  masked.

## Phase 1: data contract and `hbench/`

New code lives in the `hbench/` package ([ADR 0005](adr/0005-data-contract-and-package.md)); the
engine stays in `harness_bench.py` and calls into it.

| Module | Role |
|---|---|
| `hbench/core/models.py` | Frozen contract models (`Target`, `GraderResult`, `Costs`, `Usage`, `TrialResult`, `RunInfo`), `SCHEMA_VERSION`, `to_dict`/`from_dict` that reject unknown fields. |
| `hbench/core/ids.py` | `new_run_id`, `trial_id`, `sha256_json`. |
| `hbench/core/schema.py` | Hand-written JSON Schema for `trials.jsonl` and `run.json`, and a small stdlib validator. |
| `hbench/core/convert.py` | Legacy `results.jsonl` row to `TrialResult` (`trial_from_row`, `trials_from_rows`). |
| `hbench/results.py` | `RunWriter` (`run.json` and `trials.jsonl` per run) and `apply_gateway_costs` (used by `reconcile`). |
| `hbench/report.py` | `aggregate`, `render_markdown`, `write_csv` over `TrialResult`s; `harness_bench.cmd_report` calls it. |
| `hbench/profiles.py` | `apply_litellm_profile`: the LiteLLM-only config profile. |
| `hbench/cli.py` | The single CLI (`python3 -m hbench`); both scripts are shims over it. |

Output per `run`: the legacy `results.jsonl` is still appended (resume, `reconcile` and old
folders depend on it), and each invocation also writes `<run_id>/run.json` and
`<run_id>/trials.jsonl`. Trials hold references only (`artifact_uri` is `logs/<tag>`).

Couplings 2 (implicit row schema), 3 (status, partly: `timeout` exists, `error` has no
producer yet), 5 (report mixed with stats) and 6 (wrapper depends on internals) are addressed.
`cmd_run` still does everything else; it is split in later phases.

## Phase 3: runners, graders and plugins

Decisions: [ADR 0007](adr/0007-runner-and-grader-protocols.md). The engine still owns every process and every
defense; runners and graders are pure adapters that the engine calls.

| Module | Role |
|---|---|
| `hbench/runners/base.py` | `HarnessRunner` protocol and `TemplateRunner` (argv from the harness `command` template, usage from the parser). |
| `hbench/runners/<name>.py`, `_usage.py` | One parser module per harness (`parse_<name>`) and the shared helpers `num`, `json_lines`, `empty_usage`. |
| `hbench/runners/__init__.py` | `PARSERS` (built-ins) and `runner_for` (built-in parser, else a plugin). |
| `hbench/graders/base.py` | `Grader` protocol, `GradeContext` (with the injected `run`), `Change`, `parse_score`. |
| `hbench/graders/{hidden_tests,command,diff_constraints}.py` | The bundled graders. |
| `hbench/graders/compose.py`, `registry.py` | `compose` (`all`, `any`, `weighted`), `GRADERS`, `grader_for`, `grade_plan` (the graders of a task). |
| `hbench/plugins.py` | Entry-point loading with clear `PluginError`s. Only reached for a type that is not built in. |

In `cmd_run`: the runner builds the agent argv and parses usage; `collect_changes` captures the diff right
after the agent (before `protected_paths` are restored); `grade_trial` runs the graders with a `run` bound to
`run_step`, so each grader command gets the env allowlist, `wrap()` and a redacted log. `validate_config`
resolves every runner and grader type before any agent runs. Coupling 1 (`cmd_run` does everything) is
reduced: runner and grader are now behind protocols.

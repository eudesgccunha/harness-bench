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
| Parsers (`runners/`) | `parse_claude`, `parse_opencode`, `parse_pi`, `parse_cursor`, `parse_codex`, `PARSERS` (70–199) |
| Cost (`cost/`) | `computed_cost` (204–212), cost pick in `aggregate.run_cost` (672–679) |
| Safety (`sandbox/`) | `BASE_ENV`, `base_env`, `redact`, `validate_config`, `rmtree_inside`, `wrap`, `write_env_file` (220–303) |
| Workdir (`sandbox/`) | `prepare_workdir`, `make_baseline`, `snapshot_protected`, `restore_protected`, `save_diff` (329–402) |
| Runner (`runners/`) | `run_proc` (308), `build_cmd` (405) |
| Planner and resume | loop in `cmd_run` (453–546), `done_keys` (412) |
| Grader (`graders/`) | test `step` inside `cmd_run` (525); `passed = test_rc == 0 and not test_to` (531) |
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

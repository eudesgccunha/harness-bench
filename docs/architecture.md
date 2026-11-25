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
| `hbench/suites.py` | Suites as data (Phase 4, [ADR 0008](adr/0008-suites-as-data.md)): `load_suite` validates `suite.json` and keeps every path inside the suite directory, `suite_tasks` expands a suite into engine task dicts (`<suite>/<task>`). The engine's `expand_suites` appends them to `tasks`; `validate_task_behavior` runs the untouched and the solution check through `prepare_workdir` and `run_step`. |
| `hbench/plugins.py` | Entry-point loading with clear `PluginError`s. Only reached for a type that is not built in. |

In `cmd_run`: the runner builds the agent argv and parses usage; `collect_changes` captures the diff right
after the agent (before `protected_paths` are restored); `grade_trial` runs the graders with a `run` bound to
`run_step`, so each grader command gets the env allowlist, `wrap()` and a redacted log. `validate_config`
resolves every runner and grader type before any agent runs. Coupling 1 (`cmd_run` does everything) is
reduced: runner and grader are now behind protocols.

## Phase 5: artifact graders and the judge

Decisions: [ADR 0009](adr/0009-artifact-graders-and-judge.md). The engine owns the network the way it owns processes:
graders never open a socket, they receive a `judge` through `GradeContext`.

| Module | Role |
|---|---|
| `hbench/graders/{html_check,structured_output,llm_rubric}.py` | The artifact graders. They write violations and the judge's `reason` through `ctx.log` only. |
| `hbench/graders/_files.py` | `resolve_artifact`, `artifact_file`, `read_artifact`: a file of the workdir is read only when no part of its path is a symlink, it has one hard link and is a regular file. `asset_root` is where a grader's rubric and schema files live (the suite directory, else the config directory). |
| `hbench/judge.py` | `Rubric`, `JudgeVerdict`, the `Judge` protocol and `GatewayJudge`, which builds the chat request (fixed system prompt, random delimiters, redaction, size limit) and validates the reply. It takes the engine's `post`, so it opens no socket. |
| `hbench/calibration.py` | `calibrate`, `cohens_kappa`, labels and calibration files, `calibration_status` (calibrated or the reason it is experimental). |
| `hbench/core/schema.py` `check_schema` | Checks that a schema uses only the keywords `validate` supports (a config error otherwise). |
| `harness_bench.py` | `judge_post` (the judge `POST`: `check_litellm_url`, `NoRedirect` from `hbench/net.py`, reply size limit), `make_judge` (secret redaction, then the `harness_bench.redactors` plugin), `grader_logger` (redacted grader logs), `validate_judge`. |

`report.py` adds `## Judges` from the trials in `trials.jsonl` and the calibration file named by `judge.calibration`.
Plan: [`superpowers/plans/2026-11-24-phase-5-artifact-graders.md`](superpowers/plans/2026-11-24-phase-5-artifact-graders.md).

## Phase 6: sandboxes, resume and doctor

Decisions: [ADR 0010](adr/0010-pluggable-sandbox-and-resume.md). A sandbox builds argv and manages its own cleanup;
the engine still starts every process, so the env allowlist, the timeout, the process-group kill and the redaction
apply to every sandbox the same way.

| Module | Role |
|---|---|
| `hbench/sandboxes/base.py` | The `Sandbox` and `SandboxSession` protocols, `SandboxError`, and `HostSession` (`copy_in`/`copy_out` through `safe_copy`, which never follows a link the agent planted). |
| `hbench/sandboxes/{prefix,docker,local_tmp}.py` | The three built-in types. `prefix` builds the argv `wrap()` always built; `docker` builds `docker run` with the pinned image, `--network none` by default, `--cap-drop ALL`, the host user and the limits, and `end_step` runs `docker rm -f` for the containers of the command that just ended (killing the client leaves the container running) and `destroy` for any left; `local-tmp` isolates nothing and sets best-effort rlimits through a small launcher. |
| `hbench/sandboxes/limits.py` | `Limits` (`cpus`, `memory`, `pids`, `cpu_seconds`) and its validation. |
| `hbench/sandboxes/__init__.py` | `sandbox_for(cfg, run, env)`: the built-in type of `sandbox.type`, else a plugin of `harness_bench.sandboxes`. |
| `hbench/results.py` | `RunWriter.open_existing`, `finished_trial_ids`, an `fsync` after each trial; `Heartbeat` (a daemon thread rewrites `heartbeat.json` every 30 s) and `heartbeat_is_live`. |
| `hbench/doctor.py` | The checks behind `python3 -m hbench doctor`. |
| `harness_bench.py` | `run_proc` kills the process group of every command; `_run_matrix` plans the trials, `_run_trial` runs one with its own session (destroyed in a `finally`); `open_resumed_run`, `restore_legacy_rows` and `discard_leftovers` are the resume path. |

Order of the two result files for a trial: `trials.jsonl` first (flushed to disk), then the legacy row of `results.jsonl`;
resume rewrites the legacy row of a trial that has none (`row_from_trial`). Contract 1.3.0 adds `RunInfo.sandbox`.
Plan: [`superpowers/plans/2026-11-24-phase-6-sandbox-resume.md`](superpowers/plans/2026-11-24-phase-6-sandbox-resume.md).

## Phase 7: sinks and telemetry

Decisions: [ADR 0011](adr/0011-sinks-and-telemetry.md). `trials.jsonl` stays the source of truth and is written first;
a sink only receives the contract objects, so it cannot leak content, and a sink that fails never changes a result.

| Module | Role |
|---|---|
| `hbench/sinks/base.py` | The `Sink` protocol (`open`, `write`, `close`) and `SinkError`. |
| `hbench/sinks/__init__.py` | `sinks_for(cfg, base)`: the built-in type of each `sinks` entry, else a plugin of `harness_bench.sinks`; `sink_secret_vars` (the `${VAR}` of the `otlp` headers, secrets that never reach an agent). |
| `hbench/sinks/sqlite.py` | `SqliteSink`: tables from `docs/sql/schema.sql`, parameterized upserts by `run_id` and `trial_id`; unknown values are NULL. |
| `hbench/sinks/otlp.py` | `OtlpSink`: OTLP/JSON spans (trace id = `run_id`, run span id from `sha256("run:" + run_id)`, trial span id = `trial_id`) in `<run_dir>/traces.otlp.jsonl`, each span id once, and a `POST <endpoint>/v1/traces` when `endpoint` is set. |
| `hbench/sinks/feed.py` | `SinkFeed`: sends the run and each trial to every sink; a sink that raises is warned about once and dropped for the run. |
| `hbench/net.py` | The URL rule (`url_allowed`), `NoRedirect` and `post_bounded` (total deadline, 1 MB reply cap). |
| `hbench/results.py` | `read_run`: a run as it is on disk, without changing a file (what `export` replays). |
| `harness_bench.py` | `make_sinks`; `_run_matrix` opens the sinks after `run.json` and closes them after `writer.close`; `_run_trial` writes the trial to them after the `trials.jsonl` append and exports `BENCH_RUN_ID` and `BENCH_TRIAL_ID` to the agent. |
| `docs/sql/`, `docs/dashboards/`, `scripts/synthetic_db.py` | Reference schema and scorecard queries, an example Grafana dashboard and a seeded synthetic database to try it. |

`TrialResult.trace_id` keeps the run tag: `reconcile` matches the spend logs by it. The OTLP trace id is the `run_id`.
Plan: [`superpowers/plans/2026-11-24-phase-7-sinks-telemetry.md`](superpowers/plans/2026-11-24-phase-7-sinks-telemetry.md).

## Phase 8: orchestrator API

Decisions: [ADR 0012](adr/0012-plan-and-run-trial-api.md). `_run_trial` stays the only implementation of a trial. `run` runs it in one process with shared dirs, heartbeat and sink feed; `hbench/api.py` runs it for one `TrialSpec` in private ones, so any worker can do it.

| Module | Role |
|---|---|
| `hbench/api.py` | The stable surface: `load_config`, `start_run`, `open_run`, `plan`, `parse_spec`, `run_trial`, `finish_run`, and the errors `ConfigError`, `SpecError`, `RunError`, `SandboxRequiredError` (all `ApiError`). It validates a spec against the config and the run, returns the recorded result of a trial that is already in `trials.jsonl`, and calls the engine. |
| `hbench/core/models.py` | `TrialSpec` (contract 1.4.0): names a trial, carries no command, prompt, path, env or secret. |
| `hbench/results.py` | `locked_append` (`flock`, write, flush, `fsync`) for `trials.jsonl`, `results.jsonl` and the OTLP file; `RunWriter.append_once` (check and write under one lock); `open_existing(repair=False)` for workers. |
| `harness_bench.py` | `planned_trials` and `planned_trial` (the order of `run`, shared with `plan`); `run_trial_isolated` (private work dir, git dir, sandbox session and sink feed around `_run_trial`); `sink_feed`. |
| `hbench/cli.py` | `plan`, `run-trial` (the engine's progress goes to stderr, stdout is the result) and `finish`. |
| `adapters/` | Shell, GitHub Actions, Kubernetes and Python examples over the three calls. |

The guide is [`orchestrator-integration.md`](orchestrator-integration.md). Plan: [`superpowers/plans/2026-11-25-phase-8-orchestrator-api.md`](superpowers/plans/2026-11-25-phase-8-orchestrator-api.md).

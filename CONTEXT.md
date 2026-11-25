# Context

Glossary for harness-bench. The [evolution plan](docs/evolution-plan.md) uses eval terms;
the current code uses older names. Use the plan term in new code and docs, and keep the
current name in the existing config and results format until Phase 1 migrates it.

| Plan term | Current name | Meaning |
|---|---|---|
| Target | pair, matrix entry (`{"harness", "model"}`) | One candidate: harness × model. |
| Trial | run, one `results.jsonl` row; contract model `TrialResult` (`trials.jsonl`) | One attempt of one target on one task, keyed by `(task, harness, model, rep)`. |
| Rep | `rep` | Index of a repeated trial, from 1. |
| Run | one `harness_bench.py run` call; contract model `RunInfo` (`run.json`) | All trials of one invocation, identified by `run_id`. |
| Suite | a directory with `suite.json` (`hbench/suites.py`), listed in the config key `suites`; plain `tasks` in the config still work | A named, versioned list of tasks of one kind. Task ids become `<suite>/<task>`. `RunInfo.suite_version` records `name@version` of every suite used. |
| Capability suite | `kind: "capability"` | Hard tasks with a low pass rate; they separate the targets. |
| Regression suite | `kind: "regression"` | Tasks every healthy target must solve; the report flags a non-null target that fails one. |
| Solution | task key `solution` (a directory) | Reference files copied over the repo by `suite validate --run`; the graders must fail without it and pass with it. |
| Provenance | task key `provenance` (`source`, `license`, `url`) | Where a task comes from and its license. |
| Task | task (`id`, `repo`, `ref`, `prompt`, `test`, `hidden_tests`, `protected_paths`) | One problem with a starting repo and a way to grade it. |
| Grader | task key `graders` (default: `test` command + `hidden_tests`); contract model `GraderResult` | Decides pass or fail and gives a score from 0 to 1. Bundled types: `hidden_tests`, `command`, `diff_constraints`, `html_check`, `structured_output`, `llm_rubric`. Several graders combine through the task key `grade` (`all`, `any`, `weighted`). |
| Runner | harness entry (`command` + `parser`); `HarnessRunner` protocol | Adapter that builds the agent argv and parses its output. It never starts a process: the engine does. |
| Plugin | entry point in `harness_bench.runners`, `harness_bench.graders` or `harness_bench.redactors` | A runner, grader or redactor shipped by another package. Built-in names always win. |
| Artifact grader | `html_check`, `structured_output`, `llm_rubric` | Graders that read a file the agent wrote instead of running code. They never start a process or open a socket; every file must stay inside the workdir without a symlink. |
| Judge | config section `judge`; `Judge` protocol, `GatewayJudge` (`hbench/judge.py`) | An LLM that scores an artifact against a rubric through the OpenAI-compatible gateway. The engine owns the network and injects it into the graders. Its `reason` goes to the grader log only. Recorded in `GraderResult.judge` (the model). |
| Rubric | file `rubrics/<id>.json` (`id`, `version`, `criteria`, `pass_threshold`) | The versioned text a judge scores against. The result is `llm_rubric:<id>` at the rubric's version. |
| Calibration | `judge calibrate`, file named by `judge.calibration` | Agreement of a judge with human scores on one rubric version (mean absolute error, pass/fail agreement, Cohen's kappa). A judge is calibrated for a rubric version when kappa >= 0.6 on at least 20 labels; otherwise the report marks it **experimental**. |
| Redactor | `judge.redactor`, entry-point group `harness_bench.redactors` | A callable `(text) -> text` that runs on an artifact before it is sent to the judge, after the engine's secret redaction. The content-specific part is the user's. |
| Score | `GraderResult.score`, the `SCORE:` line | Partial credit from 0 to 1. Pass or fail stays the exit code. |
| Tool calls | `tool_calls` (usage), `Tool calls/run`, `Tool calls/solved` | Tool invocations the agent made, where the output exposes them; unknown is `None`, never `0`. |
| Trial status | `passed` (bool) | Plan adds `error` and `timeout`. Today an agent crash or timeout counts as a failure. |
| Usage | token fields in the row (`input`, `output`, `reasoning`, `cache_read`, `cache_write`, `turns`, `tool_calls`) | Tokens and tool calls parsed from the harness output. |
| Cost source | `cost_litellm`, `cost_computed`, `cost_reported` | Origin of a cost. Plan names: `gateway`, `price_table`, `harness`. Order of preference is the same. Unknown cost is `None`, never `0`. |
| Gateway | LiteLLM | Proxy that bills the model calls and keeps spend logs. |
| Bench tag | `run_tag`, `BENCH_TAG` | Per-trial tag sent as `x-litellm-tags`; `reconcile` matches spend logs by it. |
| Sandbox | `sandbox` section, `Sandbox` protocol | How the commands of a trial run: `docker` (image pinned by digest, no network by default, limits), `prefix` (a command template put in front, such as `docker run ...`) or `local-tmp` (no isolation). A sandbox only builds argv and cleans up; the engine runs every process. |
| Image digest | `TrialResult.image_digest`, `sandbox.image` | The `sha256:` digest of the image a trial ran in; the `docker` sandbox refuses an image given as a tag. |
| Heartbeat | `<run_id>/heartbeat.json` | File a running `run` rewrites (pid, host, trial, phase, time) so `--resume` can tell a live run from a killed one. |
| Resume | `run --resume RUN_ID` | Continue an interrupted run in its own directory, skipping the trials already in its `trials.jsonl` (by `trial_id`). |
| Doctor | `python3 -m hbench doctor` | Checks that a benchmark can run (config, variables, CLIs, image, network, URLs) without spending tokens. |
| Sink | `sinks` config list, `Sink` protocol (`hbench/sinks/`), entry-point group `harness_bench.sinks` | Where the trials of a run go besides `trials.jsonl`: `sqlite` (tables of `docs/sql/schema.sql`) and `otlp` (spans) are built in; Postgres, S3 or Parquet are plugins. A sink receives contract objects only, writes idempotently by `trial_id`, and a failing sink never changes a result. `python3 -m hbench export` replays a run into the sinks. |
| Span | `traces.otlp.jsonl`, `POST <endpoint>/v1/traces` | OTLP/JSON trace of a run: the trace id is the `run_id`, the run is the root span, each trial is a child span whose id is the `trial_id`. Attributes are ids, status, model, token counts and cost; never prompt, response or log content. An unknown value is absent. |
| Cost per solved task | `cost_per_solved` | Main ranking metric: total cost of a target divided by its solved trials. |
| pass@k | `pass_at_k` (report) | Chance that at least one of k runs of a task passes (capability). Unbiased estimator averaged over tasks; k is the smallest run count of any task for the target. |
| pass^k | `pass_hat_k` (report) | Chance that all k runs of a task pass (consistency). |
| Tie | `tie_with` (report), `Tie` column | A target whose cost per solved task interval overlaps the interval of its group leader; the ranking cannot separate them. |
| Pareto frontier | `pareto` (report) | Targets no other target beats on both cost per run and pass rate. |
| Negative control | `null` harness (`parser: null`) | A harness that calls no model. If it solves a task, the hidden tests are too lenient. |
| TrialSpec | `hbench.core.models.TrialSpec`, one line of `plan` output | Names one trial (`run_id`, `trial_id`, task, harness, model, `rep`, `config_sha256`) so any worker can run it with `run-trial`. Carries no command, prompt, path, env or secret: the worker resolves everything from its own copy of the config, and a spec that does not match it is refused. |

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
| Suite | `tasks` in the config | The list of tasks a run uses. |
| Task | task (`id`, `repo`, `ref`, `prompt`, `test`, `hidden_tests`, `protected_paths`) | One problem with a starting repo and a way to grade it. |
| Grader | task key `graders` (default: `test` command + `hidden_tests`); contract model `GraderResult` | Decides pass or fail and gives a score from 0 to 1. Bundled types: `hidden_tests`, `command`, `diff_constraints`. Several graders combine through the task key `grade` (`all`, `any`, `weighted`). |
| Runner | harness entry (`command` + `parser`); `HarnessRunner` protocol | Adapter that builds the agent argv and parses its output. It never starts a process: the engine does. |
| Plugin | entry point in `harness_bench.runners` or `harness_bench.graders` | A runner or grader shipped by another package. Built-in names always win. |
| Score | `GraderResult.score`, the `SCORE:` line | Partial credit from 0 to 1. Pass or fail stays the exit code. |
| Tool calls | `tool_calls` (usage), `Tool calls/run`, `Tool calls/solved` | Tool invocations the agent made, where the output exposes them; unknown is `None`, never `0`. |
| Trial status | `passed` (bool) | Plan adds `error` and `timeout`. Today an agent crash or timeout counts as a failure. |
| Usage | token fields in the row (`input`, `output`, `reasoning`, `cache_read`, `cache_write`, `turns`, `tool_calls`) | Tokens and tool calls parsed from the harness output. |
| Cost source | `cost_litellm`, `cost_computed`, `cost_reported` | Origin of a cost. Plan names: `gateway`, `price_table`, `harness`. Order of preference is the same. Unknown cost is `None`, never `0`. |
| Gateway | LiteLLM | Proxy that bills the model calls and keeps spend logs. |
| Bench tag | `run_tag`, `BENCH_TAG` | Per-trial tag sent as `x-litellm-tags`; `reconcile` matches spend logs by it. |
| Sandbox | `sandbox.prefix` | Command prefix (for example `docker run ...`) that isolates the agent and the test command. |
| Cost per solved task | `cost_per_solved` | Main ranking metric: total cost of a target divided by its solved trials. |
| pass@k | `pass_at_k` (report) | Chance that at least one of k runs of a task passes (capability). Unbiased estimator averaged over tasks; k is the smallest run count of any task for the target. |
| pass^k | `pass_hat_k` (report) | Chance that all k runs of a task pass (consistency). |
| Tie | `tie_with` (report), `Tie` column | A target whose cost per solved task interval overlaps the interval of its group leader; the ranking cannot separate them. |
| Pareto frontier | `pareto` (report) | Targets no other target beats on both cost per run and pass rate. |
| Negative control | `null` harness (`parser: null`) | A harness that calls no model. If it solves a task, the hidden tests are too lenient. |

# harness-bench docs

| Guide | What it covers |
|---|---|
| [Running a model × harness benchmark](running-a-benchmark.md) | Choosing models and tasks, building the matrix, running and reading the results |
| [Orchestrator integration](orchestrator-integration.md) | Running the trials of one run on many workers: `plan`, `run-trial`, `finish`, the `TrialSpec`, retries, shared disk and the example adapters |
| [Machine setup](machine-setup.md) | Linux, macOS, Windows (WSL2) and corporate networks |
| [Roadmap](roadmap.md) | Proposed improvements, based on OpenBench, LiteLLM Lens and the Hugging Face harness adapters study |
| [Evolution plan](evolution-plan.md) | Phases 0–9 to turn the scripts into a model selection eval with a versioned data contract |
| [Architecture](architecture.md) | Current engine structure and the couplings to break |
| [Decisions](adr/) | ADRs: stdlib runtime, incremental extraction, unittest, uv dev tooling, data contract and package, report statistics, runner and grader protocols, suites as data, artifact graders and the LLM judge, pluggable sandbox, resume and doctor, sinks and telemetry, the plan and run-trial API |
| [Phase 0 plan](superpowers/plans/2026-11-23-phase-0-safety-net.md) | Task-by-task plan for golden tests, CI and secret scanning |
| [Phase 1 plan](superpowers/plans/2026-11-24-phase-1-data-contract.md) | Task-by-task plan for the data contract, structured output, report from the contract and the single CLI |
| [Phase 2 plan](superpowers/plans/2026-11-24-phase-2-statistics.md) | Task-by-task plan for confidence intervals, pass@k, ties, Pareto frontier, negative control and paired comparison |
| [Phase 3 plan](superpowers/plans/2026-11-24-phase-3-runners-graders.md) | Task-by-task plan for runner and grader protocols, bundled graders, entry-point plugins, partial credit and tool calls |
| [Phase 4 plan](superpowers/plans/2026-11-24-phase-4-suites.md) | Task-by-task plan for versioned suites, `suite list` and `suite validate`, example suites, the by-kind report and the contamination test |
| [Phase 5 plan](superpowers/plans/2026-11-24-phase-5-artifact-graders.md) | Task-by-task plan for `html_check`, `structured_output`, the `llm_rubric` judge, calibration and the artifacts example suite |
| [Phase 6 plan](superpowers/plans/2026-11-24-phase-6-sandbox-resume.md) | Task-by-task plan for the sandbox protocol and its `docker` and `local-tmp` types, limits, process-group cleanup, resume with a heartbeat, the image digest and `doctor` |
| [Phase 7 plan](superpowers/plans/2026-11-24-phase-7-sinks-telemetry.md) | Task-by-task plan for the sink protocol, the `sqlite` and `otlp` sinks, `export`, the reference SQL and the example dashboard |
| [Phase 8 plan](superpowers/plans/2026-11-25-phase-8-orchestrator-api.md) | Task-by-task plan for `hbench/api.py`, `TrialSpec`, concurrent-safe appends, the `plan`, `run-trial` and `finish` commands, the adapters and the guide |

Terms are defined in [`CONTEXT.md`](../CONTEXT.md).

The full reference, with every script and config, is [`HARNESS_BENCH.md`](../HARNESS_BENCH.md).

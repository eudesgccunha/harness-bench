# harness-bench docs

| Guide | What it covers |
|---|---|
| [Running a model × harness benchmark](running-a-benchmark.md) | Choosing models and tasks, building the matrix, running and reading the results |
| [Machine setup](machine-setup.md) | Linux, macOS, Windows (WSL2) and corporate networks |
| [Roadmap](roadmap.md) | Proposed improvements, based on OpenBench, LiteLLM Lens and the Hugging Face harness adapters study |
| [Evolution plan](evolution-plan.md) | Phases 0–9 to turn the scripts into a model selection eval with a versioned data contract |
| [Architecture](architecture.md) | Current engine structure and the couplings to break |
| [Decisions](adr/) | ADRs: stdlib runtime, incremental extraction, unittest, uv dev tooling, data contract and package, report statistics, runner and grader protocols, suites as data |
| [Phase 0 plan](superpowers/plans/2026-11-23-phase-0-safety-net.md) | Task-by-task plan for golden tests, CI and secret scanning |
| [Phase 1 plan](superpowers/plans/2026-11-24-phase-1-data-contract.md) | Task-by-task plan for the data contract, structured output, report from the contract and the single CLI |
| [Phase 2 plan](superpowers/plans/2026-11-24-phase-2-statistics.md) | Task-by-task plan for confidence intervals, pass@k, ties, Pareto frontier, negative control and paired comparison |
| [Phase 3 plan](superpowers/plans/2026-11-24-phase-3-runners-graders.md) | Task-by-task plan for runner and grader protocols, bundled graders, entry-point plugins, partial credit and tool calls |
| [Phase 4 plan](superpowers/plans/2026-11-24-phase-4-suites.md) | Task-by-task plan for versioned suites, `suite list` and `suite validate`, example suites, the by-kind report and the contamination test |

Terms are defined in [`CONTEXT.md`](../CONTEXT.md).

The full reference, with every script and config, is [`HARNESS_BENCH.md`](../HARNESS_BENCH.md).

# Roadmap

Proposed improvements, based on good practices from [OpenBench](https://github.com/minghinmatthewlam/openbench) (O), [LiteLLM Lens CLI](https://docs.litellm.ai/docs/proxy/lens/cli) (L) and the [Hugging Face harness adapters study](https://huggingface.co/blog/harness-adapters-at-scale) (HF). Listed in suggested priority order. All of them keep the project on the Python standard library.

Each item is scheduled in a phase of the [evolution plan](evolution-plan.md):

| Item | Phase |
|---|---|
| M1 Wilson CI, M4 discrimination, M5 recommendation (text), M11 paired comparison | 2 (done, [ADR 0006](adr/0006-report-statistics.md)) |
| M5 chart (`summary.html`) | 7 (sinks) |
| M10 efficiency (tool calls) | 3 (done, [ADR 0007](adr/0007-runner-and-grader-protocols.md)) |
| M2 `validate` | 4 (done, [ADR 0008](adr/0008-suites-as-data.md): `suite validate --run`) |
| Artifact graders and LLM judge (extension of M9) | 5 (done, [ADR 0009](adr/0009-artifact-graders-and-judge.md)) |
| M12 task packs | 9 (the Exercism and Terminal-Bench importers need network access and a license review per pack; the suite `provenance` field is their hook) |
| M3 `null` harness | 2 (done: negative control) |
| M6 failure breakdown, M14 transcript hygiene | 1 |
| M7 provenance | 1 (versions in the contract), 6 (image digest) |
| M8 `doctor`, M15 egress restriction | 6 |
| M9 partial credit | 3 (done, grader `score`) |
| M13 trace export | 7 |
| M16 skills axis | after 2 (needs M1, M3, M11) |

## M1. Wilson confidence interval in the report (O)

Add a `wilson95` column to `summary.md` and the CSV. With few repetitions, it shows when two pairs cannot be told apart yet.

```python
import math


def wilson_interval(solved: int, runs: int, z: float = 1.96) -> tuple[float, float]:
    """Return the Wilson score interval for a binomial proportion."""
    if runs == 0:
        return (0.0, 1.0)
    p = solved / runs
    denom = 1 + z**2 / runs
    center = (p + z**2 / (2 * runs)) / denom
    half = z * math.sqrt(p * (1 - p) / runs + z**2 / (4 * runs**2)) / denom
    return (max(0.0, center - half), min(1.0, center + half))
```

## M2. `validate` command for tasks (O)

For each task, apply the hidden tests and check that they **fail** on the untouched `ref` and **pass** with the reference solution. The solution can be a `solution_ref` field (the fix commit) or a `solution/` directory. This catches tests that accept anything or reject the right answer.

## M3. `null` harness as a negative control (O)

A built-in harness that does nothing and spends no tokens. It must score 0% on every task. Any pass means a test is too lenient.

## M4. Discrimination report and difficulty filter (O, HF)

Flag tasks that every pair solves or that no pair solves, and add `--drop-saturated`. These tasks do not separate the pairs.

## M5. Recommendation and cost-versus-quality chart (L)

- A recommendation line, as in Lens: the cheapest pair per solved task among those within N percentage points (default 5) of the best pass rate, with at least one pass.
- A `summary.html` with a cost × pass rate scatter plot (inline SVG, no dependencies).

## M6. Failure breakdown (L)

Classify each failed run as `wrong_answer` (test failed), `timeout`, `harness_error` (CLI exited with an error), `tool_misuse` (for example, it changed a protected file) or `no_change` (empty diff). Show the breakdown per pair in the report.

## M7. Provenance and reproducibility (O)

Record in each `results.jsonl` line: each CLI's version, the config hash, the task hash, the Docker image digest, the OS and architecture, and the reasoning effort. This makes it safe to merge results from several machines.

## M8. `doctor` command (O)

A check that spends no tokens, or one minimal call:

- CLIs present in the image, with the expected versions;
- each matrix model answering on the right endpoint;
- the tag reaching LiteLLM (one cheap call, then a look at `/spend/logs`);
- the spend key holding the `get_spend_routes` permission.

## M9. Partial credit (O)

Done in Phase 3: a `hidden_tests` or `command` grader with `"score": true` reads the last `SCORE: <0.0–1.0>` line of the command's stdout as the grader score. Exit code 0 still decides pass or fail, and without the option or a score line the score is 1.0 on pass and 0.0 otherwise. Useful for large tasks with many tests. A score is only as trustworthy as the test output, which agent-written code can influence.

## M10. Efficiency metrics (HF)

Done in Phase 3: the report has `Tool calls/run` and `Tool calls/solved` next to tokens and turns (unknown when a format does not expose them). In the HF study, efficiency gains explained much of the accuracy gain.

## M11. Paired comparison between pairs

Every pair runs the same tasks, so a paired test (McNemar) can say whether pair A beats pair B, with a p-value. It is more sensitive than comparing pass rates on their own.

## M12. Imported task packs (O)

Import tasks from Exercism (MIT) and Terminal-Bench (Apache-2.0), with provenance and license per task, and report them in a tier separate from your own tasks.

## M13. Trace export to Lens (L)

Optional: export each run as an OTLP trace (`LENS_TRACE_ENDPOINT`) with the run tag, to investigate failures in Lens. Or document how to use the Lens plugin for Claude Code and Codex inside the sandbox.

## M14. Transcript hygiene (O)

A `scrub` command that removes emails, home paths, hostnames and key-shaped strings from logs, and a `publish` mode that builds a shareable bundle (report and filtered results) **without** raw transcripts.

## M15. Egress restriction

A Docker network with an egress proxy that allows only the LiteLLM host (see "Why not OpenSandbox (yet)" in `HARNESS_BENCH.md`). It reduces the risk of a prompt injection leaking data.

## M16. Skills axis

Measure whether a skill or an `AGENTS.md` block changes solve rate and cost, with a no-skill control and a paired comparison. Needs M1, M3 and M11 first. Full design: [skills-axis.md](skills-axis.md).

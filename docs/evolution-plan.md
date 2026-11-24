# Evolution plan

> Planning document. Goal: evolve harness-bench without losing its core idea, which is
> **model selection eval**: find the harness × model combination that solves the tasks
> at the lowest cost per solved task.

The first draft of this plan was written from the README only. The section
[Adaptations to repo rules](#adaptations-to-repo-rules) records where it changed after
reading the code. The decisions are in [`adr/`](adr/), the terms in
[`../CONTEXT.md`](../CONTEXT.md) and the current structure in
[`architecture.md`](architecture.md).

---

## 0. Principles (non-negotiable)

1. **The core stays model selection.** Every new feature must answer "does this improve
   the harness × model comparison?". If it does not, it stays out or becomes an optional
   plugin.
2. **Backward compatibility.** The current commands, flags (`--dry-run`, `--reps`,
   `--only`) and `summary.md` keep working. The Markdown becomes a view derived from the
   structured results.
3. **Infrastructure-agnostic core.** The core does not import an orchestrator, a
   database, observability or cloud SDKs. All of that comes in as optional *adapters*.
4. **Real cost first.** Cost comes from the gateway spend logs. The price table and the
   cost reported by the harness are fallbacks, and every result records its origin
   (`cost_source`).
5. **Reproducibility.** Every result records the version of the harness, the model, the
   image (digest), the suite and the grader.
6. **Secure by default.** Agents run without approval prompts, so isolation is mandatory
   and opting out is explicit (the current rule of requiring a sandbox or
   `--allow-unsandboxed` stays).
7. **Generic and public.** The repository uses only public or synthetic tasks, fixtures
   and examples. See section 9.

### Non-goals

- Not an observability platform, and not a replacement for a trace backend.
- No production UI of its own. Dashboards are left to external tools (for example,
  Grafana or Metabase), fed by the sinks.
- No scheduling and no queue. That is the job of an external orchestrator, through an
  adapter.
- Not an agent framework.

---

## Adaptations to repo rules

| Draft said | Repo rule or fact | Decision |
|---|---|---|
| Pydantic v2 models, pip extras, entry points | AGENTS.md: Python 3.12, standard library only; the runtime installs no dependencies | Runtime stays stdlib. Frozen `dataclasses` with hand-written validation; JSON Schema written by hand and checked by a test. Dev tools only in CI and pre-commit. [ADR 0001](adr/0001-stdlib-runtime.md) |
| New `harness_bench/` package from the start | `harness_bench.py` is the single source of truth; the wrapper and `HARNESS_BENCH.md` depend on it | Extract modules one at a time behind characterization tests; the two scripts stay as entry points. [ADR 0002](adr/0002-incremental-extraction.md) |
| `pytest` | No installs at runtime; `tests/smoke.py` is plain Python | New tests use `unittest`. [ADR 0003](adr/0003-test-runner.md) |
| `pip` and a requirements file for dev tools | Dev tools need a reproducible, hash-pinned install | uv manages dev tooling only; `uv.lock` is the source, `Makefile` wraps commands. [ADR 0004](adr/0004-uv-dev-tooling.md) |
| Python ≥ 3.11 | Both scripts exit below 3.12 | Python 3.12 |
| Fake harness to be written (Phase 0) | `tests/fake_agent.py` already mimics every CLI | Reuse it |
| Resume a run (Phase 6) | `done_keys` already skips finished `(task, harness, model, rep)` rows | Phase 6 keeps that key and moves it to `trial_id` |
| Target, trial, suite, grader | Code says pair, run, tasks, test | Glossary in [`CONTEXT.md`](../CONTEXT.md) |
| Separate `roadmap.md` items | M1–M16 overlap the phases | Each M-item now names its phase in [`roadmap.md`](roadmap.md) |

---

## 1. Current state

| Exists | Note |
|---|---|
| Same tasks on harness × model pairs | Core idea |
| Pass/fail from hidden tests | Code grader |
| Real cost from gateway spend logs | Differentiator; keep |
| Harnesses: Claude Code, Pi, OpenCode, Codex CLI, Cursor | Cursor reports pass rate and time only |
| `--dry-run`, `--reps`, `--only` | Keep |
| `summary.md` output | Becomes a derived view |
| Isolation: temp workdir, minimal env, protected tests, redacted logs | Keep and test |
| Docker image with the CLIs, smoke tests, Docker E2E | Base for characterization tests |
| MIT license | Keep |

---

## 2. Target architecture

```
harness_bench/
  core/          # dataclass models, IDs, schema versioning, errors
  suites/        # loading and validation of suites/tasks (JSON)
  runners/       # one adapter per harness (HarnessRunner protocol)
  graders/       # Grader protocol + implementations
  cost/          # cost resolution: gateway -> price table -> harness
  stats/         # pass rate, CI, pass@k, pass^k, cost per success, ranking
  sandbox/       # Sandbox protocol: local-tmp, docker (default), plugins
  sinks/         # json/jsonl (default), markdown (default), optional ones
  telemetry/     # optional OpenTelemetry (no-op when not installed)
  cli/           # current commands + new subcommands
adapters/        # optional packages: orchestrators, databases, storage
examples/        # public/synthetic suites, example configs
docs/
```

Suite files stay JSON (stdlib), not YAML.

Data flow:

```
Suite ──► Planner (task × harness × model × rep)
            │
            ▼
        Sandbox.create ──► HarnessRunner.run ──► Graders ──► CostResolver
            │                                               │
            ▼                                               ▼
        TrialResult (JSON, versioned schema) ──► Sinks (jsonl, md, ...)
            │
            ▼
        stats ──► RunSummary / Ranking ──► summary.md (compatible)
```

### Plugin extension

Third parties add runners, graders, sandboxes and sinks without touching the core,
through `importlib.metadata.entry_points` groups (`harness_bench.graders`, and so on).
Optional adapters that need third-party packages (OpenTelemetry, Postgres, S3) live
outside the core and are never imported by it.

---

## 3. Data contract (heart of the evolution)

Rules:
- Frozen `dataclasses` with validation in `__post_init__`; unknown fields rejected when
  loading.
- `schema_version` in every persisted artifact. A breaking change bumps the major
  version.
- Stable IDs: `run_id` (UUID), deterministic `trial_id`
  (hash of `run_id + task_id + target_id + rep`).
- Raw content (prompts, transcripts, diffs) **never** goes into the result. The result
  keeps only references (`artifact_uri`), because the content may be sensitive for the
  user.

Sketch (illustrative, refined in Phase 1):

```python
"""Core models of harness-bench."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

SCHEMA_VERSION = "1.0.0"


class CostSource(StrEnum):
    GATEWAY = "gateway"
    PRICE_TABLE = "price_table"
    HARNESS = "harness"
    UNKNOWN = "unknown"


class TrialStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"  # infrastructure failure, not the agent's
    TIMEOUT = "timeout"


@dataclass(frozen=True, kw_only=True)
class Target:
    """One candidate combination: harness × model."""

    harness: str
    harness_version: str
    model: str
    params: dict[str, str | int | float | bool] = field(default_factory=dict)

    @property
    def target_id(self) -> str:
        return f"{self.harness}@{self.harness_version}:{self.model}"


@dataclass(frozen=True, kw_only=True)
class GraderResult:
    grader: str
    grader_version: str
    passed: bool
    score: float | None = None  # 0..1
    details_uri: str | None = None


@dataclass(frozen=True, kw_only=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None  # unknown is None, never 0
    cost_source: CostSource = CostSource.UNKNOWN


@dataclass(frozen=True, kw_only=True)
class TrialResult:
    schema_version: str = SCHEMA_VERSION
    run_id: str
    trial_id: str
    task_id: str
    suite_version: str
    target: Target
    rep: int
    status: TrialStatus
    graders: tuple[GraderResult, ...]
    usage: Usage
    latency_s: float
    started_at: datetime
    image_digest: str | None = None
    trace_id: str | None = None
    artifact_uri: str | None = None
```

Important distinction: `ERROR` (infrastructure) does not count as an agent failure. It
goes into a separate metric and does not pollute the ranking.

---

## 4. Phases

Each phase ends with something usable and does not break the previous one.

### Phase 0 — Safety net (no new features)

Goal: refactor without fear. Implementation plan:
[`superpowers/plans/2026-11-23-phase-0-safety-net.md`](superpowers/plans/2026-11-23-phase-0-safety-net.md).

- [x] Characterization tests: run the current flow with fake agents and freeze the
      generated `summary.md` (golden file).
- [x] CI with `ruff check`, `ruff format --check`, `mypy` (strict per new module),
      `bandit -r`, unit tests and `tests/smoke.py`.
- [x] Secret scanning in CI (gitleaks) and `pre-commit`.
- [x] Coverage gate: `make coverage` also measures the engine subprocesses started by smoke and golden tests; `fail_under` is the measured baseline.
- [x] Map the coupling between the two scripts and record it in
      [`architecture.md`](architecture.md).
- [x] Deterministic fake harness: `tests/fake_agent.py`.

Acceptance: green CI and a golden file identical to current behavior.

### Phase 1 — Data contract and structured output

Implementation plan:
[`superpowers/plans/2026-11-24-phase-1-data-contract.md`](superpowers/plans/2026-11-24-phase-1-data-contract.md).
Decisions: [ADR 0005](adr/0005-data-contract-and-package.md).

- [x] Implement `core/` (section 3).
- [x] Write `results/<run_id>/trials.jsonl` + `run.json` (run metadata).
- [x] Generate `summary.md` **from** the JSONL (the golden file still matches).
- [x] Single CLI, keeping `harness_bench.py` and `harness_bench_litellm.py` as shims.
- [x] `harness-bench schema export` writes JSON Schema for data consumers.

Acceptance: same results as before and a JSONL that validates against the schema.

### Follow-ups from Phase 0 review

Engine items deliberately left alone in Phase 0 (no behavior change allowed there):

- [x] `step()` closure in `cmd_run` (ruff B023 ignored) → pass loop values as arguments.
- [x] Long lines in `harness_bench.py` (E501 ignored per file) → wrap, then drop the ignore.
- [x] `git` is called by name (bandit B607 skipped) → resolve once with `shutil.which`.
- [x] `cmd_report` crashes with `IndexError` on an empty `results.jsonl` (pinned by `test_empty_results_file_crashes_with_index_error`) → exit with "no results", update the test.

### Phase 2 — Statistics for decisions

A ranking by cost per success is only trustworthy with explicit uncertainty.

- [ ] Pass rate with a confidence interval (Wilson).
- [ ] pass@k (capability) and pass^k (consistency) per target.
- [ ] Cost per solved task, with a bootstrap CI.
- [ ] Ranking with statistical ties (targets with overlapping CIs are marked as tied).
- [ ] Cost × pass rate Pareto frontier in `summary.md`.
- [ ] Minimum `--reps` recommendation for a desired CI width.

Acceptance: `summary.md` shows CI, pass^k and ties. `stats/` functions have tests with
known cases.

### Phase 3 — Runner and grader protocols

- [ ] `HarnessRunner` protocol: `prepare`, `run`, `collect_usage`, `version`. Move the
      current harnesses to adapters.
- [ ] `Grader` protocol: takes the final workspace and metadata, returns `GraderResult`.
- [ ] Bundled graders:
  - `hidden_tests` (the current one)
  - `command` (any command with an exit code, for example lint, type-check, build)
  - `diff_constraints` (allowed/forbidden files, diff size)
- [ ] Grader composition: `all`, `any`, weighted.
- [ ] Registration through entry points.

Acceptance: a new grader can be added from another package without touching the core.

### Phase 4 — Suites as data

- [ ] Versioned suite format (JSON) with task, repository/fixture (fixed commit), prompt,
      graders, timeout and tags.
- [ ] `harness-bench suite validate` and `suite list`.
- [ ] Example suites **with public or synthetic repositories only**:
  - dependency upgrade in a sample Python project
  - fix of a known, public vulnerability in a sample project
  - small refactor with existing tests
- [ ] Split between **capability** suites (hard, low pass rate) and **regression**
      suites (must always pass).
- [ ] Contamination guard: hidden tests outside the agent workspace (exists; add an
      automated test that proves it).

Acceptance: anyone clones the repo and runs an example suite from scratch.

### Phase 5 — Tasks beyond code (optional, keeps the focus)

The harness × model comparison also applies to agents that produce artifacts other than
code. It comes in as an extension, without changing the core.

- [ ] `llm_rubric` grader: LLM judge through the OpenAI-compatible gateway, versioned
      rubric, structured output, `temperature=0`, judge recorded in the result.
- [ ] `html_check` grader: valid HTML, required elements, no external scripts, broken
      links.
- [ ] `structured_output` grader: validates JSON against a schema.
- [ ] Judge calibration: a command that compares judge scores with human labels on a
      sample and reports agreement. Without calibration, the rubric is marked
      "experimental" in the report.
- [ ] Redaction hook before content goes to the judge (interface; the implementation
      belongs to the user).

Acceptance: an example suite that generates a synthetic HTML report runs with rubric +
`html_check`.

### Phase 6 — Pluggable sandbox and long tasks

- [ ] `Sandbox` protocol: `create`, `exec`, `copy_in`, `copy_out`, `destroy`.
- [ ] Implementations: `local-tmp` (dev, needs an explicit flag) and `docker` (default).
      Other sandboxes come in as third-party plugins.
- [ ] Per-trial limits: timeout, CPU, memory, network (gateway allowlist).
- [ ] Heartbeat and checkpoint for long trials; resume an interrupted run, skipping
      finished trials (idempotent by `trial_id`; today by `done_keys`).
- [ ] Image pinned by digest and recorded in the result.

Acceptance: kill the process mid-run and resume without repeating finished trials.

### Phase 7 — Sinks and telemetry (optional)

- [ ] Sinks: `jsonl` and `markdown` (default); `postgres`, `s3`, `parquet` as adapters.
- [ ] Reference SQL schema in `docs/sql/` with scorecard queries (pass rate, pass^k,
      cost per success per target).
- [ ] Optional OpenTelemetry:
  - span per run and per trial, attributes `eval.run_id`, `eval.trial_id`,
    `eval.target_id`, `eval.status`, and `gen_ai.*` from the semantic conventions
  - **no** prompt/response content in spans by default
  - no-op when not installed
- [ ] Propagate `run_id` as metadata on the gateway call, to join with spend logs.
- [ ] Example dashboard (importable JSON) for an open source dashboard tool, with
      synthetic data.

Acceptance: the same run feeds JSONL, a database and traces, and `trial_id` links the
three.

### Phase 8 — Orchestrator integration (optional)

- [ ] Stable Python API: `plan(suite, targets, reps) -> list[TrialSpec]` and
      `run_trial(spec) -> TrialResult`. That is what any orchestrator calls.
- [ ] Serializable `TrialSpec` (JSON), to run on any worker.
- [ ] Example adapters in `adapters/` (separate packages) for popular orchestrators.
      Each is only a shell over `run_trial`.
- [ ] Guide "how to integrate with your orchestrator" that assumes none in particular.

Acceptance: `run_trial` behaves the same from the CLI, a script and an adapter.

### Phase 9 — From benchmark to decision

- [ ] `harness-bench compare <run_a> <run_b>`: ranking diff and regressions.
- [ ] `harness-bench recommend`: best target under constraints (for example minimum pass
      rate, maximum cost, maximum latency).
- [ ] CI gate: fail when the current target's pass rate drops below the threshold on the
      regression suite.
- [ ] Export the decision as a versionable config file (the "winning target").

Acceptance: a model/harness switch is justified by a reproducible report.

---

## 5. Suggested PR sequence

| # | PR | Phase |
|---|---|---|
| 1 | CI (ruff, mypy, bandit, unittest, smoke, gitleaks) + pre-commit | 0 |
| 2 | Characterization tests (golden `summary.md`) | 0 |
| 3 | `core/` dataclass models + JSON Schema export | 1 |
| 4 | JSONL writer + derived `summary.md` | 1 |
| 5 | Single CLI + shims for the old scripts | 1 |
| 6 | `stats/` (Wilson, pass@k, pass^k, bootstrap, Pareto) | 2 |
| 7 | `HarnessRunner` protocol + harness migration | 3 |
| 8 | `Grader` protocol + `command` and `diff_constraints` | 3 |
| 9 | Suite format + validation + public example suites | 4 |
| 10 | `llm_rubric`, `html_check`, calibration | 5 |
| 11 | `Sandbox` protocol + limits + resume | 6 |
| 12 | Optional sinks + reference SQL | 7 |
| 13 | Optional OpenTelemetry | 7 |
| 14 | `plan`/`run_trial` API + integration guide | 8 |
| 15 | `compare`, `recommend`, CI gate | 9 |

Small PRs, each with tests and a `CHANGELOG.md` entry.

---

## 6. Code quality

- Python 3.12, standard library at runtime; one `pyproject.toml` for tool config only.
- `ruff` (lint + format), `mypy --strict` on new modules, `bandit`.
- No `shell=True` in `subprocess`; arguments as lists; timeouts always.
- Structured logs with secret redaction (exists; covered by `tests/smoke.py`).
- Minimum coverage on `core/`, `stats/`, `graders/`.
- Semantic versioning and release notes.
- Release with provenance (signed images, pinned CI actions and tool versions), given the
  recent supply chain attacks in the LLM gateway ecosystem.

---

## 7. Metrics the report must show

| Metric | Purpose |
|---|---|
| Pass rate (95% CI) | Average capability |
| pass@k | Can the target solve it in k attempts? |
| pass^k | Does the target solve it consistently? |
| Cost per solved task (CI) | Main ranking criterion |
| Tokens in/out | Cost diagnosis |
| Latency p50/p95 | Operational constraint |
| `ERROR`/`TIMEOUT` rate | Infrastructure health, separate from quality |
| Pareto frontier | Cost × quality decision |

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Becoming a platform and losing focus | Principle 1 + non-goals + adapters outside the core |
| Misleading ranking with few reps | CI, statistical ties, `--reps` recommendation |
| Inconsistent LLM judge | Versioned rubric, calibration, "experimental" mark |
| Contamination (agent sees the tests) | Tests outside the workspace + automated test |
| Destructive agent | Mandatory sandbox, restricted network, secrets out of the env |
| Results not comparable over time | Versions of schema, suite, grader, harness, image |
| Unrepresentative tasks | Document how each user builds their own suites |

---

## 9. Content hygiene

The repository is public and open source.

**Content**
- [ ] No code, config, prompt, data, system name, diagram, architecture, endpoint,
      customer or use case from any employer or client.
- [ ] Suites, fixtures and examples only from public or synthetic repositories, with
      compatible licenses recorded.
- [ ] Generic infrastructure examples (several possible orchestrators, sandboxes and
      backends), never a mirror of one real environment.
- [ ] No logs, traces or results from runs made in third-party environments.
- [ ] `gitleaks` in CI and pre-commit; review history before each release.

**Community**
- [x] `LICENSE` (MIT).
- [ ] `CONTRIBUTING.md` with DCO (`Signed-off-by`), `SECURITY.md`, `CODE_OF_CONDUCT.md`.
- [ ] README notice that agents run code without approval and must run in a disposable
      environment.

---

## 10. Definition of done (v1.0)

- [ ] Current CLI compatible, with `summary.md` derived from the JSONL.
- [ ] Versioned data contract published as JSON Schema.
- [ ] Ranking with CI, pass^k and statistical ties.
- [ ] Runners, graders, sandboxes and sinks pluggable through entry points.
- [ ] At least 3 public example suites that run from scratch.
- [ ] `plan`/`run_trial` API documented for orchestrator integration.
- [ ] CI with ruff, mypy, bandit, tests and secret scanning.
- [ ] Docs: architecture, how to build a suite, how to build a grader, how to
      integrate, security model.

---

## References

- Anthropic — *Demystifying evals for AI agents* (task, trial, grader, pass@k vs pass^k,
  capability vs regression).
- OpenTelemetry — GenAI Semantic Conventions.
- Wilson score interval (CI for proportions).
- Python Packaging — entry points.

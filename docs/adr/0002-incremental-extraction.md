# ADR 0002: Extract modules incrementally from the engine

- Status: accepted
- Date: 2026-11-23

## Context

The evolution plan targets a `harness_bench/` package (`core/`, `runners/`, `graders/`,
`stats/`, ...). Today `harness_bench.py` is the single source of truth.
`harness_bench_litellm.py` calls `cmd_run`, `cmd_reconcile` and `cmd_report`, and
`scripts/build_md.py` bundles the source files into `HARNESS_BENCH.md`. See
[`architecture.md`](../architecture.md).

## Decision

- No big-bang rewrite. Phase 0 freezes current behavior with golden tests first.
- Modules move out of `harness_bench.py` one at a time, each in its own PR, with the
  golden tests and `tests/smoke.py` green before and after.
- `harness_bench.py` and `harness_bench_litellm.py` stay as entry points with the same
  commands and flags. When the single CLI lands (Phase 1), they become shims.
- Every PR that adds a source file also adds it to `scripts/build_md.py` and, when the
  Docker build uses it, to the `.dockerignore` allowlist.
- The security defenses listed in AGENTS.md move with the code they protect, and their
  cases in `tests/smoke.py` keep passing unchanged.

## Consequences

- The engine stays runnable after every PR.
- The AGENTS.md map must be updated each time a module moves.

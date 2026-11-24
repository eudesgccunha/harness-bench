# ADR 0004: uv manages dev tooling

- Status: accepted
- Date: 2026-11-24

## Context

Phase 0 added ruff, mypy, bandit, coverage, pre-commit and actionlint. The first CI
draft installed them with `pip` from a hand-compiled `requirements-dev.txt` with hashes.
[ADR 0001](0001-stdlib-runtime.md) keeps the runtime on the standard library, and
[ADR 0003](0003-test-runner.md) keeps tests runnable with plain `python3`.

## Decision

- [uv](https://docs.astral.sh/uv/) manages **dev tooling only**. Tools live in
  `[dependency-groups] dev` in `pyproject.toml`; `[project] dependencies` stays empty and
  `[tool.uv] package = false`.
- `uv.lock` is the hash-pinned source of truth for tool versions. CI and pre-commit use
  it (`uv sync --locked --group dev`, `uv run ...`), so versions are defined once.
- A `Makefile` wraps the common commands; `make check` is the full "Done when".
- This replaces the `requirements-dev.in` / `requirements-dev.txt` approach.

## Consequences

- `python3 tests/smoke.py` and the unittest suite still run with plain `python3`, no installs.
- Contributors need uv for lint, types, security and coverage, not for running the benchmark.
- Updating a tool is `uv lock --upgrade-package <name>` plus review of `uv.lock`.

# ADR 0001: The runtime stays on the standard library

- Status: accepted
- Date: 2026-11-23

## Context

The [evolution plan](../evolution-plan.md) draft asked for Pydantic v2 models, a pip
package with optional extras, and `ruff`, `mypy`, `bandit` and `gitleaks`. AGENTS.md
says: "Python 3.12, standard library only. The runtime environment does not install
dependencies." The benchmark runs on another machine and inside a sandbox image where
nothing is installed with pip.

## Decision

- Code that runs a benchmark (`harness_bench.py`, the wrapper and every module extracted
  from them) imports only the standard library.
- Data models are `@dataclass(frozen=True, kw_only=True)` with validation in
  `__post_init__`. Loading rejects unknown fields.
- The JSON Schema for results is written by hand, versioned with `SCHEMA_VERSION`, and a
  test checks that it matches the dataclass fields.
- Plugins, when they arrive, use `importlib.metadata.entry_points`.
- Development tools (`ruff`, `mypy`, `bandit`, `gitleaks`, `pre-commit`) run only in CI
  and in pre-commit. A `pyproject.toml` holds their config and declares no runtime
  dependencies.
- Adapters that need third-party packages (OpenTelemetry, Postgres, S3) live outside the
  core and are never imported by it.

## Consequences

- No install step on the benchmark machine or in the sandbox image.
- More validation code to write and test than with Pydantic.
- A contributor without the dev tools can still run `python3 tests/smoke.py`.

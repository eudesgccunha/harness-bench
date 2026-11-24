# ADR 0003: New tests use unittest

- Status: accepted
- Date: 2026-11-23

## Context

The plan draft listed `pytest`. [ADR 0001](0001-stdlib-runtime.md) keeps the runtime on
the standard library, and `tests/smoke.py` is plain Python with an exit code.

## Decision

- New tests are `unittest` test cases in `tests/test_*.py`.
- Command: `python3 -m unittest discover -s tests -p 'test_*.py'`.
- `tests/smoke.py` stays the end-to-end gate, with one case per security defense.

## Consequences

- Tests run on any machine with Python 3.12, without installs.
- No pytest fixtures or plugins. Shared setup goes into helper functions in `tests/`.

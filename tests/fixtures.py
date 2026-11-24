"""Shared test fixtures: fake benchmark config, temp dirs, results rows and output capture.

Used by every test module (smoke, golden and the in-process unit tests) so they exercise the
same setup. Importing it puts the repo root on sys.path, so tests can import the engine directly.
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # lets tests import the engine in-process (coverage sees it directly)
PY = sys.executable
AGENT = str(ROOT / "tests" / "fake_agent.py")
HARNESS_KINDS = ("claude", "opencode", "pi", "codex", "cursor")


def prices() -> dict[str, dict[str, int]]:
    """A fresh price table (one entry, `x`), so callers may mutate it."""
    return {"x": {"input": 3, "output": 15}}


def harness(kind: str, extra_env: Mapping[str, str], mode: str = "{model_name}") -> dict[str, Any]:
    """Config entry that runs tests/fake_agent.py as harness `kind`."""
    return {
        "parser": kind,
        "command": [PY, AGENT, kind, mode],
        "env": {"ANTHROPIC_CUSTOM_HEADERS": "x-litellm-tags: {run_tag}", **extra_env},
    }


def fizzbuzz_task() -> dict[str, Any]:
    """A fresh copy of the fizzbuzz task, so callers may mutate it."""
    return {
        "id": "fizzbuzz",
        "repo": "tasks/fizzbuzz/repo",
        "prompt": "fix it",
        "hidden_tests": "tasks/fizzbuzz/hidden_tests",
        "test": f"{PY} -m unittest -q test_hidden.py",
    }


def matrix() -> list[dict[str, Any]]:
    """One entry per harness kind; `nofix` makes the agent leave the bug in place."""
    return [
        {"harness": "claude", "model": "fix"},
        {"harness": "opencode", "model": "p/nofix"},
        {"harness": "pi", "model": "p/fix"},
        {"harness": "codex", "model": "fix", "price": "x"},
        {"harness": "cursor", "model": "fix", "price": "x"},
    ]


def fake_config(extra_env: Mapping[str, str]) -> dict[str, Any]:
    """Full benchmark config with all five fake harnesses; `extra_env` goes into each harness env."""
    return {
        "reps": 1,
        "harnesses": {kind: harness(kind, extra_env) for kind in HARNESS_KINDS},
        "matrix": matrix(),
        "prices": prices(),
        "tasks": [fizzbuzz_task()],
    }


def tmpdir(test: unittest.TestCase) -> Path:
    """A temp directory removed when `test` finishes."""
    path = Path(tempfile.mkdtemp(prefix="hb-unit-")).resolve()
    test.addCleanup(shutil.rmtree, path, True)
    return path


def run_row(**overrides: Any) -> dict[str, Any]:
    """A complete results.jsonl row (the fields `aggregate` and `reconcile` read)."""
    row: dict[str, Any] = {
        "task": "t1",
        "harness": "h",
        "model": "m",
        "rep": 1,
        "passed": True,
        "input": 100,
        "output": 10,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "turns": 1,
        "wall_s": 1.0,
        "parse_ok": True,
        "run_tag": "tag-1",
        "t_start": 1_700_000_000.0,
        "t_end": 1_700_000_010.0,
        "cost_reported": None,
        "cost_computed": None,
    }
    row.update(overrides)
    return row


def write_rows(path: Path, *rows: dict[str, Any]) -> None:
    """Write `rows` as a results.jsonl file at `path`."""
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@contextlib.contextmanager
def quiet() -> Iterator[io.StringIO]:
    """Capture stdout (the engine prints progress); yields the buffer."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def call_quiet(fn: Callable[..., Any], *args: Any) -> str:
    """Call `fn(*args)` with stdout captured and return what it printed."""
    with quiet() as buf:
        fn(*args)
    return buf.getvalue()

"""The grader protocol, its inputs and the `SCORE:` convention (ADR 0007).

A grader never starts a process: it asks the engine through `GradeContext.run`, which applies the env
allowlist, the sandbox wrapper and log redaction.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from hbench.core.models import GraderResult

# run(command, timeout, log_name) -> (exit code, timed out, stdout, stderr). A score is only ever read from stdout.
RunStep = Callable[[str, int, str], tuple[int | None, bool, str, str]]

_SCORE_LINE = re.compile(r"SCORE:\s*([0-9.]+)\s*")


@dataclass(frozen=True)
class Change:
    """One file the agent changed: POSIX path relative to the workdir and the changed line counts."""

    path: str
    added: int
    deleted: int


@dataclass(frozen=True)
class GradeContext:
    """Everything a grader may use: the workdir, the task config, the benchmark dir, the agent's changes
    (captured before protected paths are restored) and the engine's `run`."""

    workdir: Path
    task: Mapping[str, Any]
    base: Path
    changes: tuple[Change, ...]
    run: RunStep


@runtime_checkable
class Grader(Protocol):
    name: str
    version: str

    def grade(self, ctx: GradeContext) -> GraderResult:
        """The verdict for one trial."""


def parse_score(output: str, warn: Callable[[str], None] | None = None) -> float | None:
    """Score from the last `SCORE: <number>` line of `output`, or None.

    A value outside [0, 1] or not a number gives None (the caller falls back to pass/fail) and a warning.
    The last line wins, so an earlier line printed by agent code cannot override the real one."""
    value: str | None = None
    for line in output.splitlines():
        match = _SCORE_LINE.fullmatch(line)
        if match:
            value = match.group(1)
    if value is None:
        return None
    try:
        score = float(value)
    except ValueError:
        score = -1.0
    if not 0 <= score <= 1:
        if warn:
            warn(f"warning: ignored SCORE value {value!r}: use a number from 0 to 1")
        return None
    return score

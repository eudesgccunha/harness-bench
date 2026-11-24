"""Shared by the graders that run a command: the verdict from its outcome and the validation of its timeout."""

from collections.abc import Mapping
from typing import Any

from hbench.core.models import GraderResult
from hbench.graders.base import parse_score

DEFAULT_TIMEOUT = 900


def check_timeout(value: Any) -> int:
    """A timeout in seconds: a whole number above 0 (bool is not a number here)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"timeout must be a whole number of seconds above 0, got {value!r}")
    return value


def check_score_flag(cfg: Mapping[str, Any]) -> bool:
    """The grader's `score` option: true to read partial credit from a `SCORE:` line (default false)."""
    value = cfg.get("score", False)
    if not isinstance(value, bool):
        raise ValueError(f"'score' must be true or false, got {value!r}")
    return value


def command_result(
    name: str, version: str, rc: int | None, timed_out: bool, stdout: str, use_score: bool = False
) -> GraderResult:
    """Passed on exit code 0 without a timeout. The score is 1.0 on pass and 0.0 on fail, unless `use_score` is set
    and the last `SCORE:` line of stdout is valid. Stderr is never read: agent code can print to it freely."""
    passed = rc == 0 and not timed_out
    score = parse_score(stdout, print) if use_score else None
    return GraderResult(
        grader=name,
        grader_version=version,
        passed=passed,
        score=score if score is not None else (1.0 if passed else 0.0),
        timed_out=timed_out,
    )

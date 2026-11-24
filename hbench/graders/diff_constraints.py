"""Grader that checks what the agent changed, without running anything."""

import fnmatch
import posixpath
from collections.abc import Mapping, Sequence
from typing import Any

from hbench.core.models import GraderResult
from hbench.graders.base import Change, GradeContext


def _globs(cfg: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = cfg.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"diff_constraints '{key}' must be a list of glob strings")
    return tuple(value)


def _max_lines(cfg: Mapping[str, Any]) -> int | None:
    value = cfg.get("max_changed_lines")
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"diff_constraints 'max_changed_lines' must be a whole number >= 0, got {value!r}")
    return value


def _normalized(path: str) -> str | None:
    """The path relative to the workdir without `.` or `..` parts, or None when it points outside."""
    clean = posixpath.normpath(path)
    if clean.startswith("/") or clean == ".." or clean.startswith("../"):
        return None
    return clean


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


class DiffConstraintsGrader:
    """Options: `allowed` and `forbidden` (glob lists, `fnmatch` on POSIX paths relative to the workdir; an empty
    `allowed` allows everything), `max_changed_lines` and `ignore_lines` (globs of paths left out of the line count
    only, such as `*/__pycache__/*`; the path checks still apply to them). A path outside the workdir is always
    forbidden."""

    name = "diff_constraints"
    version = "1"

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self._allowed = _globs(cfg, "allowed")
        self._forbidden = _globs(cfg, "forbidden")
        self._max_lines = _max_lines(cfg)
        self._ignore_lines = _globs(cfg, "ignore_lines")

    def violations(self, changes: Sequence[Change]) -> list[str]:
        """One message per broken constraint."""
        found = []
        for change in changes:
            path = _normalized(change.path)
            if path is None or _matches(path, self._forbidden):
                found.append(f"forbidden path {change.path!r}")
            elif self._allowed and not _matches(path, self._allowed):
                found.append(f"path {change.path!r} is not allowed")
        total = sum(
            c.added + c.deleted for c in changes if not _matches(_normalized(c.path) or c.path, self._ignore_lines)
        )
        if self._max_lines is not None and total > self._max_lines:
            found.append(f"{total} changed lines, more than {self._max_lines}")
        return found

    def grade(self, ctx: GradeContext) -> GraderResult:
        passed = not self.violations(ctx.changes)
        return GraderResult(grader=self.name, grader_version=self.version, passed=passed, score=1.0 if passed else 0.0)

"""Grader that runs a command and passes on exit code 0."""

from collections.abc import Mapping
from typing import Any

from hbench.core.models import GraderResult
from hbench.graders._command import DEFAULT_TIMEOUT, check_score_flag, check_timeout, command_result
from hbench.graders.base import GradeContext


class CommandGrader:
    """Options: `command` (required), `timeout` in seconds (default 900) and `score` (true reads a `SCORE:` line)."""

    name = "command"
    version = "1"

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        command = cfg.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command grader needs a non-empty 'command' string")
        self._command = command
        self._timeout = check_timeout(cfg.get("timeout", DEFAULT_TIMEOUT))
        self._use_score = check_score_flag(cfg)

    def grade(self, ctx: GradeContext) -> GraderResult:
        rc, timed_out, stdout, _ = ctx.run(self._command, self._timeout, "command")
        return command_result(self.name, self.version, rc, timed_out, stdout, self._use_score)

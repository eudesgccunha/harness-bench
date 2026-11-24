"""The default grader: copy the hidden tests into the workdir, then run the task's test command."""

import shutil
from collections.abc import Mapping
from typing import Any

from hbench.core.models import GraderResult
from hbench.graders._command import DEFAULT_TIMEOUT, check_score_flag, command_result
from hbench.graders.base import GradeContext


class HiddenTestsGrader:
    """Reads the task keys `hidden_tests`, `test` and `test_timeout`. Its one option is `score` (true reads a
    `SCORE:` line from the test output)."""

    name = "hidden_tests"
    version = "1"

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self._use_score = check_score_flag(cfg)

    def grade(self, ctx: GradeContext) -> GraderResult:
        hidden = ctx.task.get("hidden_tests")
        if hidden:
            shutil.copytree((ctx.base / hidden).resolve(), ctx.workdir, dirs_exist_ok=True)
        rc, timed_out, stdout, _ = ctx.run(ctx.task["test"], ctx.task.get("test_timeout", DEFAULT_TIMEOUT), "test")
        return command_result(self.name, self.version, rc, timed_out, stdout, self._use_score)

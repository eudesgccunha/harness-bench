"""Grader that asks an LLM judge to score an artifact against a versioned rubric (ADR 0009).

The grader opens no socket: the engine injects the judge through `GradeContext.judge`. What the judge says goes to
the grader log only. The result records the rubric (`llm_rubric:<id>` at the rubric's version) and the judge model."""

from collections.abc import Mapping
from typing import Any

from hbench.core.models import GraderResult
from hbench.graders._files import asset_root, read_artifact
from hbench.graders._options import reject_unknown, string_option
from hbench.graders.base import GradeContext
from hbench.judge import Judge, JudgeError, JudgeVerdict, Rubric, parse_rubric


class LlmRubricGrader:
    """Options: `file` (the artifact in the workdir) and `rubric` (a rubric file, relative to the suite directory or,
    for a config task, to the benchmark directory). The artifact passes when the judge's score reaches the rubric's
    `pass_threshold`. Anything that goes wrong (no judge, a file that cannot be read, a bad rubric, a judge error)
    fails the grader and is written to its log."""

    name = "llm_rubric"
    version = "1"

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        reject_unknown(cfg, {"file", "rubric"}, self.name)
        self._file = string_option(cfg, "file", self.name)
        self._rubric = string_option(cfg, "rubric", self.name)

    def _failed(self, rubric: Rubric | None, judge: str | None) -> GraderResult:
        name = self.name if rubric is None else f"{self.name}:{rubric.id}"
        version = self.version if rubric is None else rubric.version
        return GraderResult(grader=name, grader_version=version, passed=False, score=0.0, judge=judge)

    def grade(self, ctx: GradeContext) -> GraderResult:
        if ctx.judge is None:
            ctx.log("no judge is configured for this run\n")
            return self._failed(None, None)
        try:
            rubric = parse_rubric(read_artifact(asset_root(ctx.task, ctx.base), self._rubric))
            artifact = read_artifact(ctx.workdir, self._file)
        except ValueError as e:
            ctx.log(f"{e}\n")
            return self._failed(None, ctx.judge.model)
        try:
            verdict = ctx.judge.score(rubric, artifact)
        except JudgeError as e:
            ctx.log(f"{e}\n")
            return self._failed(rubric, ctx.judge.model)
        return self._result(ctx, ctx.judge, rubric, verdict)

    def _result(self, ctx: GradeContext, judge: Judge, rubric: Rubric, verdict: JudgeVerdict) -> GraderResult:
        ctx.log(f"score {verdict.score}: {verdict.reason}\n")
        return GraderResult(
            grader=f"{self.name}:{rubric.id}",
            grader_version=rubric.version,
            passed=verdict.score >= rubric.pass_threshold,
            score=verdict.score,
            judge=judge.model,
        )

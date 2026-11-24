"""Built-in grader registry and the grading plan of a task (ADR 0007, decisions 4 and 6)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeGuard

from hbench.graders.base import Grader
from hbench.graders.command import CommandGrader
from hbench.graders.compose import check_grade
from hbench.graders.diff_constraints import DiffConstraintsGrader
from hbench.graders.hidden_tests import HiddenTestsGrader
from hbench.graders.html_check import HtmlCheckGrader
from hbench.graders.llm_rubric import LlmRubricGrader
from hbench.graders.structured_output import StructuredOutputGrader
from hbench.plugins import GRADER_GROUP, create_plugin

GRADERS: Mapping[str, Callable[[Mapping[str, Any]], Grader]] = {
    "hidden_tests": HiddenTestsGrader,
    "command": CommandGrader,
    "diff_constraints": DiffConstraintsGrader,
    "html_check": HtmlCheckGrader,
    "llm_rubric": LlmRubricGrader,
    "structured_output": StructuredOutputGrader,
}


@dataclass(frozen=True)
class GradePlan:
    """The graders of a task, in order, and how their results combine."""

    graders: tuple[Grader, ...]
    weights: tuple[float, ...]
    mode: str
    threshold: float | None


def _is_grader(obj: object) -> TypeGuard[Grader]:
    return isinstance(obj, Grader)


def grader_for(entry: Mapping[str, Any]) -> Grader:
    """The grader of one `graders` entry: a built-in type, else the plugin of that name.
    Raises ValueError (a `PluginError` for an unknown type) for a bad entry."""
    if not isinstance(entry, Mapping) or not isinstance(entry.get("type"), str):
        raise ValueError("each grader must be an object with a 'type' string")
    name = entry["type"]
    if name in GRADERS:
        return GRADERS[name](entry)
    return create_plugin(GRADER_GROUP, name, GRADERS, entry, _is_grader, "Grader")


def grade_plan(task: Mapping[str, Any]) -> GradePlan:
    """The plan for `task`. Without a `graders` key it is the single `hidden_tests` grader.
    Raises ValueError for a bad `graders` list, weight or `grade` block."""
    entries = task.get("graders", [{"type": "hidden_tests"}])
    if not isinstance(entries, list) or not entries:
        raise ValueError("graders must be a non-empty list")
    graders = tuple(grader_for(e) for e in entries)
    if any(g.name == "hidden_tests" for g in graders) and "test" not in task:
        raise ValueError("the hidden_tests grader needs the task's 'test' command")
    grade = task.get("grade", {})
    if not isinstance(grade, Mapping):
        raise ValueError("grade must be an object with 'mode' and 'threshold'")
    mode, threshold = grade.get("mode", "all"), grade.get("threshold")
    weights = tuple(e.get("weight", 1) for e in entries)
    check_grade(mode, threshold, weights, len(graders))
    return GradePlan(graders=graders, weights=weights, mode=mode, threshold=threshold)


__all__ = ["GRADERS", "GRADER_GROUP", "GradePlan", "grade_plan", "grader_for"]

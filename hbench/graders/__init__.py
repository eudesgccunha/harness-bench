"""Graders: the protocol, bundled graders, composition and the grading plan of a task (ADR 0007)."""

from hbench.graders.base import Change, GradeContext, Grader, parse_score
from hbench.graders.compose import compose
from hbench.graders.registry import GRADER_GROUP, GRADERS, GradePlan, grade_plan, grader_for

__all__ = [
    "GRADERS",
    "GRADER_GROUP",
    "Change",
    "GradeContext",
    "GradePlan",
    "Grader",
    "compose",
    "grade_plan",
    "grader_for",
    "parse_score",
]

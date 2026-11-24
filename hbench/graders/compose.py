"""Combine several grader results into one verdict and score (ADR 0007, decision 6)."""

from collections.abc import Sequence

from hbench.core.models import GraderResult

MODES = ("all", "any", "weighted")


def _score(result: GraderResult) -> float:
    """The grader's score; 1.0 on pass and 0.0 otherwise when it gave none."""
    if result.score is not None:
        return result.score
    return 1.0 if result.passed else 0.0


def check_grade(mode: object, threshold: object, weights: Sequence[object], count: int) -> None:
    """Raises ValueError unless `mode` is known, there is one positive number per grader in `weights`, and a
    `weighted` grade has a `threshold` above 0 and up to 1."""
    if mode not in MODES:
        raise ValueError(f"unknown grade mode {mode!r}: use one of {', '.join(MODES)}")
    if len(weights) != count or not all(_is_positive_number(w) for w in weights):
        raise ValueError("each grader needs a weight above 0 (one per grader)")
    if mode == "weighted":
        _threshold(threshold)


def _threshold(value: object) -> float:
    """The `weighted` pass threshold: a number above 0 and up to 1."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 1:
        raise ValueError("weighted grading needs a threshold above 0 and up to 1")
    return value


def _is_positive_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def compose(
    results: Sequence[GraderResult], mode: str, weights: Sequence[float], threshold: float | None
) -> tuple[bool, float]:
    """`(passed, score)`. `all`: every grader passes, score is the mean. `any`: one passes, score is the max.
    `weighted`: score is the weighted mean, passed when it reaches `threshold` (in (0, 1], required)."""
    if not results:
        raise ValueError("no grader results to compose")
    check_grade(mode, threshold, weights, len(results))
    scores = [_score(r) for r in results]
    if mode == "all":
        return all(r.passed for r in results), sum(scores) / len(scores)
    if mode == "any":
        return any(r.passed for r in results), max(scores)
    score = sum(w * s for w, s in zip(weights, scores, strict=True)) / sum(weights)
    return score >= _threshold(threshold), score

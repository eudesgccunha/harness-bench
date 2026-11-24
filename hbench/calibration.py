"""Judge calibration (ADR 0009): how well an LLM judge agrees with human scores on one rubric.

`calibrate` judges a set of labelled artifacts and measures the mean absolute error of the scores, the agreement of
the pass/fail verdicts and Cohen's kappa. The report calls a judge calibrated only when a calibration file matches the
rubric id, the rubric version and the judge model, with kappa >= `MIN_KAPPA` on at least `MIN_LABELS` labels.
"""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hbench.judge import Judge, Rubric, load_rubric

MIN_KAPPA = 0.6
MIN_LABELS = 20

_CALIBRATION_KEYS = ("rubric_id", "rubric_version", "judge", "n", "mean_absolute_error", "agreement", "kappa")
_LABEL_KEYS = frozenset({"artifact", "rubric", "human_score"})


@dataclass(frozen=True, kw_only=True)
class Label:
    """One artifact with its rubric and the score a human gave it."""

    artifact: str
    rubric: Rubric
    human_score: float


@dataclass(frozen=True, kw_only=True)
class Calibration:
    """The result of calibrating one judge on one rubric version."""

    rubric_id: str
    rubric_version: str
    judge: str
    n: int
    mean_absolute_error: float
    agreement: float
    kappa: float


def cohens_kappa(a: Sequence[bool], b: Sequence[bool]) -> float:
    """Cohen's kappa of two raters' pass/fail labels: `(p_o - p_e) / (1 - p_e)`.

    When both raters only ever give one and the same label, chance agreement is 1 and the formula is 0/0. A sample
    without variation says nothing about how well the judge separates good from bad, so that case is defined as 0.0
    (it can never make a judge calibrated). Raises ValueError for empty or unequal inputs."""
    if not a or len(a) != len(b):
        raise ValueError("kappa needs two non-empty sequences of the same length")
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    chance = (sum(a) / n) * (sum(b) / n) + (1 - sum(a) / n) * (1 - sum(b) / n)
    if chance == 1:
        return 0.0
    return (observed - chance) / (1 - chance)


def calibrate(judge: Judge, labels: Sequence[Label]) -> Calibration:
    """Judge every label and compare with the human scores. All labels must use one rubric (id and version); a
    `JudgeError` aborts the calibration, since a smaller sample would flatter the result. Raises ValueError."""
    if not labels:
        raise ValueError("no labels to calibrate on")
    rubric = labels[0].rubric
    if any((x.rubric.id, x.rubric.version) != (rubric.id, rubric.version) for x in labels):
        raise ValueError("every label must use the same rubric id and version: calibrate one rubric at a time")
    scores = [judge.score(x.rubric, x.artifact).score for x in labels]
    humans = [x.human_score for x in labels]
    judged = [s >= rubric.pass_threshold for s in scores]
    wanted = [h >= rubric.pass_threshold for h in humans]
    return Calibration(
        rubric_id=rubric.id,
        rubric_version=rubric.version,
        judge=judge.model,
        n=len(labels),
        mean_absolute_error=sum(abs(s - h) for s, h in zip(scores, humans, strict=True)) / len(labels),
        agreement=sum(x == y for x, y in zip(judged, wanted, strict=True)) / len(labels),
        kappa=cohens_kappa(judged, wanted),
    )


def calibration_status(calibration: Calibration | None, rubric_id: str, version: str, judge: str) -> str | None:
    """None when `calibration` makes the pair calibrated, else why it does not (the report marks it experimental)."""
    if calibration is None:
        return "no calibration"
    if (calibration.rubric_id, calibration.rubric_version) != (rubric_id, version):
        return f"calibration is for another rubric ({calibration.rubric_id}@{calibration.rubric_version})"
    if calibration.judge != judge:
        return f"calibration is for another judge ({calibration.judge})"
    if calibration.n < MIN_LABELS:
        return f"only {calibration.n} labels, {MIN_LABELS} needed"
    if calibration.kappa < MIN_KAPPA:
        return f"kappa {calibration.kappa:.2f} is below {MIN_KAPPA}"
    return None


def _number(value: object, where: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        raise ValueError(f"{where} must be a number from {low} to {high}")
    return value


def _label(line: str, base: Path) -> Label:
    document = json.loads(line)
    if not isinstance(document, dict) or set(document) != _LABEL_KEYS:
        raise ValueError("a label is an object with artifact, rubric and human_score")
    paths = [document["artifact"], document["rubric"]]
    if not all(isinstance(p, str) and p for p in paths):
        raise ValueError("artifact and rubric must be file paths")
    try:
        artifact = (base / paths[0]).read_text(errors="replace")
    except OSError as e:
        raise ValueError(f"cannot read artifact {paths[0]} ({e.strerror or e})") from e
    return Label(
        artifact=artifact,
        rubric=load_rubric(base / paths[1]),
        human_score=_number(document["human_score"], "human_score", 0, 1),
    )


def read_labels(path: Path) -> list[Label]:
    """The labels of a JSONL file: per line `artifact` and `rubric` (paths, relative to the file) and `human_score`
    in [0, 1]. Raises ValueError naming the line."""
    try:
        text = path.read_text()
    except OSError as e:
        raise ValueError(f"cannot read labels {path} ({e.strerror or e})") from e
    labels = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            try:
                labels.append(_label(line, path.parent))
            except ValueError as e:
                raise ValueError(f"{path} line {number}: {e}") from e
    if not labels:
        raise ValueError(f"{path} has no labels")
    return labels


def write_calibration(path: Path, calibration: Calibration) -> None:
    path.write_text(json.dumps(asdict(calibration), indent=2) + "\n")


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("n must be a whole number >= 0")
    return value


def _calibration(document: Any) -> Calibration:
    if not isinstance(document, dict) or set(document) != set(_CALIBRATION_KEYS):
        raise ValueError(f"a calibration is an object with {', '.join(_CALIBRATION_KEYS)}")
    if not all(isinstance(document[k], str) for k in ("rubric_id", "rubric_version", "judge")):
        raise ValueError("rubric_id, rubric_version and judge must be strings")
    return Calibration(
        rubric_id=document["rubric_id"],
        rubric_version=document["rubric_version"],
        judge=document["judge"],
        n=_count(document["n"]),
        mean_absolute_error=_number(document["mean_absolute_error"], "mean_absolute_error", 0, 1),
        agreement=_number(document["agreement"], "agreement", 0, 1),
        kappa=_number(document["kappa"], "kappa", -1, 1),
    )


def read_calibration(path: Path) -> Calibration:
    """The calibration in `path`. Raises ValueError when it cannot be read or is not a calibration."""
    try:
        return _calibration(json.loads(path.read_text()))
    except OSError as e:
        raise ValueError(f"cannot read calibration {path} ({e.strerror or e})") from e
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from e

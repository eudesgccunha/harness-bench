"""Tests for judge calibration: Cohen's kappa, the calibration run, its file format and `judge calibrate`."""

import json
import os
import unittest
from typing import Any
from unittest import mock

import fake_litellm
import fixtures
import harness_bench as hb
from hbench import cli
from hbench.calibration import (
    MIN_KAPPA,
    MIN_LABELS,
    Calibration,
    Label,
    calibrate,
    calibration_status,
    cohens_kappa,
    read_calibration,
    read_labels,
    write_calibration,
)
from hbench.judge import JudgeError, JudgeVerdict, Rubric
from test_judge import FakeJudge, JudgeServerCase, verdict_reply

T, F = True, False
RUBRIC = Rubric(id="q", version="2", criteria="c", pass_threshold=0.5)


class KappaTests(unittest.TestCase):
    def test_perfect_agreement_is_one(self) -> None:
        self.assertEqual(cohens_kappa([T, F, T, F], [T, F, T, F]), 1.0)

    def test_agreement_no_better_than_chance_is_zero(self) -> None:
        self.assertEqual(cohens_kappa([T, T, F, F], [T, F, T, F]), 0.0)

    def test_systematic_disagreement_is_negative(self) -> None:
        self.assertEqual(cohens_kappa([T, F, T, F], [F, T, F, T]), -1.0)

    def test_a_known_textbook_value(self) -> None:
        # 20 yes/yes, 5 yes/no, 10 no/yes, 15 no/no: p_o = 0.7, p_e = 0.5, kappa = 0.4
        a = [T] * 25 + [F] * 25
        b = [T] * 20 + [F] * 5 + [T] * 10 + [F] * 15
        self.assertAlmostEqual(cohens_kappa(a, b), 0.4)

    def test_when_both_always_give_the_same_label_kappa_is_zero_not_one(self) -> None:
        # Chance agreement is 1, so the formula is 0/0. A sample with no variation shows nothing about how well the
        # judge separates good from bad, so it is defined as 0.0 and can never make a judge calibrated.
        self.assertEqual(cohens_kappa([T] * 30, [T] * 30), 0.0)
        self.assertEqual(cohens_kappa([F] * 30, [F] * 30), 0.0)

    def test_one_constant_rater_against_a_varying_one_is_zero(self) -> None:
        self.assertEqual(cohens_kappa([T] * 4, [T, T, F, F]), 0.0)

    def test_empty_or_unequal_inputs_are_errors(self) -> None:
        for a, b in (([], []), ([T], [T, F])):
            with self.assertRaises(ValueError):
                cohens_kappa(a, b)


def label(human: float, text: str = "artifact", rubric: Rubric = RUBRIC) -> Label:
    return Label(artifact=text, rubric=rubric, human_score=human)


class SequenceJudge:
    """A judge that returns the scores in order."""

    model = "judge-model"

    def __init__(self, *scores: float) -> None:
        self.scores = list(scores)
        self.artifacts: list[str] = []

    def score(self, rubric: Rubric, artifact: str) -> JudgeVerdict:
        self.artifacts.append(artifact)
        return JudgeVerdict(score=self.scores.pop(0), passed=True, reason="")


class CalibrateTests(unittest.TestCase):
    def test_the_measures_over_a_fake_judge(self) -> None:
        judge = SequenceJudge(0.9, 0.2, 0.6, 0.4)
        labels = [label(1.0, "a"), label(0.0, "b"), label(0.4, "c"), label(0.5, "d")]
        got = calibrate(judge, labels)
        self.assertEqual(judge.artifacts, ["a", "b", "c", "d"])
        self.assertEqual((got.rubric_id, got.rubric_version, got.judge, got.n), ("q", "2", "judge-model", 4))
        self.assertAlmostEqual(got.mean_absolute_error, (0.1 + 0.2 + 0.2 + 0.1) / 4)
        # judge: T F T F; human: T F F T (the threshold is 0.5 and 0.5 passes)
        self.assertEqual(got.agreement, 0.5)
        self.assertEqual(got.kappa, 0.0)

    def test_the_pass_threshold_of_the_rubric_applies_to_both_sides(self) -> None:
        strict = Rubric(id="q", version="2", criteria="c", pass_threshold=0.8)
        got = calibrate(SequenceJudge(0.7, 0.9), [label(0.9, rubric=strict), label(0.6, rubric=strict)])
        self.assertEqual(got.agreement, 0.0)  # the judge: F T, the human: T F

    def test_no_labels_or_labels_of_two_rubrics_are_errors(self) -> None:
        other = Rubric(id="q", version="3", criteria="c")
        for labels in ([], [label(1.0), label(1.0, rubric=other)]):
            with self.assertRaises(ValueError):
                calibrate(SequenceJudge(1.0, 1.0), labels)

    def test_a_judge_error_aborts_the_calibration(self) -> None:
        with self.assertRaises(JudgeError):
            calibrate(FakeJudge(JudgeError("down")), [label(1.0)])


def calibration(**overrides: Any) -> Calibration:
    values: dict[str, Any] = {
        "rubric_id": "q",
        "rubric_version": "2",
        "judge": "judge-model",
        "n": 25,
        "mean_absolute_error": 0.12,
        "agreement": 0.9,
        "kappa": 0.7,
    }
    values.update(overrides)
    return Calibration(**values)


class StatusTests(unittest.TestCase):
    def status(self, cal: Calibration | None, version: str = "2", judge: str = "judge-model") -> str | None:
        return calibration_status(cal, "q", version, judge)

    def test_a_matching_calibration_with_enough_labels_and_kappa_is_calibrated(self) -> None:
        self.assertIsNone(self.status(calibration()))
        self.assertIsNone(self.status(calibration(kappa=MIN_KAPPA, n=MIN_LABELS)))

    def test_everything_else_gives_the_reason(self) -> None:
        for cal, version, judge, word in (
            (None, "2", "judge-model", "no calibration"),
            (calibration(rubric_id="other"), "2", "judge-model", "another rubric"),
            (calibration(), "3", "judge-model", "another rubric"),
            (calibration(), "2", "other-model", "another judge"),
            (calibration(kappa=0.59), "2", "judge-model", "kappa"),
            (calibration(n=19), "2", "judge-model", "labels"),
        ):
            reason = self.status(cal, version, judge)
            self.assertIsNotNone(reason)
            self.assertIn(word, reason or "", (cal, version, judge))


class FileTests(unittest.TestCase):
    def test_a_calibration_round_trips_through_its_file(self) -> None:
        path = fixtures.tmpdir(self) / "cal.json"
        write_calibration(path, calibration())
        self.assertEqual(read_calibration(path), calibration())
        self.assertEqual(json.loads(path.read_text())["kappa"], 0.7)

    def test_a_bad_calibration_file_is_a_value_error(self) -> None:
        directory = fixtures.tmpdir(self)
        good = calibration().__dict__
        bad_documents: list[Any] = [
            "{not json",
            [],
            {k: v for k, v in good.items() if k != "kappa"},
            {**good, "extra": 1},
            {**good, "n": 1.5},
            {**good, "n": True},
            {**good, "kappa": "0.7"},
            {**good, "kappa": True},
            {**good, "judge": 3},
            {**good, "agreement": 1.5},
            {**good, "mean_absolute_error": -1},
        ]
        for document in bad_documents:
            path = directory / "c.json"
            path.write_text(document if isinstance(document, str) else json.dumps(document))
            with self.assertRaises(ValueError, msg=repr(document)):
                read_calibration(path)
        with self.assertRaises(ValueError):
            read_calibration(directory / "missing.json")


class LabelsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = fixtures.tmpdir(self)
        (self.dir / "a.html").write_text("<h1>a</h1>")
        (self.dir / "rubric.json").write_text(json.dumps({"id": "q", "version": "2", "criteria": "c"}))
        self.path = self.dir / "labels.jsonl"

    def write(self, *lines: object) -> None:
        self.path.write_text("\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n")

    def test_paths_are_relative_to_the_labels_file(self) -> None:
        self.write({"artifact": "a.html", "rubric": "rubric.json", "human_score": 0.75}, "")
        (got,) = read_labels(self.path)
        self.assertEqual((got.artifact, got.rubric.id, got.human_score), ("<h1>a</h1>", "q", 0.75))

    def test_an_absolute_path_is_used_as_it_is(self) -> None:
        self.write({"artifact": str(self.dir / "a.html"), "rubric": str(self.dir / "rubric.json"), "human_score": 1})
        self.assertEqual(len(read_labels(self.path)), 1)

    def test_every_bad_line_is_a_value_error_naming_the_line(self) -> None:
        base = {"artifact": "a.html", "rubric": "rubric.json", "human_score": 0.5}
        bad_lines: list[object] = [
            "{not json",
            [],
            {k: v for k, v in base.items() if k != "human_score"},
            {**base, "extra": 1},
            {**base, "human_score": 1.5},
            {**base, "human_score": -0.5},
            {**base, "human_score": True},
            {**base, "human_score": "1"},
            {**base, "artifact": 3},
            {**base, "artifact": "missing.html"},
            {**base, "rubric": "missing.json"},
            {**base, "rubric": 3},
        ]
        for line in bad_lines:
            self.write(base, line)
            with self.assertRaises(ValueError, msg=repr(line)) as cm:
                read_labels(self.path)
            self.assertIn("line 2", str(cm.exception), repr(line))

    def test_an_empty_or_missing_file_is_a_value_error(self) -> None:
        self.path.write_text("\n\n")
        for path in (self.path, self.dir / "missing.jsonl"):
            with self.assertRaises(ValueError):
                read_labels(path)


class CalibrateCommandTests(JudgeServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.dir = fixtures.tmpdir(self)
        (self.dir / "a.html").write_text("<h1>a</h1>")
        (self.dir / "rubric.json").write_text(json.dumps({"id": "q", "version": "2", "criteria": "c"}))
        lines = [{"artifact": "a.html", "rubric": "rubric.json", "human_score": h} for h in (1.0, 0.0, 0.9)]
        (self.dir / "labels.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
        self.cfg: dict[str, Any] = {
            "judge": {"base_url": self.url, "api_key": "${FAKE_KEY}", "model": "judge-model", "timeout": 5}
        }
        patched = mock.patch.dict(os.environ, {"FAKE_KEY": fake_litellm.KEY})
        patched.start()
        self.addCleanup(patched.stop)

    def calibrate(self, cfg: Any = None) -> str:
        (self.dir / "config.json").write_text(json.dumps(self.cfg if cfg is None else cfg))
        argv = ["judge", "calibrate", "--config", str(self.dir / "config.json")]
        argv += ["--labels", str(self.dir / "labels.jsonl"), "--out", str(self.dir / "cal.json")]
        with fixtures.quiet() as out:
            cli.main(argv)
        return out.getvalue()

    def test_the_command_judges_every_label_and_writes_the_calibration(self) -> None:
        out = self.calibrate()
        cal = read_calibration(self.dir / "cal.json")
        self.assertEqual((cal.rubric_id, cal.rubric_version, cal.judge, cal.n), ("q", "2", "judge-model", 3))
        self.assertAlmostEqual(cal.mean_absolute_error, (0.1 + 0.9 + 0.0) / 3)
        self.assertAlmostEqual(cal.agreement, 2 / 3)
        self.assertEqual(cal.kappa, 0.0)
        self.assertEqual(len(self.requests()), 3)
        self.assertIn("kappa", out)
        self.assertIn("experimental", out)  # 3 labels and kappa 0 cannot calibrate a judge

    def test_a_calibrated_result_is_announced(self) -> None:
        with mock.patch("hbench.cli.calibrate", return_value=calibration()):
            self.assertIn("calibrated", self.calibrate())

    def test_a_config_without_a_valid_judge_section_exits(self) -> None:
        for cfg in (
            {},
            {"judge": {"base_url": "https://x"}},
            {"judge": {**self.cfg["judge"], "base_url": "http://x.example.com"}},
        ):
            with self.assertRaises(SystemExit, msg=repr(cfg)):
                self.calibrate(cfg)

    def test_a_judge_failure_exits_with_a_message_and_writes_nothing(self) -> None:
        os.environ["FAKE_JUDGE_MODE"] = "http500"
        with self.assertRaises(SystemExit) as cm:
            self.calibrate()
        self.assertIn("judge", str(cm.exception.code))
        self.assertFalse((self.dir / "cal.json").exists())

    def test_bad_labels_exit_with_the_message(self) -> None:
        (self.dir / "labels.jsonl").write_text("{not json\n")
        with self.assertRaises(SystemExit) as cm:
            self.calibrate()
        self.assertIn("line 1", str(cm.exception.code))

    def test_the_judge_key_is_redacted_from_the_artifacts_it_is_shown(self) -> None:
        key = fixtures.fake_secret("calibration")
        (self.dir / "a.html").write_text(f"key={key}")
        self.cfg["judge"]["api_key"] = "${JUDGE_SECRET}"
        sent: list[bytes] = []

        def capture(url: str, headers: Any, body: bytes, timeout: float) -> bytes:
            sent.append(body)
            return verdict_reply()

        with mock.patch.dict(os.environ, {"JUDGE_SECRET": key}), mock.patch.object(hb, "judge_post", capture):
            self.calibrate()
        self.assertEqual(len(sent), 3)
        self.assertTrue(all(key.encode() not in body and b"***REDACTED***" in body for body in sent))


if __name__ == "__main__":
    unittest.main()

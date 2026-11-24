"""Unit tests for the grader protocol helpers: score parsing and verdict composition."""

import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import fixtures
from hbench.core.models import GraderResult
from hbench.graders.base import Change, GradeContext, Grader, parse_score
from hbench.graders.command import CommandGrader
from hbench.graders.compose import compose
from hbench.graders.diff_constraints import DiffConstraintsGrader
from hbench.graders.hidden_tests import HiddenTestsGrader
from hbench.graders.registry import GRADER_GROUP, grade_plan, grader_for


def result(passed: bool, score: float | None) -> GraderResult:
    return GraderResult(grader="g", grader_version="1", passed=passed, score=score)


class ParseScoreTests(unittest.TestCase):
    def test_the_last_score_line_wins(self) -> None:
        self.assertEqual(parse_score("SCORE: 1.0\nnoise\nSCORE: 0.25\n"), 0.25)

    def test_no_score_line_gives_none(self) -> None:
        self.assertIsNone(parse_score("all tests passed\n"))

    def test_out_of_range_and_malformed_values_give_none_with_a_warning(self) -> None:
        for text in ("SCORE: 1.5", "SCORE: 1.2.3", "SCORE: .", "SCORE: 99"):
            warnings: list[str] = []
            self.assertIsNone(parse_score(text, warnings.append), text)
            self.assertEqual(len(warnings), 1, text)

    def test_a_bad_value_without_a_warn_callback_is_just_ignored(self) -> None:
        self.assertIsNone(parse_score("SCORE: 1.5"))

    def test_text_that_is_not_a_score_line_is_ignored_without_a_warning(self) -> None:
        for text in ("SCORE: abc", "SCORE: -0.1", "SCORE:"):
            warnings: list[str] = []
            self.assertIsNone(parse_score(text, warnings.append), text)
            self.assertEqual(warnings, [], text)

    def test_a_bad_last_line_does_not_fall_back_to_an_earlier_one(self) -> None:
        self.assertIsNone(parse_score("SCORE: 0.9\nSCORE: 1.5"))

    def test_surrounding_whitespace_is_allowed_after_the_prefix(self) -> None:
        self.assertEqual(parse_score("SCORE:   0.5   "), 0.5)

    def test_a_line_that_only_contains_score_text_is_ignored(self) -> None:
        self.assertIsNone(parse_score("my SCORE: 1.0 is great"))


class ComposeTests(unittest.TestCase):
    MIXED = [result(True, 1.0), result(False, 0.0)]

    def test_all_requires_every_grader_and_averages(self) -> None:
        self.assertEqual(compose(self.MIXED, "all", [1, 1], None), (False, 0.5))

    def test_any_needs_one_pass_and_takes_the_best_score(self) -> None:
        self.assertEqual(compose(self.MIXED, "any", [1, 1], None), (True, 1.0))

    def test_weighted_compares_the_weighted_mean_with_the_threshold(self) -> None:
        self.assertEqual(compose(self.MIXED, "weighted", [3, 1], 0.7), (True, 0.75))
        self.assertEqual(compose(self.MIXED, "weighted", [1, 3], 0.7), (False, 0.25))

    def test_a_missing_score_counts_as_one_on_pass_and_zero_otherwise(self) -> None:
        self.assertEqual(compose([result(True, None), result(False, None)], "all", [1, 1], None), (False, 0.5))

    def test_weighted_score_at_the_threshold_passes(self) -> None:
        self.assertEqual(compose([result(True, 0.7)], "weighted", [1], 0.7), (True, 0.7))

    def test_a_failed_grader_keeps_its_partial_score(self) -> None:
        self.assertEqual(compose([result(False, 0.4)], "any", [1], None), (False, 0.4))
        self.assertEqual(compose([result(False, 0.8), result(True, 0.6)], "weighted", [1, 1], 0.7), (True, 0.7))

    def test_weighted_without_a_valid_threshold_raises(self) -> None:
        for threshold in (None, 0.0, 1.5):
            with self.assertRaises(ValueError):
                compose(self.MIXED, "weighted", [1, 1], threshold)

    def test_empty_results_raise(self) -> None:
        with self.assertRaises(ValueError):
            compose([], "all", [], None)

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            compose(self.MIXED, "median", [1, 1], None)

    def test_weights_must_match_the_results_and_be_positive(self) -> None:
        for weights in ([1], [1, 0], [1, -1]):
            with self.assertRaises(ValueError):
                compose(self.MIXED, "weighted", weights, 0.5)


class ProtocolShapeTests(unittest.TestCase):
    def test_change_and_context_are_plain_values(self) -> None:
        change = Change(path="a.py", added=1, deleted=2)
        self.assertEqual((change.path, change.added, change.deleted), ("a.py", 1, 2))
        ctx = GradeContext(
            workdir=fixtures.ROOT,
            task={},
            base=fixtures.ROOT,
            changes=(change,),
            run=lambda c, t, n: (0, False, "", ""),
        )
        self.assertEqual(ctx.run("x", 1, "n"), (0, False, "", ""))

    def test_a_class_with_name_version_and_grade_is_a_grader(self) -> None:
        class Always:
            name = "always"
            version = "1"

            def grade(self, ctx: GradeContext) -> GraderResult:
                return result(True, 1.0)

        self.assertIsInstance(Always(), Grader)
        self.assertNotIsInstance(object(), Grader)


class FakeRun:
    """Stands in for the engine's `run`: records the calls and answers with a fixed outcome."""

    def __init__(self, rc: int | None = 0, timed_out: bool = False, output: str = "", stderr: str = "") -> None:
        self.outcome = (rc, timed_out, output, stderr)
        self.calls: list[tuple[str, int, str]] = []

    def __call__(self, command: str, timeout: int, log_name: str) -> tuple[int | None, bool, str, str]:
        self.calls.append((command, timeout, log_name))
        return self.outcome


def context(
    run: FakeRun,
    task: Mapping[str, Any] | None = None,
    workdir: Path | None = None,
    base: Path | None = None,
    changes: tuple[Change, ...] = (),
) -> GradeContext:
    return GradeContext(
        workdir=workdir or fixtures.ROOT, task=task or {}, base=base or fixtures.ROOT, changes=changes, run=run
    )


class HiddenTestsGraderTests(unittest.TestCase):
    def test_hidden_tests_are_copied_before_the_test_command_runs(self) -> None:
        base, workdir = fixtures.tmpdir(self), fixtures.tmpdir(self)
        (base / "hidden").mkdir()
        (base / "hidden" / "test_hidden.py").write_text("x")
        seen: list[bool] = []

        def run(command: str, timeout: int, log_name: str) -> tuple[int | None, bool, str, str]:
            seen.append((workdir / "test_hidden.py").exists())
            return 0, False, "", ""

        task = {"test": "pytest", "hidden_tests": "hidden"}
        result = HiddenTestsGrader({}).grade(GradeContext(workdir=workdir, task=task, base=base, changes=(), run=run))
        self.assertEqual((seen, result.passed, result.score, result.grader), ([True], True, 1.0, "hidden_tests"))

    def test_without_hidden_tests_only_the_command_runs_with_the_task_timeout(self) -> None:
        run = FakeRun(rc=1)
        result = HiddenTestsGrader({}).grade(context(run, {"test": "t", "test_timeout": 5}))
        self.assertEqual((run.calls, result.passed, result.score), ([("t", 5, "test")], False, 0.0))

    def test_default_timeout_is_900(self) -> None:
        run = FakeRun()
        HiddenTestsGrader({}).grade(context(run, {"test": "t"}))
        self.assertEqual(run.calls[0][1], 900)

    def test_timeout_fails_the_grader_and_is_flagged(self) -> None:
        result = HiddenTestsGrader({}).grade(context(FakeRun(rc=-1, timed_out=True), {"test": "t"}))
        self.assertEqual((result.passed, result.timed_out, result.score), (False, True, 0.0))

    def test_a_missing_exit_code_is_not_a_pass(self) -> None:
        result = HiddenTestsGrader({}).grade(context(FakeRun(rc=None), {"test": "t"}))
        self.assertFalse(result.passed)

    def test_score_line_gives_partial_credit_only_when_the_grader_opts_in(self) -> None:
        run = FakeRun(rc=1, output="3 of 4\nSCORE: 0.75\n")
        opted = HiddenTestsGrader({"score": True}).grade(context(run, {"test": "t"}))
        plain = HiddenTestsGrader({}).grade(context(run, {"test": "t"}))
        self.assertEqual(((opted.passed, opted.score), (plain.passed, plain.score)), ((False, 0.75), (False, 0.0)))

    def test_without_the_opt_in_a_stdout_score_is_ignored_on_a_pass_too(self) -> None:
        result = HiddenTestsGrader({}).grade(context(FakeRun(rc=0, output="SCORE: 0.2\n"), {"test": "t"}))
        self.assertEqual((result.passed, result.score), (True, 1.0))

    def test_an_earlier_score_line_from_agent_code_loses_to_the_last_one(self) -> None:
        out = "SCORE: 1.0\nreal run\nSCORE: 0.5\n"
        result = HiddenTestsGrader({"score": True}).grade(context(FakeRun(rc=1, output=out), {"test": "t"}))
        self.assertEqual(result.score, 0.5)

    def test_a_score_printed_to_stderr_is_never_read(self) -> None:
        run = FakeRun(rc=1, output="SCORE: 0.1\n", stderr="SCORE: 1.0\n")
        result = HiddenTestsGrader({"score": True}).grade(context(run, {"test": "t"}))
        self.assertEqual((result.passed, result.score), (False, 0.1))

    def test_an_out_of_range_score_falls_back_to_pass_fail(self) -> None:
        with fixtures.quiet() as out:
            run = FakeRun(output="SCORE: 7")
            result = HiddenTestsGrader({"score": True}).grade(context(run, {"test": "t"}))
        self.assertEqual((result.passed, result.score, "ignored SCORE" in out.getvalue()), (True, 1.0, True))

    def test_a_timeout_fails_even_when_the_exit_code_is_zero(self) -> None:
        result = HiddenTestsGrader({}).grade(context(FakeRun(rc=0, timed_out=True), {"test": "t"}))
        self.assertEqual((result.passed, result.timed_out), (False, True))

    def test_a_score_option_that_is_not_a_boolean_is_rejected(self) -> None:
        for bad in ("yes", 1, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                HiddenTestsGrader({"score": bad})


class CommandGraderTests(unittest.TestCase):
    def test_runs_the_command_with_its_timeout_and_log_name(self) -> None:
        run = FakeRun()
        result = CommandGrader({"command": "make lint", "timeout": 30}).grade(context(run))
        self.assertEqual((run.calls, result.passed, result.grader), ([("make lint", 30, "command")], True, "command"))

    def test_default_timeout_is_900_and_failure_scores_zero(self) -> None:
        run = FakeRun(rc=2)
        result = CommandGrader({"command": "x"}).grade(context(run))
        self.assertEqual((run.calls[0][1], result.passed, result.score), (900, False, 0.0))

    def test_timeout_and_score_follow_the_same_rules_as_hidden_tests(self) -> None:
        run = FakeRun(rc=-1, timed_out=True, output="SCORE: 0.2")
        result = CommandGrader({"command": "x", "score": True}).grade(context(run))
        self.assertEqual((result.passed, result.timed_out, result.score), (False, True, 0.2))

    def test_stderr_score_with_a_failing_exit_code_does_not_pass_under_weighted(self) -> None:
        run = FakeRun(rc=1, output="1 failed\n", stderr="SCORE: 1.0\n")
        for cfg in ({"command": "x"}, {"command": "x", "score": True}):
            graded = CommandGrader(cfg).grade(context(run))
            self.assertEqual(compose([graded], "weighted", [1], 0.5)[0], False, msg=str(cfg))

    def test_a_stdout_score_without_the_opt_in_cannot_lift_a_failing_grader(self) -> None:
        graded = CommandGrader({"command": "x"}).grade(context(FakeRun(rc=1, output="SCORE: 1.0\n")))
        self.assertEqual((compose([graded], "weighted", [1], 0.5), graded.score), ((False, 0.0), 0.0))

    def test_bad_config_is_rejected(self) -> None:
        for cfg in (
            {},
            {"command": ""},
            {"command": 3},
            {"command": "x", "timeout": 0},
            {"command": "x", "timeout": "5"},
            {"command": "x", "score": "yes"},
        ):
            with self.assertRaises(ValueError, msg=str(cfg)):
                CommandGrader(cfg)


def changes_of(*paths: str, lines: int = 1) -> tuple[Change, ...]:
    return tuple(Change(path=p, added=lines, deleted=0) for p in paths)


class DiffConstraintsGraderTests(unittest.TestCase):
    def grade(self, cfg: Mapping[str, Any], changes: tuple[Change, ...]) -> GraderResult:
        run = FakeRun()
        result = DiffConstraintsGrader(cfg).grade(context(run, changes=changes))
        self.assertEqual(run.calls, [])  # it runs no command
        return result

    def test_no_constraints_pass_everything(self) -> None:
        self.assertEqual(self.grade({}, changes_of("a.py", "b/c.py")).passed, True)

    def test_a_change_to_a_forbidden_path_fails(self) -> None:
        result = self.grade({"forbidden": ["run_tests.py", "tests/*"]}, changes_of("src/a.py", "tests/x.py"))
        self.assertEqual((result.passed, result.score), (False, 0.0))

    def test_only_allowed_paths_may_change(self) -> None:
        cfg = {"allowed": ["src/*.py"]}
        self.assertTrue(self.grade(cfg, changes_of("src/a.py")).passed)
        self.assertFalse(self.grade(cfg, changes_of("src/a.py", "README.md")).passed)

    def test_forbidden_wins_over_allowed(self) -> None:
        cfg = {"allowed": ["src/*"], "forbidden": ["src/secret.py"]}
        self.assertFalse(self.grade(cfg, changes_of("src/secret.py")).passed)

    def test_max_changed_lines_counts_added_and_deleted(self) -> None:
        cfg = {"max_changed_lines": 10}
        self.assertTrue(self.grade(cfg, (Change("a", 6, 4),)).passed)
        self.assertFalse(self.grade(cfg, (Change("a", 6, 5),)).passed)
        self.assertFalse(self.grade(cfg, (Change("a", 6, 0), Change("b", 5, 0))).passed)

    def test_paths_outside_the_workdir_count_as_forbidden_even_without_a_forbidden_list(self) -> None:
        for path in ("../x", "/etc/passwd", "a/../../x", ".."):
            self.assertFalse(self.grade({}, changes_of(path)).passed, path)

    def test_paths_are_normalized_before_matching(self) -> None:
        cfg = {"forbidden": ["run_tests.py"]}
        for path in ("./run_tests.py", "src/../run_tests.py"):
            self.assertFalse(self.grade(cfg, changes_of(path)).passed, path)

    def test_a_rename_is_seen_as_both_sides(self) -> None:
        cfg = {"forbidden": ["run_tests.py"]}
        self.assertFalse(self.grade(cfg, changes_of("run_tests.py", "renamed.py")).passed)

    def test_violations_are_listed_for_the_log(self) -> None:
        grader = DiffConstraintsGrader({"forbidden": ["a"], "max_changed_lines": 0})
        found = grader.violations(changes_of("a"))
        self.assertEqual(len(found), 2)

    def test_ignore_lines_drops_matching_paths_from_the_count_only(self) -> None:
        pyc = Change("tests/__pycache__/t.pyc", 10**9, 0)
        cfg = {"max_changed_lines": 50, "ignore_lines": ["*/__pycache__/*"]}
        self.assertTrue(self.grade(cfg, (pyc, Change("src/a.py", 5, 5))).passed)
        self.assertFalse(self.grade({"max_changed_lines": 50}, (pyc,)).passed)
        self.assertFalse(self.grade({**cfg, "forbidden": ["tests/*"]}, (pyc,)).passed)
        self.assertFalse(self.grade({**cfg, "allowed": ["src/*"]}, (pyc,)).passed)
        self.assertFalse(self.grade(cfg, (pyc, Change("src/a.py", 30, 30))).passed)

    def test_bad_config_is_rejected(self) -> None:
        for cfg in (
            {"ignore_lines": "*.pyc"},
            {"ignore_lines": [1]},
            {"allowed": "src/*"},
            {"forbidden": [1]},
            {"max_changed_lines": -1},
            {"max_changed_lines": True},
            {"max_changed_lines": "5"},
        ):
            with self.assertRaises(ValueError, msg=str(cfg)):
                DiffConstraintsGrader(cfg)


class GradePlanTests(unittest.TestCase):
    TASK = {"id": "t", "test": "true"}

    def test_a_task_without_graders_gets_the_hidden_tests_grader_and_mode_all(self) -> None:
        plan = grade_plan(self.TASK)
        self.assertEqual(
            ([g.name for g in plan.graders], plan.mode, plan.weights, plan.threshold),
            (["hidden_tests"], "all", (1,), None),
        )

    def test_graders_weights_and_grade_block_are_read(self) -> None:
        task = {
            **self.TASK,
            "graders": [{"type": "hidden_tests", "weight": 3}, {"type": "command", "command": "x"}],
            "grade": {"mode": "weighted", "threshold": 0.6},
        }
        plan = grade_plan(task)
        self.assertEqual(
            ([g.name for g in plan.graders], plan.weights, plan.mode, plan.threshold),
            (["hidden_tests", "command"], (3, 1), "weighted", 0.6),
        )

    def test_unknown_type_names_the_type_and_the_built_ins(self) -> None:
        with self.assertRaises(ValueError) as cm:
            grader_for({"type": "nope"})
        self.assertIn("'nope'", str(cm.exception))
        self.assertIn("hidden_tests", str(cm.exception))
        self.assertEqual(GRADER_GROUP, "harness_bench.graders")

    def test_bad_plans_are_rejected(self) -> None:
        bad: list[dict[str, Any]] = [
            {"graders": []},
            {"graders": "hidden_tests"},
            {"graders": ["hidden_tests"]},
            {"graders": [{"kind": "command"}]},
            {"graders": [{"type": "command"}]},
            {"graders": [{"type": "hidden_tests", "weight": 0}]},
            {"graders": [{"type": "hidden_tests", "weight": "2"}]},
            {"grade": "all"},
            {"grade": {"mode": "median"}},
            {"grade": {"mode": "weighted"}},
            {"grade": {"mode": "weighted", "threshold": 2}},
            {"grade": {"mode": "weighted", "threshold": True}},
        ]
        for extra in bad:
            with self.assertRaises(ValueError, msg=str(extra)):
                grade_plan({**self.TASK, **extra})

    def test_hidden_tests_without_a_test_command_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            grade_plan({"id": "t"})
        grade_plan({"id": "t", "graders": [{"type": "command", "command": "x"}]})


if __name__ == "__main__":
    unittest.main()

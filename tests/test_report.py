"""Tests for the report derived from the contract (`hbench.report`)."""

import math
import re
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

import fixtures
from hbench.core.convert import trials_from_rows
from hbench.calibration import Calibration
from hbench.core.models import GraderResult
from hbench.report import aggregate, fmt_int, fmt_money, fmt_tool_calls, render_markdown, write_csv
from hbench.stats import pass_at_k, pass_hat_k, reps_for_ci_width, wilson_interval
from test_core_models import trial
from test_golden import GOLDEN, mask_summary

NOW = datetime(2026, 11, 24, 12, 30)


def golden_trials() -> list[Any]:
    return trials_from_rows(fixtures.read_rows(GOLDEN / "results.jsonl"))


class GoldenOutputTests(unittest.TestCase):
    def test_markdown_matches_the_golden_summary(self) -> None:
        trials = golden_trials()
        md = render_markdown(aggregate(trials, "auto"), trials, "auto", NOW)
        self.assertEqual(md.split("\n", 1)[0], "# Harness x model — 2026-11-24 12:30")
        self.assertEqual(mask_summary(md), (GOLDEN / "summary.md").read_text())

    def test_csv_matches_the_golden_summary(self) -> None:
        path = fixtures.tmpdir(self) / "summary.csv"
        write_csv(aggregate(golden_trials(), "auto"), path)
        self.assertEqual(path.read_text(), (GOLDEN / "summary.csv").read_text())


class AggregateTests(unittest.TestCase):
    def test_legacy_rows_without_parse_ok_or_costs_aggregate(self) -> None:
        row = {
            "task": "t",
            "harness": "h",
            "model": "m",
            "rep": 1,
            "passed": True,
            "input": 100,
            "output": 10,
            "cache_read": 0,
            "cache_write": 0,
            "turns": 2,
            "wall_s": 3.0,
        }
        (entry,) = aggregate(trials_from_rows([row]), "auto")
        self.assertEqual(
            (entry["cost_total"], entry["cost_source"], entry["parse_failures"], entry["tokens_total"]),
            (None, "n/a", 0, 110),
        )

    def test_forced_gateway_cost_without_data_gives_none_not_zero(self) -> None:
        trials = trials_from_rows([fixtures.run_row(cost_computed=1.0)])
        (entry,) = aggregate(trials, "litellm")
        self.assertEqual((entry["cost_total"], entry["cost_per_solved"], entry["cost_source"]), (None, None, "n/a"))

    def test_forced_source_keeps_the_legacy_name(self) -> None:
        trials = trials_from_rows([fixtures.run_row(cost_computed=1.0)])
        (entry,) = aggregate(trials, "computed")
        self.assertEqual((entry["cost_total"], entry["cost_source"]), (1.0, "computed"))

    def test_unknown_cost_source_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            aggregate(trials_from_rows([fixtures.run_row()]), "bogus")

    def test_unsolved_pair_has_infinite_cost_per_solved(self) -> None:
        trials = trials_from_rows([fixtures.run_row(passed=False, cost_computed=1.0)])
        (entry,) = aggregate(trials, "auto")
        self.assertEqual((entry["cost_per_solved"], entry["tokens_per_solved"]), (float("inf"), float("inf")))


LEGACY_KEYS = [
    "harness",
    "model",
    "cost_source",
    "runs",
    "solved",
    "pass_rate",
    "tokens_total",
    "tokens_per_run",
    "tokens_per_solved",
    "cache_hit",
    "cost_total",
    "cost_per_run",
    "cost_per_solved",
    "cost_per_1m_tokens",
    "avg_turns",
    "avg_wall_s",
    "parse_failures",
]


def pair_trials(
    harness: str, model: str, outcomes: list[bool], costs: list[float | None] | None = None, task: str = "t1"
) -> list[Any]:
    """Trials of one pair on one task: one per outcome, rep 1.. in order."""
    costs = costs if costs is not None else [0.1] * len(outcomes)
    rows = [
        fixtures.run_row(harness=harness, model=model, task=task, rep=i, passed=ok, cost_computed=c)
        for i, (ok, c) in enumerate(zip(outcomes, costs, strict=True), 1)
    ]
    return trials_from_rows(rows)


class StatisticsTests(unittest.TestCase):
    def test_three_reps_get_interval_and_pass_estimators(self) -> None:
        (entry,) = aggregate(pair_trials("h", "m", [True, True, False]), "auto")
        low, high = wilson_interval(2, 3)
        self.assertEqual((entry["pass_ci_low"], entry["pass_ci_high"]), (low, high))
        self.assertEqual(
            (entry["k"], entry["pass_at_k"], entry["pass_hat_k"]), (3, pass_at_k(3, 2, 3), pass_hat_k(3, 2, 3))
        )
        self.assertLessEqual(entry["cost_per_solved_ci_low"], entry["cost_per_solved"])
        self.assertGreaterEqual(entry["cost_per_solved_ci_high"], entry["cost_per_solved"])
        self.assertEqual((entry["null_control"], entry["tie_with"]), (False, None))

    def test_existing_keys_keep_their_order(self) -> None:
        (entry,) = aggregate(pair_trials("h", "m", [True]), "auto")
        self.assertEqual(list(entry)[:17], LEGACY_KEYS)

    def test_k_is_the_smallest_run_count_over_tasks(self) -> None:
        trials = pair_trials("h", "m", [True, False, True], task="a") + pair_trials("h", "m", [True], task="b")
        trials += pair_trials("x", "m", [True], task="a")  # another pair has no runs on task b
        table = {e["harness"]: e for e in aggregate(trials, "auto")}
        self.assertEqual((table["h"]["k"], table["x"]["k"]), (1, 1))
        self.assertAlmostEqual(table["h"]["pass_at_k"], (2 / 3 + 1) / 2)

    def test_unknown_cost_gives_no_interval_tie_or_frontier(self) -> None:
        trials = pair_trials("h", "m", [True, True, True], [0.1, None, 0.1])
        (entry,) = aggregate(trials, "auto")
        self.assertEqual(
            (entry["cost_per_solved"], entry["cost_per_solved_ci_low"], entry["cost_per_solved_ci_high"]),
            (None, None, None),
        )
        self.assertEqual((entry["tie_with"], entry["pareto"]), (None, False))

    def test_zero_passes_have_infinite_cost_and_no_interval(self) -> None:
        (entry,) = aggregate(pair_trials("h", "m", [False, False]), "auto")
        self.assertEqual((entry["cost_per_solved"], entry["cost_per_solved_ci_low"]), (math.inf, None))
        self.assertEqual(entry["tie_with"], None)

    def test_overlapping_cost_intervals_tie_with_the_leader(self) -> None:
        trials = pair_trials("a", "m", [True] * 4, [0.10, 0.10, 0.12, 0.12])
        trials += pair_trials("b", "m", [True] * 4, [0.11, 0.11, 0.12, 0.12])
        table = aggregate(trials, "auto")
        self.assertEqual([e["harness"] for e in table], ["a", "b"])
        self.assertEqual([e["tie_with"] for e in table], [1, 1])

    def test_pareto_marks_pairs_no_other_pair_beats(self) -> None:
        trials = pair_trials("cheap", "m", [True, True], [0.1, 0.1]) + pair_trials(
            "dear", "m", [True, True], [0.2, 0.2]
        )
        trials += pair_trials("weak", "m", [True, False], [0.05, 0.05])
        table = {e["harness"]: e["pareto"] for e in aggregate(trials, "auto")}
        self.assertEqual(table, {"cheap": True, "dear": False, "weak": True})

    def test_null_pairs_are_flagged_and_stay_off_the_frontier(self) -> None:
        trials = pair_trials("null", "none", [True], [0.0]) + pair_trials("h", "m", [True])
        table = {e["harness"]: e for e in aggregate(trials, "auto", null_harnesses={"null"})}
        self.assertEqual((table["null"]["null_control"], table["null"]["pareto"]), (True, False))
        self.assertEqual((table["h"]["null_control"], table["h"]["pareto"]), (False, True))

    def test_target_width_sets_the_repetitions_needed(self) -> None:
        trials = pair_trials("h", "m", [True, False])
        (wide,) = aggregate(trials, "auto", target_ci_width=0.4)
        (narrow,) = aggregate(trials, "auto", target_ci_width=0.1)
        self.assertLess(wide["reps_needed"], narrow["reps_needed"])
        self.assertEqual(narrow["reps_needed"], reps_for_ci_width(0.5, 0.1, 1))


def render(trials: list[Any], null: tuple[str, ...] = (), **kwargs: Any) -> str:
    width = kwargs.get("target_ci_width", 0.2)
    table = aggregate(trials, "auto", null_harnesses=null, target_ci_width=width)
    return render_markdown(table, trials, "auto", NOW, **kwargs)


def judged(rubric: str = "q", version: str = "2", judge: str | None = "judge-model", grader: str | None = None) -> Any:
    result = GraderResult(
        grader=grader or f"llm_rubric:{rubric}", grader_version=version, passed=True, score=0.9, judge=judge
    )
    return trial(graders=(GraderResult(grader="html_check", grader_version="1", passed=True), result))


def calibration(**overrides: Any) -> Calibration:
    values: dict[str, Any] = {
        "rubric_id": "q",
        "rubric_version": "2",
        "judge": "judge-model",
        "n": 25,
        "mean_absolute_error": 0.1,
        "agreement": 0.9,
        "kappa": 0.7,
    }
    values.update(overrides)
    return Calibration(**values)


class JudgesSectionTests(unittest.TestCase):
    def section(self, trials: list[Any], cal: Calibration | None = None) -> str:
        md = render(trials[:1] or [trial()], judged=trials, calibration=cal)
        return md.partition("## Judges")[2]

    def test_no_section_without_llm_rubric_results(self) -> None:
        for judged_trials in ([], [trial()], [judged(grader="llm_rubric")], [judged(judge=None)]):
            self.assertNotIn("## Judges", render([trial()], judged=judged_trials), judged_trials)

    def test_a_pair_without_calibration_is_experimental(self) -> None:
        text = self.section([judged()])
        self.assertIn("| q | 2 | judge-model | **experimental** (no calibration) |", text)
        self.assertIn("kappa >= 0.6", text)

    def test_a_matching_calibration_makes_the_pair_calibrated(self) -> None:
        self.assertIn(
            "| q | 2 | judge-model | calibrated (kappa 0.70, 25 labels) |", self.section([judged()], calibration())
        )

    def test_a_calibration_that_does_not_fit_leaves_it_experimental_with_the_reason(self) -> None:
        for cal, word in (
            (calibration(rubric_version="1"), "another rubric"),
            (calibration(judge="other"), "another judge"),
            (calibration(kappa=0.5), "kappa 0.50"),
            (calibration(n=10), "10 labels"),
        ):
            text = self.section([judged()], cal)
            self.assertIn("**experimental**", text)
            self.assertIn(word, text)

    def test_every_pair_in_use_is_listed_once_and_sorted(self) -> None:
        trials = [judged("b"), judged("a"), judged("a"), judged("a", "3"), judged("a", judge="other")]
        rows = [line for line in self.section(trials).splitlines() if line.startswith("| a") or line.startswith("| b")]
        self.assertEqual(
            [row.split(" | ")[:3] for row in rows],
            [
                ["| a", "2", "judge-model"],
                ["| a", "2", "other"],
                ["| a", "3", "judge-model"],
                ["| b", "2", "judge-model"],
            ],
        )


class RenderStatisticsTests(unittest.TestCase):
    def test_reps_fixture_matches_its_golden(self) -> None:
        trials = trials_from_rows(fixtures.read_rows(GOLDEN / "results_reps.jsonl"))
        self.assertEqual(mask_summary(render(trials, ("null",))), (GOLDEN / "summary_reps.md").read_text())

    def test_main_table_shows_interval_estimators_tie_and_frontier(self) -> None:
        md = render(pair_trials("h", "m", [True, True, False], [0.05, 0.06, 0.07]))
        self.assertIn("| Pass 95% CI | pass@k | pass^k | Cost/solved 95% CI | Tie | Pareto |", md)
        self.assertRegex(md, r"\| 21–94% \| 100% \(k=3\) \| 0% \(k=3\) \| \$0\.0500–inf \| +\| yes \|")

    def test_recommendation_is_the_cheapest_pair_within_the_tolerance(self) -> None:
        trials = pair_trials("best", "m", [True] * 20, [0.5] * 20) + pair_trials(
            "close", "m", [True] * 19 + [False], [0.1] * 20
        )
        trials += pair_trials("far", "m", [True] * 10 + [False] * 10, [0.01] * 20)
        md = render(trials)
        self.assertIn(
            "**Recommendation:** close/m — cheapest per solved task within 5 pp of the best pass rate (100%).", md
        )
        self.assertIn("within 60 pp", render(trials, recommend_within_pp=60))

    def test_unknown_cost_unsolved_and_null_pairs_are_never_recommended(self) -> None:
        trials = pair_trials("nocost", "m", [True], [None]) + pair_trials("nopass", "m", [False])
        trials += pair_trials("null", "none", [True], [0.0])
        self.assertNotIn("Recommendation", render(trials, ("null",)))

    def test_pareto_section_lists_the_frontier(self) -> None:
        md = render(pair_trials("cheap", "m", [True]) + pair_trials("dear", "m", [True], [0.2]))
        self.assertIn("## Pareto frontier (cost per run × pass rate)\n\n- cheap/m: $0.1000 per run, 100%\n", md)
        self.assertNotIn("dear/m: $", md)

    def test_null_control_that_solves_a_task_prints_a_warning(self) -> None:
        md = render(pair_trials("null", "none", [True, False], [0.0, 0.0]) + pair_trials("h", "m", [True]), ("null",))
        self.assertIn("## Negative control\n\n**Warning:** null control solved tasks: null/none (1/2).", md)

    def test_null_control_that_solves_nothing_is_reported_clean(self) -> None:
        md = render(pair_trials("null", "none", [False], [0.0]) + pair_trials("h", "m", [True]), ("null",))
        self.assertIn("## Negative control\n\nnull control solved nothing.", md)

    def test_no_negative_control_section_without_null_pairs(self) -> None:
        self.assertNotIn("Negative control", render(pair_trials("h", "m", [True])))

    def test_discrimination_lists_saturated_and_unsolved_tasks_without_null_pairs(self) -> None:
        trials = pair_trials("a", "m", [True, True], task="easy") + pair_trials("b", "m", [True], task="easy")
        trials += pair_trials("a", "m", [False], task="hard") + pair_trials("b", "m", [False], task="hard")
        trials += pair_trials("a", "m", [True, False], task="mixed") + pair_trials("b", "m", [True], task="mixed")
        trials += pair_trials("null", "none", [False], [0.0], task="easy")
        md = render(trials, ("null",))
        self.assertIn("- Saturated (solved in every run): easy\n- Unsolved (never solved): hard\n", md)

    def test_single_pair_has_discrimination_but_no_paired_comparison(self) -> None:
        md = render(pair_trials("h", "m", [True, False], task="t1") + pair_trials("h", "m", [True], task="t2"))
        self.assertIn("- Saturated (solved in every run): t2", md)
        self.assertNotIn("Paired comparison", md)

    def test_paired_comparison_counts_discordant_runs_against_the_first_pair(self) -> None:
        trials = pair_trials("a", "m", [True] * 6, [0.1] * 6) + pair_trials("b", "m", [False] * 6, [0.2] * 6)
        trials += pair_trials("c", "m", [True] * 6, [0.3] * 6)
        md = render(trials)
        self.assertIn("## Paired comparison vs #1 (McNemar, exact)\n", md)
        self.assertIn("- #3 b/m: b=6, c=0, p=0.0312", md)
        self.assertIn("- #2 c/m: b=0, c=0, p=1.0000", md)

    def test_paired_comparison_skips_pairs_that_share_no_runs(self) -> None:
        md = render(pair_trials("a", "m", [True], task="x") + pair_trials("b", "m", [True], task="y"))
        self.assertNotIn("Paired comparison", md)

    def test_repetitions_section_gives_reps_per_task_for_the_target_width(self) -> None:
        md = render(pair_trials("h", "m", [True, False]), target_ci_width=0.4)
        self.assertIn(
            f"- To reach a pass-rate CI width of 0.40: h/m needs {reps_for_ci_width(0.5, 0.4, 1)} reps per task.", md
        )

    def test_a_report_of_null_pairs_only_has_no_recommendation_or_repetitions(self) -> None:
        md = render(pair_trials("null", "none", [False], [0.0]), ("null",))
        self.assertEqual([h for h in ("Recommendation", "Repetitions", "Discrimination") if h in md], [])
        self.assertIn("null control solved nothing.", md)

    def test_unreachable_repetitions_say_more_than_the_limit(self) -> None:
        trials = pair_trials("h", "m", [True, False], task="a") + pair_trials("h", "m", [True, False], task="b")
        md = render(trials, target_ci_width=1e-9)
        self.assertIn("h/m needs more than 5000 reps per task.", md)

    def test_a_null_control_that_passed_is_ranked_after_every_real_pair(self) -> None:
        trials = pair_trials("null", "none", [True, True], [0.0, 0.0]) + pair_trials("a", "m", [True, True])
        trials += pair_trials("b", "m", [True, False]) + pair_trials("c", "m", [False, False])
        trials += pair_trials("d", "m", [True, True], [None, None])  # unknown cost sorts before the null control
        md = render(trials, ("null",))
        ranks = re.findall(r"^\| (\d) \| (\w+) \|", md, re.M)
        self.assertEqual([h for _, h in ranks], ["a", "b", "c", "d", "null"])
        self.assertIn("- #2 b/m:", md)
        self.assertNotIn("- #1 ", md)
        self.assertIn("**Warning:** null control solved tasks: null/none (2/2).", md)
        self.assertNotIn("**Recommendation:** null", md)

    def test_pairs_with_no_pass_stay_off_the_frontier(self) -> None:
        md = render(pair_trials("good", "m", [True], [0.2]) + pair_trials("free", "m", [False], [0.01]))
        self.assertIn("- good/m:", md)
        self.assertNotIn("- free/m:", md)

    def test_every_review_focus_case_renders(self) -> None:
        trials = pair_trials("h", "m", [False, False], [None, 0.1]) + pair_trials("x", "m", [True], task="other")
        trials += pair_trials("h", "m", [True], [0.1], task="other")
        self.assertIn("## Per task", render(trials))


class FormatTests(unittest.TestCase):
    def test_money_none_inf_small_and_large(self) -> None:
        self.assertEqual([fmt_money(v) for v in (None, math.inf, 0.12345, 12.345)], ["n/a", "inf", "$0.1235", "$12.35"])

    def test_int_none_inf_and_grouping(self) -> None:
        self.assertEqual([fmt_int(v) for v in (None, math.inf, 1234567.4)], ["n/a", "n/a", "1,234,567"])


class ToolCallStatisticsTests(unittest.TestCase):
    @staticmethod
    def trials(*calls: int | None, passed: tuple[bool, ...] | None = None) -> list[Any]:
        outcomes = passed if passed is not None else (True,) * len(calls)
        rows = [
            fixtures.run_row(rep=i, passed=ok, tool_calls=c)
            for i, (c, ok) in enumerate(zip(calls, outcomes, strict=True), 1)
        ]
        return trials_from_rows(rows)

    def test_averages_per_run_and_per_solved_task(self) -> None:
        (entry,) = aggregate(self.trials(4, 6, 8, passed=(True, False, True)), "auto")
        self.assertEqual((entry["tool_calls_per_run"], entry["tool_calls_per_solved"]), (6.0, 9.0))

    def test_one_unknown_trial_makes_the_pair_unknown_not_zero(self) -> None:
        (entry,) = aggregate(self.trials(4, None), "auto")
        self.assertEqual((entry["tool_calls_per_run"], entry["tool_calls_per_solved"]), (None, None))

    def test_a_known_zero_stays_zero(self) -> None:
        (entry,) = aggregate(self.trials(0, 0), "auto")
        self.assertEqual((entry["tool_calls_per_run"], entry["tool_calls_per_solved"]), (0.0, 0.0))

    def test_nothing_solved_gives_infinite_per_solved(self) -> None:
        (entry,) = aggregate(self.trials(3, passed=(False,)), "auto")
        self.assertEqual((entry["tool_calls_per_run"], entry["tool_calls_per_solved"]), (3.0, math.inf))

    def test_the_new_keys_come_last(self) -> None:
        (entry,) = aggregate(self.trials(1), "auto")
        self.assertEqual(list(entry)[-2:], ["tool_calls_per_run", "tool_calls_per_solved"])

    def test_the_table_has_two_new_columns_with_n_a_when_unknown(self) -> None:
        known = render(self.trials(2, 4))
        unknown = render(self.trials(None))
        self.assertIn("| Pareto | Tool calls/run | Tool calls/solved |", known)
        self.assertRegex(known, r"\| 3\.0 \| 3\.0 \|\n")
        self.assertRegex(unknown, r"\| n/a \| n/a \|\n\n")

    def test_nothing_solved_renders_inf_and_not_n_a_in_the_table(self) -> None:
        table = render(self.trials(3, passed=(False,)))
        self.assertRegex(table, r"\| 3\.0 \| inf \|\n")

    def test_formatting(self) -> None:
        self.assertEqual([fmt_tool_calls(v) for v in (None, math.inf, 0.0, 2.55)], ["n/a", "inf", "0.0", "2.5"])


KINDS = {"cap": "capability", "reg": "regression"}


class SuiteKindTests(unittest.TestCase):
    """`## By suite kind`: only when the trials come from more than one suite kind."""

    def mixed(self, regression: list[bool], capability: list[bool] | None = None) -> list[Any]:
        return [
            *pair_trials("h", "m", capability or [True, False], task="cap/a"),
            *pair_trials("h", "m", regression, task="reg/b"),
            *pair_trials("g", "m", [True, True], task="reg/b"),
            *pair_trials("g", "m", [True, True], task="cap/a"),
        ]

    def test_solved_over_runs_per_pair_and_kind(self) -> None:
        md = render(self.mixed([True, True]), suite_kinds=KINDS)
        self.assertIn("## By suite kind", md)
        self.assertIn("| capability | 2/2 | 1/2 |", md)
        self.assertIn("| regression | 2/2 | 2/2 |", md)

    def test_a_failed_regression_task_is_flagged_for_its_pair(self) -> None:
        md = render(self.mixed([True, False]), suite_kinds=KINDS)
        self.assertIn("**Warning:** regression tasks failed: h/m (reg/b 1/2)", md)

    def test_no_warning_when_every_regression_task_passed(self) -> None:
        self.assertNotIn("Warning:** regression", render(self.mixed([True, True]), suite_kinds=KINDS))

    def test_a_null_control_failing_a_regression_task_is_not_flagged(self) -> None:
        trials = [*self.mixed([True, True]), *pair_trials("n", "none", [False], task="reg/b")]
        md = render(trials, null=("n",), suite_kinds=KINDS)
        self.assertNotIn("Warning:** regression", md)

    def test_a_pair_missing_from_a_kind_shows_a_dash(self) -> None:
        trials = [*pair_trials("h", "m", [True], task="cap/a"), *pair_trials("g", "m", [True], task="reg/b")]
        self.assertIn("| capability | 1/1 | - |", render(trials, suite_kinds=KINDS))

    def test_one_kind_only_adds_no_section(self) -> None:
        trials = pair_trials("h", "m", [True], task="reg/b")
        self.assertNotIn("By suite kind", render(trials, suite_kinds=KINDS))

    def test_tasks_of_unknown_suites_and_config_tasks_are_left_out(self) -> None:
        trials = [*pair_trials("h", "m", [True], task="plain"), *pair_trials("h", "m", [True], task="zzz/x")]
        self.assertNotIn("By suite kind", render(trials, suite_kinds=KINDS))

    def test_a_config_task_named_like_a_suite_does_not_count_as_that_suite(self) -> None:
        trials = [*pair_trials("h", "m", [True], task="cap/a"), *pair_trials("h", "m", [True], task="reg")]
        self.assertNotIn("By suite kind", render(trials, suite_kinds=KINDS))

    def test_without_suite_kinds_the_report_has_no_section(self) -> None:
        self.assertNotIn("By suite kind", render(self.mixed([True, True])))


class WriteCsvTests(unittest.TestCase):
    def test_writes_header_and_one_line_per_pair(self) -> None:
        path: Path = fixtures.tmpdir(self) / "s.csv"
        write_csv(aggregate(trials_from_rows([fixtures.run_row()]), "auto"), path)
        self.assertEqual(len(path.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()

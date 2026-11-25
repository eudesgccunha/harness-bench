"""Tests for `hbench.decide`: recommend, the target file and the CI gate (trials are built in memory)."""

import json
import unittest
from datetime import UTC, datetime
from typing import Any

import fixtures
from hbench import decide
from hbench.core.models import Costs, RunInfo, TrialResult, TrialStatus
from test_core_models import run_info, target, target_decision, trial, usage

T1 = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)


def make(
    harness: str,
    model: str,
    task: str,
    passed: bool,
    *,
    cost: float | None = 0.1,
    rep: int = 1,
    latency: float = 10.0,
    status: TrialStatus | None = None,
) -> TrialResult:
    """One trial; `cost=None` is an unknown cost."""
    costs = Costs() if cost is None else Costs(price_table=cost)
    return trial(
        target=target(harness=harness, model=model),
        task_id=task,
        rep=rep,
        status=status or (TrialStatus.PASSED if passed else TrialStatus.FAILED),
        usage=usage(costs=costs),
        latency_s=latency,
    )


def null_trial(task: str = "suite/a", harness: str = "null") -> TrialResult:
    """What the `null` harness records: zero tokens, a real 0.0 cost and a real 0 tool calls."""
    return trial(
        target=target(harness=harness, model="none" if harness == "null" else "m"),
        task_id=task,
        status=TrialStatus.FAILED,
        usage=usage(input_tokens=0, output_tokens=0, costs=Costs(harness=0.0), tool_calls=0),
    )


def finished(**overrides: Any) -> RunInfo:
    return run_info(**{"run_id": "a" * 32, "finished_at": T1, **overrides})


def names(recommendation: decide.Recommendation) -> list[str]:
    return [f"{c.harness}/{c.model}" for c in recommendation.candidates]


def three_targets() -> list[TrialResult]:
    """cheap/m: 1 of 2 solved at $0.1 per run; mid/m: 2 of 2 at $0.2; rich/m: 2 of 2 at $0.9."""
    return [
        make("cheap", "m", "s/a", True, cost=0.1),
        make("cheap", "m", "s/b", False, cost=0.1),
        make("mid", "m", "s/a", True, cost=0.2),
        make("mid", "m", "s/b", True, cost=0.2),
        make("rich", "m", "s/a", True, cost=0.9),
        make("rich", "m", "s/b", True, cost=0.9),
    ]


class ConstraintsTests(unittest.TestCase):
    def test_given_lists_only_the_constraints_that_were_set(self) -> None:
        self.assertEqual(decide.Constraints().given(), {})
        full = decide.Constraints(min_pass_rate=0.5, max_cost_per_solved=1, max_cost_per_run=2, max_latency_s=3)
        self.assertEqual(
            full.given(),
            {"min_pass_rate": 0.5, "max_cost_per_solved": 1, "max_cost_per_run": 2, "max_latency_s": 3},
        )

    def test_out_of_range_values_are_refused(self) -> None:
        for bad in (
            {"min_pass_rate": 1.5},
            {"min_pass_rate": -0.1},
            {"max_cost_per_solved": -1.0},
            {"max_cost_per_run": float("nan")},
            {"max_latency_s": float("inf")},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                decide.Constraints(**bad)


class RecommendTests(unittest.TestCase):
    def test_the_pick_is_the_cheapest_per_solved_task(self) -> None:
        rec = decide.recommend(finished(), three_targets(), decide.Constraints(), "auto")
        # cheap: $0.2 per solved, mid: $0.2 per solved (tie on cost, higher pass rate first), rich: $0.9
        self.assertEqual(names(rec), ["mid/m", "cheap/m", "rich/m"])
        assert rec.pick is not None
        self.assertEqual((rec.pick.harness, rec.pick.failed), ("mid", ()))

    def test_a_pass_rate_floor_excludes_the_cheap_poor_target(self) -> None:
        rec = decide.recommend(finished(), three_targets(), decide.Constraints(min_pass_rate=0.9), "auto")
        by_name = {f"{c.harness}/{c.model}": c for c in rec.candidates}
        self.assertEqual(by_name["cheap/m"].failed, ("pass rate 50% < 90%",))
        assert rec.pick is not None
        self.assertEqual(rec.pick.harness, "mid")

    def test_cost_and_latency_limits_exclude_targets(self) -> None:
        trials = [*three_targets(), make("slow", "m", "s/a", True, cost=0.05, latency=99.0)]
        rec = decide.recommend(
            finished(),
            trials,
            decide.Constraints(max_cost_per_solved=0.5, max_cost_per_run=0.5, max_latency_s=50),
            "auto",
        )
        by_name = {f"{c.harness}/{c.model}": c for c in rec.candidates}
        self.assertEqual(
            by_name["rich/m"].failed, ("cost per solved $0.9000 > $0.5000", "cost per run $0.9000 > $0.5000")
        )
        self.assertEqual(by_name["slow/m"].failed, ("latency 99s > 50s",))
        assert rec.pick is not None
        self.assertEqual(rec.pick.harness, "mid")

    def test_an_unknown_cost_never_meets_a_cost_constraint(self) -> None:
        trials = [make("free", "m", "s/a", True, cost=None), make("paid", "m", "s/a", True, cost=0.5)]
        for constraints in (
            decide.Constraints(max_cost_per_solved=100),
            decide.Constraints(max_cost_per_run=100),
            decide.Constraints(max_cost_per_solved=100, max_cost_per_run=100),
        ):
            rec = decide.recommend(finished(), trials, constraints, "auto")
            by_name = {c.harness: c for c in rec.candidates}
            self.assertEqual(by_name["free"].failed, ("unknown cost",))
            assert rec.pick is not None
            self.assertEqual(rec.pick.harness, "paid")

    def test_an_unknown_cost_is_ranked_after_the_known_ones_not_as_zero(self) -> None:
        trials = [make("free", "m", "s/a", True, cost=None), make("paid", "m", "s/a", True, cost=0.5)]
        rec = decide.recommend(finished(), trials, decide.Constraints(), "auto")
        self.assertEqual(names(rec), ["paid/m", "free/m"])
        assert rec.pick is not None
        self.assertEqual(rec.pick.harness, "paid")

    def test_with_only_unknown_costs_the_pick_has_a_null_cost(self) -> None:
        rec = decide.recommend(finished(), [make("free", "m", "s/a", True, cost=None)], decide.Constraints(), "auto")
        decision = decide.decision_for(rec)
        self.assertEqual((decision.evidence.cost_per_solved, decision.evidence.cost_per_run), (None, None))

    def test_a_target_that_solved_nothing_is_never_picked(self) -> None:
        trials = [make("dud", "m", "s/a", False, cost=0.01), make("ok", "m", "s/a", True, cost=0.5)]
        rec = decide.recommend(finished(), trials, decide.Constraints(), "auto")
        by_name = {c.harness: c for c in rec.candidates}
        self.assertEqual(by_name["dud"].failed, ("solved nothing",))
        assert rec.pick is not None
        self.assertEqual(rec.pick.harness, "ok")

    def test_the_null_control_is_never_recommended_and_is_listed_apart(self) -> None:
        trials = [*three_targets(), null_trial("s/a"), null_trial("s/b")]
        rec = decide.recommend(finished(), trials, decide.Constraints(), "auto")
        self.assertEqual(rec.null_control, ("null/none",))
        self.assertNotIn("null/none", names(rec))
        assert rec.pick is not None
        self.assertNotEqual(rec.pick.harness, "null")

    def test_without_an_eligible_target_there_is_no_pick(self) -> None:
        rec = decide.recommend(
            finished(), three_targets(), decide.Constraints(min_pass_rate=1.0, max_latency_s=1), "auto"
        )
        self.assertIsNone(rec.pick)
        self.assertEqual(rec.tied, ())
        with self.assertRaises(ValueError):
            decide.decision_for(rec)
        self.assertIn("No eligible target", decide.render_recommendation(rec))

    def test_a_run_without_trials_has_no_pick(self) -> None:
        rec = decide.recommend(finished(), [], decide.Constraints(), "auto")
        self.assertEqual((rec.candidates, rec.pick), ((), None))

    def test_an_unknown_cost_source_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            decide.recommend(finished(), three_targets(), decide.Constraints(), "bogus")

    def test_a_pick_tied_with_another_eligible_target_is_warned_about(self) -> None:
        a = [make("a", "m", f"s/{i}", i % 3 != 0, cost=0.10 + i * 0.05) for i in range(8)]
        b = [make("b", "m", f"s/{i}", i % 3 != 1, cost=0.11 + i * 0.05) for i in range(8)]
        rec = decide.recommend(finished(), [*a, *b], decide.Constraints(), "auto")
        self.assertEqual(rec.tied, ("b/m",))
        self.assertIn("statistically tied", decide.render_recommendation(rec))

    def test_a_tied_target_that_is_not_eligible_is_not_a_warning(self) -> None:
        a = [make("a", "m", f"s/{i}", i % 3 != 0, cost=0.10 + i * 0.05) for i in range(8)]
        b = [make("b", "m", f"s/{i}", i % 3 != 1, cost=0.11 + i * 0.05) for i in range(8)]
        rec = decide.recommend(finished(), [*a, *b], decide.Constraints(max_cost_per_run=0.2), "auto")
        self.assertEqual(rec.tied, ())

    def test_json_lists_every_target_with_the_constraints_it_failed(self) -> None:
        rec = decide.recommend(finished(), three_targets(), decide.Constraints(min_pass_rate=0.9), "auto")
        doc = decide.recommendation_json(rec)
        rows = {f"{r['harness']}/{r['model']}": r for r in doc["targets"]}
        self.assertEqual(
            (rows["cheap/m"]["eligible"], rows["cheap/m"]["failed"], rows["mid/m"]["eligible"]),
            (False, ["pass rate 50% < 90%"], True),
        )
        self.assertEqual((doc["pick"]["harness"], doc["constraints"]), ("mid", {"min_pass_rate": 0.9}))
        self.assertEqual(json.loads(json.dumps(doc)), doc)

    def test_json_without_a_pick_and_an_infinite_cost_stay_valid_json(self) -> None:
        rec = decide.recommend(finished(), [make("dud", "m", "s/a", False)], decide.Constraints(), "auto")
        doc = decide.recommendation_json(rec)
        self.assertEqual((doc["pick"], doc["targets"][0]["cost_per_solved"]), (None, None))
        self.assertNotIn("Infinity", json.dumps(doc, allow_nan=False))

    def test_markdown_names_the_pick_its_intervals_and_the_constraints(self) -> None:
        rec = decide.recommend(finished(), three_targets(), decide.Constraints(min_pass_rate=0.9), "auto")
        md = decide.render_recommendation(rec)
        self.assertIn("min pass rate 90%", md)
        self.assertIn("**Pick:** mid/m", md)
        self.assertIn("95% CI", md)
        self.assertIn("pass rate 50% < 90%", md)
        self.assertIn(
            "Negative control",
            decide.render_recommendation(
                decide.recommend(finished(), [*three_targets(), null_trial()], decide.Constraints(), "auto")
            ),
        )


class TargetDecisionTests(unittest.TestCase):
    def test_the_decision_carries_the_run_the_constraints_and_the_evidence(self) -> None:
        info = finished(config_sha256="d" * 64, suite_version="regression@1.0.0")
        rec = decide.recommend(info, three_targets(), decide.Constraints(min_pass_rate=0.9), "computed")
        decision = decide.decision_for(rec)
        self.assertEqual(
            (decision.harness, decision.model, decision.run_id, decision.config_sha256, decision.suite_version),
            ("mid", "m", info.run_id, "d" * 64, "regression@1.0.0"),
        )
        self.assertEqual((decision.cost_source, dict(decision.constraints)), ("computed", {"min_pass_rate": 0.9}))
        ev = decision.evidence
        self.assertEqual((ev.runs, ev.solved, ev.pass_rate, ev.cost_per_solved, ev.cost_per_run), (2, 2, 1.0, 0.2, 0.2))
        self.assertEqual(ev.avg_wall_s, 10.0)
        self.assertLess(ev.pass_ci_low, 1.0)

    def test_the_same_run_gives_the_same_bytes_and_no_timestamp(self) -> None:
        rec = decide.recommend(finished(), three_targets(), decide.Constraints(), "auto")
        first = decide.encode_target(decide.decision_for(rec))
        second = decide.encode_target(decide.decision_for(rec))
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))
        keys = list(json.loads(first))
        self.assertEqual(keys, sorted(keys))
        self.assertNotIn("started_at", first)

    def test_the_file_holds_no_command_env_secret_or_path(self) -> None:
        text = decide.encode_target(target_decision())
        for word in ("command", "env", "secret", "path", "prompt"):
            self.assertNotIn(f'"{word}"', text)


class TargetFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = fixtures.tmpdir(self)

    def test_write_then_load_round_trips(self) -> None:
        decision = target_decision()
        path = self.dir / "target.json"
        decide.write_target(path, decision)
        self.assertEqual(decide.load_target(path), decision)
        self.assertEqual(path.read_text(), decide.encode_target(decision))

    def test_writing_replaces_an_existing_file_atomically_and_leaves_no_temp_file(self) -> None:
        path = self.dir / "target.json"
        path.write_text("old")
        decide.write_target(path, target_decision())
        self.assertEqual(decide.load_target(path), target_decision())
        self.assertEqual([p.name for p in self.dir.iterdir()], ["target.json"])

    def test_writing_refuses_to_follow_a_symlink(self) -> None:
        victim = self.dir / "victim.txt"
        victim.write_text("keep")
        link = self.dir / "target.json"
        link.symlink_to(victim)
        with self.assertRaises(decide.TargetFileError):
            decide.write_target(link, target_decision())
        self.assertEqual(victim.read_text(), "keep")
        self.assertTrue(link.is_symlink())

    def test_writing_into_a_missing_directory_is_a_target_file_error(self) -> None:
        with self.assertRaises(decide.TargetFileError):
            decide.write_target(self.dir / "nope" / "target.json", target_decision())

    def test_loading_refuses_what_is_not_a_valid_decision(self) -> None:
        good = target_decision().to_dict()
        cases = {
            "extra key": {**good, "command": ["x"]},
            "wrong major version": {**good, "schema_version": "2.0.0"},
            "missing key": {k: v for k, v in good.items() if k != "evidence"},
            "not an object": [1, 2],
        }
        for label, document in cases.items():
            path = self.dir / "t.json"
            path.write_text(json.dumps(document))
            with self.subTest(label), self.assertRaises(decide.TargetFileError):
                decide.load_target(path)
        path.write_text("{not json")
        with self.assertRaises(decide.TargetFileError):
            decide.load_target(path)

    def test_loading_refuses_a_missing_file_a_symlink_and_a_huge_file(self) -> None:
        with self.assertRaises(decide.TargetFileError):
            decide.load_target(self.dir / "missing.json")
        real = self.dir / "real.json"
        real.write_text(decide.encode_target(target_decision()))
        link = self.dir / "link.json"
        link.symlink_to(real)
        with self.assertRaises(decide.TargetFileError):
            decide.load_target(link)
        big = self.dir / "big.json"
        big.write_text(" " * (decide.MAX_TARGET_BYTES + 1))
        with self.assertRaises(decide.TargetFileError):
            decide.load_target(big)

    def test_a_file_that_is_a_directory_is_refused(self) -> None:
        (self.dir / "d").mkdir()
        with self.assertRaises(decide.TargetFileError):
            decide.load_target(self.dir / "d")


class GateTests(unittest.TestCase):
    def trials(self) -> list[TrialResult]:
        return [
            make("claude", "m", "regression/a", True),
            make("claude", "m", "regression/b", True),
            make("claude", "m", "regression/c", False),
            make("claude", "m", "other/z", False),
            make("pi", "m", "regression/a", True),
        ]

    def gate(self, info: RunInfo, trials: list[TrialResult], min_pass_rate: float, **kw: str) -> decide.GateResult:
        return decide.gate(
            info,
            trials,
            kw.get("harness", "claude"),
            kw.get("model", "m"),
            kw.get("suite", "regression"),
            min_pass_rate,
        )

    def info(self) -> RunInfo:
        return finished(targets=(target(harness="claude", model="m"), target(harness="pi", model="m")))

    def test_it_passes_at_or_above_the_floor_and_prints_the_interval(self) -> None:
        result = self.gate(self.info(), self.trials(), 2 / 3)
        self.assertTrue(result.ok)
        self.assertIn("pass rate 67% (2/3", result.message)
        self.assertIn("95% CI", result.message)

    def test_it_only_counts_the_trials_of_the_suite(self) -> None:
        result = self.gate(self.info(), self.trials(), 0.9, harness="pi")
        self.assertEqual((result.ok, result.solved, result.runs), (True, 1, 1))

    def test_it_fails_below_the_floor(self) -> None:
        result = self.gate(self.info(), self.trials(), 0.8)
        self.assertFalse(result.ok)
        self.assertIn("below the minimum 80%", result.message)

    def test_error_and_timeout_trials_count_as_failed(self) -> None:
        trials = [
            make("claude", "m", "regression/a", True),
            make("claude", "m", "regression/b", False, status=TrialStatus.ERROR),
            make("claude", "m", "regression/c", False, status=TrialStatus.TIMEOUT),
        ]
        result = self.gate(self.info(), trials, 0.5)
        self.assertEqual((result.ok, result.solved, result.runs), (False, 1, 3))

    def test_it_fails_closed_on_an_unfinished_run(self) -> None:
        result = self.gate(run_info(finished_at=None), self.trials(), 0.0)
        self.assertEqual(result.ok, False)
        self.assertIn("not finished", result.message)

    def test_it_fails_closed_when_the_target_is_not_in_the_run(self) -> None:
        for kw in ({"harness": "ghost"}, {"model": "ghost"}):
            result = self.gate(self.info(), self.trials(), 0.0, **kw)
            self.assertEqual(result.ok, False)
            self.assertIn("not in run", result.message)

    def test_it_fails_closed_without_trials_of_the_suite(self) -> None:
        result = self.gate(self.info(), self.trials(), 0.0, suite="capability")
        self.assertEqual(result.ok, False)
        self.assertIn("no trial of suite 'capability'", result.message)

    def test_a_suite_name_is_matched_whole_not_as_a_prefix(self) -> None:
        trials = [make("claude", "m", "regression-extra/a", True)]
        result = self.gate(self.info(), trials, 0.0)
        self.assertEqual(result.ok, False)

    def test_an_invalid_floor_is_refused(self) -> None:
        for bad in (-0.1, 1.5, float("nan")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.gate(self.info(), self.trials(), bad)


def sweep(harness: str, outcomes: list[bool], *, cost: float | None = 0.1, tasks: int = 0) -> list[TrialResult]:
    """One trial per outcome, on tasks `s/t<offset+i>`."""
    return [make(harness, "m", f"s/t{tasks + i}", ok, cost=cost) for i, ok in enumerate(outcomes)]


def compare(
    a: list[TrialResult],
    b: list[TrialResult],
    *,
    info_a: RunInfo | None = None,
    info_b: RunInfo | None = None,
    **kw: Any,
) -> decide.Comparison:
    return decide.compare(
        (info_a or finished(), a), (info_b or finished(run_id="b" * 32), b), kw.pop("cost_source", "auto"), **kw
    )


class CompareTests(unittest.TestCase):
    def test_a_target_that_loses_enough_paired_outcomes_is_a_regression(self) -> None:
        cmp = compare(sweep("x", [True] * 6), sweep("x", [False] * 6))
        self.assertTrue(cmp.has_regression)
        (reg,) = cmp.regressions
        self.assertEqual((reg["harness"], reg["b"], reg["c"]), ("x", 6, 0))
        self.assertAlmostEqual(reg["p"], 2 / 64)

    def test_too_few_lost_outcomes_are_not_significant(self) -> None:
        cmp = compare(sweep("x", [True] * 5 + [False]), sweep("x", [False] * 5 + [False]))
        self.assertFalse(cmp.has_regression)
        self.assertEqual(cmp.regressions, ())

    def test_alpha_moves_the_threshold(self) -> None:
        a, b = sweep("x", [True] * 5), sweep("x", [False] * 5)
        self.assertFalse(compare(a, b).has_regression)
        self.assertTrue(compare(a, b, alpha=0.1).has_regression)

    def test_an_improvement_or_a_balanced_change_is_not_a_regression(self) -> None:
        better = compare(sweep("x", [False] * 6), sweep("x", [True] * 6))
        self.assertFalse(better.has_regression)
        mixed = compare(sweep("x", [True] * 6 + [False] * 6), sweep("x", [False] * 6 + [True] * 6))
        self.assertEqual((mixed.has_regression, len(mixed.task_regressions)), (False, 6))

    def test_an_error_trial_counts_as_failed(self) -> None:
        errors = [make("x", "m", f"s/t{i}", False, status=TrialStatus.ERROR) for i in range(6)]
        self.assertTrue(compare(sweep("x", [True] * 6), errors).has_regression)

    def test_only_pairs_present_in_both_runs_are_paired(self) -> None:
        cmp = compare(sweep("x", [True] * 6), sweep("x", [False] * 6, tasks=10))
        self.assertFalse(cmp.has_regression)
        self.assertTrue(any("task set differs" in w for w in cmp.warnings))

    def test_a_rep_is_paired_with_the_same_rep(self) -> None:
        a = [make("x", "m", "s/t", True, rep=r) for r in range(1, 7)]
        b = [make("x", "m", "s/t", False, rep=r) for r in range(1, 7)]
        self.assertTrue(compare(a, b).has_regression)
        shifted = [make("x", "m", "s/t", False, rep=r) for r in range(7, 13)]
        self.assertFalse(compare(a, shifted).has_regression)

    def test_a_task_solved_in_a_and_never_in_b_is_listed_as_information(self) -> None:
        a = [make("x", "m", "s/a", True), make("x", "m", "s/b", True), make("x", "m", "s/c", False)]
        b = [make("x", "m", "s/a", False), make("x", "m", "s/b", True), make("x", "m", "s/c", False)]
        cmp = compare(a, b)
        self.assertEqual([(r["harness"], r["task"]) for r in cmp.task_regressions], [("x", "s/a")])
        self.assertFalse(cmp.has_regression)

    def test_a_task_solved_once_in_b_is_not_a_task_regression(self) -> None:
        a = [make("x", "m", "s/a", True, rep=1), make("x", "m", "s/a", True, rep=2)]
        b = [make("x", "m", "s/a", False, rep=1), make("x", "m", "s/a", True, rep=2)]
        self.assertEqual(compare(a, b).task_regressions, ())

    def test_the_ranking_diff_has_ranks_pass_rates_and_costs_of_both_runs(self) -> None:
        a = [*sweep("cheap", [True, True], cost=0.1), *sweep("dear", [True, True], cost=0.5)]
        b = [*sweep("cheap", [True, True], cost=0.9), *sweep("dear", [True, True], cost=0.5)]
        cmp = compare(a, b)
        rows = {row["harness"]: row for row in cmp.targets}
        self.assertEqual((rows["cheap"]["rank_a"], rows["cheap"]["rank_b"]), (1, 2))
        self.assertEqual((rows["dear"]["rank_a"], rows["dear"]["rank_b"]), (2, 1))
        self.assertEqual(rows["cheap"]["a"]["cost_per_solved"], 0.1)
        self.assertEqual(rows["cheap"]["b"]["cost_per_solved"], 0.9)
        self.assertAlmostEqual(rows["cheap"]["delta_cost_per_solved"], 0.8)
        self.assertEqual(rows["cheap"]["delta_pass_rate"], 0.0)

    def test_an_unknown_cost_has_no_delta_not_a_zero(self) -> None:
        a, b = sweep("x", [True, True], cost=0.1), sweep("x", [True, True], cost=None)
        (row,) = compare(a, b).targets
        self.assertEqual((row["b"]["cost_per_solved"], row["delta_cost_per_solved"]), (None, None))

    def test_a_target_that_solves_nothing_has_a_null_cost_per_solved_in_json(self) -> None:
        cmp = compare(sweep("x", [True, True]), sweep("x", [False, False]))
        self.assertIsNone(cmp.targets[0]["b"]["cost_per_solved"])
        self.assertNotIn("Infinity", json.dumps(cmp.to_dict(), allow_nan=False))

    def test_targets_only_in_one_run_are_listed_not_compared(self) -> None:
        a = [*sweep("both", [True] * 6), *sweep("gone", [True] * 6)]
        b = [*sweep("both", [True] * 6), *sweep("new", [False] * 6)]
        cmp = compare(a, b)
        self.assertEqual((cmp.only_a, cmp.only_b), (("gone/m",), ("new/m",)))
        self.assertEqual([row["harness"] for row in cmp.targets], ["both"])
        self.assertFalse(cmp.has_regression)

    def test_the_null_control_is_listed_apart_and_never_compared(self) -> None:
        a = [*sweep("x", [True] * 6), *[null_trial(f"s/t{i}") for i in range(6)]]
        cmp = compare(a, list(a))
        self.assertEqual(cmp.null_control, ("null/none",))
        self.assertEqual([row["harness"] for row in cmp.targets], ["x"])
        self.assertIn("null/none", decide.render_comparison(cmp))

    def test_a_target_that_breaks_completely_in_b_is_still_a_regression(self) -> None:
        # An auth or gateway failure: zero usage, a real 0.0 cost and 0 tool calls look like the null control.
        broken = [null_trial(f"s/t{i}", harness="x") for i in range(6)]
        cmp = compare(sweep("x", [True] * 6), broken)
        self.assertTrue(cmp.has_regression)
        self.assertEqual([r["harness"] for r in cmp.regressions], ["x"])
        self.assertEqual(len(cmp.task_regressions), 6)

    def test_a_target_that_broke_only_in_b_stays_in_the_ranking_with_zero_percent(self) -> None:
        broken = [null_trial(f"s/t{i}", harness="x") for i in range(6)]
        cmp = compare(sweep("x", [True] * 6), broken)
        self.assertEqual(cmp.null_control, ())
        self.assertEqual((cmp.only_a, cmp.only_b), ((), ()))
        (row,) = cmp.targets
        self.assertEqual((row["harness"], row["b"]["pass_rate"]), ("x", 0.0))

    def test_a_null_control_present_in_only_one_run_is_still_apart(self) -> None:
        a = sweep("x", [True])
        cmp = compare(a, [*a, null_trial("s/t0")])
        self.assertEqual((cmp.null_control, cmp.only_b), (("null/none",), ()))

    def test_a_real_null_control_is_never_flagged(self) -> None:
        a = [null_trial(f"s/t{i}") for i in range(6)]
        cmp = compare(a, list(a))
        self.assertEqual((cmp.has_regression, cmp.task_regressions), (False, ()))

    def test_comparability_warnings(self) -> None:
        a, b = sweep("x", [True] * 2), sweep("x", [True] * 2, tasks=5)
        cmp = compare(
            a,
            b + [make("x", "m", "s/t0", True, rep=2)],
            info_a=finished(config_sha256="1" * 64, suite_version="s@1.0.0", reps=1),
            info_b=finished(run_id="b" * 32, config_sha256="2" * 64, suite_version="s@2.0.0", reps=2),
        )
        text = " ".join(cmp.warnings)
        for needle in ("config_sha256", "suite_version", "task set", "reps"):
            self.assertIn(needle, text)

    def test_identical_setups_have_no_warnings_and_unrecorded_reps_fall_back_to_the_data(self) -> None:
        a = sweep("x", [True] * 2)
        self.assertEqual(compare(a, list(a)).warnings, ())
        more = a + [make("x", "m", "s/t0", True, rep=2)]
        self.assertTrue(any("reps" in w for w in compare(a, more).warnings))

    def test_an_unfinished_run_is_warned_about(self) -> None:
        a = sweep("x", [True] * 2)
        cmp = compare(a, list(a), info_b=run_info(run_id="b" * 32))
        self.assertTrue(any("not finished" in w for w in cmp.warnings))

    def test_the_same_input_gives_the_same_bytes(self) -> None:
        a, b = sweep("x", [True] * 6), sweep("x", [False] * 6)
        first, second = compare(a, b), compare(a, b)
        self.assertEqual(decide.render_comparison(first), decide.render_comparison(second))
        self.assertEqual(
            json.dumps(first.to_dict(), sort_keys=True, indent=2),
            json.dumps(second.to_dict(), sort_keys=True, indent=2),
        )

    def test_markdown_reports_the_regression_and_the_clean_case(self) -> None:
        bad = decide.render_comparison(compare(sweep("x", [True] * 6), sweep("x", [False] * 6)))
        self.assertIn("## Regressions", bad)
        self.assertIn("x/m", bad)
        self.assertIn("p=0.0312", bad)
        clean = decide.render_comparison(compare(sweep("x", [True] * 6), sweep("x", [True] * 6)))
        self.assertIn("No regression.", clean)
        self.assertIn("Runs are comparable.", clean)

    def test_markdown_lists_task_regressions_and_the_other_targets(self) -> None:
        a = [make("x", "m", "s/a", True), make("only-a", "m", "s/a", True)]
        b = [make("x", "m", "s/a", False), make("only-b", "m", "s/a", True)]
        md = decide.render_comparison(compare(a, b))
        self.assertIn("s/a", md)
        self.assertIn("Only in A: only-a/m", md)
        self.assertIn("Only in B: only-b/m", md)

    def test_an_unknown_cost_source_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            compare(sweep("x", [True]), sweep("x", [True]), cost_source="bogus")

    def test_an_alpha_outside_the_open_unit_interval_is_refused(self) -> None:
        for bad in (0.0, 1.0, -0.5, float("nan")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                compare(sweep("x", [True]), sweep("x", [True]), alpha=bad)

"""Tests for the pure statistics functions (`hbench.stats`)."""

import math
import unittest

from hbench.stats import (
    _splitmix64,
    bootstrap_ratio_ci,
    mcnemar_exact,
    mean_over_tasks,
    pareto_front,
    pass_at_k,
    pass_hat_k,
    reps_for_ci_width,
    tie_leaders,
    wilson_interval,
)


def rounded(interval: tuple[float, float]) -> tuple[float, float]:
    return round(interval[0], 4), round(interval[1], 4)


class WilsonTests(unittest.TestCase):
    def test_known_values(self) -> None:
        self.assertEqual(rounded(wilson_interval(8, 10)), (0.4902, 0.9433))
        self.assertEqual(rounded(wilson_interval(0, 10)), (0.0, 0.2775))

    def test_no_data_is_the_whole_range(self) -> None:
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))

    def test_bounds_are_clamped_to_the_unit_interval(self) -> None:
        # Unclamped, the float arithmetic gives -2.8e-17 for (0, 10) and 1.0000000000000002 for (5, 5).
        self.assertGreaterEqual(wilson_interval(0, 10)[0], 0.0)
        self.assertLessEqual(wilson_interval(5, 5)[1], 1.0)


class PassAtKTests(unittest.TestCase):
    def test_pass_at_k(self) -> None:
        self.assertAlmostEqual(pass_at_k(5, 2, 1), 0.4)
        self.assertAlmostEqual(pass_at_k(5, 2, 2), 0.7)
        self.assertEqual(pass_at_k(5, 0, 3), 0.0)

    def test_pass_hat_k(self) -> None:
        self.assertAlmostEqual(pass_hat_k(5, 2, 2), 0.1)
        self.assertEqual(pass_hat_k(5, 5, 3), 1.0)

    def test_bad_arguments_raise(self) -> None:
        for fn in (pass_at_k, pass_hat_k):
            for n, c, k in ((5, 2, 0), (5, 2, 6), (5, 6, 1)):
                with self.subTest(fn=fn.__name__, n=n, c=c, k=k), self.assertRaises(ValueError):
                    fn(n, c, k)

    def test_mean_over_tasks_averages_the_estimator(self) -> None:
        self.assertAlmostEqual(mean_over_tasks([(5, 5), (5, 0)], 1, pass_at_k), 0.5)

    def test_mean_over_no_tasks_is_zero(self) -> None:
        self.assertEqual(mean_over_tasks([], 1, pass_at_k), 0.0)


class BootstrapTests(unittest.TestCase):
    COSTS = [1.0, 1.0, 1.0, 1.0]
    PASSED = [True, True, True, False]

    def test_same_seed_gives_the_same_interval(self) -> None:
        a = bootstrap_ratio_ci(self.COSTS, self.PASSED, "h|m")
        self.assertEqual(a, bootstrap_ratio_ci(self.COSTS, self.PASSED, "h|m"))

    def test_other_seed_gives_another_interval(self) -> None:
        costs = [0.5, 1.0, 2.0, 1.5, 0.7, 3.0]
        passed = [True, False, True, True, False, True]
        self.assertNotEqual(bootstrap_ratio_ci(costs, passed, "a"), bootstrap_ratio_ci(costs, passed, "b"))

    def test_known_interval_for_fixed_input(self) -> None:
        # Expected values come from a separate throwaway script (own SplitMix64 and percentile bootstrap, same
        # sha256 seeding): sorted resample ratios [49..51] are 1.9, 1.9111.., 1.925 and [1949..1951] are
        # 8.475, 8.6667.., 8.7, so moving either percentile index by one changes the result.
        costs = [0.3, 1.7, 2.2, 4.1, 5.9, 0.8, 3.3, 2.9, 1.1]
        passed = [True, False, True, True, False, True, True, True, False]
        self.assertEqual(bootstrap_ratio_ci(costs, passed, "b"), (1.911111111111111, 8.666666666666666))

    def test_splitmix64_matches_the_reference_vector(self) -> None:
        draws = _splitmix64(1234567)
        self.assertEqual(
            [next(draws) for _ in range(3)], [6457827717110365317, 3203168211198807973, 9817491932198370423]
        )

    def test_no_pass_gives_infinite_interval(self) -> None:
        self.assertEqual(bootstrap_ratio_ci([1.0, 2.0], [False, False], "s"), (math.inf, math.inf))

    def test_interval_contains_the_point_estimate(self) -> None:
        low, high = bootstrap_ratio_ci(self.COSTS, self.PASSED, "s")
        self.assertLessEqual(low, 4 / 3)
        self.assertGreaterEqual(high, 4 / 3)

    def test_mismatched_or_empty_input_raises(self) -> None:
        for costs, passed in (([], []), ([1.0], [True, False])):
            with self.subTest(costs=costs), self.assertRaises(ValueError):
                bootstrap_ratio_ci(costs, passed, "s")


class McNemarTests(unittest.TestCase):
    def test_known_values(self) -> None:
        self.assertEqual(mcnemar_exact(0, 6), 0.03125)
        self.assertEqual(mcnemar_exact(3, 3), 1.0)
        self.assertEqual(mcnemar_exact(0, 0), 1.0)


class ParetoTests(unittest.TestCase):
    def test_dominated_points_are_off_the_front(self) -> None:
        self.assertEqual(pareto_front([(1, 0.5), (2, 0.9), (3, 0.8), (1, 0.4)]), [True, True, False, False])

    def test_identical_points_both_stay(self) -> None:
        self.assertEqual(pareto_front([(1, 0.5), (1, 0.5)]), [True, True])


class TieTests(unittest.TestCase):
    def test_groups_by_overlap_with_the_leader(self) -> None:
        self.assertEqual(tie_leaders([(1, 2), (1.5, 3), (4, 5), None]), [1, 1, None, None])

    def test_a_low_bound_equal_to_the_leader_high_bound_is_a_tie(self) -> None:
        self.assertEqual(tie_leaders([(1, 2), (2, 3)]), [1, 1])

    def test_a_later_group_can_tie_too(self) -> None:
        self.assertEqual(tie_leaders([(1, 2), (5, 6), (5.5, 7)]), [None, 2, 2])

    def test_missing_interval_ends_the_group(self) -> None:
        self.assertEqual(tie_leaders([(1, 2), None, (1.5, 3)]), [None, None, None])


class RepsTests(unittest.TestCase):
    def test_smallest_run_count_reaching_the_width(self) -> None:
        # Wilson width at p = 0.5: 0.2002 with 92 runs, 0.1992 with 93.
        self.assertEqual(reps_for_ci_width(0.5, 0.2, 1), 93)
        self.assertEqual(reps_for_ci_width(0.5, 0.2003, 1), 92)

    def test_runs_are_converted_to_reps_per_task(self) -> None:
        self.assertEqual(reps_for_ci_width(0.5, 0.2, 10), 10)

    def test_no_data_assumes_one_half(self) -> None:
        self.assertEqual(reps_for_ci_width(None, 0.2, 1), reps_for_ci_width(0.5, 0.2, 1))

    def test_unreachable_width_gives_none(self) -> None:
        self.assertIsNone(reps_for_ci_width(0.5, 1e-9, 1, max_runs=50))


if __name__ == "__main__":
    unittest.main()

"""Tests for the report derived from the contract (`hbench.report`)."""

import math
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

import fixtures
from hbench.core.convert import trials_from_rows
from hbench.report import aggregate, fmt_int, fmt_money, render_markdown, write_csv
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


class FormatTests(unittest.TestCase):
    def test_money_none_inf_small_and_large(self) -> None:
        self.assertEqual([fmt_money(v) for v in (None, math.inf, 0.12345, 12.345)], ["n/a", "inf", "$0.1235", "$12.35"])

    def test_int_none_inf_and_grouping(self) -> None:
        self.assertEqual([fmt_int(v) for v in (None, math.inf, 1234567.4)], ["n/a", "n/a", "1,234,567"])


class WriteCsvTests(unittest.TestCase):
    def test_writes_header_and_one_line_per_pair(self) -> None:
        path: Path = fixtures.tmpdir(self) / "s.csv"
        write_csv(aggregate(trials_from_rows([fixtures.run_row()]), "auto"), path)
        self.assertEqual(len(path.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()

"""The example dashboard and its synthetic data (ADR 0011, decision 10): the JSON loads, every panel runs the
scorecard query on the synthetic database, and the database is the same on every run."""

import contextlib
import io
import json
import re
import sqlite3
import unittest

import fixtures
from scripts import synthetic_db
from test_sqlite_sink import queries

DASHBOARD = fixtures.ROOT / "docs" / "dashboards" / "grafana-scorecard.json"


def squash(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = fixtures.tmpdir(self) / "synthetic.sqlite"
        synthetic_db.write_database(self.path)
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.dashboard = json.loads(DASHBOARD.read_text())

    def test_the_dashboard_loads_and_has_panels_with_one_query_each(self) -> None:
        panels = self.dashboard["panels"]
        self.assertGreaterEqual(len(panels), 3)
        self.assertTrue(all(len(p["targets"]) == 1 for p in panels))

    def test_every_panel_runs_a_scorecard_query_on_the_synthetic_database(self) -> None:
        scorecard = {squash(q) for q in queries().values()}
        for panel in self.dashboard["panels"]:
            sql = panel["targets"][0]["queryText"]
            with self.subTest(panel=panel["title"]):
                self.assertIn(squash(sql), scorecard)
                self.assertEqual(sql, panel["targets"][0]["rawQueryText"])
                self.assertEqual(len(self.db.execute(sql).fetchall()), len(synthetic_db.TARGETS))

    def test_every_scorecard_query_is_on_the_dashboard(self) -> None:
        on_dashboard = {squash(p["targets"][0]["queryText"]) for p in self.dashboard["panels"]}
        self.assertEqual({squash(q) for q in queries().values()}, on_dashboard)

    def test_the_target_without_a_cost_is_unknown_not_zero(self) -> None:
        rows = {r[0]: r for r in self.db.execute(queries()["cost_per_success"])}
        self.assertIsNone(rows["cursor/small"][3])
        self.assertGreater(rows["claude/large"][3], 0)
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM trials WHERE target_id = 'cursor/small' AND cost_usd IS NULL"
            ).fetchone(),
            (len(synthetic_db.TASKS) * synthetic_db.REPS,),
        )

    def test_the_unknown_cost_target_is_ranked_last(self) -> None:
        rows = self.db.execute(queries()["cost_per_success"]).fetchall()
        self.assertEqual(rows[-1][0], "cursor/small")

    def test_the_same_seed_writes_the_same_rows_and_a_rewrite_adds_none(self) -> None:
        other = fixtures.tmpdir(self) / "other.sqlite"
        synthetic_db.write_database(other)
        again = sqlite3.connect(other)
        self.addCleanup(again.close)
        query = "SELECT * FROM trials ORDER BY trial_id"
        self.assertEqual(self.db.execute(query).fetchall(), again.execute(query).fetchall())
        self.assertEqual(synthetic_db.write_database(other), len(synthetic_db.TARGETS) * 18)
        self.assertEqual(again.execute("SELECT COUNT(*) FROM trials").fetchone(), (54,))

    def test_a_different_seed_changes_the_outcomes(self) -> None:
        other = fixtures.tmpdir(self) / "other.sqlite"
        synthetic_db.write_database(other, seed=8)
        again = sqlite3.connect(other)
        self.addCleanup(again.close)
        query = "SELECT passed FROM trials ORDER BY trial_id"
        self.assertNotEqual(self.db.execute(query).fetchall(), again.execute(query).fetchall())

    def test_main_writes_the_database_and_checks_its_arguments(self) -> None:
        out = fixtures.tmpdir(self) / "main.sqlite"
        with fixtures.quiet() as printed:
            self.assertEqual(synthetic_db.main(["synthetic_db", str(out)]), 0)
        self.assertIn("wrote 54 synthetic trials", printed.getvalue())
        self.assertTrue(out.exists())
        with contextlib.redirect_stderr(io.StringIO()) as usage:
            self.assertEqual(synthetic_db.main(["synthetic_db"]), 2)
        self.assertIn("usage:", usage.getvalue())

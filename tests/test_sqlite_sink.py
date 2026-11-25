"""The `sqlite` sink and the reference SQL of `docs/sql` (ADR 0011, decisions 3 and 9)."""

import dataclasses
import re
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import fixtures
from hbench.core.models import GraderResult, TrialStatus
from hbench.sinks.base import SinkError
from hbench.sinks.sqlite import SCHEMA_PATH, SqliteSink
from sink_helpers import make_info, make_trial, with_usage

SCORECARD = Path(__file__).resolve().parent.parent / "docs" / "sql" / "scorecard.sql"


def queries() -> dict[str, str]:
    """The named queries of `scorecard.sql`: the text after each `-- name:` line up to its semicolon."""
    found = re.findall(r"^-- name: (\w+)\n(.*?;)\n", SCORECARD.read_text(), flags=re.S | re.M)
    return {name: "\n".join(line for line in body.splitlines() if not line.startswith("--")) for name, body in found}


class SqliteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = fixtures.tmpdir(self)
        self.path = self.dir / "sub" / "bench.sqlite"
        self.sink = SqliteSink(self.path)

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        self.addCleanup(con.close)
        return con

    def count(self, table: str) -> int:
        return int(self.db().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class SqliteSinkTests(SqliteTestCase):
    def test_open_creates_the_tables_of_the_reference_schema_and_the_run_row(self) -> None:
        self.sink.open(make_info(finished=False))
        names = {r[0] for r in self.db().execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(names, {"runs", "trials", "grader_results"})
        self.assertEqual(
            self.db().execute("SELECT run_id, finished_at, sandbox, sandboxed FROM runs").fetchall(),
            [(make_info().run_id, None, "local-tmp", 1)],
        )

    def test_a_trial_is_one_row_with_its_graders_and_the_run_is_finished_on_close(self) -> None:
        trial = dataclasses.replace(
            make_trial(),
            graders=(
                GraderResult(grader="hidden_tests", grader_version="1", passed=True),
                GraderResult(grader="llm_rubric", grader_version="2", passed=False, score=0.25, judge="j-1"),
            ),
        )
        self.sink.open(make_info(finished=False))
        self.sink.write(trial)
        self.sink.close(make_info())
        row = (
            self.db()
            .execute(
                "SELECT target_id, status, passed, input_tokens, tool_calls, cost_usd, cost_source, run_tag FROM trials"
            )
            .fetchone()
        )
        self.assertEqual(row, ("claude/fix", "passed", 1, 100, 3, 0.5, "gateway", trial.trace_id))
        self.assertEqual(
            self.db()
            .execute("SELECT position, grader, passed, score, judge FROM grader_results ORDER BY position")
            .fetchall(),
            [(0, "hidden_tests", 1, None, None), (1, "llm_rubric", 0, 0.25, "j-1")],
        )
        self.assertIsNotNone(self.db().execute("SELECT finished_at FROM runs").fetchone()[0])

    def test_sending_a_trial_again_updates_it_and_never_duplicates_it(self) -> None:
        trial = make_trial()
        self.sink.open(make_info())
        self.sink.write(trial)
        self.sink.write(dataclasses.replace(trial, status=TrialStatus.FAILED))
        self.sink.close(make_info())
        self.sink = SqliteSink(self.path)  # a replay opens the same file again
        self.sink.open(make_info())
        self.sink.write(trial)
        self.sink.close(make_info())
        self.assertEqual((self.count("runs"), self.count("trials"), self.count("grader_results")), (1, 1, 1))
        self.assertEqual(self.db().execute("SELECT status, passed FROM trials").fetchall(), [("passed", 1)])

    def test_unknown_costs_tokens_and_tool_calls_stay_null(self) -> None:
        unparsed = with_usage(make_trial(cost=None), parsed=False, input_tokens=0, output_tokens=0, tool_calls=None)
        self.sink.open(make_info())
        self.sink.write(unparsed)
        self.sink.close(make_info())
        columns = "usage_parsed, input_tokens, output_tokens, turns, tool_calls, cost_usd, cost_gateway, cost_source"
        row = self.db().execute(f"SELECT {columns} FROM trials").fetchone()
        self.assertEqual(row, (0, None, None, None, None, None, None, "unknown"))

    def test_sql_like_text_in_a_field_is_data(self) -> None:
        nasty = "x'); DROP TABLE trials;--"
        self.sink.open(make_info())
        self.sink.write(make_trial(task_id=nasty))
        self.sink.close(make_info())
        self.assertEqual(self.db().execute("SELECT task_id FROM trials").fetchall(), [(nasty,)])
        self.assertEqual(self.count("grader_results"), 1)

    def test_write_before_open_is_an_error(self) -> None:
        with self.assertRaises(SinkError):
            self.sink.write(make_trial())

    def test_a_missing_schema_file_is_a_sink_error(self) -> None:
        with mock.patch("hbench.sinks.sqlite.SCHEMA_PATH", self.dir / "none.sql"), self.assertRaises(SinkError) as cm:
            self.sink.open(make_info())
        self.assertIn("cannot read the schema", str(cm.exception))

    def test_the_runtime_schema_is_in_the_package(self) -> None:
        self.assertEqual(SCHEMA_PATH.parts[-3:], ("hbench", "sinks", "schema.sql"))

    def test_the_reference_schema_in_the_docs_is_the_same_file(self) -> None:
        docs = fixtures.ROOT / "docs" / "sql" / "schema.sql"
        self.assertEqual(docs.read_text(), SCHEMA_PATH.read_text())


class ScorecardTests(SqliteTestCase):
    """Two targets, two tasks, two reps each. `good` always passes at $0.5 a trial; `flaky` passes `a` once and never
    `b`, and the cost of one of its trials is unknown."""

    def setUp(self) -> None:
        super().setUp()
        self.sink.open(make_info())
        for task in ("a", "b"):
            for rep in (1, 2):
                self.sink.write(make_trial(task_id=task, harness="good", rep=rep))
        flaky = (
            ("a", 1, TrialStatus.PASSED, 1.0),
            ("a", 2, TrialStatus.FAILED, 1.0),
            ("b", 1, TrialStatus.FAILED, None),
            ("b", 2, TrialStatus.FAILED, 1.0),
        )
        for task, rep, status, cost in flaky:
            self.sink.write(make_trial(task_id=task, harness="flaky", rep=rep, status=status, cost=cost))
        self.sink.close(make_info())

    def run_query(self, name: str) -> dict[str, tuple[object, ...]]:
        rows = self.db().execute(queries()[name]).fetchall()
        return {r[0]: tuple(r[1:]) for r in rows}

    def test_every_query_of_the_file_is_found_and_runs(self) -> None:
        self.assertEqual(set(queries()), {"pass_rate", "pass_hat_k", "cost_per_success"})

    def test_pass_rate(self) -> None:
        self.assertEqual(self.run_query("pass_rate"), {"good/fix": (4, 4, 1.0), "flaky/fix": (4, 1, 0.25)})

    def test_pass_hat_k_counts_the_tasks_that_passed_on_every_rep(self) -> None:
        self.assertEqual(self.run_query("pass_hat_k"), {"good/fix": (2, 2, 1.0), "flaky/fix": (2, 0, 0.0)})

    def test_cost_per_success_is_unknown_when_any_cost_is_unknown_and_never_zero(self) -> None:
        result = self.run_query("cost_per_success")
        self.assertEqual(result["good/fix"], (4, 2.0, 0.5))
        self.assertEqual(result["flaky/fix"], (1, None, None))

    def test_a_target_with_an_unknown_cost_sorts_last_never_cheapest(self) -> None:
        rows = self.db().execute(queries()["cost_per_success"]).fetchall()
        self.assertEqual([r[0] for r in rows], ["good/fix", "flaky/fix"])

    def test_cost_per_success_is_null_when_nothing_was_solved(self) -> None:
        con = self.db()
        con.execute("UPDATE trials SET passed = 0, cost_usd = 1.0")
        self.assertEqual(con.execute(queries()["cost_per_success"]).fetchall()[0][1:], (0, 4.0, None))

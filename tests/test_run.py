"""In-process tests for `cmd_run`, `aggregate` and `cmd_report` with the fake agents."""

import argparse
import shutil
import unittest
from pathlib import Path
from typing import Any

import fixtures
import harness_bench as hb


def args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "reps": None,
        "only": None,
        "keep": False,
        "dry_run": False,
        "allow_unsandboxed": True,
        "no_report": True,
        "cost_source": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def rows_of(base: Path) -> list[dict[str, Any]]:
    return fixtures.read_rows(base / "results" / "results.jsonl")


class RunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = fixtures.tmpdir(self)
        shutil.copytree(fixtures.ROOT / "tasks" / "fizzbuzz", self.base / "tasks" / "fizzbuzz")
        self.cfg = fixtures.fake_config({"FAKE_TAGS_FILE": str(self.base / "tags.txt")})
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}, {"harness": "pi", "model": "p/nofix"}]


class CmdRunTests(RunTestCase):
    def test_refuses_to_run_without_a_sandbox(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(allow_unsandboxed=False), self.cfg, self.base)
        self.assertIn("No sandbox configured", str(cm.exception.code))

    def test_dry_run_prints_commands_and_writes_no_results(self) -> None:
        out = fixtures.call_quiet(hb.cmd_run, args(dry_run=True, allow_unsandboxed=False), self.cfg, self.base)
        self.assertEqual((out.count("fake_agent.py"), (self.base / "results" / "results.jsonl").exists()), (2, False))

    def test_only_filters_harnesses(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        self.assertEqual([r["harness"] for r in rows_of(self.base)], ["claude"])

    def test_reps_argument_overrides_config(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], reps=2), self.cfg, self.base)
        self.assertEqual([r["rep"] for r in rows_of(self.base)], [1, 2])

    def test_hidden_tests_decide_pass_and_unknown_cost_stays_none(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        rows = {r["harness"]: r for r in rows_of(self.base)}
        self.assertEqual(
            (rows["claude"]["passed"], rows["pi"]["passed"], rows["pi"]["cost_computed"]), (True, False, None)
        )

    def test_finished_runs_are_skipped_on_resume(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        out = fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertEqual((out.count("skip  "), len(rows_of(self.base))), (2, 2))

    def test_agent_timeout_is_recorded_and_fails_the_run(self) -> None:
        # The agent flushes a readiness line before sleeping; 5 s leaves room for slow interpreter start-up.
        code = "import time; print('ready', flush=True); time.sleep(60)"
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, "-c", code]
        self.cfg["tasks"][0]["timeout"] = 5
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        row = rows_of(self.base)[0]
        log = next((self.base / "results" / "logs").glob("*.agent.jsonl")).read_text()
        self.assertEqual(
            (row["agent_timed_out"], row["passed"], row["parse_ok"], "ready" in log), (True, False, False, True)
        )

    def test_setup_command_runs_and_is_logged(self) -> None:
        self.cfg["tasks"][0]["setup"] = "echo prepared"
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        logs = list((self.base / "results" / "logs").glob("*.setup.log"))
        self.assertIn("prepared", logs[0].read_text())

    def test_keep_and_work_dir_leave_the_workdir_in_place(self) -> None:
        work = fixtures.tmpdir(self) / "work"
        self.cfg["work_dir"] = str(work)
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], keep=True), self.cfg, self.base)
        self.assertTrue((work / "fizzbuzz__claude__fix__r1" / "fizzbuzz.py").exists())

    def test_leftover_workdir_and_snapshot_from_a_crashed_run_are_replaced(self) -> None:
        work = fixtures.tmpdir(self) / "work"
        tag = "fizzbuzz__claude__fix__r1"
        (work / tag).mkdir(parents=True)
        (work / tag / "stale.txt").write_text("old")
        (work / f".{tag}.protected").mkdir()
        self.cfg["work_dir"] = str(work)
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], keep=True), self.cfg, self.base)
        self.assertEqual((work / tag / "stale.txt").exists(), False)

    def test_report_is_written_unless_suppressed(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], no_report=False), self.cfg, self.base)
        self.assertTrue((self.base / "results" / "summary.md").exists())

    def test_task_without_hidden_tests_runs_its_own_test_command(self) -> None:
        task = self.cfg["tasks"][0]
        task.pop("hidden_tests")
        task["test"] = "true"
        fixtures.call_quiet(hb.cmd_run, args(only=["pi"]), self.cfg, self.base)
        self.assertTrue(rows_of(self.base)[0]["passed"])

    def test_protected_file_is_snapshotted_and_restored_to_its_original_content(self) -> None:
        original = (fixtures.ROOT / "tasks" / "fizzbuzz" / "repo" / "fizzbuzz.py").read_text()
        work = fixtures.tmpdir(self) / "work"
        self.cfg["work_dir"] = str(work)
        self.cfg["tasks"][0]["protected_paths"] = ["fizzbuzz.py"]
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], keep=True), self.cfg, self.base)
        kept = work / "fizzbuzz__claude__fix__r1" / "fizzbuzz.py"
        self.assertEqual(kept.read_text(), original)  # the agent's "fix" was reverted, not deleted

    def test_protected_paths_are_restored_before_the_hidden_tests(self) -> None:
        self.cfg["tasks"][0]["protected_paths"] = ["fizzbuzz.py"]
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        self.assertFalse(rows_of(self.base)[0]["passed"])  # the "fix" was reverted by the restore


class AggregateTests(unittest.TestCase):
    def test_cost_source_litellm_without_data_gives_none_not_zero(self) -> None:
        table = hb.aggregate([fixtures.run_row(cost_computed=1.0)], "litellm")
        self.assertEqual(
            (table[0]["cost_total"], table[0]["cost_per_solved"], table[0]["cost_source"]), (None, None, "n/a")
        )

    def test_auto_prefers_litellm_then_computed_then_reported(self) -> None:
        rows = [
            fixtures.run_row(rep=1, cost_litellm=1.0, cost_computed=2.0, cost_reported=3.0),
            fixtures.run_row(rep=2, cost_computed=2.0, cost_reported=3.0),
            fixtures.run_row(rep=3, cost_reported=3.0),
        ]
        table = hb.aggregate(rows, "auto")
        self.assertEqual((table[0]["cost_total"], table[0]["cost_source"]), (6.0, "computed+litellm+reported"))

    def test_one_unknown_cost_makes_the_pair_unknown(self) -> None:
        table = hb.aggregate([fixtures.run_row(cost_computed=1.0), fixtures.run_row(rep=2)], "auto")
        self.assertIsNone(table[0]["cost_total"])

    def test_pairs_without_cost_sort_last(self) -> None:
        rows = [fixtures.run_row(harness="a"), fixtures.run_row(harness="b", cost_computed=1.0)]
        self.assertEqual([t["harness"] for t in hb.aggregate(rows, "auto")], ["b", "a"])


class CmdReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = fixtures.tmpdir(self)
        (self.base / "results").mkdir()

    def test_missing_results_file_exits_with_a_message(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_report, args(), {}, self.base)
        self.assertIn("no results at", str(cm.exception.code))

    def test_empty_results_file_exits_with_no_results(self) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl")
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_report, args(), {}, self.base)
        self.assertIn("no results in", str(cm.exception.code))

    def test_unknown_cost_source_exits_with_a_message_not_a_traceback(self) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl", fixtures.run_row())
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_report, args(cost_source="bogus"), {}, self.base)
        self.assertIn("auto, litellm, computed, reported", str(cm.exception.code))

    def test_pairs_without_token_data_get_a_warning(self) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl", fixtures.run_row(parse_ok=False))
        out = fixtures.call_quiet(hb.cmd_report, args(), {}, self.base)
        self.assertIn("no token data from the harness for: h/m (1)", out)

    def test_cost_source_comes_from_config_when_not_given(self) -> None:
        fixtures.write_rows(
            self.base / "results" / "results.jsonl", fixtures.run_row(cost_litellm=2.0, cost_computed=1.0)
        )
        fixtures.call_quiet(hb.cmd_report, args(), {"cost_source": "computed"}, self.base)
        self.assertIn("Cost source: `computed`", (self.base / "results" / "summary.md").read_text())

    def test_task_missing_for_a_pair_is_shown_as_dash(self) -> None:
        fixtures.write_rows(
            self.base / "results" / "results.jsonl", fixtures.run_row(task="a"), fixtures.run_row(task="b", harness="g")
        )
        fixtures.call_quiet(hb.cmd_report, args(), {}, self.base)
        self.assertIn("| a | 1/1 | - |", (self.base / "results" / "summary.md").read_text())


if __name__ == "__main__":
    unittest.main()

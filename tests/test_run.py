"""In-process tests for `cmd_run`, `aggregate` and `cmd_report` with the fake agents."""

import argparse
import json
import shutil
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import fixtures
import harness_bench as hb
from hbench.core.ids import sha256_json


def args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "reps": None,
        "only": None,
        "keep": False,
        "dry_run": False,
        "allow_unsandboxed": True,
        "no_report": True,
        "cost_source": None,
        "resume": None,
        "force": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def rows_of(base: Path) -> list[dict[str, Any]]:
    return fixtures.read_rows(base / "results" / "results.jsonl")


class RunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = fixtures.tmpdir(self)
        shutil.copytree(fixtures.FIZZBUZZ, self.base / "tasks" / "fizzbuzz")
        self.cfg = fixtures.fake_config({"FAKE_TAGS_FILE": str(self.base / "tags.txt")})
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}, {"harness": "pi", "model": "p/nofix"}]


class CmdRunTests(RunTestCase):
    def test_refuses_to_run_without_a_sandbox(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(allow_unsandboxed=False), self.cfg, self.base)
        self.assertIn("No sandbox configured", str(cm.exception.code))

    def test_a_git_older_than_2_40_still_runs_with_the_attributes_file_alone(self) -> None:
        with mock.patch.object(hb, "git_version", return_value=(2, 34)):
            fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        self.assertEqual([r["passed"] for r in rows_of(self.base)], [True])

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

    def test_null_harness_fails_the_task_at_zero_reported_cost(self) -> None:
        self.cfg["harnesses"]["null"] = {"command": ["true"], "parser": "null"}
        self.cfg["matrix"] = [{"harness": "null", "model": "none"}]
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (row,) = rows_of(self.base)
        self.assertEqual((row["passed"], row["cost_reported"], row["parse_ok"]), (False, 0.0, True))

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

    def test_the_baseline_git_dir_is_outside_the_workdir_and_removed_after_the_trial(self) -> None:
        work = fixtures.tmpdir(self) / "work"
        self.cfg["work_dir"] = str(work)
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], keep=True), self.cfg, self.base)
        kept = sorted(p.name for p in work.iterdir())
        self.assertEqual(
            (kept, (work / "fizzbuzz__claude__fix__r1" / ".git").exists()), (["fizzbuzz__claude__fix__r1"], False)
        )

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
        original = (fixtures.FIZZBUZZ / "repo" / "fizzbuzz.py").read_text()
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


def trials_of(base: Path) -> list[dict[str, Any]]:
    (trials,) = (base / "results").glob("*/trials.jsonl")
    return fixtures.read_rows(trials)


def log_names(base: Path) -> set[str]:
    return {p.name.split("__r1.", 1)[1] for p in (base / "results" / "logs").iterdir() if "__r1." in p.name}


class GraderRunTests(RunTestCase):
    """`cmd_run` with the `graders` and `grade` task keys."""

    def setUp(self) -> None:
        super().setUp()
        self.task = self.cfg["tasks"][0]
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}]

    def run_one(self, **overrides: Any) -> dict[str, Any]:
        self.task.update(overrides)
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        return rows_of(self.base)[-1]

    def test_every_grader_result_is_written_to_the_trial(self) -> None:
        self.run_one(graders=[{"type": "hidden_tests"}, {"type": "command", "command": "true"}])
        (trial,) = trials_of(self.base)
        self.assertEqual(
            [(g["grader"], g["passed"], g["score"]) for g in trial["graders"]],
            [("hidden_tests", True, 1.0), ("command", True, 1.0)],
        )

    def test_logs_are_named_test_for_hidden_tests_and_by_index_and_type_for_the_rest(self) -> None:
        self.run_one(graders=[{"type": "hidden_tests"}, {"type": "command", "command": "echo graded"}])
        names = log_names(self.base)
        self.assertTrue({"test.log", "grader-1-command.log"} <= names, names)
        log = next((self.base / "results" / "logs").glob("*grader-1-command.log")).read_text()
        self.assertIn("graded", log)

    def test_mode_all_fails_when_one_grader_fails(self) -> None:
        row = self.run_one(graders=[{"type": "hidden_tests"}, {"type": "command", "command": "false"}])
        self.assertEqual(row["passed"], False)
        self.assertEqual(trials_of(self.base)[0]["status"], "failed")

    def test_mode_any_passes_when_one_grader_passes(self) -> None:
        self.cfg["matrix"] = [{"harness": "pi", "model": "p/nofix"}]  # the hidden tests fail
        row = self.run_one(
            graders=[{"type": "hidden_tests"}, {"type": "command", "command": "true"}], grade={"mode": "any"}
        )
        self.assertEqual(row["passed"], True)

    def test_weighted_mode_uses_the_threshold(self) -> None:
        self.cfg["matrix"] = [{"harness": "pi", "model": "p/nofix"}]
        graders = [{"type": "hidden_tests", "weight": 1}, {"type": "command", "command": "true", "weight": 3}]
        row = self.run_one(graders=graders, grade={"mode": "weighted", "threshold": 0.7})
        self.assertEqual(row["passed"], True)

    def test_a_grader_timeout_marks_the_trial_as_timed_out(self) -> None:
        sleeper = f"{fixtures.PY} -c 'import time; time.sleep(60)'"
        self.run_one(graders=[{"type": "command", "command": sleeper, "timeout": 1}])
        self.assertEqual(trials_of(self.base)[0]["status"], "timeout")

    def test_a_task_without_graders_writes_the_legacy_row_and_grader(self) -> None:
        row = self.run_one()
        (trial,) = trials_of(self.base)
        self.assertEqual(("graders" in row, "score" in row), (False, False))
        self.assertEqual([(g["grader"], g["score"]) for g in trial["graders"]], [("test", None)])

    def test_changes_are_captured_before_protected_paths_are_restored(self) -> None:
        graders = [{"type": "diff_constraints", "forbidden": ["fizzbuzz.py"]}]
        row = self.run_one(graders=graders, protected_paths=["fizzbuzz.py"])  # the agent edits a protected file
        self.assertEqual(row["passed"], False)

    def test_diff_constraints_pass_when_the_agent_stays_inside_the_limits(self) -> None:
        row = self.run_one(graders=[{"type": "diff_constraints", "allowed": ["fizzbuzz.py"], "max_changed_lines": 50}])
        self.assertEqual(row["passed"], True)

    def test_diff_constraints_fail_closed_when_the_agent_leaves_a_git_dir_behind(self) -> None:
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, "-c", "import os; os.mkdir('.git')"]
        row = self.run_one(graders=[{"type": "diff_constraints", "max_changed_lines": 1000}])
        self.assertEqual(row["passed"], False)

    def agent_writing_a_pyc(self) -> None:
        code = (
            "import os; os.makedirs('tests/__pycache__');"
            " open('tests/__pycache__/t.cpython-312.pyc', 'wb').write(b'\\0' * 9)"
        )
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, "-c", code]

    def test_bytecode_left_under_tests_passes_only_with_ignore_lines_and_the_source_glob(self) -> None:
        self.agent_writing_a_pyc()
        limits = {"type": "diff_constraints", "forbidden": ["tests/*.py"], "max_changed_lines": 50}
        ignored = self.run_one(graders=[{**limits, "ignore_lines": ["*/__pycache__/*"]}])
        plain = self.run_one(id="plain", graders=[limits])
        self.assertEqual((ignored["passed"], plain["passed"]), (True, False))

    def test_a_pyc_is_still_a_forbidden_path_when_the_glob_covers_it(self) -> None:
        self.agent_writing_a_pyc()
        row = self.run_one(graders=[{"type": "diff_constraints", "forbidden": ["tests/*"], "ignore_lines": ["*.pyc"]}])
        self.assertEqual(row["passed"], False)

    def test_a_secret_the_agent_writes_to_a_file_is_redacted_in_the_audit_diff(self) -> None:
        secret = "-".join(["abcdef", "fake", "12345"])  # built at runtime so secret scanners skip it
        self.cfg["harnesses"]["claude"]["env"]["MY_SECRET"] = secret
        code = "import os; open('leak.txt', 'w').write(os.environ['MY_SECRET'])"
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, "-c", code]
        self.run_one()
        diff = next((self.base / "results" / "logs").glob("*.diff")).read_text()
        self.assertEqual((secret in diff, "leak.txt" in diff, "***REDACTED***" in diff), (False, True, True))

    def test_an_exception_in_a_grader_fails_that_grader_and_the_matrix_goes_on(self) -> None:
        class Broken:
            name = "broken"
            version = "1"

            def __init__(self, cfg: Any) -> None:
                pass

            def grade(self, ctx: Any) -> Any:
                raise RuntimeError("boom")

        self.task["graders"] = [{"type": "broken"}]
        with mock.patch.dict(hb.GRADERS, {"broken": Broken}):
            out = fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (trial,) = trials_of(self.base)
        self.assertEqual((trial["graders"][0]["passed"], "RuntimeError" in out, "boom" in out), (False, True, False))

    def test_bad_grader_config_exits_naming_the_task_before_any_agent_runs(self) -> None:
        self.task["graders"] = [{"type": "command"}]
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertIn("task fizzbuzz", str(cm.exception.code))
        self.assertFalse((self.base / "results" / "results.jsonl").exists())


class SuiteRunTests(RunTestCase):
    """Tasks that come from `suites` in the config."""

    def setUp(self) -> None:
        super().setUp()
        fixtures.write_fizzbuzz_suite(self.base, "fizz")
        self.cfg["tasks"] = []
        self.cfg["suites"] = ["suites/fizz"]
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}, {"harness": "pi", "model": "p/nofix"}]

    def test_suite_tasks_run_with_prefixed_ids_and_the_hidden_tests_decide(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertEqual(
            [(r["task"], r["passed"]) for r in rows_of(self.base)], [("fizz/fizzbuzz", True), ("fizz/fizzbuzz", False)]
        )

    def test_config_tasks_and_suite_tasks_run_together(self) -> None:
        self.cfg["tasks"] = [fixtures.fizzbuzz_task()]
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        self.assertEqual([r["task"] for r in rows_of(self.base)], ["fizzbuzz", "fizz/fizzbuzz"])

    def test_a_config_with_only_suites_needs_no_tasks_key(self) -> None:
        del self.cfg["tasks"]
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        self.assertEqual([r["task"] for r in rows_of(self.base)], ["fizz/fizzbuzz"])

    def test_the_slash_in_the_id_never_reaches_a_log_or_workdir_name(self) -> None:
        work = fixtures.tmpdir(self) / "work"
        self.cfg["work_dir"] = str(work)
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], keep=True), self.cfg, self.base)
        logs = self.base / "results" / "logs"
        self.assertEqual(
            (sorted(p.name for p in work.iterdir()), {p.parent for p in logs.rglob("*") if p.is_file()}),
            (["fizz_fizzbuzz__claude__fix__r1"], {logs}),
        )
        self.assertTrue(all(p.name.startswith("fizz_fizzbuzz__") for p in logs.iterdir()))

    def test_run_info_lists_the_suites_with_their_versions_and_the_task_hash(self) -> None:
        fixtures.write_fizzbuzz_suite(self.base, "other", "regression")
        self.cfg["suites"] = ["suites/fizz", "suites/other"]
        self.cfg["tasks"] = [fixtures.fizzbuzz_task()]
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        (info,) = (self.base / "results").glob("*/run.json")
        label, _, digest = json.loads(info.read_text())["suite_version"].partition("+")
        self.assertEqual((label, len(digest)), ("fizz@1.0.0,other@1.0.0", 12))

    def test_suite_content_does_not_change_the_recorded_hash_with_the_machine_path(self) -> None:
        other = fixtures.tmpdir(self)
        fixtures.write_fizzbuzz_suite(other, "fizz")
        versions = []
        for base in (self.base, other):
            fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, base)
            (info,) = (base / "results").glob("*/run.json")
            versions.append(json.loads(info.read_text())["suite_version"])
        self.assertEqual(versions[0], versions[1])

    def test_a_config_without_suites_keeps_the_plain_task_hash(self) -> None:
        cfg = fixtures.fake_config({})
        cfg["matrix"] = [{"harness": "claude", "model": "fix"}]
        shutil.copytree(fixtures.FIZZBUZZ, self.base / "tasks" / "fizzbuzz", dirs_exist_ok=True)
        fixtures.call_quiet(hb.cmd_run, args(), cfg, self.base)
        (info,) = (self.base / "results").glob("*/run.json")
        self.assertEqual(json.loads(info.read_text())["suite_version"], sha256_json(cfg["tasks"])[:12])

    def test_dry_run_prints_the_commands_of_the_suite_tasks(self) -> None:
        out = fixtures.call_quiet(hb.cmd_run, args(dry_run=True, allow_unsandboxed=False), self.cfg, self.base)
        self.assertEqual(out.count("fake_agent.py"), 2)

    def test_two_suites_with_the_same_name_are_a_config_error_naming_both(self) -> None:
        fixtures.write_fizzbuzz_suite(self.base, "fizz", directory="copy")
        self.cfg["suites"] = ["suites/fizz", "suites/copy"]
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        message = str(cm.exception.code)
        self.assertTrue(all(s in message for s in ("fizz/fizzbuzz", "suites/fizz", "suites/copy")), message)

    def test_a_config_task_with_the_id_of_a_suite_task_is_a_config_error_naming_both(self) -> None:
        self.cfg["tasks"] = [dict(fixtures.fizzbuzz_task(), id="fizz/fizzbuzz")]
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        message = str(cm.exception.code)
        self.assertTrue(all(s in message for s in ("fizz/fizzbuzz", "config tasks", "suites/fizz")), message)
        self.assertFalse((self.base / "results" / "results.jsonl").exists())

    def test_an_invalid_suite_exits_naming_the_suite_and_the_problem(self) -> None:
        (self.base / "suites" / "fizz" / "suite.json").write_text("{}")
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertTrue(all(s in str(cm.exception.code) for s in ("suites/fizz", "missing key")), cm.exception.code)

    def test_suites_must_be_a_list_of_paths(self) -> None:
        for bad in ("suites/fizz", [3], {"a": 1}):
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as cm:
                fixtures.call_quiet(hb.cmd_run, args(), dict(self.cfg, suites=bad), self.base)
            self.assertIn("suites must be a list", str(cm.exception.code))

    def test_the_report_has_the_by_suite_kind_section_when_two_kinds_ran(self) -> None:
        fixtures.write_fizzbuzz_suite(self.base, "guard", "regression")
        self.cfg["suites"] = ["suites/fizz", "suites/guard"]
        fixtures.call_quiet(hb.cmd_run, args(no_report=False), self.cfg, self.base)
        summary = (self.base / "results" / "summary.md").read_text()
        self.assertIn("| capability | 1/1 | 0/1 |", summary)
        self.assertIn("**Warning:** regression tasks failed: pi/p/nofix (guard/fizzbuzz 0/1)", summary)

    def test_report_alone_reads_the_kinds_from_the_config_suites(self) -> None:
        fixtures.write_fizzbuzz_suite(self.base, "guard", "regression")
        self.cfg["suites"] = ["suites/fizz", "suites/guard"]
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        fixtures.call_quiet(hb.cmd_report, args(), self.cfg, self.base)
        self.assertIn("## By suite kind", (self.base / "results" / "summary.md").read_text())

    def test_report_warns_and_skips_the_kind_section_when_the_suites_cannot_load(self) -> None:
        fixtures.write_fizzbuzz_suite(self.base, "guard", "regression")
        self.cfg["suites"] = ["suites/fizz", "suites/guard"]
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (self.base / "suites" / "guard" / "suite.json").write_text("{}")
        out = fixtures.call_quiet(hb.cmd_report, args(), self.cfg, self.base)
        self.assertIn("warning: cannot read the suites", out)
        self.assertIn("suites/guard", out)
        self.assertNotIn("By suite kind", (self.base / "results" / "summary.md").read_text())

    def test_a_single_suite_kind_leaves_the_report_unchanged(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(no_report=False), self.cfg, self.base)
        self.assertNotIn("By suite kind", (self.base / "results" / "summary.md").read_text())


class RunTaskAttemptTests(unittest.TestCase):
    """`run_task_attempt`: the trial `suite validate --run` runs, with a solution copied in place of an agent."""

    def setUp(self) -> None:
        self.root = fixtures.tmpdir(self)
        self.base = self.root / "base"
        self.work_root = self.root / "work"
        self.git_root = self.root / "git"
        for path in (self.base, self.work_root, self.git_root):
            path.mkdir()
        self.repo, self.solution = self.base / "repo", self.base / "solution"
        for path in (self.repo, self.solution):
            path.mkdir()
        self.cfg = fixtures.fake_config({})

    def attempt(self, task: dict[str, Any], solution: Path | None) -> bool:
        with fixtures.quiet():
            return hb.run_task_attempt(self.cfg, task, self.base, self.work_root, self.git_root, "trial", solution)

    def test_a_solution_is_not_copied_through_a_symlink_the_repo_left_in_the_workdir(self) -> None:
        (self.repo / "esc").symlink_to("../..", target_is_directory=True)  # from the workdir: the dir above `work`
        (self.solution / "esc").mkdir()
        (self.solution / "esc" / "PWNED").write_text("x")
        task = {"id": "t", "repo": "repo", "test": "true"}
        self.assertTrue(self.attempt(task, self.solution))
        self.assertFalse((self.root / "PWNED").exists())
        self.assertFalse((self.work_root.parent / "PWNED").exists())

    def test_stale_bytecode_in_the_solution_does_not_reach_the_workdir(self) -> None:
        (self.solution / "__pycache__").mkdir()
        (self.solution / "__pycache__" / "x.pyc").write_text("stale")
        (self.solution / "new.txt").write_text("n")
        task = {"id": "t", "repo": "repo", "test": "test -e new.txt && ! test -e __pycache__"}
        self.assertTrue(self.attempt(task, self.solution))

    def test_protected_paths_are_restored_after_the_solution_is_copied(self) -> None:
        (self.repo / "guard.txt").write_text("ok")
        (self.solution / "guard.txt").write_text("tampered")
        check = "import sys; sys.exit(0 if open('guard.txt').read() == 'ok' else 1)"
        task = {"id": "t", "repo": "repo", "test": f'{fixtures.PY} -c "{check}"', "protected_paths": ["guard.txt"]}
        self.assertEqual((self.attempt(task, None), self.attempt(task, self.solution)), (True, True))


class SuiteVersionTests(unittest.TestCase):
    def suite_task(self, suite_dir: str, **overrides: Any) -> dict[str, Any]:
        task = {
            "id": "s/t",
            "suite": "s",
            "suite_label": "s@1.0.0",
            "suite_dir": suite_dir,
            "repo": f"{suite_dir}/t/repo",
            "hidden_tests": f"{suite_dir}/t/hidden_tests",
            "solution": f"{suite_dir}/t/solution",
            "prompt": "p",
        }
        task.update(overrides)
        return task

    def test_the_hash_does_not_depend_on_where_the_suite_is_checked_out(self) -> None:
        self.assertEqual(
            hb.suite_version_of([self.suite_task("/one/s")]), hb.suite_version_of([self.suite_task("/other/place/s")])
        )

    def test_the_hash_follows_which_directories_the_task_uses(self) -> None:
        a = self.suite_task("/one/s")
        for key in ("repo", "hidden_tests", "solution"):
            with self.subTest(key=key):
                b = self.suite_task("/one/s", **{key: f"/one/s/other/{key}"})
                self.assertNotEqual(hb.suite_version_of([a]), hb.suite_version_of([b]))

    def test_a_git_url_repo_stays_in_the_hash(self) -> None:
        a = self.suite_task("/one/s", repo="https://example.com/x.git")
        b = self.suite_task("/one/s", repo="https://example.com/y.git")
        self.assertNotEqual(hb.suite_version_of([a]), hb.suite_version_of([b]))


class ConfigTaskKeyTests(RunTestCase):
    """The keys a suite adds to its tasks are the engine's, never a config task's (it would pose as a suite task)."""

    def test_a_config_task_with_a_suite_key_is_rejected_naming_the_key(self) -> None:
        for key in ("suite", "suite_kind", "suite_label", "suite_dir", "solution"):
            with self.subTest(key=key), self.assertRaises(SystemExit) as cm:
                self.cfg["tasks"] = [dict(fixtures.fizzbuzz_task(), **{key: "x"})]
                fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
            self.assertTrue(all(s in str(cm.exception.code) for s in ("fizzbuzz", key, "suite")), cm.exception.code)

    def test_the_check_runs_without_a_suites_key_too(self) -> None:
        self.cfg["tasks"] = [dict(fixtures.fizzbuzz_task(), suite="x")]
        with self.assertRaises(SystemExit):
            hb.expand_suites(self.cfg, self.base)


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

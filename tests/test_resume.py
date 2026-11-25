"""Resume by `trial_id`, the heartbeat and the legacy row of a trial (ADR 0010, decisions 8 and 9)."""

import contextlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import fixtures
import harness_bench as hb
from hbench import results
from hbench.core.convert import row_from_trial, trial_from_row
from hbench.core.ids import trial_id
from hbench.core.models import RunInfo, TrialStatus
from hbench.results import (
    HEARTBEAT_FILE,
    Heartbeat,
    ResumeError,
    RunWriter,
    heartbeat_is_live,
    read_heartbeat,
)
from test_core_models import run_info, trial
from test_run import RunTestCase, args, rows_of
from test_sandboxes import RecordingSandbox

NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)


def dead_pid() -> int:
    """The pid of a process that has ended and been reaped."""
    child = subprocess.Popen([fixtures.PY, "-c", "pass"])
    child.wait()
    return child.pid


def write_heartbeat(path: Path, **overrides: Any) -> None:
    beat: dict[str, Any] = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "trial_id": "t",
        "phase": "agent",
        "updated_at": NOW.isoformat(),
    }
    beat.update(overrides)
    path.write_text(json.dumps(beat))


class RowFromTrialTests(unittest.TestCase):
    def test_the_legacy_row_of_a_trial_carries_what_reconcile_and_report_read(self) -> None:
        original = {
            "ts": "2026-11-24T12:00:00+00:00",
            "task": "fizzbuzz",
            "harness": "claude",
            "model": "m",
            "rep": 2,
            "passed": False,
            "agent_rc": 3,
            "agent_timed_out": True,
            "wall_s": 4.5,
            "parse_ok": True,
            "run_tag": "bench-20261124120000-x",
            "t_start": 0.0,
            "t_end": 0.0,
            "sandboxed": True,
            "input": 10,
            "output": 5,
            "reasoning": 2,
            "cache_read": 7,
            "cache_write": 1,
            "turns": 3,
            "tool_calls": 4,
            "cost_reported": 0.3,
            "cost_computed": 0.2,
        }
        t = trial_from_row(original, run_id="r" * 32, suite_version="s", harness_version="1")
        row = row_from_trial(t)
        times = ("t_start", "t_end")
        self.assertEqual(
            {k: v for k, v in row.items() if k not in times}, {k: v for k, v in original.items() if k not in times}
        )
        self.assertEqual((row["t_end"], row["t_start"]), (NOW.timestamp(), NOW.timestamp() - 4.5))
        self.assertNotIn("cost_litellm", row)

    def test_a_gateway_cost_comes_back_as_cost_litellm_and_unknown_costs_stay_none(self) -> None:
        t = trial()
        row = row_from_trial(
            trial(
                usage=type(t.usage)(
                    input_tokens=1, output_tokens=1, parsed=False, costs=type(t.usage.costs)(gateway=0.0)
                )
            )
        )
        self.assertEqual(
            (row["cost_litellm"], row["cost_reported"], row["cost_computed"], row["parse_ok"]), (0.0, None, None, False)
        )

    def test_a_trial_that_timed_out_is_an_agent_timeout_and_one_that_passed_is_not(self) -> None:
        self.assertEqual(
            (
                row_from_trial(trial(status=TrialStatus.TIMEOUT))["agent_timed_out"],
                row_from_trial(trial(status=TrialStatus.PASSED))["agent_timed_out"],
                row_from_trial(trial(status=TrialStatus.FAILED))["passed"],
            ),
            (True, False, False),
        )


class RunWriterResumeTests(unittest.TestCase):
    def make(self, out: Path, *trials: Any, run_id: str = "a" * 32) -> RunWriter:
        writer = RunWriter(out, run_info(run_id=run_id))
        for t in trials:
            writer.append(t)
        return writer

    def test_open_existing_keeps_the_run_and_appends_to_its_trials(self) -> None:
        out = fixtures.tmpdir(self)
        first = self.make(out, trial(trial_id="1" * 16))
        reopened = RunWriter.open_existing(out, "a" * 32)
        reopened.append(trial(trial_id="2" * 16, rep=2))
        self.assertEqual(
            (reopened.info, reopened.finished_trial_ids(), first.info == reopened.info),
            (first.info, {"1" * 16, "2" * 16}, True),
        )

    def test_reopening_does_not_rewrite_run_json(self) -> None:
        out = fixtures.tmpdir(self)
        self.make(out)
        info_path = out / ("a" * 32) / "run.json"
        info_path.write_text(info_path.read_text() + "\n\n")  # a marker that a rewrite would remove
        before = info_path.read_text()
        RunWriter.open_existing(out, "a" * 32)
        self.assertEqual(info_path.read_text(), before)

    def test_every_append_is_flushed_to_disk(self) -> None:
        out = fixtures.tmpdir(self)
        writer = self.make(out)
        with mock.patch("os.fsync") as fsync:
            writer.append(trial())
        fsync.assert_called_once()

    def test_a_last_line_cut_short_by_a_crash_is_dropped(self) -> None:
        out = fixtures.tmpdir(self)
        writer = self.make(out, trial(trial_id="1" * 16))
        with writer.trials_path.open("a") as f:
            f.write('{"run_id": "cut off')
        reopened = RunWriter.open_existing(out, "a" * 32)
        reopened.append(trial(trial_id="2" * 16, rep=2))
        self.assertEqual(reopened.finished_trial_ids(), {"1" * 16, "2" * 16})

    def test_a_trials_file_that_was_never_created_reads_as_empty(self) -> None:
        out = fixtures.tmpdir(self)
        writer = self.make(out)
        writer.trials_path.unlink()
        self.assertEqual(RunWriter.open_existing(out, "a" * 32).finished_trial_ids(), set())

    def test_a_run_id_that_is_not_a_run_id_is_refused_without_touching_the_path(self) -> None:
        out = fixtures.tmpdir(self)
        (out / "x").mkdir()
        for bad in ("../x", "x", "", "A" * 32, "a" * 31, "/etc", "a" * 32 + "/../x"):
            with self.subTest(run_id=bad), self.assertRaisesRegex(ResumeError, "not a run id"):
                RunWriter.open_existing(out, bad)

    def test_a_run_that_does_not_exist_is_a_clean_error(self) -> None:
        with self.assertRaisesRegex(ResumeError, "no run " + "b" * 32):
            RunWriter.open_existing(fixtures.tmpdir(self), "b" * 32)

    def test_invalid_run_json_or_trials_are_a_clean_error(self) -> None:
        out = fixtures.tmpdir(self)
        writer = self.make(out)
        (writer.run_dir / "trials.jsonl").write_text("not json\n")
        with self.assertRaisesRegex(ResumeError, "trial data"):
            RunWriter.open_existing(out, "a" * 32).finished_trial_ids()
        (writer.run_dir / "run.json").write_text("{}")
        with self.assertRaisesRegex(ResumeError, "not valid run data"):
            RunWriter.open_existing(out, "a" * 32)


class HeartbeatTests(unittest.TestCase):
    def test_the_file_has_pid_host_trial_phase_and_time_and_is_removed_at_the_end(self) -> None:
        path = fixtures.tmpdir(self) / HEARTBEAT_FILE
        with Heartbeat(path, extra={"work_root": "/w"}) as heartbeat:
            heartbeat.beat("trial-1", "agent")
            data = read_heartbeat(path)
            assert data is not None
            self.assertEqual(
                (data["pid"], data["trial_id"], data["phase"], data["work_root"]),
                (os.getpid(), "trial-1", "agent", "/w"),
            )
            self.assertEqual(data["hostname"], socket.gethostname())
            self.assertIsNotNone(datetime.fromisoformat(data["updated_at"]).utcoffset())
        self.assertFalse(path.exists())

    def test_start_and_stop_can_be_called_without_a_with_block(self) -> None:
        path = fixtures.tmpdir(self) / HEARTBEAT_FILE
        heartbeat = Heartbeat(path)
        heartbeat.start()
        self.assertTrue(path.exists())
        heartbeat.stop()
        self.assertFalse(path.exists())

    def test_the_first_write_happens_on_entry_with_no_trial(self) -> None:
        path = fixtures.tmpdir(self) / HEARTBEAT_FILE
        with Heartbeat(path):
            data = read_heartbeat(path)
            assert data is not None
            self.assertEqual((data["trial_id"], data["phase"]), (None, "start"))

    def test_a_write_that_fails_warns_once_and_the_beat_goes_on(self) -> None:
        path = fixtures.tmpdir(self) / HEARTBEAT_FILE
        attempts: list[int] = []
        real_write = results._atomic_write

        def flaky(target: Path, text: str) -> None:
            attempts.append(1)
            if len(attempts) < 6:
                raise OSError("disk full")
            real_write(target, text)

        stderr = io.StringIO()
        with mock.patch.object(results, "_atomic_write", flaky), contextlib.redirect_stderr(stderr):
            with Heartbeat(path, interval=0.02) as heartbeat:
                heartbeat.beat("t", "agent")  # a phase change on the main thread fails too, and must not raise
                deadline = time.time() + 30
                while not path.exists() and time.time() < deadline:
                    time.sleep(0.005)
                self.assertTrue(heartbeat_is_live(path))
        self.assertEqual(stderr.getvalue().count("warning: heartbeat"), 1)
        self.assertIn("disk full", stderr.getvalue())

    def test_a_thread_rewrites_the_file_while_the_run_is_busy(self) -> None:
        path = fixtures.tmpdir(self) / HEARTBEAT_FILE
        writes: list[int] = []
        real_write = Heartbeat._write

        def spy(self: Heartbeat) -> None:
            writes.append(1)
            real_write(self)

        with mock.patch.object(Heartbeat, "_write", spy), Heartbeat(path, interval=0.02):
            deadline = time.time() + 30
            while len(writes) < 4 and time.time() < deadline:
                time.sleep(0.005)
            self.assertTrue(heartbeat_is_live(path))
        self.assertGreaterEqual(len(writes), 4)
        self.assertFalse(path.exists())

    def test_the_file_is_removed_when_the_run_ends_with_an_error(self) -> None:
        path = fixtures.tmpdir(self) / HEARTBEAT_FILE
        with self.assertRaises(RuntimeError), Heartbeat(path):
            raise RuntimeError("boom")
        self.assertFalse(path.exists())


class LivenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = fixtures.tmpdir(self) / HEARTBEAT_FILE

    def live(self, **overrides: Any) -> bool:
        write_heartbeat(self.path, **overrides)
        return heartbeat_is_live(self.path, now=NOW + timedelta(seconds=30))

    def test_a_fresh_beat_of_a_live_pid_is_live(self) -> None:
        self.assertTrue(self.live())

    def test_a_fresh_beat_of_a_dead_pid_is_not(self) -> None:
        self.assertFalse(self.live(pid=dead_pid()))

    def test_a_stale_beat_is_not_live_even_for_a_live_pid(self) -> None:
        write_heartbeat(self.path)
        self.assertFalse(heartbeat_is_live(self.path, now=NOW + timedelta(seconds=90)))
        self.assertTrue(heartbeat_is_live(self.path, now=NOW + timedelta(seconds=89)))

    def test_a_fresh_beat_of_another_host_counts_as_live_because_its_pid_cannot_be_checked(self) -> None:
        self.assertTrue(self.live(hostname="another-host", pid=dead_pid()))

    def test_a_pid_of_another_user_counts_as_alive(self) -> None:
        with mock.patch("os.kill", side_effect=PermissionError):
            self.assertTrue(self.live())

    def test_a_missing_or_malformed_beat_is_not_live(self) -> None:
        self.assertFalse(heartbeat_is_live(self.path, now=NOW))
        self.path.write_text("not json")
        self.assertFalse(heartbeat_is_live(self.path, now=NOW))
        self.path.write_text("[1]")
        self.assertFalse(heartbeat_is_live(self.path, now=NOW))
        for overrides in (
            {"pid": 0},
            {"pid": -1},
            {"pid": True},
            {"pid": "1"},
            {"hostname": 3},
            {"updated_at": "yesterday"},
            {"updated_at": None},
            {"updated_at": "2026-11-24T12:00:00"},
        ):
            with self.subTest(overrides=overrides):
                self.assertFalse(self.live(**overrides))


class ResumeRunTests(RunTestCase):
    def cmd_run(self, **overrides: Any) -> str:
        return fixtures.call_quiet(hb.cmd_run, args(**overrides), self.cfg, self.base)

    def run_dirs(self) -> list[Path]:
        return sorted(p.parent for p in (self.base / "results").glob("*/trials.jsonl"))

    def first_run(self, **overrides: Any) -> Path:
        self.cmd_run(**overrides)
        (run_dir,) = self.run_dirs()
        return run_dir

    def trials(self, run_dir: Path) -> list[dict[str, Any]]:
        return fixtures.read_rows(run_dir / "trials.jsonl")

    def test_resume_runs_only_the_trials_the_run_has_not_finished_in_the_same_run(self) -> None:
        run_dir = self.first_run(only=["claude"])
        out = self.cmd_run(resume=run_dir.name)
        trials = self.trials(run_dir)
        self.assertEqual(
            (self.run_dirs(), [t["target"]["harness"] for t in trials], out.count("run   "), out.count("skip  ")),
            ([run_dir], ["claude", "pi"], 1, 1),
        )
        self.assertEqual(len({t["trial_id"] for t in trials}), 2)
        self.assertEqual([r["harness"] for r in rows_of(self.base)], ["claude", "pi"])
        self.assertEqual({t["run_id"] for t in trials}, {run_dir.name})

    def test_resume_of_a_finished_run_repeats_nothing(self) -> None:
        run_dir = self.first_run()
        before = (run_dir / "trials.jsonl").read_text(), (self.base / "results" / "results.jsonl").read_text()
        out = self.cmd_run(resume=run_dir.name)
        self.assertEqual(
            (
                out.count("run   "),
                (run_dir / "trials.jsonl").read_text(),
                (self.base / "results" / "results.jsonl").read_text(),
            ),
            (0, *before),
        )

    def test_a_trial_written_before_its_legacy_row_is_neither_repeated_nor_lost(self) -> None:
        run_dir = self.first_run()
        results = self.base / "results" / "results.jsonl"
        lines = results.read_text().splitlines(keepends=True)
        results.write_text(lines[0])  # the crash: the second trial reached trials.jsonl, its legacy row did not
        trials_before = (run_dir / "trials.jsonl").read_text()
        out = self.cmd_run(resume=run_dir.name)
        rows = rows_of(self.base)
        original = json.loads(lines[1])
        self.assertEqual(
            (out.count("run   "), (run_dir / "trials.jsonl").read_text(), [r["harness"] for r in rows]),
            (0, trials_before, ["claude", "pi"]),
        )
        rewritten = rows[1]
        for key in (
            "task",
            "harness",
            "model",
            "rep",
            "passed",
            "agent_rc",
            "parse_ok",
            "run_tag",
            "sandboxed",
            "input",
            "output",
            "turns",
        ):
            self.assertEqual(rewritten[key], original[key], key)

    def test_the_trial_is_durable_before_the_legacy_row_is_written(self) -> None:
        real_dumps = json.dumps

        def dumps(obj: Any, *a: Any, **kw: Any) -> str:
            if isinstance(obj, dict) and "agent_rc" in obj:  # the legacy row
                raise OSError("killed before the legacy row")
            return real_dumps(obj, *a, **kw)

        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}]
        with mock.patch.object(json, "dumps", dumps), self.assertRaises(OSError):
            self.cmd_run()
        (run_dir,) = self.run_dirs()
        results = self.base / "results" / "results.jsonl"
        self.assertEqual((len(self.trials(run_dir)), results.read_text()), (1, ""))
        out = self.cmd_run(resume=run_dir.name)
        self.assertEqual((out.count("run   "), len(self.trials(run_dir)), len(rows_of(self.base))), (0, 1, 1))

    def test_a_second_resume_after_the_repair_changes_nothing(self) -> None:
        run_dir = self.first_run()
        results = self.base / "results" / "results.jsonl"
        results.write_text(results.read_text().splitlines(keepends=True)[0])
        self.cmd_run(resume=run_dir.name)
        after_first = results.read_text()
        self.cmd_run(resume=run_dir.name)
        self.assertEqual(results.read_text(), after_first)

    def test_resume_refuses_while_a_live_heartbeat_holds_the_run_and_force_overrides(self) -> None:
        run_dir = self.first_run(only=["claude"])
        write_heartbeat(run_dir / HEARTBEAT_FILE, updated_at=datetime.now(UTC).isoformat())
        with self.assertRaises(SystemExit) as cm:
            self.cmd_run(resume=run_dir.name)
        self.assertIn("still running", str(cm.exception.code))
        self.assertIn("--force", str(cm.exception.code))
        self.assertEqual(len(self.trials(run_dir)), 1)
        self.cmd_run(resume=run_dir.name, force=True)
        self.assertEqual(len(self.trials(run_dir)), 2)

    def test_a_heartbeat_of_a_dead_process_does_not_block_resume(self) -> None:
        run_dir = self.first_run(only=["claude"])
        write_heartbeat(run_dir / HEARTBEAT_FILE, pid=dead_pid(), updated_at=datetime.now(UTC).isoformat())
        self.cmd_run(resume=run_dir.name)
        self.assertEqual(len(self.trials(run_dir)), 2)
        self.assertFalse((run_dir / HEARTBEAT_FILE).exists())

    def test_resume_of_a_run_that_does_not_exist_or_a_path_exits_with_a_message(self) -> None:
        self.first_run()
        for run_id in ("c" * 32, "../results", "..", "/etc/passwd"):
            with self.subTest(run_id=run_id), self.assertRaises(SystemExit) as cm:
                self.cmd_run(resume=run_id)
            self.assertIn("resume:", str(cm.exception.code))
        self.assertEqual(len(self.run_dirs()), 1)

    def test_resume_with_another_config_is_refused(self) -> None:
        run_dir = self.first_run(only=["claude"])
        self.cfg["timeout"] = 5
        with self.assertRaises(SystemExit) as cm:
            self.cmd_run(resume=run_dir.name)
        self.assertIn("config changed", str(cm.exception.code))

    def test_resume_cannot_be_a_dry_run(self) -> None:
        run_dir = self.first_run()
        with self.assertRaises(SystemExit) as cm:
            self.cmd_run(resume=run_dir.name, dry_run=True)
        self.assertIn("--dry-run", str(cm.exception.code))

    def test_resume_without_the_flag_still_skips_done_keys_in_a_new_run(self) -> None:
        self.first_run()
        out = self.cmd_run()
        self.assertEqual((out.count("skip  "), len(self.run_dirs())), (2, 2))

    def test_the_heartbeat_is_written_during_the_run_and_gone_after_it(self) -> None:
        phases: list[str] = []
        real_beat = Heartbeat.beat

        def spy(self: Heartbeat, trial_id: str | None, phase: str) -> None:
            phases.append(phase)
            real_beat(self, trial_id, phase)

        with mock.patch.object(Heartbeat, "beat", spy):
            run_dir = self.first_run(only=["claude"])
        self.assertEqual(phases, ["setup", "agent", "grade", "record"])
        self.assertFalse((run_dir / HEARTBEAT_FILE).exists())

    def test_resume_removes_the_temp_dirs_an_interrupted_run_left_but_only_its_own_kind(self) -> None:
        run_dir = self.first_run(only=["claude"])
        tmp = Path(tempfile.gettempdir())
        work = Path(tempfile.mkdtemp(prefix="harness-bench-work-"))
        git = Path(tempfile.mkdtemp(prefix="harness-bench-git-"))
        other = Path(tempfile.mkdtemp(prefix="something-else-"))
        for path in (work, git, other):
            self.addCleanup(shutil.rmtree, path, True)
        (work / ".trial.env").write_text("SECRET=1")
        write_heartbeat(
            run_dir / HEARTBEAT_FILE,
            pid=dead_pid(),
            work_root=str(work),
            work_token=hb.plant_token(work),
            git_root=str(git),
            git_token=hb.plant_token(git),
            updated_at=NOW.isoformat(),
        )
        self.cmd_run(resume=run_dir.name)
        self.assertEqual((work.exists(), git.exists(), other.exists()), (False, False, True))
        write_heartbeat(
            run_dir / HEARTBEAT_FILE,
            pid=dead_pid(),
            work_root=str(other),
            git_root=str(tmp),
            updated_at=NOW.isoformat(),
        )
        self.cmd_run(resume=run_dir.name)
        self.assertEqual((other.exists(), tmp.exists()), (True, True))
        write_heartbeat(run_dir / HEARTBEAT_FILE, pid=dead_pid(), work_root=5, updated_at=NOW.isoformat())
        self.cmd_run(resume=run_dir.name)

    def test_a_heartbeat_can_only_remove_a_temp_dir_that_holds_its_token(self) -> None:
        run_dir = self.first_run(only=["claude"])
        stranger = Path(tempfile.mkdtemp(prefix="harness-bench-work-"))  # the right name, but not made by that run
        tokened = Path(tempfile.mkdtemp(prefix="harness-bench-git-"))
        real = hb.plant_token(tokened)
        for path in (stranger, tokened):
            (path / "keep.txt").write_text("keep")
            self.addCleanup(shutil.rmtree, path, True)
        cases: list[dict[str, Any]] = [
            {"work_root": str(stranger)},  # no token in the heartbeat nor in the dir
            {"work_root": str(stranger), "work_token": "guess"},  # a token the dir does not hold
            {"work_root": str(stranger), "work_token": 5},
            {"git_root": str(tokened), "git_token": "not-" + real},  # the dir has a token, another one
            {"git_root": str(tokened), "git_token": ""},
        ]
        for case in cases:
            with self.subTest(case=case):
                write_heartbeat(run_dir / HEARTBEAT_FILE, pid=dead_pid(), updated_at=NOW.isoformat(), **case)
                self.cmd_run(resume=run_dir.name)
                self.assertEqual((stranger / "keep.txt").exists(), (tokened / "keep.txt").exists(), True)

    def test_the_private_temp_dirs_of_a_run_hold_a_token_that_its_heartbeat_names(self) -> None:
        seen: list[dict[str, Any]] = []
        real_beat = Heartbeat.beat

        def spy(self: Heartbeat, trial_id: str | None, phase: str) -> None:
            data = read_heartbeat(self.path)
            if data:
                seen.append(data)
            real_beat(self, trial_id, phase)

        with mock.patch.object(Heartbeat, "beat", spy):
            self.first_run(only=["claude"])
        beat = seen[0]
        self.assertEqual(len(beat["work_token"]), 32)
        self.assertEqual(len(beat["git_token"]), 32)
        self.assertNotEqual(beat["work_token"], beat["git_token"])

    def test_a_heartbeat_that_names_a_link_to_another_directory_removes_nothing(self) -> None:
        run_dir = self.first_run(only=["claude"])
        victim = fixtures.tmpdir(self)
        (victim / "keep.txt").write_text("keep")
        link = Path(tempfile.gettempdir()) / f"harness-bench-work-link-{os.getpid()}"
        link.symlink_to(victim, target_is_directory=True)
        self.addCleanup(link.unlink, True)
        write_heartbeat(run_dir / HEARTBEAT_FILE, pid=dead_pid(), work_root=str(link), updated_at=NOW.isoformat())
        self.cmd_run(resume=run_dir.name)
        self.assertEqual(((victim / "keep.txt").read_text(), link.is_symlink()), ("keep", True))

    def test_a_leftover_env_file_of_the_trial_is_removed_before_it_runs_again(self) -> None:
        work = fixtures.tmpdir(self) / "work"
        work.mkdir()
        self.cfg["work_dir"] = str(work)
        run_dir = self.first_run(only=["claude"])
        leftover = work / ".fizzbuzz__pi__p_nofix__r1.env"
        leftover.write_text("SECRET=1")
        seen: list[bool] = []
        real_write = hb.write_env_file

        def spy(env: Any, path: Path) -> None:
            seen.append(path.exists() and path.read_text() == "SECRET=1")
            real_write(env, path)

        with mock.patch.object(hb, "write_env_file", spy):
            self.cmd_run(resume=run_dir.name)
        self.assertEqual((bool(seen), any(seen), leftover.exists()), (True, False, False))

    def test_resume_reclaims_the_containers_of_each_trial_it_runs_again_and_a_plain_run_does_not(self) -> None:
        sandbox = RecordingSandbox()
        with mock.patch.object(hb, "make_sandbox", return_value=sandbox):
            run_dir = self.first_run(only=["claude"])
            self.assertEqual([e for e in sandbox.events if e.startswith("reclaim")], [])
            self.cmd_run(resume=run_dir.name)
        pi_trial = self.trials(run_dir)[1]["trial_id"]
        self.assertEqual([e for e in sandbox.events if e.startswith("reclaim")], [f"reclaim {pi_trial}"])
        self.assertIn(f"create {pi_trial}", sandbox.events)

    def test_the_trial_id_of_a_trial_is_known_before_it_runs(self) -> None:
        run_dir = self.first_run()
        run_id = run_dir.name
        expected = {
            trial_id(run_id, "fizzbuzz", f"{h}@unknown:{m}", 1) for h, m in (("claude", "fix"), ("pi", "p/nofix"))
        }
        self.assertEqual({t["trial_id"] for t in self.trials(run_dir)}, expected)

    def test_the_run_records_the_matrix_of_the_run_it_resumes(self) -> None:
        run_dir = self.first_run(only=["claude"])
        before = RunInfo.from_dict(json.loads((run_dir / "run.json").read_text()))
        self.cmd_run(resume=run_dir.name)
        after = RunInfo.from_dict(json.loads((run_dir / "run.json").read_text()))
        self.assertEqual(
            (after.run_id, after.targets, after.started_at), (before.run_id, before.targets, before.started_at)
        )
        self.assertIsNotNone(after.finished_at)


class KillAndResumeTests(unittest.TestCase):
    """The acceptance test: SIGKILL a real run after its first trial, resume it, and check every trial exists once."""

    def test_a_run_killed_after_its_first_trial_resumes_to_exactly_the_expected_trials(self) -> None:
        base = fixtures.tmpdir(self)
        fixtures.copy_engine(base)
        shutil.copytree(fixtures.FIZZBUZZ, base / "tasks" / "fizzbuzz")
        flag, pid_file, tags = base / "slow.flag", base / "slow.pid", base / "tags.txt"
        flag.write_text("")
        env_vars = {
            "FAKE_TAGS_FILE": "${FAKE_TAGS_FILE}",
            "FAKE_SLOW_FLAG": "${FAKE_SLOW_FLAG}",
            "FAKE_SLOW_PID": "${FAKE_SLOW_PID}",
        }
        cfg = {
            "reps": 1,
            "harnesses": {
                "fast": fixtures.harness("claude", env_vars, "fix"),
                "slow": fixtures.harness("pi", env_vars, "slow-once"),
            },
            "matrix": [{"harness": "fast", "model": "m"}, {"harness": "slow", "model": "p/m"}],
            "tasks": [fixtures.fizzbuzz_task()],
        }
        (base / "config.json").write_text(json.dumps(cfg))
        env = dict(os.environ, FAKE_TAGS_FILE=str(tags), FAKE_SLOW_FLAG=str(flag), FAKE_SLOW_PID=str(pid_file))
        command = [fixtures.PY, "harness_bench.py", "run", "--config", "config.json", "--allow-unsandboxed"]
        with (base / "first.log").open("w") as log:
            engine = subprocess.Popen(
                command, cwd=base, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        self.addCleanup(self.stop_everything, engine, pid_file)
        trials_files = base / "results"
        deadline = time.time() + 120
        while time.time() < deadline and engine.poll() is None:
            if any(p.stat().st_size for p in trials_files.glob("*/trials.jsonl")):
                break
            time.sleep(0.02)
        self.assertIsNone(engine.poll(), (base / "first.log").read_text())
        os.killpg(engine.pid, signal.SIGKILL)  # the engine only: the agent has its own session
        engine.wait()
        (run_dir,) = [p.parent for p in trials_files.glob("*/trials.jsonl")]
        heartbeat = read_heartbeat(run_dir / HEARTBEAT_FILE)
        assert heartbeat is not None
        self.assertEqual(heartbeat["pid"], engine.pid)  # the killed run left its heartbeat behind
        leftovers = [Path(heartbeat[k]) for k in ("work_root", "git_root")]
        self.assertTrue(all(p.exists() for p in leftovers))
        flag.unlink()
        resumed = subprocess.run(
            [*command, "--resume", run_dir.name], cwd=base, env=env, capture_output=True, text=True, timeout=300
        )
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        run_id = run_dir.name
        expected = {trial_id(run_id, "fizzbuzz", f"{h}@unknown:{m}", 1) for h, m in (("fast", "m"), ("slow", "p/m"))}
        stored = [t["trial_id"] for t in fixtures.read_rows(run_dir / "trials.jsonl")]
        keys = [
            (r["task"], r["harness"], r["model"], r["rep"])
            for r in fixtures.read_rows(base / "results" / "results.jsonl")
        ]
        self.assertEqual((sorted(stored), sorted(set(stored))), (sorted(expected), sorted(expected)))
        self.assertEqual(sorted(keys), [("fizzbuzz", "fast", "m", 1), ("fizzbuzz", "slow", "p/m", 1)])
        self.assertTrue(all(r["passed"] for r in fixtures.read_rows(base / "results" / "results.jsonl")))
        self.assertEqual(
            (len(list((base / "results").glob("*/trials.jsonl"))), (run_dir / HEARTBEAT_FILE).exists()), (1, False)
        )
        self.assertEqual([p.exists() for p in leftovers], [False, False])
        self.assertIsNotNone(RunInfo.from_dict(json.loads((run_dir / "run.json").read_text())).finished_at)

    @staticmethod
    def stop_everything(engine: "subprocess.Popen[bytes]", pid_file: Path) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(engine.pid, signal.SIGKILL)
        engine.wait()
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)

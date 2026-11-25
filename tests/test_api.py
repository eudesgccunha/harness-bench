"""The orchestrator API (ADR 0012): `plan` and `run_trial` give the results of `run`, a forged spec runs nothing,
a retry is safe, and workers on one run directory never tear a line."""

import contextlib
import inspect
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import fixtures
import harness_bench as hb
import run_parallel
from hbench import api, cli
from hbench.core.convert import row_from_trial
from hbench.core.ids import trial_id as make_trial_id
from hbench.core.models import RunInfo, TrialResult, TrialSpec
from hbench.results import LogCursor, RunWriter, locked_append, locked_log, read_run, trial_lock
from hbench.sinks import otlp, sqlite
from hbench.sinks.otlp import TRACES_FILE
from test_core_models import trial
from test_run import RunTestCase, args, rows_of

VOLATILE_TRIAL_KEYS = {"run_id", "trial_id", "started_at", "latency_s", "trace_id", "artifact_uri"}


def masked(result: TrialResult | dict[str, Any]) -> dict[str, Any]:
    """A trial without its ids and clocks: what two runs of the same trial must share."""
    data = result.to_dict() if isinstance(result, TrialResult) else dict(result)
    return {k: v for k, v in data.items() if k not in VOLATILE_TRIAL_KEYS}


def tree(path: Path) -> list[str]:
    """Every file and directory under `path`, to prove that nothing was written."""
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


class ApiTestCase(RunTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config_path = self.base / "config.json"
        self.write_config()

    def write_config(self) -> api.Config:
        self.config_path.write_text(json.dumps(self.cfg))
        self.config = api.load_config(self.config_path)
        return self.config

    def start(self, **kwargs: Any) -> tuple[Any, list[TrialSpec]]:
        run = api.start_run(self.config, **kwargs)
        return run, api.plan(self.config, run, **kwargs)

    def run_dir(self, run_id: str) -> Path:
        return self.base / "results" / run_id

    def run_all(self) -> tuple[Any, list[TrialResult]]:
        run, specs = self.start()
        with fixtures.quiet():
            return run, [api.run_trial(self.config, spec, allow_unsandboxed=True) for spec in specs]


class SignatureTests(unittest.TestCase):
    def test_the_public_signatures_are_pinned(self) -> None:
        only = "only: collections.abc.Sequence[str] | None = None"
        cfg, info, spec = "config: hbench.api.Config", "hbench.core.models.RunInfo", "hbench.core.models.TrialSpec"
        expected = {
            "load_config": "(path: pathlib.Path | str, *, litellm: bool = False) -> hbench.api.Config",
            "start_run": f"({cfg}, *, {only}, reps: int | None = None) -> {info}",
            "plan": f"({cfg}, run: {info}, *, {only}, reps: int | None = None) -> list[{spec}]",
            "run_trial": (
                f"({cfg}, spec: {spec}, *, allow_unsandboxed: bool = False) -> hbench.core.models.TrialResult"
            ),
            "finish_run": f"({cfg}, run_id: str, *, report: bool = True) -> {info}",
        }
        for name, signature in expected.items():
            with self.subTest(name=name):
                self.assertEqual(str(inspect.signature(getattr(api, name), eval_str=False)).replace("'", ""), signature)


class LoadConfigTests(ApiTestCase):
    def test_it_expands_and_validates_the_config(self) -> None:
        self.assertEqual(
            (self.config.base, self.config.out_dir), (self.base.resolve(), self.base.resolve() / "results")
        )
        self.assertEqual([t["id"] for t in self.config.cfg["tasks"]], ["fizzbuzz"])

    def test_every_config_problem_is_a_config_error(self) -> None:
        bad = self.base / "bad.json"
        with self.assertRaises(api.ConfigError):
            api.load_config(self.base / "missing.json")
        bad.write_text("{not json")
        with self.assertRaises(api.ConfigError):
            api.load_config(bad)
        bad.write_text("[]")
        with self.assertRaises(api.ConfigError) as cm:
            api.load_config(bad)
        self.assertIn("JSON object", str(cm.exception))
        bad.write_text(json.dumps({**self.cfg, "cost_source": "nope"}))
        with self.assertRaises(api.ConfigError):
            api.load_config(bad)
        with self.assertRaises(api.ConfigError) as cm:
            api.load_config(self.config_path, litellm=True)
        self.assertIn("litellm", str(cm.exception))

    def test_the_litellm_profile_drops_the_harnesses_without_spend_logs(self) -> None:
        self.cfg["litellm"] = {"base_url": "http://localhost:4000", "api_key": "${LITELLM_KEY}"}
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}, {"harness": "cursor", "model": "fix"}]
        self.config_path.write_text(json.dumps(self.cfg))
        with fixtures.quiet():
            config = api.load_config(self.config_path, litellm=True)
        self.assertEqual([p["harness"] for p in config.cfg["matrix"]], ["claude"])
        self.assertEqual(config.out_dir.name, "results-litellm")


class StartAndPlanTests(ApiTestCase):
    def test_start_run_creates_the_run_files_and_plan_lists_every_trial_in_run_order(self) -> None:
        run, specs = self.start(reps=2)
        self.assertEqual(sorted(p.name for p in self.run_dir(run.run_id).iterdir()), ["run.json", "trials.jsonl"])
        self.assertEqual(
            [(s.harness, s.model, s.task_id, s.rep) for s in specs],
            [("claude", "fix", "fizzbuzz", 1), ("claude", "fix", "fizzbuzz", 2)]
            + [("pi", "p/nofix", "fizzbuzz", 1), ("pi", "p/nofix", "fizzbuzz", 2)],
        )
        self.assertEqual({s.run_id for s in specs}, {run.run_id})
        self.assertEqual(len({s.trial_id for s in specs}), 4)

    def test_plan_is_pure(self) -> None:
        run = api.start_run(self.config)
        before = tree(self.base)
        first = api.plan(self.config, run, only=["pi"])
        self.assertEqual((tree(self.base), [s.harness for s in first]), (before, ["pi"]))
        self.assertEqual(api.plan(self.config, run, only=["pi"]), first)

    def test_plan_takes_the_reps_of_the_run_and_refuses_more(self) -> None:
        run = api.start_run(self.config, reps=2)
        self.assertEqual(run.reps, 2)
        self.assertEqual(len(api.plan(self.config, run)), 4)
        with self.assertRaises(api.RunError):
            api.plan(self.config, run, reps=3)

    def test_plan_uses_the_trial_ids_that_run_records(self) -> None:
        with fixtures.quiet():
            hb.cmd_run(args(), self.cfg, self.base)
        (run_dir,) = (self.base / "results").glob("*/run.json")
        info, trials = read_run(run_dir.parent)
        specs = api.plan(self.config, info)
        self.assertEqual({s.trial_id for s in specs}, {t.trial_id for t in trials})

    def test_a_bad_reps_stops_before_a_run_directory_exists(self) -> None:
        for bad in (0, -1):
            with self.assertRaises(api.ConfigError):
                api.start_run(self.config, reps=bad)
        self.cfg["reps"] = "many"
        with self.assertRaises(api.ConfigError):
            api.start_run(self.write_config())
        self.assertFalse(list((self.base / "results").glob("*/run.json")) if (self.base / "results").exists() else [])

    def test_a_changed_config_cannot_plan_or_reopen_the_run(self) -> None:
        run = api.start_run(self.config)
        self.cfg["timeout"] = 5
        changed = self.write_config()
        with self.assertRaises(api.RunError):
            api.plan(changed, run)
        with self.assertRaises(api.RunError):
            api.open_run(changed, run.run_id)

    def test_open_run_returns_a_run_that_exists_and_refuses_one_that_does_not(self) -> None:
        run = api.start_run(self.config)
        self.assertEqual(api.open_run(self.config, run.run_id), run)
        with self.assertRaises(api.RunError):
            api.open_run(self.config, "e" * 32)


class RunTrialTests(ApiTestCase):
    def test_run_trial_records_what_run_records_for_the_same_trial(self) -> None:
        _, results = self.run_all()
        other = fixtures.tmpdir(self)
        shutil.copytree(fixtures.FIZZBUZZ, other / "tasks" / "fizzbuzz")
        with fixtures.quiet():
            hb.cmd_run(args(), self.cfg, other)
        reference = [masked(row) for row in fixtures.read_rows(next((other / "results").glob("*/trials.jsonl")))]
        self.assertEqual([masked(r) for r in results], reference)
        self.assertEqual([r.passed for r in results], [True, False])

    def test_a_legacy_row_cut_short_by_a_crash_does_not_spoil_the_next_row(self) -> None:
        run, specs = self.start(only=["claude"])
        results = self.base / "results" / "results.jsonl"
        results.write_text('{"task": "cut')
        with fixtures.quiet():
            api.run_trial(self.config, specs[0], allow_unsandboxed=True)
        self.assertEqual(len(rows_of(self.base)), 1)  # every line is valid JSON

    def test_the_result_is_in_both_result_files(self) -> None:
        run, results = self.run_all()
        info, recorded = read_run(self.run_dir(run.run_id))
        self.assertEqual(recorded, results)
        self.assertEqual(
            [(r["task"], r["harness"], r["model"], r["rep"]) for r in rows_of(self.base)],
            [("fizzbuzz", "claude", "fix", 1), ("fizzbuzz", "pi", "p/nofix", 1)],
        )
        self.assertEqual(rows_of(self.base)[0]["passed"], row_from_trial(results[0])["passed"])

    def test_a_trial_that_is_already_recorded_returns_its_result_and_runs_nothing(self) -> None:
        run, specs = self.start(only=["claude"])
        with fixtures.quiet():
            first = api.run_trial(self.config, specs[0], allow_unsandboxed=True)
        with mock.patch.object(hb, "run_trial_isolated", side_effect=AssertionError("ran again")):
            second = api.run_trial(self.config, specs[0])  # no sandbox flag: it never gets that far
        self.assertEqual(second, first)
        self.assertEqual(len(read_run(self.run_dir(run.run_id))[1]), 1)
        self.assertEqual(len(rows_of(self.base)), 1)

    def test_two_workers_that_ran_the_same_trial_leave_one_record(self) -> None:
        run, specs = self.start(only=["claude"])
        with fixtures.quiet():
            first = api.run_trial(self.config, specs[0], allow_unsandboxed=True)
            info = read_run(self.run_dir(run.run_id))[0]
            # The second worker read the run before the first one wrote its trial.
            with mock.patch.object(api, "read_run", return_value=(info, [])):
                second = api.run_trial(self.config, specs[0], allow_unsandboxed=True)
        self.assertEqual(second, first)
        self.assertEqual((len(read_run(self.run_dir(run.run_id))[1]), len(rows_of(self.base))), (1, 1))

    def test_a_worker_that_waited_for_the_trial_lock_returns_what_the_first_recorded(self) -> None:
        run, specs = self.start(only=["claude"])
        with fixtures.quiet():
            first = api.run_trial(self.config, specs[0], allow_unsandboxed=True)
        real_read_run = read_run
        calls: list[Path] = []

        def read_run_before_the_other_worker_finished(run_dir: Path) -> Any:
            calls.append(run_dir)
            return (real_read_run(run_dir)[0], []) if len(calls) == 1 else real_read_run(run_dir)

        with (
            mock.patch.object(api, "read_run", read_run_before_the_other_worker_finished),
            mock.patch.object(hb, "run_trial_isolated", side_effect=AssertionError("ran again")),
        ):
            second = api.run_trial(self.config, specs[0], allow_unsandboxed=True)
        self.assertEqual((second, len(calls)), (first, 2))

    def test_two_processes_given_the_same_spec_run_the_agent_once(self) -> None:
        slow = (
            "import time, sys, runpy; time.sleep(2); sys.argv = sys.argv[1:]; "
            "runpy.run_path(sys.argv[0], run_name='__main__')"
        )
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, "-c", slow, fixtures.AGENT, "claude", "{model_name}"]
        self.config = self.write_config()
        run, specs = self.start(only=["claude"])
        outputs: list[subprocess.CompletedProcess[str]] = []

        def worker() -> None:
            outputs.append(
                subprocess.run(
                    [fixtures.PY, "-m", "hbench", "run-trial", "--config", str(self.config_path)]
                    + ["--spec", "-", "--allow-unsandboxed"],
                    input=json.dumps(specs[0].to_dict()),
                    capture_output=True,
                    text=True,
                    cwd=fixtures.ROOT,
                )
            )

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([p.returncode for p in outputs], [0, 0], [p.stderr for p in outputs])
        self.assertEqual({json.loads(p.stdout)["trial_id"] for p in outputs}, {specs[0].trial_id})
        self.assertEqual(len((self.base / "tags.txt").read_text().splitlines()), 1)  # the agent ran once
        self.assertEqual((len(read_run(self.run_dir(run.run_id))[1]), len(rows_of(self.base))), (1, 1))

    def test_without_a_sandbox_and_without_the_flag_nothing_runs(self) -> None:
        run, specs = self.start()
        before = tree(self.base)
        with self.assertRaises(api.SandboxRequiredError) as cm:
            api.run_trial(self.config, specs[0])
        self.assertIn("No sandbox configured", str(cm.exception))
        self.assertEqual(tree(self.base), before)

    def test_a_work_dir_inside_the_repository_is_a_config_error(self) -> None:
        self.cfg["work_dir"] = str(self.base / "inside")
        config = self.write_config()
        run = api.start_run(config)
        (spec,) = api.plan(config, run, only=["claude"])
        with self.assertRaises(api.ConfigError) as cm:
            api.run_trial(config, spec, allow_unsandboxed=True)
        self.assertIn("work_dir", str(cm.exception))
        self.assertEqual(read_run(self.run_dir(run.run_id))[1], [])

    def spy_on_private_dirs(self, fail: bool) -> list[tuple[Path, Path]]:
        seen: list[tuple[Path, Path]] = []
        real = hb._run_trial

        def spy(m: Any, planned: Any, tid: str) -> TrialResult:
            seen.append((m.work_root, m.git_root))
            self.assertTrue(m.work_root.is_dir() and m.git_root.is_dir())
            if fail:
                raise RuntimeError("boom")
            result: TrialResult = real(m, planned, tid)
            return result

        patcher = mock.patch.object(hb, "_run_trial", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_the_private_dirs_are_removed_after_the_trial(self) -> None:
        seen = self.spy_on_private_dirs(fail=False)
        self.run_all()
        self.assertEqual(len(seen), 2)
        self.assertEqual(len({work for work, _ in seen}), 2)  # one private work dir per trial
        for work, git in seen:
            self.assertFalse(work.exists() or git.exists())

    def test_the_private_dirs_are_removed_when_the_trial_raises(self) -> None:
        seen = self.spy_on_private_dirs(fail=True)
        run, specs = self.start(only=["claude"])
        with self.assertRaises(RuntimeError), fixtures.quiet():
            api.run_trial(self.config, specs[0], allow_unsandboxed=True)
        ((work, git),) = seen
        self.assertFalse(work.exists() or git.exists())
        self.assertEqual(read_run(self.run_dir(run.run_id))[1], [])

    def test_a_configured_work_dir_is_kept(self) -> None:
        work = fixtures.tmpdir(self) / "work"
        self.cfg["work_dir"] = str(work)
        self.config = self.write_config()
        self.run_all()
        self.assertTrue(work.is_dir())

    def test_a_sink_feed_belongs_to_the_trial_and_the_run_span_to_finish(self) -> None:
        self.cfg["sinks"] = [{"type": "sqlite", "path": "out.db"}, {"type": "otlp"}]
        self.config = self.write_config()
        run, results = self.run_all()
        spans = (self.run_dir(run.run_id) / TRACES_FILE).read_text()
        self.assertEqual(spans.count('"name": "eval.trial"'), 2)
        self.assertNotIn('"name": "eval.run"', spans)
        with contextlib.closing(sqlite3.connect(self.base / "out.db")) as db:
            self.assertEqual(db.execute("SELECT count(*), max(finished_at) FROM trials, runs").fetchone(), (2, None))
        api.finish_run(self.config, run.run_id, report=False)
        self.assertEqual((self.run_dir(run.run_id) / TRACES_FILE).read_text().count('"name": "eval.run"'), 1)
        with contextlib.closing(sqlite3.connect(self.base / "out.db")) as db:
            self.assertIsNotNone(db.execute("SELECT finished_at FROM runs").fetchone()[0])
        self.assertEqual(len(results), 2)


class ForgedSpecTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.started, specs = self.start(only=["pi"])
        self.spec = specs[0]

    def forge(self, **overrides: Any) -> TrialSpec:
        return TrialSpec.from_dict({**self.spec.to_dict(), **overrides})

    def assert_refused(self, spec: TrialSpec, fragment: str) -> None:
        before = tree(self.base)
        with self.assertRaises(api.SpecError) as cm:
            api.run_trial(self.config, spec, allow_unsandboxed=True)
        self.assertIn(fragment, str(cm.exception))
        self.assertEqual(tree(self.base), before)  # nothing ran and nothing was written
        self.assertEqual(read_run(self.run_dir(self.started.run_id))[1], [])

    def test_a_spec_for_another_config_is_refused(self) -> None:
        self.assert_refused(self.forge(config_sha256="0" * 64), "config_sha256")

    def test_an_unknown_task_harness_or_pair_is_refused(self) -> None:
        self.assert_refused(self.forge(task_id="rm-rf"), "unknown task")
        self.assert_refused(self.forge(harness="evil"), "unknown harness")
        self.assert_refused(self.forge(model="other"), "not a pair")
        self.assert_refused(self.forge(harness="claude"), "not a pair")  # a harness of the config, with another model

    def test_a_trial_id_that_is_not_the_recomputed_one_is_refused(self) -> None:
        self.assert_refused(self.forge(trial_id="0" * 16), "trial_id")
        self.assert_refused(self.forge(rep=7), "trial_id")  # the id belongs to rep 1
        self.assert_refused(self.forge(run_id="f" * 32), "trial_id")

    def test_a_trial_outside_the_plan_of_the_run_is_refused(self) -> None:
        # rep 99 with the trial_id that matches it: the run planned one repetition
        planned = api.plan(self.config, self.started, only=["pi"], reps=1)[0]
        beyond = TrialSpec.from_dict(
            {
                **planned.to_dict(),
                "rep": 99,
                "trial_id": make_trial_id(self.started.run_id, "fizzbuzz", "pi@unknown:p/nofix", 99),
            }
        )
        self.assert_refused(beyond, "beyond")
        # a harness the run left out with --only: the run has the target `pi` alone
        other = api.plan(self.config, api.start_run(self.config, only=["claude"]), only=["claude"])[0]
        out_of_run = TrialSpec.from_dict(
            {
                **other.to_dict(),
                "run_id": self.started.run_id,
                "trial_id": make_trial_id(self.started.run_id, "fizzbuzz", "claude@unknown:fix", 1),
            }
        )
        self.assert_refused(out_of_run, "not a target")

    def test_a_run_of_an_older_version_without_reps_uses_the_config_reps(self) -> None:
        info = self.started.to_dict()
        del info["reps"]
        (self.run_dir(self.started.run_id) / "run.json").write_text(json.dumps(info))
        beyond = api.plan(self.config, RunInfo.from_dict(info), only=["pi"], reps=1)[0]
        self.assertEqual(beyond.rep, 1)
        far = TrialSpec.from_dict(
            {
                **beyond.to_dict(),
                "rep": 99,
                "trial_id": make_trial_id(self.started.run_id, "fizzbuzz", "pi@unknown:p/nofix", 99),
            }
        )
        self.assert_refused(far, "beyond")

    def test_a_run_that_does_not_exist_is_refused(self) -> None:
        other = api.plan(self.config, RunInfo.from_dict({**self.started.to_dict(), "run_id": "9" * 32}))
        shutil.rmtree(self.run_dir("9" * 32), ignore_errors=True)
        self.assert_refused(other[0], "run " + "9" * 32)

    def test_a_run_of_another_config_is_refused(self) -> None:
        info = self.started.to_dict() | {"config_sha256": "1" * 64}
        (self.run_dir(self.started.run_id) / "run.json").write_text(json.dumps(info))
        self.assert_refused(self.spec, "was not started with this config")

    def test_malformed_specs_are_refused_before_they_are_a_spec(self) -> None:
        good = self.spec.to_dict()
        bad_texts = [
            "not json",
            "[]",
            json.dumps({**good, "run_id": "../../etc"}),
            json.dumps({**good, "run_id": "a" * 31 + "/"}),
            json.dumps({**good, "command": ["rm", "-rf", "/"]}),
            json.dumps({k: v for k, v in good.items() if k != "model"}),
            json.dumps({**good, "rep": 0}),
            json.dumps({**good, "schema_version": "2.0.0"}),
        ]
        for text in bad_texts:
            with self.subTest(text=text), self.assertRaises(api.SpecError):
                api.parse_spec(text)
        self.assertEqual(api.parse_spec(json.dumps(good)), self.spec)


class FinishRunTests(ApiTestCase):
    def test_finish_sets_the_finish_time_once_and_writes_the_report(self) -> None:
        run, _ = self.run_all()
        with fixtures.quiet():
            info = api.finish_run(self.config, run.run_id)
        self.assertIsNotNone(info.finished_at)
        self.assertEqual(read_run(self.run_dir(run.run_id))[0], info)
        self.assertTrue((self.base / "results" / "summary.md").exists())
        with fixtures.quiet():
            again = api.finish_run(self.config, run.run_id, report=False)
        self.assertEqual(again.finished_at, info.finished_at)

    def test_finish_restores_the_legacy_row_of_a_trial_that_lost_it(self) -> None:
        run, results = self.run_all()
        rows = (self.base / "results" / "results.jsonl").read_text().splitlines()
        (self.base / "results" / "results.jsonl").write_text(rows[0] + "\n")  # a worker killed between the two files
        api.finish_run(self.config, run.run_id, report=False)
        self.assertEqual(len(rows_of(self.base)), len(results))

    def test_finish_of_a_run_with_an_invalid_trials_file_is_a_run_error(self) -> None:
        run = api.start_run(self.config)
        (self.run_dir(run.run_id) / "trials.jsonl").write_text("garbage\n")
        with self.assertRaises(api.RunError):
            api.finish_run(self.config, run.run_id, report=False)

    def test_without_report_no_summary_is_written(self) -> None:
        run, _ = self.run_all()
        api.finish_run(self.config, run.run_id, report=False)
        self.assertFalse((self.base / "results" / "summary.md").exists())

    def test_a_report_without_results_is_a_run_error(self) -> None:
        run = api.start_run(self.config)
        with self.assertRaises(api.RunError) as cm, fixtures.quiet():
            api.finish_run(self.config, run.run_id)
        self.assertIn("no results", str(cm.exception))

    def test_a_run_that_does_not_exist_or_has_another_config_is_a_run_error(self) -> None:
        with self.assertRaises(api.RunError):
            api.finish_run(self.config, "e" * 32)
        with self.assertRaises(api.RunError):
            api.finish_run(self.config, "not-a-run")


class LockedAppendTests(unittest.TestCase):
    def test_threads_appending_long_lines_never_tear_them(self) -> None:
        path = fixtures.tmpdir(self) / "file.jsonl"
        path.touch()

        def worker(n: int) -> None:
            for i in range(15):
                locked_append(path, json.dumps({"n": n, "i": i, "pad": "x" * 200_000}) + "\n")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(sorted((r["n"], r["i"]) for r in rows), [(n, i) for n in range(4) for i in range(15)])

    def test_locked_append_drops_a_last_line_cut_short_by_a_crash(self) -> None:
        path = fixtures.tmpdir(self) / "file.jsonl"
        path.write_text('{"a": 1}\n{"cut')
        locked_append(path, '{"b": 2}\n')
        self.assertEqual(path.read_text(), '{"a": 1}\n{"b": 2}\n')
        only_cut = fixtures.tmpdir(self) / "cut.jsonl"
        only_cut.write_text("no newline at all")
        locked_append(only_cut, "x\n")
        self.assertEqual(only_cut.read_text(), "x\n")

    def test_a_cursor_reads_only_what_was_added_and_starts_over_when_the_file_changed(self) -> None:
        path = fixtures.tmpdir(self) / "file.jsonl"
        path.write_text("one\ntwo\n")
        cursor = LogCursor()
        with locked_log(path, cursor) as (_, text):
            self.assertEqual((text, cursor.fresh), ("one\ntwo\n", True))
            cursor.ids.add("seen")
        locked_append(path, "three\n")
        with locked_log(path, cursor) as (_, text):
            self.assertEqual((text, cursor.fresh, cursor.ids), ("three\n", False, {"seen"}))
        with path.open("a") as f:
            f.write("cut")  # a writer killed in the middle of a line
        with locked_log(path, cursor) as (_, text):
            self.assertEqual((text, path.read_text()), ("", "one\ntwo\nthree\n"))
        path.write_text("x\n")  # the file shrank
        with locked_log(path, cursor) as (_, text):
            self.assertEqual((text, cursor.fresh, cursor.ids), ("x\n", True, set()))
        replacement = path.with_name("other.jsonl")
        replacement.write_text("y\nz\n")
        replacement.replace(path)  # another file under the same name
        cursor.ids.add("stale")
        with locked_log(path, cursor) as (_, text):
            self.assertEqual((text, cursor.fresh, cursor.ids), ("y\nz\n", True, set()))

    def test_append_once_sees_what_another_writer_added_since_its_last_read(self) -> None:
        from test_core_models import run_info

        out = fixtures.tmpdir(self)
        first = RunWriter(out, run_info(run_id="a" * 32))
        second = RunWriter.open_existing(out, "a" * 32)
        a, b = trial(trial_id="1" * 16), trial(trial_id="2" * 16)
        self.assertIs(first.append_once(a), a)
        self.assertEqual(second.append_once(trial(trial_id="1" * 16, rep=2)), a)
        self.assertIs(first.append_once(b), b)
        self.assertEqual(second.append_once(trial(trial_id="2" * 16, rep=3)), b)  # found in what was added
        self.assertEqual([t.trial_id for t in first.trials()], ["1" * 16, "2" * 16])

    def test_the_otlp_sink_appends_under_the_same_lock(self) -> None:
        out = fixtures.tmpdir(self)
        sink = otlp.OtlpSink(out, None, {}, 1.0)
        from sink_helpers import make_info, make_trial

        sink.open(make_info())
        with mock.patch.object(otlp, "locked_log", wraps=locked_log) as spy:
            sink.write(make_trial())
        self.assertEqual([call.args[0] for call in spy.call_args_list], [out / make_info().run_id / TRACES_FILE])

    def test_two_sinks_on_one_run_write_a_span_once_and_repair_a_cut_line(self) -> None:
        from sink_helpers import make_info, make_trial

        out = fixtures.tmpdir(self)
        first, second = (otlp.OtlpSink(out, None, {}, 1.0) for _ in range(2))
        first.open(make_info())
        second.open(make_info())  # opened before the first one wrote anything
        first.write(make_trial())
        path = out / make_info().run_id / TRACES_FILE
        with path.open("a") as f:
            f.write('{"resourceSp')  # a worker killed in the middle of a line
        second.write(make_trial())  # the same span: not repeated
        second.write(make_trial(rep=2))
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            [json.loads(line) for line in lines][1]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"],
            make_trial(rep=2).trial_id,
        )

    def test_a_trial_lock_makes_a_second_holder_wait(self) -> None:
        run_dir = fixtures.tmpdir(self)
        order: list[str] = []
        entered, release = threading.Event(), threading.Event()

        def first() -> None:
            with trial_lock(run_dir, "a" * 16):
                order.append("first in")
                entered.set()
                release.wait(5)
                order.append("first out")

        def second() -> None:
            entered.wait(5)
            with trial_lock(run_dir, "a" * 16):
                order.append("second in")

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for t in threads:
            t.start()
        entered.wait(5)
        release.set()
        for t in threads:
            t.join()
        self.assertEqual(order, ["first in", "first out", "second in"])

    def test_the_sqlite_sink_waits_for_a_locked_database(self) -> None:
        from sink_helpers import make_info

        sink = sqlite.SqliteSink(fixtures.tmpdir(self) / "x.db")
        sink.open(make_info())
        db = sink._connection()
        self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], int(sqlite.BUSY_TIMEOUT_S * 1000))
        sink.close(make_info())


class AppendOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.out = fixtures.tmpdir(self)
        from test_core_models import run_info

        self.writer = RunWriter(self.out, run_info(run_id="a" * 32))

    def test_a_trial_is_written_once_and_the_stored_one_comes_back(self) -> None:
        first = trial(trial_id="1" * 16)
        self.assertIs(self.writer.append_once(first), first)
        again = trial(trial_id="1" * 16, rep=2)
        self.assertEqual(self.writer.append_once(again), first)
        self.assertEqual([t.trial_id for t in self.writer.trials()], ["1" * 16])

    def test_a_last_line_cut_short_by_a_crash_is_dropped_under_the_lock(self) -> None:
        self.writer.append_once(trial(trial_id="1" * 16))
        with self.writer.trials_path.open("a") as f:
            f.write('{"trial_id": "2222')
        self.writer.append_once(trial(trial_id="3" * 16))
        self.assertEqual([t.trial_id for t in self.writer.trials()], ["1" * 16, "3" * 16])

    def test_a_line_that_is_no_trial_never_matches(self) -> None:
        with self.writer.trials_path.open("a") as f:
            f.write("garbage\n[1]\n")
        stored = trial(trial_id="4" * 16)
        self.assertIs(self.writer.append_once(stored), stored)

    def test_open_existing_keeps_a_last_line_that_may_be_another_workers(self) -> None:
        self.writer.append_once(trial())
        with self.writer.trials_path.open("a") as f:
            f.write('{"half')
        RunWriter.open_existing(self.out, "a" * 32, repair=False)
        self.assertTrue(self.writer.trials_path.read_text().endswith('{"half'))
        RunWriter.open_existing(self.out, "a" * 32)
        self.assertTrue(self.writer.trials_path.read_text().endswith("}\n"))


class ConcurrentWorkersTests(ApiTestCase):
    def test_four_worker_processes_on_one_run_directory_record_every_trial_once(self) -> None:
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}, {"harness": "pi", "model": "p/fix"}]
        self.cfg["reps"] = 4
        self.config = self.write_config()
        run, specs = self.start()
        self.assertEqual(len(specs), 8)
        failures: list[str] = []

        def worker(mine: list[TrialSpec]) -> None:
            for spec in mine:
                p = subprocess.run(
                    [fixtures.PY, "-m", "hbench", "run-trial", "--config", str(self.config_path), "--spec", "-"]
                    + ["--allow-unsandboxed"],
                    input=json.dumps(spec.to_dict()),
                    capture_output=True,
                    text=True,
                    cwd=fixtures.ROOT,
                )
                if p.returncode != 0 or TrialResult.from_dict(json.loads(p.stdout)).trial_id != spec.trial_id:
                    failures.append(p.stderr)

        threads = [threading.Thread(target=worker, args=(specs[i::4],)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(failures, [])
        lines = (self.run_dir(run.run_id) / "trials.jsonl").read_text().splitlines()
        self.assertEqual(sorted(json.loads(line)["trial_id"] for line in lines), sorted(s.trial_id for s in specs))
        keys = [(r["task"], r["harness"], r["model"], r["rep"]) for r in rows_of(self.base)]
        self.assertEqual((len(keys), len(set(keys))), (8, 8))


class AcceptanceTests(RunTestCase):
    """`run`, the CLI commands, a script over the API and the Python adapter give the same trials (ADR 0012)."""

    def world(self) -> tuple[Path, Path]:
        """A new benchmark directory with the sample task and the config of this test."""
        base = fixtures.tmpdir(self)
        shutil.copytree(fixtures.FIZZBUZZ, base / "tasks" / "fizzbuzz")
        path = base / "config.json"
        path.write_text(json.dumps(self.cfg))
        return base, path

    def trials_of(self, base: Path) -> list[dict[str, Any]]:
        (path,) = (base / "results").glob("*/trials.jsonl")
        rows = [masked(json.loads(line)) for line in path.read_text().splitlines()]
        return sorted(rows, key=lambda r: (r["target"]["harness"], r["rep"]))

    def test_run_the_cli_commands_a_script_and_the_adapter_agree(self) -> None:
        self.cfg["reps"] = 2
        reference_base, _ = self.world()
        with fixtures.quiet():
            hb.cmd_run(args(), self.cfg, reference_base)
        reference = self.trials_of(reference_base)
        self.assertEqual(len(reference), 4)

        cli_base, cli_config = self.world()
        with fixtures.quiet() as out:
            cli.main(["plan", "--config", str(cli_config)])
        lines = out.getvalue().splitlines()
        for line in lines:
            with fixtures.quiet(), mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(line.encode()))):
                cli.main(["run-trial", "--config", str(cli_config), "--allow-unsandboxed", "--spec", "-"])
        run_id = json.loads(lines[0])["run_id"]
        with fixtures.quiet():
            cli.main(["finish", "--config", str(cli_config), "--run", run_id])
        self.assertEqual(self.trials_of(cli_base), reference)
        self.assertTrue((cli_base / "results" / "summary.md").exists())

        api_base, api_config = self.world()
        config = api.load_config(api_config)
        run = api.start_run(config)
        with fixtures.quiet():
            for spec in api.plan(config, run):
                api.run_trial(config, spec, allow_unsandboxed=True)
            api.finish_run(config, run.run_id, report=False)
        self.assertEqual(self.trials_of(api_base), reference)

        adapter_base, adapter_config = self.world()
        with fixtures.quiet():
            results = run_parallel.run_parallel(adapter_config, workers=2, allow_unsandboxed=True)
        self.assertEqual(self.trials_of(adapter_base), reference)
        self.assertEqual([r.target.harness for r in results], ["claude", "claude", "pi", "pi"])

    def test_the_adapter_runs_as_a_script_and_reports_a_bad_config(self) -> None:
        base, config = self.world()
        script = str(fixtures.ROOT / "adapters" / "python" / "run_parallel.py")
        env = {**os.environ, "PYTHONPATH": str(fixtures.ROOT)}
        p = subprocess.run(
            [fixtures.PY, script, "--config", str(config), "--workers", "2", "--only", "claude"]
            + ["--reps", "1", "--allow-unsandboxed"],
            capture_output=True,
            text=True,
            env=env,
            cwd=base,
        )
        self.assertEqual((p.returncode, p.stdout.splitlines()[-1]), (0, "1 of 1 trials passed"), p.stderr)
        p = subprocess.run(
            [fixtures.PY, script, "--config", str(base / "nope.json")],
            capture_output=True,
            text=True,
            env=env,
            cwd=base,
        )
        self.assertEqual(p.returncode, 1)
        self.assertIn("run_parallel: cannot read the config", p.stderr)

    def test_the_adapter_worker_function_runs_one_spec(self) -> None:
        base, config = self.world()
        loaded = api.load_config(config)
        run = api.start_run(loaded)
        (spec,) = api.plan(loaded, run, only=["claude"])
        with fixtures.quiet():
            text = run_parallel.run_one(str(config), json.dumps(spec.to_dict()), True)
        self.assertEqual(TrialResult.from_dict(json.loads(text)).trial_id, spec.trial_id)


if __name__ == "__main__":
    unittest.main()

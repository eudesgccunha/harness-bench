"""Sinks in a real run and `export` (ADR 0011, decisions 5 and 8): a failing sink never changes a result, the same
`trial_id` joins `trials.jsonl`, SQLite and the spans, and a replay adds nothing twice."""

import contextlib
import json
import os
import re
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any
from unittest import mock

import fake_collector
import fixtures
import harness_bench as hb
from hbench import cli
from hbench.core.models import TrialResult
from hbench.sinks.otlp import TRACES_FILE, run_span_id
from test_run import RunTestCase, args, rows_of
from test_sinks import Recording

VOLATILE = {"ts", "wall_s", "t_start", "t_end", "run_tag"}


class SinkRunTestCase(RunTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.probe = self.base / "probe.jsonl"
        self.cfg = fixtures.fake_config(
            {"FAKE_TAGS_FILE": str(self.base / "tags.txt"), "FAKE_PROBE_FILE": str(self.probe)}
        )
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}, {"harness": "pi", "model": "p/nofix"}]

    def start_collector(self, mode: str = "ok") -> tuple[fake_collector.Collector, str]:
        server, port, collector = fake_collector.serve()
        collector.mode = mode
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return collector, f"http://127.0.0.1:{port}"

    def run_dir(self, base: Path | None = None) -> Path:
        (path,) = ((base or self.base) / "results").glob("*/run.json")
        return path.parent

    def trials(self, base: Path | None = None) -> list[TrialResult]:
        lines = (self.run_dir(base) / "trials.jsonl").read_text().splitlines()
        return [TrialResult.from_dict(json.loads(line)) for line in lines]

    def sqlite_trial_ids(self, path: Path) -> list[str]:
        con = sqlite3.connect(path)
        with contextlib.closing(con):
            return [r[0] for r in con.execute("SELECT trial_id FROM trials ORDER BY trial_id")]


def outcome(base: Path) -> dict[str, Any]:
    """What a run produced, without its ids and clocks: results rows, trial statuses and graders, the report."""
    rows = [{k: v for k, v in r.items() if k not in VOLATILE} for r in rows_of(base)]
    (run_json,) = (base / "results").glob("*/trials.jsonl")
    trials = [json.loads(line) for line in run_json.read_text().splitlines()]
    shape = [
        (t["task_id"], t["status"], t["rep"], [g["passed"] for g in t["graders"]], t["usage"]["input_tokens"])
        for t in trials
    ]
    report = (base / "results" / "summary.md").read_text()
    report = re.sub(r"^(# Harness x model — )\d{4}-\d{2}-\d{2} \d{2}:\d{2}", r"\1<masked>", report)  # minute clock
    return {"rows": rows, "trials": shape, "report": report}


class WithoutSinksTests(SinkRunTestCase):
    def test_a_run_without_sinks_writes_no_sink_file(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        self.assertEqual([p.name for p in self.run_dir().iterdir()], ["run.json", "trials.jsonl"])

    def test_the_agent_gets_the_run_and_trial_ids_and_still_not_the_spend_key(self) -> None:
        self.cfg["litellm"] = {"base_url": "http://127.0.0.1:1", "api_key": "${HB_SPEND_KEY}"}
        with mock.patch.dict(os.environ, {"HB_SPEND_KEY": fixtures.fake_secret("spend")}):
            fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        (probe,) = [json.loads(line) for line in self.probe.read_text().splitlines()]
        (trial,) = self.trials()
        self.assertEqual((probe["bench_run_id"], probe["bench_trial_id"]), (trial.run_id, trial.trial_id))
        self.assertEqual([r["agent_rc"] for r in rows_of(self.base)], [0])  # the fake agent exits 9 on a leaked key


class BothSinksTests(SinkRunTestCase):
    """The acceptance: one `trial_id` in `trials.jsonl`, in SQLite and in the spans."""

    def configure(self, collector_url: str) -> None:
        self.cfg["sinks"] = [
            {"type": "sqlite", "path": "out/bench.sqlite"},
            {"type": "otlp", "endpoint": collector_url},
        ]

    def test_the_trial_id_joins_the_jsonl_the_database_and_the_spans(self) -> None:
        collector, url = self.start_collector()
        self.configure(url)
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        run_dir = self.run_dir()
        ids = sorted(t.trial_id for t in self.trials())
        self.assertEqual(len(ids), 2)
        self.assertEqual(self.sqlite_trial_ids(self.base / "out" / "bench.sqlite"), ids)
        spans = [
            json.loads(line)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            for line in (run_dir / TRACES_FILE).read_text().splitlines()
        ]
        run_id = self.trials()[0].run_id
        self.assertEqual(sorted(s["spanId"] for s in spans if s["spanId"] != run_span_id(run_id)), ids)
        self.assertEqual({s["traceId"] for s in spans}, {run_id})
        self.assertEqual(sorted(collector.span_ids()), sorted(ids + [run_span_id(run_id)]))
        con = sqlite3.connect(self.base / "out" / "bench.sqlite")
        with contextlib.closing(con):
            self.assertIsNotNone(con.execute("SELECT finished_at FROM runs").fetchone()[0])

    def test_the_run_tag_stays_in_trace_id_so_reconcile_still_matches(self) -> None:
        _, url = self.start_collector()
        self.configure(url)
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        (trial,) = self.trials()
        (row,) = rows_of(self.base)
        self.assertEqual(trial.trace_id, row["run_tag"])


class InterruptedRunTests(SinkRunTestCase):
    def test_the_sinks_are_closed_when_a_trial_raises(self) -> None:
        sink = Recording()
        with (
            mock.patch.object(hb, "make_sinks", return_value=[sink]),
            mock.patch.object(hb, "_run_trial", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        self.assertEqual(sink.calls, ["open", "close"])


class FailingSinkTests(SinkRunTestCase):
    """A sink that cannot work changes nothing: same results, same report, one warning."""

    def baseline(self) -> dict[str, Any]:
        base = fixtures.tmpdir(self)
        shutil.copytree(fixtures.FIZZBUZZ, base / "tasks" / "fizzbuzz")
        fixtures.call_quiet(hb.cmd_run, args(no_report=False), dict(self.cfg), base)
        return outcome(base)

    def run_with(self, sinks: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
        err = fixtures.tmpdir(self) / "stderr.txt"
        with err.open("w") as f, contextlib.redirect_stderr(f):
            fixtures.call_quiet(hb.cmd_run, args(no_report=False), dict(self.cfg, sinks=sinks), self.base)
        return outcome(self.base), err.read_text()

    def test_a_collector_that_answers_500_leaves_the_results_as_they_are(self) -> None:
        collector, url = self.start_collector("http500")
        got, warnings = self.run_with([{"type": "otlp", "endpoint": url}])
        self.assertEqual(got, self.baseline())
        self.assertEqual(warnings.count("warning: sink OtlpSink failed"), 1)
        self.assertEqual(len(collector.requests), 1)  # dropped after the first failure

    def test_a_redirect_is_not_followed_and_changes_nothing(self) -> None:
        elsewhere, elsewhere_url = self.start_collector()
        collector, url = self.start_collector("redirect")
        collector.redirect_url = elsewhere_url + "/v1/traces"
        got, warnings = self.run_with([{"type": "otlp", "endpoint": url}])
        self.assertEqual(got, self.baseline())
        self.assertEqual((warnings.count("warning: sink"), elsewhere.requests), (1, []))

    def test_a_collector_that_times_out_leaves_the_results_as_they_are(self) -> None:
        _, url = self.start_collector("slow")
        got, warnings = self.run_with([{"type": "otlp", "endpoint": url, "timeout": 0.2}])
        self.assertEqual(got, self.baseline())
        self.assertIn("took too long", warnings)

    def test_a_database_that_cannot_be_opened_is_one_warning(self) -> None:
        (self.base / "bench.sqlite").mkdir()  # a directory where the file should be
        got, warnings = self.run_with([{"type": "sqlite", "path": "bench.sqlite"}])
        self.assertEqual(got, self.baseline())
        self.assertEqual(warnings.count("warning: sink SqliteSink failed"), 1)

    def test_a_sink_that_fails_does_not_stop_the_sink_after_it(self) -> None:
        (self.base / "bad.sqlite").mkdir()
        got, warnings = self.run_with(
            [{"type": "sqlite", "path": "bad.sqlite"}, {"type": "sqlite", "path": "good.sqlite"}]
        )
        self.assertEqual(got, self.baseline())
        self.assertEqual(len(self.sqlite_trial_ids(self.base / "good.sqlite")), 2)
        self.assertEqual(warnings.count("warning: sink"), 1)

    def test_the_warning_redacts_a_header_secret(self) -> None:
        secret = fixtures.fake_secret("otlp")
        _, url = self.start_collector("http500")
        with mock.patch.dict(os.environ, {"HB_OTLP_KEY": secret}):
            _, warnings = self.run_with([{"type": "otlp", "endpoint": url, "headers": {"x-key": "${HB_OTLP_KEY}"}}])
        self.assertTrue(warnings and secret not in warnings)

    def test_a_header_secret_is_never_passed_to_the_agent(self) -> None:
        secret = fixtures.fake_secret("otlp")
        _, url = self.start_collector()
        self.cfg["env_passthrough"] = ["HB_OTLP_KEY"]
        self.cfg["sinks"] = [{"type": "otlp", "endpoint": url, "headers": {"x-key": "${HB_OTLP_KEY}"}}]
        with mock.patch.dict(os.environ, {"HB_OTLP_KEY": secret}), self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertIn("must not be passed to agents", str(cm.exception.code))

    def test_an_invalid_sink_entry_exits_before_any_agent_runs(self) -> None:
        self.cfg["sinks"] = [{"type": "sqlite"}]
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertIn("sinks: sinks[0]:", str(cm.exception.code))
        self.assertFalse((self.base / "results" / "results.jsonl").exists())


class ResumeAndExportTests(SinkRunTestCase):
    def test_resume_does_not_send_a_finished_trial_again(self) -> None:
        collector, url = self.start_collector()
        self.cfg["sinks"] = [{"type": "sqlite", "path": "bench.sqlite"}, {"type": "otlp", "endpoint": url}]
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        run_id = self.trials()[0].run_id
        before = len(collector.requests)
        fixtures.call_quiet(hb.cmd_run, args(resume=run_id), self.cfg, self.base)
        self.assertEqual(len(collector.requests) - before, 1)  # only the run span, again
        spans = (self.run_dir() / TRACES_FILE).read_text().splitlines()
        self.assertEqual(len(spans), 3)
        self.assertEqual(len(self.sqlite_trial_ids(self.base / "bench.sqlite")), 2)

    def export(self, *extra: str) -> str:
        argv = ["export", "--config", str(self.base / "config.json"), "--run", str(self.run_dir()), *extra]
        with fixtures.quiet() as out:
            cli.main(argv)
        return out.getvalue()

    def write_config(self, sinks: list[dict[str, Any]]) -> None:
        (self.base / "config.json").write_text(json.dumps(dict(self.cfg, sinks=sinks)))

    def test_export_replays_a_run_into_the_sinks_and_twice_adds_nothing(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)  # a run without sinks
        _, url = self.start_collector()
        self.write_config([{"type": "sqlite", "path": "late.sqlite"}, {"type": "otlp", "endpoint": url}])
        first = self.export()
        self.assertIn("exported 2 trial(s)", first)
        self.assertIn("to 2 of 2 sink(s)", first)
        self.export()
        ids = sorted(t.trial_id for t in self.trials())
        self.assertEqual(self.sqlite_trial_ids(self.base / "late.sqlite"), ids)
        spans = [
            json.loads(x)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"]
            for x in (self.run_dir() / TRACES_FILE).read_text().splitlines()
        ]
        self.assertEqual(sorted(spans), sorted([*ids, run_span_id(self.trials()[0].run_id)]))

    def test_export_of_an_interrupted_run_replays_what_it_has(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        run_dir = self.run_dir()
        lines = (run_dir / "trials.jsonl").read_text().splitlines()
        (run_dir / "trials.jsonl").write_text(lines[0] + "\n" + lines[1][:30])  # the second write was cut short
        self.write_config([{"type": "sqlite", "path": "late.sqlite"}])
        self.assertIn("exported 1 trial(s)", self.export())
        self.assertEqual(len(self.sqlite_trial_ids(self.base / "late.sqlite")), 1)

    def test_export_exits_1_when_a_sink_fails_and_warns_without_the_secret(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        secret = fixtures.fake_secret("otlp")
        _, url = self.start_collector("http500")
        self.write_config([{"type": "otlp", "endpoint": url, "headers": {"x-key": "${HB_OTLP_KEY}"}}])
        err = fixtures.tmpdir(self) / "stderr.txt"
        with mock.patch.dict(os.environ, {"HB_OTLP_KEY": secret}), err.open("w") as f, contextlib.redirect_stderr(f):
            with self.assertRaises(SystemExit) as cm:
                self.export()
        self.assertEqual(cm.exception.code, 1)
        self.assertTrue("warning: sink OtlpSink failed" in err.read_text() and secret not in err.read_text())

    def test_export_reports_an_unreadable_or_invalid_config_without_a_traceback(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        bad = self.base / "bad.json"
        bad.write_text("{not json")
        for config, text in ((self.base / "nowhere.json", "export: "), (bad, "export: ")):
            with self.subTest(config=config.name), self.assertRaises(SystemExit) as cm:
                cli.main(["export", "--config", str(config), "--run", str(self.run_dir())])
            self.assertTrue(str(cm.exception.code).startswith(text))
            self.assertIn(config.name, str(cm.exception.code))

    def test_export_refuses_what_it_cannot_replay(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.write_config([])
        with self.assertRaises(SystemExit) as cm:
            self.export()
        self.assertIn("the config has no sinks", str(cm.exception.code))
        self.write_config([{"type": "sqlite", "path": "late.sqlite"}])
        elsewhere = fixtures.tmpdir(self) / "not-the-run-id"
        shutil.copytree(self.run_dir(), elsewhere)
        for run, text in (
            (elsewhere, "is not the directory of run"),
            (self.base / "nowhere", "is not a run directory"),
        ):
            with self.subTest(run=run), self.assertRaises(SystemExit) as cm:
                cli.main(["export", "--config", str(self.base / "config.json"), "--run", str(run)])
            self.assertIn(text, str(cm.exception.code))
        (self.run_dir() / "run.json").write_text("{}")
        with self.assertRaises(SystemExit) as cm:
            self.export()
        self.assertIn("is not valid run data", str(cm.exception.code))

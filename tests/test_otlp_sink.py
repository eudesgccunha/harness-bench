"""The `otlp` sink: span ids, attributes, the trace file, the POST to a collector and its failure modes (ADR 0011)."""

import dataclasses
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

import fake_collector
import fixtures
from hbench import net
from hbench.core.ids import trial_id
from hbench.core.models import TrialStatus
from hbench.sinks import SinkError, sinks_for
from hbench.sinks.feed import SinkFeed
from hbench.sinks.otlp import TRACES_FILE, OtlpSink, run_span_id
from sink_helpers import RUN_ID, STARTED, make_info, make_trial, with_usage

TRIAL_KEYS = {
    "eval.run_id",
    "eval.trial_id",
    "eval.target_id",
    "eval.task_id",
    "eval.rep",
    "eval.status",
    "eval.passed",
    "eval.suite_version",
    "eval.sandbox",
    "gen_ai.request.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "eval.cost_usd",
}


def span_of(request: dict[str, Any]) -> dict[str, Any]:
    (resource,) = request["resourceSpans"]
    (scope,) = resource["scopeSpans"]
    (span,) = scope["spans"]
    assert isinstance(span, dict)
    return span


def attributes_of(span: dict[str, Any]) -> dict[str, Any]:
    return {a["key"]: a["value"] for a in span["attributes"]}


def file_requests(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


class OtlpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.out = fixtures.tmpdir(self)
        self.trace_file = self.out / RUN_ID / TRACES_FILE

    def sink(self, **section: Any) -> OtlpSink:
        (sink,) = sinks_for({"output_dir": str(self.out), "sinks": [{"type": "otlp", **section}]}, self.out)
        assert isinstance(sink, OtlpSink)
        return sink

    def start_collector(self, mode: str = "ok") -> tuple[fake_collector.Collector, str]:
        server, port, collector = fake_collector.serve()
        collector.mode = mode
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return collector, f"http://127.0.0.1:{port}"


class SpanTests(OtlpTestCase):
    def test_ids_follow_the_adr_and_a_trial_is_a_child_of_the_run_span(self) -> None:
        sink, trial = self.sink(), make_trial()
        sink.open(make_info())
        sink.write(trial)
        sink.close(make_info())
        trial_span, run_span = (span_of(r) for r in file_requests(self.trace_file))
        self.assertEqual(
            (trial_span["traceId"], trial_span["spanId"], trial_span["parentSpanId"]),
            (RUN_ID, trial.trial_id, run_span["spanId"]),
        )
        self.assertEqual(run_span["traceId"], RUN_ID)
        self.assertEqual(run_span["spanId"], run_span_id(RUN_ID))
        self.assertRegex(run_span["spanId"], r"^[0-9a-f]{16}$")
        self.assertNotIn("parentSpanId", run_span)
        self.assertEqual(trial.trial_id, trial_id(RUN_ID, "fizzbuzz", "claude/fix", 1))

    def test_the_attributes_are_exactly_the_documented_set_with_their_types(self) -> None:
        sink, trial = self.sink(), make_trial()
        sink.open(make_info())
        sink.write(trial)
        attributes = attributes_of(span_of(file_requests(self.trace_file)[0]))
        self.assertEqual(set(attributes), TRIAL_KEYS)
        self.assertEqual(
            attributes,
            {
                "eval.run_id": {"stringValue": RUN_ID},
                "eval.trial_id": {"stringValue": trial.trial_id},
                "eval.target_id": {"stringValue": "claude/fix"},
                "eval.task_id": {"stringValue": "fizzbuzz"},
                "eval.rep": {"intValue": "1"},
                "eval.status": {"stringValue": "passed"},
                "eval.passed": {"boolValue": True},
                "eval.suite_version": {"stringValue": "demo@1"},
                "eval.sandbox": {"stringValue": "local-tmp"},
                "gen_ai.request.model": {"stringValue": "fix"},
                "gen_ai.usage.input_tokens": {"intValue": "100"},
                "gen_ai.usage.output_tokens": {"intValue": "20"},
                "gen_ai.usage.cache_read.input_tokens": {"intValue": "40"},
                "eval.cost_usd": {"doubleValue": 0.5},
            },
        )

    def test_unknown_values_are_absent_never_zero(self) -> None:
        unknown = with_usage(make_trial(cost=None), parsed=False, input_tokens=0, output_tokens=0, cache_read_tokens=0)
        sink = self.sink()
        sink.open(make_info())
        sink.write(unknown)
        sink.write(with_usage(make_trial(rep=2), cache_read_tokens=0))
        first, second = (attributes_of(span_of(r)) for r in file_requests(self.trace_file))
        self.assertEqual(
            TRIAL_KEYS - set(first),
            {
                "gen_ai.usage.input_tokens",
                "gen_ai.usage.output_tokens",
                "gen_ai.usage.cache_read.input_tokens",
                "eval.cost_usd",
            },
        )
        self.assertEqual(TRIAL_KEYS - set(second), {"gen_ai.usage.cache_read.input_tokens"})

    def test_only_an_error_trial_has_the_error_status(self) -> None:
        sink = self.sink()
        sink.open(make_info())
        for rep, status in enumerate(TrialStatus, start=1):
            sink.write(make_trial(rep=rep, status=status))
        codes = {
            attributes_of(span)["eval.status"]["stringValue"]: span["status"]["code"]
            for span in (span_of(r) for r in file_requests(self.trace_file))
        }
        self.assertEqual(codes, {"passed": 0, "failed": 0, "error": 2, "timeout": 0})

    def test_times_come_from_started_at_and_latency(self) -> None:
        sink, trial = self.sink(), make_trial(rep=1, latency_s=12.5)
        sink.open(make_info())
        sink.write(trial)
        span = span_of(file_requests(self.trace_file)[0])
        self.assertEqual(int(span["startTimeUnixNano"]), int(trial.started_at.timestamp()) * 1_000_000_000)
        self.assertEqual(int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"]), 12_500_000_000)

    def test_the_run_span_runs_from_the_start_of_the_run_to_its_end(self) -> None:
        sink = self.sink()
        sink.open(make_info(finished=False))
        sink.write(make_trial(rep=2, latency_s=100.0))
        sink.close(make_info())
        sink.close(make_info())
        _, run_span = (span_of(r) for r in file_requests(self.trace_file))
        self.assertEqual(int(run_span["startTimeUnixNano"]), int(STARTED.timestamp()) * 1_000_000_000)
        end = make_info().finished_at
        assert end is not None
        self.assertEqual(int(run_span["endTimeUnixNano"]), int(end.timestamp()) * 1_000_000_000)
        self.assertEqual(len(file_requests(self.trace_file)), 2)  # the second close did not add a second run span

    def test_closing_an_unfinished_run_writes_no_run_span_and_a_later_finished_close_writes_it_once(self) -> None:
        collector, url = self.start_collector()
        sink = self.sink(endpoint=url)
        sink.open(make_info(finished=False))
        sink.write(make_trial())
        sink.close(make_info(finished=False))  # Ctrl-C, an error, or a dropped sink
        self.assertEqual(len(file_requests(self.trace_file)), 1)
        self.assertEqual(len(collector.requests), 1)
        later = self.sink(endpoint=url)  # resume or export
        later.open(make_info(finished=False))
        later.close(make_info())
        self.assertEqual(
            sorted(span_of(r)["spanId"] for r in file_requests(self.trace_file)),
            sorted([make_trial().trial_id, run_span_id(RUN_ID)]),
        )
        self.assertEqual(len(collector.requests), 2)

    def test_no_span_holds_the_agent_output_or_a_grader_log_line(self) -> None:
        canary_out, canary_log = "AGENT-STDOUT-CANARY", "GRADER-LOG-CANARY"
        trial = dataclasses.replace(make_trial(), artifact_uri=f"logs/{canary_out}")  # references only, never content
        sink = self.sink()
        sink.open(make_info())
        sink.write(trial)
        sink.close(make_info())
        text = self.trace_file.read_text()
        self.assertTrue(canary_out not in text and canary_log not in text and "artifact" not in text)
        span_keys = {"traceId", "spanId", "parentSpanId", "name", "kind", "startTimeUnixNano", "endTimeUnixNano"}
        for request in file_requests(self.trace_file):
            self.assertLessEqual(set(span_of(request)), span_keys | {"attributes", "status"})

    def test_the_file_has_one_request_per_line(self) -> None:
        sink = self.sink()
        sink.open(make_info())
        for rep in (1, 2, 3):
            sink.write(make_trial(rep=rep))
        sink.close(make_info())
        lines = self.trace_file.read_text().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(len(json.loads(line)["resourceSpans"]) == 1 for line in lines))


class ReplayTests(OtlpTestCase):
    def test_a_replay_leaves_each_span_id_once_in_the_file(self) -> None:
        trials = [make_trial(rep=rep) for rep in (1, 2)]
        for _ in range(2):
            sink = self.sink()
            sink.open(make_info())
            for trial in trials:
                sink.write(trial)
            sink.close(make_info())
        ids = [span_of(r)["spanId"] for r in file_requests(self.trace_file)]
        self.assertEqual(sorted(ids), sorted([t.trial_id for t in trials] + [run_span_id(RUN_ID)]))

    def test_a_resumed_run_adds_only_the_new_trials(self) -> None:
        first = self.sink()
        first.open(make_info(finished=False))
        first.write(make_trial(rep=1))  # the run was killed here: no close
        second = self.sink()
        second.open(make_info(finished=False))
        second.write(make_trial(rep=2))
        second.close(make_info())
        self.assertEqual(len(file_requests(self.trace_file)), 3)

    def test_a_last_line_cut_short_by_a_crash_is_not_a_span_and_the_next_line_starts_on_its_own(self) -> None:
        self.trace_file.parent.mkdir()
        self.trace_file.write_text('{"resourceSpans": [{"scopeSp')
        sink = self.sink()
        sink.open(make_info())
        sink.write(make_trial())
        lines = self.trace_file.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(span_of(json.loads(lines[1]))["spanId"], make_trial().trial_id)

    def test_lines_that_are_not_requests_hold_no_span_ids(self) -> None:
        self.trace_file.parent.mkdir()
        self.trace_file.write_text(
            "[1]\n{}\n" + json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [{"x": 1}]}]}]}) + "\n"
        )
        sink = self.sink()
        sink.open(make_info())
        sink.write(make_trial())
        self.assertEqual(len(self.trace_file.read_text().splitlines()), 4)

    def test_nested_entries_of_the_wrong_type_hold_no_span_ids(self) -> None:
        shapes: list[object] = [
            {"resourceSpans": [1]},
            {"resourceSpans": "abc"},
            {"resourceSpans": [{"scopeSpans": [2]}]},
            {"resourceSpans": [{"scopeSpans": "abc"}]},
            {"resourceSpans": [{"scopeSpans": [{"spans": [3]}]}]},
            {"resourceSpans": [{"scopeSpans": [{"spans": "abc"}]}]},
        ]
        self.trace_file.parent.mkdir()
        self.trace_file.write_text("".join(json.dumps(shape) + "\n" for shape in shapes))
        sink = self.sink()
        sink.open(make_info())
        sink.write(make_trial())
        self.assertEqual(len(self.trace_file.read_text().splitlines()), len(shapes) + 1)

    def test_write_before_open_is_an_error(self) -> None:
        with self.assertRaises(SinkError):
            self.sink().write(make_trial())

    def test_close_before_open_is_an_error(self) -> None:
        with self.assertRaises(SinkError):
            self.sink().close(make_info())


class CollectorTests(OtlpTestCase):
    def test_every_span_is_posted_to_v1_traces_with_the_json_content_type_and_the_headers(self) -> None:
        secret = fixtures.fake_secret("otlp")
        collector, url = self.start_collector()
        with mock.patch.dict(os.environ, {"HB_OTLP_KEY": secret}):
            sink = self.sink(endpoint=url + "/", headers={"x-key": "${HB_OTLP_KEY}"})
        trial = make_trial()
        sink.open(make_info())
        sink.write(trial)
        sink.close(make_info())
        self.assertEqual([r.path for r in collector.requests], ["/v1/traces"] * 2)
        self.assertEqual(collector.span_ids(), [trial.trial_id, run_span_id(RUN_ID)])
        first = collector.requests[0]
        self.assertEqual((first.headers["content-type"], first.headers["x-key"]), ("application/json", secret))
        self.assertNotIn(secret, self.trace_file.read_text())

    def test_a_replay_posts_again_but_the_file_keeps_each_span_once(self) -> None:
        collector, url = self.start_collector()
        for _ in range(2):
            sink = self.sink(endpoint=url)
            sink.open(make_info())
            sink.write(make_trial())
            sink.close(make_info())
        self.assertEqual(len(collector.span_ids()), 4)
        self.assertEqual(len(file_requests(self.trace_file)), 2)

    def test_a_collector_that_fails_raises_a_sink_error_with_no_header_in_it(self) -> None:
        secret = fixtures.fake_secret("otlp")
        collector, url = self.start_collector("http500")
        with mock.patch.dict(os.environ, {"HB_OTLP_KEY": secret}):
            sink = self.sink(endpoint=url, headers={"x-key": "${HB_OTLP_KEY}"})
        sink.open(make_info())
        with self.assertRaises(SinkError) as cm:
            sink.write(make_trial())
        self.assertEqual(str(cm.exception), "the collector answered HTTP 500")
        self.assertEqual(len(file_requests(self.trace_file)), 1)  # the file is written first
        self.assertEqual(len(collector.requests), 1)

    def test_a_redirect_is_not_followed(self) -> None:
        elsewhere, elsewhere_url = self.start_collector()
        collector, url = self.start_collector("redirect")
        collector.redirect_url = elsewhere_url + "/v1/traces"
        sink = self.sink(endpoint=url)
        sink.open(make_info())
        with self.assertRaises(SinkError) as cm:
            sink.write(make_trial())
        self.assertIn("HTTP 302", str(cm.exception))
        self.assertEqual(elsewhere.requests, [])

    def test_a_collector_that_answers_too_slowly_times_out(self) -> None:
        _, url = self.start_collector("slow")
        sink = self.sink(endpoint=url, timeout=0.3)
        sink.open(make_info())
        with self.assertRaises(SinkError) as cm:
            sink.write(make_trial())
        self.assertIn("took too long", str(cm.exception))

    def test_a_collector_that_drips_its_reply_hits_the_total_deadline(self) -> None:
        _, url = self.start_collector("drip")
        sink = self.sink(endpoint=url, timeout=0.06)
        sink.open(make_info())
        with self.assertRaises(SinkError) as cm:
            sink.write(make_trial())
        self.assertIn("took too long", str(cm.exception))

    def test_a_reply_above_the_cap_is_refused(self) -> None:
        _, url = self.start_collector("big")
        sink = self.sink(endpoint=url)
        sink.open(make_info())
        with self.assertRaises(SinkError) as cm:
            sink.write(make_trial())
        self.assertIn("too large", str(cm.exception))

    def test_a_connection_that_fails_names_only_the_error_type(self) -> None:
        sink = self.sink(endpoint="http://127.0.0.1:1")
        sink.open(make_info())
        with self.assertRaises(SinkError) as cm:
            sink.write(make_trial())
        self.assertEqual(str(cm.exception), "the collector exchange failed: URLError")

    def test_after_a_failed_post_close_writes_the_run_span_to_the_file_and_sends_nothing(self) -> None:
        collector, url = self.start_collector("http500")
        sink = self.sink(endpoint=url)
        sink.open(make_info())
        with self.assertRaises(SinkError):
            sink.write(make_trial())
        sink.close(make_info())
        self.assertEqual(len(collector.requests), 1)
        self.assertEqual(len(file_requests(self.trace_file)), 2)

    def test_the_feed_warns_once_and_the_warning_holds_no_secret(self) -> None:
        secret = fixtures.fake_secret("otlp")
        collector, url = self.start_collector("http500")
        with mock.patch.dict(os.environ, {"HB_OTLP_KEY": secret}):
            sink = self.sink(endpoint=url, headers={"x-key": "${HB_OTLP_KEY}"})
        warnings: list[str] = []
        feed = SinkFeed([sink], warnings.append)
        feed.open(make_info())
        feed.write(make_trial(rep=1))
        feed.write(make_trial(rep=2))
        feed.close(make_info())
        self.assertEqual((len(warnings), len(collector.requests), feed.failed), (1, 1, 1))
        self.assertNotIn(secret, warnings[0])
        self.assertEqual(len(file_requests(self.trace_file)), 2)  # trial + close run span


class NetTests(unittest.TestCase):
    def test_the_url_rule(self) -> None:
        self.assertEqual(
            [
                net.url_allowed(u)
                for u in ("https://a.example", "http://localhost:1", "http://[::1]:1", "http://a.example", "file:///x")
            ],
            [True, True, True, False, False],
        )

    def test_post_bounded_refuses_a_url_that_breaks_the_rule(self) -> None:
        with self.assertRaises(ValueError):
            net.post_bounded("http://a.example/v1/traces", {}, b"{}", 1)

    def test_a_reply_that_is_read_after_the_caller_gave_up_stops_the_read(self) -> None:
        server, port, collector = fake_collector.serve()
        collector.mode = "drip"
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/traces", data=b"{}", method="POST")
        abandoned = threading.Event()
        abandoned.set()
        with self.assertRaises(TimeoutError):
            net._exchange(request, 5, abandoned)

    def test_an_http_error_status_is_raised_as_it_is(self) -> None:
        server, port, collector = fake_collector.serve()
        collector.mode = "http500"
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with self.assertRaises(urllib.error.HTTPError):
            net.post_bounded(f"http://127.0.0.1:{port}/v1/traces", {}, b"{}", 5)

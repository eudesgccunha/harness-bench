"""The `sinks` config section, the registry, plugin sinks and `SinkFeed` (ADR 0011, decisions 2, 4 and 5)."""

import unittest
from pathlib import Path
from typing import Any

import fixtures
import harness_bench as hb
from hbench.core.models import RunInfo, TrialResult
from hbench.plugins import SINK_GROUP, PluginError
from hbench.sinks import Sink, SinkError, sink_secret_vars, sinks_for
from hbench.sinks.feed import SinkFeed
from hbench.sinks.otlp import OtlpSink
from hbench.sinks.sqlite import SqliteSink
from sink_helpers import make_info, make_trial
from test_plugins import installed

SINK_MODULE = """
class DemoSink:
    def __init__(self, cfg):
        self.cfg = cfg

    def open(self, info):
        pass

    def write(self, trial):
        pass

    def close(self, info):
        pass


def sink_factory(cfg):
    return DemoSink(cfg)


def not_a_sink(cfg):
    return object()
"""


def build(entries: object, base: Path | None = None, **extra: Any) -> list[Sink]:
    return sinks_for({"sinks": entries, **extra}, base or Path("/base"))


class RegistryTests(unittest.TestCase):
    def test_no_sinks_section_means_no_sinks(self) -> None:
        self.assertEqual(sinks_for({}, Path("/base")), [])

    def test_the_built_in_types_are_built_without_io_and_paths_are_relative_to_the_config(self) -> None:
        sqlite, otlp = build(
            [{"type": "sqlite", "path": "out/bench.sqlite"}, {"type": "otlp"}], output_dir="res", base=None
        )
        assert isinstance(sqlite, SqliteSink) and isinstance(otlp, OtlpSink)
        self.assertEqual(sqlite.path, Path("/base/out/bench.sqlite"))
        self.assertEqual((otlp.out_dir, otlp.endpoint), (Path("/base/res"), None))

    def test_a_comment_key_is_allowed(self) -> None:
        self.assertEqual(len(build([{"type": "sqlite", "path": "a.db", "_comment": "x"}])), 1)

    def test_sinks_must_be_a_list_of_objects_with_a_type(self) -> None:
        for bad, text in (
            ({"type": "sqlite"}, "must be a list"),
            (["sqlite"], "sinks[0]: must be an object"),
            ([{"path": "a"}], "type must be a non-empty string"),
            ([{"type": 3}], "type must be a non-empty string"),
        ):
            with self.subTest(bad=bad), self.assertRaises(SinkError) as cm:
                build(bad)
            self.assertIn(text, str(cm.exception))

    def test_an_unknown_key_names_the_sink_and_the_allowed_keys(self) -> None:
        with self.assertRaises(SinkError) as cm:
            build([{"type": "sqlite", "path": "a.db"}, {"type": "otlp", "endpont": "https://x"}])
        self.assertIn("sinks[1]: unknown keys ['endpont']", str(cm.exception))
        self.assertIn("'endpoint'", str(cm.exception))

    def test_a_sqlite_sink_needs_a_path(self) -> None:
        for entry in ({"type": "sqlite"}, {"type": "sqlite", "path": ""}, {"type": "sqlite", "path": 3}):
            with self.subTest(entry=entry), self.assertRaises(SinkError) as cm:
                build([entry])
            self.assertIn("needs a path", str(cm.exception))

    def test_an_unknown_type_is_a_plugin_error_naming_the_group(self) -> None:
        with self.assertRaises(PluginError) as cm:
            build([{"type": "postgres"}])
        message = str(cm.exception)
        self.assertTrue(message.startswith("sinks[0]: unknown sink type 'postgres'"), message)
        self.assertIn(SINK_GROUP, message)

    def test_a_plugin_sink_gets_its_entry_and_is_a_sink(self) -> None:
        ep = f"[{SINK_GROUP}]\ndemo = demo_plugin:sink_factory\n"
        with installed(self, ep, SINK_MODULE):
            with fixtures.quiet():
                (sink,) = build([{"type": "demo", "dsn": "x"}])
        self.assertIsInstance(sink, Sink)
        self.assertEqual(getattr(sink, "cfg", None), {"type": "demo", "dsn": "x"})

    def test_a_plugin_that_returns_something_else_is_refused(self) -> None:
        ep = f"[{SINK_GROUP}]\ndemo = demo_plugin:not_a_sink\n"
        with installed(self, ep, SINK_MODULE), self.assertRaises(PluginError) as cm:
            with fixtures.quiet():
                build([{"type": "demo"}])
        self.assertIn("not a Sink", str(cm.exception))

    def test_a_built_in_name_cannot_be_taken_by_a_plugin(self) -> None:
        ep = f"[{SINK_GROUP}]\nsqlite = demo_plugin:sink_factory\n"
        with installed(self, ep, SINK_MODULE), self.assertRaises(PluginError) as cm:
            build([{"type": "other"}])
        self.assertIn("built-in name", str(cm.exception))


class OtlpSectionTests(unittest.TestCase):
    def test_an_endpoint_over_plain_http_to_a_remote_host_is_refused(self) -> None:
        for endpoint in ("http://collector.example.com:4318", "ftp://x", 7):
            with self.subTest(endpoint=endpoint), self.assertRaises(SinkError) as cm:
                build([{"type": "otlp", "endpoint": endpoint}])
            self.assertIn("https (http only for localhost)", str(cm.exception))

    def test_https_and_localhost_endpoints_are_accepted(self) -> None:
        for endpoint in ("https://collector.example.com", "http://localhost:4318", "http://127.0.0.1:4318"):
            with self.subTest(endpoint=endpoint):
                (sink,) = build([{"type": "otlp", "endpoint": endpoint}])
                assert isinstance(sink, OtlpSink)
                self.assertEqual(sink.endpoint, endpoint)

    def test_a_header_value_must_be_exactly_one_env_reference(self) -> None:
        literal = fixtures.fake_secret("header")
        for value in (literal, f"Bearer ${{{'X_KEY'}}}", "${A}${B}", 5):
            with self.subTest(value=value), self.assertRaises(SinkError) as cm:
                build([{"type": "otlp", "endpoint": "https://c.example.com", "headers": {"x-key": value}}])
            self.assertIn("exactly ${VAR}", str(cm.exception))
            self.assertNotIn(literal, str(cm.exception))

    def test_a_header_name_must_not_be_empty_and_headers_must_be_an_object(self) -> None:
        for headers in ({"": "${X_HB_KEY}"}, ["x"]):
            with self.subTest(headers=headers), self.assertRaises(SinkError):
                build([{"type": "otlp", "endpoint": "https://c.example.com", "headers": headers}])

    def test_a_header_whose_variable_is_not_set_is_an_error_naming_the_variable(self) -> None:
        with self.assertRaises(SinkError) as cm:
            build([{"type": "otlp", "endpoint": "https://c.example.com", "headers": {"x-key": "${HB_NO_SUCH_VAR}"}}])
        self.assertIn("HB_NO_SUCH_VAR", str(cm.exception))

    def test_headers_without_an_endpoint_are_refused(self) -> None:
        with self.assertRaises(SinkError) as cm:
            build([{"type": "otlp", "headers": {"x-key": "${PATH}"}}])
        self.assertIn("need an endpoint", str(cm.exception))

    def test_the_timeout_must_be_a_positive_number(self) -> None:
        for timeout in (0, -1, "10", True):
            with self.subTest(timeout=timeout), self.assertRaises(SinkError) as cm:
                build([{"type": "otlp", "timeout": timeout}])
            self.assertIn("timeout", str(cm.exception))

    def test_the_header_variables_are_secrets_of_the_config(self) -> None:
        cfg = {
            "sinks": [
                {"type": "otlp", "headers": {"a": "${HB_ONE}", "b": "${HB_TWO}", "c": 3}},
                "x",
                {"type": "sqlite"},
            ]
        }
        self.assertEqual(sink_secret_vars(cfg), {"HB_ONE", "HB_TWO"})
        self.assertEqual(sink_secret_vars({"sinks": [{"type": "otlp", "headers": "x"}]}), set())
        self.assertEqual(sink_secret_vars({}), set())
        self.assertEqual(hb.spend_key_vars(cfg), {"HB_ONE", "HB_TWO"})


class Recording:
    """A sink that records its calls and can be told to fail on one of them."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name == self.fail_on:
            raise OSError("disk full")

    def open(self, info: RunInfo) -> None:
        self._call("open")

    def write(self, trial: TrialResult) -> None:
        self._call("write")

    def close(self, info: RunInfo) -> None:
        self._call("close")


class SinkFeedTests(unittest.TestCase):
    def test_every_sink_gets_the_run_and_each_trial(self) -> None:
        a, b = Recording(), Recording()
        feed = SinkFeed([a, b], lambda message: self.fail(message))
        feed.open(make_info())
        feed.write(make_trial())
        feed.close(make_info())
        self.assertEqual((a.calls, b.calls, feed.failed), (["open", "write", "close"],) * 2 + (0,))

    def test_a_sink_that_raises_is_reported_once_and_dropped_while_the_others_go_on(self) -> None:
        bad, good = Recording(fail_on="write"), Recording()
        warnings: list[str] = []
        feed = SinkFeed([bad, good], warnings.append)
        feed.open(make_info())
        feed.write(make_trial(rep=1))
        feed.write(make_trial(rep=2))
        feed.close(make_info())
        self.assertEqual(bad.calls, ["open", "write", "close"])  # dropped, then closed best effort
        self.assertEqual(good.calls, ["open", "write", "write", "close"])
        self.assertEqual(feed.failed, 1)
        (warning,) = warnings
        self.assertIn("Recording failed and is off for this run", warning)
        self.assertIn("OSError: disk full", warning)
        self.assertIn("python3 -m hbench export", warning)

    def test_a_sink_that_fails_to_open_is_dropped_too(self) -> None:
        warnings: list[str] = []
        sink = Recording(fail_on="open")
        feed = SinkFeed([sink], warnings.append)
        feed.open(make_info())
        feed.write(make_trial())
        feed.close(make_info())
        self.assertEqual((sink.calls, len(warnings)), (["open", "close"], 1))

    def test_a_dropped_sink_is_closed_best_effort(self) -> None:
        sink = Recording(fail_on="write")
        feed = SinkFeed([sink], lambda message: None)
        feed.open(make_info())
        feed.write(make_trial())
        self.assertEqual(sink.calls, ["open", "write", "close"])

    def test_a_dropped_sink_whose_close_also_fails_stays_quiet(self) -> None:
        class Broken(Recording):
            def _call(self, name: str) -> None:
                self.calls.append(name)
                raise OSError("gone")

        warnings: list[str] = []
        sink = Broken()
        feed = SinkFeed([sink], warnings.append)
        feed.open(make_info())
        self.assertEqual((sink.calls, len(warnings), feed.failed), (["open", "close"], 1, 1))

    def test_a_sink_dropped_before_the_run_is_opened_is_not_closed(self) -> None:
        sink = Recording(fail_on="write")
        feed = SinkFeed([sink], lambda message: None)
        feed.write(make_trial())
        self.assertEqual(sink.calls, ["write"])

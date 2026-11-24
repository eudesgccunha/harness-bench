"""Entry-point plugins: loading, error reporting and an end-to-end run with a plugin grader and runner."""

import contextlib
import importlib
import sys
import textwrap
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import TypeGuard
from unittest import mock

import fixtures
import harness_bench as hb
from hbench import plugins
from hbench.graders import Grader, grader_for
from hbench.plugins import GRADER_GROUP, RUNNER_GROUP, PluginError, create_plugin, load_plugin
from hbench.runners import runner_for
from test_run import RunTestCase, args, rows_of

DEMO_MODULE = """
from hbench.core.models import GraderResult


class DemoGrader:
    name = "demo"
    version = "9"

    def __init__(self, cfg):
        self.passes = cfg.get("passes", True)

    def grade(self, ctx):
        return GraderResult(grader=self.name, grader_version=self.version, passed=self.passes, score=0.5)


class LogNameGrader:
    name = "logname"
    version = "1"

    def __init__(self, cfg):
        pass

    def grade(self, ctx):
        rc, timed_out, stdout, stderr = ctx.run("true", 30, "../../escape/..//x")
        return GraderResult(grader=self.name, grader_version=self.version, passed=rc == 0, score=1.0)


class DemoRunner:
    name = "demo-runner"

    def __init__(self, cfg):
        self.cfg = cfg

    def prepare(self, workdir):
        (workdir / "prepared.txt").write_text("hello")

    def command(self, model, prompt):
        return [part.format(model=model, model_name=model, provider="", prompt=prompt) for part in self.cfg["command"]]

    def parse_usage(self, stdout):
        return None

    def version(self):
        return "7.7"


def grader_factory(cfg):
    return DemoGrader(cfg)


def logname_factory(cfg):
    return LogNameGrader(cfg)


def runner_factory(cfg):
    return DemoRunner(cfg)


def not_a_grader(cfg):
    return object()


def raising_factory(cfg):
    raise RuntimeError("bad config")
"""


@contextlib.contextmanager
def installed(
    test: unittest.TestCase, entry_points: str, module: str = DEMO_MODULE, dist: str = "demo_plugin"
) -> Iterator[Path]:
    """A throwaway distribution on sys.path: metadata with `entry_points` and a `demo_plugin` module."""
    root = fixtures.tmpdir(test)
    info = root / f"{dist}-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.1\n")
    (info / "entry_points.txt").write_text(textwrap.dedent(entry_points))
    (root / "demo_plugin.py").write_text(module)
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    plugins.reset_announced()
    try:
        yield root
    finally:
        sys.path.remove(str(root))
        sys.modules.pop("demo_plugin", None)
        importlib.invalidate_caches()


def is_grader(obj: object) -> TypeGuard[Grader]:
    return isinstance(obj, Grader)


GRADER_EP = f"[{GRADER_GROUP}]\ndemo = demo_plugin:grader_factory\n"


class LoadPluginTests(unittest.TestCase):
    def test_a_registered_factory_is_loaded_and_announced_once(self) -> None:
        with installed(self, GRADER_EP):
            with fixtures.quiet() as out:
                factory = load_plugin(GRADER_GROUP, "demo", ("command",))
                load_plugin(GRADER_GROUP, "demo", ("command",))
            self.assertTrue(callable(factory))
            self.assertEqual(out.getvalue(), f"plugin {GRADER_GROUP}:demo from demo_plugin 0.1\n")

    def test_an_unknown_type_names_the_type_the_group_and_the_built_ins(self) -> None:
        with self.assertRaises(PluginError) as cm:
            load_plugin(GRADER_GROUP, "nope", ("command", "hidden_tests"))
        message = str(cm.exception)
        self.assertTrue(
            all(part in message for part in ("grader", "'nope'", GRADER_GROUP, "command, hidden_tests")), message
        )

    def test_a_plugin_with_a_built_in_name_is_an_error(self) -> None:
        with installed(self, f"[{GRADER_GROUP}]\ncommand = demo_plugin:grader_factory\n"):
            with self.assertRaises(PluginError) as cm:
                load_plugin(GRADER_GROUP, "other", ("command",))
        self.assertIn("built-in name", str(cm.exception))
        self.assertIn("demo_plugin", str(cm.exception))

    def test_two_plugins_with_the_same_name_are_ambiguous(self) -> None:
        with installed(self, GRADER_EP), installed(self, GRADER_EP, dist="demo_other"):
            with self.assertRaises(PluginError) as cm:
                load_plugin(GRADER_GROUP, "demo", ())
        self.assertIn("more than one", str(cm.exception))

    def test_an_import_error_is_wrapped_with_the_plugin_name(self) -> None:
        with installed(self, GRADER_EP, module="raise RuntimeError('broken import')\n"):
            with self.assertRaises(PluginError) as cm:
                load_plugin(GRADER_GROUP, "demo", ())
        self.assertTrue(all(p in str(cm.exception) for p in (f"{GRADER_GROUP}:demo", "RuntimeError", "broken import")))

    def test_an_entry_point_that_is_not_callable_is_an_error(self) -> None:
        with installed(self, f"[{GRADER_GROUP}]\ndemo = demo_plugin:DEMO\n", module="DEMO = 3\n"):
            with self.assertRaises(PluginError) as cm:
                load_plugin(GRADER_GROUP, "demo", ())
        self.assertIn("not callable", str(cm.exception))

    def test_a_factory_that_raises_is_wrapped(self) -> None:
        with installed(self, f"[{GRADER_GROUP}]\ndemo = demo_plugin:raising_factory\n"):
            with self.assertRaises(PluginError) as cm, fixtures.quiet():
                grader_for({"type": "demo"})
        self.assertTrue(all(p in str(cm.exception) for p in ("demo", "RuntimeError", "bad config")))

    def test_a_result_of_the_wrong_type_is_an_error_naming_the_plugin(self) -> None:
        with installed(self, f"[{GRADER_GROUP}]\ndemo = demo_plugin:not_a_grader\n"):
            with self.assertRaises(PluginError) as cm, fixtures.quiet():
                grader_for({"type": "demo"})
        self.assertTrue(all(p in str(cm.exception) for p in (f"{GRADER_GROUP}:demo", "object", "Grader")))

    def test_create_plugin_checks_the_result_with_the_given_guard(self) -> None:
        with installed(self, GRADER_EP), fixtures.quiet():
            built = create_plugin(GRADER_GROUP, "demo", (), {}, is_grader, "Grader")
        self.assertEqual(built.name, "demo")


class RegistryFallbackTests(unittest.TestCase):
    def test_a_grader_type_that_is_not_built_in_comes_from_a_plugin(self) -> None:
        with installed(self, GRADER_EP), fixtures.quiet():
            grader = grader_for({"type": "demo", "passes": False})
        self.assertEqual((grader.name, grader.version), ("demo", "9"))

    def test_a_runner_parser_that_is_not_built_in_comes_from_a_plugin(self) -> None:
        with installed(self, f"[{RUNNER_GROUP}]\ndemo-runner = demo_plugin:runner_factory\n"), fixtures.quiet():
            runner = runner_for({"parser": "demo-runner", "command": ["x"]})
        self.assertEqual((runner.name, runner.version()), ("demo-runner", "7.7"))

    def test_unknown_runner_or_grader_gives_a_plugin_error_naming_the_group(self) -> None:
        with self.assertRaises(PluginError) as cm:
            runner_for({"parser": "nope", "command": ["x"]})
        self.assertIn(RUNNER_GROUP, str(cm.exception))

    def test_a_harness_without_a_parser_or_a_valid_command_is_rejected(self) -> None:
        for cfg in (
            {"command": ["x"]},
            {"parser": 3, "command": ["x"]},
            {"parser": "pi"},
            {"parser": "pi", "command": "x"},
        ):
            with self.assertRaises(ValueError, msg=str(cfg)):
                runner_for(cfg)


class EndToEndTests(RunTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}]

    def test_a_plugin_grader_grades_a_task_through_cmd_run_without_core_changes(self) -> None:
        self.cfg["tasks"][0]["graders"] = [{"type": "demo"}]
        with installed(self, GRADER_EP):
            out = fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (row,) = rows_of(self.base)
        trial = fixtures.read_rows(next((self.base / "results").glob("*/trials.jsonl")))[0]
        self.assertEqual(
            (row["passed"], trial["graders"][0]["grader"], trial["graders"][0]["score"]), (True, "demo", 0.5)
        )
        self.assertIn("plugin harness_bench.graders:demo from demo_plugin 0.1", out)

    def test_a_plugin_runner_builds_the_command_prepares_the_workdir_and_names_the_version(self) -> None:
        script = self.base / "agent.py"
        script.write_text("import os, sys\nsys.exit(0 if os.path.exists('prepared.txt') else 3)\n")
        self.cfg["harnesses"]["claude"] = {"parser": "demo-runner", "command": [fixtures.PY, str(script)]}
        self.cfg["tasks"][0]["graders"] = [
            {"type": "command", "command": "test -f prepared.txt"},
            {"type": "diff_constraints", "forbidden": ["prepared.txt"]},
        ]
        with installed(self, f"[{RUNNER_GROUP}]\ndemo-runner = demo_plugin:runner_factory\n"):
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (row,) = rows_of(self.base)
        run_json = next((self.base / "results").glob("*/run.json")).read_text()
        diff = next((self.base / "results" / "logs").glob("*.diff")).read_text()
        self.assertEqual(
            (row["passed"], row["agent_rc"], row["parse_ok"], '"harness_version": "7.7"' in run_json),
            (True, 0, False, True),
        )
        # `prepare` runs before the baseline, so its file is part of the starting state, not an agent change.
        self.assertNotIn("prepared.txt", diff)

    def test_a_plugin_grader_cannot_write_its_log_outside_the_logs_dir(self) -> None:
        self.cfg["tasks"][0]["graders"] = [{"type": "logname"}]
        with installed(self, f"[{GRADER_GROUP}]\nlogname = demo_plugin:logname_factory\n"):
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        logs = self.base / "results" / "logs"
        written = sorted(p.name for p in logs.glob("*grader-0-*"))
        outside = [p for p in self.base.rglob("*escape*") if not p.is_relative_to(logs)]
        self.assertEqual((len(written), "/" in written[0], outside), (1, False, []))

    def test_an_unknown_grader_type_exits_before_any_agent_runs(self) -> None:
        self.cfg["tasks"][0]["graders"] = [{"type": "nope"}]
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        message = str(cm.exception.code)
        self.assertTrue(all(p in message for p in ("task fizzbuzz", "'nope'", GRADER_GROUP)), message)
        self.assertFalse((self.base / "results" / "results.jsonl").exists())

    def test_an_unknown_runner_exits_before_any_agent_runs(self) -> None:
        self.cfg["harnesses"]["claude"]["parser"] = "nope"
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertTrue(all(p in str(cm.exception.code) for p in ("harness claude", "'nope'", RUNNER_GROUP)))
        self.assertFalse((self.base / "results" / "results.jsonl").exists())

    def test_a_plugin_that_fails_to_load_exits_with_a_message_not_a_traceback(self) -> None:
        self.cfg["tasks"][0]["graders"] = [{"type": "demo"}]
        with installed(self, GRADER_EP, module="raise RuntimeError('broken import')\n"):
            with self.assertRaises(SystemExit) as cm:
                fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertIn("broken import", str(cm.exception.code))

    def test_a_plain_config_never_looks_for_plugins(self) -> None:
        with mock.patch.object(plugins, "entry_points") as entry_points:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        entry_points.assert_not_called()


if __name__ == "__main__":
    unittest.main()

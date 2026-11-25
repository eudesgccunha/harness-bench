"""The sandbox protocol and its implementations (ADR 0010): registry, `prefix`, `local-tmp`, copies and lifecycle."""

import json
import os
import signal
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeGuard
from unittest import mock

import fixtures
import harness_bench as hb
from hbench.plugins import PluginError
from hbench.sandboxes import BUILTIN_TYPES, Sandbox, SandboxError, SandboxSession, sandbox_for
from test_plugins import installed
from test_run import RunTestCase, args, rows_of

ENV: dict[str, str] = {"PATH": os.environ.get("PATH", "")}


def never_run(argv: list[str], cwd: Path | None, env: Mapping[str, str], timeout: float) -> tuple[int, bool, str, str]:
    raise AssertionError(f"no process expected, got {argv}")


def build(cfg: Mapping[str, Any]) -> Sandbox:
    return sandbox_for(cfg, never_run, ENV)


def example_sandbox_section() -> dict[str, Any]:
    section: dict[str, Any] = json.loads((fixtures.ROOT / "config.example.json").read_text())["sandbox"]
    return section


class RegistryTests(unittest.TestCase):
    def test_without_a_sandbox_section_commands_run_on_the_host(self) -> None:
        sandbox = build({})
        self.assertEqual((sandbox.describe(), sandbox.isolated, sandbox.image_digest()), ("local-tmp", False, None))

    def test_the_type_defaults_to_prefix_when_a_prefix_is_set(self) -> None:
        sandbox = build({"sandbox": {"prefix": ["box"]}})
        self.assertEqual((sandbox.describe(), sandbox.isolated), ("prefix", True))

    def test_an_explicit_type_wins(self) -> None:
        self.assertEqual(build({"sandbox": {"type": "local-tmp"}}).describe(), "local-tmp")

    def test_builtin_types_are_listed(self) -> None:
        self.assertEqual(BUILTIN_TYPES, ("prefix", "docker", "local-tmp"))

    def test_the_section_must_be_an_object(self) -> None:
        with self.assertRaisesRegex(SandboxError, "sandbox must be an object"):
            build({"sandbox": ["docker"]})

    def test_the_type_must_be_a_string(self) -> None:
        with self.assertRaisesRegex(SandboxError, "sandbox.type must be a string"):
            build({"sandbox": {"type": 3}})

    def test_an_unknown_type_without_a_plugin_is_an_error_that_names_the_entry_point_group(self) -> None:
        with self.assertRaisesRegex(PluginError, "harness_bench.sandboxes"):
            build({"sandbox": {"type": "nope"}})

    def test_unknown_keys_are_a_config_error_for_a_builtin_type(self) -> None:
        with self.assertRaisesRegex(SandboxError, "unknown sandbox keys: \\['netwrk'\\]"):
            build({"sandbox": {"type": "local-tmp", "netwrk": "none"}})

    def test_a_comment_key_is_allowed(self) -> None:
        self.assertEqual(build({"sandbox": {"_comment": "why", "prefix": ["box"]}}).describe(), "prefix")


class PrefixTests(unittest.TestCase):
    def test_argv_of_the_example_config_is_what_the_old_wrap_built(self) -> None:
        section = example_sandbox_section()
        session = build({"sandbox": section}).create(Path("/w"), "trial")
        expected = [p.format(workdir="/w", env_file="/e") for p in section["prefix"]] + ["a", "b"]
        self.assertEqual(session.argv(["a", "b"], Path("/e")), expected)
        self.assertEqual(expected[:9], ["docker", "run", "--rm", "--env-file", "/e", "-v", "/w:/w", "-w", "/w"])
        self.assertEqual(expected[-3:], ["harness-bench-agents", "a", "b"])

    def test_prefix_is_formatted_and_prepended(self) -> None:
        cfg = {"sandbox": {"prefix": ["box", "--env-file", "{env_file}", "-v", "{workdir}:/w"]}}
        argv = build(cfg).create(Path("/w"), "t").argv(["a"], Path("/e"))
        self.assertEqual(argv, ["box", "--env-file", "/e", "-v", "/w:/w", "a"])

    def test_the_prefix_must_be_a_list_of_strings(self) -> None:
        for bad in ("docker run", [], ["ok", 3], None):
            with self.subTest(bad=bad), self.assertRaisesRegex(SandboxError, "sandbox.prefix"):
                build({"sandbox": {"type": "prefix", "prefix": bad}})

    def test_an_image_digest_is_unknown(self) -> None:
        self.assertIsNone(build({"sandbox": {"prefix": ["box"]}}).image_digest())

    def test_reclaim_and_destroy_do_nothing(self) -> None:
        sandbox = build({"sandbox": {"prefix": ["box"]}})
        sandbox.reclaim("t")
        sandbox.create(Path("/w"), "t").destroy()


class LocalTmpTests(unittest.TestCase):
    def test_argv_is_unchanged(self) -> None:
        session = build({}).create(Path("/w"), "t")
        self.assertEqual(session.argv(["a", "b"], Path("/e")), ["a", "b"])

    def test_the_run_is_refused_without_the_flag_and_accepted_with_it(self) -> None:
        for cfg in ({}, {"sandbox": {"type": "local-tmp"}}):
            with self.subTest(cfg=cfg):
                with self.assertRaises(SystemExit) as cm:
                    hb.require_sandbox(cfg, False)
                self.assertIn("No sandbox configured", str(cm.exception.code))
                hb.require_sandbox(cfg, True)

    def test_a_configured_sandbox_needs_no_flag(self) -> None:
        hb.require_sandbox({"sandbox": {"prefix": ["box"]}}, False)

    def test_is_sandboxed_follows_the_type(self) -> None:
        self.assertEqual(
            (hb.is_sandboxed({}), hb.is_sandboxed({"sandbox": {"prefix": ["box"]}})),
            (False, True),
        )

    def test_a_bad_section_exits_with_a_message(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            hb.is_sandboxed({"sandbox": {"type": "prefix"}})
        self.assertIn("sandbox.prefix", str(cm.exception.code))


class CopyTests(unittest.TestCase):
    def session(self, workdir: Path) -> SandboxSession:
        return build({}).create(workdir, "t")

    def test_copy_in_puts_a_file_and_a_tree_in_the_workdir(self) -> None:
        work, src = fixtures.tmpdir(self), fixtures.tmpdir(self)
        (src / "one.txt").write_text("1")
        (src / "tree").mkdir()
        (src / "tree" / "two.txt").write_text("2")
        session = self.session(work)
        session.copy_in(src / "one.txt", "a/b/one.txt")
        session.copy_in(src / "tree", "c")
        self.assertEqual(((work / "a/b/one.txt").read_text(), (work / "c/two.txt").read_text()), ("1", "2"))

    def test_copy_in_never_writes_through_a_link_the_agent_planted(self) -> None:
        work, src, host = fixtures.tmpdir(self), fixtures.tmpdir(self), fixtures.tmpdir(self)
        (host / "secret.txt").write_text("host")
        (work / "dir").symlink_to(host, target_is_directory=True)
        (work / "file.txt").symlink_to(host / "secret.txt")
        (src / "file.txt").write_text("new")
        session = self.session(work)
        session.copy_in(src / "file.txt", "dir/file.txt")
        session.copy_in(src / "file.txt", "file.txt")
        self.assertEqual(
            ((host / "secret.txt").read_text(), list(host.iterdir()), (work / "file.txt").read_text()),
            ("host", [host / "secret.txt"], "new"),
        )

    def test_a_path_that_leaves_the_workdir_is_refused(self) -> None:
        work, src = fixtures.tmpdir(self), fixtures.tmpdir(self)
        (src / "f").write_text("x")
        session = self.session(work)
        for rel in ("/abs", "../out", "a/../../out", ""):
            with self.subTest(rel=rel):
                with self.assertRaises(SandboxError):
                    session.copy_in(src / "f", rel)
                with self.assertRaises(SandboxError):
                    session.copy_out(rel, src / "dest")

    def test_copy_out_returns_a_file_and_a_tree(self) -> None:
        work, dest = fixtures.tmpdir(self), fixtures.tmpdir(self)
        (work / "out").mkdir()
        (work / "out" / "r.txt").write_text("r")
        (work / "out" / "sub").mkdir()
        (work / "out" / "sub" / "s.txt").write_text("s")
        session = self.session(work)
        session.copy_out("out/r.txt", dest / "one" / "r.txt")
        session.copy_out("out", dest / "tree")
        self.assertEqual(
            (
                (dest / "one/r.txt").read_text(),
                (dest / "tree/r.txt").read_text(),
                (dest / "tree/sub/s.txt").read_text(),
            ),
            ("r", "r", "s"),
        )

    def test_copy_out_never_follows_a_link_the_agent_planted(self) -> None:
        work, dest, host = fixtures.tmpdir(self), fixtures.tmpdir(self), fixtures.tmpdir(self)
        (host / "secret.txt").write_text("host")
        (work / "leak.txt").symlink_to(host / "secret.txt")
        (work / "via").symlink_to(host, target_is_directory=True)
        (work / "tree").mkdir()
        (work / "tree" / "leak").symlink_to(host / "secret.txt")
        (work / "tree" / "dirlink").symlink_to(host, target_is_directory=True)
        os.mkfifo(work / "pipe")
        session = self.session(work)
        for rel in ("leak.txt", "via/secret.txt", "tree", "pipe", "missing"):
            with self.subTest(rel=rel), self.assertRaises((ValueError, FileNotFoundError)):
                session.copy_out(rel, dest / "x")
        self.assertEqual([p for p in dest.rglob("*") if p.is_file() and p.read_text() == "host"], [])

    def test_copy_out_of_a_tree_with_a_special_file_leaves_no_such_file(self) -> None:
        work, dest = fixtures.tmpdir(self), fixtures.tmpdir(self)
        (work / "tree").mkdir()
        os.mkfifo(work / "tree" / "pipe")
        with self.assertRaises(ValueError):
            self.session(work).copy_out("tree", dest / "x")


class RecordingSandbox:
    """A plugin-style sandbox that runs commands unchanged and records its lifecycle."""

    isolated = True

    def __init__(self) -> None:
        self.events: list[str] = []
        self.steps = 0  # `end_step` calls so far
        self.failing_steps: set[int] = set()  # the numbers of the calls that raise
        self.failing_destroy = False

    def create(self, workdir: Path, trial_id: str) -> SandboxSession:
        self.events.append(f"create {trial_id}")
        return RecordingSession(self)

    def reclaim(self, trial_id: str) -> None:
        self.events.append(f"reclaim {trial_id}")

    def image_digest(self) -> str | None:
        return "sha256:" + "a" * 64

    def describe(self) -> str:
        return "recording"


class RecordingSession:
    def __init__(self, owner: RecordingSandbox) -> None:
        self.owner = owner
        self.events = owner.events

    def argv(self, argv: list[str], env_file: Path) -> list[str]:
        self.events.append("argv " + " ".join(argv))
        return argv

    def copy_in(self, src: Path, rel: str) -> None:
        self.events.append(f"copy_in {rel}")

    def copy_out(self, rel: str, dest: Path) -> None:
        self.events.append(f"copy_out {rel}")

    def end_step(self) -> None:
        self.events.append("end_step")
        self.owner.steps += 1
        if self.owner.steps in self.owner.failing_steps:
            raise SandboxError("container of the step could not be removed")

    def destroy(self) -> None:
        self.events.append("destroy")
        if self.owner.failing_destroy:
            raise SandboxError("container could not be removed")


def _is_sandbox(obj: object) -> TypeGuard[Sandbox]:
    return isinstance(obj, Sandbox)


PLUGIN_MODULE = """
from pathlib import Path


class Plugin:
    isolated = True

    def __init__(self, cfg):
        self.cfg = cfg

    def create(self, workdir, trial_id):
        raise NotImplementedError

    def reclaim(self, trial_id):
        pass

    def image_digest(self):
        return None

    def describe(self):
        return "plugin:" + self.cfg["label"]


def factory(cfg):
    return Plugin(cfg)


def not_a_sandbox(cfg):
    return object()
"""


class PluginTests(unittest.TestCase):
    def test_a_plugin_sandbox_is_built_from_its_section(self) -> None:
        points = "[harness_bench.sandboxes]\nvm = demo_plugin:factory\n"
        with installed(self, points, PLUGIN_MODULE), fixtures.quiet() as buf:
            sandbox = build({"sandbox": {"type": "vm", "label": "x"}})
        self.assertEqual((sandbox.describe(), sandbox.isolated), ("plugin:x", True))
        self.assertIn("plugin harness_bench.sandboxes:vm from demo_plugin 0.1", buf.getvalue())

    def test_a_plugin_that_returns_something_else_is_an_error(self) -> None:
        points = "[harness_bench.sandboxes]\nvm = demo_plugin:not_a_sandbox\n"
        with (
            installed(self, points, PLUGIN_MODULE),
            fixtures.quiet(),
            self.assertRaisesRegex(PluginError, "not a Sandbox"),
        ):
            build({"sandbox": {"type": "vm"}})

    def test_a_plugin_cannot_take_a_builtin_name(self) -> None:
        points = "[harness_bench.sandboxes]\nprefix = demo_plugin:factory\n"
        with installed(self, points, PLUGIN_MODULE), self.assertRaisesRegex(PluginError, "built-in name"):
            build({"sandbox": {"type": "vm"}})

    def test_the_recording_sandbox_satisfies_the_protocol(self) -> None:
        self.assertTrue(_is_sandbox(RecordingSandbox()))


class LifecycleTests(RunTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sandbox = RecordingSandbox()
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}]
        patcher = mock.patch.object(hb, "make_sandbox", return_value=self.sandbox)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_trial_gets_a_session_that_is_destroyed_after_its_commands(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        events = self.sandbox.events
        self.assertEqual(
            (len([e for e in events if e.startswith("create ")]), events.count("destroy"), events[-1]),
            (1, 1, "destroy"),
        )
        self.assertTrue(any(e.startswith("argv ") and "fake_agent.py" in e for e in events))
        self.assertTrue(any(e.startswith("argv sh -c") for e in events))  # the hidden tests ran through it too
        (row,) = rows_of(self.base)
        self.assertTrue(row["sandboxed"])

    def test_the_run_and_the_trial_record_what_the_sandbox_describes(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (run_dir,) = [p.parent for p in (self.base / "results").glob("*/trials.jsonl")]
        info = json.loads((run_dir / "run.json").read_text())
        (trial,) = fixtures.read_rows(run_dir / "trials.jsonl")
        self.assertEqual((info["sandbox"], trial["image_digest"]), ("recording", "sha256:" + "a" * 64))

    def test_every_command_ends_its_step_before_the_engine_looks_at_the_workdir_or_runs_the_next_one(self) -> None:
        events = self.sandbox.events
        real_collect = hb.collect_changes

        def collect(baseline: Any) -> Any:
            events.append("collect_changes")
            return real_collect(baseline)

        with mock.patch.object(hb, "collect_changes", collect):
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        commands = [i for i, e in enumerate(events) if e.startswith("argv ")]
        self.assertEqual(len(commands), 2)  # the agent, then the hidden tests
        for index in commands:
            self.assertEqual(events[index + 1], "end_step")
        self.assertEqual(events[commands[0] + 2], "collect_changes")

    def test_the_step_ends_when_the_agent_times_out_or_the_command_raises(self) -> None:
        self.cfg["tasks"][0]["timeout"] = 0.2
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, "-c", "import time; time.sleep(30)"]
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        events = self.sandbox.events
        first = next(i for i, e in enumerate(events) if e.startswith("argv "))
        self.assertEqual(events[first + 1], "end_step")
        self.cfg["output_dir"] = "results-again"
        with mock.patch.object(hb, "run_proc", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertEqual(events[-2:], ["end_step", "destroy"])

    def ran_graders(self) -> bool:
        return any(e.startswith("argv sh -c") for e in self.sandbox.events)

    def test_an_agent_container_that_cannot_be_removed_fails_the_trial_closed_without_grading(self) -> None:
        self.sandbox.failing_steps = {1}
        out = fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (row,) = rows_of(self.base)
        self.assertEqual((row["passed"], self.ran_graders(), self.sandbox.events[-1]), (False, False, "destroy"))
        self.assertIn("container of the step could not be removed", out)

    def test_a_grader_container_that_cannot_be_removed_fails_the_trial_although_the_tests_passed(self) -> None:
        self.sandbox.failing_steps = {2}
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (row,) = rows_of(self.base)
        self.assertEqual((row["passed"], self.ran_graders()), (False, True))

    def test_a_session_that_cannot_be_destroyed_is_a_warning_after_the_verdict(self) -> None:
        self.sandbox.failing_destroy = True
        out = fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (row,) = rows_of(self.base)
        self.assertEqual(row["passed"], True)
        self.assertIn("warning: could not remove the sandbox of", out)

    def test_run_step_without_a_place_to_record_the_loss_raises(self) -> None:
        self.sandbox.failing_steps = {1}
        work = fixtures.tmpdir(self)
        session = self.sandbox.create(work, "t")
        with self.assertRaises(SandboxError):
            hb.run_step(session, "true", dict(os.environ), 30, work / "log", work, work / "env", [])

    def test_the_session_is_destroyed_when_the_agent_step_raises(self) -> None:
        with mock.patch.object(hb, "run_proc", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertEqual(self.sandbox.events.count("destroy"), 1)


class LimitsTests(unittest.TestCase):
    def test_memory_converts_to_bytes_and_unset_limits_stay_none(self) -> None:
        from hbench.sandboxes.limits import Limits, parse_limits

        keys = frozenset({"memory", "pids"})
        self.assertEqual(
            (
                parse_limits({"memory": "512k"}, keys).memory_bytes,
                parse_limits({"memory": "3m"}, keys).memory_bytes,
                parse_limits({"memory": "2g"}, keys).memory_bytes,
                parse_limits(None, keys),
            ),
            (512 * 1024, 3 * 1024**2, 2 * 1024**3, Limits()),
        )


def local_argv(limits: Mapping[str, Any], argv: list[str]) -> list[str]:
    return build({"sandbox": {"type": "local-tmp", "limits": limits}}).create(Path("/w"), "t").argv(argv, Path("/e"))


def run_limited(limits: Mapping[str, Any], code: str, timeout: float = 60) -> tuple[int, bool, str, str]:
    return hb.run_proc(local_argv(limits, [fixtures.PY, "-c", code]), None, dict(os.environ), timeout)


class LocalLimitsTests(unittest.TestCase):
    def test_a_limit_that_local_tmp_cannot_apply_leaves_the_argv_alone(self) -> None:
        self.assertEqual(local_argv({"cpus": 2}, ["a", "b"]), ["a", "b"])
        self.assertEqual(local_argv({}, ["a", "b"]), ["a", "b"])

    def test_the_limits_become_rlimits_in_a_launcher_that_then_runs_the_command(self) -> None:
        argv = local_argv({"memory": "2g", "pids": 64, "cpu_seconds": 30, "cpus": 1}, ["a", "b"])
        self.assertEqual((argv[0], argv[1], argv[2]), (fixtures.PY, "-I", "-c"))
        self.assertEqual(
            (json.loads(argv[4]), argv[5:]),
            ({"RLIMIT_AS": 2 * 1024**3, "RLIMIT_NPROC": 64, "RLIMIT_CPU": 30}, ["a", "b"]),
        )

    def test_unsupported_limit_keys_and_bad_values_are_config_errors(self) -> None:
        for limits in ({"gpus": 1}, {"cpu_seconds": 0}, {"memory": "9"}):
            with self.subTest(limits=limits), self.assertRaises(SandboxError):
                build({"sandbox": {"type": "local-tmp", "limits": limits}})

    def test_the_memory_limit_stops_a_process_that_allocates_more(self) -> None:
        rc, _, out, err = run_limited({"memory": "512m"}, "b = bytearray(2 * 1024**3); print('allocated')")
        self.assertEqual((rc == 0, "allocated" in out, "MemoryError" in err), (False, False, True))

    def test_a_process_inside_the_limit_runs_normally(self) -> None:
        self.assertEqual(run_limited({"memory": "2g", "cpu_seconds": 60}, "print('fine')")[:3:2], (0, "fine\n"))

    def test_the_cpu_limit_kills_a_busy_loop(self) -> None:
        rc, timed_out, _, _ = run_limited({"cpu_seconds": 1}, "while True: pass")
        self.assertEqual((rc, timed_out), (-int(signal.SIGKILL), False))  # soft = hard: the kernel kills

    @unittest.skipIf(os.geteuid() == 0, "root is not bound by RLIMIT_NPROC")
    def test_the_process_limit_stops_a_fork(self) -> None:
        code = "import os\ntry:\n    os.fork()\nexcept OSError as e:\n    print('refused', e.errno)\n"
        rc, _, out, _ = run_limited({"pids": 1}, code)
        self.assertEqual((rc, out.startswith("refused")), (0, True))


class RunRecordsSandboxTests(RunTestCase):
    def recorded(self) -> tuple[str, str | None]:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (run_dir,) = [p.parent for p in (self.base / "results").glob("*/trials.jsonl")]
        info = json.loads((run_dir / "run.json").read_text())
        trials = fixtures.read_rows(run_dir / "trials.jsonl")
        return info["sandbox"], trials[0]["image_digest"]

    def test_without_a_sandbox_the_run_is_local_tmp_and_there_is_no_digest(self) -> None:
        self.assertEqual(self.recorded(), ("local-tmp", None))

    def test_a_prefix_sandbox_is_named_prefix_and_has_no_digest(self) -> None:
        self.cfg["sandbox"] = {"prefix": ["env", "X=1"]}
        self.assertEqual(self.recorded(), ("prefix", None))

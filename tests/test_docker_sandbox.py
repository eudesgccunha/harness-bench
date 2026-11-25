"""The `docker` sandbox (ADR 0010): config checks, the exact `docker run` argv, cleanup, and a run through a fake
`docker` executable. No Docker is needed."""

import json
import os
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from unittest import mock

import fake_docker
import fixtures
import harness_bench as hb
from hbench.sandboxes import Sandbox, SandboxError, SandboxSession, sandbox_for
from test_run import RunTestCase, args, rows_of

DIGEST = "a" * 64
IMAGE = f"harness-bench-agents@sha256:{DIGEST}"
LIST_T1_0 = ["docker", "ps", "-aq", "--filter", "name=^hb-t1-0$"]  # the check that a removed container is gone
LIST_T1_1 = ["docker", "ps", "-aq", "--filter", "name=^hb-t1-1$"]
GONE = (1, False, "", "Error response from daemon: No such container: hb-t1-0")
Call = tuple[list[str], Path | None, Mapping[str, str], float]


class Recorder:
    """A `run_proc` stand-in: records every call and answers with `result`."""

    def __init__(
        self,
        result: tuple[int, bool, str, str] = (0, False, "", ""),
        script: list[tuple[int, bool, str, str]] | None = None,
    ) -> None:
        self.calls: list[Call] = []
        self.result = result
        self.script = list(script or [])  # answers for the first calls; `result` answers the rest

    def __call__(
        self, argv: list[str], cwd: Path | None, env: Mapping[str, str], timeout: float
    ) -> tuple[int, bool, str, str]:
        self.calls.append((argv, cwd, env, timeout))
        return self.script.pop(0) if self.script else self.result


def docker_cfg(**section: Any) -> dict[str, Any]:
    return {"sandbox": {"type": "docker", "image": IMAGE, **section}}


def build(cfg: Mapping[str, Any], run: Callable[..., Any] | None = None) -> Sandbox:
    return sandbox_for(cfg, run or Recorder(), {"PATH": "/bin"})


def argv_of(sandbox: Sandbox, trial: str = "t1") -> list[str]:
    return sandbox.create(Path("/w"), trial).argv(["agent", "--go"], Path("/e"))


class ConfigTests(unittest.TestCase):
    def test_a_digest_pinned_image_is_accepted(self) -> None:
        sandbox = build(docker_cfg())
        self.assertEqual(
            (sandbox.describe(), sandbox.isolated, sandbox.image_digest()),
            (f"docker:{IMAGE}", True, f"sha256:{DIGEST}"),
        )

    def test_a_tag_is_a_config_error(self) -> None:
        for image in ("img:latest", "img", "registry.example.com/team/img:1.2"):
            with self.subTest(image=image), self.assertRaisesRegex(SandboxError, "pinned by digest"):
                build(docker_cfg(image=image))

    def test_a_malformed_digest_is_a_config_error(self) -> None:
        for image in (
            "img@sha256:abc",
            f"img@sha256:{'A' * 64}",
            f"img@sha256:{'a' * 65}",
            f"img@sha512:{'a' * 64}",
            f"img@sha256:{'g' * 64}",
            f"-img@sha256:{DIGEST}",
            f"img @sha256:{DIGEST}",
            f"@sha256:{DIGEST}",
        ):
            with self.subTest(image=image), self.assertRaisesRegex(SandboxError, "pinned by digest"):
                build(docker_cfg(image=image))

    def test_the_image_is_required_and_a_string(self) -> None:
        for section in ({"type": "docker"}, {"type": "docker", "image": 3}):
            with self.subTest(section=section), self.assertRaisesRegex(SandboxError, "sandbox.image"):
                build({"sandbox": section})

    def test_a_registry_with_a_port_is_a_valid_image_name(self) -> None:
        image = f"localhost:5000/team/agents@sha256:{DIGEST}"
        self.assertEqual(build(docker_cfg(image=image)).describe(), f"docker:{image}")

    def test_the_network_is_checked(self) -> None:
        for network in (3, "", "host", "container:x", "-x", "a b"):
            with self.subTest(network=network), self.assertRaisesRegex(SandboxError, "sandbox.network"):
                build(docker_cfg(network=network))

    def test_bad_limits_are_a_config_error(self) -> None:
        bad: list[tuple[Any, str]] = [
            ({"cpus": 0}, "limits.cpus"),
            ({"cpus": -1}, "limits.cpus"),
            ({"cpus": True}, "limits.cpus"),
            ({"cpus": "2"}, "limits.cpus"),
            ({"cpus": float("inf")}, "limits.cpus"),
            ({"cpus": float("nan")}, "limits.cpus"),
            ({"memory": "2"}, "limits.memory"),
            ({"memory": "2gb"}, "limits.memory"),
            ({"memory": "0m"}, "limits.memory"),
            ({"memory": "-1g"}, "limits.memory"),
            ({"memory": 2048}, "limits.memory"),
            ({"memory": "2 g"}, "limits.memory"),
            ({"pids": 0}, "limits.pids"),
            ({"pids": 1.5}, "limits.pids"),
            ({"pids": True}, "limits.pids"),
            ({"pids": "9"}, "limits.pids"),
            ({"cpu_seconds": 5}, "limits"),
            ({"gpus": 1}, "limits"),
            ([1], "limits must be an object"),
        ]
        for limits, message in bad:
            with self.subTest(limits=limits), self.assertRaisesRegex(SandboxError, message):
                build(docker_cfg(limits=limits))

    def test_mounts_are_checked(self) -> None:
        for mounts in (
            "/a:/b",
            ["a:/b"],
            ["/a:b"],
            ["/a:/b:rw"],
            ["/a:/b:ro:x"],
            [3],
            ["/a b:/c"],
            ["/a:/work"],
        ):  # the last: the workdir itself
            with self.subTest(mounts=mounts), self.assertRaisesRegex(SandboxError, "sandbox.mounts"):
                build(docker_cfg(mounts=mounts))

    def test_nothing_may_be_mounted_at_or_under_the_workdir_however_the_path_is_written(self) -> None:
        for target in ("/work/", "/work/sub", "/x/../work", "//work", "/work/./a", "/a/../work/b"):
            with self.subTest(target=target), self.assertRaisesRegex(SandboxError, "sandbox.mounts"):
                build(docker_cfg(mounts=[f"/h:{target}"]))

    def test_a_mount_target_is_written_normalized(self) -> None:
        argv = argv_of(build(docker_cfg(mounts=["/h/a:/x/../y/./z:ro", "/h/b://w"])))
        self.assertEqual(argv[7:13], ["-v", "/w:/work", "-v", "/h/a:/y/z:ro", "-v", "/h/b:/w"])

    def test_unknown_keys_and_a_prefix_are_refused(self) -> None:
        for extra in ({"netwrk": "none"}, {"prefix": ["x"]}):
            with self.subTest(extra=extra), self.assertRaisesRegex(SandboxError, "unknown sandbox keys"):
                build(docker_cfg(**extra))

    def test_a_trial_id_that_is_not_a_container_name_is_refused(self) -> None:
        sandbox = build(docker_cfg())
        for trial in ("", "-x", "a b", "../x", ".x"):
            with self.subTest(trial=trial), self.assertRaisesRegex(SandboxError, "trial id"):
                sandbox.create(Path("/w"), trial)


class ArgvTests(unittest.TestCase):
    def test_default_argv_has_no_network_no_capabilities_and_the_host_user(self) -> None:
        self.assertEqual(
            argv_of(build(docker_cfg())),
            [
                "docker",
                "run",
                "--rm",
                "--name",
                "hb-t1-0",
                "--env-file",
                "/e",
                "-v",
                "/w:/work",
                "-w",
                "/work",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                IMAGE,
                "agent",
                "--go",
            ],
        )

    def test_full_limits_and_a_named_network(self) -> None:
        limits = {"cpus": 1.5, "memory": "2g", "pids": 256}
        argv = argv_of(build(docker_cfg(network="hb-egress", limits=limits)))
        self.assertEqual(
            argv[9:23],
            [
                "-w",
                "/work",
                "--network",
                "hb-egress",
                "--cpus",
                "1.5",
                "--memory",
                "2g",
                "--memory-swap",
                "2g",
                "--pids-limit",
                "256",
                "--cap-drop",
                "ALL",
            ],
        )
        self.assertEqual(argv[-3:], [IMAGE, "agent", "--go"])

    def test_a_whole_number_of_cpus_has_no_decimal_point(self) -> None:
        self.assertIn("2", argv_of(build(docker_cfg(limits={"cpus": 2.0}))))

    def test_each_limit_alone_adds_only_its_own_flags(self) -> None:
        for limits, flags in (
            ({"memory": "512m"}, ["--memory", "512m", "--memory-swap", "512m"]),
            ({"pids": 64}, ["--pids-limit", "64"]),
            ({"cpus": 3}, ["--cpus", "3"]),
        ):
            with self.subTest(limits=limits):
                argv = argv_of(build(docker_cfg(limits=limits)))
                start = argv.index("none") + 1
                self.assertEqual(argv[start : start + len(flags)], flags)

    def test_mounts_follow_the_workdir_mount(self) -> None:
        argv = argv_of(build(docker_cfg(mounts=["/h/tests:/h/tests:ro", "/h/shared:/h/shared"])))
        self.assertEqual(argv[7:13], ["-v", "/w:/work", "-v", "/h/tests:/h/tests:ro", "-v", "/h/shared:/h/shared"])

    def test_every_command_of_a_trial_gets_its_own_container_name(self) -> None:
        session = build(docker_cfg()).create(Path("/w"), "t1")
        names = [session.argv(["x"], Path("/e"))[4] for _ in range(3)]
        self.assertEqual(names, ["hb-t1-0", "hb-t1-1", "hb-t1-2"])

    def test_the_prefix_form_still_builds_what_it_always_built(self) -> None:
        section = json.loads((fixtures.ROOT / "config.example.json").read_text())["sandbox"]
        argv = sandbox_for({"sandbox": section}, Recorder(), {}).create(Path("/w"), "t").argv(["a"], Path("/e"))
        self.assertEqual((argv[:3], argv[-2:]), (["docker", "run", "--rm"], ["harness-bench-agents", "a"]))


class DestroyTests(unittest.TestCase):
    def test_destroy_removes_every_container_of_the_session_with_rm_f(self) -> None:
        run = Recorder()
        session = build(docker_cfg(), run).create(Path("/w"), "t1")
        session.argv(["a"], Path("/e"))
        session.argv(["b"], Path("/e"))
        session.destroy()
        self.assertEqual((run.calls[0][1], dict(run.calls[0][2])), (None, {"PATH": "/bin"}))
        self.assertEqual(
            [c[0] for c in run.calls],
            [
                ["docker", "rm", "-f", "hb-t1-0"],
                LIST_T1_0,
                ["docker", "rm", "-f", "hb-t1-1"],
                LIST_T1_1,
            ],
        )

    def test_end_step_removes_only_the_containers_of_that_step(self) -> None:
        run = Recorder()
        session = build(docker_cfg(), run).create(Path("/w"), "t1")
        session.argv(["a"], Path("/e"))
        session.end_step()
        session.end_step()  # nothing new: no second call
        session.argv(["b"], Path("/e"))
        session.end_step()
        session.destroy()  # nothing pending
        self.assertEqual(
            [c[0] for c in run.calls],
            [["docker", "rm", "-f", "hb-t1-0"], LIST_T1_0, ["docker", "rm", "-f", "hb-t1-1"], LIST_T1_1],
        )

    def test_destroy_without_a_command_runs_nothing(self) -> None:
        run = Recorder()
        build(docker_cfg(), run).create(Path("/w"), "t1").destroy()
        self.assertEqual(run.calls, [])

    def pending(self, run: Callable[..., Any]) -> SandboxSession:
        session = build(docker_cfg(), run).create(Path("/w"), "t1")
        session.argv(["a"], Path("/e"))
        return session

    def test_a_container_that_is_already_gone_is_fine(self) -> None:
        run = Recorder(script=[GONE])
        self.pending(run).end_step()
        self.assertEqual([c[0] for c in run.calls], [["docker", "rm", "-f", "hb-t1-0"], LIST_T1_0])

    def test_any_other_failure_of_rm_fails_closed(self) -> None:
        run = Recorder(script=[(1, False, "", "Cannot connect to the Docker daemon")])
        with self.assertRaisesRegex(SandboxError, "docker rm -f hb-t1-0 failed: Cannot connect"):
            self.pending(run).end_step()
        self.assertEqual(len(run.calls), 1)

    def test_a_helper_that_times_out_or_is_missing_fails_closed(self) -> None:
        with self.assertRaisesRegex(SandboxError, "timed out"):
            self.pending(Recorder(script=[(-1, True, "", "")])).end_step()
        missing = mock.Mock(side_effect=FileNotFoundError(2, "No such file or directory"))
        with self.assertRaisesRegex(SandboxError, "docker rm -f hb-t1-0: No such file or directory"):
            self.pending(missing).end_step()

    def test_a_container_still_listed_after_rm_is_removed_again(self) -> None:
        run = Recorder(script=[(0, False, "", ""), (0, False, "abc123\n", ""), (0, False, "", ""), (0, False, "", "")])
        self.pending(run).end_step()
        self.assertEqual(
            [c[0] for c in run.calls],
            [["docker", "rm", "-f", "hb-t1-0"], LIST_T1_0, ["docker", "rm", "-f", "hb-t1-0"], LIST_T1_0],
        )

    def test_a_container_that_survives_the_second_rm_fails_closed(self) -> None:
        listed = (0, False, "abc123\n", "")
        run = Recorder(script=[(0, False, "", ""), listed, (0, False, "", ""), listed])
        with self.assertRaisesRegex(SandboxError, "hb-t1-0 could not be removed"):
            self.pending(run).end_step()

    def test_a_listing_that_fails_fails_closed(self) -> None:
        run = Recorder(script=[(0, False, "", ""), (1, False, "", "daemon down")])
        with self.assertRaisesRegex(SandboxError, "docker ps -aq --filter name=\\^hb-t1-0\\$ failed: daemon down"):
            self.pending(run).end_step()

    def test_destroy_fails_closed_too(self) -> None:
        run = Recorder(script=[(1, False, "", "daemon down")])
        with self.assertRaises(SandboxError):
            self.pending(run).destroy()

    def test_a_second_destroy_does_nothing(self) -> None:
        run = Recorder()
        session = build(docker_cfg(), run).create(Path("/w"), "t1")
        session.argv(["a"], Path("/e"))
        session.destroy()
        session.destroy()
        self.assertEqual(len(run.calls), 2)  # one `rm -f` and the check that the container is gone

    def test_destroy_tries_again_for_a_container_end_step_could_not_remove(self) -> None:
        run = Recorder(script=[(1, False, "", "daemon down")])
        session = self.pending(run)
        with self.assertRaises(SandboxError):
            session.end_step()
        session.destroy()  # the daemon is back: the same container, removed now
        self.assertEqual(
            [c[0] for c in run.calls],
            [["docker", "rm", "-f", "hb-t1-0"], ["docker", "rm", "-f", "hb-t1-0"], LIST_T1_0],
        )

    def test_reclaim_removes_the_containers_a_killed_run_left_for_the_trial(self) -> None:
        run = Recorder(script=[(0, False, "hb-t1-0\nhb-t1-3\n", "")])
        build(docker_cfg(), run).reclaim("t1")
        self.assertEqual(
            [c[0] for c in run.calls],
            [
                ["docker", "ps", "-a", "--filter", "name=hb-t1-", "--format", "{{.Names}}"],
                ["docker", "rm", "-f", "hb-t1-0"],
                LIST_T1_0,
                ["docker", "rm", "-f", "hb-t1-3"],
                ["docker", "ps", "-aq", "--filter", "name=^hb-t1-3$"],
            ],
        )

    def test_reclaim_with_nothing_left_runs_only_the_listing(self) -> None:
        run = Recorder()
        build(docker_cfg(), run).reclaim("t1")
        self.assertEqual([c[0][1] for c in run.calls], ["ps"])

    def test_reclaim_when_docker_cannot_list_fails_closed(self) -> None:
        run = Recorder((1, False, "", "daemon down"))
        with self.assertRaisesRegex(SandboxError, "daemon down"):
            build(docker_cfg(), run).reclaim("t1")
        self.assertEqual(len(run.calls), 1)


class FakeDockerRunTests(RunTestCase):
    """The engine end to end through the real argv of the sandbox, with a fake `docker` on PATH."""

    def setUp(self) -> None:
        super().setUp()
        bin_dir = fixtures.tmpdir(self)
        fake_docker.install(bin_dir)
        self.log = bin_dir / "docker.log"
        patcher = mock.patch.dict(
            os.environ,
            {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_DOCKER_LOG": str(self.log)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg["sandbox"] = {
            "type": "docker",
            "image": IMAGE,
            "limits": {"cpus": 2, "memory": "1g", "pids": 128},
            "mounts": [f"{fixtures.ROOT / 'tests'}:{fixtures.ROOT / 'tests'}:ro", f"{self.base}:{self.base}"],
        }
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}]
        self.cfg["env_passthrough"] = ["FAKE_DOCKER_LOG"]  # the fake docker is a process of the run: env allowlist

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_the_agent_and_the_graders_run_through_docker_run_and_the_containers_are_removed(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(allow_unsandboxed=False), self.cfg, self.base)
        runs = [c for c in self.calls() if c[0] == "run"]
        self.assertEqual(len(runs), 2)  # the agent and the hidden tests
        for call in runs:
            self.assertEqual(call[call.index("--network") + 1], "none")
            self.assertIn("--cap-drop", call)
            self.assertEqual(call[call.index("--pids-limit") + 1], "128")
            self.assertEqual(call[call.index(IMAGE) - 1], f"{os.getuid()}:{os.getgid()}")
        names = [c[c.index("--name") + 1] for c in runs]
        self.assertEqual(len(set(names)), 2)
        self.assertEqual([c for c in self.calls() if c[0] == "rm"], [["rm", "-f", name] for name in names])
        (row,) = rows_of(self.base)
        self.assertEqual((row["passed"], row["sandboxed"]), (True, True))

    def test_the_run_and_its_trial_record_the_sandbox_and_the_image_digest(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(allow_unsandboxed=False), self.cfg, self.base)
        (run_dir,) = [p.parent for p in (self.base / "results").glob("*/trials.jsonl")]
        info = json.loads((run_dir / "run.json").read_text())
        (trial,) = fixtures.read_rows(run_dir / "trials.jsonl")
        self.assertEqual(
            (
                info["sandbox"],
                info["sandboxed"],
                info["schema_version"],
                trial["image_digest"],
                trial["schema_version"],
            ),
            (f"docker:{IMAGE}", True, "1.3.0", f"sha256:{DIGEST}", "1.3.0"),
        )

    def test_a_container_that_outlives_the_timeout_is_removed_before_the_next_step(self) -> None:
        state = fixtures.tmpdir(self)
        self.addCleanup(self.kill_survivors, state)
        names = ["FAKE_DOCKER_STATE", "FAKE_DOCKER_LOG"]
        self.cfg["env_passthrough"] = names
        steady = (
            f"{fixtures.PY} -c \"import os, time; a = os.path.getsize('late.txt'); time.sleep(0.5); "
            "raise SystemExit(0 if os.path.getsize('late.txt') == a else 1)\""
        )
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, fixtures.AGENT, "claude", "slow-writer"]
        self.cfg["tasks"][0] = {
            **self.cfg["tasks"][0],
            "timeout": 3,
            "graders": [{"type": "command", "command": steady}],
        }
        with mock.patch.dict(os.environ, {"FAKE_DOCKER_STATE": str(state)}):
            fixtures.call_quiet(hb.cmd_run, args(allow_unsandboxed=False), self.cfg, self.base)
        (row,) = rows_of(self.base)
        self.assertEqual((row["agent_timed_out"], row["passed"]), (True, True))  # the writer was gone for the grader

    @staticmethod
    def kill_survivors(state: Path) -> None:
        for pid_file in state.iterdir():
            fake_docker.kill_container(int(pid_file.read_text()))

    def test_validate_config_rejects_a_tag_before_any_run(self) -> None:
        self.cfg["sandbox"] = {"type": "docker", "image": "harness-bench-agents:latest"}
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(allow_unsandboxed=False), self.cfg, self.base)
        self.assertIn("pinned by digest", str(cm.exception.code))
        self.assertFalse(self.log.exists())

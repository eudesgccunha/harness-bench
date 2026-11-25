"""`doctor` (ADR 0010, decision 10): every check passes and fails on its own, with a fake docker and a fake LiteLLM."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import fake_docker
import fixtures
import harness_bench as hb
from hbench import cli
from hbench.doctor import Check, run_checks
from test_litellm import ServerTestCase, ok

DIGEST = "a" * 64
IMAGE = f"harness-bench-agents@sha256:{DIGEST}"


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


class DoctorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = fixtures.tmpdir(self)
        self.cfg: dict[str, Any] = fixtures.fake_config({})
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}, {"harness": "pi", "model": "p/fix"}]
        bin_dir = fixtures.tmpdir(self)
        fake_docker.install(bin_dir)
        self.log = bin_dir / "docker.log"
        patcher = mock.patch.dict(
            os.environ,
            {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_DOCKER_LOG": str(self.log)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def checks(self, network: bool = False) -> dict[str, Check]:
        return by_name(run_checks(self.cfg, self.base, network, hb))

    def docker_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def use_docker(self, **section: Any) -> None:
        # The fake docker is a process of the run: it reads these through the env allowlist.
        self.cfg["env_passthrough"] = [
            "FAKE_DOCKER_LOG",
            "FAKE_DOCKER_INSPECT",
            "FAKE_DOCKER_INSPECT_RC",
            "FAKE_DOCKER_MISSING",
            "FAKE_DOCKER_VERSION",
        ]
        self.cfg["sandbox"] = {"type": "docker", "image": IMAGE, **section}


class ConfigAndEnvTests(DoctorCase):
    def test_a_good_local_config_passes_every_check(self) -> None:
        checks = run_checks(self.cfg, self.base, False, hb)
        self.assertTrue(all(c.ok for c in checks), [c for c in checks if not c.ok])
        self.assertEqual(
            [c.name for c in checks],
            ["config", "env", "sandbox", "harness claude", "harness pi", "urls"],
        )

    def test_the_engine_is_found_when_it_is_not_given(self) -> None:
        self.assertEqual(
            [c.name for c in run_checks(self.cfg, self.base, False)],
            [c.name for c in run_checks(self.cfg, self.base, False, hb)],
        )

    def test_an_invalid_config_is_one_failed_check_and_nothing_else_runs(self) -> None:
        self.use_docker(image="harness-bench-agents:latest")
        checks = run_checks(self.cfg, self.base, True, hb)
        self.assertEqual([(c.name, c.ok) for c in checks], [("config", False)])
        self.assertIn("pinned by digest", checks[0].detail)
        self.assertEqual(self.docker_calls(), [])

    def test_a_config_that_misses_a_section_is_a_failed_check_not_a_traceback(self) -> None:
        del self.cfg["harnesses"]
        (check,) = run_checks(self.cfg, self.base, False, hb)
        self.assertEqual((check.name, check.ok), ("config", False))
        self.assertIn("harnesses", check.detail)

    def test_the_variables_the_config_names_must_be_set_and_only_their_names_are_shown(self) -> None:
        self.cfg["litellm"] = {"base_url": "${HB_DOC_URL}", "api_key": "${HB_DOC_KEY}"}
        self.cfg["harnesses"]["claude"]["env"]["TOKEN"] = "${HB_DOC_TOKEN}"
        self.cfg["env_passthrough"] = ["HB_DOC_PASS"]
        secret = fixtures.fake_secret("doctor")
        with mock.patch.dict(os.environ, {"HB_DOC_KEY": secret, "HB_DOC_URL": "http://127.0.0.1:1"}):
            check = self.checks()["env"]
        self.assertFalse(check.ok)
        self.assertIn("HB_DOC_TOKEN", check.detail)
        self.assertIn("HB_DOC_PASS", check.detail)
        self.assertNotIn("HB_DOC_KEY", check.detail)
        self.assertNotIn(secret, check.detail)

    def test_all_variables_set_passes(self) -> None:
        self.cfg["litellm"] = {"base_url": "${HB_DOC_URL}", "api_key": "${HB_DOC_KEY}"}
        self.cfg["judge"] = {"base_url": "${HB_DOC_URL}", "api_key": "${HB_DOC_KEY}", "model": "m"}
        with mock.patch.dict(os.environ, {"HB_DOC_KEY": "k", "HB_DOC_URL": "http://127.0.0.1:1"}):
            self.assertTrue(self.checks()["env"].ok)


class HarnessCliTests(DoctorCase):
    def test_a_local_cli_must_be_on_the_path(self) -> None:
        self.cfg["harnesses"]["pi"]["command"] = ["no-such-cli-for-doctor", "--x"]
        checks = self.checks()
        self.assertEqual((checks["harness claude"].ok, checks["harness pi"].ok), (True, False))
        self.assertIn("no-such-cli-for-doctor", checks["harness pi"].detail)

    def test_a_prefix_sandbox_cannot_see_the_cli_so_a_missing_one_is_only_a_warning(self) -> None:
        self.cfg["sandbox"] = {"prefix": ["env"]}
        self.cfg["harnesses"]["pi"]["command"] = ["no-such-cli-for-doctor"]
        check = self.checks()["harness pi"]
        self.assertEqual((check.ok, check.warning), (True, True))

    def test_the_cli_is_looked_up_once_per_harness_even_when_the_matrix_repeats_it(self) -> None:
        self.cfg["matrix"] = [{"harness": "claude", "model": "a"}, {"harness": "claude", "model": "b"}]
        self.assertEqual([n for n in self.checks() if n.startswith("harness")], ["harness claude"])

    def test_a_harness_without_a_command_has_nothing_to_look_up(self) -> None:
        self.cfg["harnesses"]["pi"]["command"] = []
        check = self.checks()["harness pi"]
        self.assertEqual((check.ok, check.detail), (True, "no command to look up"))

    def test_a_harness_outside_the_matrix_is_not_checked(self) -> None:
        self.cfg["matrix"] = [{"harness": "claude", "model": "a"}]
        self.assertEqual([n for n in self.checks() if n.startswith("harness")], ["harness claude"])


class DockerChecksTests(DoctorCase):
    def inspect(self, rc: int = 0, **info: Any) -> None:
        image = {"Id": f"sha256:{DIGEST}", "RepoDigests": [f"harness-bench-agents@sha256:{DIGEST}"], **info}
        os.environ["FAKE_DOCKER_INSPECT"] = json.dumps([image])
        os.environ["FAKE_DOCKER_INSPECT_RC"] = str(rc)
        self.addCleanup(os.environ.pop, "FAKE_DOCKER_INSPECT", None)
        self.addCleanup(os.environ.pop, "FAKE_DOCKER_INSPECT_RC", None)

    def test_the_pinned_image_is_found_by_its_digest_and_each_cli_answers_through_the_sandbox(self) -> None:
        self.use_docker()
        self.inspect()
        checks = self.checks()
        self.assertTrue(all(c.ok for c in checks.values()), [c for c in checks.values() if not c.ok])
        self.assertEqual(checks["sandbox"].detail, f"docker:{IMAGE}, image present")
        runs = [c for c in self.docker_calls() if c[0] == "run"]
        self.assertEqual(len(runs), 2)
        for call in runs:  # the probe has the same limits as an agent: no network, no capabilities
            self.assertEqual(call[call.index("--network") + 1], "none")
            self.assertIn("--cap-drop", call)
            self.assertEqual(call[-1], "--version")
        self.assertEqual(checks["harness claude"].detail, "1.2.3")

    def test_each_probe_container_is_removed_again_and_its_temp_dir_is_gone(self) -> None:
        self.use_docker()
        self.inspect()
        self.checks()
        removed = [c[2:] for c in self.docker_calls() if c[0] == "rm"]
        self.assertEqual([len(r) for r in removed], [1, 1])
        self.assertEqual(len({r[0] for r in removed}), 2)  # a name of its own per invocation
        for (name,) in removed:
            self.assertRegex(name, r"^hb-doctor-[0-9a-f]{8}-0$")
        self.assertEqual(list(Path(tempfile.gettempdir()).glob("harness-bench-doctor-*")), [])

    def test_a_digest_that_the_local_image_does_not_have_fails(self) -> None:
        self.use_docker()
        self.inspect(Id="sha256:" + "b" * 64, RepoDigests=["other@sha256:" + "c" * 64])
        check = self.checks()["sandbox"]
        self.assertFalse(check.ok)
        self.assertIn("digest", check.detail)

    def test_an_image_docker_does_not_have_fails_with_the_command_to_get_it(self) -> None:
        self.use_docker()
        self.inspect(rc=1)
        check = self.checks()["sandbox"]
        self.assertFalse(check.ok)
        self.assertIn("docker pull", check.detail)

    def test_a_machine_without_docker_fails_the_image_check_with_the_reason(self) -> None:
        self.use_docker()
        with mock.patch.object(hb, "run_proc", side_effect=FileNotFoundError(2, "No such file or directory")):
            checks = self.checks()
        self.assertFalse(checks["sandbox"].ok)
        self.assertIn("docker: No such file or directory", checks["sandbox"].detail)
        self.assertFalse(checks["harness claude"].ok)

    def test_an_unreadable_inspect_reply_fails(self) -> None:
        self.use_docker()
        os.environ["FAKE_DOCKER_INSPECT"] = "not json"
        self.addCleanup(os.environ.pop, "FAKE_DOCKER_INSPECT", None)
        self.assertFalse(self.checks()["sandbox"].ok)

    def test_a_cli_the_image_lacks_fails_and_the_others_still_pass(self) -> None:
        self.use_docker()
        self.inspect()
        self.cfg["harnesses"]["pi"]["command"] = ["pi", "--mode", "json"]
        with mock.patch.dict(os.environ, {"FAKE_DOCKER_MISSING": "pi"}):
            checks = self.checks()
        self.assertEqual((checks["harness claude"].ok, checks["harness pi"].ok), (True, False))
        self.assertIn("pi --version failed in the image", checks["harness pi"].detail)
        self.assertIn("executable file not found", checks["harness pi"].detail)

    def test_the_cli_probes_are_skipped_when_the_image_is_missing(self) -> None:
        self.use_docker()
        self.inspect(rc=1)
        checks = self.checks()
        self.assertEqual([c for c in self.docker_calls() if c[0] == "run"], [])
        self.assertFalse(checks["harness claude"].ok)
        self.assertIn("image", checks["harness claude"].detail)

    def test_no_network_is_fine_and_needs_no_docker_call(self) -> None:
        self.use_docker()
        self.inspect()
        check = self.checks()["network"]
        self.assertEqual(
            (check.ok, check.warning, check.detail), (True, False, "none: no network inside the container")
        )
        self.assertEqual([c for c in self.docker_calls() if c[0] == "network"], [])

    def test_an_internal_network_passes_and_a_routable_one_warns(self) -> None:
        self.use_docker(network="hb-egress")
        self.inspect()
        for internal, expected in ((True, (True, False)), (False, (True, True))):
            with self.subTest(internal=internal):
                with mock.patch.dict(os.environ, {"FAKE_DOCKER_INSPECT": json.dumps([{"Internal": internal}])}):
                    check = self.checks()["network"]
                self.assertEqual((check.ok, check.warning), expected)
        self.assertIn("docker network create --internal", check.detail)

    def test_a_network_docker_does_not_know_fails(self) -> None:
        self.use_docker(network="hb-egress")
        self.inspect()
        with mock.patch.dict(os.environ, {"FAKE_DOCKER_INSPECT_RC": "1"}):
            check = self.checks()["network"]
        self.assertFalse(check.ok)

    def test_an_unreadable_network_reply_fails(self) -> None:
        self.use_docker(network="hb-egress")
        self.inspect()
        with mock.patch.dict(os.environ, {"FAKE_DOCKER_INSPECT": "{}"}):
            self.assertFalse(self.checks()["network"].ok)


class UrlAndNetworkTests(ServerTestCase, DoctorCase):
    def setUp(self) -> None:
        ServerTestCase.setUp(self)
        DoctorCase.setUp(self)
        self.cfg["litellm"] = {"base_url": self.server.url, "api_key": "${HB_TEST_LITELLM_KEY}"}
        self.cfg["env_passthrough"] = []

    def test_a_plain_http_remote_url_fails_and_https_and_localhost_pass(self) -> None:
        for url, expected in (("http://litellm.example.com", False), ("https://litellm.example.com", True)):
            with self.subTest(url=url):
                self.cfg["litellm"]["base_url"] = url
                self.assertEqual(self.checks()["urls"].ok, expected)
        self.cfg["litellm"]["base_url"] = self.server.url
        self.assertTrue(self.checks()["urls"].ok)

    def test_the_judge_url_follows_the_same_rule(self) -> None:
        self.cfg["judge"] = {"base_url": "http://judge.example.com", "api_key": "${HB_TEST_LITELLM_KEY}", "model": "m"}
        check = self.checks()["urls"]
        self.assertFalse(check.ok)
        self.assertIn("judge.base_url", check.detail)

    def test_without_the_flag_nothing_goes_over_the_network(self) -> None:
        self.checks()
        self.assertEqual(self.server.requests, [])

    def test_the_flag_makes_one_health_get_and_one_spend_logs_get_of_page_size_one(self) -> None:
        self.server.route("/health/liveliness", ok("I'm alive!"))
        self.server.route("/spend/logs/v2", ok({"data": [], "total_pages": 1}))
        checks = self.checks(network=True)
        self.assertEqual([r[0] for r in self.server.requests], ["/health/liveliness", "/spend/logs/v2"])
        self.assertEqual([r[2] for r in self.server.requests], ["Bearer k-test"] * 2)
        self.assertIn("page_size=1", self.server.requests[1][1])
        self.assertEqual((checks["litellm health"].ok, checks["litellm spend key"].ok), (True, True))

    def test_a_spend_key_without_permission_fails_that_check_only(self) -> None:
        self.server.route("/health/liveliness", ok("I'm alive!"))
        self.server.route("/spend/logs/v2", (403, {}, {}))
        checks = self.checks(network=True)
        self.assertEqual((checks["litellm health"].ok, checks["litellm spend key"].ok), (True, False))
        self.assertIn("403", checks["litellm spend key"].detail)

    def test_an_unreachable_gateway_fails_both_checks(self) -> None:
        self.cfg["litellm"]["base_url"] = "http://127.0.0.1:1"
        checks = self.checks(network=True)
        self.assertEqual((checks["litellm health"].ok, checks["litellm spend key"].ok), (False, False))

    def test_the_flag_without_a_litellm_section_adds_no_check(self) -> None:
        del self.cfg["litellm"]
        self.assertEqual([n for n in self.checks(network=True) if n.startswith("litellm")], [])

    def test_a_gateway_variable_that_is_not_set_skips_the_url_and_network_checks_with_a_reason(self) -> None:
        self.cfg["litellm"] = {"base_url": "${HB_DOC_UNSET_URL}", "api_key": "${HB_TEST_LITELLM_KEY}"}
        checks = self.checks(network=True)
        self.assertEqual(self.server.requests, [])
        self.assertIn("HB_DOC_UNSET_URL", checks["urls"].detail)
        self.assertEqual([n for n in checks if n.startswith("litellm")], [])


class CommandTests(DoctorCase):
    def write(self) -> str:
        path = self.base / "config.json"
        path.write_text(json.dumps(self.cfg))
        return str(path)

    def call(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        code = 0
        with redirect_stdout(buf):
            try:
                cli.main(["doctor", "--config", self.write(), *argv])
            except SystemExit as e:
                code = int(e.code) if isinstance(e.code, int) else 1
        return code, buf.getvalue()

    def test_a_good_config_prints_one_line_per_check_and_exits_zero(self) -> None:
        code, out = self.call()
        self.assertEqual(code, 0)
        self.assertIn("ok    config", out)
        self.assertIn("6 checks, 0 failed, 0 warnings", out)

    def test_any_failed_check_exits_one(self) -> None:
        self.cfg["harnesses"]["pi"]["command"] = ["no-such-cli-for-doctor"]
        code, out = self.call()
        self.assertEqual(code, 1)
        self.assertIn("FAIL  harness pi", out)
        self.assertIn("1 failed", out)

    def test_a_warning_alone_does_not_fail_the_run(self) -> None:
        self.cfg["sandbox"] = {"prefix": ["env"]}
        self.cfg["harnesses"]["pi"]["command"] = ["no-such-cli-for-doctor"]
        code, out = self.call()
        self.assertEqual(code, 0)
        self.assertIn("warn  harness pi", out)
        self.assertIn("1 warnings", out)

    def test_doctor_runs_nothing_but_probes_and_spends_nothing(self) -> None:
        with mock.patch.object(hb, "cmd_run") as run:
            self.call()
        run.assert_not_called()

    def test_doctor_writes_no_results(self) -> None:
        self.assertEqual(self.call()[0], 0)
        self.assertEqual(list(Path(self.base).glob("results*")), [])

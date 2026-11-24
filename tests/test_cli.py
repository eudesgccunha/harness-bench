"""Tests for the single CLI (`python3 -m hbench`), the LiteLLM profile and the two shims."""

import contextlib
import copy
import importlib
import io
import json
import shutil
import subprocess
import sys
import unittest
from typing import Any
from unittest import mock

import fixtures
from hbench import cli
from hbench.core.schema import schema_document
from hbench.profiles import apply_litellm_profile
from test_litellm import ScriptedServer, ok

ROOT = fixtures.ROOT
PY = fixtures.PY


def run_script(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([PY, *argv], cwd=ROOT, capture_output=True, text=True)


class SchemaExportTests(unittest.TestCase):
    def test_module_entry_point_prints_the_schema_document(self) -> None:
        p = run_script("-m", "hbench", "schema", "export")
        self.assertEqual((p.returncode, json.loads(p.stdout)), (0, schema_document()))

    def test_out_writes_the_same_document_to_a_file(self) -> None:
        path = fixtures.tmpdir(self) / "schema.json"
        with fixtures.quiet() as out:
            cli.main(["schema", "export", "--out", str(path)])
        self.assertEqual((json.loads(path.read_text()), out.getvalue()), (schema_document(), ""))

    def test_output_is_stable_and_indented(self) -> None:
        with fixtures.quiet() as out:
            cli.main(["schema", "export"])
        self.assertEqual(out.getvalue(), json.dumps(schema_document(), indent=2, sort_keys=True) + "\n")


class EntryPointTests(unittest.TestCase):
    def test_importing_the_main_module_does_not_run_the_cli(self) -> None:
        with mock.patch.object(sys, "argv", ["hbench"]):  # a run would fail on the missing subcommand
            importlib.import_module("hbench.__main__")

    def test_engine_lookup_puts_the_repo_root_on_the_path(self) -> None:
        with mock.patch.object(sys, "path", ["/nonexistent"]):
            engine = cli._engine()
            self.assertEqual(sys.path[0], str(ROOT))
        self.assertTrue(hasattr(engine, "cmd_run"))


class ProgNameTests(unittest.TestCase):
    def test_usage_names_the_script_that_was_run(self) -> None:
        for script in ("harness_bench.py", "harness_bench_litellm.py"):
            with self.subTest(script=script):
                self.assertIn(f"usage: {script}", run_script(script, "--help").stdout)

    def test_module_invocation_is_shown_as_python_dash_m(self) -> None:
        self.assertIn("usage: python3 -m hbench", run_script("-m", "hbench", "--help").stdout)

    def test_litellm_shim_help_says_the_profile_is_applied_automatically(self) -> None:
        out = run_script("harness_bench_litellm.py", "--help").stdout
        self.assertIn("LiteLLM profile is applied automatically", out)

    def test_plain_help_has_no_litellm_epilog(self) -> None:
        self.assertNotIn("applied automatically", run_script("harness_bench.py", "--help").stdout)


class ShimHelpTests(unittest.TestCase):
    def test_both_shims_list_the_three_commands(self) -> None:
        for script in ("harness_bench.py", "harness_bench_litellm.py"):
            with self.subTest(script=script):
                p = run_script(script, "--help")
                self.assertEqual(p.returncode, 0, p.stderr)
                for command in ("run", "report", "reconcile"):
                    self.assertIn(command, p.stdout)

    def test_missing_subcommand_is_a_usage_error(self) -> None:
        for script in ("harness_bench.py", "harness_bench_litellm.py"):
            with self.subTest(script=script):
                self.assertEqual(run_script(script).returncode, 2)

    def test_litellm_shim_does_not_inject_the_profile_into_other_commands(self) -> None:
        p = run_script("harness_bench_litellm.py", "schema", "export")
        self.assertEqual((p.returncode, json.loads(p.stdout)), (0, schema_document()))


class LitellmProfileTests(unittest.TestCase):
    def config(self) -> dict[str, Any]:
        cfg = fixtures.fake_config({})
        cfg["litellm"] = {"base_url": "https://x", "api_key": "${K}"}
        return cfg

    def test_does_not_mutate_its_input(self) -> None:
        cfg = self.config()
        before = copy.deepcopy(cfg)
        with fixtures.quiet():
            result = apply_litellm_profile(cfg)
        self.assertEqual(cfg, before)
        self.assertIsNot(result, cfg)

    def test_drops_cursor_and_forces_litellm_cost(self) -> None:
        with fixtures.quiet() as out:
            result = apply_litellm_profile(self.config())
        self.assertEqual(
            ("cursor" in result["harnesses"], len(result["matrix"]), result["cost_source"], result["output_dir"]),
            (False, 4, "litellm", "results-litellm"),
        )
        self.assertIn("ignored 1 matrix entries", out.getvalue())

    def test_an_explicit_output_dir_is_kept(self) -> None:
        cfg = self.config()
        cfg["output_dir"] = "mine"
        with fixtures.quiet():
            self.assertEqual(apply_litellm_profile(cfg)["output_dir"], "mine")

    def test_missing_litellm_section_exits(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            apply_litellm_profile(fixtures.fake_config({}))
        self.assertIn("needs a 'litellm' section", str(cm.exception.code))

    def test_matrix_entry_for_an_undefined_harness_exits(self) -> None:
        cfg = self.config()
        cfg["matrix"].append({"harness": "ghost", "model": "m"})
        with fixtures.quiet(), self.assertRaises(SystemExit) as cm:
            apply_litellm_profile(cfg)
        self.assertIn("undefined harnesses", str(cm.exception.code))


class CliDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ScriptedServer()
        self.addCleanup(self.server.close)
        self.base = fixtures.tmpdir(self)
        shutil.copytree(fixtures.FIZZBUZZ, self.base / "tasks" / "fizzbuzz")
        self.cfg = fixtures.fake_config({})
        self.cfg["litellm"] = {"base_url": self.server.url, "api_key": "k-test"}
        self.cfg_path = self.base / "config.json"
        self.cfg_path.write_text(json.dumps(self.cfg))
        self.server.route("/spend/logs/v2", ok({"data": [], "total_pages": 1}))

    def cli(self, *argv: str) -> str:
        with fixtures.quiet() as out:
            cli.main([*argv, "--config", str(self.cfg_path)])
        return out.getvalue()

    def rows(self, out_dir: str) -> list[dict[str, Any]]:
        return fixtures.read_rows(self.base / out_dir / "results.jsonl")

    def test_run_with_litellm_profile_drops_cursor_and_forces_gateway_cost(self) -> None:
        # Patch the CLI's own `time` name: a global `time.sleep` patch also catches the polling sleeps that
        # `subprocess.run(timeout=...)` makes while the fake agents run, which makes the call count load-dependent.
        with mock.patch("hbench.cli.time") as clock:
            out = self.cli("run", "--litellm", "--settle", "0", "--allow-unsandboxed")
        clock.sleep.assert_called_once_with(0)
        self.assertEqual(out.count("# Harness x model"), 1)  # one report, printed by reconcile after the wait
        self.assertGreater(out.index("# Harness x model"), out.index("waiting"))
        harnesses = {r["harness"] for r in self.rows("results-litellm")}
        self.assertEqual(harnesses, {"claude", "opencode", "pi", "codex"})
        summary = (self.base / "results-litellm" / "summary.md").read_text()
        self.assertEqual(("Cost source: `litellm`" in summary, "waiting 0s" in out), (True, True))

    def test_run_without_the_profile_keeps_every_harness_and_reports_directly(self) -> None:
        out = self.cli("run", "--allow-unsandboxed")
        self.assertEqual(
            (
                {r["harness"] for r in self.rows("results")} >= {"cursor"},
                "waiting" in out,
                "Cost source: `auto`" in out,
            ),
            (True, False, True),
        )

    def test_report_and_reconcile_commands_dispatch_to_the_engine(self) -> None:
        self.cli("run", "--allow-unsandboxed", "--only", "claude")
        self.assertIn("Cost source: `auto`", self.cli("report"))
        self.assertIn("runs matched by tag", self.cli("reconcile"))

    def test_cost_source_flag_conflicts_with_the_profile(self) -> None:
        err = io.StringIO()
        with fixtures.quiet(), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            self.cli("report", "--litellm", "--cost-source", "computed")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--cost-source cannot be combined with --litellm", err.getvalue())

    def test_report_with_the_profile_forces_the_gateway_cost(self) -> None:
        self.cli("run", "--allow-unsandboxed", "--only", "claude")
        shutil.copytree(self.base / "results", self.base / "results-litellm")
        self.assertIn("Cost source: `litellm`", self.cli("report", "--litellm"))


class SuiteCommandTests(unittest.TestCase):
    """`suite list` and `suite validate [--run]` over a one-task suite built on the fizzbuzz sample."""

    def setUp(self) -> None:
        self.base = fixtures.tmpdir(self)
        self.suite = fixtures.write_fizzbuzz_suite(self.base, "fizz")
        self.cfg: dict[str, Any] = {"harnesses": {}, "suites": ["suites/fizz"]}
        self.cfg_path = self.base / "config.json"

    def write_config(self) -> str:
        self.cfg_path.write_text(json.dumps(self.cfg))
        return str(self.cfg_path)

    def call(self, *argv: str, code: int | None = 0) -> str:
        """Output of `cli.main(argv)`; the exit code is checked (None means it must not exit)."""
        err = io.StringIO()
        with fixtures.quiet() as out, contextlib.redirect_stderr(err):
            if code is None:
                cli.main(list(argv))
            else:
                with self.assertRaises(SystemExit) as cm:
                    cli.main(list(argv))
                exit_code = cm.exception.code
                if not isinstance(exit_code, int):  # `sys.exit("message")`: status 1 and the message on stderr
                    err.write(str(exit_code))
                self.assertEqual(exit_code if isinstance(exit_code, int) else 1, code, err.getvalue())
        return out.getvalue() + err.getvalue()

    def test_list_prints_name_version_kind_task_count_and_tags(self) -> None:
        out = self.call("suite", "list", str(self.suite), code=None)
        self.assertEqual(out.strip(), "fizz  1.0.0  capability  1 task  tags: bugfix")

    def test_list_reads_the_suites_of_a_config(self) -> None:
        out = self.call("suite", "list", "--config", self.write_config(), code=None)
        self.assertIn("fizz  1.0.0  capability", out)

    def test_list_pluralizes_tasks_and_shows_a_dash_without_tags(self) -> None:
        document = json.loads((self.suite / "suite.json").read_text())
        document["tasks"][0].pop("tags")
        document["tasks"].append(dict(document["tasks"][0], id="second"))
        (self.suite / "suite.json").write_text(json.dumps(document))
        self.assertIn("2 tasks  tags: -", self.call("suite", "list", str(self.suite), code=None))

    def test_list_without_paths_or_config_is_a_usage_error(self) -> None:
        self.assertIn("give suite directories or --config", self.call("suite", "list", code=2))

    def test_list_of_a_bad_suite_exits_with_the_problem(self) -> None:
        (self.suite / "suite.json").write_text("{}")
        self.assertIn("missing key", self.call("suite", "list", str(self.suite), code=1))

    def test_validate_static_reports_ok_per_suite(self) -> None:
        out = self.call("suite", "validate", str(self.suite), code=None)
        self.assertEqual(out.strip(), "ok    fizz@1.0.0 (1 task)")

    def test_validate_static_fails_with_the_message_and_still_checks_the_other_suites(self) -> None:
        bad = self.base / "suites" / "bad"
        bad.mkdir()
        (bad / "suite.json").write_text("{}")
        out = self.call("suite", "validate", str(bad), str(self.suite), code=1)
        self.assertIn("FAIL  ", out)
        self.assertIn("missing key", out)
        self.assertIn("ok    fizz@1.0.0", out)

    def test_validate_static_checks_the_tasks_the_way_run_does(self) -> None:
        document = json.loads((self.suite / "suite.json").read_text())
        document["tasks"][0]["graders"] = [{"type": "command"}]
        (self.suite / "suite.json").write_text(json.dumps(document))
        out = self.call("suite", "validate", str(self.suite), code=1)
        self.assertIn("task fizz/fizzbuzz", out)

    def test_validate_defaults_to_the_suites_of_the_config(self) -> None:
        self.assertIn("ok    fizz@1.0.0", self.call("suite", "validate", "--config", self.write_config(), code=None))

    def test_validate_run_reports_ok_when_the_tests_fail_untouched_and_pass_with_the_solution(self) -> None:
        out = self.call("suite", "validate", "--run", "--allow-unsandboxed", str(self.suite), code=None)
        self.assertIn("fizz/fizzbuzz: ok", out)

    def test_validate_run_flags_a_task_whose_untouched_repo_already_passes(self) -> None:
        (self.suite / "fizzbuzz" / "repo" / "fizzbuzz.py").write_text(fixtures.FIXED_FIZZBUZZ)
        out = self.call("suite", "validate", "--run", "--allow-unsandboxed", str(self.suite), code=1)
        self.assertIn("fizz/fizzbuzz: trivially passing", out)

    def test_validate_run_flags_a_solution_that_fails_the_tests(self) -> None:
        buggy = (fixtures.FIZZBUZZ / "repo" / "fizzbuzz.py").read_text()
        (self.suite / "fizzbuzz" / "solution" / "fizzbuzz.py").write_text(buggy)
        out = self.call("suite", "validate", "--run", "--allow-unsandboxed", str(self.suite), code=1)
        self.assertIn("fizz/fizzbuzz: solution fails", out)

    def test_a_static_failure_skips_the_behavioral_run_of_the_valid_suites(self) -> None:
        bad = self.base / "suites" / "bad"
        bad.mkdir()
        (bad / "suite.json").write_text("{}")
        out = self.call("suite", "validate", "--run", "--allow-unsandboxed", str(bad), str(self.suite), code=1)
        self.assertIn("FAIL  ", out)
        self.assertNotIn("fizz/fizzbuzz", out)

    def test_validate_run_does_not_write_through_a_symlink_the_solution_meets_in_the_workdir(self) -> None:
        """A suite with a symlink never loads, so `validate --run` never copies a solution through one."""
        outside = fixtures.tmpdir(self)
        (self.suite / "fizzbuzz" / "repo" / "esc").symlink_to(outside, target_is_directory=True)
        (self.suite / "fizzbuzz" / "solution" / "esc").mkdir()
        (self.suite / "fizzbuzz" / "solution" / "esc" / "PWNED").write_text("x")
        out = self.call("suite", "validate", "--run", "--allow-unsandboxed", str(self.suite), code=1)
        self.assertIn("symlink esc", out)
        self.assertEqual(list(outside.iterdir()), [])

    def test_validate_run_flags_a_task_without_a_solution_and_does_not_run_it(self) -> None:
        document = json.loads((self.suite / "suite.json").read_text())
        del document["tasks"][0]["solution"]
        (self.suite / "suite.json").write_text(json.dumps(document))
        out = self.call("suite", "validate", "--run", "--allow-unsandboxed", str(self.suite), code=1)
        self.assertIn("fizz/fizzbuzz: no solution", out)

    def test_validate_run_without_a_sandbox_refuses_like_run(self) -> None:
        out = self.call("suite", "validate", "--run", str(self.suite), code=1)
        self.assertIn("No sandbox configured", out)
        self.assertNotIn("fizz/fizzbuzz", out)

    def test_validate_run_uses_the_sandbox_prefix_of_the_config(self) -> None:
        marker = self.base / "sandboxed.txt"
        self.cfg["sandbox"] = {"prefix": ["env", f"MARKER={marker}"]}
        calls: list[list[str]] = []
        real_run_proc = importlib.import_module("harness_bench").run_proc

        def spy(argv: list[str], *rest: Any) -> Any:
            calls.append(argv)
            return real_run_proc(argv, *rest)

        with mock.patch("harness_bench.run_proc", spy):
            out = self.call("suite", "validate", "--run", "--config", self.write_config(), code=None)
        self.assertIn("fizz/fizzbuzz: ok", out)
        self.assertTrue(calls and all(c[:2] == ["env", f"MARKER={marker}"] for c in calls), calls)

    def test_validate_run_runs_repo_code_in_a_workdir_outside_the_suite_and_removes_it(self) -> None:
        work = fixtures.tmpdir(self) / "work"
        self.cfg["work_dir"] = str(work)
        self.cfg["tasks"] = []
        out = self.call("suite", "validate", "--run", "--allow-unsandboxed", "--config", self.write_config(), code=None)
        self.assertIn("fizz/fizzbuzz: ok", out)
        self.assertEqual([p.name for p in work.iterdir() if not p.name.startswith(".")], ["logs"])
        self.assertEqual(sorted(p.name for p in self.suite.iterdir()), ["fizzbuzz", "suite.json"])

    def test_validate_run_keeps_the_env_allowlist_and_redacts_the_spend_key(self) -> None:
        secret = "-".join(["spend", "fake", "value", "12345"])  # built at runtime so secret scanners skip it
        self.cfg["litellm"] = {"base_url": "http://localhost:1", "api_key": "${HB_TEST_SPEND_KEY}"}
        document = json.loads((self.suite / "suite.json").read_text())
        document["tasks"][0]["setup"] = f"echo spend=${{HB_TEST_SPEND_KEY:-unset}} >&2; echo {secret}"
        (self.suite / "suite.json").write_text(json.dumps(document))
        work = fixtures.tmpdir(self) / "work"
        self.cfg["work_dir"] = str(work)
        with mock.patch.dict("os.environ", {"HB_TEST_SPEND_KEY": secret}):
            self.call("suite", "validate", "--run", "--allow-unsandboxed", "--config", self.write_config(), code=None)
        log = next((work / "logs").glob("*.setup.log")).read_text()
        self.assertEqual((secret in log, "spend=unset" in log, "***REDACTED***" in log), (False, True, True))


class ReportOptionsTests(unittest.TestCase):
    """`report` over hand-written rows: `--drop-saturated` and the knobs read from the config."""

    def setUp(self) -> None:
        self.base = fixtures.tmpdir(self)
        (self.base / "results").mkdir()
        self.cfg: dict[str, Any] = {
            "output_dir": "results",
            "harnesses": {"null": {"parser": "null", "command": ["true"]}},
        }

    def report(self, *argv: str) -> str:
        cfg_path = self.base / "config.json"
        cfg_path.write_text(json.dumps(self.cfg))
        with fixtures.quiet() as out:
            cli.main(["report", "--config", str(cfg_path), *argv])
        return out.getvalue()

    def write_rows(self, *rows: dict[str, Any]) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl", *rows)

    def row(self, task: str, harness: str, passed: bool, rep: int = 1) -> dict[str, Any]:
        return fixtures.run_row(task=task, harness=harness, model="m", passed=passed, rep=rep, cost_computed=0.1)

    def test_drop_saturated_removes_the_task_from_the_matrix_and_the_pass_rates(self) -> None:
        self.write_rows(
            self.row("easy", "a", True),
            self.row("hard", "a", True),
            self.row("easy", "b", True),
            self.row("hard", "b", False),
            self.row("easy", "null", False),  # a failing null pair does not keep `easy` from being saturated
        )
        kept = self.report()
        dropped = self.report("--drop-saturated")
        self.assertIn("| easy |", kept)
        self.assertNotIn("| easy |", dropped)
        self.assertIn("| hard |", dropped)
        self.assertIn("| 1 | a | m | 1/1 | 100% |", dropped)
        self.assertIn("| 1 | a | m | 2/2 | 100% |", kept)

    def test_dropping_every_task_says_so_and_exits_cleanly(self) -> None:
        self.write_rows(self.row("easy", "a", True), self.row("easy", "b", True))
        out = self.report("--drop-saturated")
        self.assertIn("no tasks left after --drop-saturated", out)
        self.assertNotIn("| # |", out)

    def test_config_knobs_reach_the_report(self) -> None:
        self.cfg.update(recommend_within_pp=60, target_ci_width=0.4)
        self.write_rows(self.row("t", "a", True), self.row("t", "a", False, rep=2))
        out = self.report()
        self.assertIn("within 60 pp", out)
        self.assertIn("CI width of 0.40", out)


class ShimMainTests(unittest.TestCase):
    def test_engine_main_delegates_to_the_cli(self) -> None:
        import harness_bench as hb

        with mock.patch.object(sys, "argv", ["harness_bench.py", "schema", "export"]), fixtures.quiet() as out:
            hb.main()
        self.assertEqual(json.loads(out.getvalue()), schema_document())

    def test_litellm_shim_adds_the_profile_flag_after_the_subcommand(self) -> None:
        import harness_bench_litellm as hbl

        with mock.patch.object(cli, "main") as cli_main, mock.patch.object(sys, "argv", ["x", "run", "--config", "c"]):
            hbl.main()
        cli_main.assert_called_once_with(["run", "--litellm", "--config", "c"], epilog=mock.ANY)

    def test_litellm_shim_passes_flags_without_a_subcommand_through(self) -> None:
        import harness_bench_litellm as hbl

        with mock.patch.object(cli, "main") as cli_main, mock.patch.object(sys, "argv", ["x", "--help"]):
            hbl.main()
        cli_main.assert_called_once_with(["--help"], epilog=mock.ANY)


if __name__ == "__main__":
    unittest.main()

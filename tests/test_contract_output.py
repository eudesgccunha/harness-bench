"""Tests for the contract output: `trial_from_row`, `RunWriter`, `apply_gateway_costs`, and `run`/`reconcile` files."""

import json
import os
import shutil
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import fixtures
import harness_bench as hb
from hbench.core.convert import trial_from_row, trial_from_row_lenient, trials_from_rows
from hbench.core.models import ContractError, Costs, CostSource, RunInfo, TrialResult, TrialStatus
from hbench.core.schema import RUN_SCHEMA, TRIAL_SCHEMA, validate
from hbench.results import RunWriter, apply_gateway_costs
from test_core_models import run_info, trial
from test_litellm import ScriptedServer, ok
from test_run import RunTestCase, args

API_KEY = "sk-test-agent-key-0123456789"
PROMPT = "PROMPT-CANARY-do-not-store"
MINIMAL_ROW: dict[str, Any] = {
    "task": "t1",
    "harness": "h",
    "model": "m",
    "rep": 1,
    "passed": True,
    "input": 100,
    "output": 10,
    "cache_read": 5,
    "cache_write": 2,
    "turns": 3,
    "wall_s": 1.5,
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return fixtures.read_rows(path)


def convert(row: dict[str, Any], **kw: Any) -> TrialResult:
    return trial_from_row(row, run_id="r" * 32, suite_version="s1", **kw)


class TrialFromRowTests(unittest.TestCase):
    def test_minimal_pre_phase_1_row_converts(self) -> None:
        t = convert(MINIMAL_ROW)
        self.assertEqual(
            (t.status, t.target.target_id, t.usage.total_tokens, t.usage.parsed, t.sandboxed, t.usage.cost_usd),
            (TrialStatus.PASSED, "h@unknown:m", 117, True, False, None),
        )
        self.assertEqual(
            (t.usage.cost_source, t.started_at, t.trace_id, t.artifact_uri),
            (CostSource.UNKNOWN, datetime.fromtimestamp(0, UTC), None, None),
        )
        self.assertEqual(validate(t.to_dict(), TRIAL_SCHEMA), [])

    def test_full_row_maps_every_field(self) -> None:
        row = dict(
            MINIMAL_ROW,
            ts="2026-11-24T10:00:00+00:00",
            reasoning=7,
            parse_ok=False,
            cost_litellm=0.3,
            cost_computed=0.2,
            cost_reported=0.1,
            run_tag="bench-20261124100000-fizz__claude__a-b__r1",
            agent_rc=2,
            sandboxed=True,
            passed=False,
        )
        t = convert(row, harness_version="9.9")
        self.assertEqual(
            (t.status, t.usage.reasoning_tokens, t.usage.parsed, t.usage.costs, t.agent_exit_code, t.sandboxed),
            (TrialStatus.FAILED, 7, False, Costs(gateway=0.3, price_table=0.2, harness=0.1), 2, True),
        )
        self.assertEqual(
            (
                t.trace_id,
                t.artifact_uri,
                t.target.harness_version,
                t.started_at.isoformat(),
                t.latency_s,
                t.suite_version,
            ),
            (row["run_tag"], "logs/fizz__claude__a-b__r1", "9.9", "2026-11-24T10:00:00+00:00", 1.5, "s1"),
        )

    def test_trial_from_row_stays_strict_about_rep(self) -> None:
        with self.assertRaises(ContractError):
            convert(dict(MINIMAL_ROW, rep=0))

    def test_report_path_reads_a_hand_written_rep_zero_as_rep_one(self) -> None:
        (t,) = trials_from_rows([dict(MINIMAL_ROW, rep=0)])
        self.assertEqual(t.rep, 1)

    def test_bad_numbers_from_a_parser_are_contract_errors(self) -> None:
        for key, bad in (("input", -1), ("input", float("nan")), ("output", float("inf")), ("cache_read", True)):
            with self.subTest(key=key, bad=bad), self.assertRaises(ContractError):
                convert(dict(MINIMAL_ROW, **{key: bad}))
        for key, bad in (("cost_reported", True), ("cost_computed", float("nan")), ("cost_litellm", -0.5)):
            with self.subTest(key=key, bad=bad), self.assertRaises(ContractError):
                convert(dict(MINIMAL_ROW, **{key: bad}))

    def test_lenient_conversion_keeps_the_trial_but_drops_the_bad_numbers(self) -> None:
        warnings: list[str] = []
        row = dict(MINIMAL_ROW, cost_reported=True, input=-4)
        t = trial_from_row_lenient(row, run_id="r" * 32, suite_version="s1", warn=warnings.append)
        self.assertEqual(
            (t.usage.parsed, t.usage.total_tokens, t.usage.cost_usd, t.passed, t.latency_s, len(warnings)),
            (False, 0, None, True, 1.5, 2),
        )
        self.assertTrue(all("t1" in w for w in warnings))
        self.assertTrue(any("input" in w for w in warnings) and any("cost_reported" in w for w in warnings))

    def test_tool_calls_come_from_the_row_and_default_to_unknown(self) -> None:
        self.assertEqual(
            [
                convert(dict(MINIMAL_ROW, **extra)).usage.tool_calls
                for extra in ({}, {"tool_calls": None}, {"tool_calls": 3})
            ],
            [None, None, 3],
        )

    def test_a_bad_tool_call_count_becomes_unknown_and_keeps_everything_else(self) -> None:
        for bad in (-1, True, 1.5, "2", float("nan")):
            warnings: list[str] = []
            t = trial_from_row_lenient(
                dict(MINIMAL_ROW, tool_calls=bad), run_id="r" * 32, suite_version="s1", warn=warnings.append
            )
            self.assertEqual((t.usage.tool_calls, t.usage.parsed, t.usage.total_tokens), (None, True, 117), repr(bad))
            self.assertTrue(len(warnings) == 1 and "tool_calls" in warnings[0], repr(bad))

    def test_a_bad_tool_call_count_is_dropped_silently_without_a_warn_callback(self) -> None:
        t = trial_from_row_lenient(dict(MINIMAL_ROW, tool_calls=-1), run_id="r" * 32, suite_version="s1")
        self.assertIsNone(t.usage.tool_calls)

    def test_a_valid_gateway_cost_survives_a_bad_harness_cost(self) -> None:
        warnings: list[str] = []
        row = dict(MINIMAL_ROW, cost_litellm=0.5, cost_computed=0.2, cost_reported=True)
        t = trial_from_row_lenient(row, run_id="r" * 32, suite_version="s1", warn=warnings.append)
        self.assertEqual(
            (t.usage.costs, t.usage.parsed, t.usage.total_tokens, len(warnings)),
            (Costs(gateway=0.5, price_table=0.2, harness=None), True, 117, 1),
        )
        self.assertIn("cost_reported", warnings[0])

    def test_a_bad_count_does_not_wipe_valid_costs(self) -> None:
        row = dict(MINIMAL_ROW, cost_litellm=0.5, cost_computed=2.0, output=float("nan"))
        t = trial_from_row_lenient(row, run_id="r" * 32, suite_version="s1")
        self.assertEqual((t.usage.costs.gateway, t.usage.parsed, t.usage.total_tokens), (0.5, False, 0))

    def test_a_bad_count_drops_the_price_table_cost_but_keeps_the_others(self) -> None:
        warnings: list[str] = []
        row = dict(MINIMAL_ROW, input=-1, cost_computed=1.0, cost_litellm=0.5, cost_reported=0.25)
        t = trial_from_row_lenient(row, run_id="r" * 32, suite_version="s1", warn=warnings.append)
        self.assertEqual(t.usage.costs, Costs(gateway=0.5, price_table=None, harness=0.25))
        self.assertTrue(any("price-table cost dropped" in w for w in warnings))

    def test_a_good_price_table_cost_is_kept_when_only_a_cost_is_bad(self) -> None:
        row = dict(MINIMAL_ROW, cost_computed=1.0, cost_reported=True)
        t = trial_from_row_lenient(row, run_id="r" * 32, suite_version="s1")
        self.assertEqual(t.usage.costs.price_table, 1.0)

    def test_a_long_non_numeric_value_is_not_echoed(self) -> None:
        secret = "SECRET-" + "x" * 300
        bad_value: Any = secret
        warnings: list[str] = []
        trial_from_row_lenient(
            dict(MINIMAL_ROW, input=secret, cost_reported=secret),
            run_id="r" * 32,
            suite_version="s1",
            warn=warnings.append,
        )
        with self.assertRaises(ContractError) as cm:
            convert(dict(MINIMAL_ROW, cache_read=secret))
        with self.assertRaises(ContractError) as cm2:
            Costs(**{"harness": bad_value})
        self.assertEqual(len(warnings), 2)
        self.assertFalse(any("SECRET" in m for m in [*warnings, str(cm.exception), str(cm2.exception)]))

    def test_every_bad_field_gets_its_own_warning(self) -> None:
        warnings: list[str] = []
        row = dict(MINIMAL_ROW, cost_litellm=-1.0, cost_computed=float("inf"), turns=True)
        trial_from_row_lenient(row, run_id="r" * 32, suite_version="s1", warn=warnings.append)
        self.assertEqual(len(warnings), 3)

    def test_integral_floats_are_counts_but_fractions_are_not(self) -> None:
        self.assertEqual(convert(dict(MINIMAL_ROW, input=2.0)).usage.input_tokens, 2)
        with self.assertRaises(ContractError):
            convert(dict(MINIMAL_ROW, input=1.9))
        t = trial_from_row_lenient(dict(MINIMAL_ROW, input=1.9), run_id="r" * 32, suite_version="s1")
        self.assertEqual((t.usage.parsed, t.usage.total_tokens), (False, 0))

    def test_report_path_warns_about_sanitized_rows_when_asked(self) -> None:
        warnings: list[str] = []
        trials_from_rows([dict(MINIMAL_ROW, cost_computed=float("nan"))], warn=warnings.append)
        self.assertEqual(len(warnings), 1)

    def test_report_path_survives_bad_rows(self) -> None:
        (t,) = trials_from_rows([dict(MINIMAL_ROW, cost_computed=float("nan"))])
        self.assertEqual((t.usage.parsed, t.usage.cost_usd), (True, None))

    def test_trial_id_depends_on_the_row_identity(self) -> None:
        self.assertNotEqual(convert(MINIMAL_ROW).trial_id, convert(dict(MINIMAL_ROW, rep=2)).trial_id)

    def test_timeout_mapping(self) -> None:
        failed = dict(MINIMAL_ROW, passed=False)
        self.assertEqual(convert(dict(failed, agent_timed_out=True)).status, TrialStatus.TIMEOUT)
        self.assertEqual(convert(failed, test_timed_out=True).status, TrialStatus.TIMEOUT)
        self.assertEqual(convert(failed).status, TrialStatus.FAILED)
        self.assertEqual(convert(dict(MINIMAL_ROW, agent_timed_out=True)).status, TrialStatus.PASSED)

    def test_test_timeout_is_recorded_on_the_grader(self) -> None:
        t = convert(dict(MINIMAL_ROW, passed=False), test_timed_out=True)
        self.assertEqual((t.graders[0].passed, t.graders[0].timed_out), (False, True))

    def test_started_at_falls_back_from_ts_to_t_start(self) -> None:
        row = dict(MINIMAL_ROW, ts="not a time", t_start=1_700_000_000.0)
        self.assertEqual(convert(row).started_at, datetime.fromtimestamp(1_700_000_000, UTC))

    def test_naive_ts_is_taken_as_utc(self) -> None:
        self.assertEqual(
            convert(dict(MINIMAL_ROW, ts="2026-01-02T03:04:05")).started_at, datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        )

    def test_run_tag_without_the_bench_prefix_gives_no_artifact_uri(self) -> None:
        t = convert(dict(MINIMAL_ROW, run_tag="tag-1"))
        self.assertEqual((t.trace_id, t.artifact_uri), ("tag-1", None))


class RunWriterTests(unittest.TestCase):
    def test_writes_run_json_at_construction_and_appends_trials(self) -> None:
        out = fixtures.tmpdir(self)
        info = run_info()
        writer = RunWriter(out, info)
        run_dir = out / info.run_id
        self.assertEqual(RunInfo.from_dict(json.loads((run_dir / "run.json").read_text())), info)
        writer.append(trial())
        writer.append(trial(rep=2))
        lines = read_jsonl(run_dir / "trials.jsonl")
        self.assertEqual([line["rep"] for line in lines], [1, 2])

    def test_close_records_the_finish_time(self) -> None:
        out = fixtures.tmpdir(self)
        info = run_info()
        writer = RunWriter(out, info)
        finished = datetime(2026, 11, 24, 13, 0, tzinfo=UTC)
        writer.close(finished)
        stored = RunInfo.from_dict(json.loads((out / info.run_id / "run.json").read_text()))
        self.assertEqual(stored.finished_at, finished)
        self.assertEqual((out / info.run_id / "trials.jsonl").read_text(), "")


class ApplyGatewayCostsTests(unittest.TestCase):
    def make(self, out: Path, run_id: str, *traces: str | None) -> Path:
        writer = RunWriter(out, run_info(run_id=run_id))
        for rep, trace in enumerate(traces, 1):
            writer.append(trial(run_id=run_id, trace_id=trace, rep=rep))
        return out / run_id / "trials.jsonl"

    def test_sets_gateway_cost_on_matching_trials_across_run_dirs(self) -> None:
        out = fixtures.tmpdir(self)
        first = self.make(out, "a" * 32, "t1", "t2", None)
        second = self.make(out, "b" * 32, "t1")
        updated = apply_gateway_costs(out, {"t1": 0.25})
        costs = [[line["usage"]["costs"]["gateway"] for line in read_jsonl(p)] for p in (first, second)]
        self.assertEqual((updated, costs), (2, [[0.25, None, None], [0.25]]))
        self.assertEqual(read_jsonl(first)[0]["usage"]["cost_source"], "gateway")

    def test_other_cost_fields_are_kept(self) -> None:
        out = fixtures.tmpdir(self)
        path = self.make(out, "a" * 32, "t1")
        apply_gateway_costs(out, {"t1": 0.25})
        self.assertEqual(read_jsonl(path)[0]["usage"]["costs"], {"gateway": 0.25, "price_table": 0.5, "harness": None})

    def test_output_without_run_dirs_updates_nothing(self) -> None:
        out = fixtures.tmpdir(self)
        (out / "logs").mkdir()
        self.assertEqual(apply_gateway_costs(out, {"t1": 1.0}), 0)

    def test_no_match_leaves_files_untouched(self) -> None:
        out = fixtures.tmpdir(self)
        path = self.make(out, "a" * 32, "t1")
        before = path.read_text()
        self.assertEqual(apply_gateway_costs(out, {"zzz": 1.0}), 0)
        self.assertEqual(path.read_text(), before)

    def test_a_foreign_or_corrupt_file_is_skipped_with_a_warning_and_others_are_updated(self) -> None:
        out = fixtures.tmpdir(self)
        bad = self.make(out, "a" * 32, "t1")
        bad.write_text(bad.read_text() + '{"nope": 1}\n')
        broken = self.make(out, "b" * 32, "t1")
        broken.write_text("not json\n")
        good = self.make(out, "c" * 32, "t1")
        before = (bad.read_text(), broken.read_text())
        with fixtures.quiet() as buf:
            updated = apply_gateway_costs(out, {"t1": 1.0})
        self.assertEqual(updated, 1)
        self.assertEqual((bad.read_text(), broken.read_text()), before)
        self.assertEqual(read_jsonl(good)[0]["usage"]["costs"]["gateway"], 1.0)
        self.assertIn(str(bad), buf.getvalue())
        self.assertIn(str(broken), buf.getvalue())

    def test_a_bad_spend_skips_only_that_tag_with_a_warning(self) -> None:
        out = fixtures.tmpdir(self)
        path = self.make(out, "a" * 32, "t1", "t2", "t3")
        with fixtures.quiet() as buf:
            updated = apply_gateway_costs(out, {"t1": float("nan"), "t2": -2.0, "t3": 0.25})
        gateways = [line["usage"]["costs"]["gateway"] for line in read_jsonl(path)]
        self.assertEqual((updated, gateways), (1, [None, None, 0.25]))
        self.assertIn("t1", buf.getvalue())
        self.assertIn("t2", buf.getvalue())

    def test_a_zero_gateway_cost_is_known_and_applied(self) -> None:
        out = fixtures.tmpdir(self)
        path = self.make(out, "a" * 32, "t1")
        self.assertEqual(apply_gateway_costs(out, {"t1": 0.0}), 1)
        usage = read_jsonl(path)[0]["usage"]
        self.assertEqual((usage["costs"]["gateway"], usage["cost_usd"], usage["cost_source"]), (0.0, 0.0, "gateway"))

    def test_the_count_is_trials_not_files(self) -> None:
        out = fixtures.tmpdir(self)
        self.make(out, "a" * 32, "t1", "t1", "t2")
        self.assertEqual(apply_gateway_costs(out, {"t1": 0.5}), 2)

    def test_a_failed_replace_leaves_no_temp_file_and_the_original(self) -> None:
        out = fixtures.tmpdir(self)
        path = self.make(out, "a" * 32, "t1")
        before = path.read_text()
        with mock.patch("os.replace", side_effect=OSError("disk")), self.assertRaises(OSError):
            apply_gateway_costs(out, {"t1": 1.0})
        self.assertEqual(
            (path.read_text(), sorted(p.name for p in path.parent.iterdir())), (before, ["run.json", "trials.jsonl"])
        )

    def test_rewritten_files_keep_their_permissions(self) -> None:
        out = fixtures.tmpdir(self)
        path = self.make(out, "a" * 32, "t1")
        path.chmod(0o640)
        apply_gateway_costs(out, {"t1": 1.0})
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_new_files_are_world_readable_like_other_results(self) -> None:
        out = fixtures.tmpdir(self)
        RunWriter(out, run_info(run_id="d" * 32))
        self.assertEqual((out / ("d" * 32) / "run.json").stat().st_mode & 0o777, 0o644)


class RunOutputTests(RunTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg["tasks"][0]["prompt"] = PROMPT
        for h in self.cfg["harnesses"].values():
            h["env"]["FAKE_API_KEY"] = "${FAKE_API_KEY}"
        patcher = mock.patch.dict(os.environ, {"FAKE_API_KEY": API_KEY})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.out = self.base / "results"

    def run_dirs(self) -> list[Path]:
        """Run directories, oldest first (their names are random)."""
        dirs = [p for p in self.out.iterdir() if (p / "run.json").exists()]
        return sorted(dirs, key=lambda p: json.loads((p / "run.json").read_text())["started_at"])

    def test_one_run_dir_with_valid_run_json_and_trials(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (run_dir,) = self.run_dirs()
        info = json.loads((run_dir / "run.json").read_text())
        trials = read_jsonl(run_dir / "trials.jsonl")
        legacy = fixtures.read_rows(self.out / "results.jsonl")
        self.assertEqual(validate(info, RUN_SCHEMA), [])
        self.assertEqual([validate(t, TRIAL_SCHEMA) for t in trials], [[], []])
        self.assertEqual((len(trials), len({t["trial_id"] for t in trials})), (len(legacy), len(legacy)))
        self.assertEqual({t["run_id"] for t in trials} | {info["run_id"]}, {run_dir.name})
        self.assertIsNotNone(info["finished_at"])
        self.assertEqual([t["status"] for t in trials], ["passed", "failed"])

    def test_run_info_describes_the_run(self) -> None:
        self.cfg["harnesses"]["claude"]["version"] = "2.0.1"
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        (run_dir,) = self.run_dirs()
        info = RunInfo.from_dict(json.loads((run_dir / "run.json").read_text()))
        self.assertEqual([t.target_id for t in info.targets], ["claude@2.0.1:fix"])
        self.assertEqual(
            (info.sandboxed, len(info.config_sha256), len(info.suite_version), info.harness_bench_version),
            (False, 64, 12, hb.VERSION),
        )

    def test_trials_hold_no_secret_prompt_or_agent_output(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (run_dir,) = self.run_dirs()
        text = (run_dir / "trials.jsonl").read_text() + (run_dir / "run.json").read_text()
        self.assertEqual((API_KEY in text, PROMPT in text), (False, False))

    def test_artifact_uri_points_at_the_logs_of_the_trial(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        (run_dir,) = self.run_dirs()
        uri = read_jsonl(run_dir / "trials.jsonl")[0]["artifact_uri"]
        self.assertEqual(uri, "logs/fizzbuzz__claude__fix__r1")
        self.assertTrue((self.out / f"{uri}.agent.jsonl").exists())

    def test_legacy_row_is_unchanged_by_the_contract(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        row = fixtures.read_rows(self.out / "results.jsonl")[0]
        self.assertEqual(
            sorted(row),
            sorted(
                "ts task harness model rep passed agent_rc agent_timed_out wall_s parse_ok run_tag t_start t_end "
                "sandboxed input output reasoning cache_read cache_write cost_reported turns tool_calls "
                "cost_computed".split()
            ),
        )

    def test_resume_writes_a_new_run_dir_with_no_trials(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        first, second = self.run_dirs()
        info = json.loads((second / "run.json").read_text())
        self.assertEqual(
            (
                len(read_jsonl(first / "trials.jsonl")),
                (second / "trials.jsonl").read_text(),
                info["finished_at"] is None,
            ),
            (2, "", False),
        )

    def test_interrupted_run_is_resumed_into_a_new_run_dir(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        first, second = self.run_dirs()
        self.assertEqual(
            (
                [t["target"]["harness"] for t in read_jsonl(first / "trials.jsonl")],
                [t["target"]["harness"] for t in read_jsonl(second / "trials.jsonl")],
            ),
            (["claude"], ["pi"]),
        )

    def test_dry_run_writes_no_run_dir(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(dry_run=True), self.cfg, self.base)
        self.assertEqual([p.name for p in self.out.iterdir()], ["logs"])

    def test_trials_record_a_test_timeout(self) -> None:
        self.cfg["tasks"][0]["test"] = f"{fixtures.PY} -c 'import time; time.sleep(60)'"
        self.cfg["tasks"][0]["test_timeout"] = 1
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        (run_dir,) = self.run_dirs()
        (line,) = read_jsonl(run_dir / "trials.jsonl")
        self.assertEqual((line["status"], line["graders"][0]["timed_out"]), ("timeout", True))


class HostileAgentOutputTests(RunTestCase):
    """Parser output is untrusted: bad numbers must not abort the matrix or split the two result files."""

    CASES = {
        "nan cost": ('{"type":"result","total_cost_usd":NaN,"usage":{"input_tokens":5},"num_turns":1}', True),
        "bool cost": ('{"type":"result","total_cost_usd":true,"usage":{"input_tokens":5},"num_turns":1}', True),
        "negative tokens": ('{"type":"result","usage":{"input_tokens":-5,"output_tokens":3},"num_turns":1}', False),
    }

    def set_agent(self, output: str) -> None:
        """Claude harness that prints `output` (a script file: command parts go through str.format)."""
        script = self.base / "agent_output.py"
        script.write_text(f"print({output!r})\n")
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, str(script)]

    def test_bad_numbers_keep_both_files_aligned(self) -> None:
        for name, (output, parsed) in self.CASES.items():
            with self.subTest(case=name):
                shutil.rmtree(self.base / "results", ignore_errors=True)
                self.set_agent(output)
                out = fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
                (run_dir,) = [p for p in (self.base / "results").iterdir() if (p / "run.json").exists()]
                lines = read_jsonl(run_dir / "trials.jsonl")
                self.assertEqual(len(fixtures.read_rows(self.base / "results" / "results.jsonl")), len(lines))
                self.assertEqual([validate(t, TRIAL_SCHEMA) for t in lines], [[]])
                self.assertEqual((lines[0]["usage"]["parsed"], lines[0]["usage"]["cost_usd"]), (parsed, None))
                tokens = lines[0]["usage"]
                kept = tokens["input_tokens"] + tokens["output_tokens"] if parsed else 0
                self.assertEqual(kept, 5 if parsed else 0)
                self.assertIn(lines[0]["trace_id"], out)  # the warning names the trial by its run tag
                self.assertIsNotNone(json.loads((run_dir / "run.json").read_text())["finished_at"])
                self.assertIn("warning", out)

    def test_the_matrix_continues_after_a_bad_trial(self) -> None:
        self.set_agent(self.CASES["bool cost"][0])
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertEqual(
            [r["harness"] for r in fixtures.read_rows(self.base / "results" / "results.jsonl")], ["claude", "pi"]
        )

    def test_report_prints_a_warning_naming_the_sanitized_field(self) -> None:
        self.set_agent(self.CASES["nan cost"][0])
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        out = fixtures.call_quiet(hb.cmd_report, args(), self.cfg, self.base)
        self.assertIn("warning: ", out)
        self.assertIn("cost_reported", out)

    def test_report_still_works_over_the_bad_legacy_row(self) -> None:
        self.set_agent(self.CASES["negative tokens"][0])
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        out = fixtures.call_quiet(hb.cmd_report, args(), self.cfg, self.base)
        self.assertIn("no token data from the harness for: claude/fix (1)", out)


class ReconcileContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = fixtures.tmpdir(self)
        shutil.copytree(fixtures.ROOT / "tasks" / "fizzbuzz", self.base / "tasks" / "fizzbuzz")
        self.server = ScriptedServer()
        self.addCleanup(self.server.close)
        patcher = mock.patch.dict(os.environ, {"HB_TEST_KEY": "k-test"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = fixtures.fake_config({})
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}]
        self.cfg["litellm"] = {"base_url": self.server.url, "api_key": "${HB_TEST_KEY}"}

    def test_gateway_cost_is_written_into_trials_by_trace_id(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        tag = fixtures.read_rows(self.base / "results" / "results.jsonl")[0]["run_tag"]
        self.server.route(
            "/spend/logs/v2",
            ok({"data": [{"request_tags": [tag], "spend": 0.25}, {"request_tags": [tag], "spend": 0.5}]}),
        )
        out = fixtures.call_quiet(hb.cmd_reconcile, mock.Mock(cost_source=None), self.cfg, self.base)
        (trials_path,) = (self.base / "results").glob("*/trials.jsonl")
        usage = read_jsonl(trials_path)[0]["usage"]
        self.assertEqual((usage["costs"]["gateway"], usage["cost_usd"], usage["cost_source"]), (0.75, 0.75, "gateway"))
        self.assertIn("contract: 1 trials updated", out)

    def test_pre_phase_1_output_without_run_dirs_still_reconciles(self) -> None:
        results = self.base / "results"
        results.mkdir()
        fixtures.write_rows(results / "results.jsonl", fixtures.run_row())
        self.server.route("/spend/logs/v2", ok({"data": [{"request_tags": ["tag-1"], "spend": 0.4}]}))
        out = fixtures.call_quiet(hb.cmd_reconcile, mock.Mock(cost_source=None), self.cfg, self.base)
        self.assertEqual(
            (fixtures.read_rows(results / "results.jsonl")[0]["cost_litellm"], "contract: 0 trials updated" in out),
            (0.4, True),
        )


if __name__ == "__main__":
    unittest.main()

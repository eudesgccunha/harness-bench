"""Tests for the data contract models in `hbench.core`."""

import copy
import dataclasses
import pickle
import unittest
from types import MappingProxyType
from datetime import UTC, datetime
from typing import Any

import fixtures  # noqa: F401  (puts the repo root on sys.path)
from hbench.core.ids import new_run_id, sha256_json, trial_id
from hbench.core.models import (
    SCHEMA_VERSION,
    ContractError,
    Costs,
    CostSource,
    GraderResult,
    RunInfo,
    Target,
    TrialResult,
    TrialStatus,
    Usage,
)

T0 = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)


def target(**overrides: Any) -> Target:
    values: dict[str, Any] = {
        "harness": "claude",
        "harness_version": "1.2.3",
        "model": "sonnet",
        "params": {"effort": "high", "n": 2, "t": 0.5, "flag": True},
    }
    values.update(overrides)
    return Target(**values)


def usage(**overrides: Any) -> Usage:
    values: dict[str, Any] = {
        "input_tokens": 10,
        "output_tokens": 5,
        "parsed": True,
        "costs": Costs(price_table=0.5),
    }
    values.update(overrides)
    return Usage(**values)


def trial(**overrides: Any) -> TrialResult:
    values: dict[str, Any] = {
        "run_id": "r" * 32,
        "trial_id": "t" * 16,
        "task_id": "fizzbuzz",
        "suite_version": "abc",
        "target": target(),
        "rep": 1,
        "status": TrialStatus.PASSED,
        "graders": (GraderResult(grader="test", grader_version="1", passed=True, score=1.0, details_uri="logs/x"),),
        "usage": usage(),
        "latency_s": 1.5,
        "started_at": T0,
        "agent_exit_code": 0,
        "sandboxed": True,
        "trace_id": "bench-1",
        "artifact_uri": "logs/x",
    }
    values.update(overrides)
    return TrialResult(**values)


def run_info(**overrides: Any) -> RunInfo:
    values: dict[str, Any] = {
        "run_id": "r" * 32,
        "started_at": T0,
        "finished_at": None,
        "harness_bench_version": "0.0.0",
        "config_sha256": "c" * 64,
        "suite_version": "abc",
        "python": "3.12.0",
        "platform": "linux",
        "sandboxed": False,
        "targets": (target(),),
    }
    values.update(overrides)
    return RunInfo(**values)


class RoundTripTests(unittest.TestCase):
    def test_every_model_survives_to_dict_from_dict(self) -> None:
        cases: list[Any] = [
            target(),
            GraderResult(grader="g", grader_version="1", passed=False, score=None, timed_out=True),
            Costs(gateway=1.0, price_table=None, harness=0.0),
            usage(),
            trial(),
            trial(status=TrialStatus.TIMEOUT, agent_exit_code=None, trace_id=None, artifact_uri=None),
            run_info(),
            run_info(finished_at=T0),
        ]
        for obj in cases:
            with self.subTest(obj=type(obj).__name__):
                self.assertEqual(type(obj).from_dict(obj.to_dict()), obj)

    def test_to_dict_is_json_ready(self) -> None:
        d = trial().to_dict()
        self.assertEqual(
            (d["status"], d["started_at"], type(d["graders"]), d["usage"]["costs"]["price_table"]),
            ("passed", "2026-11-24T12:00:00+00:00", list, 0.5),
        )

    def test_usage_serialises_the_picked_cost(self) -> None:
        d = usage(costs=Costs(gateway=2.0, harness=1.0)).to_dict()
        self.assertEqual((d["cost_usd"], d["cost_source"]), (2.0, "gateway"))

    def test_usage_rejects_a_picked_cost_that_disagrees(self) -> None:
        d = usage().to_dict()
        d["cost_usd"] = 99.0
        with self.assertRaises(ContractError):
            Usage.from_dict(d)


class FromDictRejectionTests(unittest.TestCase):
    def test_extra_key_is_rejected_and_named(self) -> None:
        d = trial().to_dict()
        d["surprise"] = 1
        with self.assertRaises(ContractError) as cm:
            TrialResult.from_dict(d)
        self.assertIn("surprise", str(cm.exception))

    def test_missing_key_is_rejected_and_named(self) -> None:
        d = trial().to_dict()
        del d["task_id"]
        with self.assertRaises(ContractError) as cm:
            TrialResult.from_dict(d)
        self.assertIn("task_id", str(cm.exception))

    def test_other_major_version_is_rejected_and_minor_accepted(self) -> None:
        d = trial().to_dict()
        d["schema_version"] = "1.4.0"
        self.assertEqual(TrialResult.from_dict(d).schema_version, "1.4.0")
        d["schema_version"] = "2.0.0"
        with self.assertRaises(ContractError):
            TrialResult.from_dict(d)

    def test_malformed_version_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            trial(schema_version="one")

    def test_unknown_enum_value_is_a_contract_error(self) -> None:
        d = trial().to_dict()
        d["status"] = "exploded"
        with self.assertRaises(ContractError):
            TrialResult.from_dict(d)

    def test_wrong_type_is_a_contract_error(self) -> None:
        d = trial().to_dict()
        d["rep"] = "one"
        with self.assertRaises(ContractError):
            TrialResult.from_dict(d)

    def test_list_field_must_be_a_list(self) -> None:
        d = trial().to_dict()
        d["graders"] = "nope"
        with self.assertRaises(ContractError):
            TrialResult.from_dict(d)

    def test_bad_timestamp_is_a_contract_error(self) -> None:
        for bad in ("yesterday", 12345):
            d = trial().to_dict()
            d["started_at"] = bad
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                TrialResult.from_dict(d)


class TypeCheckTests(unittest.TestCase):
    def test_scalar_fields_are_type_checked(self) -> None:
        bad: list[tuple[str, Any]] = [
            ("task_id", 5),
            ("sandboxed", 1),
            ("rep", True),
            ("rep", 1.0),
            ("latency_s", True),
            ("latency_s", "1"),
            ("status", "passed"),
            ("started_at", "2026-11-24T12:00:00+00:00"),
            ("graders", [GraderResult(grader="g", grader_version="1", passed=True)]),
            ("target", {"harness": "h"}),
            ("agent_exit_code", "0"),
            ("trace_id", 7),
        ]
        for name, value in bad:
            with self.subTest(field=name, value=value), self.assertRaises(ContractError):
                trial(**{name: value})

    def test_nested_and_container_types_are_checked(self) -> None:
        with self.assertRaises(ContractError):
            target(model=["x"])
        with self.assertRaises(ContractError):
            target(params={"k": [1]})
        with self.assertRaises(ContractError):
            target(params={1: "v"})
        with self.assertRaises(ContractError):
            usage(parsed="yes")
        with self.assertRaises(ContractError):
            usage(input_tokens=1.5)
        with self.assertRaises(ContractError):
            usage(input_tokens=True)
        with self.assertRaises(ContractError):
            run_info(targets=[target()])

    def test_from_dict_applies_the_same_checks(self) -> None:
        d = trial().to_dict()
        d["task_id"] = 5
        with self.assertRaises(ContractError):
            TrialResult.from_dict(d)

    def test_non_finite_and_bool_costs_are_rejected(self) -> None:
        bad_values: list[Any] = [float("nan"), float("inf"), True, "1"]
        for bad in bad_values:
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                Costs(**{"harness": bad})

    def test_tuple_elements_are_type_checked(self) -> None:
        with self.assertRaises(ContractError):
            trial(graders=("x",))
        with self.assertRaises(ContractError):
            run_info(targets=(1,))

    def test_non_finite_latency_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            trial(latency_s=float("nan"))

    def test_missing_schema_version_is_rejected_although_it_has_a_default(self) -> None:
        for model in (trial(), run_info()):
            d = model.to_dict()
            del d["schema_version"]
            with self.subTest(model=type(model).__name__), self.assertRaises(ContractError) as cm:
                type(model).from_dict(d)
            self.assertIn("schema_version", str(cm.exception))


class TargetParamsTests(unittest.TestCase):
    def test_models_survive_deepcopy_and_pickle(self) -> None:
        for model in (target(), trial(), run_info()):
            for clone in (copy.deepcopy(model), pickle.loads(pickle.dumps(model))):
                with self.subTest(model=type(model).__name__):
                    self.assertEqual((clone, hash(clone)), (model, hash(model)))
                    self.assertIsNot(clone, model)

    def test_params_are_copied_and_read_only(self) -> None:
        given = {"a": 1}
        t = target(params=given)
        given["a"] = 2
        self.assertEqual((t.params["a"], isinstance(t.params, MappingProxyType)), (1, True))

    def test_models_are_hashable_and_equal_targets_hash_equal(self) -> None:
        self.assertEqual(hash(target(params={"a": 1, "b": 2})), hash(target(params={"b": 2, "a": 1})))
        self.assertEqual(len({trial(), trial()}), 1)
        self.assertNotEqual(hash(target(params={"a": 1})), hash(target(params={"a": 2})))


class ValidationTests(unittest.TestCase):
    def test_costs_pick_prefers_gateway_then_price_table_then_harness(self) -> None:
        self.assertEqual(Costs(gateway=None, price_table=0.5, harness=0.7).pick(), (0.5, CostSource.PRICE_TABLE))
        self.assertEqual(Costs(gateway=0.1, price_table=0.5, harness=0.7).pick(), (0.1, CostSource.GATEWAY))
        self.assertEqual(Costs(harness=0.7).pick(), (0.7, CostSource.HARNESS))

    def test_costs_pick_unknown_is_none_not_zero(self) -> None:
        self.assertEqual(Costs().pick(), (None, CostSource.UNKNOWN))

    def test_costs_pick_can_force_a_source(self) -> None:
        costs = Costs(gateway=None, price_table=0.5)
        self.assertEqual(costs.pick(CostSource.GATEWAY), (None, CostSource.UNKNOWN))
        self.assertEqual(costs.pick(CostSource.PRICE_TABLE), (0.5, CostSource.PRICE_TABLE))
        self.assertEqual(costs.pick(CostSource.UNKNOWN), (None, CostSource.UNKNOWN))

    def test_zero_cost_is_a_known_cost(self) -> None:
        self.assertEqual(Costs(gateway=0.0, harness=1.0).pick(), (0.0, CostSource.GATEWAY))

    def test_negative_cost_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            Costs(harness=-1)

    def test_negative_token_count_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            usage(cache_read_tokens=-1)

    def test_usage_totals_and_picked_cost(self) -> None:
        u = usage(reasoning_tokens=3, cache_read_tokens=4, cache_write_tokens=5, costs=Costs(gateway=2.0))
        self.assertEqual((u.total_tokens, u.cost_usd, u.cost_source), (27, 2.0, CostSource.GATEWAY))

    def test_trial_id_is_stable_and_depends_on_rep(self) -> None:
        a = trial_id("run", "task", "h@v:m", 1)
        self.assertEqual(a, trial_id("run", "task", "h@v:m", 1))
        self.assertNotEqual(a, trial_id("run", "task", "h@v:m", 2))
        self.assertEqual(len(a), 16)

    def test_run_ids_are_unique_hex(self) -> None:
        a, b = new_run_id(), new_run_id()
        self.assertEqual((len(a), a == b, int(a, 16) >= 0), (32, False, True))

    def test_sha256_json_ignores_key_order(self) -> None:
        self.assertEqual(sha256_json({"a": 1, "b": 2}), sha256_json({"b": 2, "a": 1}))
        self.assertEqual(len(sha256_json([])), 64)

    def test_target_id(self) -> None:
        self.assertEqual(target().target_id, "claude@1.2.3:sonnet")

    def test_naive_datetime_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            trial(started_at=datetime(2026, 1, 1))
        with self.assertRaises(ContractError):
            run_info(finished_at=datetime(2026, 1, 1))

    def test_grader_score_must_be_in_unit_interval(self) -> None:
        for bad in (1.5, -0.1):
            with self.assertRaises(ContractError):
                GraderResult(grader="g", grader_version="1", passed=True, score=bad)

    def test_trial_rep_and_latency_bounds(self) -> None:
        with self.assertRaises(ContractError):
            trial(rep=0)
        with self.assertRaises(ContractError):
            trial(latency_s=-0.1)

    def test_passed_property_follows_status(self) -> None:
        self.assertEqual((trial().passed, trial(status=TrialStatus.FAILED).passed), (True, False))

    def test_models_are_frozen(self) -> None:
        field = "rep"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(trial(), field, 2)

    def test_schema_version_default(self) -> None:
        self.assertEqual(trial().schema_version, SCHEMA_VERSION)


class ToolCallsTests(unittest.TestCase):
    def test_tool_calls_default_to_unknown_and_round_trip(self) -> None:
        self.assertIsNone(usage().tool_calls)
        for calls in (None, 0, 7):
            self.assertEqual(Usage.from_dict(usage(tool_calls=calls).to_dict()).tool_calls, calls)

    def test_a_1_0_0_trial_without_tool_calls_still_loads(self) -> None:
        d = trial().to_dict()
        del d["usage"]["tool_calls"]
        d["schema_version"] = "1.0.0"
        loaded = TrialResult.from_dict(d)
        self.assertEqual((loaded.usage.tool_calls, loaded.schema_version), (None, "1.0.0"))

    def test_the_current_contract_version_is_1_1_0(self) -> None:
        self.assertEqual((SCHEMA_VERSION, trial().schema_version), ("1.1.0", "1.1.0"))

    def test_tool_calls_must_be_a_whole_number_of_at_least_zero(self) -> None:
        for bad in (-1, 1.5, True, "3"):
            with self.assertRaises(ContractError, msg=repr(bad)):
                usage(tool_calls=bad)


if __name__ == "__main__":
    unittest.main()

"""Tests for the hand-written JSON Schema and its small validator."""

import copy
import unittest
from dataclasses import fields
from collections.abc import Mapping
from typing import Any

import fixtures  # noqa: F401  (puts the repo root on sys.path)
from hbench.core import models
from hbench.core.schema import RUN_SCHEMA, TRIAL_SCHEMA, TRIAL_SPEC_SCHEMA, check_schema, schema_document, validate
from test_core_models import run_info, trial, trial_spec


class ValidateContractTests(unittest.TestCase):
    def test_a_valid_trial_and_run_have_no_errors(self) -> None:
        self.assertEqual(validate(trial().to_dict(), TRIAL_SCHEMA), [])
        self.assertEqual(validate(run_info().to_dict(), RUN_SCHEMA), [])
        self.assertEqual(validate(run_info(finished_at=trial().started_at).to_dict(), RUN_SCHEMA), [])

    def test_wrong_type_names_the_path(self) -> None:
        d = trial().to_dict()
        d["usage"]["input_tokens"] = "ten"
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.usage.input_tokens: expected integer"])

    def test_missing_required_property(self) -> None:
        d = trial().to_dict()
        del d["usage"]["costs"]
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.usage: missing required property 'costs'"])

    def test_extra_property_is_rejected(self) -> None:
        d = trial().to_dict()
        d["target"]["surprise"] = 1
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.target.surprise: unexpected property"])

    def test_bad_enum_value(self) -> None:
        d = trial().to_dict()
        d["status"] = "exploded"
        self.assertEqual(len(validate(d, TRIAL_SCHEMA)), 1)
        self.assertIn("$.status: expected one of", validate(d, TRIAL_SCHEMA)[0])

    def test_rep_zero_violates_the_minimum(self) -> None:
        d = trial().to_dict()
        d["rep"] = 0
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.rep: must be >= 1"])

    def test_score_above_one_violates_the_maximum(self) -> None:
        d = trial().to_dict()
        d["graders"][0]["score"] = 1.5
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.graders[0].score: must be <= 1"])

    def test_null_is_accepted_only_where_the_type_allows_it(self) -> None:
        d = trial(agent_exit_code=None).to_dict()
        d["task_id"] = None
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.task_id: expected string"])

    def test_bool_is_not_an_integer_or_a_number(self) -> None:
        d = trial().to_dict()
        d["rep"] = True
        d["latency_s"] = False
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.rep: expected integer", "$.latency_s: expected number"])

    def test_non_finite_numbers_are_not_numbers(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertEqual(validate(bad, {"type": "number"}), ["$: expected number"])
        self.assertEqual(validate(10**400, {"type": "integer"}), [])

    def test_non_dict_and_non_list_instances(self) -> None:
        self.assertEqual(validate([], TRIAL_SCHEMA), ["$: expected object"])
        d = trial().to_dict()
        d["graders"] = {}
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.graders: expected array"])

    def test_tool_calls_may_be_missing_null_or_a_count(self) -> None:
        d = trial().to_dict()
        for ok in (None, 0, 5):
            d["usage"]["tool_calls"] = ok
            self.assertEqual(validate(d, TRIAL_SCHEMA), [], repr(ok))
        del d["usage"]["tool_calls"]
        self.assertEqual(validate(d, TRIAL_SCHEMA), [])
        d["usage"]["tool_calls"] = -1
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.usage.tool_calls: must be >= 0"])

    def test_params_accept_free_keys(self) -> None:
        d = trial().to_dict()
        d["target"]["params"] = {"anything": 1}
        self.assertEqual(validate(d, TRIAL_SCHEMA), [])

    def test_schema_document_holds_both_schemas_with_ids(self) -> None:
        doc = schema_document()
        self.assertEqual((doc["trial"], doc["run"], doc["trial_spec"]), (TRIAL_SCHEMA, RUN_SCHEMA, TRIAL_SPEC_SCHEMA))
        self.assertTrue(TRIAL_SCHEMA["$id"].endswith("/trial/1.4.0"))
        self.assertTrue(RUN_SCHEMA["$id"].endswith("/run/1.4.0"))
        self.assertTrue(TRIAL_SPEC_SCHEMA["$id"].endswith("/trial_spec/1.4.0"))

    def test_a_trial_spec_validates_and_a_forged_one_does_not(self) -> None:
        self.assertEqual(validate(trial_spec().to_dict(), TRIAL_SPEC_SCHEMA), [])
        forged = {**trial_spec().to_dict(), "command": ["x"]}
        self.assertEqual(validate(forged, TRIAL_SPEC_SCHEMA), ["$.command: unexpected property"])
        self.assertEqual(validate({**trial_spec().to_dict(), "rep": 0}, TRIAL_SPEC_SCHEMA), ["$.rep: must be >= 1"])
        self.assertEqual(check_schema(TRIAL_SPEC_SCHEMA), TRIAL_SPEC_SCHEMA)


class ValidatorKeywordTests(unittest.TestCase):
    def test_unknown_keyword_raises(self) -> None:
        with self.assertRaises(ValueError) as cm:
            validate(1, {"type": "integer", "pattern": "x"})
        self.assertIn("pattern", str(cm.exception))

    def test_ref_resolves_against_defs(self) -> None:
        schema = {"$defs": {"n": {"type": "integer", "minimum": 2}}, "type": "array", "items": {"$ref": "#/$defs/n"}}
        self.assertEqual(validate([2, 1], schema), ["$[1]: must be >= 2"])

    def test_unresolvable_ref_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate(1, {"$ref": "#/$defs/missing"})
        with self.assertRaises(ValueError):
            validate(1, {"$ref": "http://elsewhere/x"})

    def test_type_list_with_null(self) -> None:
        schema = {"type": ["string", "null"]}
        self.assertEqual(validate(None, schema), [])
        self.assertEqual(validate("a", schema), [])
        self.assertEqual(validate(1, schema), ["$: expected string or null"])

    def test_unknown_type_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate(1, {"type": "color"})


class CheckSchemaTests(unittest.TestCase):
    def test_a_supported_schema_is_returned_as_it_is(self) -> None:
        schema = {
            "$defs": {"n": {"type": ["integer", "null"], "minimum": 0, "maximum": 5}},
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/n"}, "b": {"type": "array", "items": {"enum": [1, 2]}}},
            "required": ["a"],
            "additionalProperties": False,
        }
        self.assertIs(check_schema(schema), schema)
        self.assertEqual(check_schema(TRIAL_SCHEMA), TRIAL_SCHEMA)

    def test_every_malformed_or_unsupported_schema_is_refused(self) -> None:
        cycle = {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"}
        bad_schemas: list[object] = [
            [],
            "string",
            {"pattern": "x"},
            {"type": 3},
            {"type": [3]},
            {"type": "color"},
            {"required": "a"},
            {"required": [1]},
            {"enum": "a"},
            {"minimum": True},
            {"maximum": "9"},
            {"additionalProperties": {"type": "string"}},
            {"properties": []},
            {"$defs": 3},
            {"properties": {"a": []}},
            {"properties": {"a": {"format": "uri"}}},
            {"items": 3},
            {"items": {"minLength": 1}},
            {"$ref": 3},
            {"$ref": "#/$defs/missing"},
            {"$ref": "http://elsewhere/x"},
            {"$defs": {"a": 3}, "$ref": "#/$defs/a"},
            {"$defs": {"a": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"},
            cycle,
        ]
        for bad in bad_schemas:
            with self.assertRaises(ValueError, msg=repr(bad)):
                check_schema(bad)

    def test_a_ref_below_the_root_needs_the_root_defs(self) -> None:
        with self.assertRaises(ValueError):
            check_schema({"properties": {"a": {"$ref": "#/$defs/n"}}})

    def test_a_recursive_schema_through_properties_is_fine(self) -> None:
        tree = {
            "$defs": {
                "node": {"type": "object", "properties": {"kids": {"type": "array", "items": {"$ref": "#/$defs/node"}}}}
            },
            "$ref": "#/$defs/node",
        }
        self.assertIs(check_schema(tree), tree)


def _node(schema: Mapping[str, Any], name: str | None) -> Mapping[str, Any]:
    node: Mapping[str, Any] = schema if name is None else schema["$defs"][name]
    return node


class SchemaMatchesModelsTests(unittest.TestCase):
    """The schema is hand written: fail when a dataclass field is added or removed on one side only."""

    CASES: tuple[tuple[type[models._Model], Mapping[str, Any], str | None], ...] = (
        (models.TrialResult, TRIAL_SCHEMA, None),
        (models.Target, TRIAL_SCHEMA, "target"),
        (models.GraderResult, TRIAL_SCHEMA, "grader_result"),
        (models.Usage, TRIAL_SCHEMA, "usage"),
        (models.Costs, TRIAL_SCHEMA, "costs"),
        (models.RunInfo, RUN_SCHEMA, None),
        (models.TrialSpec, TRIAL_SPEC_SCHEMA, None),
        (models.Target, RUN_SCHEMA, "target"),
    )

    def test_properties_and_required_keys_match_the_dataclasses(self) -> None:
        for cls, schema, name in self.CASES:
            with self.subTest(model=cls.__name__, node=name):
                node = _node(schema, name)
                expected = {f.name for f in fields(cls)} | set(cls.DERIVED_KEYS)
                self.assertEqual(set(node["properties"]), expected)
                self.assertEqual(set(node["required"]), set(cls.required_keys()))
                self.assertIs(node["additionalProperties"], False)

    def test_schema_is_not_mutated_by_validation(self) -> None:
        before = copy.deepcopy(TRIAL_SCHEMA)
        validate(trial().to_dict(), TRIAL_SCHEMA)
        self.assertEqual(TRIAL_SCHEMA, before)


if __name__ == "__main__":
    unittest.main()

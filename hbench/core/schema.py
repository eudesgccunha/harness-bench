"""JSON Schema (draft 2020-12) for `trials.jsonl` lines and `run.json`, plus a small stdlib validator.

The schema is written by hand. `validate` supports only the keywords the schema uses and raises
`ValueError` on any other, so the schema cannot silently rely on something that is not checked.
`tests/test_core_schema.py` fails when the schema and the dataclass fields drift apart.
"""

import math
from collections.abc import Mapping
from typing import Any

from hbench.core.models import SCHEMA_VERSION, CostSource, TrialStatus

_ID_BASE = "https://harness-bench.local/schema"
_DIALECT = "https://json-schema.org/draft/2020-12/schema"

_STRING: dict[str, Any] = {"type": "string"}
_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}
_BOOLEAN: dict[str, Any] = {"type": "boolean"}
_COUNT: dict[str, Any] = {"type": "integer", "minimum": 0}
_COST: dict[str, Any] = {"type": ["number", "null"], "minimum": 0}


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": sorted(required), "additionalProperties": False}


_TARGET = _object(
    {
        "harness": _STRING,
        "harness_version": _STRING,
        "model": _STRING,
        # Free-form map of scalar settings: keys are not known in advance.
        "params": {"type": "object"},
    },
    ["harness", "harness_version", "model", "params"],
)

_GRADER_RESULT = _object(
    {
        "grader": _STRING,
        "grader_version": _STRING,
        "passed": _BOOLEAN,
        "score": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "timed_out": _BOOLEAN,
        "details_uri": _NULLABLE_STRING,
    },
    ["grader", "grader_version", "passed"],
)

_COSTS = _object({"gateway": _COST, "price_table": _COST, "harness": _COST}, [])

_USAGE = _object(
    {
        "input_tokens": _COUNT,
        "output_tokens": _COUNT,
        "reasoning_tokens": _COUNT,
        "cache_read_tokens": _COUNT,
        "cache_write_tokens": _COUNT,
        "turns": _COUNT,
        "parsed": _BOOLEAN,
        "costs": {"$ref": "#/$defs/costs"},
        "cost_usd": _COST,
        "cost_source": {"enum": [s.value for s in CostSource]},
    },
    ["input_tokens", "output_tokens", "parsed", "costs", "cost_usd", "cost_source"],
)

TRIAL_SCHEMA: dict[str, Any] = {
    "$schema": _DIALECT,
    "$id": f"{_ID_BASE}/trial/{SCHEMA_VERSION}",
    "title": "Trial result",
    "$defs": {"target": _TARGET, "grader_result": _GRADER_RESULT, "costs": _COSTS, "usage": _USAGE},
    **_object(
        {
            "schema_version": _STRING,
            "run_id": _STRING,
            "trial_id": _STRING,
            "task_id": _STRING,
            "suite_version": _STRING,
            "target": {"$ref": "#/$defs/target"},
            "rep": {"type": "integer", "minimum": 1},
            "status": {"enum": [s.value for s in TrialStatus]},
            "graders": {"type": "array", "items": {"$ref": "#/$defs/grader_result"}},
            "usage": {"$ref": "#/$defs/usage"},
            "latency_s": {"type": "number", "minimum": 0},
            "started_at": _STRING,
            "agent_exit_code": {"type": ["integer", "null"]},
            "sandboxed": _BOOLEAN,
            "image_digest": _NULLABLE_STRING,
            "trace_id": _NULLABLE_STRING,
            "artifact_uri": _NULLABLE_STRING,
        },
        [
            "schema_version",
            "run_id",
            "trial_id",
            "task_id",
            "suite_version",
            "target",
            "rep",
            "status",
            "graders",
            "usage",
            "latency_s",
            "started_at",
            "agent_exit_code",
            "sandboxed",
        ],
    ),
}

RUN_SCHEMA: dict[str, Any] = {
    "$schema": _DIALECT,
    "$id": f"{_ID_BASE}/run/{SCHEMA_VERSION}",
    "title": "Run info",
    "$defs": {"target": _TARGET},
    **_object(
        {
            "schema_version": _STRING,
            "run_id": _STRING,
            "started_at": _STRING,
            "finished_at": _NULLABLE_STRING,
            "harness_bench_version": _STRING,
            "config_sha256": _STRING,
            "suite_version": _STRING,
            "python": _STRING,
            "platform": _STRING,
            "sandboxed": _BOOLEAN,
            "targets": {"type": "array", "items": {"$ref": "#/$defs/target"}},
        },
        [
            "schema_version",
            "run_id",
            "started_at",
            "finished_at",
            "harness_bench_version",
            "config_sha256",
            "suite_version",
            "python",
            "platform",
            "sandboxed",
            "targets",
        ],
    ),
}


def schema_document() -> dict[str, Any]:
    """Both schemas in one document, as `schema export` prints it."""
    return {"trial": TRIAL_SCHEMA, "run": RUN_SCHEMA}


# ---------------------------------------------------------------- validator

_ANNOTATIONS = {"$schema", "$id", "title", "description"}
_KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "minimum",
    "maximum",
    "$defs",
    "$ref",
} | _ANNOTATIONS

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    # bool is a subclass of int in Python but is not a JSON number.
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    # JSON has no NaN or Infinity.
    "number": lambda v: (
        isinstance(v, int | float) and not isinstance(v, bool) and (isinstance(v, int) or math.isfinite(v))
    ),
}


def validate(instance: object, schema: Mapping[str, Any]) -> list[str]:
    """Errors found in `instance` (empty when it conforms), as `"$.path: message"` strings."""
    errors: list[str] = []
    _check(instance, schema, schema, "$", errors)
    return errors


def _resolve(ref: str, root: Mapping[str, Any]) -> Mapping[str, Any]:
    prefix = "#/$defs/"
    defs: Mapping[str, Any] = root.get("$defs", {})
    name = ref[len(prefix) :]
    if not ref.startswith(prefix) or name not in defs:
        raise ValueError(f"unresolvable $ref {ref!r}")
    resolved: Mapping[str, Any] = defs[name]
    return resolved


def _type_names(schema: Mapping[str, Any]) -> list[str]:
    declared = schema["type"]
    names: list[str] = [declared] if isinstance(declared, str) else list(declared)
    unknown = [n for n in names if n not in _TYPE_CHECKS]
    if unknown:
        raise ValueError(f"unsupported type {unknown[0]!r}")
    return names


def _check(instance: object, schema: Mapping[str, Any], root: Mapping[str, Any], path: str, errors: list[str]) -> None:
    unsupported = set(schema) - _KEYWORDS
    if unsupported:
        raise ValueError(f"unsupported schema keyword {sorted(unsupported)[0]!r}")
    if "$ref" in schema:
        _check(instance, _resolve(schema["$ref"], root), root, path, errors)
        return
    if "type" in schema:
        names = _type_names(schema)
        if not any(_TYPE_CHECKS[n](instance) for n in names):
            errors.append(f"{path}: expected {' or '.join(names)}")
            return
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']}")
    _check_bounds(instance, schema, path, errors)
    if isinstance(instance, dict):
        _check_object(instance, schema, root, path, errors)
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            _check(item, schema["items"], root, f"{path}[{i}]", errors)


def _check_bounds(instance: object, schema: Mapping[str, Any], path: str, errors: list[str]) -> None:
    if isinstance(instance, bool) or not isinstance(instance, int | float):
        return
    if "minimum" in schema and instance < schema["minimum"]:
        errors.append(f"{path}: must be >= {schema['minimum']}")
    if "maximum" in schema and instance > schema["maximum"]:
        errors.append(f"{path}: must be <= {schema['maximum']}")


def _check_object(
    instance: dict[str, Any], schema: Mapping[str, Any], root: Mapping[str, Any], path: str, errors: list[str]
) -> None:
    properties: Mapping[str, Any] = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in instance:
            errors.append(f"{path}: missing required property {key!r}")
    for key, value in instance.items():
        if key in properties:
            _check(value, properties[key], root, f"{path}.{key}", errors)
        elif schema.get("additionalProperties") is False:
            errors.append(f"{path}.{key}: unexpected property")

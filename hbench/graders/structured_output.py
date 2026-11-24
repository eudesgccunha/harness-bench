"""Grader for a JSON artifact: the file must parse and satisfy a JSON Schema."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hbench.core.models import GraderResult
from hbench.core.schema import check_schema, validate
from hbench.graders._files import asset_root, read_artifact
from hbench.graders._options import cap_violations, reject_unknown, string_option
from hbench.graders.base import GradeContext


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _load(text: str) -> Any:
    """Strict JSON: no `NaN` or `Infinity`. Raises ValueError (also for a document nested too deep)."""
    try:
        return json.loads(text, parse_constant=_no_constant)
    except RecursionError as e:
        raise ValueError("nested too deep") from e


def load_schema_file(root: Path, rel: str) -> Mapping[str, Any]:
    """The checked JSON Schema in the file `rel` under `root` (a suite or benchmark directory). Raises ValueError
    when the file cannot be read, is not JSON or uses a keyword `hbench.core.schema` does not support."""
    return check_schema(_load(read_artifact(root, rel)))


class StructuredOutputGrader:
    """Options: `file` (the JSON file in the workdir) and `schema`: a JSON Schema object written in the config, or
    the path of a schema file relative to the suite directory (the benchmark directory for a config task). Only the
    keywords `hbench.core.schema` supports are allowed; another one is a config error (a file is checked when the
    grader reads it)."""

    name = "structured_output"
    version = "1"

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        reject_unknown(cfg, {"file", "schema"}, self.name)
        self._file = string_option(cfg, "file", self.name)
        schema = cfg.get("schema")
        if isinstance(schema, Mapping):
            check_schema(schema)
        elif not isinstance(schema, str) or not schema:
            raise ValueError(f"{self.name}: 'schema' must be a JSON Schema object or the path of a schema file")
        self._schema = schema

    def _load_schema(self, ctx: GradeContext) -> Mapping[str, Any]:
        if isinstance(self._schema, Mapping):
            return self._schema
        return load_schema_file(asset_root(ctx.task, ctx.base), self._schema)

    def violations(self, ctx: GradeContext) -> list[str]:
        """One message per problem: an unreadable file or schema, invalid JSON, or each schema error."""
        try:
            schema = self._load_schema(ctx)
        except ValueError as e:
            return [f"schema: {e}"]
        try:
            document = _load(read_artifact(ctx.workdir, self._file))
        except ValueError as e:
            return [f"{self._file}: {e}"]
        return cap_violations(validate(document, schema))

    def grade(self, ctx: GradeContext) -> GraderResult:
        found = self.violations(ctx)
        ctx.log("".join(message + "\n" for message in found))
        passed = not found
        return GraderResult(grader=self.name, grader_version=self.version, passed=passed, score=1.0 if passed else 0.0)

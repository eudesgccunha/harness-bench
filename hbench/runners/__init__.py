"""Harness runners: the protocol, the built-in output parsers and the registry (ADR 0007)."""

from collections.abc import Mapping
from typing import Any, TypeGuard

from hbench.plugins import RUNNER_GROUP, create_plugin

from hbench.runners._usage import Usage, empty_usage, json_lines, num
from hbench.runners.base import HarnessRunner, ParseUsage, TemplateRunner
from hbench.runners.claude import parse_claude
from hbench.runners.codex import parse_codex
from hbench.runners.cursor import parse_cursor
from hbench.runners.null import parse_null
from hbench.runners.opencode import parse_opencode
from hbench.runners.pi import parse_pi

__all__ = [
    "PARSERS",
    "HarnessRunner",
    "TemplateRunner",
    "Usage",
    "empty_usage",
    "json_lines",
    "num",
    "parse_claude",
    "parse_codex",
    "parse_cursor",
    "parse_null",
    "parse_opencode",
    "parse_pi",
    "runner_for",
]

PARSERS: Mapping[str, ParseUsage] = {
    "claude": parse_claude,
    "opencode": parse_opencode,
    "pi": parse_pi,
    "cursor": parse_cursor,
    "codex": parse_codex,
    "null": parse_null,
}


def _is_runner(obj: object) -> TypeGuard[HarnessRunner]:
    return isinstance(obj, HarnessRunner)


def runner_for(harness_cfg: Mapping[str, Any]) -> HarnessRunner:
    """The runner of a harness config entry: a built-in parser, else the plugin of that name.
    Raises ValueError (a `PluginError` for an unknown name) when the entry is unusable."""
    parser = harness_cfg.get("parser")
    if not isinstance(parser, str):
        raise ValueError("a harness needs a 'parser' name")
    if parser in PARSERS:
        return TemplateRunner(harness_cfg, PARSERS[parser])
    return create_plugin(RUNNER_GROUP, parser, PARSERS, harness_cfg, _is_runner, "HarnessRunner")

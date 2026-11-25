"""Sinks: the protocol, the built-in types and the registry (ADR 0011).

`sinks_for(cfg, base)` is the only way the engine gets its sinks: for each entry of the `sinks` list, the built-in
type of `type`, else the plugin of that name in the entry-point group `harness_bench.sinks`. Building a sink does no
I/O; `Sink.open` does.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeGuard

from hbench.plugins import SINK_GROUP, create_plugin
from hbench.sinks.base import Sink, SinkError
from hbench.sinks.otlp import OtlpSink
from hbench.sinks.sqlite import SqliteSink

__all__ = ["BUILTIN_TYPES", "Sink", "SinkError", "sink_secret_vars", "sinks_for"]

BUILTIN_TYPES = ("sqlite", "otlp")
_ALLOWED_KEYS: Mapping[str, frozenset[str]] = {
    "sqlite": frozenset({"type", "path"}),
    "otlp": frozenset({"type", "endpoint", "headers", "timeout"}),
}
_ENV_REF = re.compile(r"\$\{(\w+)\}")


def _is_sink(obj: object) -> TypeGuard[Sink]:
    return isinstance(obj, Sink)


def _entries(cfg: Mapping[str, Any]) -> list[Any]:
    section = cfg.get("sinks")
    if section is None:
        return []
    if not isinstance(section, list):
        raise SinkError("sinks must be a list of objects")
    return section


def _build(entry: object, base: Path, out_dir: Path) -> Sink:
    if not isinstance(entry, Mapping):
        raise SinkError("must be an object")
    kind = entry.get("type")
    if not isinstance(kind, str) or not kind:
        raise SinkError("type must be a non-empty string")
    if kind in _ALLOWED_KEYS:
        unknown = sorted(set(entry) - _ALLOWED_KEYS[kind] - {"_comment"})
        if unknown:
            raise SinkError(f"unknown keys {unknown} (a {kind} sink takes {sorted(_ALLOWED_KEYS[kind])})")
    if kind == "sqlite":
        return SqliteSink.from_section(entry, base)
    if kind == "otlp":
        return OtlpSink.from_section(entry, out_dir)
    return create_plugin(SINK_GROUP, kind, BUILTIN_TYPES, entry, _is_sink, "Sink")


def sinks_for(cfg: Mapping[str, Any], base: Path) -> list[Sink]:
    """The sinks of the `sinks` list of `cfg`; paths are relative to `base`, the directory of the config. Raises
    `SinkError` or `PluginError` (both `ValueError`) for a bad entry; the message names the entry."""
    out_dir = base / cfg.get("output_dir", "results")
    sinks = []
    for index, entry in enumerate(_entries(cfg)):
        try:
            sinks.append(_build(entry, base, out_dir))
        except ValueError as e:
            raise type(e)(f"sinks[{index}]: {e}") from e
    return sinks


def sink_secret_vars(cfg: Mapping[str, Any]) -> set[str]:
    """The environment variables named by the header values of the sinks: secrets that never go to an agent."""
    names: set[str] = set()
    for entry in cfg.get("sinks") or []:
        headers = entry.get("headers") if isinstance(entry, Mapping) else None
        if isinstance(headers, Mapping):
            for value in headers.values():
                names |= set(_ENV_REF.findall(value)) if isinstance(value, str) else set()
    return names

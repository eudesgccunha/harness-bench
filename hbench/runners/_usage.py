"""Helpers shared by the output parsers: numeric lookup, JSONL reading and the empty usage dict."""

import json
from collections.abc import Iterator
from typing import Any

Usage = dict[str, Any]


def num(d: object, *keys: str) -> int | float:
    """First numeric value found under any of keys, else 0."""
    if not isinstance(d, dict):
        return 0
    for k in keys:
        v = d.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool):
            return v
    return 0


def json_lines(text: str) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from a JSONL stream; skip non-JSON lines."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def empty_usage() -> Usage:
    return {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cost_reported": None,
        "turns": 0,
        "tool_calls": None,
    }

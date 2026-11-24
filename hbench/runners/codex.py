"""Parser for `codex exec --json`."""

from typing import Any

from hbench.runners._usage import Usage, empty_usage, json_lines, num

# `item.completed` item types that are the agent acting on the world (not messages or reasoning).
TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search"}


def _item_type(event: dict[str, Any]) -> object:
    item = event.get("item")
    return item.get("type") if isinstance(item, dict) else None


def parse_codex(stdout: str) -> Usage | None:
    """codex exec --json: usage on turn.completed is the thread's running total, so take
    the last one. cached_input_tokens is a subset of input_tokens; reasoning_output_tokens
    is a subset of output_tokens (not added again)."""
    last, turns = None, 0
    tool_calls: int | None = None  # unknown until an `item.completed` event shows this CLI version reports items
    for obj in json_lines(stdout):
        if obj.get("type") == "turn.completed" and isinstance(obj.get("usage"), dict):
            last = obj["usage"]
            turns += 1
        elif obj.get("type") == "item.completed":
            tool_calls = (tool_calls or 0) + (_item_type(obj) in TOOL_ITEMS)
    if last is None:
        return None
    u = empty_usage()
    cached = num(last, "cached_input_tokens")
    u["input"] = max(num(last, "input_tokens") - cached, 0)
    u["cache_read"] = cached
    u["output"] = num(last, "output_tokens")
    u["turns"] = turns
    u["tool_calls"] = tool_calls
    return u

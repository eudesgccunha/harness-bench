"""Parser for `claude -p --output-format json`."""

import json

from collections.abc import Iterable
from typing import Any

from hbench.runners._usage import Usage, empty_usage, json_lines, num


def _count_tool_calls(events: Iterable[dict[str, Any]]) -> int | None:
    """Unique `tool_use` blocks over the stream-json `assistant` events; None when there are no such events
    (the single-result JSON format does not list tool calls)."""
    seen_assistant, ids, anonymous = False, set(), 0
    for event in events:
        if event.get("type") != "assistant":
            continue
        seen_assistant = True
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if block.get("id") is None:
                    anonymous += 1
                else:
                    ids.add(str(block["id"]))
    return len(ids) + anonymous if seen_assistant else None


def parse_claude(stdout: str) -> Usage | None:
    """claude -p --output-format json (single result object; stream-json also works)."""
    result = None
    events = list(json_lines(stdout))
    for obj in events:
        if obj.get("type") == "result":
            result = obj
    if result is None:
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            return None
    u = empty_usage()
    model_usage = result.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        # Includes side calls (subagents, small helper model), not only the main model.
        for m in model_usage.values():
            u["input"] += num(m, "inputTokens")
            u["output"] += num(m, "outputTokens")
            u["cache_read"] += num(m, "cacheReadInputTokens")
            u["cache_write"] += num(m, "cacheCreationInputTokens")
    else:
        usage = result.get("usage", {})
        u["input"] = num(usage, "input_tokens")
        u["output"] = num(usage, "output_tokens")
        u["cache_read"] = num(usage, "cache_read_input_tokens")
        u["cache_write"] = num(usage, "cache_creation_input_tokens")
    if isinstance(result.get("total_cost_usd"), int | float):
        u["cost_reported"] = result["total_cost_usd"]
    u["turns"] = num(result, "num_turns")
    u["tool_calls"] = _count_tool_calls(events)
    return u

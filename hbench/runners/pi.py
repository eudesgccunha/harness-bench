"""Parser for `pi --mode json`."""

from hbench.runners._usage import Usage, empty_usage, json_lines, num


def parse_pi(stdout: str) -> Usage | None:
    """pi --mode json: sum usage of assistant messages on message_end events only."""
    u = empty_usage()
    cost, seen, has_cost = 0.0, False, False
    tool_calls: int | None = None
    for obj in json_lines(stdout):
        if obj.get("type") != "message_end":
            continue
        msg = obj.get("message", {}) or {}
        if msg.get("role") != "assistant":
            continue
        usage = msg.get("usage", {}) or {}
        u["input"] += num(usage, "input", "inputTokens", "input_tokens")
        u["output"] += num(usage, "output", "outputTokens", "output_tokens")
        u["cache_read"] += num(usage, "cacheRead", "cache_read")
        u["cache_write"] += num(usage, "cacheWrite", "cache_write")
        c = usage.get("cost")
        if isinstance(c, dict) and isinstance(c.get("total"), int | float):
            cost += c["total"]
            has_cost = True
        elif isinstance(c, int | float):
            cost += c
            has_cost = True
        u["turns"] += 1
        seen = True
        content = msg.get("content")
        if isinstance(content, list):
            calls = sum(1 for block in content if isinstance(block, dict) and block.get("type") == "toolCall")
            tool_calls = (tool_calls or 0) + calls
    if not seen:
        return None
    u["cost_reported"] = cost if has_cost else None
    u["tool_calls"] = tool_calls
    return u

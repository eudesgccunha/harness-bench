"""Parser for `opencode run --format json`."""

from hbench.runners._usage import Usage, empty_usage, json_lines, num


def parse_opencode(stdout: str) -> Usage | None:
    """opencode run --format json: sum every step_finish event."""
    u = empty_usage()
    cost, seen = 0.0, False
    tool_calls: int | None = None  # unknown until a `tool_use` event shows this CLI version reports them
    for obj in json_lines(stdout):
        kind = str(obj.get("type", "")).replace("-", "_")
        if kind == "tool_use":
            tool_calls = (tool_calls or 0) + 1
        if kind != "step_finish":
            continue
        part = obj.get("part", obj)
        tok = part.get("tokens", {}) or {}
        cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
        u["input"] += num(tok, "input")
        u["output"] += num(tok, "output")
        u["reasoning"] += num(tok, "reasoning")
        u["cache_read"] += num(cache, "read") or num(tok, "cacheRead", "cache_read")
        u["cache_write"] += num(cache, "write") or num(tok, "cacheWrite", "cache_write")
        cost += num(part, "cost")
        u["turns"] += 1
        seen = True
    if not seen:
        return None
    u["cost_reported"] = cost
    u["tool_calls"] = tool_calls
    return u

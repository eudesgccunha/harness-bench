"""Parser for the `null` negative-control harness."""

from hbench.runners._usage import Usage, empty_usage


def parse_null(stdout: str) -> Usage:
    """Negative control: no model is called, so usage is zero and the cost is a real 0.0.
    A `null` pair that solves a task means the hidden tests are too lenient."""
    u = empty_usage()
    u["cost_reported"] = 0.0
    u["tool_calls"] = 0
    return u

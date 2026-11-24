"""Report derived from the contract: aggregate `TrialResult`s per harness x model pair, render Markdown and CSV."""

import csv
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from hbench.core.models import CostSource, TrialResult

# The report keeps the names used by `cost_source` in configs and CLI flags.
_SOURCE_BY_NAME: Mapping[str, CostSource | None] = {
    "auto": None,
    "litellm": CostSource.GATEWAY,
    "computed": CostSource.PRICE_TABLE,
    "reported": CostSource.HARNESS,
}
COST_SOURCES = tuple(_SOURCE_BY_NAME)
_NAME_BY_SOURCE: Mapping[CostSource, str] = {s: n for n, s in _SOURCE_BY_NAME.items() if s is not None}


def fmt_money(v: float | None) -> str:
    if v is None:
        return "n/a"
    if math.isinf(v):
        return "inf"
    return f"${v:.4f}" if v < 1 else f"${v:.2f}"


def fmt_int(v: float | None) -> str:
    return "n/a" if v is None or math.isinf(v) else f"{v:,.0f}"


def _pair_entry(harness: str, model: str, trials: Sequence[TrialResult], source: CostSource | None) -> dict[str, Any]:
    n = len(trials)
    solved = sum(1 for t in trials if t.passed)
    tokens = sum(t.usage.total_tokens for t in trials)
    fresh_in = sum(t.usage.input_tokens for t in trials)
    cache_read = sum(t.usage.cache_read_tokens for t in trials)
    picked = [t.usage.costs.pick(source) for t in trials]
    costs = [c for c, _ in picked]
    cost = None if any(c is None for c in costs) else sum(c for c in costs if c is not None)
    names = sorted({_NAME_BY_SOURCE[s] for c, s in picked if c is not None})
    return {
        "harness": harness,
        "model": model,
        "cost_source": "+".join(names) or "n/a",
        "runs": n,
        "solved": solved,
        "pass_rate": solved / n,
        "tokens_total": tokens,
        "tokens_per_run": tokens / n,
        "tokens_per_solved": tokens / solved if solved else math.inf,
        "cache_hit": cache_read / (cache_read + fresh_in) if cache_read + fresh_in else 0,
        "cost_total": cost,
        "cost_per_run": cost / n if cost is not None else None,
        "cost_per_solved": (cost / solved if solved else math.inf) if cost is not None else None,
        "cost_per_1m_tokens": cost / tokens * 1e6 if cost is not None and tokens else None,
        "avg_turns": sum(t.usage.turns for t in trials) / n,
        "avg_wall_s": sum(t.latency_s for t in trials) / n,
        "parse_failures": sum(1 for t in trials if not t.usage.parsed),
    }


def aggregate(trials: Sequence[TrialResult], cost_source: str) -> list[dict[str, Any]]:
    """One entry per harness x model pair, cheapest per solved task first (unknown costs last)."""
    if cost_source not in _SOURCE_BY_NAME:
        raise ValueError(f"unknown cost source {cost_source!r}: use one of {', '.join(COST_SOURCES)}")
    source = _SOURCE_BY_NAME[cost_source]
    groups: dict[tuple[str, str], list[TrialResult]] = {}
    for t in trials:
        groups.setdefault((t.target.harness, t.target.model), []).append(t)
    table = [_pair_entry(h, m, ts, source) for (h, m), ts in groups.items()]

    def sort_key(entry: dict[str, Any]) -> tuple[bool, float, float]:
        c = entry["cost_per_solved"]
        return (c is None, c if c is not None else 0, -entry["pass_rate"])

    table.sort(key=sort_key)
    return table


def write_csv(table: Sequence[Mapping[str, Any]], path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(table[0].keys()))
        w.writeheader()
        w.writerows(table)


_HEAD = (
    "| # | Harness | Model | Solved | Pass rate | Cost/solved | Cost/run "
    "| Cost/1M tokens | Tokens/solved | Tokens/run | Cache hit | Turns | Time (s) "
    "| Cost source |"
)


def _table_line(i: int, t: Mapping[str, Any]) -> str:
    return (
        f"| {i} | {t['harness']} | {t['model']} | {t['solved']}/{t['runs']} "
        f"| {t['pass_rate']:.0%} | {fmt_money(t['cost_per_solved'])} | {fmt_money(t['cost_per_run'])} "
        f"| {fmt_money(t['cost_per_1m_tokens'])} | {fmt_int(t['tokens_per_solved'])} "
        f"| {fmt_int(t['tokens_per_run'])} | {t['cache_hit']:.0%} | {t['avg_turns']:.1f} "
        f"| {t['avg_wall_s']:.0f} | {t['cost_source']} |"
    )


def _per_task_lines(table: Sequence[Mapping[str, Any]], trials: Sequence[TrialResult]) -> list[str]:
    pairs = [(t["harness"], t["model"]) for t in table]
    lines = [
        "",
        "## Per task (solved/runs)",
        "",
        "| Task | " + " | ".join(f"{h}<br>{m}" for h, m in pairs) + " |",
        "|---|" + "---|" * len(pairs),
    ]
    for task in sorted({t.task_id for t in trials}):
        cells = []
        for h, m in pairs:
            rs = [t for t in trials if t.task_id == task and t.target.harness == h and t.target.model == m]
            cells.append(f"{sum(t.passed for t in rs)}/{len(rs)}" if rs else "-")
        lines.append(f"| {task} | " + " | ".join(cells) + " |")
    return lines


def render_markdown(
    table: Sequence[Mapping[str, Any]], trials: Sequence[TrialResult], cost_source: str, now: datetime
) -> str:
    lines = [
        f"# Harness x model — {now.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"Cost source: `{cost_source}` (auto = litellm > computed > reported). Sorted by cost per solved task.",
        "",
        _HEAD,
        "|" + "---|" * 14,
    ]
    lines += [_table_line(i, t) for i, t in enumerate(table, 1)]
    bad = [t for t in table if t["parse_failures"]]
    if bad:
        lines += [
            "",
            "**Warning:** no token data from the harness for: "
            + ", ".join(f"{t['harness']}/{t['model']} ({t['parse_failures']})" for t in bad)
            + ". See `results/logs/*.agent.jsonl`.",
        ]
    lines += _per_task_lines(table, trials)
    return "\n".join(lines) + "\n"

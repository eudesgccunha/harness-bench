"""Report derived from the contract: aggregate `TrialResult`s per harness x model pair, render Markdown and CSV."""

import csv
import math
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from hbench.core.models import CostSource, TrialResult
from hbench.stats import (
    MAX_RUNS,
    Interval,
    bootstrap_ratio_ci,
    mcnemar_exact,
    mean_over_tasks,
    pareto_front,
    pass_at_k,
    pass_hat_k,
    reps_for_ci_width,
    tie_leaders,
    wilson_interval,
)

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


def _task_counts(trials: Sequence[TrialResult]) -> list[tuple[int, int]]:
    """`(runs, passes)` per task."""
    per_task: dict[str, list[bool]] = {}
    for t in trials:
        per_task.setdefault(t.task_id, []).append(t.passed)
    return [(len(outcomes), sum(outcomes)) for outcomes in per_task.values()]


def _cost_interval(
    harness: str, model: str, trials: Sequence[TrialResult], costs: Sequence[float | None]
) -> Interval | None:
    """Bootstrap interval of cost per solved task; none when any cost is unknown or nothing passed."""
    known = [c for c in costs if c is not None]
    if len(known) != len(costs) or not any(t.passed for t in trials):
        return None
    return bootstrap_ratio_ci(known, [t.passed for t in trials], f"{harness}|{model}")


def _pair_entry(
    harness: str,
    model: str,
    trials: Sequence[TrialResult],
    source: CostSource | None,
    *,
    null: bool,
    target_ci_width: float,
) -> dict[str, Any]:
    n = len(trials)
    solved = sum(1 for t in trials if t.passed)
    tokens = sum(t.usage.total_tokens for t in trials)
    fresh_in = sum(t.usage.input_tokens for t in trials)
    cache_read = sum(t.usage.cache_read_tokens for t in trials)
    picked = [t.usage.costs.pick(source) for t in trials]
    costs = [c for c, _ in picked]
    cost = None if any(c is None for c in costs) else sum(c for c in costs if c is not None)
    names = sorted({_NAME_BY_SOURCE[s] for c, s in picked if c is not None})
    counts = _task_counts(trials)
    k = min(runs for runs, _ in counts)
    pass_low, pass_high = wilson_interval(solved, n)
    cost_ci = _cost_interval(harness, model, trials, costs)
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
        "pass_ci_low": pass_low,
        "pass_ci_high": pass_high,
        "k": k,
        "pass_at_k": mean_over_tasks(counts, k, pass_at_k),
        "pass_hat_k": mean_over_tasks(counts, k, pass_hat_k),
        "cost_per_solved_ci_low": cost_ci[0] if cost_ci else None,
        "cost_per_solved_ci_high": cost_ci[1] if cost_ci else None,
        "tie_with": None,
        "pareto": False,
        "reps_needed": reps_for_ci_width(solved / n, target_ci_width, len(counts)),
        "null_control": null,
    }


def _mark_ties_and_frontier(table: list[dict[str, Any]]) -> None:
    """Fill `tie_with` and `pareto` once the table is sorted. Null pairs and unknown costs take part in neither."""
    intervals: list[Interval | None] = [
        None
        if e["null_control"] or e["cost_per_solved_ci_low"] is None
        else (e["cost_per_solved_ci_low"], e["cost_per_solved_ci_high"])
        for e in table
    ]
    for entry, leader in zip(table, tie_leaders(intervals), strict=True):
        entry["tie_with"] = leader
    eligible = [e for e in table if e["cost_per_run"] is not None and e["solved"] and not e["null_control"]]
    on_front = pareto_front([(e["cost_per_run"], e["pass_rate"]) for e in eligible])
    for entry, flag in zip(eligible, on_front, strict=True):
        entry["pareto"] = flag


def aggregate(
    trials: Sequence[TrialResult],
    cost_source: str,
    *,
    null_harnesses: Collection[str] = (),
    target_ci_width: float = 0.2,
) -> list[dict[str, Any]]:
    """One entry per harness x model pair, cheapest per solved task first (unknown costs next, negative controls last).

    Besides the cost and token figures, each entry carries the uncertainty of its pass rate and cost, ties,
    the Pareto frontier and the repetitions needed for a pass-rate interval of at most `target_ci_width`.
    """
    if cost_source not in _SOURCE_BY_NAME:
        raise ValueError(f"unknown cost source {cost_source!r}: use one of {', '.join(COST_SOURCES)}")
    source = _SOURCE_BY_NAME[cost_source]
    groups: dict[tuple[str, str], list[TrialResult]] = {}
    for t in trials:
        groups.setdefault((t.target.harness, t.target.model), []).append(t)
    table = [
        _pair_entry(h, m, ts, source, null=h in null_harnesses, target_ci_width=target_ci_width)
        for (h, m), ts in groups.items()
    ]

    def sort_key(entry: dict[str, Any]) -> tuple[bool, bool, float, float]:
        c = entry["cost_per_solved"]
        return (entry["null_control"], c is None, c if c is not None else 0, -entry["pass_rate"])

    table.sort(key=sort_key)
    _mark_ties_and_frontier(table)
    return table


def write_csv(table: Sequence[Mapping[str, Any]], path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(table[0].keys()))
        w.writeheader()
        w.writerows(table)


_HEAD = (
    "| # | Harness | Model | Solved | Pass rate | Cost/solved | Cost/run "
    "| Cost/1M tokens | Tokens/solved | Tokens/run | Cache hit | Turns | Time (s) "
    "| Cost source | Pass 95% CI | pass@k | pass^k | Cost/solved 95% CI | Tie | Pareto |"
)
_COLUMNS = 20


def _range(low: float | None, high: float | None, fmt: Callable[[float], str], unit: str = "") -> str:
    return "n/a" if low is None or high is None else f"{fmt(low)}–{fmt(high)}{unit}"


def _table_line(i: int, t: Mapping[str, Any]) -> str:
    return (
        f"| {i} | {t['harness']} | {t['model']} | {t['solved']}/{t['runs']} "
        f"| {t['pass_rate']:.0%} | {fmt_money(t['cost_per_solved'])} | {fmt_money(t['cost_per_run'])} "
        f"| {fmt_money(t['cost_per_1m_tokens'])} | {fmt_int(t['tokens_per_solved'])} "
        f"| {fmt_int(t['tokens_per_run'])} | {t['cache_hit']:.0%} | {t['avg_turns']:.1f} "
        f"| {t['avg_wall_s']:.0f} | {t['cost_source']} "
        f"| {_range(t['pass_ci_low'], t['pass_ci_high'], lambda v: f'{v * 100:.0f}', '%')} "
        f"| {t['pass_at_k']:.0%} (k={t['k']}) | {t['pass_hat_k']:.0%} (k={t['k']}) "
        f"| {_range(t['cost_per_solved_ci_low'], t['cost_per_solved_ci_high'], fmt_money)} "
        f"| {'' if t['tie_with'] is None else f'={t["tie_with"]}'} | {'yes' if t['pareto'] else ''} |"
    )


def _name(t: Mapping[str, Any]) -> str:
    return f"{t['harness']}/{t['model']}"


def _recommendation_lines(table: Sequence[Mapping[str, Any]], within_pp: float) -> list[str]:
    """The cheapest pair per solved task among those within `within_pp` points of the best pass rate."""
    real = [t for t in table if not t["null_control"]]
    if not real:
        return []
    best = max(t["pass_rate"] for t in real)
    eligible = [
        t
        for t in real
        if t["solved"] and t["cost_per_solved"] is not None and t["pass_rate"] >= best - within_pp / 100 - 1e-9
    ]
    if not eligible:
        return []
    pick = min(eligible, key=lambda t: t["cost_per_solved"])
    return [
        "",
        f"**Recommendation:** {_name(pick)} — cheapest per solved task within {within_pp:g} pp "
        f"of the best pass rate ({best:.0%}).",
    ]


def _pareto_lines(table: Sequence[Mapping[str, Any]]) -> list[str]:
    front = [t for t in table if t["pareto"]]
    if not front:
        return []
    lines = ["", "## Pareto frontier (cost per run × pass rate)", ""]
    return lines + [f"- {_name(t)}: {fmt_money(t['cost_per_run'])} per run, {t['pass_rate']:.0%}" for t in front]


def _null_control_lines(table: Sequence[Mapping[str, Any]]) -> list[str]:
    nulls = [t for t in table if t["null_control"]]
    if not nulls:
        return []
    passed = [t for t in nulls if t["solved"]]
    lines = ["", "## Negative control", ""]
    if not passed:
        return lines + ["null control solved nothing."]
    names = ", ".join(f"{_name(t)} ({t['solved']}/{t['runs']})" for t in passed)
    return lines + [f"**Warning:** null control solved tasks: {names}. The hidden tests are too lenient."]


def _real_trials(trials: Sequence[TrialResult], null_harnesses: Collection[str]) -> list[TrialResult]:
    """Trials of the harnesses that are not a negative control."""
    return [t for t in trials if t.target.harness not in null_harnesses]


def task_discrimination(trials: Sequence[TrialResult]) -> tuple[list[str], list[str]]:
    """Task ids solved in every run (saturated) and in none (unsolved), over `trials`."""
    outcomes: dict[str, list[bool]] = {}
    for t in trials:
        outcomes.setdefault(t.task_id, []).append(t.passed)
    saturated = sorted(task for task, o in outcomes.items() if all(o))
    unsolved = sorted(task for task, o in outcomes.items() if not any(o))
    return saturated, unsolved


def drop_saturated(trials: Sequence[TrialResult], null_harnesses: Collection[str] = ()) -> list[TrialResult]:
    """`trials` without the tasks every non-null run solved: they cannot tell the pairs apart."""
    saturated, _ = task_discrimination(_real_trials(trials, null_harnesses))
    return [t for t in trials if t.task_id not in saturated]


def _discrimination_lines(table: Sequence[Mapping[str, Any]], trials: Sequence[TrialResult]) -> list[str]:
    null = {t["harness"] for t in table if t["null_control"]}
    saturated, unsolved = task_discrimination(_real_trials(trials, null))
    lines = []
    if saturated:
        lines.append(f"- Saturated (solved in every run): {', '.join(saturated)}")
    if unsolved:
        lines.append(f"- Unsolved (never solved): {', '.join(unsolved)}")
    return ["", "## Discrimination", ""] + lines if lines else []


def _outcomes(trials: Sequence[TrialResult], pair: tuple[str, str]) -> dict[tuple[str, int], bool]:
    return {(t.task_id, t.rep): t.passed for t in trials if (t.target.harness, t.target.model) == pair}


def _paired_lines(table: Sequence[Mapping[str, Any]], trials: Sequence[TrialResult]) -> list[str]:
    """Exact McNemar test between the best-ranked real pair and every other real pair, pairing runs by task and rep."""
    real = [(rank, t) for rank, t in enumerate(table, 1) if not t["null_control"]]
    if len(real) < 2:
        return []
    (ref_rank, ref), others = real[0], real[1:]
    ref_out = _outcomes(trials, (ref["harness"], ref["model"]))
    lines = []
    for rank, t in others:
        out = _outcomes(trials, (t["harness"], t["model"]))
        shared = ref_out.keys() & out.keys()
        if not shared:
            continue
        b = sum(ref_out[k] and not out[k] for k in shared)
        c = sum(out[k] and not ref_out[k] for k in shared)
        lines.append(f"- #{rank} {_name(t)}: b={b}, c={c}, p={mcnemar_exact(b, c):.4f}")
    if not lines:
        return []
    note = f"b: only #{ref_rank} solved the run; c: only the other pair did."
    return ["", f"## Paired comparison vs #{ref_rank} (McNemar, exact)", "", note, ""] + lines


def _reps_text(t: Mapping[str, Any], trials: Sequence[TrialResult]) -> str:
    if t["reps_needed"] is not None:
        return str(t["reps_needed"])
    own = [x for x in trials if (x.target.harness, x.target.model) == (t["harness"], t["model"])]
    return f"more than {MAX_RUNS // len(_task_counts(own))}"


def _repetitions_lines(
    table: Sequence[Mapping[str, Any]], trials: Sequence[TrialResult], target_ci_width: float
) -> list[str]:
    real = [t for t in table if not t["null_control"]]
    if not real:
        return []
    lines = [
        f"- To reach a pass-rate CI width of {target_ci_width:.2f}: "
        f"{_name(t)} needs {_reps_text(t, trials)} reps per task."
        for t in real
    ]
    return ["", "## Repetitions", ""] + lines


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
    table: Sequence[Mapping[str, Any]],
    trials: Sequence[TrialResult],
    cost_source: str,
    now: datetime,
    *,
    recommend_within_pp: float = 5.0,
    target_ci_width: float = 0.2,
) -> str:
    lines = [
        f"# Harness x model — {now.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"Cost source: `{cost_source}` (auto = litellm > computed > reported). Sorted by cost per solved task.",
        "",
        _HEAD,
        "|" + "---|" * _COLUMNS,
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
    lines += _recommendation_lines(table, recommend_within_pp)
    lines += _pareto_lines(table)
    lines += _null_control_lines(table)
    lines += _discrimination_lines(table, trials)
    lines += _paired_lines(table, trials)
    lines += _repetitions_lines(table, trials, target_ci_width)
    lines += _per_task_lines(table, trials)
    return "\n".join(lines) + "\n"

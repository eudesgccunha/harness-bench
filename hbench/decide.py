"""From benchmark to decision (ADR 0013): recommend a target under constraints, write it as a versionable file and
gate CI on it, and compare two runs. Pure functions over `RunInfo` and `TrialResult`: no network, and files are only
touched by `load_target` and `write_target`. Every figure comes from `report.aggregate`, so these commands and the
report never disagree. Unknown cost is `None`, never 0: it never meets a cost constraint.
"""

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hbench.core.models import CONSTRAINT_KEYS, ContractError, Evidence, RunInfo, TargetDecision, TrialResult
from hbench.graders._files import read_artifact
from hbench.report import aggregate, fmt_money
from hbench.results import atomic_write
from hbench.stats import mcnemar_exact, wilson_interval

MAX_TARGET_BYTES = 64 * 1024  # a decision is a few hundred bytes; a bigger file is not one


class TargetFileError(ValueError):
    """A target file cannot be read, is not a valid `TargetDecision`, or cannot be written."""


def _name(harness: str, model: str) -> str:
    return f"{harness}/{model}"


def _finite(value: float | None) -> float | None:
    """`value` for JSON: an infinite cost per solved task (nothing solved) is written as unknown, `solved` says why."""
    return None if value is None or math.isinf(value) else value


def null_harnesses(trials: Sequence[TrialResult]) -> frozenset[str]:
    """The harnesses that are a negative control, found from what the `null` parser records: every trial parsed with
    zero tokens, a real 0.0 reported cost and a real 0 tool calls. No real agent looks like that, and the run data
    does not name the control (the config does)."""
    by_harness: dict[str, list[bool]] = {}
    for t in trials:
        u = t.usage
        silent = u.parsed and u.total_tokens == 0 and u.tool_calls == 0 and u.costs.harness == 0.0
        by_harness.setdefault(t.target.harness, []).append(silent)
    return frozenset(h for h, flags in by_harness.items() if all(flags))


# ---------------------------------------------------------------- recommend


@dataclass(frozen=True, kw_only=True)
class Constraints:
    """What a target must meet to be recommended; `None` is not set."""

    min_pass_rate: float | None = None
    max_cost_per_solved: float | None = None
    max_cost_per_run: float | None = None
    max_latency_s: float | None = None

    def __post_init__(self) -> None:
        rate = self.min_pass_rate
        if rate is not None and not 0 <= rate <= 1:
            raise ValueError(f"min_pass_rate must be in [0, 1], got {rate}")
        for key in CONSTRAINT_KEYS[1:]:
            value = getattr(self, key)
            if value is not None and not (math.isfinite(value) and value >= 0):
                raise ValueError(f"{key} must be a finite number >= 0, got {value}")

    def given(self) -> dict[str, float]:
        """The constraints that were set, by name."""
        return {key: getattr(self, key) for key in CONSTRAINT_KEYS if getattr(self, key) is not None}


@dataclass(frozen=True, kw_only=True)
class Candidate:
    """One non-null target of the run, in report order, with the constraints it failed (none: eligible)."""

    rank: int
    harness: str
    model: str
    entry: Mapping[str, Any]
    failed: tuple[str, ...]

    @property
    def name(self) -> str:
        return _name(self.harness, self.model)

    @property
    def eligible(self) -> bool:
        return not self.failed


@dataclass(frozen=True, kw_only=True)
class Recommendation:
    info: RunInfo
    cost_source: str
    constraints: Constraints
    candidates: tuple[Candidate, ...]
    pick: Candidate | None
    tied: tuple[str, ...]  # eligible targets whose cost-per-solved interval ties the pick's
    null_control: tuple[str, ...]


def _failures(entry: Mapping[str, Any], c: Constraints) -> tuple[str, ...]:
    """Why `entry` is not eligible. An unknown cost fails every cost constraint, once, as `unknown cost`."""
    reasons: list[str] = []
    if not entry["solved"]:
        reasons.append("solved nothing")
    if c.min_pass_rate is not None and entry["pass_rate"] < c.min_pass_rate - 1e-9:
        reasons.append(f"pass rate {entry['pass_rate']:.0%} < {c.min_pass_rate:.0%}")
    for label, value, limit in (
        ("cost per solved", entry["cost_per_solved"], c.max_cost_per_solved),
        ("cost per run", entry["cost_per_run"], c.max_cost_per_run),
    ):
        if limit is None:
            continue
        if value is None:
            if "unknown cost" not in reasons:
                reasons.append("unknown cost")
        elif value > limit:
            reasons.append(f"{label} {fmt_money(value)} > {fmt_money(limit)}")
    if c.max_latency_s is not None and entry["avg_wall_s"] > c.max_latency_s:
        reasons.append(f"latency {entry['avg_wall_s']:.0f}s > {c.max_latency_s:g}s")
    return tuple(reasons)


def recommend(
    info: RunInfo, trials: Sequence[TrialResult], constraints: Constraints, cost_source: str
) -> Recommendation:
    """Check every non-null target against `constraints`; the pick is the first eligible one in report order
    (cheapest per solved task, unknown cost after known, higher pass rate first on equal cost)."""
    nulls = null_harnesses(trials)
    table = aggregate(trials, cost_source, null_harnesses=nulls)
    candidates = tuple(
        Candidate(
            rank=rank,
            harness=e["harness"],
            model=e["model"],
            entry=e,
            failed=_failures(e, constraints),
        )
        for rank, e in enumerate(table, 1)
        if not e["null_control"]
    )
    pick = next((c for c in candidates if c.eligible), None)
    tied: tuple[str, ...] = ()
    if pick is not None and pick.entry["tie_with"] is not None:
        tied = tuple(
            c.name for c in candidates if c is not pick and c.eligible and c.entry["tie_with"] == pick.entry["tie_with"]
        )
    return Recommendation(
        info=info,
        cost_source=cost_source,
        constraints=constraints,
        candidates=candidates,
        pick=pick,
        tied=tied,
        null_control=tuple(_name(e["harness"], e["model"]) for e in table if e["null_control"]),
    )


def decision_for(recommendation: Recommendation) -> TargetDecision:
    """The `TargetDecision` of the pick. Raises `ValueError` when nothing was picked."""
    pick = recommendation.pick
    if pick is None:
        raise ValueError("no target was picked")
    e = pick.entry
    info = recommendation.info
    return TargetDecision(
        harness=pick.harness,
        model=pick.model,
        run_id=info.run_id,
        config_sha256=info.config_sha256,
        suite_version=info.suite_version,
        cost_source=recommendation.cost_source,
        constraints=recommendation.constraints.given(),
        evidence=Evidence(
            runs=e["runs"],
            solved=e["solved"],
            pass_rate=e["pass_rate"],
            pass_ci_low=e["pass_ci_low"],
            pass_ci_high=e["pass_ci_high"],
            cost_per_solved=e["cost_per_solved"],
            cost_per_run=e["cost_per_run"],
            avg_wall_s=e["avg_wall_s"],
        ),
    )


def _constraints_text(constraints: Constraints) -> str:
    given = constraints.given()
    labels = {
        "min_pass_rate": lambda v: f"min pass rate {v:.0%}",
        "max_cost_per_solved": lambda v: f"max cost per solved {fmt_money(v)}",
        "max_cost_per_run": lambda v: f"max cost per run {fmt_money(v)}",
        "max_latency_s": lambda v: f"max latency {v:g}s",
    }
    return ", ".join(labels[key](value) for key, value in given.items()) or "none"


def _percent_range(low: float, high: float) -> str:
    return f"{low * 100:.0f}–{high * 100:.0f}%"


def render_recommendation(rec: Recommendation) -> str:
    """The recommendation as Markdown: every target with the constraints it failed, then the pick."""
    lines = [
        f"# Recommendation — run {rec.info.run_id}",
        "",
        f"Cost source: `{rec.cost_source}`. Constraints: {_constraints_text(rec.constraints)}.",
        "",
        "| # | Harness | Model | Solved | Pass rate | Cost/solved | Cost/run | Time (s) | Eligible |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in rec.candidates:
        e = c.entry
        lines.append(
            f"| {c.rank} | {c.harness} | {c.model} | {e['solved']}/{e['runs']} | {e['pass_rate']:.0%} "
            f"| {fmt_money(e['cost_per_solved'])} | {fmt_money(e['cost_per_run'])} | {e['avg_wall_s']:.0f} "
            f"| {'; '.join(c.failed) or 'yes'} |"
        )
    pick = rec.pick
    lines.append("")
    if pick is None:
        lines.append("**No eligible target.** Relax a constraint or run more trials.")
    else:
        e = pick.entry
        lines.append(
            f"**Pick:** {pick.name} — pass rate {e['pass_rate']:.0%} "
            f"(95% CI {_percent_range(e['pass_ci_low'], e['pass_ci_high'])}), "
            f"{fmt_money(e['cost_per_solved'])} per solved task."
        )
        if rec.tied:
            lines += [
                "",
                f"**Warning:** statistically tied with {', '.join(rec.tied)} (cost-per-solved intervals overlap): "
                "the pick is not clearly better. Run more repetitions before a switch.",
            ]
    if rec.null_control:
        lines += ["", "## Negative control", "", f"Never recommended: {', '.join(rec.null_control)}."]
    return "\n".join(lines) + "\n"


def recommendation_json(rec: Recommendation) -> dict[str, Any]:
    """The recommendation as a JSON-ready dict (an infinite cost per solved task is `null`)."""
    targets = [
        {
            "rank": c.rank,
            "harness": c.harness,
            "model": c.model,
            "runs": c.entry["runs"],
            "solved": c.entry["solved"],
            "pass_rate": c.entry["pass_rate"],
            "cost_per_solved": _finite(c.entry["cost_per_solved"]),
            "cost_per_run": c.entry["cost_per_run"],
            "avg_wall_s": c.entry["avg_wall_s"],
            "eligible": c.eligible,
            "failed": list(c.failed),
        }
        for c in rec.candidates
    ]
    return {
        "run_id": rec.info.run_id,
        "cost_source": rec.cost_source,
        "constraints": rec.constraints.given(),
        "targets": targets,
        "pick": None if rec.pick is None else decision_for(rec).to_dict(),
        "tied_with": list(rec.tied),
        "null_control": list(rec.null_control),
    }


# ---------------------------------------------------------------- target file


def encode_target(decision: TargetDecision) -> str:
    """The bytes of the target file: sorted keys, a final newline and no timestamp, so the same run gives the same
    file and it diffs cleanly in git."""
    return json.dumps(decision.to_dict(), indent=2, sort_keys=True, allow_nan=False) + "\n"


def write_target(path: Path, decision: TargetDecision) -> None:
    """Write the target file atomically. A symlink at `path` is refused, never followed."""
    if path.is_symlink():
        raise TargetFileError(f"target file {path}: refusing to write through a symlink")
    try:
        atomic_write(path, encode_target(decision))
    except OSError as e:
        raise TargetFileError(f"target file {path}: cannot be written ({e.strerror or e})") from e


def load_target(path: Path) -> TargetDecision:
    """The `TargetDecision` in `path`, read without following a link and with a size limit. Anything else (extra or
    missing keys, another major version, invalid values) is a `TargetFileError`."""
    try:
        text = read_artifact(path.parent, path.name, MAX_TARGET_BYTES)
        document = json.loads(text)
        if not isinstance(document, dict):
            raise TargetFileError("not a JSON object")
        return TargetDecision.from_dict(document)
    except (ValueError, ContractError) as e:  # TargetFileError, JSON errors and contract errors are ValueErrors
        raise TargetFileError(f"target file {path}: {e}") from e


# ---------------------------------------------------------------- gate


@dataclass(frozen=True, kw_only=True)
class GateResult:
    ok: bool
    message: str
    solved: int = 0
    runs: int = 0


def gate(
    info: RunInfo,
    trials: Sequence[TrialResult],
    harness: str,
    model: str,
    suite: str,
    min_pass_rate: float,
) -> GateResult:
    """The CI gate: the pass rate of one target over the trials of `suite` must reach `min_pass_rate`. It fails
    closed: an unfinished run, a target that is not in the run and no trial of the suite for it are all failures.
    `error` and `timeout` trials count as failed."""
    if not 0 <= min_pass_rate <= 1:
        raise ValueError(f"min_pass_rate must be in [0, 1], got {min_pass_rate}")
    who = _name(harness, model)
    if info.finished_at is None:
        return GateResult(ok=False, message=f"run {info.run_id} is not finished (finished_at is null)")
    if (harness, model) not in {(t.harness, t.model) for t in info.targets}:
        return GateResult(ok=False, message=f"target {who} is not in run {info.run_id}")
    mine = [
        t
        for t in trials
        if (t.target.harness, t.target.model) == (harness, model) and t.task_id.startswith(f"{suite}/")
    ]
    if not mine:
        return GateResult(ok=False, message=f"no trial of suite '{suite}' for {who} in run {info.run_id}")
    solved = sum(t.passed for t in mine)
    rate = solved / len(mine)
    low, high = wilson_interval(solved, len(mine))
    verdict = "below" if rate < min_pass_rate - 1e-9 else "meets"
    message = (
        f"{who} on suite '{suite}': pass rate {rate:.0%} ({solved}/{len(mine)}, 95% CI "
        f"{_percent_range(low, high)}) {verdict} the minimum {min_pass_rate:.0%}"
    )
    return GateResult(ok=verdict == "meets", message=message, solved=solved, runs=len(mine))


# ---------------------------------------------------------------- compare

Run = tuple[RunInfo, Sequence[TrialResult]]


@dataclass(frozen=True, kw_only=True)
class Comparison:
    """Two runs side by side: warnings about comparability, the ranking diff of the targets in both runs, the
    regressions (significant, paired) and the tasks that went from solved to never solved."""

    run_a: str
    run_b: str
    cost_source: str
    alpha: float
    warnings: tuple[str, ...]
    targets: tuple[Mapping[str, Any], ...]
    only_a: tuple[str, ...]
    only_b: tuple[str, ...]
    null_control: tuple[str, ...]
    regressions: tuple[Mapping[str, Any], ...]
    task_regressions: tuple[Mapping[str, Any], ...]

    @property
    def has_regression(self) -> bool:
        return bool(self.regressions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_a": self.run_a,
            "run_b": self.run_b,
            "cost_source": self.cost_source,
            "alpha": self.alpha,
            "warnings": list(self.warnings),
            "targets": [dict(t) for t in self.targets],
            "only_in_a": list(self.only_a),
            "only_in_b": list(self.only_b),
            "null_control": list(self.null_control),
            "regressions": [dict(r) for r in self.regressions],
            "task_regressions": [dict(r) for r in self.task_regressions],
        }


def _reps(info: RunInfo, trials: Sequence[TrialResult]) -> int:
    """The repetitions a run planned, or the most a trial of it records when it did not say."""
    return info.reps if info.reps is not None else max((t.rep for t in trials), default=0)


def _comparability(a: Run, b: Run) -> tuple[str, ...]:
    (info_a, trials_a), (info_b, trials_b) = a, b
    warnings = [f"run {i.run_id} is not finished" for i, _ in (a, b) if i.finished_at is None]
    for label, x, y in (
        ("config_sha256", info_a.config_sha256, info_b.config_sha256),
        ("suite_version", info_a.suite_version, info_b.suite_version),
        ("reps", _reps(info_a, trials_a), _reps(info_b, trials_b)),
    ):
        if x != y:
            warnings.append(f"{label} differs: {x} vs {y}")
    tasks_a, tasks_b = {t.task_id for t in trials_a}, {t.task_id for t in trials_b}
    if tasks_a != tasks_b:
        warnings.append(f"task set differs: {len(tasks_a - tasks_b)} only in A, {len(tasks_b - tasks_a)} only in B")
    return tuple(warnings)


def _side(rank: int, entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "rank": rank,
        "solved": entry["solved"],
        "runs": entry["runs"],
        "pass_rate": entry["pass_rate"],
        "cost_per_solved": _finite(entry["cost_per_solved"]),
        "cost_per_run": entry["cost_per_run"],
    }


def _delta(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else b - a


def _outcomes(trials: Sequence[TrialResult], pair: tuple[str, str]) -> dict[tuple[str, int], bool]:
    return {(t.task_id, t.rep): t.passed for t in trials if (t.target.harness, t.target.model) == pair}


def _lost_tasks(out_a: Mapping[tuple[str, int], bool], out_b: Mapping[tuple[str, int], bool]) -> list[str]:
    """Tasks present in both runs that were solved at least once in A and never in B."""
    shared = {task for task, _ in out_a} & {task for task, _ in out_b}
    return sorted(
        task
        for task in shared
        if any(ok for (t, _), ok in out_a.items() if t == task)
        and not any(ok for (t, _), ok in out_b.items() if t == task)
    )


def _null_in_both(trials_a: Sequence[TrialResult], trials_b: Sequence[TrialResult]) -> frozenset[str]:
    """Harnesses that look like the null control in every run they appear in. A real target that broke in one run
    only (an auth failure) looks null there but not in the other, so it stays in the ranking."""
    null_a, null_b = null_harnesses(trials_a), null_harnesses(trials_b)
    in_a, in_b = {t.target.harness for t in trials_a}, {t.target.harness for t in trials_b}
    return frozenset(h for h in in_a | in_b if (h in null_a or h not in in_a) and (h in null_b or h not in in_b))


def compare(a: Run, b: Run, cost_source: str, alpha: float = 0.05) -> Comparison:
    """Compare run `a` (the baseline) with run `b`. A target in both runs regressed when, over the `(task, rep)` pairs
    present in both, it passed in A and failed in B more often than the reverse (`b > c`) and the exact McNemar test
    gives `p < alpha`. An `error` or `timeout` trial counts as failed.
    Null-control pairs are listed apart from the ranking but still checked for regressions."""
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    (info_a, trials_a), (info_b, trials_b) = a, b
    nulls = _null_in_both(trials_a, trials_b)
    tables = [
        [e for e in aggregate(trials, cost_source, null_harnesses=nulls) if not e["null_control"]]
        for trials in (trials_a, trials_b)
    ]
    entries = [{(e["harness"], e["model"]): (rank, e) for rank, e in enumerate(table, 1)} for table in tables]
    in_a, in_b = entries
    pairs_a = {(t.target.harness, t.target.model) for t in trials_a}
    pairs_b = {(t.target.harness, t.target.model) for t in trials_b}
    rows: list[dict[str, Any]] = []
    for pair, (rank_a, entry_a) in in_a.items():
        if pair not in in_b:
            continue
        rank_b, entry_b = in_b[pair]
        rows.append(
            {
                "harness": pair[0],
                "model": pair[1],
                "rank_a": rank_a,
                "rank_b": rank_b,
                "a": _side(rank_a, entry_a),
                "b": _side(rank_b, entry_b),
                "delta_pass_rate": entry_b["pass_rate"] - entry_a["pass_rate"],
                "delta_cost_per_solved": _delta(
                    _finite(entry_a["cost_per_solved"]), _finite(entry_b["cost_per_solved"])
                ),
            }
        )
    # Regressions are checked for every target in both runs, whatever it looks like: a real target that fails
    # every trial of B (an auth or gateway failure) has the signature of the null control and must still be caught.
    regressions: list[dict[str, Any]] = []
    task_regressions: list[dict[str, Any]] = []
    for pair in sorted(pairs_a & pairs_b, key=lambda p: (in_a[p][0] if p in in_a else math.inf, p)):
        out_a, out_b = _outcomes(trials_a, pair), _outcomes(trials_b, pair)
        shared = out_a.keys() & out_b.keys()
        lost = sum(out_a[k] and not out_b[k] for k in shared)
        gained = sum(out_b[k] and not out_a[k] for k in shared)
        p = mcnemar_exact(lost, gained)
        if lost > gained and p < alpha:
            regressions.append({"harness": pair[0], "model": pair[1], "b": lost, "c": gained, "p": p})
        task_regressions += [{"harness": pair[0], "model": pair[1], "task": t} for t in _lost_tasks(out_a, out_b)]
    return Comparison(
        run_a=info_a.run_id,
        run_b=info_b.run_id,
        cost_source=cost_source,
        alpha=alpha,
        warnings=_comparability(a, b),
        targets=tuple(rows),
        only_a=tuple(_name(*pair) for pair in in_a if pair not in pairs_b),
        only_b=tuple(_name(*pair) for pair in in_b if pair not in pairs_a),
        null_control=tuple(
            sorted(
                {_name(t.target.harness, t.target.model) for t in (*trials_a, *trials_b) if t.target.harness in nulls}
            )
        ),
        regressions=tuple(regressions),
        task_regressions=tuple(task_regressions),
    )


def render_comparison(cmp: Comparison) -> str:
    """The comparison as Markdown. The same input always gives the same bytes."""
    lines = [
        f"# Comparison — {cmp.run_a} vs {cmp.run_b}",
        "",
        f"Cost source: `{cmp.cost_source}`. A is the baseline. Regression: passed in A and failed in B more often "
        f"than the reverse, exact McNemar p < {cmp.alpha:g}.",
        "",
        "## Comparability",
        "",
    ]
    lines += [f"- **Warning:** {w}" for w in cmp.warnings] or ["Runs are comparable."]
    lines += [
        "",
        "## Ranking",
        "",
        "| Harness | Model | Rank A | Rank B | Pass A | Pass B | Δ pass (pp) "
        "| Cost/solved A | Cost/solved B | Δ cost |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in cmp.targets:
        a, b = row["a"], row["b"]
        delta_cost = row["delta_cost_per_solved"]
        lines.append(
            f"| {row['harness']} | {row['model']} | {a['rank']} | {b['rank']} "
            f"| {a['pass_rate']:.0%} ({a['solved']}/{a['runs']}) | {b['pass_rate']:.0%} ({b['solved']}/{b['runs']}) "
            f"| {row['delta_pass_rate'] * 100:+.0f} | {fmt_money(a['cost_per_solved'])} "
            f"| {fmt_money(b['cost_per_solved'])} "
            f"| {'n/a' if delta_cost is None else f'{delta_cost:+.4f}'} |"
        )
    lines += ["", "## Regressions", ""]
    lines += [
        f"- {_name(r['harness'], r['model'])}: b={r['b']} passed in A and failed in B, c={r['c']} the reverse, "
        f"p={r['p']:.4f}"
        for r in cmp.regressions
    ] or ["No regression."]
    if cmp.task_regressions:
        lines += ["", "## Tasks solved in A and never in B (information)", ""]
        lines += [f"- {_name(r['harness'], r['model'])}: {r['task']}" for r in cmp.task_regressions]
    for label, group in (("Only in A", cmp.only_a), ("Only in B", cmp.only_b)):
        if group:
            lines += ["", f"{label}: {', '.join(group)}"]
    if cmp.null_control:
        lines += ["", "## Negative control", "", f"Not ranked: {', '.join(cmp.null_control)}."]
    return "\n".join(lines) + "\n"

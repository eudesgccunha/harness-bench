"""Pure statistics for the report (ADR 0006): intervals, pass@k, bootstrap, McNemar, Pareto, ties, repetitions."""

import hashlib
import math
from collections.abc import Callable, Iterator, Sequence

Interval = tuple[float, float]

# Upper limit of total runs the repetitions recommendation searches.
MAX_RUNS = 10_000


def _wilson(p: float, n: float, z: float) -> Interval:
    """Wilson score interval for the observed proportion `p` over `n` runs."""
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> Interval:
    """Wilson score interval of a pass rate; no data gives the whole range."""
    if n == 0:
        return 0.0, 1.0
    return _wilson(successes / n, n, z)


def _check_counts(n: int, c: int, k: int) -> None:
    if k < 1 or k > n or c > n:
        raise ValueError(f"need 1 <= k <= n and c <= n, got n={n}, c={c}, k={k}")


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased chance that at least one of k runs passes, given c passes in n runs."""
    _check_counts(n, c, k)
    return 1 - math.comb(n - c, k) / math.comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """Unbiased chance that all k runs pass, given c passes in n runs."""
    _check_counts(n, c, k)
    return math.comb(c, k) / math.comb(n, k)


def mean_over_tasks(counts: Sequence[tuple[int, int]], k: int, estimator: Callable[[int, int, int], float]) -> float:
    """Average of `estimator(n, c, k)` over tasks; `counts` holds `(n, c)` per task."""
    if not counts:
        return 0.0
    return sum(estimator(n, c, k) for n, c in counts) / len(counts)


def _splitmix64(seed: int) -> Iterator[int]:
    """Deterministic 64-bit generator (SplitMix64): the bootstrap needs reproducible draws, not secrecy."""
    mask = (1 << 64) - 1
    state = seed & mask
    while True:
        state = (state + 0x9E3779B97F4A7C15) & mask
        z = ((state ^ (state >> 30)) * 0xBF58476D1CE4E5B9) & mask
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & mask
        yield z ^ (z >> 31)


def _ratio(costs: Sequence[float], passed: Sequence[bool]) -> float:
    solved = sum(passed)
    return sum(costs) / solved if solved else math.inf


def bootstrap_ratio_ci(
    costs: Sequence[float], passed: Sequence[bool], seed: str, n_boot: int = 2000, level: float = 0.95
) -> Interval:
    """Percentile bootstrap interval of cost per solved task. A resample with no pass counts as inf."""
    if not costs or len(costs) != len(passed):
        raise ValueError("costs and passed must be non-empty and of the same length")
    draws = _splitmix64(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))
    n = len(costs)
    stats = []
    for _ in range(n_boot):
        idx = [next(draws) % n for _ in range(n)]
        stats.append(_ratio([costs[i] for i in idx], [passed[i] for i in idx]))
    stats.sort()
    tail = (1 - level) / 2
    return stats[int(tail * n_boot)], stats[min(n_boot - 1, int((1 - tail) * n_boot))]


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for `b` and `c` discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / (1 << n)
    return min(1.0, 2 * tail)


def pareto_front(points: Sequence[tuple[float, float]]) -> list[bool]:
    """Whether each `(cost, quality)` point is on the frontier: lower cost and higher quality are better."""

    def dominates(a: tuple[float, float], b: tuple[float, float]) -> bool:
        return a[0] <= b[0] and a[1] >= b[1] and a != b

    return [not any(dominates(other, p) for other in points) for p in points]


def tie_leaders(intervals: Sequence[Interval | None]) -> list[int | None]:
    """Group pairs sorted by rank: one ties the group leader when its low bound is <= the leader's high bound.

    Returns the 1-based rank of each pair's group leader, `None` for a pair without interval or alone in its group.
    """
    leaders: list[int | None] = []
    leader: int | None = None
    for i, interval in enumerate(intervals):
        if interval is None:
            leader = None
            leaders.append(None)
            continue
        leader_interval = intervals[leader] if leader is not None else None
        if leader_interval is None or interval[0] > leader_interval[1]:
            leader = i
        leaders.append(leader)
    return [lead + 1 if lead is not None and leaders.count(lead) > 1 else None for lead in leaders]


def reps_for_ci_width(
    p: float | None, width: float, tasks: int, z: float = 1.96, max_runs: int = MAX_RUNS
) -> int | None:
    """Reps per task so the Wilson interval of pass rate `p` (0.5 when unknown) is at most `width` wide.

    `None` when even `max_runs` runs in total are not enough.
    """
    rate = 0.5 if p is None else p
    for n in range(1, max_runs + 1):
        low, high = _wilson(rate, n, z)
        if high - low <= width:
            return math.ceil(n / tasks)
    return None

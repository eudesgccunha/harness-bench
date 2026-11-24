# ADR 0006: Statistics in the report (Phase 2)

- Status: accepted
- Date: 2026-11-24

## Context

Phase 2 of the [evolution plan](../evolution-plan.md) makes the ranking carry its
uncertainty: Wilson CI, pass@k and pass^k, a bootstrap CI for cost per solved task, ties,
a Pareto frontier and a repetitions recommendation. Roadmap items M1, M3, M4, M5, M10 and
M11 are scheduled in the same phase. Runtime stays stdlib ([ADR 0001](0001-stdlib-runtime.md)).

## Decision

1. **One module, `hbench/stats.py`**, pure functions over numbers, no I/O. It becomes the
   `stats/` package only when it grows past one cohesive module.
2. **Pass rate CI:** Wilson score interval, z = 1.96 (95%). `0/0` gives `(0, 1)`.
3. **pass@k and pass^k per pair:** computed per task with the unbiased estimators
   (pass@k = 1 − C(n−c, k)/C(n, k); pass^k = C(c, k)/C(n, k)), then averaged over tasks.
   k is the smallest number of runs any task has for that pair; with k = 1 both equal the
   pass rate.
4. **Cost per solved CI:** percentile bootstrap over the pair's trials, 2000 resamples,
   95%. The RNG is a small SplitMix64 generator seeded with the sha256 of `harness|model`
   (`random.Random` trips bandit B311 and this project allows no new ignores), so the
   report is deterministic and goldens stay stable. A resample with no pass counts as
   `inf`. Unknown cost (any trial `None`) gives no CI, never a CI built from zeros.
5. **Ties:** pairs are already sorted by cost per solved. Walking that order, a pair is
   tied with the current group leader when its CI lower bound is ≤ the leader's CI upper
   bound; otherwise it starts a new group. The table shows the leader's rank (`=1`).
   Pairs without a CI are never tied.
6. **Pareto frontier:** over (cost per run, pass rate) for pairs with known cost; a pair
   is on the frontier when no other pair is at least as cheap and at least as good, and
   strictly better in one. Pairs with no pass are not eligible: a pair that
   solves nothing is not a trade-off, however cheap.
7. **Repetitions recommendation:** smallest total runs n where the Wilson CI width at
   the observed pass rate (0.5 when there is no data) is ≤ `target_ci_width` (config,
   default 0.20), converted to reps per task (ceil(n / tasks)). When even 10,000 total runs are not
   enough, the value is unknown and the report says `more than <10000 / tasks>`.
8. **Negative control (M3):** built-in parser `null` (usage all zero, harness cost
   `0.0`, which is a real zero: no model call happens). Example config entry runs
   `["true"]`. Any pass by a `null` pair prints a warning: the tests are too lenient.
   Null pairs are excluded from discrimination and from the recommendation, and the table
   sorts them last, so a null pair that passes (cost 0) cannot take rank #1.
9. **Discrimination (M4):** tasks solved by every non-null pair (saturated) or by none
   are listed; `report --drop-saturated` removes saturated tasks before aggregating.
10. **Paired comparison (M11):** exact two-sided McNemar test between the #1 pair and
    every other pair, pairing trials by `(task_id, rep)`.
11. **Recommendation (M5, text only):** the cheapest pair per solved task among pairs
    within `recommend_within_pp` (config, default 5) percentage points of the best pass
    rate, with at least one pass.
12. **Deferred:** the M5 `summary.html` chart goes to Phase 7 (sinks); M10 tool-call
    counts need parser changes and go to Phase 3 (runner protocol).
13. **Compatibility:** new CSV columns are appended after the existing ones; existing
    Markdown columns keep their order, new ones are added at the end of the main table.
    This is an intended change to `summary.md`/`summary.csv`, so the report goldens are
    regenerated once (`HB_UPDATE_GOLDEN=1`); `tests/golden/run.jsonl` does not change.

## Consequences

- `summary.md` shows CI, pass^k and ties; consumers of `summary.csv` that read by column
  name keep working.
- The bootstrap adds about 2000 × trials operations per pair, negligible at benchmark
  sizes.

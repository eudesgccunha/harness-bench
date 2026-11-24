# Phase 2: Statistics for Decisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The report shows uncertainty (Wilson CI, pass@k, pass^k, bootstrap CI for cost per solved, ties), a Pareto frontier, a reps recommendation, a negative control, task discrimination, a paired comparison and a recommendation line.

**Architecture:** Pure functions in `hbench/stats.py`; `hbench/report.py` calls them and renders. Decisions: [ADR 0006](../../adr/0006-report-statistics.md).

**Tech Stack:** Python 3.12 stdlib (`math.comb`, `random.Random`, `hashlib`, `statistics`); dev tools via uv (`make check`).

**Spec:** [`docs/evolution-plan.md`](../../evolution-plan.md) Phase 2, and roadmap items M1, M3, M4, M5 (text part), M11 in [`docs/roadmap.md`](../../roadmap.md).

## Global Constraints

- Python 3.12, standard library only at runtime.
- Unknown cost is `None`, never `0`. No CI is ever computed from unknown costs.
- Everything in English. Type hints everywhere; `hbench/` is mypy strict.
- All AGENTS.md defenses unchanged; `tests/smoke.py` keeps passing.
- `make check` green after every task; coverage `fail_under = 100`.
- Report output must be deterministic (fixed seeds), so goldens are stable.
- Goldens: `tests/golden/summary.md` and `summary.csv` are regenerated **once, in Task 4**, with `HB_UPDATE_GOLDEN=1`, and the diff is reviewed: existing columns and lines unchanged, only additions. `tests/golden/run.jsonl` must not change.
- New files go into `scripts/build_md.py` and the mypy list; `HARNESS_BENCH.md` regenerated.
- Edit files with targeted replacements; never rewrite a whole existing file from memory.
- No commits.

## Review Focus

- A pair with 0 runs on some task, or tasks with different run counts: k = min runs, no crash. Test in Task 1.
- A pair with unknown cost on one trial: no cost CI, never tied, excluded from the Pareto frontier. Test in Task 3.
- A pair with 0 passes: cost per solved `inf`, CI `n/a`, not recommended. Test in Task 3.
- Only one non-null pair: no paired comparison section, no discrimination noise (every task trivially saturated or unsolved is still listed correctly). Test in Task 4.
- `--drop-saturated` that drops every task: report says "no tasks left after --drop-saturated" and exits 0 without a table. Test in Task 5.

---

### Task 1: `hbench/stats.py`

**Files:**
- Create: `hbench/stats.py`
- Test: `tests/test_stats.py`

**Interfaces:**
- Produces:
  - `Interval = tuple[float, float]`
  - `wilson_interval(successes: int, n: int, z: float = 1.96) -> Interval`
  - `pass_at_k(n: int, c: int, k: int) -> float`; `pass_hat_k(n: int, c: int, k: int) -> float` (raise `ValueError` when `k < 1` or `k > n` or `c > n`)
  - `mean_over_tasks(counts: Sequence[tuple[int, int]], k: int, estimator: Callable[[int, int, int], float]) -> float` (counts are `(n, c)` per task)
  - `bootstrap_ratio_ci(costs: Sequence[float], passed: Sequence[bool], seed: str, n_boot: int = 2000, level: float = 0.95) -> Interval` (statistic = sum(costs)/sum(passed), `inf` when a resample has no pass; seed via `int(sha256(seed).hexdigest()[:16], 16)`)
  - `mcnemar_exact(b: int, c: int) -> float` (two-sided; `b + c == 0` gives `1.0`)
  - `pareto_front(points: Sequence[tuple[float, float]]) -> list[bool]` (points are `(cost, quality)`; lower cost and higher quality are better)
  - `tie_leaders(intervals: Sequence[Interval | None]) -> list[int | None]` (input sorted by rank; output is the 1-based rank of each pair's group leader, `None` when the pair has no interval or is alone in its group)
  - `reps_for_ci_width(p: float | None, width: float, tasks: int, z: float = 1.96, max_runs: int = 10_000) -> int`

- [x] **Step 1: Write failing tests with known values:** `wilson_interval(8, 10)` ≈ `(0.4902, 0.9433)` (4 decimals); `wilson_interval(0, 10)` ≈ `(0.0, 0.2775)`; `wilson_interval(0, 0) == (0.0, 1.0)`; `pass_at_k(5, 2, 1) == 0.4`; `pass_at_k(5, 2, 2) == 0.7`; `pass_hat_k(5, 2, 2) == 0.1`; `pass_hat_k(5, 5, 3) == 1.0`; `pass_at_k(5, 0, 3) == 0.0`; `mcnemar_exact(0, 6) == 0.03125`; `mcnemar_exact(3, 3) == 1.0`; `mcnemar_exact(0, 0) == 1.0`; `pareto_front([(1, 0.5), (2, 0.9), (3, 0.8), (1, 0.4)]) == [True, True, False, False]`; `tie_leaders([(1, 2), (1.5, 3), (4, 5), None]) == [1, 1, None, None]`; bootstrap is deterministic for the same seed, differs for another seed, gives `(inf, inf)` when nothing passed, and its interval contains the point estimate for a simple case; `reps_for_ci_width(0.5, 0.2, 1)` equals the smallest n with Wilson width ≤ 0.2 (assert by checking n and n−1); bad `k` raises `ValueError`.
- [x] **Step 2:** `make unit`; FAIL (import error).
- [x] **Step 3:** Implement.
- [x] **Step 4:** `make check` green.

### Task 2: Negative control (`null` parser)

**Files:**
- Modify: `harness_bench.py` (`parse_null`, `PARSERS`), `config.example.json` and `config.litellm.example.json` (add a commented-by-name `null` harness: `"null": {"command": ["true"], "parser": "null"}` and a matrix entry `{"harness": "null", "model": "none"}`), `scripts/build_md.py` (document it)
- Test: `tests/test_parsers.py`, `tests/test_run.py`

**Interfaces:**
- Produces: `parse_null(stdout: str) -> dict` returning `empty_usage()` with `cost_reported = 0.0`.
- Note: `harness_bench_litellm.py` drops only `parser: cursor`; the `null` pair has no LiteLLM tag and keeps cost `0.0` from the harness source, so in the LiteLLM profile (`cost_source = litellm`) it has unknown cost. That is expected and documented.

- [x] **Step 1: Failing tests:** `parse_null("anything") == {...zeros..., "cost_reported": 0.0}`; a fake run with the `null` harness on the fizzbuzz task writes a row with `passed == False` and `cost_reported == 0.0`.
- [x] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** `make check` green.

### Task 3: Aggregate with statistics

**Files:**
- Modify: `hbench/report.py` (`_pair_entry`, `aggregate`)
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: Task 1 functions.
- Produces: each table entry keeps every existing key in order, then appends: `pass_ci_low`, `pass_ci_high`, `k`, `pass_at_k`, `pass_hat_k`, `cost_per_solved_ci_low`, `cost_per_solved_ci_high` (both `None` when cost unknown), `tie_with` (leader rank or `None`), `pareto` (bool; `False` when cost unknown), `reps_needed` (int), `null_control` (bool: harness parser is `null`; aggregate takes an optional `null_harnesses: Collection[str] = ()`). Signature: `aggregate(trials, cost_source, *, null_harnesses: Collection[str] = (), target_ci_width: float = 0.2) -> list[dict[str, Any]]`. The sort order is unchanged except that null pairs sort last.

- [x] **Step 1: Failing tests:** entries for a 3-rep fixture (build trials in the test) have the expected Wilson bounds, k, pass@k and pass^k; unknown cost on one trial gives no cost CI, `tie_with None`, `pareto False`; zero passes give no recommendation-eligible entry and `cost_per_solved == inf`; existing keys and their order unchanged (`list(entry)[:17]` equals the old key list).
- [x] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** `make check` green (report goldens still pass: Markdown does not use the new keys yet; the CSV golden will now differ, so mark only the CSV golden assertion as expected-to-change in this task and regenerate it in Task 4; do not regenerate yet).

### Task 4: Render the statistics

**Files:**
- Modify: `hbench/report.py` (`_HEAD`, `_table_line`, `render_markdown`, new section helpers), `tests/golden/summary.md`, `tests/golden/summary.csv`
- Create: `tests/golden/results_reps.jsonl` (2 non-null pairs + 1 `null` pair, 2 tasks, 3 reps, known costs; one task saturated) and `tests/golden/summary_reps.md`
- Test: `tests/test_report.py`, `tests/test_golden.py`

**Interfaces:**
- Main table: existing 14 columns unchanged, then `Pass 95% CI` (`45–88%`), `pass@k` and `pass^k` (`67%`, header shows `(k)` per cell as `67% (k=3)`), `Cost/solved 95% CI` (`$0.0500–$0.0800`, `n/a`), `Tie` (`=1` or empty), `Pareto` (`yes` or empty).
- Sections after the main table, each only when it has content, in this order: `**Recommendation:** <harness>/<model> — cheapest per solved task within <N> pp of the best pass rate (<rate>).`; `## Pareto frontier (cost per run × pass rate)` (list); `## Negative control` (warning listing null pairs that passed, or `null control solved nothing.`); `## Discrimination` (saturated and unsolved task lists); `## Paired comparison vs #1 (McNemar, exact)` (one line per other pair: `b`, `c`, `p`); `## Repetitions` (`To reach a pass-rate CI width of <W>: <harness>/<model> needs <reps> reps per task.`); then the existing per-task matrix last.

- [x] **Step 1: Failing tests:** `render_markdown` on `results_reps.jsonl` equals `summary_reps.md` (first line masked as in the existing golden); every Review Focus case renders without crashing; null pairs never appear in Recommendation or Discrimination.
- [x] **Step 2:** FAIL. **Step 3:** Implement.
- [x] **Step 4:** Regenerate report goldens once: `HB_UPDATE_GOLDEN=1 make unit`. Check `git diff tests/golden/summary.md tests/golden/summary.csv`: only added columns, added sections; old values unchanged. `git diff --exit-code tests/golden/run.jsonl`.
- [x] **Step 5:** `make check` green.

### Task 5: Config, CLI and docs

**Files:**
- Modify: `harness_bench.py` (`validate_config`: `recommend_within_pp` number 0–100, `target_ci_width` number in (0, 1]; `cmd_report` passes them and the null harness names), `hbench/cli.py` (`report --drop-saturated`; also on `run`/`reconcile` only if they print the report — pass through `args`), `scripts/build_md.py` (report section explains every new column and section), `docs/running-a-benchmark.md` (reading the results), `docs/roadmap.md` (M1, M3, M4, M11 done in Phase 2; M5 text done, chart → Phase 7; M10 → Phase 3), `docs/evolution-plan.md` (tick Phase 2 items, link this plan and ADR 0006), `docs/README.md` (ADR 0006, this plan), `AGENTS.md` (Map: `hbench/stats.py`), `CONTEXT.md` (pass@k, pass^k, tie, Pareto, negative control).
- Test: `tests/test_cli.py`, `tests/test_workspace.py`

- [x] **Step 1: Failing tests:** `report --drop-saturated` removes the saturated task from the per-task matrix and from pass rates; dropping every task prints `no tasks left after --drop-saturated` and exits 0; invalid `recommend_within_pp`/`target_ci_width` exit with a message naming the key.
- [x] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Docs. **Step 5:** `make check` green; `git diff --exit-code tests/golden/run.jsonl`.

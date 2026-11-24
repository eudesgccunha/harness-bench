# Harness x model — <masked>

Cost source: `auto` (auto = litellm > computed > reported). Sorted by cost per solved task.

| # | Harness | Model | Solved | Pass rate | Cost/solved | Cost/run | Cost/1M tokens | Tokens/solved | Tokens/run | Cache hit | Turns | Time (s) | Cost source | Pass 95% CI | pass@k | pass^k | Cost/solved 95% CI | Tie | Pareto | Tool calls/run | Tool calls/solved |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | claude | sonnet | 2/2 | 100% | $0.0645 | $0.0645 | $1.19 | 54,430 | 54,430 | 99% | 5.0 | 40 | litellm | 34–100% | 100% (k=1) | 100% (k=1) | $0.0587–$0.0704 |  | yes | n/a | n/a |
| 2 | opencode | glm | 0/2 | 0% | inf | $0.0101 | $0.3904 | n/a | 26,000 | 78% | 8.0 | 56 | computed | 0–66% | 0% (k=1) | 0% (k=1) | n/a |  |  | n/a | n/a |
| 3 | cursor | auto | 1/2 | 50% | n/a | n/a | n/a | 0 | 0 | 0% | 0.0 | 24 | n/a | 9–91% | 50% (k=1) | 50% (k=1) | n/a |  |  | n/a | n/a |

**Warning:** no token data from the harness for: cursor/auto (2). See `results/logs/*.agent.jsonl`.

**Recommendation:** claude/sonnet — cheapest per solved task within 5 pp of the best pass rate (100%).

## Pareto frontier (cost per run × pass rate)

- claude/sonnet: $0.0645 per run, 100%

## Paired comparison vs #1 (McNemar, exact)

b: only #1 solved the run; c: only the other pair did.

- #2 opencode/glm: b=2, c=0, p=0.5000
- #3 cursor/auto: b=1, c=0, p=1.0000

## Repetitions

- To reach a pass-rate CI width of 0.20: claude/sonnet needs 8 reps per task.
- To reach a pass-rate CI width of 0.20: opencode/glm needs 8 reps per task.
- To reach a pass-rate CI width of 0.20: cursor/auto needs 47 reps per task.

## Per task (solved/runs)

| Task | claude<br>sonnet | opencode<br>glm | cursor<br>auto |
|---|---|---|---|
| alpha | 1/1 | 0/1 | 1/1 |
| beta | 1/1 | 0/1 | 0/1 |

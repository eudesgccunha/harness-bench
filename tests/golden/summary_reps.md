# Harness x model — <masked>

Cost source: `auto` (auto = litellm > computed > reported). Sorted by cost per solved task.

| # | Harness | Model | Solved | Pass rate | Cost/solved | Cost/run | Cost/1M tokens | Tokens/solved | Tokens/run | Cache hit | Turns | Time (s) | Cost source | Pass 95% CI | pass@k | pass^k | Cost/solved 95% CI | Tie | Pareto | Tool calls/run | Tool calls/solved |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | opencode | glm | 4/6 | 67% | $0.0600 | $0.0400 | $26.67 | 2,250 | 1,500 | 83% | 3.0 | 20 | litellm | 30–90% | 100% (k=3) | 50% (k=3) | $0.0367–$0.1400 | =1 | yes | n/a | n/a |
| 2 | claude | sonnet | 5/6 | 83% | $0.0900 | $0.0750 | $50.00 | 1,800 | 1,500 | 83% | 3.0 | 20 | litellm | 44–97% | 100% (k=3) | 50% (k=3) | $0.0617–$0.1667 | =1 | yes | n/a | n/a |
| 3 | null | none | 0/6 | 0% | inf | $0.0000 | n/a | n/a | 0 | 0% | 0.0 | 1 | reported | 0–39% | 0% (k=3) | 0% (k=3) | n/a |  |  | n/a | n/a |

**Recommendation:** claude/sonnet — cheapest per solved task within 5 pp of the best pass rate (83%).

## Pareto frontier (cost per run × pass rate)

- opencode/glm: $0.0400 per run, 67%
- claude/sonnet: $0.0750 per run, 83%

## Negative control

null control solved nothing.

## Discrimination

- Saturated (solved in every run): alpha

## Paired comparison vs #1 (McNemar, exact)

b: only #1 solved the run; c: only the other pair did.

- #2 claude/sonnet: b=0, c=1, p=1.0000

## Repetitions

- To reach a pass-rate CI width of 0.20: opencode/glm needs 42 reps per task.
- To reach a pass-rate CI width of 0.20: claude/sonnet needs 27 reps per task.

## Per task (solved/runs)

| Task | opencode<br>glm | claude<br>sonnet | null<br>none |
|---|---|---|---|
| alpha | 3/3 | 3/3 | 0/3 |
| beta | 1/3 | 2/3 | 0/3 |

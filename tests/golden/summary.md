# Harness x model — <masked>

Cost source: `auto` (auto = litellm > computed > reported). Sorted by cost per solved task.

| # | Harness | Model | Solved | Pass rate | Cost/solved | Cost/run | Cost/1M tokens | Tokens/solved | Tokens/run | Cache hit | Turns | Time (s) | Cost source |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | claude | sonnet | 2/2 | 100% | $0.0645 | $0.0645 | $1.19 | 54,430 | 54,430 | 99% | 5.0 | 40 | litellm |
| 2 | opencode | glm | 0/2 | 0% | inf | $0.0101 | $0.3904 | n/a | 26,000 | 78% | 8.0 | 56 | computed |
| 3 | cursor | auto | 1/2 | 50% | n/a | n/a | n/a | 0 | 0 | 0% | 0.0 | 24 | n/a |

**Warning:** no token data from the harness for: cursor/auto (2). See `results/logs/*.agent.jsonl`.

## Per task (solved/runs)

| Task | claude<br>sonnet | opencode<br>glm | cursor<br>auto |
|---|---|---|---|
| alpha | 1/1 | 0/1 | 1/1 |
| beta | 1/1 | 0/1 | 0/1 |

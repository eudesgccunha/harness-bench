# Running a model × harness benchmark

This guide shows how to find which models from your LiteLLM gateway work best in each harness, mainly **Pi, OpenCode and Claude Code** (Codex is optional). It uses `harness_bench_litellm.py`, which:

- runs the same real task for every harness × model pair;
- decides success with **hidden tests**, never with the agent's own claim;
- reads the **real cost** from the LiteLLM spend logs, through one `x-litellm-tags` tag per run;
- ranks the pairs by **cost per solved task**;
- runs the agents inside the Docker sandbox, with configs generated from `LITELLM_BASE_URL`.

For machine-specific setup (macOS, Windows, corporate proxy), see [Machine setup](machine-setup.md).

## Related projects

- [OpenBench](https://github.com/minghinmatthewlam/openbench) compares coding-agent harnesses with a strict method: checker validation, a negative control, Wilson intervals, partial credit and imported tasks. It has no native LiteLLM integration, so this project uses it as a methodology reference, not as a runner.
- [LiteLLM Lens CLI](https://docs.litellm.ai/docs/proxy/lens/cli) compares models × harnesses (Claude Code, OpenCode, Pi) in a cost-versus-quality report. At the time of writing it is a private preview with no public package, and its cost is estimated from tokens.
- [LiteLLM Lens](https://docs.litellm.ai/docs/proxy/lens) runs standalone and shows agent traces. It is useful to investigate failures.

## 1. Prerequisites

- Python 3.12+ and `git`
- A working Docker installation (see [Machine setup](machine-setup.md))
- Access to LiteLLM with two keys:
  - `LITELLM_API_KEY`: the virtual key the agents use. **Create one per person, with a `max_budget`.**
  - `LITELLM_SPEND_KEY`: a key with the `get_spend_routes` permission, to read `/spend/logs`. It never reaches the agent.

## 2. Choose the models

1. List the gateway models (`GET /v1/models`) and shortlist **4 to 6** with good tool-calling support.
2. Check that each model answers on the endpoint each harness uses:

   | Harness | Endpoint | Config |
   |---|---|---|
   | Claude Code | `/v1/messages` | `ANTHROPIC_BASE_URL` |
   | Pi (Claude models) | `/v1/messages` | provider `litellm-anthropic` (`anthropic-messages`) |
   | Pi (other models) | `/v1/chat/completions` | provider `litellm` (`openai-completions`) |
   | OpenCode | `/v1/chat/completions` | provider `litellm` (`@ai-sdk/openai-compatible`) |
   | Codex | `/v1/responses` (`wire_api = "responses"`; use `"chat"` if LiteLLM does not expose it) | `model_providers.litellm` |

3. Estimate the size before running: `harnesses × models × tasks × reps`. For example, 3 × 5 × 10 × 3 = 450 runs.

Claude Code with a non-Anthropic model depends on LiteLLM translating `/v1/messages` to that provider. Confirm it in the smoke test before the full round.

## 3. Build the tasks

Follow "Choosing tasks" in `HARNESS_BENCH.md`, plus the OpenBench discipline:

- Use **real bugs that were already fixed**, with `ref` set to the commit before the fix. Use the tests from the fix commit as `hidden_tests`.
- Write the `prompt` like an issue, with no hint of the solution.
- List the tests and test configs in `protected_paths`.
- **Check each task's polarity**: the hidden tests must **fail** on the untouched `ref` and **pass** with the fix applied. Put the tasks in a suite with a `solution` directory each and run `python3 -m hbench suite validate --run --config config.json` (see [Suites](../HARNESS_BENCH.md#suites)); it reports `ok`, `trivially passing` or `solution fails` per task, and follows the same sandbox rule as `run`.
- **Keep two kinds of suite**: `capability` suites (hard, low pass rate) tell the pairs apart; `regression` suites (must always pass) tell you the setup works. The report adds `## By suite kind` and flags a failed regression task.
- **Try the examples first**: `config.example.json` lists `suites/regression` and `suites/examples-capability` (synthetic, stdlib only, offline). `python3 -m hbench suite list --config config.json` shows them.
- **Filter by difficulty**: after a pilot round, drop tasks that every pair solves or that no pair solves. They do not separate the pairs.
- Start with **8 to 15 tasks**.

## 4. Configure the matrix

```bash
cp config.litellm.example.json config.litellm.json
```

Fill `matrix` with the pairs you want. For a fair comparison:

1. **Phase A, harness effect:** the same model in all three harnesses (Claude Code × Pi × OpenCode).
2. **Phase B, model effect:** vary the models inside each harness.
3. Pin the **reasoning effort** and record it in the pair name.
4. Pin the CLI versions when building the image: `--build-arg CLAUDE_CODE_VERSION=... --build-arg PI_VERSION=... --build-arg OPENCODE_VERSION=...`.
5. Use `reps` ≥ 3.

## 5. Run

```bash
export LITELLM_BASE_URL=https://your-litellm.example.com   # no /v1
export LITELLM_API_KEY=sk-...      # agents' virtual key
export LITELLM_SPEND_KEY=sk-...    # spend reader

# 0. Offline test of the script itself
python3 tests/smoke.py

# 1. Sandbox image (once per machine) and container test
docker build -f sandbox/Dockerfile -t harness-bench-agents \
  --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
python3 tests/docker_e2e.py

# 2. Check the generated commands (spends nothing)
python3 harness_bench_litellm.py run --config config.litellm.json --dry-run

# 3. Smoke test: 1 repetition per harness. Check results-litellm/logs/ and that cost is not n/a
python3 harness_bench_litellm.py run --config config.litellm.json --reps 1 --only pi
python3 harness_bench_litellm.py run --config config.litellm.json --reps 1 --only claude-code opencode

# 4. Full round (resumes where it stopped)
python3 harness_bench_litellm.py run --config config.litellm.json

# 5. If LiteLLM wrote its logs late
python3 harness_bench_litellm.py reconcile --config config.litellm.json
```

**Do not use `--allow-unsandboxed` on a work machine.** The agents run without asking for approval, and the tests execute the code they wrote.

## 6. Read the results

`results-litellm/summary.md` has the ranking by cost per solved task and the per-task pass matrix.

- "Unmatched runs" or a cost of `n/a` means the tag did not reach LiteLLM. See "LiteLLM: what to check" in `HARNESS_BENCH.md`.
- With 3 repetitions, pass rates are noisy. The table shows a Wilson interval for the pass rate and a bootstrap interval for cost per solved task. Treat a gap as real only when the intervals do not overlap: pairs marked `=N` in the `Tie` column cannot be told apart.
- `pass@k` is the capability of a pair (at least one of k runs passes) and `pass^k` its consistency (all k runs pass). A large gap between them means the pair is unreliable.
- The `Recommendation` line names the cheapest pair among those close to the best pass rate. The `Paired comparison` section tells whether the best pair really beats each other pair (small `p`). The `Repetitions` section says how many reps per task would narrow the pass-rate interval to `target_ci_width`.
- Add the `null` harness (see `config.example.json`) as a negative control: if it solves any task, the hidden tests are too lenient. The `Discrimination` section lists tasks every pair solves or none solves; `report --drop-saturated` leaves the saturated ones out.
- Investigate failures through the `.diff` files and logs in `logs/`, and through Lens traces if you use Lens.

## Checklist

- [ ] `python3 tests/smoke.py` passes
- [ ] `python3 tests/docker_e2e.py` passes
- [ ] `--dry-run` shows the expected commands
- [ ] the `--reps 1` smoke test shows real cost (not `n/a`) for every harness
- [ ] the virtual key has a budget

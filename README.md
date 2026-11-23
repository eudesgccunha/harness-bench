# harness-bench

Which combination of **agent tool** (harness) and **model** solves your tasks at the lowest cost?

This repository runs the same real tasks across many harness × model pairs. For each pair it measures:

- whether the task was solved, according to hidden tests;
- how many tokens were spent;
- what it actually cost, from LiteLLM spend logs.

The report is ranked by **cost per solved task**.

Supported harnesses: Claude Code, Pi, OpenCode and Codex CLI, all through LiteLLM, plus Cursor (`agent` CLI), which reports only solve rate and time.

## Quick start

Requirements: Python 3.12+, `git` and the agent CLIs.

```bash
python3 tests/smoke.py                               # offline test of the script

export LITELLM_BASE_URL=https://your-litellm.example.com
export LITELLM_API_KEY=sk-...                        # used by the agents
export LITELLM_SPEND_KEY=sk-...                      # reads /spend/logs

cp config.litellm.example.json config.litellm.json   # fill in models and tasks
python3 harness_bench_litellm.py run --config config.litellm.json --dry-run
python3 harness_bench_litellm.py run --config config.litellm.json --reps 1 --only pi   # add --allow-unsandboxed if there is no sandbox
python3 harness_bench_litellm.py run --config config.litellm.json
```

The result goes to `results-litellm/summary.md`.

With the Docker image, the Pi, OpenCode and Codex configs are generated inside the container from `LITELLM_BASE_URL`. Without Docker, use `pi-models.example.json`, `opencode.example.json` and `codex-config.example.toml`, replacing `__LITELLM_BASE_URL__`. Claude Code is configured by the benchmark config itself.

## Two scripts

| Script | Harnesses | Cost |
|---|---|---|
| `harness_bench_litellm.py` | Claude Code, Pi, OpenCode, Codex | always real (LiteLLM) |
| `harness_bench.py` | all, including Cursor | LiteLLM → price table → cost reported by the harness |

## Full documentation

Guides for running a benchmark, machine setup and the roadmap: [`docs/`](docs/README.md).

[`HARNESS_BENCH.md`](HARNESS_BENCH.md) covers the step-by-step setup, the config for each harness, what to check in LiteLLM, the rules for a fair comparison and the complete source code. You can rebuild the project from that file alone.

## Security

The agents run **without asking for approval**, and the tests execute code they wrote. The script isolates what it can:

- temporary workdir outside the repository, without git history;
- minimal environment, without the LiteLLM spend key;
- test files protected against tampering;
- audit diff and logs with secrets redacted.

The rest depends on a sandbox. `run` requires `sandbox.prefix` or `--allow-unsandboxed`, which you should use only inside a disposable VM. The example configs already use the image from `sandbox/Dockerfile`, which has all the CLIs:

```bash
docker build -f sandbox/Dockerfile -t harness-bench-agents \
  --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
python3 tests/docker_e2e.py
```

Details in the "Security" section of [`HARNESS_BENCH.md`](HARNESS_BENCH.md).

`.gitignore` already excludes local configs, results and real tasks. Do not force-add those files.

## License

[MIT](LICENSE)

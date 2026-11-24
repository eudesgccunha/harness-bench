# harness-bench: cost per solved task (harness × model)

A script to compare **harness × model** pairs on real tasks from your own code. It measures pass rate, tokens and **cost per solved task**.

Target setup:

| Harness | Billing | Models | Cost source in the report |
|---|---|---|---|
| Claude Code | API through LiteLLM | Anthropic | LiteLLM (real) |
| Pi | API through LiteLLM | OpenAI, Anthropic, Gemini | LiteLLM (real) |
| OpenCode | API through LiteLLM | OpenAI, Anthropic, Gemini | LiteLLM (real) |
| Codex CLI | API through LiteLLM | OpenAI | LiteLLM (real) |
| Cursor (`agent` CLI) | Cursor subscription | OpenAI, Anthropic, Gemini, Grok | no tokens in the JSON: pass rate and time only |

Gemini CLI is not part of the benchmark.

Requirements: Python 3.12+ (standard library only), `git`, and the CLIs installed and authenticated.

## Core idea

1. Each run gets a unique tag (`bench-<date>-<task>__<harness>__<model>__rN`).
2. The script exports the tag as `BENCH_TAG`, and the harness sends it to LiteLLM in the `x-litellm-tags` header.
3. After the round, `reconcile` fetches the LiteLLM spend logs and sums the real cost of each tag.

So the cost comes from the real bill (what LiteLLM charged), not from each harness's own estimate. This includes auxiliary calls such as subagents, compaction and title generation.

## Two scripts

| Script | Harnesses | Cost | Output |
|---|---|---|---|
| `harness_bench.py` | all, including Cursor | `auto` (LiteLLM → price table → reported) | `results/` |
| `harness_bench_litellm.py` | only those that go through LiteLLM: Claude Code, Pi, OpenCode and Codex | always LiteLLM (real cost) | `results-litellm/` |

`harness_bench_litellm.py` uses the same engine. Both files must be in the same folder. It does the following:

- drops any harness with `parser: cursor` from the config;
- requires the `litellm` section in the config;
- after the round, waits `--settle` seconds (default 60) for LiteLLM to write its logs;
- runs `reconcile` and builds the report with real cost only.

```bash
cp config.litellm.example.json config.litellm.json
python3 harness_bench_litellm.py run --config config.litellm.json --dry-run
python3 harness_bench_litellm.py run --config config.litellm.json --reps 1 --only pi   # smoke test (needs sandbox.prefix or --allow-unsandboxed)
python3 harness_bench_litellm.py run --config config.litellm.json                     # full round
python3 harness_bench_litellm.py reconcile --config config.litellm.json               # if the logs are late
```

If a run shows cost `n/a`, its tag did not reach LiteLLM. See the "LiteLLM: what to check" section.

## Layout

```
harness-bench/
├── harness_bench.py
├── harness_bench_litellm.py     # LiteLLM-only variant (no Cursor)
├── hbench/                      # data contract, report and single CLI (ADR 0005)
├── README.md / AGENTS.md
├── scripts/build_md.py          # generates this HARNESS_BENCH.md
├── sandbox/Dockerfile           # sandbox image with the CLIs
├── sandbox/entrypoint.sh        # renders harness configs from LITELLM_BASE_URL
├── tests/smoke.py               # end-to-end test with fake agents and a fake LiteLLM
├── tests/docker_e2e.py          # same flow inside the container
├── config.json                  # copy from config.example.json
├── config.litellm.json          # copy from config.litellm.example.json
├── pi-models.example.json       # goes to ~/.pi/agent/models.json
├── opencode.example.json        # goes to ~/.config/opencode/opencode.json (or the project's opencode.json)
├── codex-config.example.toml    # snippet for ~/.codex/config.toml
└── tasks/
    └── fizzbuzz/
        ├── repo/fizzbuzz.py
        └── hidden_tests/test_hidden.py   # copied in only AFTER the agent runs
```

## Step by step

```bash
# Variables
export LITELLM_BASE_URL=https://your-litellm.example.com
export LITELLM_API_KEY=sk-...        # virtual key used by the harnesses
export LITELLM_SPEND_KEY=sk-...      # key allowed to read spend (can be the same one)

cp config.example.json config.json   # replace the model, price and task placeholders

# 0. Offline test of the script itself (no network, no real CLIs)
python3 tests/smoke.py

# 0b. Sandbox image (once per machine) and container test
docker build -f sandbox/Dockerfile -t harness-bench-agents \
  --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
python3 tests/docker_e2e.py

# 1. Check the generated commands (spends nothing)
python3 harness_bench.py run --config config.json --dry-run

# 2. Smoke test: 1 repetition per harness. Check results/logs/ before going on
python3 harness_bench.py run --config config.json --reps 1 --only pi
python3 harness_bench.py run --config config.json --reps 1 --only claude-code opencode codex cursor

# 3. Full round (resumes where it stopped)
python3 harness_bench.py run --config config.json

# 4. Real cost from LiteLLM + report
python3 harness_bench.py reconcile --config config.json

# Report again, forcing a cost source
python3 harness_bench.py report --config config.json --cost-source litellm
```

Outputs in `results/`:

- `results.jsonl`: one line per run, with raw data;
- `summary.md` and `summary.csv`: table sorted by cost per solved task, the statistics sections described in [Reading the report](#reading-the-report) and the per-task pass matrix;
- `logs/`: each agent's JSON output, stderr, setup, tests, and the `.diff` of everything the agent changed. Known secrets show as `***REDACTED***`.

`run` only starts with `sandbox.prefix` set (see [Security](#security)). Inside a disposable VM, add `--allow-unsandboxed`.

## Reading the report

Besides the cost and token columns, the main table of `summary.md` shows how sure the numbers are. All intervals are 95% and the report is deterministic (the bootstrap is seeded with the pair name).

| Column or section | Meaning |
|---|---|
| `Pass 95% CI` | Wilson score interval of the pass rate. |
| `pass@k` | Chance that at least one of k runs of a task passes (capability), averaged over tasks. k is the smallest number of runs any task has for the pair, shown as `(k=3)`. With k = 1 it equals the pass rate. |
| `pass^k` | Chance that all k runs pass (consistency), averaged over tasks. |
| `Cost/solved 95% CI` | Percentile bootstrap (2000 resamples) of cost per solved task. `n/a` when any cost is unknown or nothing passed: no interval is ever built from unknown costs. |
| `Tie` | `=N` marks pairs whose cost interval overlaps the interval of the group leader (rank N): the ranking cannot tell them apart. |
| `Pareto` | `yes` when no other pair is at least as cheap per run and at least as good, and strictly better in one. Pairs with unknown cost or no pass are never on the frontier. |
| Recommendation | The cheapest pair per solved task among pairs within `recommend_within_pp` points (config, default 5) of the best pass rate, with at least one pass. |
| Negative control | Pairs whose harness uses `parser: null`. Any pass prints a warning: the tests are too lenient. Null pairs are left out of the recommendation, ties, frontier and discrimination, and sort last. |
| Discrimination | Tasks every non-null run solved (saturated) and tasks no run solved. They do not separate the pairs. `report --drop-saturated` removes the saturated ones before aggregating (also accepted by `run` and `reconcile`, which print the report). |
| Paired comparison | Exact two-sided McNemar test between the best-ranked pair and every other pair, pairing runs by task and rep. `b` counts runs only the best pair solved, `c` runs only the other pair solved. |
| Repetitions | Reps per task needed for a pass-rate interval at most `target_ci_width` wide (config, default 0.20), at the observed pass rate; `more than N` when 10,000 total runs are not enough. |

Both config keys are optional: `recommend_within_pp` is a number from 0 to 100, `target_ci_width` a number above 0 and up to 1.

## Cost sources

| Source | Where it comes from | When to use |
|---|---|---|
| `litellm` | LiteLLM spend logs, by tag | Main source. Real cost |
| `computed` | harness tokens × the `prices` table | When there is no LiteLLM, or to estimate Cursor if it exposes tokens |
| `reported` | cost the harness reports itself | Last resort |

`auto` (default) uses `litellm`, then `computed`, then `reported`. The "Cost source" column shows which one was used.

## Per-harness setup

### Claude Code

`config.example.json` already sets the per-run variables:

- `ANTHROPIC_BASE_URL` = LiteLLM;
- `ANTHROPIC_AUTH_TOKEN` = virtual key;
- `ANTHROPIC_CUSTOM_HEADERS` = `x-litellm-tags: {run_tag}`.

`{run_tag}` is replaced with each run's tag. The LiteLLM guides disagree on authentication (`ANTHROPIC_AUTH_TOKEN` or an `x-litellm-api-key` header). Use whichever format already works on your machine.

### Pi

With the Docker image, there is nothing to do: the entrypoint generates `~/.pi/agent/models.json` from `pi-models.example.json`. Without Docker, copy the file to `~/.pi/agent/models.json` and replace `__LITELLM_BASE_URL__` with the proxy URL. Pi accepts `headers` per provider, and `$BENCH_TAG` is read from the environment.

- Claude models: use the `litellm-anthropic` provider (`api: anthropic-messages`). Through the OpenAI-compatible API, Anthropic prompt caching may not work, which raises cost and skews the comparison.
- GPT and Gemini models: use the `litellm` provider (`openai-completions`).
- Model format in the config: `provider/model`. The command uses `--provider {provider} --model {model_name}`.

```json
{
  "providers": {
    "litellm": {
      "baseUrl": "__LITELLM_BASE_URL__/v1",
      "api": "openai-completions",
      "apiKey": "$LITELLM_API_KEY",
      "headers": { "x-litellm-tags": "$BENCH_TAG" },
      "models": [
        { "id": "GPT_MODEL" },
        { "id": "GEMINI_MODEL" }
      ]
    },
    "litellm-anthropic": {
      "baseUrl": "__LITELLM_BASE_URL__",
      "api": "anthropic-messages",
      "apiKey": "$LITELLM_API_KEY",
      "headers": { "x-litellm-tags": "$BENCH_TAG" },
      "models": [
        { "id": "CLAUDE_MODEL" }
      ]
    }
  }
}
```

### OpenCode

With the Docker image, the entrypoint generates the config. Without Docker, use `opencode.example.json` as the global or project config, replacing `__LITELLM_BASE_URL__`. The official examples show `{env:VAR}` in `apiKey`. I found no official example with `{env:...}` inside `headers`. **Check in LiteLLM that the tag arrives.** Model in the config: `litellm/MODEL`.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "litellm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LiteLLM",
      "options": {
        "baseURL": "__LITELLM_BASE_URL__/v1",
        "apiKey": "{env:LITELLM_API_KEY}",
        "headers": { "x-litellm-tags": "{env:BENCH_TAG}" }
      },
      "models": {
        "CLAUDE_MODEL": {},
        "GPT_MODEL": {},
        "GEMINI_MODEL": {}
      }
    }
  }
}
```

### Codex CLI

With the Docker image, the entrypoint generates `config.toml`. Without Docker, add the snippet to `~/.codex/config.toml` (or to a dedicated `CODEX_HOME`), replacing `__LITELLM_BASE_URL__`. `env_http_headers` maps header → env var name. **Check these key names in the docs for your Codex version.** The command uses `--dangerously-bypass-approvals-and-sandbox`: isolation comes from the container. Codex's own sandbox relies on kernel features that usually fail inside unprivileged containers.

```toml
# Snippet for ~/.codex/config.toml (replace the base_url placeholder with the proxy URL; the Docker image does this for you)
[model_providers.litellm]
name = "LiteLLM"
base_url = "__LITELLM_BASE_URL__/v1"
env_key = "LITELLM_API_KEY"                         # name of the env var holding the key
wire_api = "responses"                              # use "chat" if your LiteLLM does not expose /responses
env_http_headers = { "x-litellm-tags" = "BENCH_TAG" }  # header <- value of the BENCH_TAG env var
```

Parser: the `usage` in the `turn.completed` event is the cumulative thread total, so the script takes the last one. `cached_input_tokens` is part of `input_tokens`, and reasoning is part of `output_tokens`.

### Cursor (`agent` CLI)

- The official docs for `--output-format json` **have no token or cost fields**. Some versions include `usage`, and the parser reads it when present.
- Without tokens, Cursor reports only pass rate and time. Cost per task does not apply: Cursor plans bill per seat and usage pool, not per token on your API.
- To estimate cost, compare usage on the Cursor dashboard before and after the round.
- **Grok is only available in Cursor**, so the harness effect cannot be isolated for Grok.
- Binary: the docs use `agent`. Older installs may use `cursor-agent`. Adjust `command`.
- Flags used: `-p`, `--output-format json`, `--force` (approves commands) and `--trust` (trusts the workspace in headless mode).

### `null` (negative control)

- The `null` harness (`"parser": "null"`, `"command": ["true"]`) calls no model and changes nothing. Usage is zero and the harness cost is a real `0.0`.
- If a `null` pair solves a task, the hidden tests are too lenient: the report prints a warning. Null pairs are left out of the recommendation and of the discrimination lists.
- It has no LiteLLM spend, so under `--litellm` (cost source `litellm`) its cost is unknown, which is expected.

### Tool calls

Each parser also counts tool calls (`tool_calls`, shown as `Tool calls/run` and `Tool calls/solved`) where the output exposes them, and reports unknown (never `0`) elsewhere: Claude counts unique `tool_use` blocks in `stream-json` `assistant` events (the single JSON result has none); OpenCode counts `tool_use` events (unknown until the first one is seen, so a CLI version that emits none is not reported as 0; an OpenCode run with genuinely zero tool calls is therefore also unknown); Pi counts `toolCall` blocks of assistant `message_end` events; Codex counts completed `command_execution`, `file_change`, `mcp_tool_call` and `web_search` items (unknown until an `item.completed` event is seen); Cursor has no count; `null` is a real 0. One unknown trial makes the pair unknown.

## LiteLLM: what to check

- `reconcile` first tries `GET /spend/logs/v2` (paginated, dates as `YYYY-MM-DD HH:MM:SS`). If that fails, it falls back to the legacy `GET /spend/logs?summarize=false`. Logs are matched to runs by the `request_tags` field, inside the script.
- A non-admin key needs the `get_spend_routes` permission to read spend. Ask whoever runs your LiteLLM proxy.
- Logs may take a few seconds to be written. Run `reconcile` a few minutes after the round ends. You can run it as often as you like.
- If "Unmatched runs" shows up, the tag did not arrive. Check in `results/logs/` and in the LiteLLM dashboard that `x-litellm-tags` is present in that harness's requests.
- Parts of the LiteLLM docs list spend per tag as an enterprise feature. The script does not depend on the tags endpoint: it reads individual logs and filters locally. The `request_tags` field must still be populated.

## Fair comparison

- **Start with the same model across harnesses**: Claude in Claude Code × Pi × OpenCode × Cursor; GPT in Codex × Pi × OpenCode × Cursor. This isolates the harness effect. Compare models afterwards.
- **Clean configuration**: Claude Code loads `~/.claude/CLAUDE.md`, MCP servers and plugins, which add tokens. Run the benchmark with a dedicated user or HOME, and do the same for the other harnesses (`~/.pi/agent/`, `~/.config/opencode/`, `CODEX_HOME`).
- **Same "effort"**: thinking and reasoning effort change cost a lot. Pin them per model and record them in the pair name (e.g. `GPT_MODEL` with `-c model_reasoning_effort=medium` in Codex; `model:high` in Pi).
- **Variance**: use `reps` ≥ 3 and 5–10 tasks before drawing conclusions.

## Security

The agents run **without asking for approval**, and the `test` command executes code the agent wrote. Treat the agent and the task repository as untrusted: a README or issue can carry a prompt injection.

What the script does on every run:

1. **Workdir outside the benchmark repository**: a new temporary directory, or an absolute `work_dir` outside the repository (the script rejects a path inside it). So the agent does not work next to hidden tests, config and results.
2. **No git history**: the task repository is copied without `.git`. A clone at the commit before the fix would still contain the fix commit, and the remote would point back to the benchmark. The single `baseline` commit lives in a private temporary git directory outside the workdir and `work_dir` (deleted after the trial). Every git call on the workdir ignores its `.git`, `.gitignore`, `.gitattributes`, replace refs and git config, and every diff starts from a fresh index, so the agent cannot hide a change or run a command on the host through git. If the agent leaves a `.git` anywhere in the workdir, or `git add` fails (for example on an unreadable file), its changes count as unknown and `diff_constraints` fails; a binary file counts as more than any `max_changed_lines`. This works on any git version (an `info/attributes` file in the baseline git directory does the work; git 2.40 or newer adds `--attr-source` as a second layer). The guarantee holds for sandboxed runs: an unsandboxed agent runs as your user and could find and edit the temporary git directory.
3. **Minimal environment**: the agent gets only `PATH`, `HOME`, `USER`, locale, `TERM`, `TMPDIR`, the variables in `env_passthrough` and those in the harness `env` block. `setup` and `test` get the same, without the harness `env`. The LiteLLM spend-read key (`litellm.api_key`) **never** reaches the agent: the script rejects a config that tries to pass it through.
4. **Protected files**: before the agent runs, each path in `protected_paths` is copied outside the workdir. Before the test, that content is restored and anything the agent created there is deleted. Use it for the test runner and configs (`tests/`, `package.json`, `pytest.ini`, `conftest.py`, `vitest.config.ts`). Without it, an agent can "solve" the task by making the test always pass.
5. **Audit**: `logs/<run>.diff` keeps everything the agent changed relative to the baseline.
6. **Logs without secrets**: values of the harness `env` variables and the spend key are replaced with `***REDACTED***`.
7. **Config validation**: task ids and harness names accept only letters, digits, `_`, `.` and `-`. `repo` and `ref` cannot start with `-`. `protected_paths` must be relative and without `..`. The script deletes only directories inside the work root.
8. **LiteLLM**: `base_url` must use `https` (http only for localhost), and redirects are not followed, so the key does not go to another host.

What the script does **not** solve on its own: without a sandbox, the agent runs as your user and can read any of your files (including `~/.ssh`) and tamper with results. That is why `run` requires `sandbox.prefix` or `--allow-unsandboxed`.

### Docker sandbox

`sandbox.prefix` is the command put in front of the agent, `setup` and `test`. Placeholders: `{workdir}` and `{env_file}`. The `env_file` is a `KEY=value` file with mode 0600, deleted right after each step. The host's `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL` and `TMPDIR` do not go into it: the container uses the image's own.

The `sandbox/Dockerfile` image has:
- Python 3.12, Node.js 22, `git` and `tini`;
- Claude Code, Pi, OpenCode and Codex CLI, through npm. Pi is installed with `--ignore-scripts`, as its docs recommend;
- Cursor CLI, optional, with `--build-arg INSTALL_CURSOR=true`;
- the unprivileged `agent` user, with the same UID/GID as the host. So files in the workdir stay yours;
- an entrypoint that generates the Pi, OpenCode and Codex configs from `LITELLM_BASE_URL`. That is why `LITELLM_BASE_URL` is in `env_passthrough`.

```bash
docker build -f sandbox/Dockerfile -t harness-bench-agents \
  --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
# pinned versions for reproducible results:
#   --build-arg CLAUDE_CODE_VERSION=... --build-arg PI_VERSION=...
#   --build-arg OPENCODE_VERSION=... --build-arg CODEX_VERSION=...
```

The `prefix` in the example configs mounts only the workdir, at the same path, and limits the container:

```json
"prefix": ["docker", "run", "--rm", "--env-file", "{env_file}",
           "-v", "{workdir}:{workdir}", "-w", "{workdir}",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--pids-limit", "512", "--memory", "4g", "--cpus", "2",
           "harness-bench-agents"]
```

- The CLIs authenticate through environment variables (LiteLLM). Your `~/.claude`, `~/.ssh` and similar do not enter the container. Each CLI starts with a clean configuration, which also makes the comparison fairer.
- `.dockerignore` keeps everything not needed out of the build context (configs, results, tasks).
- **Network**: the container has open internet egress, because it must reach LiteLLM. To restrict egress to LiteLLM only, use a Docker network with an egress proxy, or OpenSandbox (below).
- Validate with `python3 tests/docker_e2e.py`. The test runs fake agents inside the container and checks HOME, UID, secrets and the test results.

### Why not OpenSandbox (yet)

[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) is a good option when network control matters: it has a per-domain egress allowlist, enforced at DNS and network level. But it does not fit this script's prefix model:

- it needs a server (`opensandbox-server`) and creates sandboxes through an API (`osb sandbox create`, `osb command run <id>`);
- files go in and out by upload/download, not by bind mount (support for host volumes was not confirmed);
- the client is the `opensandbox` Python SDK, an external dependency, and this script uses only the standard library.

Integrating it would need a dedicated backend: create the sandbox, upload the workdir, run, download the result and destroy it. It is worth it when you need to restrict the agents' egress.

## Choosing tasks

- Use real bugs that were already fixed, with `ref` = the commit before the fix.
- Write the prompt like an issue, with no hint of the solution.
- Use the tests from the fix commit as hidden tests.
- `setup` (e.g. `npm ci`) runs before the agent and does not count toward cost.
- List the repository's tests and test configs in `protected_paths`.

## Graders

A task passes when its graders say so. Without a `graders` key, the single `hidden_tests` grader runs the task's `hidden_tests` copy and `test` command, as before. With one, each entry is an object with a `type` (and an optional `weight`), run in order after the agent finishes:

```json
"graders": [
  {"type": "hidden_tests"},
  {"type": "command", "command": "ruff check .", "timeout": 120},
  {"type": "diff_constraints", "forbidden": ["run_tests.py", "tests/*"], "max_changed_lines": 200}
],
"grade": {"mode": "all"}
```

- `hidden_tests`: copies `hidden_tests` into the workdir, then runs `test` (`test_timeout`, default 900 s).
- `command`: runs `command` (`timeout`, default 900 s). Passes on exit code 0.
- `diff_constraints`: runs nothing. It checks what the agent changed, captured right after the agent finishes and before `protected_paths` are restored: every changed path must match `allowed` (empty means any) and none may match `forbidden` (`fnmatch` globs on paths relative to the workdir; `*` also matches `/`), and the added plus deleted lines must not exceed `max_changed_lines`. A path outside the workdir is always forbidden, and so is an unreadable diff. Renames count as a delete and an add. A binary file counts as more than any `max_changed_lines`. Interpreters leave bytecode (`__pycache__/*.pyc`) in the workdir, and a `.pyc` is binary: list `ignore_lines` globs (for example `["*/__pycache__/*"]`) to leave those paths out of the **line count** only (the `allowed` and `forbidden` checks still apply). Do not ignore bytecode in `forbidden` or `allowed`: a crafted `.pyc` can poison the tests. Instead list the test directories in `protected_paths`, because the restore before the tests deletes everything the agent added there (bytecode included), and write `forbidden` with source globs (`tests/*.py`) so harmless bytecode does not trip it. Keep `ignore_lines` to generated artifacts only: a broad glob such as `*.py` turns `max_changed_lines` off.
- `grade`: `mode` is `all` (default: every grader passes, score is the mean), `any` (one passes, score is the best) or `weighted` (score is the weighted mean using each grader's `weight`, default 1; it passes at `threshold`, required, above 0 and up to 1).
- Partial credit is opt-in: set `"score": true` on a `hidden_tests` or `command` grader and it reads the **last** line `SCORE: <0 to 1>` of the command's **stdout** (stderr is never read). Passing is still exit code 0 without a timeout. Without the option, or without a valid `SCORE` line, the score is 1.0 on pass and 0.0 otherwise (an out-of-range value is ignored with a warning). The test command runs code the agent wrote, which can print a `SCORE` line after the real one, so a score is only as trustworthy as the test output (including a later stdout `SCORE:` line printed by agent code, for example from `atexit`), and a `weighted` verdict with `score: true` inherits exactly that trust. Use `all` or `any` (exit codes only) when the agent can influence the scorer.
- Logs: `<tag>.test.log` for `hidden_tests`, `<tag>.grader-<index>-<type>.log` for the others. Only the engine starts processes, through the same env allowlist, sandbox wrapper and log redaction as the test command.
- `trials.jsonl` lists every grader result. Rows of tasks without `graders` keep the single `test` grader.

### Plugins

Another package can add runners and graders without touching this repository. It registers a factory `(config) -> HarnessRunner | Grader` under the entry-point group `harness_bench.runners` (the harness `parser` name) or `harness_bench.graders` (the grader `type`). Built-in names always win and a plugin that reuses one is an error. Plugins are loaded only when the config names a type that is not built in, and the config is validated before any agent runs. The plugin's package name and version are printed once. A runner builds the argv and parses the output; a grader asks the engine to run commands through `ctx.run`. Neither starts processes.

## `command` placeholders

`{prompt}`, `{model}`, `{provider}` (part before the `/`), `{model_name}` (part after the `/`).
In `env`: `${VAR}` is expanded from the environment and `{run_tag}` becomes the run's tag. `BENCH_TAG` is always exported.
In `sandbox.prefix`: `{workdir}` and `{env_file}`.

---

## `harness_bench.py`

```python
#!/usr/bin/env python3
"""harness-bench: compare coding-agent harness x model pairs.

Metrics per pair: pass rate, tokens, cost per run, cost per solved task,
cost per 1M tokens. Python 3.12+, standard library only.

Usage:
  python3 harness_bench.py run    --config config.json [--reps 3] [--only HARNESS] [--keep] [--dry-run]
  python3 harness_bench.py reconcile --config config.json   # pull real spend from LiteLLM
  python3 harness_bench.py report --config config.json
"""

import functools
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if sys.version_info < (3, 12):
    sys.exit("Python 3.12+ required")

from hbench import cli, report  # noqa: E402
from hbench import runners as _runners  # noqa: E402
from hbench.core.convert import trial_from_row_lenient, trials_from_rows  # noqa: E402
from hbench.core.ids import new_run_id, sha256_json  # noqa: E402
from hbench.core.models import GraderResult, RunInfo, Target  # noqa: E402
from hbench.graders import GRADERS as GRADERS, Change, GradeContext, compose, grade_plan  # noqa: E402
from hbench.results import RunWriter, apply_gateway_costs  # noqa: E402
from hbench.runners import PARSERS as PARSERS, empty_usage as empty_usage, json_lines as json_lines  # noqa: E402
from hbench.runners import num as num, runner_for  # noqa: E402
from hbench.runners.base import build_argv  # noqa: E402

VERSION = "0.0.0"

# The parsers live in `hbench.runners`. They are re-exported under the legacy names with a loose return type,
# because this module and the tests that call it are not strictly typed.
parse_claude: Callable[[str], Any] = _runners.parse_claude
parse_opencode: Callable[[str], Any] = _runners.parse_opencode
parse_pi: Callable[[str], Any] = _runners.parse_pi
parse_cursor: Callable[[str], Any] = _runners.parse_cursor
parse_codex: Callable[[str], Any] = _runners.parse_codex
parse_null: Callable[[str], Any] = _runners.parse_null


# ---------------------------------------------------------------- helpers


def expand_env(value):
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


# ---------------------------------------------------------------- cost


def computed_cost(u, price):
    """Cost from token counts and a USD per 1M tokens price entry."""
    if not price or not any(v for k, v in price.items() if not k.startswith("_")):
        return None  # missing or all-zero price entry: fall back to reported cost
    out_price = price.get("output", 0)
    return (
        u["input"] * price.get("input", 0)
        + (u["output"] + u["reasoning"]) * out_price
        + u["cache_read"] * price.get("cache_read", price.get("input", 0))
        + u["cache_write"] * price.get("cache_write", price.get("input", 0))
    ) / 1e6


# ---------------------------------------------------------------- safety helpers

# Environment the agent and the setup/test commands get. Everything else (cloud
# credentials, tokens, SSH agent, the LiteLLM spend key) stays out unless a harness
# names it explicitly in its "env" block.
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR")
SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def safe(s):
    """Filesystem-safe fragment: no separators, no leading dots."""
    return SAFE_NAME.sub("_", str(s)).lstrip(".") or "_"


def env_var_names(value):
    return set(re.findall(r"\$\{(\w+)\}", value or ""))


def base_env(cfg):
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    for k in cfg.get("env_passthrough", []):
        if k in os.environ:
            env[k] = os.environ[k]
    for k in spend_key_vars(cfg):
        env.pop(k, None)
    return env


def spend_key_vars(cfg):
    return env_var_names((cfg.get("litellm") or {}).get("api_key"))


def redact(text, secrets):
    for s in secrets:
        if s and len(s) >= 8:
            text = text.replace(s, "***REDACTED***")
    return text


def check_cost_source(name):
    if name not in report.COST_SOURCES:
        sys.exit(f"invalid cost_source {name!r}: use one of {', '.join(report.COST_SOURCES)}")


def report_options(cfg):
    """`(recommend_within_pp, target_ci_width)` from the config, validated; exits with a message naming the key."""
    pp = cfg.get("recommend_within_pp", 5)
    width = cfg.get("target_ci_width", 0.2)
    if isinstance(pp, bool) or not isinstance(pp, int | float) or not 0 <= pp <= 100:
        sys.exit(f"invalid recommend_within_pp {pp!r}: use a number from 0 to 100")
    if isinstance(width, bool) or not isinstance(width, int | float) or not 0 < width <= 1:
        sys.exit(f"invalid target_ci_width {width!r}: use a number above 0 and up to 1")
    return pp, width


def validate_config(cfg):
    check_cost_source(cfg.get("cost_source", "auto"))
    report_options(cfg)
    for task in cfg["tasks"]:
        if SAFE_NAME.search(task["id"]) or task["id"].startswith("."):
            sys.exit(f"invalid task id {task['id']!r}: use only letters, digits, _ . - (no leading dot)")
        for field in ("repo", "ref"):
            if str(task.get(field, "")).startswith("-"):
                sys.exit(f"task {task['id']}: {field} must not start with '-'")
        for p in task.get("protected_paths", []):
            if Path(p).is_absolute() or ".." in Path(p).parts:
                sys.exit(f"task {task['id']}: protected path {p!r} must be relative, without '..'")
        try:
            grade_plan(task)
        except ValueError as e:
            sys.exit(f"task {task['id']}: {e}")
        if (
            task.get("hidden_tests")
            and "graders" in task
            and not any(g.get("type") == "hidden_tests" for g in task["graders"])
        ):
            print(
                f"warning: task {task['id']} sets 'hidden_tests' but its graders have no hidden_tests grader: ignored"
            )
    for name, harness in cfg["harnesses"].items():
        if SAFE_NAME.search(name):
            sys.exit(f"invalid harness name {name!r}")
        try:
            runner_for(harness)
        except ValueError as e:
            sys.exit(f"harness {name}: {e}")
    leaked = spend_key_vars(cfg) & (
        set(cfg.get("env_passthrough", []))
        | {v for h in cfg["harnesses"].values() for val in h.get("env", {}).values() for v in env_var_names(val)}
    )
    if leaked:
        sys.exit(f"LiteLLM spend key {sorted(leaked)} must not be passed to agents")


def rmtree_inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or not path.is_relative_to(root):
        raise RuntimeError(f"refusing to delete {path}: outside {root}")
    shutil.rmtree(path, ignore_errors=True)


def wrap(cfg, argv, workdir, env_file):
    """Prefix a command with the configured sandbox (e.g. docker run ...)."""
    prefix = (cfg.get("sandbox") or {}).get("prefix")
    if not prefix:
        return argv
    fields = {"workdir": str(workdir), "env_file": str(env_file)}
    return [p.format(**fields) for p in prefix] + argv


# Host-specific values that would break a container (host PATH, home, user, temp dir).
# The sandbox image provides its own.
HOST_ONLY_ENV = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR"}


def write_env_file(env, path):
    """Docker --env-file format; 0600; deleted after the run."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for k, v in env.items():
            if k not in HOST_ONLY_ENV and "\n" not in v:
                f.write(f"{k}={v}\n")


# ---------------------------------------------------------------- run


def run_proc(argv, cwd, env, timeout):
    """Run argv without a shell; return (rc, timed_out, stdout, stderr)."""
    try:
        p = subprocess.run(
            argv, cwd=cwd, env=env, timeout=timeout, stdin=subprocess.DEVNULL, capture_output=True, text=True
        )
        return p.returncode, False, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return -1, True, out, err


def git_bin():
    """Absolute path of the git executable; exits with a clear message when it is missing."""
    return shutil.which("git") or sys.exit("git not found on PATH")


def git(*args, cwd=None):
    subprocess.run([git_bin(), *args], cwd=cwd, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True)


def is_git_source(src):
    return src.endswith(".git") or "://" in src or src.startswith("git@") or (Path(src) / ".git").exists()


def prepare_workdir(task, base, workdir):
    """Fresh copy of the task repo, without git history.

    History is dropped on purpose: a clone at a pre-fix ref still holds the fix commit,
    and the origin remote would point back into this repo."""
    src = task["repo"]
    if not re.match(r"^(\w+://|git@)", src) and not Path(src).is_absolute():
        src = str((base / src).resolve())
    if is_git_source(src):
        cache = base / ".cache" / safe(task["id"])
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            git("clone", "--quiet", "--", src, str(cache))
        git("clone", "--quiet", "--local", "--", str(cache), str(workdir))
        if task.get("ref"):
            git("-C", str(workdir), "checkout", "--quiet", "--detach", task["ref"])
        shutil.rmtree(workdir / ".git")
    else:
        shutil.copytree(src, workdir, symlinks=True)


# Options of every git call on the agent's workdir, so nothing the agent leaves there can steer or hide the diff:
# no replace refs, no ignore rules from any config, no fsmonitor, no hooks, no untracked cache.
# Defense in depth: the baseline git dir is outside the workdir and its config is ours, so most of these are
# already inert. They stay on because each one closes a way to hide a change or run a command on the host if the
# first line of defense ever fails (a git dir moved into the workdir, a config written next to it, a new git default).
GIT_HARDENING = (
    "--no-replace-objects",
    "-c",
    "core.excludesFile=/dev/null",
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.untrackedCache=false",
)


# Attributes the diff must not see. `info/attributes` in the baseline git dir has the highest precedence, so with this
# line no `.gitattributes` in the workdir can change how a file is added or counted: `working-tree-encoding` would
# make `git add` fail, `-diff` or `binary` would turn counts into `-`, `text`/`eol`/`ident`/`filter` would rewrite
# the content. It works on every git version. Real binary detection (NUL bytes) is not affected.
INFO_ATTRIBUTES = "* !text !eol !crlf !working-tree-encoding !filter !ident !diff !merge !export-subst\n"
# The empty tree: with `--attr-source` git also ignores the workdir's `.gitattributes` (git 2.40 or newer).
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MIN_GIT_FOR_ATTR_SOURCE = (2, 40)


class GitError(Exception):
    """A git call the diff depends on failed. Callers fail closed."""


@functools.cache
def git_version():
    """`(major, minor)` of the git on PATH."""
    out = subprocess.run([git_bin(), "--version"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def git_env(index_file=None):
    """Environment for the hardened git: PATH only, no user or system config, no inherited GIT_* variable.
    `index_file` is the one thing added: a throwaway index for a diff."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        **({"GIT_INDEX_FILE": str(index_file)} if index_file else {}),
    }


def hardened_git(git_dir, work_tree, *args, check=False, timeout=300, index_file=None):
    """Run git against the baseline `git_dir` (outside the workdir) and the agent's `work_tree`.

    The workdir's own `.git` (config, `info/exclude`, index flags, replace refs) is never read: the agent controls
    it, and its config could run commands on the host (`core.fsmonitor`, `filter.*`, `diff.*.textconv`). Its
    `.gitattributes` files are ignored too (`info/attributes` of the git dir, see `make_baseline`)."""
    # Defense in depth: `info/attributes` already outranks the workdir's `.gitattributes`. On git 2.40 or newer
    # `--attr-source` with the empty tree adds a second layer, in case that file is ever missing or changed.
    attr_source = [f"--attr-source={EMPTY_TREE}"] if git_version() >= MIN_GIT_FOR_ATTR_SOURCE else []
    return subprocess.run(
        [
            git_bin(),
            f"--git-dir={git_dir}",
            f"--work-tree={work_tree}",
            *attr_source,
            *GIT_HARDENING,
            *args,
        ],
        cwd=work_tree,
        env=git_env(index_file),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        check=check,
        timeout=timeout,
    )


@dataclass(frozen=True)
class Baseline:
    """The state the agent starts from: a git dir outside the workdir, the workdir and the baseline commit."""

    git_dir: Path
    workdir: Path
    sha: str


def write_info_attributes(git_dir):
    """Write `INFO_ATTRIBUTES` to `<git_dir>/info/attributes`, before the first `git add`."""
    info = Path(git_dir) / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text(INFO_ATTRIBUTES)


def make_baseline(workdir, git_dir):
    """Single commit with the state the agent starts from (after setup), in a git dir outside the workdir.
    Ignore rules do not apply, so files a `.gitignore` lists are part of the baseline too."""
    ident = ["-c", "user.name=harness-bench", "-c", "user.email=bench@localhost", "-c", "commit.gpgsign=false"]
    hardened_git(git_dir, workdir, "init", "--quiet", "-b", "main", check=True)
    try:
        write_info_attributes(git_dir)
    except OSError:
        pass  # `staged_diff` checks the file before every diff and fails closed when it is missing
    hardened_git(git_dir, workdir, "add", "-A", "--force", check=True)
    hardened_git(git_dir, workdir, *ident, "commit", "--quiet", "--allow-empty", "-m", "baseline", check=True)
    sha = hardened_git(git_dir, workdir, "rev-parse", "HEAD", check=True).stdout.strip()
    return Baseline(git_dir=Path(git_dir), workdir=Path(workdir), sha=sha)


def _remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def snapshot_protected(task, workdir, snap):
    """Copy protected paths out of the workdir before the agent runs. The snapshot lives
    outside the workdir, so moving git refs or deleting .git cannot alter it."""
    for rel in task.get("protected_paths", []):
        src, dst = workdir / rel, snap / rel
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, dst, symlinks=True)
        elif src.exists() or src.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst, follow_symlinks=False)


def restore_protected(task, workdir, snap):
    """Undo agent edits to test infrastructure: put back the snapshot of each protected
    path and delete anything the agent added there."""
    for rel in task.get("protected_paths", []):
        target, saved = workdir / rel, snap / rel
        _remove(target)
        if saved.is_dir() and not saved.is_symlink():
            shutil.copytree(saved, target, symlinks=True)
        elif saved.exists() or saved.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target, follow_symlinks=False)


def staged_diff(baseline, *flags):
    """`git diff --cached` of everything in the workdir (new files and ignored files included) against baseline.

    Every call builds a fresh index from the baseline commit, so flags kept in a stored index (`skip-worktree`,
    `assume-unchanged`) have no effect. Raises `GitError` when git cannot read the baseline or add the workdir:
    a diff without those files would compare the baseline with itself and show no change at all."""
    try:
        attributes_ok = (baseline.git_dir / "info" / "attributes").read_text() == INFO_ATTRIBUTES
    except OSError:
        attributes_ok = False
    if not attributes_ok:
        raise GitError("git diff failed: info/attributes of the baseline git dir is missing or changed")
    with tempfile.TemporaryDirectory(prefix="harness-bench-index-") as tmp:
        index = Path(tmp) / "index"
        git_dir, workdir = baseline.git_dir, baseline.workdir
        # Defense in depth: `add` into the fresh index would do, this also checks that the baseline sha exists.
        read = hardened_git(git_dir, workdir, "read-tree", baseline.sha, index_file=index)
        if read.returncode != 0:
            raise GitError(f"git diff failed: {read.stderr.strip()}")
        added = hardened_git(git_dir, workdir, "add", "-A", "--force", index_file=index)
        if added.returncode != 0:
            raise GitError(f"git add failed: {added.stderr.strip()}")
        # Defense in depth: the config is ours, so no external diff or textconv is set; these refuse one anyway.
        return hardened_git(
            git_dir,
            workdir,
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            *flags,
            baseline.sha,
            index_file=index,
        )


def nested_git_entries(workdir):
    """Paths (relative to the workdir) of every `.git` file, directory or link inside it. After the agent ran there
    should be none: `git add` would treat such a directory as a repository and hide what it holds."""
    found = []
    for dirpath, dirnames, filenames in os.walk(workdir):
        found += [str((Path(dirpath) / n).relative_to(workdir)) for n in (*dirnames, *filenames) if n == ".git"]
        dirnames[:] = [d for d in dirnames if d != ".git"]
    return sorted(found)


def save_diff(baseline, path, limit=2_000_000, secrets=()):
    """Everything the agent changed vs baseline, including new files (for audit). A diff that cannot be built is
    reported in the file (stderr redacted with `secrets`), never left empty."""
    nested = nested_git_entries(baseline.workdir)
    note = (
        f"nested .git: {', '.join(nested)} (changes inside them are not shown; counted as unknown)\n" if nested else ""
    )
    try:
        p = staged_diff(baseline, "--stat", "--patch")
        text = p.stdout if p.returncode == 0 else f"git diff failed:\n{p.stderr}"
    except (GitError, subprocess.SubprocessError, OSError) as e:
        text = f"{e}" if isinstance(e, GitError) else f"git diff failed: {e}"
    text = redact(note + text, secrets)
    path.write_text(text[:limit] + ("\n[truncated]" if len(text) > limit else ""))


# A path outside the workdir: `diff_constraints` treats it as forbidden, so an unreadable diff fails closed.
UNKNOWN_CHANGES = (Change(path="../unknown-changes", added=0, deleted=0),)


# What a binary file counts as: more than any `max_changed_lines`. Git prints `-` for it and gives no line count, so
# fail closed; counting it as 0 would let the agent dodge the limit with a blob.
BINARY_LINES = 10**9


def parse_numstat(text):
    """Changes from `git diff --numstat -z --no-renames` output. A binary file (`-`) counts as `BINARY_LINES`."""
    changes = []
    for record in text.split("\0"):
        if not record:
            continue
        added, deleted, path = record.split("\t", 2)
        # A binary file prints `-\t-`: `deleted` stays 0 and `added` carries the whole (huge) count.
        changes.append(
            Change(
                path=path,
                added=int(added) if added.isdigit() else BINARY_LINES,
                deleted=int(deleted) if deleted.isdigit() else 0,
            )
        )
    return tuple(changes)


def collect_changes(baseline):
    """What the agent changed vs baseline as `Change`s (renames show as a delete and an add), for the graders.
    Fails closed with `UNKNOWN_CHANGES` when the diff is unreadable (git cannot add a file, git is too old) or the
    agent left a `.git` in the workdir."""
    if nested_git_entries(baseline.workdir):
        return UNKNOWN_CHANGES
    try:
        p = staged_diff(baseline, "--numstat", "-z", "--no-renames")
        return parse_numstat(p.stdout) if p.returncode == 0 else UNKNOWN_CHANGES
    except (GitError, subprocess.SubprocessError, OSError, ValueError):
        return UNKNOWN_CHANGES


def build_cmd(template, model, prompt):
    return build_argv(template, model, prompt)


def done_keys(results_path):
    keys = set()
    if results_path.exists():
        for obj in json_lines(results_path.read_text()):
            keys.add((obj["task"], obj["harness"], obj["model"], obj["rep"]))
    return keys


def resolve_work_root(cfg, base):
    """Work directories live outside the benchmark repo, so the agent's cwd does not
    sit next to hidden tests, config and results."""
    if cfg.get("work_dir"):
        root = Path(expand_env(cfg["work_dir"])).expanduser().resolve()
        if root.is_relative_to(base.resolve()):
            sys.exit(f"work_dir {root} is inside the benchmark repo; use a path outside it")
        root.mkdir(parents=True, exist_ok=True)
        return root
    return Path(tempfile.mkdtemp(prefix="harness-bench-work-"))


def run_step(cfg, command, env, timeout, log_path, workdir, env_file, secrets):
    """Run a setup or grader command in the sandbox wrapper and write its redacted log (stdout, then stderr).
    Returns `(rc, timed_out, stdout, stderr)`, both redacted and kept apart: a score is only read from stdout."""
    write_env_file(env, env_file)
    try:
        rc, to, out, err = run_proc(wrap(cfg, ["sh", "-c", command], workdir, env_file), workdir, env, timeout)
    finally:
        env_file.unlink(missing_ok=True)
    out, err = redact(out, secrets), redact(err, secrets)
    log_path.write_text(out + "\n--- stderr ---\n" + err)
    return rc, to, out, err


def grade_trial(plan, task, base, workdir, changes, run_for):
    """Run the task's graders over the finished workdir and compose them: `(passed, results)`.

    `run_for(prefix)` gives a grader its `run`: the engine's `run_step`, with log names starting at `prefix`.
    A grader that raises fails (only its exception type is printed, since the message may hold agent output)."""
    results = []
    for index, grader in enumerate(plan.graders):
        prefix = "" if grader.name == "hidden_tests" else f"grader-{index}-"
        ctx = GradeContext(workdir=workdir, task=task, base=base, changes=changes, run=run_for(prefix))
        try:
            results.append(grader.grade(ctx))
        except Exception as e:
            print(f"warning: grader {grader.name} failed with {type(e).__name__}; counted as failed")
            results.append(GraderResult(grader=grader.name, grader_version=grader.version, passed=False, score=0.0))
    passed, _ = compose(results, plan.mode, plan.weights, plan.threshold)
    return passed, results


def make_run_info(cfg, only, sandboxed):
    """Metadata of this `run`. The config hash covers `${VAR}` references, never expanded values."""
    targets = tuple(
        Target(
            harness=pair["harness"],
            harness_version=runner_for(cfg["harnesses"][pair["harness"]]).version(),
            model=pair["model"],
            params={},
        )
        for pair in cfg["matrix"]
        if not only or pair["harness"] in only
    )
    return RunInfo(
        run_id=new_run_id(),
        started_at=datetime.now(UTC),
        finished_at=None,
        harness_bench_version=VERSION,
        config_sha256=sha256_json(cfg),
        suite_version=sha256_json(cfg["tasks"])[:12],
        python=platform.python_version(),
        platform=platform.platform(),
        sandboxed=sandboxed,
        targets=targets,
    )


def cmd_run(args, cfg, base):
    """Run the matrix. The baseline git dirs of the trials live in a private temp dir that is always removed: not in
    `work_dir`, whose names an unsandboxed agent could guess."""
    with tempfile.TemporaryDirectory(prefix="harness-bench-git-") as git_root:
        _run_matrix(args, cfg, base, Path(git_root))


def _run_matrix(args, cfg, base, git_root):
    validate_config(cfg)
    sandboxed = bool((cfg.get("sandbox") or {}).get("prefix"))
    if not sandboxed and not args.dry_run and not args.allow_unsandboxed:
        sys.exit(
            "No sandbox configured. Agents run without approval prompts and the test\n"
            "command executes agent-written code as your user. Set sandbox.prefix in the\n"
            "config (see HARNESS_BENCH.md) or pass --allow-unsandboxed inside a disposable VM."
        )
    out_dir = base / cfg.get("output_dir", "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = out_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    results_path = out_dir / "results.jsonl"
    finished = done_keys(results_path)
    reps = args.reps or cfg.get("reps", 1)
    harnesses = cfg["harnesses"]
    prices = cfg.get("prices", {})
    work_root = None if args.dry_run else resolve_work_root(cfg, base)
    if work_root:
        print(f"work dir: {work_root}")
    secrets_all = [os.environ.get(v, "") for v in spend_key_vars(cfg)]
    plans = {task["id"]: grade_plan(task) for task in cfg["tasks"]}
    writer = None if args.dry_run else RunWriter(out_dir, make_run_info(cfg, args.only, sandboxed))

    for pair in cfg["matrix"]:
        hname, model = pair["harness"], pair["model"]
        if args.only and hname not in args.only:
            continue
        h = harnesses[hname]
        price = prices.get(pair.get("price", model))
        for task in cfg["tasks"]:
            for rep in range(1, reps + 1):
                key = (task["id"], hname, model, rep)
                if key in finished and not args.dry_run:
                    print(f"skip  {key} (done)")
                    continue
                runner = runner_for(h)
                cmd = runner.command(model, task["prompt"])
                if args.dry_run:
                    print(shlex.join(cmd))
                    continue
                tag = f"{safe(task['id'])}__{safe(hname)}__{safe(model)}__r{rep}"
                workdir = work_root / tag
                run_tag = f"bench-{datetime.now().strftime('%Y%m%d%H%M%S')}-{tag}"
                cmd_env = base_env(cfg)
                agent_env = dict(cmd_env, BENCH_TAG=run_tag)  # Pi/OpenCode/Codex send it as x-litellm-tags
                harness_env = {k: expand_env(v).replace("{run_tag}", run_tag) for k, v in h.get("env", {}).items()}
                agent_env.update(harness_env)
                secrets = secrets_all + [v for v in harness_env.values() if v != run_tag]
                if workdir.exists():
                    rmtree_inside(workdir, work_root)
                prepare_workdir(task, base, workdir)
                env_file = work_root / f".{tag}.env"

                if task.get("setup"):
                    run_step(
                        cfg,
                        task["setup"],
                        cmd_env,
                        task.get("setup_timeout", 900),
                        logs_dir / f"{tag}.setup.log",
                        workdir,
                        env_file,
                        secrets,
                    )
                runner.prepare(workdir)
                git_dir = git_root / f"{tag}.git"
                baseline = make_baseline(workdir, git_dir)
                snap = work_root / f".{tag}.protected"
                if snap.exists():
                    rmtree_inside(snap, work_root)
                snap.mkdir()
                snapshot_protected(task, workdir, snap)

                print(f"run   {tag}", flush=True)
                t0 = time.time()
                write_env_file(agent_env, env_file)
                try:
                    rc, timed_out, stdout, stderr = run_proc(
                        wrap(cfg, cmd, workdir, env_file),
                        workdir,
                        agent_env,
                        task.get("timeout", cfg.get("timeout", 1800)),
                    )
                finally:
                    env_file.unlink(missing_ok=True)
                wall = time.time() - t0
                (logs_dir / f"{tag}.agent.jsonl").write_text(redact(stdout, secrets))
                (logs_dir / f"{tag}.agent.stderr").write_text(redact(stderr, secrets))

                usage = runner.parse_usage(stdout)
                parse_ok = usage is not None
                usage = usage or empty_usage()

                # Captured before the protected paths are restored: an edit there must still count as a change.
                changes = collect_changes(baseline)
                save_diff(baseline, logs_dir / f"{tag}.diff", secrets=secrets)
                rmtree_inside(git_dir, git_root)
                restore_protected(task, workdir, snap)
                rmtree_inside(snap, work_root)

                def run_for(prefix, tag=tag, workdir=workdir, env_file=env_file, secrets=secrets, cmd_env=cmd_env):
                    def run(command, timeout, log_name):
                        log = logs_dir / f"{tag}.{prefix}{safe(log_name)}.log"
                        return run_step(cfg, command, cmd_env, timeout, log, workdir, env_file, secrets)

                    return run

                passed, grader_results = grade_trial(plans[task["id"]], task, base, workdir, changes, run_for)
                test_to = any(r.timed_out for r in grader_results)

                row = {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "task": task["id"],
                    "harness": hname,
                    "model": model,
                    "rep": rep,
                    "passed": passed,
                    "agent_rc": rc,
                    "agent_timed_out": timed_out,
                    "wall_s": round(wall, 1),
                    "parse_ok": parse_ok,
                    "run_tag": run_tag,
                    "t_start": t0,
                    "t_end": t0 + wall,
                    "sandboxed": sandboxed,
                    **usage,
                    "cost_computed": computed_cost(usage, price) if parse_ok else None,
                }
                # Built before the legacy row is written, so a bad row cannot leave the two files out of step.
                trial = trial_from_row_lenient(
                    row,
                    run_id=writer.info.run_id,  # dry runs never reach this point, so the writer exists
                    suite_version=writer.info.suite_version,
                    harness_version=runner.version(),
                    test_timed_out=test_to,
                    graders=grader_results if task.get("graders") else None,
                    warn=print,
                )
                with results_path.open("a") as f:
                    f.write(json.dumps(row) + "\n")
                writer.append(trial)
                tokens = sum(usage[k] for k in ("input", "output", "reasoning", "cache_read", "cache_write"))
                print(
                    f"done  {tag} passed={row['passed']} tokens={tokens}"
                    f" cost={row['cost_computed'] if row['cost_computed'] is not None else usage['cost_reported']}",
                    flush=True,
                )
                if not args.keep:
                    rmtree_inside(workdir, work_root)
    if work_root and not args.keep and not cfg.get("work_dir"):
        shutil.rmtree(work_root, ignore_errors=True)
    if writer:
        writer.close(datetime.now(UTC))
    if not args.dry_run and not getattr(args, "no_report", False):
        cmd_report(args, cfg, base)


# ---------------------------------------------------------------- LiteLLM


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the Authorization header must not reach another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_litellm_url(base_url):
    u = urllib.parse.urlparse(base_url)
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in LOCAL_HOSTS):
        return
    sys.exit(f"litellm.base_url must use https (http only for localhost): {base_url}")


def litellm_get(base_url, key, path, params):
    check_litellm_url(base_url)
    url = base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=120) as r:
        return json.loads(r.read().decode())


def litellm_rows(base_url, key, start, end):
    """Individual spend log rows. Tries /spend/logs/v2 (paginated), then legacy /spend/logs."""
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        rows, page = [], 1
        while True:
            data = litellm_get(
                base_url,
                key,
                "/spend/logs/v2",
                {"start_date": start.strftime(fmt), "end_date": end.strftime(fmt), "page": page, "page_size": 1000},
            )
            batch = data.get("data", []) if isinstance(data, dict) else data
            rows += batch
            total_pages = data.get("total_pages", 1) if isinstance(data, dict) else 1
            if page >= total_pages or not batch:
                return rows
            page += 1
    except Exception as e:  # noqa: BLE001
        print(f"/spend/logs/v2 failed ({e}); trying legacy /spend/logs")
    data = litellm_get(
        base_url,
        key,
        "/spend/logs",
        {"start_date": start.strftime("%Y-%m-%d"), "end_date": (end).strftime("%Y-%m-%d"), "summarize": "false"},
    )
    return data.get("data", []) if isinstance(data, dict) else data


def row_tags(r):
    tags = r.get("request_tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = [t.strip() for t in tags.split(",")]
    return tags if isinstance(tags, list) else []


def cmd_reconcile(args, cfg, base):
    lcfg = cfg.get("litellm") or sys.exit("config has no 'litellm' section")
    base_url, key = expand_env(lcfg["base_url"]), expand_env(lcfg["api_key"])
    if not base_url or not key:
        sys.exit("litellm.base_url / litellm.api_key empty (check env vars)")
    results_path = base / cfg.get("output_dir", "results") / "results.jsonl"
    runs = list(json_lines(results_path.read_text()))
    tagged = [r for r in runs if r.get("run_tag")]
    if not tagged:
        sys.exit("no runs with run_tag")
    start = datetime.fromtimestamp(min(r["t_start"] for r in tagged) - 3600, UTC)
    end = datetime.fromtimestamp(max(r["t_end"] for r in tagged) + 3600, UTC)
    logs = litellm_rows(base_url, key, start, end)
    by_tag = {}
    for row in logs:
        for t in row_tags(row):
            by_tag.setdefault(t, []).append(row)
    matched = 0
    spend_by_tag = {}
    for r in runs:
        hits = by_tag.get(r.get("run_tag"), [])
        if hits:
            matched += 1
            r["cost_litellm"] = sum(num(h, "spend") for h in hits)
            spend_by_tag[r["run_tag"]] = r["cost_litellm"]
            r["litellm_requests"] = len(hits)
            r["litellm_prompt_tokens"] = sum(num(h, "prompt_tokens") for h in hits)
            r["litellm_completion_tokens"] = sum(num(h, "completion_tokens") for h in hits)
    results_path.write_text("".join(json.dumps(r) + "\n" for r in runs))
    print(f"LiteLLM: {len(logs)} log rows, {matched}/{len(runs)} runs matched by tag")
    print(f"contract: {apply_gateway_costs(results_path.parent, spend_by_tag)} trials updated")
    if matched < len(tagged):
        print(
            "Unmatched runs: harness did not send x-litellm-tags, run not via LiteLLM "
            "(e.g. Cursor), or logs not written yet."
        )
    cmd_report(args, cfg, base)


# ---------------------------------------------------------------- report


def aggregate(rows, cost_source):
    """Per harness x model entries from legacy rows (the work is done over contract trials)."""
    return report.aggregate(trials_from_rows(rows), cost_source)


def cmd_report(args, cfg, base):
    out_dir = base / cfg.get("output_dir", "results")
    results_path = out_dir / "results.jsonl"
    if not results_path.exists():
        sys.exit(f"no results at {results_path}")
    rows = list(json_lines(results_path.read_text()))
    if not rows:
        sys.exit(f"no results in {results_path}")
    cost_source = getattr(args, "cost_source", None) or cfg.get("cost_source", "auto")
    check_cost_source(cost_source)
    within_pp, target_width = report_options(cfg)
    null_harnesses = {name for name, h in cfg.get("harnesses", {}).items() if h.get("parser") == "null"}
    trials = trials_from_rows(rows, warn=print)
    if getattr(args, "drop_saturated", False):
        trials = report.drop_saturated(trials, null_harnesses)
        if not trials:
            print("no tasks left after --drop-saturated")
            return
    table = report.aggregate(trials, cost_source, null_harnesses=null_harnesses, target_ci_width=target_width)
    report.write_csv(table, out_dir / "summary.csv")
    md = report.render_markdown(
        table, trials, cost_source, datetime.now(), recommend_within_pp=within_pp, target_ci_width=target_width
    )
    (out_dir / "summary.md").write_text(md)
    print("\n" + md)
    print(f"saved {out_dir / 'summary.md'} and {out_dir / 'summary.csv'}")


# ---------------------------------------------------------------- main


def main():
    """Entry point: the single CLI in `hbench.cli` (same commands and flags as before)."""
    cli.main()


if __name__ == "__main__":
    main()
```

## `harness_bench_litellm.py`

```python
#!/usr/bin/env python3
"""harness-bench (LiteLLM only): compare harnesses that bill through LiteLLM.

Same engine as harness_bench.py, with these differences:
- Cursor (and any harness with parser "cursor") is removed from the matrix.
- Cost always comes from LiteLLM spend logs (real spend, matched by per-run tag).
- After the run it waits for LiteLLM to write logs, then reconciles and reports.
- Results go to results-litellm/ by default, separate from the full benchmark.

Usage:
  python3 harness_bench_litellm.py run       --config config.litellm.json [--reps 3] [--only pi codex] [--settle 60]
  python3 harness_bench_litellm.py reconcile --config config.litellm.json
  python3 harness_bench_litellm.py report    --config config.litellm.json

This is a shim over `python3 -m hbench <command> --litellm ...`.
"""

import json
import sys
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit("Python 3.12+ required")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hbench import cli  # noqa: E402
from hbench.profiles import apply_litellm_profile  # noqa: E402

LITELLM_COMMANDS = {"run", "report", "reconcile"}
EPILOG = "The LiteLLM profile is applied automatically to run, report and reconcile (same as passing --litellm)."


def load_config(path):
    """The config with the LiteLLM profile applied, and its directory."""
    cfg_path = Path(path).resolve()
    return apply_litellm_profile(json.loads(cfg_path.read_text())), cfg_path.parent


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in LITELLM_COMMANDS:
        argv = [argv[0], "--litellm", *argv[1:]]
    cli.main(argv, epilog=EPILOG)


if __name__ == "__main__":
    main()
```

## `hbench/` package

New code lives in the `hbench/` package ([ADR 0005](docs/adr/0005-data-contract-and-package.md)); the two scripts above call into it.

`hbench/__init__.py`:

```python
"""harness-bench package: versioned data contract, report, and single CLI (ADR 0005)."""
```

`hbench/core/__init__.py`:

```python
"""The data contract: models, identifiers, JSON Schema and legacy-row conversion."""
```

`hbench/core/ids.py`:

```python
"""Identifiers and content hashes for runs and trials."""

import hashlib
import json
import uuid


def new_run_id() -> str:
    """A fresh run identifier: UUID4, 32 lowercase hex characters."""
    return uuid.uuid4().hex


def trial_id(run_id: str, task_id: str, target_id: str, rep: int) -> str:
    """Deterministic trial identifier: first 16 hex characters of sha256 over the four parts."""
    key = "|".join((run_id, task_id, target_id, str(rep)))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def sha256_json(obj: object) -> str:
    """sha256 hex of the canonical JSON form (sorted keys, no whitespace)."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
```

`hbench/core/models.py`:

```python
"""Versioned data contract: frozen dataclasses that validate themselves and round-trip through dicts.

`to_dict()` gives a JSON-ready dict (enums as values, datetimes as ISO 8601, tuples as lists).
`from_dict()` rejects unknown and missing fields and any `schema_version` with another major version.
Unknown cost is `None`, never 0.
"""

import dataclasses
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum, StrEnum
from types import MappingProxyType, UnionType
from typing import Any, ClassVar, Self, get_args, get_origin, get_type_hints

SCHEMA_VERSION = "1.1.0"
_VERSION_RE = re.compile(r"^(\d+)\.\d+\.\d+$")
_MAJOR = SCHEMA_VERSION.split(".")[0]


class ContractError(ValueError):
    """Data does not satisfy the contract."""


class CostSource(StrEnum):
    GATEWAY = "gateway"
    PRICE_TABLE = "price_table"
    HARNESS = "harness"
    UNKNOWN = "unknown"


class TrialStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"


# ---------------------------------------------------------------- helpers


def _plain(value: object) -> Any:
    """JSON-ready form of a contract value."""
    if isinstance(value, _Model):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _identity(value: Any) -> Any:
    return value


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ContractError(f"expected an ISO 8601 string, got {describe_value(value)}")
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise ContractError(f"invalid ISO 8601 timestamp ({describe_value(value)})") from e


def _optional(decode: Callable[[Any], Any]) -> Callable[[Any], Any]:
    return lambda value: None if value is None else decode(value)


def _tuple_of(decode: Callable[[Any], Any]) -> Callable[[Any], tuple[Any, ...]]:
    def decode_all(value: Any) -> tuple[Any, ...]:
        if not isinstance(value, list):
            raise ContractError(f"expected a list, got {describe_value(value)}")
        return tuple(decode(v) for v in value)

    return decode_all


def _require_aware(name: str, value: datetime) -> None:
    if value.utcoffset() is None:
        raise ContractError(f"{name} must be timezone-aware")


def _check_schema_version(version: str) -> None:
    match = _VERSION_RE.match(version)
    if match is None:
        raise ContractError(f"invalid schema_version {version!r}")
    if match.group(1) != _MAJOR:
        raise ContractError(f"unsupported schema_version {version!r}: this reader understands {_MAJOR}.x.y")


def _check_non_negative(name: str, value: float | None) -> None:
    """Known numbers must be finite and >= 0 (types are checked before, so bool and str never get here)."""
    if value is not None and not (math.isfinite(value) and value >= 0):
        raise ContractError(f"{name} must be a finite number >= 0, got {value}")


def describe_value(value: object) -> str:
    """A value for an error message: numbers as they are, anything else (it may be long or hold content) by type."""
    if isinstance(value, int | float):
        return repr(value)
    return f"a {type(value).__name__}"


def _matches(value: object, expected: Any) -> bool:
    """Whether `value` fits the annotation `expected`: the shapes the contract models use, bool never an int."""
    if expected is type(None):
        return value is None
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        return isinstance(value, int | float) and not isinstance(value, bool)
    origin, args = get_origin(expected), get_args(expected)
    if origin is UnionType:
        return any(_matches(value, option) for option in args)
    if origin is tuple:
        return isinstance(value, tuple) and all(_matches(v, args[0]) for v in value)
    if origin is Mapping:
        return isinstance(value, Mapping) and all(
            _matches(k, args[0]) and _matches(v, args[1]) for k, v in value.items()
        )
    return isinstance(value, expected)


@dataclass(frozen=True, kw_only=True)
class _Model:
    """Base of every contract model: dict conversion and strict field checking."""

    # Keys that `to_dict` adds and `from_dict` accepts but derives again (never stored on the instance).
    DERIVED_KEYS: ClassVar[tuple[str, ...]] = ()
    # Per-field decoders applied by `from_dict`; fields not listed are taken as they are.
    DECODERS: ClassVar[Mapping[str, Callable[[Any], Any]]] = {}

    def __post_init__(self) -> None:
        """Every field must fit its annotation; models with more checks call this first."""
        hints = get_type_hints(type(self))
        for f in fields(self):
            value = getattr(self, f.name)
            if not _matches(value, hints[f.name]):
                raise ContractError(
                    f"{type(self).__name__}.{f.name}: expected {hints[f.name]}, got {describe_value(value)}"
                )

    @classmethod
    def required_keys(cls) -> frozenset[str]:
        """Keys a serialized form must carry: fields without a default, `schema_version` and derived keys."""
        plain = {f.name for f in fields(cls) if f.default is dataclasses.MISSING}
        versioned = {"schema_version"} & {f.name for f in fields(cls)}
        return frozenset(plain | versioned | set(cls.DERIVED_KEYS))

    def to_dict(self) -> dict[str, Any]:
        return {f.name: _plain(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Self:
        names = {f.name for f in fields(cls)}
        extra = set(d) - names - set(cls.DERIVED_KEYS)
        missing = cls.required_keys() - set(d) - set(cls.DERIVED_KEYS)
        if extra or missing:
            raise ContractError(f"{cls.__name__}: unknown fields {sorted(extra)}, missing fields {sorted(missing)}")
        try:
            obj = cls(**{k: cls.DECODERS.get(k, _identity)(v) for k, v in d.items() if k in names})
        except ContractError:
            raise
        except (ValueError, TypeError) as e:
            raise ContractError(f"{cls.__name__}: {e}") from e
        obj._check_derived(d)
        return obj

    def _check_derived(self, d: Mapping[str, Any]) -> None:
        """Hook for models with derived keys: the stored values must agree with the recomputed ones."""


# ---------------------------------------------------------------- models


@dataclass(frozen=True, kw_only=True)
class Target(_Model):
    """What is being measured: a harness at a version driving a model with parameters."""

    harness: str
    harness_version: str
    model: str
    params: Mapping[str, str | int | float | bool]

    def __post_init__(self) -> None:
        super().__post_init__()
        # A private read-only copy: the caller's dict cannot change a frozen model afterwards.
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))

    def __reduce__(self) -> tuple[Any, ...]:
        # MappingProxyType cannot be pickled or deep-copied: rebuild from a plain dict instead.
        return (_make_target, (self.harness, self.harness_version, self.model, dict(self.params)))

    def __hash__(self) -> int:
        return hash((self.harness, self.harness_version, self.model, tuple(sorted(self.params.items()))))

    @property
    def target_id(self) -> str:
        return f"{self.harness}@{self.harness_version}:{self.model}"


def _make_target(harness: str, harness_version: str, model: str, params: dict[str, Any]) -> Target:
    return Target(harness=harness, harness_version=harness_version, model=model, params=params)


@dataclass(frozen=True, kw_only=True)
class GraderResult(_Model):
    grader: str
    grader_version: str
    passed: bool
    score: float | None = None
    timed_out: bool = False
    details_uri: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.score is not None and not 0 <= self.score <= 1:
            raise ContractError(f"score must be in [0, 1], got {self.score}")


@dataclass(frozen=True, kw_only=True)
class Costs(_Model):
    """Every known cost of a trial in USD; `None` means unknown (never 0)."""

    gateway: float | None = None
    price_table: float | None = None
    harness: float | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in ("gateway", "price_table", "harness"):
            _check_non_negative(f"cost {name}", getattr(self, name))

    def pick(self, source: CostSource | None = None) -> tuple[float | None, CostSource]:
        """The cost and its source: the forced `source`, or by order gateway > price_table > harness."""
        by_source = {
            CostSource.GATEWAY: self.gateway,
            CostSource.PRICE_TABLE: self.price_table,
            CostSource.HARNESS: self.harness,
        }
        order = tuple(by_source) if source is None else (source,)
        for candidate in order:
            value = by_source.get(candidate)
            if value is not None:
                return value, candidate
        return None, CostSource.UNKNOWN


_COUNT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "turns",
    "tool_calls",
)


@dataclass(frozen=True, kw_only=True)
class Usage(_Model):
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    turns: int = 0
    parsed: bool
    costs: Costs
    tool_calls: int | None = None

    DERIVED_KEYS: ClassVar[tuple[str, ...]] = ("cost_usd", "cost_source")
    DECODERS: ClassVar[Mapping[str, Callable[[Any], Any]]] = {"costs": Costs.from_dict}

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in _COUNT_FIELDS:
            _check_non_negative(name, getattr(self, name))

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.reasoning_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def cost_usd(self) -> float | None:
        return self.costs.pick()[0]

    @property
    def cost_source(self) -> CostSource:
        return self.costs.pick()[1]

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "cost_usd": self.cost_usd, "cost_source": self.cost_source.value}

    def _check_derived(self, d: Mapping[str, Any]) -> None:
        stored = {k: d[k] for k in self.DERIVED_KEYS if k in d}
        if stored and stored != {k: v for k, v in self.to_dict().items() if k in stored}:
            raise ContractError(f"Usage: {stored} disagrees with the costs {self.costs}")


@dataclass(frozen=True, kw_only=True)
class TrialResult(_Model):
    """One attempt of one task by one target. Holds references only: no prompts, stdout or diffs."""

    schema_version: str = SCHEMA_VERSION
    run_id: str
    trial_id: str
    task_id: str
    suite_version: str
    target: Target
    rep: int
    status: TrialStatus
    graders: tuple[GraderResult, ...]
    usage: Usage
    latency_s: float
    started_at: datetime
    agent_exit_code: int | None
    sandboxed: bool
    image_digest: str | None = None
    trace_id: str | None = None
    artifact_uri: str | None = None

    DECODERS: ClassVar[Mapping[str, Callable[[Any], Any]]] = {
        "target": Target.from_dict,
        "status": TrialStatus,
        "graders": _tuple_of(GraderResult.from_dict),
        "usage": Usage.from_dict,
        "started_at": _parse_datetime,
    }

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_schema_version(self.schema_version)
        if self.rep < 1:
            raise ContractError(f"rep must be >= 1, got {self.rep}")
        _check_non_negative("latency_s", self.latency_s)
        _require_aware("started_at", self.started_at)

    @property
    def passed(self) -> bool:
        return self.status is TrialStatus.PASSED


@dataclass(frozen=True, kw_only=True)
class RunInfo(_Model):
    """Metadata of one `run` invocation (`run.json`)."""

    schema_version: str = SCHEMA_VERSION
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    harness_bench_version: str
    config_sha256: str
    suite_version: str
    python: str
    platform: str
    sandboxed: bool
    targets: tuple[Target, ...]

    DECODERS: ClassVar[Mapping[str, Callable[[Any], Any]]] = {
        "started_at": _parse_datetime,
        "finished_at": _optional(_parse_datetime),
        "targets": _tuple_of(Target.from_dict),
    }

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_schema_version(self.schema_version)
        _require_aware("started_at", self.started_at)
        if self.finished_at is not None:
            _require_aware("finished_at", self.finished_at)
```

`hbench/core/schema.py`:

```python
"""JSON Schema (draft 2020-12) for `trials.jsonl` lines and `run.json`, plus a small stdlib validator.

The schema is written by hand. `validate` supports only the keywords the schema uses and raises
`ValueError` on any other, so the schema cannot silently rely on something that is not checked.
`tests/test_core_schema.py` fails when the schema and the dataclass fields drift apart.
"""

import math
from collections.abc import Mapping
from typing import Any

from hbench.core.models import SCHEMA_VERSION, CostSource, TrialStatus

_ID_BASE = "https://harness-bench.local/schema"
_DIALECT = "https://json-schema.org/draft/2020-12/schema"

_STRING: dict[str, Any] = {"type": "string"}
_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}
_BOOLEAN: dict[str, Any] = {"type": "boolean"}
_COUNT: dict[str, Any] = {"type": "integer", "minimum": 0}
_COST: dict[str, Any] = {"type": ["number", "null"], "minimum": 0}


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": sorted(required), "additionalProperties": False}


_TARGET = _object(
    {
        "harness": _STRING,
        "harness_version": _STRING,
        "model": _STRING,
        # Free-form map of scalar settings: keys are not known in advance.
        "params": {"type": "object"},
    },
    ["harness", "harness_version", "model", "params"],
)

_GRADER_RESULT = _object(
    {
        "grader": _STRING,
        "grader_version": _STRING,
        "passed": _BOOLEAN,
        "score": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "timed_out": _BOOLEAN,
        "details_uri": _NULLABLE_STRING,
    },
    ["grader", "grader_version", "passed"],
)

_COSTS = _object({"gateway": _COST, "price_table": _COST, "harness": _COST}, [])

_USAGE = _object(
    {
        "input_tokens": _COUNT,
        "output_tokens": _COUNT,
        "reasoning_tokens": _COUNT,
        "cache_read_tokens": _COUNT,
        "cache_write_tokens": _COUNT,
        "turns": _COUNT,
        "tool_calls": {"type": ["integer", "null"], "minimum": 0},
        "parsed": _BOOLEAN,
        "costs": {"$ref": "#/$defs/costs"},
        "cost_usd": _COST,
        "cost_source": {"enum": [s.value for s in CostSource]},
    },
    ["input_tokens", "output_tokens", "parsed", "costs", "cost_usd", "cost_source"],
)

TRIAL_SCHEMA: dict[str, Any] = {
    "$schema": _DIALECT,
    "$id": f"{_ID_BASE}/trial/{SCHEMA_VERSION}",
    "title": "Trial result",
    "$defs": {"target": _TARGET, "grader_result": _GRADER_RESULT, "costs": _COSTS, "usage": _USAGE},
    **_object(
        {
            "schema_version": _STRING,
            "run_id": _STRING,
            "trial_id": _STRING,
            "task_id": _STRING,
            "suite_version": _STRING,
            "target": {"$ref": "#/$defs/target"},
            "rep": {"type": "integer", "minimum": 1},
            "status": {"enum": [s.value for s in TrialStatus]},
            "graders": {"type": "array", "items": {"$ref": "#/$defs/grader_result"}},
            "usage": {"$ref": "#/$defs/usage"},
            "latency_s": {"type": "number", "minimum": 0},
            "started_at": _STRING,
            "agent_exit_code": {"type": ["integer", "null"]},
            "sandboxed": _BOOLEAN,
            "image_digest": _NULLABLE_STRING,
            "trace_id": _NULLABLE_STRING,
            "artifact_uri": _NULLABLE_STRING,
        },
        [
            "schema_version",
            "run_id",
            "trial_id",
            "task_id",
            "suite_version",
            "target",
            "rep",
            "status",
            "graders",
            "usage",
            "latency_s",
            "started_at",
            "agent_exit_code",
            "sandboxed",
        ],
    ),
}

RUN_SCHEMA: dict[str, Any] = {
    "$schema": _DIALECT,
    "$id": f"{_ID_BASE}/run/{SCHEMA_VERSION}",
    "title": "Run info",
    "$defs": {"target": _TARGET},
    **_object(
        {
            "schema_version": _STRING,
            "run_id": _STRING,
            "started_at": _STRING,
            "finished_at": _NULLABLE_STRING,
            "harness_bench_version": _STRING,
            "config_sha256": _STRING,
            "suite_version": _STRING,
            "python": _STRING,
            "platform": _STRING,
            "sandboxed": _BOOLEAN,
            "targets": {"type": "array", "items": {"$ref": "#/$defs/target"}},
        },
        [
            "schema_version",
            "run_id",
            "started_at",
            "finished_at",
            "harness_bench_version",
            "config_sha256",
            "suite_version",
            "python",
            "platform",
            "sandboxed",
            "targets",
        ],
    ),
}


def schema_document() -> dict[str, Any]:
    """Both schemas in one document, as `schema export` prints it."""
    return {"trial": TRIAL_SCHEMA, "run": RUN_SCHEMA}


# ---------------------------------------------------------------- validator

_ANNOTATIONS = {"$schema", "$id", "title", "description"}
_KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "minimum",
    "maximum",
    "$defs",
    "$ref",
} | _ANNOTATIONS

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    # bool is a subclass of int in Python but is not a JSON number.
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    # JSON has no NaN or Infinity.
    "number": lambda v: (
        isinstance(v, int | float) and not isinstance(v, bool) and (isinstance(v, int) or math.isfinite(v))
    ),
}


def validate(instance: object, schema: Mapping[str, Any]) -> list[str]:
    """Errors found in `instance` (empty when it conforms), as `"$.path: message"` strings."""
    errors: list[str] = []
    _check(instance, schema, schema, "$", errors)
    return errors


def _resolve(ref: str, root: Mapping[str, Any]) -> Mapping[str, Any]:
    prefix = "#/$defs/"
    defs: Mapping[str, Any] = root.get("$defs", {})
    name = ref[len(prefix) :]
    if not ref.startswith(prefix) or name not in defs:
        raise ValueError(f"unresolvable $ref {ref!r}")
    resolved: Mapping[str, Any] = defs[name]
    return resolved


def _type_names(schema: Mapping[str, Any]) -> list[str]:
    declared = schema["type"]
    names: list[str] = [declared] if isinstance(declared, str) else list(declared)
    unknown = [n for n in names if n not in _TYPE_CHECKS]
    if unknown:
        raise ValueError(f"unsupported type {unknown[0]!r}")
    return names


def _check(instance: object, schema: Mapping[str, Any], root: Mapping[str, Any], path: str, errors: list[str]) -> None:
    unsupported = set(schema) - _KEYWORDS
    if unsupported:
        raise ValueError(f"unsupported schema keyword {sorted(unsupported)[0]!r}")
    if "$ref" in schema:
        _check(instance, _resolve(schema["$ref"], root), root, path, errors)
        return
    if "type" in schema:
        names = _type_names(schema)
        if not any(_TYPE_CHECKS[n](instance) for n in names):
            errors.append(f"{path}: expected {' or '.join(names)}")
            return
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']}")
    _check_bounds(instance, schema, path, errors)
    if isinstance(instance, dict):
        _check_object(instance, schema, root, path, errors)
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            _check(item, schema["items"], root, f"{path}[{i}]", errors)


def _check_bounds(instance: object, schema: Mapping[str, Any], path: str, errors: list[str]) -> None:
    if isinstance(instance, bool) or not isinstance(instance, int | float):
        return
    if "minimum" in schema and instance < schema["minimum"]:
        errors.append(f"{path}: must be >= {schema['minimum']}")
    if "maximum" in schema and instance > schema["maximum"]:
        errors.append(f"{path}: must be <= {schema['maximum']}")


def _check_object(
    instance: dict[str, Any], schema: Mapping[str, Any], root: Mapping[str, Any], path: str, errors: list[str]
) -> None:
    properties: Mapping[str, Any] = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in instance:
            errors.append(f"{path}: missing required property {key!r}")
    for key, value in instance.items():
        if key in properties:
            _check(value, properties[key], root, f"{path}.{key}", errors)
        elif schema.get("additionalProperties") is False:
            errors.append(f"{path}.{key}: unexpected property")
```

`hbench/core/convert.py`:

```python
"""Convert a legacy `results.jsonl` row into a contract `TrialResult`.

The legacy row stays the resume/reconcile record; the contract is derived from it. Rows written before
Phase 1 lack some keys (`parse_ok`, `sandboxed`, `cost_*`), so every optional key has a documented default.
"""

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from hbench.core.ids import trial_id
from hbench.core.models import (
    ContractError,
    Costs,
    describe_value,
    GraderResult,
    Target,
    TrialResult,
    TrialStatus,
    Usage,
)

_TAG_RE = re.compile(r"^bench-\d{14}-(.+)$")
_EPOCH = datetime.fromtimestamp(0, UTC)
# Run id of trials rebuilt from `results.jsonl` for reporting: they belong to no `run.json`.
LEGACY_RUN_ID = "legacy"
_NUMBER_KEYS = ("input", "output", "reasoning", "cache_read", "cache_write", "turns")
_COST_KEYS = ("cost_litellm", "cost_computed", "cost_reported")


def _started_at(row: Mapping[str, Any]) -> datetime:
    """`ts` (ISO 8601; naive means UTC), else `t_start` (epoch seconds), else the epoch."""
    try:
        ts = datetime.fromisoformat(row["ts"])
        return ts if ts.utcoffset() is not None else ts.replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError):
        pass
    if isinstance(row.get("t_start"), int | float):
        return datetime.fromtimestamp(row["t_start"], UTC)
    return _EPOCH


def _status(passed: bool, timed_out: bool) -> TrialStatus:
    if passed:
        return TrialStatus.PASSED
    return TrialStatus.TIMEOUT if timed_out else TrialStatus.FAILED


def _count(name: str, value: Any) -> int:
    """A token or turn count from untrusted parser output: a whole number >= 0 (a float is fine if integral)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ContractError(f"{name} must be a whole number, got {describe_value(value)}")
    if isinstance(value, float) and not (math.isfinite(value) and value.is_integer()):
        raise ContractError(f"{name} must be a whole number, got {value!r}")
    if value < 0:
        raise ContractError(f"{name} must be a whole number >= 0, got {value!r}")
    return int(value)


def _artifact_uri(run_tag: str | None) -> str | None:
    """Log path prefix relative to the output dir (`logs/<tag>`), or None for a tag of another shape."""
    match = _TAG_RE.match(run_tag or "")
    return f"logs/{match.group(1)}" if match else None


def trial_from_row(
    row: Mapping[str, Any],
    *,
    run_id: str,
    suite_version: str,
    harness_version: str = "unknown",
    test_timed_out: bool = False,
    graders: Sequence[GraderResult] | None = None,
) -> TrialResult:
    target = Target(harness=row["harness"], harness_version=harness_version, model=row["model"], params={})
    passed = bool(row["passed"])
    usage = Usage(
        input_tokens=_count("input_tokens", row["input"]),
        output_tokens=_count("output_tokens", row["output"]),
        reasoning_tokens=_count("reasoning_tokens", row.get("reasoning", 0)),
        cache_read_tokens=_count("cache_read_tokens", row["cache_read"]),
        cache_write_tokens=_count("cache_write_tokens", row["cache_write"]),
        turns=_count("turns", row["turns"]),
        tool_calls=None if row.get("tool_calls") is None else _count("tool_calls", row["tool_calls"]),
        parsed=bool(row.get("parse_ok", True)),
        costs=Costs(
            gateway=row.get("cost_litellm"), price_table=row.get("cost_computed"), harness=row.get("cost_reported")
        ),
    )
    return TrialResult(
        run_id=run_id,
        trial_id=trial_id(run_id, row["task"], target.target_id, row["rep"]),
        task_id=row["task"],
        suite_version=suite_version,
        target=target,
        rep=row["rep"],
        status=_status(passed, bool(row.get("agent_timed_out")) or test_timed_out),
        graders=tuple(graders)
        if graders is not None
        else (GraderResult(grader="test", grader_version="1", passed=passed, timed_out=test_timed_out),),
        usage=usage,
        latency_s=row["wall_s"],
        started_at=_started_at(row),
        agent_exit_code=row.get("agent_rc"),
        sandboxed=bool(row.get("sandboxed", False)),
        trace_id=row.get("run_tag"),
        artifact_uri=_artifact_uri(row.get("run_tag")),
    )


def _label(row: Mapping[str, Any]) -> str:
    return str(row.get("run_tag") or f"{row.get('task')}/{row.get('harness')}/{row.get('model')} rep {row.get('rep')}")


def _sanitized(row: Mapping[str, Any], warn: Callable[[str], None] | None) -> dict[str, Any]:
    """A copy of `row` without its bad fields. A bad cost becomes unknown (the other costs are kept); a bad
    count zeroes all counts, drops the price-table cost computed from them and marks the row as unparsed.
    Every bad field is reported with `warn`."""
    clean = dict(row)
    bad_count = False
    for key in _NUMBER_KEYS:
        try:
            _count(key, row.get(key, 0))
        except ContractError as e:
            bad_count = True
            if warn:
                warn(f"warning: {_label(row)}: {e}; counts dropped, trial marked as unparsed")
    if bad_count:
        clean.update(dict.fromkeys(_NUMBER_KEYS, 0), parse_ok=False)
        if clean.get("cost_computed") is not None:
            # The price-table cost was derived from these counts, so it is as wrong as they are.
            clean["cost_computed"] = None
            if warn:
                warn(f"warning: {_label(row)}: price-table cost dropped because the counts are invalid")
    for key in _COST_KEYS:
        try:
            Costs(harness=clean.get(key))
        except ContractError as e:
            clean[key] = None
            if warn:
                warn(f"warning: {_label(row)}: {key}: {e}; cost treated as unknown")
    if row.get("tool_calls") is not None:
        try:
            _count("tool_calls", row["tool_calls"])
        except ContractError as e:
            clean["tool_calls"] = None
            if warn:
                warn(f"warning: {_label(row)}: {e}; tool calls treated as unknown")
    return clean


def trial_from_row_lenient(
    row: Mapping[str, Any],
    *,
    run_id: str,
    suite_version: str,
    harness_version: str = "unknown",
    test_timed_out: bool = False,
    graders: Sequence[GraderResult] | None = None,
    warn: Callable[[str], None] | None = None,
) -> TrialResult:
    """Like `trial_from_row`, but fields that break the contract (negative, NaN, bool, fractional: parser output
    is untrusted) are dropped one by one instead of raising. See `_sanitized`."""
    kwargs = {"run_id": run_id, "suite_version": suite_version, "harness_version": harness_version}
    try:
        return trial_from_row(row, test_timed_out=test_timed_out, graders=graders, **kwargs)
    except ContractError:
        return trial_from_row(_sanitized(row, warn), test_timed_out=test_timed_out, graders=graders, **kwargs)


def trials_from_rows(rows: Iterable[Mapping[str, Any]], warn: Callable[[str], None] | None = None) -> list[TrialResult]:
    """Trials for reporting over legacy rows. A hand-written row with rep 0 is read as rep 1."""
    return [
        trial_from_row_lenient(
            {**row, "rep": max(row["rep"], 1)}, run_id=LEGACY_RUN_ID, suite_version="unknown", warn=warn
        )
        for row in rows
    ]
```

`hbench/runners/__init__.py`:

```python
"""Harness runners: the protocol, the built-in output parsers and the registry (ADR 0007)."""

from collections.abc import Mapping
from typing import Any, TypeGuard

from hbench.plugins import RUNNER_GROUP, create_plugin

from hbench.runners._usage import Usage, empty_usage, json_lines, num
from hbench.runners.base import HarnessRunner, ParseUsage, TemplateRunner
from hbench.runners.claude import parse_claude
from hbench.runners.codex import parse_codex
from hbench.runners.cursor import parse_cursor
from hbench.runners.null import parse_null
from hbench.runners.opencode import parse_opencode
from hbench.runners.pi import parse_pi

__all__ = [
    "PARSERS",
    "HarnessRunner",
    "TemplateRunner",
    "Usage",
    "empty_usage",
    "json_lines",
    "num",
    "parse_claude",
    "parse_codex",
    "parse_cursor",
    "parse_null",
    "parse_opencode",
    "parse_pi",
    "runner_for",
]

PARSERS: Mapping[str, ParseUsage] = {
    "claude": parse_claude,
    "opencode": parse_opencode,
    "pi": parse_pi,
    "cursor": parse_cursor,
    "codex": parse_codex,
    "null": parse_null,
}


def _is_runner(obj: object) -> TypeGuard[HarnessRunner]:
    return isinstance(obj, HarnessRunner)


def runner_for(harness_cfg: Mapping[str, Any]) -> HarnessRunner:
    """The runner of a harness config entry: a built-in parser, else the plugin of that name.
    Raises ValueError (a `PluginError` for an unknown name) when the entry is unusable."""
    parser = harness_cfg.get("parser")
    if not isinstance(parser, str):
        raise ValueError("a harness needs a 'parser' name")
    if parser in PARSERS:
        return TemplateRunner(harness_cfg, PARSERS[parser])
    return create_plugin(RUNNER_GROUP, parser, PARSERS, harness_cfg, _is_runner, "HarnessRunner")
```

`hbench/runners/base.py`:

```python
"""The runner protocol and the adapter built from a harness config entry.

A runner builds the agent argv and parses its output. It never starts a process: the engine
runs the argv through `wrap()` and the env allowlist (ADR 0007).
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from hbench.runners._usage import Usage

ParseUsage = Callable[[str], Usage | None]


@runtime_checkable
class HarnessRunner(Protocol):
    """A harness as an adapter: argv builder and usage parser."""

    name: str

    def prepare(self, workdir: Path) -> None:
        """Hook run on the host before the agent starts (default: nothing to do)."""

    def command(self, model: str, prompt: str) -> list[str]:
        """The agent argv for this model and prompt."""

    def parse_usage(self, stdout: str) -> Usage | None:
        """Usage from the agent stdout, or None when the format has none."""

    def version(self) -> str:
        """The harness version recorded in the results."""


def build_argv(template: list[str], model: str, prompt: str) -> list[str]:
    """Fill `{model}`, `{provider}`, `{model_name}` and `{prompt}` in each template part."""
    provider, _, model_name = model.partition("/") if "/" in model else ("", "", model)
    fields = {"model": model, "provider": provider, "model_name": model_name, "prompt": prompt}
    return [part.format(**fields) for part in template]


class TemplateRunner:
    """Runner for a harness config entry: `command` template, `parser` name, optional `version`."""

    def __init__(self, harness_cfg: Mapping[str, Any], parse: ParseUsage) -> None:
        template = harness_cfg.get("command")
        if not isinstance(template, list) or not all(isinstance(part, str) for part in template):
            raise ValueError("a harness needs a 'command' list of strings")
        self.name: str = harness_cfg["parser"]
        self._template: list[str] = template
        self._version: str = harness_cfg.get("version", "unknown")
        self._parse = parse

    def prepare(self, workdir: Path) -> None:
        """Template harnesses need no preparation."""

    def command(self, model: str, prompt: str) -> list[str]:
        return build_argv(self._template, model, prompt)

    def parse_usage(self, stdout: str) -> Usage | None:
        return self._parse(stdout)

    def version(self) -> str:
        return self._version
```

`hbench/runners/_usage.py`:

```python
"""Helpers shared by the output parsers: numeric lookup, JSONL reading and the empty usage dict."""

import json
from collections.abc import Iterator
from typing import Any

Usage = dict[str, Any]


def num(d: object, *keys: str) -> int | float:
    """First numeric value found under any of keys, else 0."""
    if not isinstance(d, dict):
        return 0
    for k in keys:
        v = d.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool):
            return v
    return 0


def json_lines(text: str) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from a JSONL stream; skip non-JSON lines."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def empty_usage() -> Usage:
    return {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cost_reported": None,
        "turns": 0,
        "tool_calls": None,
    }
```

`hbench/runners/claude.py`:

```python
"""Parser for `claude -p --output-format json`."""

import json

from collections.abc import Iterable
from typing import Any

from hbench.runners._usage import Usage, empty_usage, json_lines, num


def _count_tool_calls(events: Iterable[dict[str, Any]]) -> int | None:
    """Unique `tool_use` blocks over the stream-json `assistant` events; None when there are no such events
    (the single-result JSON format does not list tool calls)."""
    seen_assistant, ids, anonymous = False, set(), 0
    for event in events:
        if event.get("type") != "assistant":
            continue
        seen_assistant = True
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if block.get("id") is None:
                    anonymous += 1
                else:
                    ids.add(str(block["id"]))
    return len(ids) + anonymous if seen_assistant else None


def parse_claude(stdout: str) -> Usage | None:
    """claude -p --output-format json (single result object; stream-json also works)."""
    result = None
    events = list(json_lines(stdout))
    for obj in events:
        if obj.get("type") == "result":
            result = obj
    if result is None:
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            return None
    u = empty_usage()
    model_usage = result.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        # Includes side calls (subagents, small helper model), not only the main model.
        for m in model_usage.values():
            u["input"] += num(m, "inputTokens")
            u["output"] += num(m, "outputTokens")
            u["cache_read"] += num(m, "cacheReadInputTokens")
            u["cache_write"] += num(m, "cacheCreationInputTokens")
    else:
        usage = result.get("usage", {})
        u["input"] = num(usage, "input_tokens")
        u["output"] = num(usage, "output_tokens")
        u["cache_read"] = num(usage, "cache_read_input_tokens")
        u["cache_write"] = num(usage, "cache_creation_input_tokens")
    if isinstance(result.get("total_cost_usd"), int | float):
        u["cost_reported"] = result["total_cost_usd"]
    u["turns"] = num(result, "num_turns")
    u["tool_calls"] = _count_tool_calls(events)
    return u
```

`hbench/runners/opencode.py`:

```python
"""Parser for `opencode run --format json`."""

from hbench.runners._usage import Usage, empty_usage, json_lines, num


def parse_opencode(stdout: str) -> Usage | None:
    """opencode run --format json: sum every step_finish event."""
    u = empty_usage()
    cost, seen = 0.0, False
    tool_calls: int | None = None  # unknown until a `tool_use` event shows this CLI version reports them
    for obj in json_lines(stdout):
        kind = str(obj.get("type", "")).replace("-", "_")
        if kind == "tool_use":
            tool_calls = (tool_calls or 0) + 1
        if kind != "step_finish":
            continue
        part = obj.get("part", obj)
        tok = part.get("tokens", {}) or {}
        cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
        u["input"] += num(tok, "input")
        u["output"] += num(tok, "output")
        u["reasoning"] += num(tok, "reasoning")
        u["cache_read"] += num(cache, "read") or num(tok, "cacheRead", "cache_read")
        u["cache_write"] += num(cache, "write") or num(tok, "cacheWrite", "cache_write")
        cost += num(part, "cost")
        u["turns"] += 1
        seen = True
    if not seen:
        return None
    u["cost_reported"] = cost
    u["tool_calls"] = tool_calls
    return u
```

`hbench/runners/pi.py`:

```python
"""Parser for `pi --mode json`."""

from hbench.runners._usage import Usage, empty_usage, json_lines, num


def parse_pi(stdout: str) -> Usage | None:
    """pi --mode json: sum usage of assistant messages on message_end events only."""
    u = empty_usage()
    cost, seen, has_cost = 0.0, False, False
    tool_calls: int | None = None
    for obj in json_lines(stdout):
        if obj.get("type") != "message_end":
            continue
        msg = obj.get("message", {}) or {}
        if msg.get("role") != "assistant":
            continue
        usage = msg.get("usage", {}) or {}
        u["input"] += num(usage, "input", "inputTokens", "input_tokens")
        u["output"] += num(usage, "output", "outputTokens", "output_tokens")
        u["cache_read"] += num(usage, "cacheRead", "cache_read")
        u["cache_write"] += num(usage, "cacheWrite", "cache_write")
        c = usage.get("cost")
        if isinstance(c, dict) and isinstance(c.get("total"), int | float):
            cost += c["total"]
            has_cost = True
        elif isinstance(c, int | float):
            cost += c
            has_cost = True
        u["turns"] += 1
        seen = True
        content = msg.get("content")
        if isinstance(content, list):
            calls = sum(1 for block in content if isinstance(block, dict) and block.get("type") == "toolCall")
            tool_calls = (tool_calls or 0) + calls
    if not seen:
        return None
    u["cost_reported"] = cost if has_cost else None
    u["tool_calls"] = tool_calls
    return u
```

`hbench/runners/cursor.py`:

```python
"""Parser for `agent -p --output-format json` (Cursor)."""

from hbench.runners._usage import Usage, empty_usage, json_lines, num


def parse_cursor(stdout: str) -> Usage | None:
    """agent -p --output-format json. Official docs show no usage fields; some versions
    emit usage{inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens}. Returns None
    when no usage is present (pass rate and time are still recorded)."""
    result = None
    for obj in json_lines(stdout):
        if obj.get("type") == "result":
            result = obj
    if not result or not isinstance(result.get("usage"), dict):
        return None
    usage = result["usage"]
    u = empty_usage()
    u["input"] = num(usage, "inputTokens", "input_tokens")
    u["output"] = num(usage, "outputTokens", "output_tokens")
    u["cache_read"] = num(usage, "cacheReadTokens", "cache_read_tokens")
    u["cache_write"] = num(usage, "cacheWriteTokens", "cache_write_tokens")
    cost = num(usage, "totalCostUSD") or num(result, "total_cost_usd")
    u["cost_reported"] = cost or None
    u["turns"] = 1  # `tool_calls` stays unknown: the JSON result does not list them
    return u
```

`hbench/runners/codex.py`:

```python
"""Parser for `codex exec --json`."""

from typing import Any

from hbench.runners._usage import Usage, empty_usage, json_lines, num

# `item.completed` item types that are the agent acting on the world (not messages or reasoning).
TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search"}


def _item_type(event: dict[str, Any]) -> object:
    item = event.get("item")
    return item.get("type") if isinstance(item, dict) else None


def parse_codex(stdout: str) -> Usage | None:
    """codex exec --json: usage on turn.completed is the thread's running total, so take
    the last one. cached_input_tokens is a subset of input_tokens; reasoning_output_tokens
    is a subset of output_tokens (not added again)."""
    last, turns = None, 0
    tool_calls: int | None = None  # unknown until an `item.completed` event shows this CLI version reports items
    for obj in json_lines(stdout):
        if obj.get("type") == "turn.completed" and isinstance(obj.get("usage"), dict):
            last = obj["usage"]
            turns += 1
        elif obj.get("type") == "item.completed":
            tool_calls = (tool_calls or 0) + (_item_type(obj) in TOOL_ITEMS)
    if last is None:
        return None
    u = empty_usage()
    cached = num(last, "cached_input_tokens")
    u["input"] = max(num(last, "input_tokens") - cached, 0)
    u["cache_read"] = cached
    u["output"] = num(last, "output_tokens")
    u["turns"] = turns
    u["tool_calls"] = tool_calls
    return u
```

`hbench/runners/null.py`:

```python
"""Parser for the `null` negative-control harness."""

from hbench.runners._usage import Usage, empty_usage


def parse_null(stdout: str) -> Usage:
    """Negative control: no model is called, so usage is zero and the cost is a real 0.0.
    A `null` pair that solves a task means the hidden tests are too lenient."""
    u = empty_usage()
    u["cost_reported"] = 0.0
    u["tool_calls"] = 0
    return u
```

`hbench/graders/__init__.py`:

```python
"""Graders: the protocol, bundled graders, composition and the grading plan of a task (ADR 0007)."""

from hbench.graders.base import Change, GradeContext, Grader, parse_score
from hbench.graders.compose import compose
from hbench.graders.registry import GRADER_GROUP, GRADERS, GradePlan, grade_plan, grader_for

__all__ = [
    "GRADERS",
    "GRADER_GROUP",
    "Change",
    "GradeContext",
    "GradePlan",
    "Grader",
    "compose",
    "grade_plan",
    "grader_for",
    "parse_score",
]
```

`hbench/graders/base.py`:

```python
"""The grader protocol, its inputs and the `SCORE:` convention (ADR 0007).

A grader never starts a process: it asks the engine through `GradeContext.run`, which applies the env
allowlist, the sandbox wrapper and log redaction.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from hbench.core.models import GraderResult

# run(command, timeout, log_name) -> (exit code, timed out, stdout, stderr). A score is only ever read from stdout.
RunStep = Callable[[str, int, str], tuple[int | None, bool, str, str]]

_SCORE_LINE = re.compile(r"SCORE:\s*([0-9.]+)\s*")


@dataclass(frozen=True)
class Change:
    """One file the agent changed: POSIX path relative to the workdir and the changed line counts."""

    path: str
    added: int
    deleted: int


@dataclass(frozen=True)
class GradeContext:
    """Everything a grader may use: the workdir, the task config, the benchmark dir, the agent's changes
    (captured before protected paths are restored) and the engine's `run`."""

    workdir: Path
    task: Mapping[str, Any]
    base: Path
    changes: tuple[Change, ...]
    run: RunStep


@runtime_checkable
class Grader(Protocol):
    name: str
    version: str

    def grade(self, ctx: GradeContext) -> GraderResult:
        """The verdict for one trial."""


def parse_score(output: str, warn: Callable[[str], None] | None = None) -> float | None:
    """Score from the last `SCORE: <number>` line of `output`, or None.

    A value outside [0, 1] or not a number gives None (the caller falls back to pass/fail) and a warning.
    The last line wins, so an earlier line printed by agent code cannot override the real one."""
    value: str | None = None
    for line in output.splitlines():
        match = _SCORE_LINE.fullmatch(line)
        if match:
            value = match.group(1)
    if value is None:
        return None
    try:
        score = float(value)
    except ValueError:
        score = -1.0
    if not 0 <= score <= 1:
        if warn:
            warn(f"warning: ignored SCORE value {value!r}: use a number from 0 to 1")
        return None
    return score
```

`hbench/graders/compose.py`:

```python
"""Combine several grader results into one verdict and score (ADR 0007, decision 6)."""

from collections.abc import Sequence

from hbench.core.models import GraderResult

MODES = ("all", "any", "weighted")


def _score(result: GraderResult) -> float:
    """The grader's score; 1.0 on pass and 0.0 otherwise when it gave none."""
    if result.score is not None:
        return result.score
    return 1.0 if result.passed else 0.0


def check_grade(mode: object, threshold: object, weights: Sequence[object], count: int) -> None:
    """Raises ValueError unless `mode` is known, there is one positive number per grader in `weights`, and a
    `weighted` grade has a `threshold` above 0 and up to 1."""
    if mode not in MODES:
        raise ValueError(f"unknown grade mode {mode!r}: use one of {', '.join(MODES)}")
    if len(weights) != count or not all(_is_positive_number(w) for w in weights):
        raise ValueError("each grader needs a weight above 0 (one per grader)")
    if mode == "weighted":
        _threshold(threshold)


def _threshold(value: object) -> float:
    """The `weighted` pass threshold: a number above 0 and up to 1."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 1:
        raise ValueError("weighted grading needs a threshold above 0 and up to 1")
    return value


def _is_positive_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def compose(
    results: Sequence[GraderResult], mode: str, weights: Sequence[float], threshold: float | None
) -> tuple[bool, float]:
    """`(passed, score)`. `all`: every grader passes, score is the mean. `any`: one passes, score is the max.
    `weighted`: score is the weighted mean, passed when it reaches `threshold` (in (0, 1], required)."""
    if not results:
        raise ValueError("no grader results to compose")
    check_grade(mode, threshold, weights, len(results))
    scores = [_score(r) for r in results]
    if mode == "all":
        return all(r.passed for r in results), sum(scores) / len(scores)
    if mode == "any":
        return any(r.passed for r in results), max(scores)
    score = sum(w * s for w, s in zip(weights, scores, strict=True)) / sum(weights)
    return score >= _threshold(threshold), score
```

`hbench/graders/_command.py`:

```python
"""Shared by the graders that run a command: the verdict from its outcome and the validation of its timeout."""

from collections.abc import Mapping
from typing import Any

from hbench.core.models import GraderResult
from hbench.graders.base import parse_score

DEFAULT_TIMEOUT = 900


def check_timeout(value: Any) -> int:
    """A timeout in seconds: a whole number above 0 (bool is not a number here)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"timeout must be a whole number of seconds above 0, got {value!r}")
    return value


def check_score_flag(cfg: Mapping[str, Any]) -> bool:
    """The grader's `score` option: true to read partial credit from a `SCORE:` line (default false)."""
    value = cfg.get("score", False)
    if not isinstance(value, bool):
        raise ValueError(f"'score' must be true or false, got {value!r}")
    return value


def command_result(
    name: str, version: str, rc: int | None, timed_out: bool, stdout: str, use_score: bool = False
) -> GraderResult:
    """Passed on exit code 0 without a timeout. The score is 1.0 on pass and 0.0 on fail, unless `use_score` is set
    and the last `SCORE:` line of stdout is valid. Stderr is never read: agent code can print to it freely."""
    passed = rc == 0 and not timed_out
    score = parse_score(stdout, print) if use_score else None
    return GraderResult(
        grader=name,
        grader_version=version,
        passed=passed,
        score=score if score is not None else (1.0 if passed else 0.0),
        timed_out=timed_out,
    )
```

`hbench/graders/hidden_tests.py`:

```python
"""The default grader: copy the hidden tests into the workdir, then run the task's test command."""

import shutil
from collections.abc import Mapping
from typing import Any

from hbench.core.models import GraderResult
from hbench.graders._command import DEFAULT_TIMEOUT, check_score_flag, command_result
from hbench.graders.base import GradeContext


class HiddenTestsGrader:
    """Reads the task keys `hidden_tests`, `test` and `test_timeout`. Its one option is `score` (true reads a
    `SCORE:` line from the test output)."""

    name = "hidden_tests"
    version = "1"

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self._use_score = check_score_flag(cfg)

    def grade(self, ctx: GradeContext) -> GraderResult:
        hidden = ctx.task.get("hidden_tests")
        if hidden:
            shutil.copytree((ctx.base / hidden).resolve(), ctx.workdir, dirs_exist_ok=True)
        rc, timed_out, stdout, _ = ctx.run(ctx.task["test"], ctx.task.get("test_timeout", DEFAULT_TIMEOUT), "test")
        return command_result(self.name, self.version, rc, timed_out, stdout, self._use_score)
```

`hbench/graders/command.py`:

```python
"""Grader that runs a command and passes on exit code 0."""

from collections.abc import Mapping
from typing import Any

from hbench.core.models import GraderResult
from hbench.graders._command import DEFAULT_TIMEOUT, check_score_flag, check_timeout, command_result
from hbench.graders.base import GradeContext


class CommandGrader:
    """Options: `command` (required), `timeout` in seconds (default 900) and `score` (true reads a `SCORE:` line)."""

    name = "command"
    version = "1"

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        command = cfg.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command grader needs a non-empty 'command' string")
        self._command = command
        self._timeout = check_timeout(cfg.get("timeout", DEFAULT_TIMEOUT))
        self._use_score = check_score_flag(cfg)

    def grade(self, ctx: GradeContext) -> GraderResult:
        rc, timed_out, stdout, _ = ctx.run(self._command, self._timeout, "command")
        return command_result(self.name, self.version, rc, timed_out, stdout, self._use_score)
```

`hbench/graders/diff_constraints.py`:

```python
"""Grader that checks what the agent changed, without running anything."""

import fnmatch
import posixpath
from collections.abc import Mapping, Sequence
from typing import Any

from hbench.core.models import GraderResult
from hbench.graders.base import Change, GradeContext


def _globs(cfg: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = cfg.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"diff_constraints '{key}' must be a list of glob strings")
    return tuple(value)


def _max_lines(cfg: Mapping[str, Any]) -> int | None:
    value = cfg.get("max_changed_lines")
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"diff_constraints 'max_changed_lines' must be a whole number >= 0, got {value!r}")
    return value


def _normalized(path: str) -> str | None:
    """The path relative to the workdir without `.` or `..` parts, or None when it points outside."""
    clean = posixpath.normpath(path)
    if clean.startswith("/") or clean == ".." or clean.startswith("../"):
        return None
    return clean


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


class DiffConstraintsGrader:
    """Options: `allowed` and `forbidden` (glob lists, `fnmatch` on POSIX paths relative to the workdir; an empty
    `allowed` allows everything), `max_changed_lines` and `ignore_lines` (globs of paths left out of the line count
    only, such as `*/__pycache__/*`; the path checks still apply to them). A path outside the workdir is always
    forbidden."""

    name = "diff_constraints"
    version = "1"

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self._allowed = _globs(cfg, "allowed")
        self._forbidden = _globs(cfg, "forbidden")
        self._max_lines = _max_lines(cfg)
        self._ignore_lines = _globs(cfg, "ignore_lines")

    def violations(self, changes: Sequence[Change]) -> list[str]:
        """One message per broken constraint."""
        found = []
        for change in changes:
            path = _normalized(change.path)
            if path is None or _matches(path, self._forbidden):
                found.append(f"forbidden path {change.path!r}")
            elif self._allowed and not _matches(path, self._allowed):
                found.append(f"path {change.path!r} is not allowed")
        total = sum(
            c.added + c.deleted for c in changes if not _matches(_normalized(c.path) or c.path, self._ignore_lines)
        )
        if self._max_lines is not None and total > self._max_lines:
            found.append(f"{total} changed lines, more than {self._max_lines}")
        return found

    def grade(self, ctx: GradeContext) -> GraderResult:
        passed = not self.violations(ctx.changes)
        return GraderResult(grader=self.name, grader_version=self.version, passed=passed, score=1.0 if passed else 0.0)
```

`hbench/graders/registry.py`:

```python
"""Built-in grader registry and the grading plan of a task (ADR 0007, decisions 4 and 6)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeGuard

from hbench.graders.base import Grader
from hbench.graders.command import CommandGrader
from hbench.graders.compose import check_grade
from hbench.graders.diff_constraints import DiffConstraintsGrader
from hbench.graders.hidden_tests import HiddenTestsGrader
from hbench.plugins import GRADER_GROUP, create_plugin

GRADERS: Mapping[str, Callable[[Mapping[str, Any]], Grader]] = {
    "hidden_tests": HiddenTestsGrader,
    "command": CommandGrader,
    "diff_constraints": DiffConstraintsGrader,
}


@dataclass(frozen=True)
class GradePlan:
    """The graders of a task, in order, and how their results combine."""

    graders: tuple[Grader, ...]
    weights: tuple[float, ...]
    mode: str
    threshold: float | None


def _is_grader(obj: object) -> TypeGuard[Grader]:
    return isinstance(obj, Grader)


def grader_for(entry: Mapping[str, Any]) -> Grader:
    """The grader of one `graders` entry: a built-in type, else the plugin of that name.
    Raises ValueError (a `PluginError` for an unknown type) for a bad entry."""
    if not isinstance(entry, Mapping) or not isinstance(entry.get("type"), str):
        raise ValueError("each grader must be an object with a 'type' string")
    name = entry["type"]
    if name in GRADERS:
        return GRADERS[name](entry)
    return create_plugin(GRADER_GROUP, name, GRADERS, entry, _is_grader, "Grader")


def grade_plan(task: Mapping[str, Any]) -> GradePlan:
    """The plan for `task`. Without a `graders` key it is the single `hidden_tests` grader.
    Raises ValueError for a bad `graders` list, weight or `grade` block."""
    entries = task.get("graders", [{"type": "hidden_tests"}])
    if not isinstance(entries, list) or not entries:
        raise ValueError("graders must be a non-empty list")
    graders = tuple(grader_for(e) for e in entries)
    if any(g.name == "hidden_tests" for g in graders) and "test" not in task:
        raise ValueError("the hidden_tests grader needs the task's 'test' command")
    grade = task.get("grade", {})
    if not isinstance(grade, Mapping):
        raise ValueError("grade must be an object with 'mode' and 'threshold'")
    mode, threshold = grade.get("mode", "all"), grade.get("threshold")
    weights = tuple(e.get("weight", 1) for e in entries)
    check_grade(mode, threshold, weights, len(graders))
    return GradePlan(graders=graders, weights=weights, mode=mode, threshold=threshold)


__all__ = ["GRADERS", "GRADER_GROUP", "GradePlan", "grade_plan", "grader_for"]
```

`hbench/plugins.py`:

```python
"""Plugins through entry points (ADR 0007, decision 7).

Third-party packages register a factory `(config) -> HarnessRunner | Grader` in the groups
`harness_bench.runners` and `harness_bench.graders`. Built-in names always win: callers only come here for a
name that is not built in, so a plain run never imports third-party code. Every failure is a `PluginError`
with a message that names the plugin, never a traceback from the engine.
"""

from collections.abc import Callable, Collection, Mapping
from importlib.metadata import EntryPoint, entry_points
from typing import Any, TypeGuard

RUNNER_GROUP = "harness_bench.runners"
GRADER_GROUP = "harness_bench.graders"


class PluginError(ValueError):
    """A plugin is missing, ambiguous, broken or of the wrong type."""


_announced: set[str] = set()


def reset_announced() -> None:
    """Forget which plugins were already announced (for tests that install several)."""
    _announced.clear()


def _origin(ep: EntryPoint) -> str:
    dist = ep.dist
    return f"{dist.name} {dist.version}" if dist else "an unknown distribution"


def _kind(group: str) -> str:
    return group.rsplit(".", 1)[-1].removesuffix("s")


def _find(group: str, name: str, builtins: Collection[str]) -> EntryPoint:
    points = entry_points(group=group)
    for ep in points:
        if ep.name in builtins:
            raise PluginError(f"plugin {group}:{ep.name} from {_origin(ep)} uses a built-in name; rename it")
    matches = [ep for ep in points if ep.name == name]
    if not matches:
        raise PluginError(
            f"unknown {_kind(group)} type {name!r}: not built in ({', '.join(sorted(builtins))}) "
            f"and no plugin registered in the entry-point group {group!r}"
        )
    if len(matches) > 1:
        origins = ", ".join(_origin(ep) for ep in matches)
        raise PluginError(f"plugin {group}:{name} is registered by more than one package: {origins}")
    return matches[0]


def load_plugin(group: str, name: str, builtins: Collection[str]) -> Callable[[Mapping[str, Any]], object]:
    """The factory registered as `name` in `group`. Prints the plugin's distribution once per process."""
    ep = _find(group, name, builtins)
    try:
        factory = ep.load()
    except Exception as e:
        raise PluginError(f"plugin {group}:{name} failed to load: {type(e).__name__}: {e}") from e
    if not callable(factory):
        raise PluginError(f"plugin {group}:{name} is not callable: expected a factory taking the config")
    label = f"{group}:{name}"
    if label not in _announced:
        _announced.add(label)
        print(f"plugin {label} from {_origin(ep)}")
    plugin_factory: Callable[[Mapping[str, Any]], object] = factory
    return plugin_factory


def create_plugin[T](
    group: str,
    name: str,
    builtins: Collection[str],
    config: Mapping[str, Any],
    accepts: Callable[[object], TypeGuard[T]],
    expected: str,
) -> T:
    """Load the plugin `name`, call its factory with `config` and check the result with `accepts`."""
    factory = load_plugin(group, name, builtins)
    try:
        built = factory(config)
    except Exception as e:
        raise PluginError(f"plugin {group}:{name} failed to build: {type(e).__name__}: {e}") from e
    if not accepts(built):
        raise PluginError(f"plugin {group}:{name} returned a {type(built).__name__}, not a {expected}")
    return built
```

`hbench/results.py`:

```python
"""Writing the contract output of a run: `<output_dir>/<run_id>/run.json` and `trials.jsonl`.

Files hold references only (log path prefixes), never prompts, agent output or diffs.
"""

import dataclasses
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from hbench.core.models import ContractError, Costs, RunInfo, TrialResult


def _atomic_write(path: Path, text: str) -> None:
    """Write via a private temp file in the same directory, then replace, so readers never see a partial file.

    The file keeps the permissions of the one it replaces; a new file is 0644 like other result files."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        if path.exists():
            shutil.copymode(path, tmp)
        else:
            tmp.chmod(0o644)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class RunWriter:
    """Owns one run directory. `run.json` exists from construction; `close` adds the finish time."""

    def __init__(self, out_dir: Path, info: RunInfo) -> None:
        self.info = info
        self.run_dir = out_dir / info.run_id
        self.run_dir.mkdir(parents=True)
        self.trials_path = self.run_dir / "trials.jsonl"
        self.trials_path.touch()
        self._write_info()

    def append(self, trial: TrialResult) -> None:
        with self.trials_path.open("a") as f:
            f.write(json.dumps(trial.to_dict()) + "\n")
            f.flush()

    def close(self, finished_at: datetime) -> None:
        self.info = dataclasses.replace(self.info, finished_at=finished_at)
        self._write_info()

    def _write_info(self) -> None:
        _atomic_write(self.run_dir / "run.json", json.dumps(self.info.to_dict(), indent=2) + "\n")


def _with_gateway_cost(trial: TrialResult, cost: float) -> TrialResult:
    costs = dataclasses.replace(trial.usage.costs, gateway=cost)
    return dataclasses.replace(trial, usage=dataclasses.replace(trial.usage, costs=costs))


def _rewritten(path: Path, costs_by_tag: Mapping[str, float]) -> tuple[list[TrialResult], int]:
    """The trials of `path` with gateway costs applied, and how many were updated."""
    trials = [TrialResult.from_dict(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    rewritten, matched = [], 0
    for trial in trials:
        cost = costs_by_tag.get(trial.trace_id) if trial.trace_id else None
        if cost is None:
            rewritten.append(trial)
        else:
            rewritten.append(_with_gateway_cost(trial, cost))
            matched += 1
    return rewritten, matched


def _valid_costs(costs_by_tag: Mapping[str, float]) -> dict[str, float]:
    """The spends the contract accepts; a bad one (negative, NaN: it comes from the gateway) is skipped."""
    valid = {}
    for tag, cost in costs_by_tag.items():
        try:
            Costs(gateway=cost)
        except ContractError as e:
            print(f"warning: gateway spend for tag {tag!r} ignored: {e}")
        else:
            valid[tag] = cost
    return valid


def apply_gateway_costs(out_dir: Path, costs_by_tag: Mapping[str, float]) -> int:
    """Set `usage.costs.gateway` on every trial whose `trace_id` is in `costs_by_tag`, in all run dirs.

    Returns the number of trials updated. A file is rewritten only when it has a match. A file that is not valid
    contract data (corrupt, or written by another tool) is left untouched with a warning."""
    valid = _valid_costs(costs_by_tag)
    updated = 0
    for path in sorted(out_dir.glob("*/trials.jsonl")):
        try:
            trials, matched = _rewritten(path, valid)
        except ValueError as e:  # ContractError and json.JSONDecodeError are both ValueErrors
            print(f"warning: {path} skipped, not valid trial data: {e}")
            continue
        if matched:
            _atomic_write(path, "".join(json.dumps(t.to_dict()) + "\n" for t in trials))
            updated += matched
    return updated
```

`hbench/stats.py`:

```python
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
```

`hbench/report.py`:

```python
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


def fmt_tool_calls(v: float | None) -> str:
    """`n/a` when unknown, `inf` when nothing was solved (like cost per solved task)."""
    if v is None:
        return "n/a"
    return "inf" if math.isinf(v) else f"{v:.1f}"


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
    calls = [t.usage.tool_calls for t in trials]
    tool_calls = None if any(c is None for c in calls) else sum(c for c in calls if c is not None)
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
        "tool_calls_per_run": tool_calls / n if tool_calls is not None else None,
        "tool_calls_per_solved": (tool_calls / solved if solved else math.inf) if tool_calls is not None else None,
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
    "| Cost source | Pass 95% CI | pass@k | pass^k | Cost/solved 95% CI | Tie | Pareto "
    "| Tool calls/run | Tool calls/solved |"
)
_COLUMNS = 22


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
        f"| {'' if t['tie_with'] is None else f'={t["tie_with"]}'} | {'yes' if t['pareto'] else ''} "
        f"| {fmt_tool_calls(t['tool_calls_per_run'])} | {fmt_tool_calls(t['tool_calls_per_solved'])} |"
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
```

`hbench/profiles.py`:

```python
"""Config profiles. The LiteLLM profile keeps only harnesses that bill through LiteLLM and forces its cost."""

import sys
from typing import Any

# Parsers of harnesses that cannot send the LiteLLM tag header (no per-run spend to match).
EXCLUDED_PARSERS = {"cursor"}


def apply_litellm_profile(cfg: dict[str, Any]) -> dict[str, Any]:
    """A new config for the LiteLLM-only benchmark: no excluded harnesses, cost from LiteLLM, own output dir."""
    if not cfg.get("litellm"):
        sys.exit("config needs a 'litellm' section (base_url, api_key)")
    excluded = {name for name, h in cfg["harnesses"].items() if h.get("parser") in EXCLUDED_PARSERS}
    harnesses = {k: v for k, v in cfg["harnesses"].items() if k not in excluded}
    dropped = [m for m in cfg["matrix"] if m["harness"] in excluded]
    matrix = [m for m in cfg["matrix"] if m["harness"] not in excluded]
    if dropped:
        print(f"ignored {len(dropped)} matrix entries for non-LiteLLM harnesses: {sorted(excluded)}")
    unknown = {m["harness"] for m in matrix} - set(harnesses)
    if unknown:
        sys.exit(f"matrix uses undefined harnesses: {sorted(unknown)}")
    return {
        **cfg,
        "harnesses": harnesses,
        "matrix": matrix,
        "output_dir": cfg.get("output_dir", "results-litellm"),
        "cost_source": "litellm",
    }
```

`hbench/cli.py`:

```python
"""The single command line: `python3 -m hbench {run,report,reconcile,schema export}`.

`harness_bench.py` and `harness_bench_litellm.py` are shims over `main`. `--litellm` applies the LiteLLM
profile (see `hbench.profiles`) and, for `run`, waits for the spend logs and reconciles afterwards.
"""

import argparse
import importlib
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from hbench.core.schema import schema_document
from hbench.profiles import apply_litellm_profile

_DESCRIPTION = (
    "harness-bench: compare coding-agent harness x model pairs. Main metric: cost per solved task.\n\n"
    "  python3 -m hbench run       --config config.json [--reps 3] [--only HARNESS] [--keep] [--dry-run]\n"
    "  python3 -m hbench reconcile --config config.json   # pull real spend from LiteLLM\n"
    "  python3 -m hbench report    --config config.json [--drop-saturated]\n"
    "  python3 -m hbench schema export [--out PATH]       # JSON Schema of trials.jsonl and run.json\n\n"
    "Add --litellm to run, report or reconcile for the LiteLLM-only benchmark."
)


def _add_config(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", required=True)
    p.add_argument(
        "--litellm",
        action="store_true",
        help="LiteLLM-only profile: drop harnesses without LiteLLM spend, cost always from LiteLLM",
    )


def _add_drop_saturated(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--drop-saturated",
        action="store_true",
        help="leave out tasks every non-null run solved before building the report",
    )


def _prog() -> str:
    """The name the user typed: the script for the shims, `python3 -m hbench` for the module."""
    name = Path(sys.argv[0]).name
    return "python3 -m hbench" if name == "__main__.py" else name


def build_parser(epilog: str | None = None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=_prog(),
        description=_DESCRIPTION,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the matrix and write results")
    _add_config(run)
    _add_drop_saturated(run)
    run.add_argument("--reps", type=int)
    run.add_argument("--only", nargs="*", help="only these harness names")
    run.add_argument("--keep", action="store_true", help="keep work directories")
    run.add_argument("--dry-run", action="store_true", help="print commands only")
    run.add_argument(
        "--allow-unsandboxed", action="store_true", help="run without sandbox.prefix (only inside a disposable VM)"
    )
    run.add_argument(
        "--settle",
        type=int,
        default=60,
        help="with --litellm: seconds to wait for LiteLLM to write spend logs before reconcile",
    )
    report = sub.add_parser("report", help="write summary.md and summary.csv from the results")
    _add_config(report)
    _add_drop_saturated(report)
    report.add_argument("--cost-source", choices=["auto", "litellm", "computed", "reported"])
    reconcile = sub.add_parser("reconcile", help="fetch real spend per run from LiteLLM")
    _add_config(reconcile)
    _add_drop_saturated(reconcile)
    schema = sub.add_parser("schema", help="JSON Schema of the data contract")
    schema_sub = schema.add_subparsers(dest="schema_cmd", required=True)
    export = schema_sub.add_parser("export", help="print the schema document, or write it with --out")
    export.add_argument("--out", help="write to this file instead of stdout")
    return ap


def _engine() -> ModuleType:
    """The engine module. Imported here, not at module level, because the engine imports this package."""
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("harness_bench")


def _schema_export(out: str | None) -> None:
    text = json.dumps(schema_document(), indent=2, sort_keys=True)
    if out:
        Path(out).write_text(text + "\n")
    else:
        print(text)


def _run_litellm(engine: ModuleType, args: argparse.Namespace, cfg: dict[str, Any], base: Path) -> None:
    """`run` under the LiteLLM profile: report only after reconcile, when the gateway cost exists."""
    engine.cmd_run(args, cfg, base)
    if args.dry_run:
        return
    print(f"\nwaiting {args.settle}s for LiteLLM spend logs...", flush=True)
    time.sleep(args.settle)
    try:
        engine.cmd_reconcile(args, cfg, base)
    except Exception as e:  # noqa: BLE001
        print(f"reconcile failed: {e}\nRun later: python3 -m hbench reconcile --litellm --config {args.config}")


def main(argv: Sequence[str] | None = None, *, epilog: str | None = None) -> None:
    parser = build_parser(epilog)
    args = parser.parse_args(argv)
    if args.cmd == "schema":
        _schema_export(args.out)
        return
    engine = _engine()
    cfg_path = Path(args.config).resolve()
    cfg: dict[str, Any] = json.loads(cfg_path.read_text())
    base = cfg_path.parent
    if args.litellm:
        if getattr(args, "cost_source", None):
            parser.error("--cost-source cannot be combined with --litellm (the cost always comes from LiteLLM)")
        cfg = apply_litellm_profile(cfg)  # sets cfg["cost_source"] = "litellm"
        args.no_report = True
        if args.cmd == "run":
            _run_litellm(engine, args, cfg, base)
            return
    commands = {"run": engine.cmd_run, "report": engine.cmd_report, "reconcile": engine.cmd_reconcile}
    commands[args.cmd](args, cfg, base)
```

`hbench/__main__.py`:

```python
"""`python3 -m hbench`."""

from hbench.cli import main

if __name__ == "__main__":
    main()
```

## `config.litellm.example.json`

```json
{
  "reps": 3,
  "timeout": 1800,
  "cost_source": "litellm",
  "output_dir": "results-litellm",
  "sandbox": {
    "_comment": "Image from sandbox/Dockerfile. Without Docker, leave prefix empty and use --allow-unsandboxed only inside a disposable VM.",
    "prefix": [
      "docker",
      "run",
      "--rm",
      "--env-file",
      "{env_file}",
      "-v",
      "{workdir}:{workdir}",
      "-w",
      "{workdir}",
      "--cap-drop",
      "ALL",
      "--security-opt",
      "no-new-privileges",
      "--pids-limit",
      "512",
      "--memory",
      "4g",
      "--cpus",
      "2",
      "harness-bench-agents"
    ]
  },
  "env_passthrough": [
    "LITELLM_BASE_URL"
  ],
  "litellm": {
    "base_url": "${LITELLM_BASE_URL}",
    "api_key": "${LITELLM_SPEND_KEY}"
  },
  "harnesses": {
    "claude-code": {
      "parser": "claude",
      "command": [
        "claude",
        "-p",
        "{prompt}",
        "--model",
        "{model}",
        "--output-format",
        "json",
        "--dangerously-skip-permissions"
      ],
      "env": {
        "ANTHROPIC_BASE_URL": "${LITELLM_BASE_URL}",
        "ANTHROPIC_AUTH_TOKEN": "${LITELLM_API_KEY}",
        "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-tags: {run_tag}"
      }
    },
    "pi": {
      "parser": "pi",
      "command": [
        "pi",
        "--mode",
        "json",
        "--provider",
        "{provider}",
        "--model",
        "{model_name}",
        "{prompt}"
      ],
      "env": {
        "LITELLM_API_KEY": "${LITELLM_API_KEY}"
      }
    },
    "opencode": {
      "parser": "opencode",
      "command": [
        "opencode",
        "run",
        "--format",
        "json",
        "--model",
        "{model}",
        "{prompt}"
      ],
      "env": {
        "LITELLM_API_KEY": "${LITELLM_API_KEY}"
      }
    },
    "codex": {
      "parser": "codex",
      "command": [
        "codex",
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-c",
        "model_provider=litellm",
        "-m",
        "{model}",
        "{prompt}"
      ],
      "env": {
        "LITELLM_API_KEY": "${LITELLM_API_KEY}"
      }
    },
    "null": {
      "_comment": "Negative control: runs nothing. If it solves a task, the hidden tests are too lenient. It has no LiteLLM spend, so its cost is unknown in this profile.",
      "parser": "null",
      "command": [
        "true"
      ]
    }
  },
  "matrix": [
    {
      "harness": "claude-code",
      "model": "CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "pi",
      "model": "litellm-anthropic/CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "opencode",
      "model": "litellm/CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "codex",
      "model": "GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "pi",
      "model": "litellm/GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "opencode",
      "model": "litellm/GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "pi",
      "model": "litellm/GEMINI_MODEL",
      "price": "gemini"
    },
    {
      "harness": "opencode",
      "model": "litellm/GEMINI_MODEL",
      "price": "gemini"
    },
    {
      "harness": "null",
      "model": "none"
    }
  ],
  "tasks": [
    {
      "id": "fizzbuzz",
      "repo": "tasks/fizzbuzz/repo",
      "prompt": "The function fizzbuzz in fizzbuzz.py has bugs. Fix it so fizzbuzz(n) returns 'Fizz' for multiples of 3, 'Buzz' for multiples of 5, 'FizzBuzz' for multiples of both, and str(n) otherwise. Do not ask questions.",
      "hidden_tests": "tasks/fizzbuzz/hidden_tests",
      "test": "python3 -m unittest -q test_hidden.py",
      "protected_paths": []
    },
    {
      "id": "example-real-repo",
      "repo": "git@github.com:YOUR-ORG/YOUR-REPO.git",
      "ref": "COMMIT_BEFORE_THE_FIX",
      "setup": "npm ci",
      "prompt": "Describe the bug or feature here, as in a real issue.",
      "hidden_tests": "tasks/example-real-repo/hidden_tests",
      "test": "npx vitest run tests/hidden",
      "timeout": 2400,
      "protected_paths": [
        "tests",
        "package.json",
        "vitest.config.ts"
      ]
    }
  ]
}
```

## `config.example.json`

```json
{
  "reps": 3,
  "timeout": 1800,
  "cost_source": "auto",
  "output_dir": "results",
  "sandbox": {
    "_comment": "Image from sandbox/Dockerfile. Without Docker, leave prefix empty and use --allow-unsandboxed only inside a disposable VM.",
    "prefix": [
      "docker",
      "run",
      "--rm",
      "--env-file",
      "{env_file}",
      "-v",
      "{workdir}:{workdir}",
      "-w",
      "{workdir}",
      "--cap-drop",
      "ALL",
      "--security-opt",
      "no-new-privileges",
      "--pids-limit",
      "512",
      "--memory",
      "4g",
      "--cpus",
      "2",
      "harness-bench-agents"
    ]
  },
  "env_passthrough": [
    "LITELLM_BASE_URL"
  ],
  "litellm": {
    "base_url": "${LITELLM_BASE_URL}",
    "api_key": "${LITELLM_SPEND_KEY}"
  },
  "harnesses": {
    "claude-code": {
      "parser": "claude",
      "command": [
        "claude",
        "-p",
        "{prompt}",
        "--model",
        "{model}",
        "--output-format",
        "json",
        "--dangerously-skip-permissions"
      ],
      "env": {
        "ANTHROPIC_BASE_URL": "${LITELLM_BASE_URL}",
        "ANTHROPIC_AUTH_TOKEN": "${LITELLM_API_KEY}",
        "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-tags: {run_tag}"
      }
    },
    "pi": {
      "parser": "pi",
      "command": [
        "pi",
        "--mode",
        "json",
        "--provider",
        "{provider}",
        "--model",
        "{model_name}",
        "{prompt}"
      ],
      "env": {
        "LITELLM_API_KEY": "${LITELLM_API_KEY}"
      }
    },
    "cursor": {
      "parser": "cursor",
      "command": [
        "agent",
        "-p",
        "--output-format",
        "json",
        "--force",
        "--trust",
        "--model",
        "{model}",
        "{prompt}"
      ],
      "env": {
        "CURSOR_API_KEY": "${CURSOR_API_KEY}"
      }
    },
    "opencode": {
      "parser": "opencode",
      "command": [
        "opencode",
        "run",
        "--format",
        "json",
        "--model",
        "{model}",
        "{prompt}"
      ],
      "env": {
        "LITELLM_API_KEY": "${LITELLM_API_KEY}"
      }
    },
    "codex": {
      "parser": "codex",
      "command": [
        "codex",
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-c",
        "model_provider=litellm",
        "-m",
        "{model}",
        "{prompt}"
      ],
      "env": {
        "LITELLM_API_KEY": "${LITELLM_API_KEY}"
      }
    },
    "null": {
      "_comment": "Negative control: runs nothing. If it solves a task, the hidden tests are too lenient.",
      "parser": "null",
      "command": [
        "true"
      ]
    }
  },
  "matrix": [
    {
      "harness": "claude-code",
      "model": "CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "pi",
      "model": "litellm-anthropic/CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "opencode",
      "model": "litellm/CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "cursor",
      "model": "CURSOR_CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "codex",
      "model": "GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "pi",
      "model": "litellm/GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "opencode",
      "model": "litellm/GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "cursor",
      "model": "CURSOR_GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "pi",
      "model": "litellm/GEMINI_MODEL",
      "price": "gemini"
    },
    {
      "harness": "opencode",
      "model": "litellm/GEMINI_MODEL",
      "price": "gemini"
    },
    {
      "harness": "cursor",
      "model": "CURSOR_GEMINI_MODEL",
      "price": "gemini"
    },
    {
      "harness": "cursor",
      "model": "CURSOR_GROK_MODEL",
      "price": "grok"
    },
    {
      "harness": "null",
      "model": "none"
    }
  ],
  "prices": {
    "_comment": "USD per 1M tokens. Used only when there is no LiteLLM cost (e.g. Cursor). Fill in from the official price list.",
    "claude": {
      "input": 0,
      "output": 0,
      "cache_read": 0,
      "cache_write": 0
    },
    "gpt": {
      "input": 0,
      "output": 0,
      "cache_read": 0,
      "cache_write": 0
    },
    "gemini": {
      "input": 0,
      "output": 0,
      "cache_read": 0,
      "cache_write": 0
    },
    "grok": {
      "input": 0,
      "output": 0,
      "cache_read": 0,
      "cache_write": 0
    }
  },
  "tasks": [
    {
      "id": "fizzbuzz",
      "repo": "tasks/fizzbuzz/repo",
      "prompt": "The function fizzbuzz in fizzbuzz.py has bugs. Fix it so fizzbuzz(n) returns 'Fizz' for multiples of 3, 'Buzz' for multiples of 5, 'FizzBuzz' for multiples of both, and str(n) otherwise. Do not ask questions.",
      "hidden_tests": "tasks/fizzbuzz/hidden_tests",
      "test": "python3 -m unittest -q test_hidden.py",
      "protected_paths": []
    },
    {
      "id": "example-real-repo",
      "repo": "git@github.com:YOUR-ORG/YOUR-REPO.git",
      "ref": "COMMIT_BEFORE_THE_FIX",
      "setup": "npm ci",
      "prompt": "Describe the bug or feature here, as in a real issue.",
      "hidden_tests": "tasks/example-real-repo/hidden_tests",
      "test": "npx vitest run tests/hidden",
      "timeout": 2400,
      "protected_paths": [
        "tests",
        "package.json",
        "vitest.config.ts"
      ]
    }
  ]
}
```

## Example task

`tasks/fizzbuzz/repo/fizzbuzz.py` (deliberate bugs):

```python
def fizzbuzz(n):
    if n % 3 == 0:
        return "Fizz"
    if n % 5 == 0:
        return "Buzz"
    if n % 15 == 0:
        return "FizzBuzz"
    return n
```

`tasks/fizzbuzz/hidden_tests/test_hidden.py`:

```python
import unittest
from fizzbuzz import fizzbuzz


class TestFizzBuzz(unittest.TestCase):
    def test_values(self):
        self.assertEqual(fizzbuzz(1), "1")
        self.assertEqual(fizzbuzz(3), "Fizz")
        self.assertEqual(fizzbuzz(5), "Buzz")
        self.assertEqual(fizzbuzz(15), "FizzBuzz")
        self.assertEqual(fizzbuzz(30), "FizzBuzz")


if __name__ == "__main__":
    unittest.main()
```

## Offline tests

Run `python3 tests/smoke.py`. It uses fake agents and a fake LiteLLM to check the parsers, `reconcile` and both scripts, with no network and no real CLIs.

`tests/smoke.py`:

```python
#!/usr/bin/env python3
"""End-to-end smoke test with fake agents and a fake LiteLLM. No network, no real CLIs.

Run: python3 tests/smoke.py   (exit 0 = pass)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
import fake_litellm  # noqa: E402
import fixtures  # noqa: E402
import harness_bench as hb  # noqa: E402

PY = fixtures.PY
API_KEY = "sk-test-agent-key-0123456789"


def check(cond, msg):
    if not cond:
        sys.exit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def results(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def run(tmp, *args, config="config.json", expect_ok=True):
    p = subprocess.run([PY, *args, "--config", config], cwd=tmp, capture_output=True, text=True)
    if expect_ok and p.returncode != 0:
        sys.exit(f"FAIL: {' '.join(args)}\n{p.stdout}\n{p.stderr}")
    return p


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True
    )


def make_git_task(tmp):
    """Repo with a buggy commit followed by the fix: the agent must only see the first."""
    repo = tmp / "gitrepo"
    shutil.copytree(ROOT / "tasks" / "fizzbuzz" / "repo", repo)
    (repo / "run_tests.py").write_text(
        "import sys, unittest\n"
        "r = unittest.main(module='test_hidden', exit=False).result\n"
        "sys.exit(0 if r.wasSuccessful() else 1)\n"
    )
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "buggy")
    buggy = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "fizzbuzz.py").write_text("def fizzbuzz(n):\n    return 'SOLUTION'\n")
    git(repo, "commit", "-qam", "the fix")
    return str(repo), buggy


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-smoke-"))
    hide_work = Path(tempfile.mkdtemp(prefix="hb-smoke-work-"))
    try:
        fixtures.copy_engine(tmp)
        shutil.copytree(ROOT / "tasks" / "fizzbuzz", tmp / "tasks" / "fizzbuzz")
        tags_file, probe_file = tmp / "tags.txt", tmp / "probe.jsonl"
        marker = tmp / "host-exec-marker"
        os.environ.update(
            FAKE_TAGS_FILE=str(tags_file),
            FAKE_PROBE_FILE=str(probe_file),
            FAKE_KEY=fake_litellm.KEY,
            SECRET_CANARY="must-not-leak",
            FAKE_API_KEY=API_KEY,
            FAKE_MARKER=str(marker),
        )

        srv, port = fake_litellm.serve()
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        harness_env = {
            "FAKE_TAGS_FILE": "${FAKE_TAGS_FILE}",
            "FAKE_PROBE_FILE": "${FAKE_PROBE_FILE}",
            "FAKE_API_KEY": "${FAKE_API_KEY}",
            "FAKE_MARKER": "${FAKE_MARKER}",
        }
        fizz = fixtures.fizzbuzz_task()
        cfg = dict(
            fixtures.fake_config(harness_env),
            litellm={"base_url": f"http://127.0.0.1:{port}", "api_key": "${FAKE_KEY}"},
        )
        (tmp / "config.json").write_text(json.dumps(cfg))

        # --- parsers, cost, reconcile (full script)
        p = run(tmp, "harness_bench.py", "run", expect_ok=False)
        check(p.returncode != 0 and "No sandbox configured" in p.stderr, "refuses to run without sandbox")
        run(tmp, "harness_bench.py", "run", "--allow-unsandboxed")
        rows = {r["harness"]: r for r in results(tmp / "results/results.jsonl")}
        check(len(rows) == 5, "full script: 5 runs recorded")
        check(all(r["agent_rc"] == 0 for r in rows.values()), "agent env: spend key and other secrets absent")
        check(rows["claude"]["passed"] and not rows["opencode"]["passed"], "hidden tests decide pass/fail")
        check(rows["claude"]["input"] == 310 and rows["claude"]["cache_read"] == 40000, "claude: modelUsage summed")
        check(rows["opencode"]["turns"] == 3 and rows["opencode"]["reasoning"] == 150, "opencode: step_finish summed")
        check(rows["pi"]["turns"] == 3 and rows["pi"]["input"] == 2400, "pi: only message_end counted")
        check(
            rows["codex"]["input"] == 5000 and rows["codex"]["cache_read"] == 15000, "codex: cached is subset of input"
        )
        check(abs(rows["codex"]["cost_computed"] - 0.0735) < 1e-9, "codex: computed cost from prices")
        counts = {name: r["tool_calls"] for name, r in rows.items()}
        check(
            counts == {"claude": 2, "opencode": 2, "pi": 3, "codex": 2, "cursor": None},
            "tool calls counted per format, unknown (never 0) for cursor",
        )
        check(
            not rows["cursor"]["parse_ok"] and rows["cursor"]["cost_computed"] is None, "cursor: no usage, no fake $0"
        )
        logs = tmp / "results" / "logs"
        stderr_logs = "".join(f.read_text() for f in logs.glob("*.agent.stderr"))
        check(API_KEY not in stderr_logs and "***REDACTED***" in stderr_logs, "logs: harness secrets redacted")
        check(any(f.read_text() for f in logs.glob("fizzbuzz__claude__*.diff")), "audit diff saved")
        (run_dir,) = [p.parent for p in (tmp / "results").glob("*/trials.jsonl")]
        trials_text = (run_dir / "trials.jsonl").read_text()
        check(len(trials_text.splitlines()) == 5 and (run_dir / "run.json").exists(), "contract: run.json + 5 trials")
        check(API_KEY not in trials_text and "fix it" not in trials_text, "contract: no secret or prompt in trials")
        probes = [json.loads(x) for x in probe_file.read_text().splitlines()]
        check(all(not Path(pr["cwd"]).is_relative_to(tmp) for pr in probes), "workdir outside benchmark repo")
        check(not (tmp / "work").exists(), "no work dir created inside repo")

        out = run(tmp, "harness_bench.py", "reconcile").stdout
        check("4/5 runs matched" in out, "reconcile: tagged runs matched, cursor unmatched")
        rows = {r["harness"]: r for r in results(tmp / "results/results.jsonl")}
        check(abs(rows["pi"]["cost_litellm"] - 0.02) < 1e-9, "reconcile: unrelated rows ignored")
        check((tmp / "results/summary.md").exists() and (tmp / "results/summary.csv").exists(), "report files written")

        # --- LiteLLM-only script
        tags_file.unlink()
        out = run(tmp, "harness_bench_litellm.py", "run", "--settle", "0", "--allow-unsandboxed").stdout
        check("ignored 1 matrix entries" in out, "litellm script: cursor dropped")
        check("4/4 runs matched" in out, "litellm script: all runs matched")
        summary = (tmp / "results-litellm/summary.md").read_text()
        check("cursor" not in summary and summary.count("| litellm |") == 4, "litellm script: cost source litellm only")

        # --- git history, protected paths, sandbox prefix
        repo, buggy = make_git_task(tmp)
        probe_file.unlink()
        hist = {
            "id": "hist",
            "repo": repo,
            "ref": buggy,
            "prompt": "fix it",
            "hidden_tests": "tasks/fizzbuzz/hidden_tests",
            "test": f"{PY} run_tests.py",
            "protected_paths": ["run_tests.py", "tests_infra"],
        }
        cfg2 = dict(
            cfg,
            output_dir="results-sec",
            tasks=[hist],
            harnesses={"claude": fixtures.harness("claude", harness_env, "hack")},
            matrix=[{"harness": "claude", "model": "m"}],
            sandbox={"prefix": ["env", "FAKE_SANDBOX={workdir}"]},
        )
        (tmp / "config2.json").write_text(json.dumps(cfg2))
        run(tmp, "harness_bench.py", "run", config="config2.json")
        pr = json.loads(probe_file.read_text().splitlines()[0])
        check(pr["depth"] == "" and pr["remotes"] == "", "workdir: no git repo, history or remote (fix commit hidden)")
        check(pr["sandbox"] == pr["cwd"], "sandbox prefix wraps the agent command")
        row = results(tmp / "results-sec/results.jsonl")[0]
        check(not row["passed"] and row["sandboxed"], "protected paths restored: test-runner hack does not pass")
        diff = next((tmp / "results-sec/logs").glob("*.diff")).read_text()
        check("run_tests.py" in diff and "conftest.py" in diff, "audit diff shows tampering")

        # --- graders: a failing `command` grader and `diff_constraints` decide the verdict
        open_hist = {key: val for key, val in hist.items() if key != "protected_paths"}
        no_graders = dict(open_hist, id="tampered")  # the hack agent makes run_tests.py exit 0
        forbid = [{"type": "hidden_tests"}, {"type": "diff_constraints", "forbidden": ["run_tests.py"]}]
        guarded = dict(open_hist, id="guarded", graders=forbid)
        failing = dict(fizz, id="failing", graders=[{"type": "hidden_tests"}, {"type": "command", "command": "false"}])
        cfg3 = dict(
            cfg2,
            output_dir="results-grade",
            tasks=[no_graders, guarded],
            matrix=[{"harness": "claude", "model": "m"}],
        )
        (tmp / "config3.json").write_text(json.dumps(cfg3))
        run(tmp, "harness_bench.py", "run", config="config3.json")
        by_task = {r["task"]: r for r in results(tmp / "results-grade/results.jsonl")}
        check(by_task["tampered"]["passed"], "graders: without constraints the test-runner hack passes the tests")
        check(not by_task["guarded"]["passed"], "graders: diff_constraints fails the hack even though the tests pass")
        cfg4 = dict(cfg, output_dir="results-cmd", tasks=[failing], matrix=[{"harness": "claude", "model": "fix"}])
        (tmp / "config4.json").write_text(json.dumps(cfg4))
        run(tmp, "harness_bench.py", "run", "--allow-unsandboxed", config="config4.json")
        row = results(tmp / "results-cmd/results.jsonl")[0]
        check(not row["passed"], "graders: a failing command grader fails the run although the hidden tests pass")
        check(any((tmp / "results-cmd/logs").glob("*grader-1-command.log")), "graders: command log written")

        # --- the agent cannot hide a forbidden change from `diff_constraints` through git, or run host commands
        modes = {"hg": "hide-git", "hgc": "hide-git-clean", "hi": "hide-ignore", "ha": "hide-attr", "hc": "hide-config"}
        guard = [{"type": "hidden_tests"}, {"type": "diff_constraints", "forbidden": ["run_tests.py", "conftest.py"]}]
        cfg5 = dict(
            cfg2,
            output_dir="results-hide",
            work_dir=str(hide_work),
            tasks=[dict(open_hist, id="hide", graders=guard)],
            harnesses={name: fixtures.harness("claude", harness_env, mode) for name, mode in modes.items()},
            matrix=[{"harness": name, "model": "m"} for name in modes],
        )
        (tmp / "config5.json").write_text(json.dumps(cfg5))
        run(tmp, "harness_bench.py", "run", config="config5.json")
        (hide_run,) = [p.parent for p in (tmp / "results-hide").glob("*/trials.jsonl")]
        trials = results(hide_run / "trials.jsonl")  # in matrix order
        check(len(trials) == len(modes), "git hiding: one trial per mode")
        for trial, mode in zip(trials, modes.values(), strict=True):
            by_grader = {g["grader"]: g["passed"] for g in trial["graders"]}
            check(
                by_grader == {"hidden_tests": True, "diff_constraints": False},
                f"git hiding ({mode}): the tests pass but diff_constraints fails the run",
            )
        check(not marker.exists(), "workdir .git/config: fsmonitor, filter and textconv commands never run on the host")
        check(not list(hide_work.glob(".*")), "baseline git dir and snapshot removed after each trial")

        # --- grader logs are redacted like the agent logs
        leak_cmd = f"echo spend={fake_litellm.KEY} agent={API_KEY}"
        leaky = dict(fizz, id="leaky", graders=[{"type": "hidden_tests"}, {"type": "command", "command": leak_cmd}])
        cfg6 = dict(cfg, output_dir="results-leak", tasks=[leaky], matrix=[{"harness": "claude", "model": "fix"}])
        (tmp / "config6.json").write_text(json.dumps(cfg6))
        run(tmp, "harness_bench.py", "run", "--allow-unsandboxed", config="config6.json")
        (grader_log,) = (tmp / "results-leak/logs").glob("*grader-1-command.log")
        text = grader_log.read_text()
        check(
            "***REDACTED***" in text and fake_litellm.KEY not in text and API_KEY not in text,
            "grader logs: spend key and harness secrets redacted",
        )

        # --- config validation and URL policy
        for bad, why in (
            (dict(fizz, id="/etc"), "absolute task id"),
            (dict(fizz, id="../x"), "traversal task id"),
            (dict(fizz, ref="--upload-pack=x"), "option-like git ref"),
        ):
            (tmp / "bad.json").write_text(json.dumps(dict(cfg, tasks=[bad])))
            p = run(tmp, "harness_bench.py", "run", "--allow-unsandboxed", config="bad.json", expect_ok=False)
            check(p.returncode != 0, f"rejects {why}")
        leak = dict(cfg, env_passthrough=["FAKE_KEY"])
        (tmp / "bad.json").write_text(json.dumps(leak))
        p = run(tmp, "harness_bench.py", "run", "--allow-unsandboxed", config="bad.json", expect_ok=False)
        check(p.returncode != 0 and "spend key" in p.stderr, "rejects passing spend key to agents")
        for url, ok in (
            ("https://litellm.example.com", True),
            ("http://localhost:4000", True),
            ("http://litellm.example.com", False),
        ):
            try:
                hb.check_litellm_url(url)
                passed = True
            except SystemExit:
                passed = False
            check(passed == ok, f"url policy: {url} {'allowed' if ok else 'blocked'}")
        print("\nPASS")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(hide_work, ignore_errors=True)


if __name__ == "__main__":
    main()
```

`tests/fake_agent.py`:

```python
"""Fake coding-agent CLI for tests. Prints the JSON shape of a real harness.

Usage: fake_agent.py <claude|opencode|pi|codex|cursor> <fix|nofix|hack|hide-git|hide-git-clean|hide-ignore|hide-attr|hide-config>
"fix" repairs fizzbuzz.py in the cwd. "hack" leaves the bug and tampers with the test
infrastructure instead (rewrites run_tests.py, adds conftest.py, moves git refs).
The "hide-*" modes fix the bug and then try to hide a forbidden change from the diff:
"hide-git" edits run_tests.py behind skip-worktree, info/exclude and a replace ref in its own repo
("hide-git-clean" then deletes that repo),
"hide-ignore" plants conftest.py behind a .gitignore, "hide-attr" edits run_tests.py next to a
.gitattributes with `working-tree-encoding=UTF-16` (makes a naive `git add` fail), and "hide-config" arms .git/config
(fsmonitor, filter, textconv) with a command that touches $FAKE_MARKER.
Security probes: exits 9 if the LiteLLM spend key or SECRET_CANARY reached the agent,
prints FAKE_API_KEY to stderr (must be redacted in logs), and records git history depth. When BENCH_TAG is set (and kind is not cursor),
the tag is appended to $FAKE_TAGS_FILE, which fake_litellm.py serves as spend logs.
"""

import json
import os
import sys

kind, mode = sys.argv[1], sys.argv[2]
fix = mode == "fix" or mode.startswith("hide-")
for leaked in ("FAKE_KEY", "SECRET_CANARY"):
    if leaked in os.environ:
        sys.exit(9)
print(f"debug key={os.environ.get('FAKE_API_KEY', '')}", file=sys.stderr)
if os.environ.get("FAKE_PROBE_FILE"):
    import subprocess

    depth = subprocess.run(["git", "rev-list", "--all", "--count"], capture_output=True, text=True).stdout.strip()
    remotes = subprocess.run(["git", "remote"], capture_output=True, text=True).stdout.strip()
    with open(os.environ["FAKE_PROBE_FILE"], "a") as f:
        f.write(
            json.dumps(
                {
                    "cwd": os.getcwd(),
                    "depth": depth,
                    "remotes": remotes,
                    "sandbox": os.environ.get("FAKE_SANDBOX", ""),
                    "home": os.environ.get("HOME", ""),
                    "uid": os.getuid(),
                }
            )
            + "\n"
        )
if mode == "hack":
    with open("run_tests.py", "w") as f:
        f.write("import sys\nsys.exit(0)\n")
    os.makedirs("tests_infra", exist_ok=True)
    with open("tests_infra/conftest.py", "w") as f:
        f.write("# planted\n")
    import subprocess

    subprocess.run(["git", "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qam", "hack"], capture_output=True)


def hide_forbidden_change():
    """Tamper with the test infrastructure while trying to keep the change out of the diff."""
    import subprocess

    def sh(script):
        subprocess.run(["sh", "-c", script], capture_output=True)

    if mode.startswith("hide-git"):
        sh("git init -q . && git add -A && git update-index --skip-worktree run_tests.py")
        sh("echo '*' >> .git/info/exclude")
        sh("git -c user.name=x -c user.email=x@x commit -qm x && git replace $(git rev-list --max-parents=0 HEAD) HEAD")
        with open("run_tests.py", "w") as f:
            f.write("import sys\nsys.exit(0)\n")
        if mode == "hide-git-clean":  # leave no repo behind: the edit must still show by path
            sh("rm -rf .git")
    elif mode == "hide-ignore":
        with open(".gitignore", "w") as f:
            f.write("conftest.py\n")
        with open("conftest.py", "w") as f:
            f.write("# planted\n")
    elif mode == "hide-attr":
        with open(".gitattributes", "w") as f:
            f.write("* working-tree-encoding=UTF-16\n")
        with open("run_tests.py", "w") as f:
            f.write("import sys\nsys.exit(0)\n")
    else:  # hide-config
        marker = os.environ["FAKE_MARKER"]
        sh(
            f"git init -q . && git config core.fsmonitor 'touch {marker}; false'"
            f" && git config filter.x.clean 'touch {marker}; cat' && git config diff.x.textconv 'touch {marker}; cat'"
        )
        with open(".gitattributes", "w") as f:
            f.write("* filter=x diff=x\n")


if mode.startswith("hide-"):
    hide_forbidden_change()
if fix:
    with open("fizzbuzz.py", "w") as f:
        f.write(
            "def fizzbuzz(n):\n    if n % 15 == 0:\n        return 'FizzBuzz'\n"
            "    if n % 3 == 0:\n        return 'Fizz'\n    if n % 5 == 0:\n"
            "        return 'Buzz'\n    return str(n)\n"
        )

if kind == "claude":
    # stream-json shape: assistant events (one repeated tool_use block) before the result
    for index, name in enumerate(("Read", "Edit", "Edit")):
        block = {"type": "tool_use", "id": f"toolu_{min(index, 1)}", "name": name}
        print(json.dumps({"type": "assistant", "message": {"id": f"msg_{index}", "content": [block]}}))
    print(
        json.dumps(
            {
                "type": "result",
                "num_turns": 4,
                "total_cost_usd": 0.05,
                "usage": {"input_tokens": 10, "output_tokens": 500},
                "modelUsage": {
                    "main": {
                        "inputTokens": 10,
                        "outputTokens": 500,
                        "cacheReadInputTokens": 40000,
                        "cacheCreationInputTokens": 12000,
                    },
                    "helper": {"inputTokens": 300, "outputTokens": 20},
                },
            }
        )
    )
elif kind == "opencode":
    print("progress text that is not JSON")
    for _ in range(2):
        print(json.dumps({"type": "tool_use", "part": {"type": "tool"}}))
    for _ in range(3):
        print(
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "cost": 0.004,
                        "tokens": {"input": 2000, "output": 150, "reasoning": 50, "cache": {"read": 8000, "write": 0}},
                    },
                }
            )
        )
elif kind == "codex":
    print(json.dumps({"type": "thread.started"}))
    for item in ("command_execution", "command_execution", "agent_message"):
        print(json.dumps({"type": "item.completed", "item": {"type": item}}))
    print(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 20000,
                    "cached_input_tokens": 15000,
                    "output_tokens": 900,
                    "reasoning_output_tokens": 400,
                },
            }
        )
    )
elif kind == "cursor":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}))
elif kind == "pi":
    print(json.dumps({"type": "session", "version": 3}))
    msg = {
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}, {"type": "toolCall", "id": "call_1", "name": "bash"}],
        "usage": {"input": 800, "output": 120, "cacheRead": 1500, "cacheWrite": 0, "cost": {"total": 0.001}},
    }
    for _ in range(3):  # same usage on 3 event types: only message_end must count
        for typ in ("message_update", "message_end", "turn_end"):
            print(json.dumps({"type": typ, "message": msg}))
else:
    sys.exit(f"unknown kind {kind}")

if os.environ.get("BENCH_TAG") and kind != "cursor":
    with open(os.environ["FAKE_TAGS_FILE"], "a") as f:
        f.write(os.environ["BENCH_TAG"] + "\n")
```

`tests/fake_litellm.py`:

```python
"""Fake LiteLLM /spend/logs/v2 for tests: 2 rows of $0.01 per tag in FAKE_TAGS_FILE,
plus one unrelated row that must be ignored."""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY = "test-key"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            self.send_response(401)
            self.end_headers()
            return
        path = os.environ["FAKE_TAGS_FILE"]
        tags = open(path).read().split() if os.path.exists(path) else []
        rows = [
            {"request_tags": json.dumps([t]), "spend": 0.01, "prompt_tokens": 100, "completion_tokens": 10}
            for t in tags
            for _ in range(2)
        ]
        rows.append({"request_tags": ["unrelated"], "spend": 9.0, "prompt_tokens": 1})
        body = json.dumps({"data": rows, "total_pages": 1}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(port=0):
    """Start server; return (server, port)."""
    srv = HTTPServer(("127.0.0.1", port), Handler)
    return srv, srv.server_address[1]
```

Golden tests (`python3 -m unittest discover -s tests -p 'test_*.py'`) freeze the `report` and `run` output against files in `tests/golden/`. `tests/test_golden.py`:

```python
"""Golden (characterization) tests: freeze current CLI output so refactors stay safe.

Run: python3 -m unittest discover -s tests -p 'test_*.py'
Regenerate goldens only for an intended behavior change: HB_UPDATE_GOLDEN=1.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

ROOT = fixtures.ROOT
GOLDEN = ROOT / "tests" / "golden"
PY = fixtures.PY


def make_tmp(test: unittest.TestCase) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="hb-golden-"))
    test.addCleanup(shutil.rmtree, tmp, True)
    fixtures.copy_engine(tmp)
    (tmp / "results").mkdir()
    (tmp / "config.json").write_text('{"output_dir": "results"}')
    return tmp


def run_cli(tmp: Path, script: str, *args: str, config: str = "config.json") -> subprocess.CompletedProcess[str]:
    return subprocess.run([PY, script, *args, "--config", config], cwd=tmp, capture_output=True, text=True)


def mask_summary(md: str) -> str:
    lines = md.split("\n")
    lines[0] = "# Harness x model — <masked>"
    return "\n".join(lines)


def mask_row(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    for k in ("ts", "wall_s", "run_tag", "t_start", "t_end"):
        if k in row:
            row[k] = "<masked>"
    return row


class GoldenCase(unittest.TestCase):
    def assert_golden(self, name: str, actual: str) -> None:
        path = GOLDEN / name
        if os.environ.get("HB_UPDATE_GOLDEN") == "1":
            path.write_text(actual)
            return
        self.assertTrue(path.exists(), f"missing golden {path}; create with HB_UPDATE_GOLDEN=1")
        self.assertEqual(path.read_text(), actual)


class GoldenReport(GoldenCase):
    def test_summary_matches_golden(self) -> None:
        tmp = make_tmp(self)
        shutil.copy(GOLDEN / "results.jsonl", tmp / "results" / "results.jsonl")
        p = run_cli(tmp, "harness_bench.py", "report")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assert_golden("summary.md", mask_summary((tmp / "results/summary.md").read_text()))
        self.assert_golden("summary.csv", (tmp / "results/summary.csv").read_text())


class GoldenRun(GoldenCase):
    def test_results_match_golden(self) -> None:
        tmp = make_tmp(self)
        shutil.copytree(ROOT / "tasks" / "fizzbuzz", tmp / "tasks" / "fizzbuzz")
        cfg = fixtures.fake_config({"FAKE_TAGS_FILE": str(tmp / "tags.txt")})
        (tmp / "config.json").write_text(json.dumps(cfg))
        p = run_cli(tmp, "harness_bench.py", "run", "--allow-unsandboxed")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = [json.loads(x) for x in (tmp / "results/results.jsonl").read_text().splitlines() if x.strip()]
        self.assertEqual(len(rows), 5)
        actual = "".join(json.dumps(mask_row(r), sort_keys=True) + "\n" for r in rows)
        self.assert_golden("run.jsonl", actual)


if __name__ == "__main__":
    unittest.main()
```

`tests/fixtures.py` (shared by the smoke and golden tests):

```python
"""Shared test fixtures: fake benchmark config, temp dirs, results rows and output capture.

Used by every test module (smoke, golden and the in-process unit tests) so they exercise the
same setup. Importing it puts the repo root on sys.path, so tests can import the engine directly.
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # lets tests import the engine in-process (coverage sees it directly)
PY = sys.executable
AGENT = str(ROOT / "tests" / "fake_agent.py")
HARNESS_KINDS = ("claude", "opencode", "pi", "codex", "cursor")


def prices() -> dict[str, dict[str, int]]:
    """A fresh price table (one entry, `x`), so callers may mutate it."""
    return {"x": {"input": 3, "output": 15}}


def harness(kind: str, extra_env: Mapping[str, str], mode: str = "{model_name}") -> dict[str, Any]:
    """Config entry that runs tests/fake_agent.py as harness `kind`."""
    return {
        "parser": kind,
        "command": [PY, AGENT, kind, mode],
        "env": {"ANTHROPIC_CUSTOM_HEADERS": "x-litellm-tags: {run_tag}", **extra_env},
    }


def fizzbuzz_task() -> dict[str, Any]:
    """A fresh copy of the fizzbuzz task, so callers may mutate it."""
    return {
        "id": "fizzbuzz",
        "repo": "tasks/fizzbuzz/repo",
        "prompt": "fix it",
        "hidden_tests": "tasks/fizzbuzz/hidden_tests",
        "test": f"{PY} -m unittest -q test_hidden.py",
    }


def matrix() -> list[dict[str, Any]]:
    """One entry per harness kind; `nofix` makes the agent leave the bug in place."""
    return [
        {"harness": "claude", "model": "fix"},
        {"harness": "opencode", "model": "p/nofix"},
        {"harness": "pi", "model": "p/fix"},
        {"harness": "codex", "model": "fix", "price": "x"},
        {"harness": "cursor", "model": "fix", "price": "x"},
    ]


def fake_config(extra_env: Mapping[str, str]) -> dict[str, Any]:
    """Full benchmark config with all five fake harnesses; `extra_env` goes into each harness env."""
    return {
        "reps": 1,
        "harnesses": {kind: harness(kind, extra_env) for kind in HARNESS_KINDS},
        "matrix": matrix(),
        "prices": prices(),
        "tasks": [fizzbuzz_task()],
    }


def copy_engine(dest: Path) -> None:
    """Copy the engine scripts and the `hbench` package into `dest`, so they run as a standalone checkout."""
    for name in ("harness_bench.py", "harness_bench_litellm.py"):
        shutil.copy(ROOT / name, dest)
    shutil.copytree(ROOT / "hbench", dest / "hbench", ignore=shutil.ignore_patterns("__pycache__"))


def tmpdir(test: unittest.TestCase) -> Path:
    """A temp directory removed when `test` finishes."""
    path = Path(tempfile.mkdtemp(prefix="hb-unit-")).resolve()
    test.addCleanup(shutil.rmtree, path, True)
    return path


def run_row(**overrides: Any) -> dict[str, Any]:
    """A complete results.jsonl row (the fields `aggregate` and `reconcile` read)."""
    row: dict[str, Any] = {
        "task": "t1",
        "harness": "h",
        "model": "m",
        "rep": 1,
        "passed": True,
        "input": 100,
        "output": 10,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "turns": 1,
        "wall_s": 1.0,
        "parse_ok": True,
        "run_tag": "tag-1",
        "t_start": 1_700_000_000.0,
        "t_end": 1_700_000_010.0,
        "cost_reported": None,
        "cost_computed": None,
    }
    row.update(overrides)
    return row


def write_rows(path: Path, *rows: dict[str, Any]) -> None:
    """Write `rows` as a results.jsonl file at `path`."""
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@contextlib.contextmanager
def quiet() -> Iterator[io.StringIO]:
    """Capture stdout (the engine prints progress); yields the buffer."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def call_quiet(fn: Callable[..., Any], *args: Any) -> str:
    """Call `fn(*args)` with stdout captured and return what it printed."""
    with quiet() as buf:
        fn(*args)
    return buf.getvalue()
```

Unit tests call the engine in-process (`python3 -m unittest discover -s tests -p 'test_*.py'`), so `make coverage` sees every line.

`tests/test_parsers.py`:

```python
"""Unit tests for the output parsers, cost helpers and formatting. In-process, no subprocesses."""

import json
import unittest
from typing import Any

import fixtures  # noqa: F401  (puts the repo root on sys.path)
import harness_bench as hb


def jl(*objs: Any) -> str:
    return "\n".join(json.dumps(o) for o in objs)


class NumTests(unittest.TestCase):
    def test_returns_first_numeric_value_among_keys(self) -> None:
        self.assertEqual(hb.num({"a": "x", "b": 7}, "a", "b"), 7)

    def test_booleans_are_not_numbers(self) -> None:
        self.assertEqual(hb.num({"a": True}, "a"), 0)

    def test_non_dict_gives_zero(self) -> None:
        self.assertEqual(hb.num(None, "a"), 0)

    def test_missing_keys_give_zero(self) -> None:
        self.assertEqual(hb.num({}, "a"), 0)


class JsonLinesTests(unittest.TestCase):
    def test_skips_non_json_and_malformed_lines(self) -> None:
        text = 'progress\n{"a": 1}\n{broken\n  {"b": 2}  \n'
        self.assertEqual(list(hb.json_lines(text)), [{"a": 1}, {"b": 2}])


class ExpandEnvTests(unittest.TestCase):
    def test_unset_variable_expands_to_empty_string(self) -> None:
        self.assertEqual(hb.expand_env("a-${HB_SURELY_UNSET_VAR}-b"), "a--b")


class ClaudeParserTests(unittest.TestCase):
    def test_sums_model_usage_including_helper_models(self) -> None:
        out = jl(
            {
                "type": "result",
                "num_turns": 2,
                "total_cost_usd": 0.5,
                "modelUsage": {"a": {"inputTokens": 1, "outputTokens": 2}, "b": {"inputTokens": 10}},
            }
        )
        u = hb.parse_claude(out)
        self.assertEqual((u["input"], u["output"], u["turns"], u["cost_reported"]), (11, 2, 2, 0.5))

    def test_falls_back_to_usage_without_model_usage(self) -> None:
        out = jl({"type": "result", "usage": {"input_tokens": 5, "output_tokens": 6, "cache_read_input_tokens": 7}})
        u = hb.parse_claude(out)
        self.assertEqual((u["input"], u["output"], u["cache_read"]), (5, 6, 7))

    def test_missing_cost_stays_none_not_zero(self) -> None:
        self.assertIsNone(hb.parse_claude(jl({"type": "result", "usage": {}}))["cost_reported"])

    def test_parses_single_pretty_printed_object(self) -> None:
        out = json.dumps({"type": "result", "usage": {"input_tokens": 3}}, indent=2)
        self.assertEqual(hb.parse_claude(out)["input"], 3)

    def test_non_json_output_returns_none(self) -> None:
        self.assertIsNone(hb.parse_claude("not json"))

    def test_stream_json_uses_the_result_event_and_ignores_others(self) -> None:
        out = jl({"type": "assistant"}, {"type": "result", "usage": {"input_tokens": 4}})
        self.assertEqual(hb.parse_claude(out)["input"], 4)


class OpencodeParserTests(unittest.TestCase):
    def test_sums_step_finish_events_and_accepts_dash_spelling(self) -> None:
        out = jl(
            {"type": "step_finish", "part": {"cost": 0.1, "tokens": {"input": 1, "cache": {"read": 4, "write": 5}}}},
            {"type": "step-finish", "tokens": {"input": 2, "cacheRead": 6, "cacheWrite": 7}},
            {"type": "text"},
        )
        u = hb.parse_opencode(out)
        self.assertEqual((u["input"], u["cache_read"], u["cache_write"], u["turns"]), (3, 10, 12, 2))
        self.assertAlmostEqual(u["cost_reported"], 0.1)

    def test_no_step_finish_returns_none(self) -> None:
        self.assertIsNone(hb.parse_opencode(jl({"type": "text"})))


class PiParserTests(unittest.TestCase):
    def test_counts_only_assistant_message_end(self) -> None:
        usage = {"input": 10, "cost": {"total": 0.25}}
        out = jl(
            {"type": "message_end", "message": {"role": "assistant", "usage": usage}},
            {"type": "message_end", "message": {"role": "user", "usage": usage}},
            {"type": "message_update", "message": {"role": "assistant", "usage": usage}},
        )
        u = hb.parse_pi(out)
        self.assertEqual((u["input"], u["turns"], u["cost_reported"]), (10, 1, 0.25))

    def test_numeric_cost_is_accepted(self) -> None:
        out = jl({"type": "message_end", "message": {"role": "assistant", "usage": {"cost": 0.5}}})
        self.assertEqual(hb.parse_pi(out)["cost_reported"], 0.5)

    def test_absent_cost_is_none_not_zero(self) -> None:
        out = jl({"type": "message_end", "message": {"role": "assistant", "usage": {"input": 1}}})
        self.assertIsNone(hb.parse_pi(out)["cost_reported"])

    def test_no_assistant_messages_returns_none(self) -> None:
        self.assertIsNone(hb.parse_pi(jl({"type": "session"})))


class NullParserTests(unittest.TestCase):
    def test_usage_is_zero_and_the_cost_is_a_real_zero(self) -> None:
        self.assertEqual(hb.parse_null("anything"), {**hb.empty_usage(), "cost_reported": 0.0, "tool_calls": 0})


class CursorParserTests(unittest.TestCase):
    def test_result_without_usage_returns_none(self) -> None:
        self.assertIsNone(hb.parse_cursor(jl({"type": "result", "result": "ok"})))

    def test_no_result_returns_none(self) -> None:
        self.assertIsNone(hb.parse_cursor("plain text"))

    def test_events_before_the_result_are_ignored(self) -> None:
        out = jl({"type": "system"}, {"type": "result", "usage": {"inputTokens": 2}})
        self.assertEqual(hb.parse_cursor(out)["input"], 2)

    def test_usage_fields_and_cost_are_read_when_present(self) -> None:
        out = jl(
            {
                "type": "result",
                "usage": {"inputTokens": 1, "outputTokens": 2, "cacheReadTokens": 3, "cacheWriteTokens": 4},
                "total_cost_usd": 0.75,
            }
        )
        u = hb.parse_cursor(out)
        self.assertEqual((u["input"], u["output"], u["cache_read"], u["cache_write"]), (1, 2, 3, 4))
        self.assertEqual((u["cost_reported"], u["turns"]), (0.75, 1))

    def test_zero_cost_is_reported_as_none(self) -> None:
        out = jl({"type": "result", "usage": {"inputTokens": 1}})
        self.assertIsNone(hb.parse_cursor(out)["cost_reported"])


class CodexParserTests(unittest.TestCase):
    def test_uses_last_cumulative_usage_and_splits_cached_input(self) -> None:
        out = jl(
            {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5}},
            {"type": "turn.completed", "usage": {"input_tokens": 300, "cached_input_tokens": 100, "output_tokens": 9}},
        )
        u = hb.parse_codex(out)
        self.assertEqual((u["input"], u["cache_read"], u["output"], u["turns"]), (200, 100, 9, 2))

    def test_cached_larger_than_input_never_goes_negative(self) -> None:
        out = jl({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 5}})
        self.assertEqual(hb.parse_codex(out)["input"], 0)

    def test_no_completed_turn_returns_none(self) -> None:
        self.assertIsNone(hb.parse_codex(jl({"type": "thread.started"})))


class ComputedCostTests(unittest.TestCase):
    USAGE = {"input": 1_000_000, "output": 1_000_000, "reasoning": 0, "cache_read": 1_000_000, "cache_write": 0}

    def test_missing_price_gives_none(self) -> None:
        self.assertIsNone(hb.computed_cost(self.USAGE, None))

    def test_all_zero_price_gives_none_not_zero(self) -> None:
        self.assertIsNone(hb.computed_cost(self.USAGE, {"input": 0, "output": 0, "_note": "free?"}))

    def test_cache_read_defaults_to_input_price(self) -> None:
        self.assertAlmostEqual(hb.computed_cost(self.USAGE, {"input": 1, "output": 2}), 4.0)

    def test_explicit_cache_read_price_wins(self) -> None:
        self.assertAlmostEqual(hb.computed_cost(self.USAGE, {"input": 1, "output": 2, "cache_read": 0.5}), 3.5)


class RowTagsTests(unittest.TestCase):
    def test_list_is_returned_as_is(self) -> None:
        self.assertEqual(hb.row_tags({"request_tags": ["a", "b"]}), ["a", "b"])

    def test_json_string_is_decoded(self) -> None:
        self.assertEqual(hb.row_tags({"request_tags": '["a"]'}), ["a"])

    def test_comma_separated_string_is_split(self) -> None:
        self.assertEqual(hb.row_tags({"request_tags": "a, b"}), ["a", "b"])

    def test_missing_or_non_list_gives_empty(self) -> None:
        self.assertEqual((hb.row_tags({}), hb.row_tags({"request_tags": "7"})), ([], []))


class ToolCallTests(unittest.TestCase):
    """Tool calls are counted where the format exposes them and are None (never 0) elsewhere."""

    def test_empty_usage_has_unknown_tool_calls(self) -> None:
        self.assertIsNone(hb.empty_usage()["tool_calls"])

    def test_claude_stream_json_counts_unique_tool_use_blocks(self) -> None:
        use = {"type": "tool_use", "id": "a", "name": "Bash"}
        out = jl(
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "x"}, use]}},
            {"type": "assistant", "message": {"id": "m1", "content": [use]}},  # the same block seen again
            {"type": "assistant", "message": {"id": "m2", "content": [{"type": "tool_use", "id": "b"}]}},
            {"type": "assistant", "message": {"id": "m3", "content": "plain text"}},
            {"type": "result", "usage": {"input_tokens": 1}},
        )
        self.assertEqual(hb.parse_claude(out)["tool_calls"], 2)

    def test_claude_stream_json_without_tool_use_is_a_known_zero(self) -> None:
        out = jl({"type": "assistant", "message": {"content": [{"type": "text"}]}}, {"type": "result", "usage": {}})
        self.assertEqual(hb.parse_claude(out)["tool_calls"], 0)

    def test_claude_tool_use_blocks_without_an_id_are_all_counted(self) -> None:
        block = {"type": "tool_use"}
        out = jl({"type": "assistant", "message": {"content": [block, block]}}, {"type": "result", "usage": {}})
        self.assertEqual(hb.parse_claude(out)["tool_calls"], 2)

    def test_claude_single_result_object_does_not_expose_tool_calls(self) -> None:
        self.assertIsNone(hb.parse_claude(jl({"type": "result", "usage": {"input_tokens": 1}}))["tool_calls"])

    def test_opencode_counts_tool_use_events(self) -> None:
        out = jl({"type": "tool_use"}, {"type": "tool-use"}, {"type": "text"}, {"type": "step_finish", "part": {}})
        self.assertEqual(hb.parse_opencode(out)["tool_calls"], 2)

    def test_opencode_without_tool_events_leaves_tool_calls_unknown(self) -> None:
        self.assertIsNone(hb.parse_opencode(jl({"type": "step_finish", "part": {}}))["tool_calls"])

    def test_pi_counts_tool_call_blocks_on_assistant_message_end_only(self) -> None:
        call = {"type": "toolCall", "id": "1", "name": "bash"}
        msg = {"role": "assistant", "content": [{"type": "text"}, call, call], "usage": {"input": 1}}
        out = jl(
            {"type": "message_update", "message": msg},
            {"type": "message_end", "message": msg},
            {"type": "turn_end", "message": msg},
            {"type": "message_end", "message": {**msg, "role": "user"}},
        )
        self.assertEqual(hb.parse_pi(out)["tool_calls"], 2)

    def test_pi_messages_without_a_content_list_leave_tool_calls_unknown(self) -> None:
        out = jl({"type": "message_end", "message": {"role": "assistant", "usage": {"input": 1}}})
        self.assertIsNone(hb.parse_pi(out)["tool_calls"])

    def test_pi_tool_calls_are_known_once_any_message_has_content(self) -> None:
        out = jl(
            {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "toolCall"}]}},
            {"type": "message_end", "message": {"role": "assistant"}},
        )
        self.assertEqual(hb.parse_pi(out)["tool_calls"], 1)

    def test_codex_counts_completed_tool_items(self) -> None:
        done = {"type": "item.completed"}
        out = jl(
            {**done, "item": {"type": "command_execution"}},
            {**done, "item": {"type": "file_change"}},
            {**done, "item": {"type": "mcp_tool_call"}},
            {**done, "item": {"type": "web_search"}},
            {**done, "item": {"type": "agent_message"}},
            {"type": "item.started", "item": {"type": "command_execution"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        )
        self.assertEqual(hb.parse_codex(out)["tool_calls"], 4)

    def test_codex_without_item_events_leaves_tool_calls_unknown(self) -> None:
        out = jl({"type": "turn.completed", "usage": {"input_tokens": 1}})
        self.assertIsNone(hb.parse_codex(out)["tool_calls"])

    def test_codex_with_only_message_items_is_a_known_zero(self) -> None:
        out = jl(
            {"type": "item.completed", "item": {"type": "agent_message"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        )
        self.assertEqual(hb.parse_codex(out)["tool_calls"], 0)

    def test_cursor_json_has_no_tool_calls(self) -> None:
        out = jl({"type": "result", "usage": {"inputTokens": 2}})
        self.assertIsNone(hb.parse_cursor(out)["tool_calls"])

    def test_null_harness_made_no_tool_calls(self) -> None:
        self.assertEqual(hb.parse_null("")["tool_calls"], 0)


if __name__ == "__main__":
    unittest.main()
```

`tests/test_workspace.py`:

```python
"""Unit tests for config validation, sandbox wrapping, env files and workdir handling. In-process."""

import dataclasses
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import fixtures
import harness_bench as hb
from hbench.graders.diff_constraints import DiffConstraintsGrader

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def run_git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True, capture_output=True, text=True)
    return p.stdout.strip()


def base_config(**overrides: Any) -> dict[str, Any]:
    cfg = fixtures.fake_config({})
    cfg.update(overrides)
    return cfg


class ValidateConfigTests(unittest.TestCase):
    def assert_rejected(self, cfg: dict[str, Any], fragment: str) -> None:
        with self.assertRaises(SystemExit) as cm:
            hb.validate_config(cfg)
        self.assertIn(fragment, str(cm.exception.code))

    def test_valid_config_passes(self) -> None:
        hb.validate_config(base_config())

    def test_task_id_with_separator_is_rejected(self) -> None:
        cfg = base_config(tasks=[dict(fixtures.fizzbuzz_task(), id="a/b")])
        self.assert_rejected(cfg, "invalid task id")

    def test_task_id_with_leading_dot_is_rejected(self) -> None:
        cfg = base_config(tasks=[dict(fixtures.fizzbuzz_task(), id=".hidden")])
        self.assert_rejected(cfg, "invalid task id")

    def test_option_like_repo_is_rejected(self) -> None:
        cfg = base_config(tasks=[dict(fixtures.fizzbuzz_task(), repo="--upload-pack=x")])
        self.assert_rejected(cfg, "must not start with '-'")

    def test_absolute_protected_path_is_rejected(self) -> None:
        cfg = base_config(tasks=[dict(fixtures.fizzbuzz_task(), protected_paths=["/etc/passwd"])])
        self.assert_rejected(cfg, "must be relative")

    def test_protected_path_with_parent_reference_is_rejected(self) -> None:
        cfg = base_config(tasks=[dict(fixtures.fizzbuzz_task(), protected_paths=["a/../b"])])
        self.assert_rejected(cfg, "must be relative")

    def test_hidden_tests_without_a_hidden_tests_grader_warns(self) -> None:
        cfg = base_config()
        cfg["tasks"][0]["graders"] = [{"type": "command", "command": "true"}]
        with fixtures.quiet() as out:
            hb.validate_config(cfg)
        self.assertIn("task fizzbuzz sets 'hidden_tests'", out.getvalue())

    def test_hidden_tests_with_the_grader_or_the_default_graders_does_not_warn(self) -> None:
        cfg = base_config()
        with fixtures.quiet() as out:
            hb.validate_config(cfg)
            cfg["tasks"][0]["graders"] = [{"type": "hidden_tests"}, {"type": "command", "command": "true"}]
            hb.validate_config(cfg)
        self.assertEqual(out.getvalue(), "")

    def test_harness_name_with_space_is_rejected(self) -> None:
        cfg = base_config()
        cfg["harnesses"]["bad name"] = cfg["harnesses"]["claude"]
        self.assert_rejected(cfg, "invalid harness name")

    def test_unknown_cost_source_is_rejected_with_the_valid_names(self) -> None:
        self.assert_rejected(base_config(cost_source="bogus"), "auto, litellm, computed, reported")

    def test_report_knobs_in_range_are_accepted(self) -> None:
        for key, value in (("recommend_within_pp", 0), ("recommend_within_pp", 100), ("target_ci_width", 0.5)):
            with self.subTest(key=key, value=value):
                hb.validate_config(base_config(**{key: value}))
        hb.validate_config(base_config(target_ci_width=1))

    def test_report_knobs_out_of_range_or_not_numbers_are_rejected(self) -> None:
        bad = (
            ("recommend_within_pp", -1),
            ("recommend_within_pp", 101),
            ("recommend_within_pp", "5"),
            ("recommend_within_pp", True),
            ("target_ci_width", 0),
            ("target_ci_width", 1.5),
            ("target_ci_width", "0.2"),
            ("target_ci_width", True),
        )
        for key, value in bad:
            with self.subTest(key=key, value=value):
                self.assert_rejected(base_config(**{key: value}), key)

    def test_spend_key_in_env_passthrough_is_rejected(self) -> None:
        cfg = base_config(litellm={"base_url": "https://x", "api_key": "${SPEND}"}, env_passthrough=["SPEND"])
        self.assert_rejected(cfg, "spend key")

    def test_spend_key_in_harness_env_is_rejected(self) -> None:
        cfg = base_config(litellm={"base_url": "https://x", "api_key": "${SPEND}"})
        cfg["harnesses"]["pi"]["env"]["TOKEN"] = "Bearer ${SPEND}"
        self.assert_rejected(cfg, "spend key")


class SafeNameAndRedactTests(unittest.TestCase):
    def test_safe_replaces_separators_and_strips_leading_dots(self) -> None:
        self.assertEqual(hb.safe("../a/b c"), "_a_b_c")

    def test_safe_never_returns_empty(self) -> None:
        self.assertEqual(hb.safe("..."), "_")

    def test_redact_hides_long_secrets_only(self) -> None:
        text = hb.redact("value=hunter2-hunter2 short=abc", ["hunter2-hunter2", "abc", ""])
        self.assertEqual(text, "value=***REDACTED*** short=abc")


class BaseEnvTests(unittest.TestCase):
    def test_only_allowlisted_and_passthrough_variables_reach_the_agent(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.update(HB_PASS="1", HB_SECRET="2", SPEND="3")
        env = hb.base_env({"env_passthrough": ["HB_PASS", "HB_MISSING"], "litellm": {"api_key": "${SPEND}"}})
        self.assertEqual(("HB_PASS" in env, "HB_SECRET" in env, "SPEND" in env), (True, False, False))

    def test_spend_key_is_removed_even_when_allowlisted(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ["TZ"] = "UTC"
        env = hb.base_env({"litellm": {"api_key": "${TZ}"}})
        self.assertNotIn("TZ", env)


class RmtreeInsideTests(unittest.TestCase):
    def test_removes_a_directory_inside_root(self) -> None:
        root = fixtures.tmpdir(self)
        child = root / "child"
        child.mkdir()
        hb.rmtree_inside(child, root)
        self.assertFalse(child.exists())

    def test_refuses_the_root_itself(self) -> None:
        root = fixtures.tmpdir(self)
        with self.assertRaises(RuntimeError):
            hb.rmtree_inside(root, root)
        self.assertTrue(root.exists())

    def test_refuses_a_path_outside_root(self) -> None:
        root, other = fixtures.tmpdir(self), fixtures.tmpdir(self)
        with self.assertRaises(RuntimeError):
            hb.rmtree_inside(other, root)
        self.assertTrue(other.exists())


class WrapTests(unittest.TestCase):
    def test_without_prefix_returns_argv_unchanged(self) -> None:
        self.assertEqual(hb.wrap({}, ["a", "b"], "/w", "/e"), ["a", "b"])

    def test_prefix_is_formatted_and_prepended(self) -> None:
        cfg = {"sandbox": {"prefix": ["box", "--env-file", "{env_file}", "-v", "{workdir}:/w"]}}
        self.assertEqual(hb.wrap(cfg, ["a"], "/w", "/e"), ["box", "--env-file", "/e", "-v", "/w:/w", "a"])


class WriteEnvFileTests(unittest.TestCase):
    def test_file_is_private_and_skips_host_only_and_multiline_values(self) -> None:
        path = fixtures.tmpdir(self) / "env"
        hb.write_env_file({"A": "1", "PATH": "/bin", "B": "x\ny", "HOME": "/h"}, path)
        self.assertEqual((path.read_text(), path.stat().st_mode & 0o777), ("A=1\n", 0o600))


class RunProcTests(unittest.TestCase):
    def test_returns_exit_code_and_streams(self) -> None:
        code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
        rc, timed_out, out, err = hb.run_proc([fixtures.PY, "-c", code], None, os.environ.copy(), 30)
        self.assertEqual((rc, timed_out, out.strip(), err.strip()), (3, False, "out", "err"))

    def test_timeout_is_reported_with_partial_output(self) -> None:
        # The child flushes a readiness line before sleeping; 5 s leaves room for slow interpreter start-up.
        code = "import sys, time; print('partial', flush=True); print('e', file=sys.stderr, flush=True); time.sleep(60)"
        rc, timed_out, out, err = hb.run_proc([fixtures.PY, "-c", code], None, os.environ.copy(), 5)
        self.assertEqual((rc, timed_out, out.strip(), err.strip()), (-1, True, "partial", "e"))


class BuildCmdTests(unittest.TestCase):
    def test_provider_is_split_from_model(self) -> None:
        cmd = hb.build_cmd(["{provider}", "{model_name}", "{model}", "{prompt}"], "p/m", "go")
        self.assertEqual(cmd, ["p", "m", "p/m", "go"])

    def test_model_without_provider_has_empty_provider(self) -> None:
        self.assertEqual(hb.build_cmd(["[{provider}]", "{model_name}"], "m", "go"), ["[]", "m"])


class DoneKeysTests(unittest.TestCase):
    def test_missing_file_gives_no_keys(self) -> None:
        self.assertEqual(hb.done_keys(fixtures.tmpdir(self) / "none.jsonl"), set())

    def test_keys_identify_task_harness_model_rep(self) -> None:
        path = fixtures.tmpdir(self) / "r.jsonl"
        path.write_text('{"task": "t", "harness": "h", "model": "m", "rep": 2}\nnoise\n')
        self.assertEqual(hb.done_keys(path), {("t", "h", "m", 2)})


class ResolveWorkRootTests(unittest.TestCase):
    def test_work_dir_inside_the_repo_is_rejected(self) -> None:
        base = fixtures.tmpdir(self)
        with self.assertRaises(SystemExit) as cm:
            hb.resolve_work_root({"work_dir": str(base / "work")}, base)
        self.assertIn("inside the benchmark repo", str(cm.exception.code))

    def test_work_dir_outside_the_repo_is_created(self) -> None:
        base, outside = fixtures.tmpdir(self), fixtures.tmpdir(self)
        root = hb.resolve_work_root({"work_dir": str(outside / "w")}, base)
        self.assertEqual((root, root.is_dir()), (outside / "w", True))

    def test_default_is_a_fresh_temp_dir(self) -> None:
        root = hb.resolve_work_root({}, fixtures.tmpdir(self))
        self.addCleanup(root.rmdir)
        self.assertTrue(root.name.startswith("harness-bench-work-"))


class PrepareWorkdirTests(unittest.TestCase):
    def make_git_repo(self, base: Path) -> tuple[Path, str]:
        repo = base / "src"
        repo.mkdir()
        run_git(repo, "init", "-q", "-b", "main")
        (repo / "f.txt").write_text("old")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "one")
        old = run_git(repo, "rev-parse", "HEAD")
        (repo / "f.txt").write_text("new")
        run_git(repo, "commit", "-qam", "two")
        return repo, old

    def test_plain_directory_is_copied(self) -> None:
        base = fixtures.tmpdir(self)
        (base / "repo").mkdir()
        (base / "repo" / "a.txt").write_text("a")
        hb.prepare_workdir({"id": "t", "repo": "repo"}, base, base / "work")
        self.assertEqual((base / "work" / "a.txt").read_text(), "a")

    def test_git_source_is_checked_out_at_ref_without_history(self) -> None:
        base = fixtures.tmpdir(self)
        repo, old = self.make_git_repo(base)
        hb.prepare_workdir({"id": "t", "repo": str(repo), "ref": old}, base, base / "work")
        self.assertEqual(((base / "work" / "f.txt").read_text(), (base / "work" / ".git").exists()), ("old", False))

    def test_git_source_without_ref_uses_head_and_reuses_cache(self) -> None:
        base = fixtures.tmpdir(self)
        repo, _ = self.make_git_repo(base)
        task = {"id": "t", "repo": str(repo)}
        hb.prepare_workdir(task, base, base / "w1")
        hb.prepare_workdir(task, base, base / "w2")
        self.assertEqual((base / "w2" / "f.txt").read_text(), "new")

    def test_is_git_source_recognises_urls_and_ssh(self) -> None:
        self.assertEqual(
            [hb.is_git_source(s) for s in ("a.git", "https://x/y", "git@h:r", "plain")], [True] * 3 + [False]
        )


class GitBinTests(unittest.TestCase):
    def test_resolves_the_git_executable(self) -> None:
        with mock.patch("shutil.which", return_value="/usr/bin/git"):
            self.assertEqual(hb.git_bin(), "/usr/bin/git")

    def test_exits_when_git_is_missing(self) -> None:
        with mock.patch("shutil.which", return_value=None), self.assertRaises(SystemExit) as cm:
            hb.git_bin()
        self.assertEqual(cm.exception.code, "git not found on PATH")


def baseline_of(test: unittest.TestCase, work: Path) -> hb.Baseline:
    """Baseline of `work` whose git dir lives outside it, like the engine's."""
    baseline: hb.Baseline = hb.make_baseline(work, fixtures.tmpdir(test) / "baseline.git")
    return baseline


def sh(cwd: Path, script: str) -> None:
    """Run what the agent could run inside the workdir (a shell script, as the agent's user)."""
    subprocess.run(["sh", "-c", script], cwd=cwd, check=True, capture_output=True)


class MakeBaselineTests(unittest.TestCase):
    def test_returns_a_single_commit_with_current_files_and_leaves_no_git_in_the_workdir(self) -> None:
        work = fixtures.tmpdir(self)
        (work / "a.txt").write_text("a")
        baseline = baseline_of(self, work)
        count = subprocess.run(
            ["git", f"--git-dir={baseline.git_dir}", "rev-list", "--count", baseline.sha],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual((count, len(baseline.sha), (work / ".git").exists()), ("1", 40, False))

    def test_files_listed_in_gitignore_are_part_of_the_baseline(self) -> None:
        work = fixtures.tmpdir(self)
        (work / ".gitignore").write_text("secret.txt\n")
        (work / "secret.txt").write_text("s\n")
        baseline = baseline_of(self, work)
        (work / "secret.txt").write_text("changed\n")
        self.assertEqual([c.path for c in hb.collect_changes(baseline)], ["secret.txt"])


class RunStepTests(unittest.TestCase):
    def test_stdout_and_stderr_come_back_apart_and_redacted_and_the_log_has_both(self) -> None:
        work, log = fixtures.tmpdir(self), fixtures.tmpdir(self) / "step.log"
        script = "echo out-supersecret1; echo err-supersecret1 >&2; exit 3"
        step = hb.run_step({}, script, dict(os.environ), 30, log, work, work.parent / "env", ["supersecret1"])
        self.assertEqual(step, (3, False, "out-***REDACTED***\n", "err-***REDACTED***\n"))
        self.assertEqual(log.read_text(), "out-***REDACTED***\n\n--- stderr ---\nerr-***REDACTED***\n")


class HardenedGitTests(unittest.TestCase):
    def test_no_inherited_git_variable_reaches_git(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nonexistent", "GIT_CONFIG_COUNT": "1"}):
            env = hb.git_env()
        self.assertEqual(
            (sorted(k for k in env if k.startswith("GIT_")), env["GIT_CONFIG_GLOBAL"], env["GIT_CONFIG_NOSYSTEM"]),
            (["GIT_ATTR_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"], "/dev/null", "1"),
        )

    def test_a_replace_ref_in_the_git_dir_does_not_change_the_diff(self) -> None:
        work = fixtures.tmpdir(self)
        (work / "a.txt").write_text("a\n")
        baseline = baseline_of(self, work)
        (work / "a.txt").write_text("b\n")
        (work / "b.txt").write_text("c\n")
        hb.staged_diff(baseline)  # stage the edits, then make the baseline commit "contain" them
        tree = hb.hardened_git(baseline.git_dir, work, "write-tree", check=True).stdout.strip()
        other = hb.hardened_git(baseline.git_dir, work, *GIT_ID, "commit-tree", "-m", "x", tree, check=True)
        subprocess.run(
            [
                "git",
                f"--git-dir={baseline.git_dir}",
                "update-ref",
                f"refs/replace/{baseline.sha}",
                other.stdout.strip(),
            ],
            check=True,
        )
        self.assertEqual(sorted(c.path for c in hb.collect_changes(baseline)), ["a.txt", "b.txt"])

    def test_the_users_global_config_and_excludes_cannot_hide_a_file(self) -> None:
        home = fixtures.tmpdir(self)
        (home / "ignore").write_text("conftest.py\n")
        (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {home / 'ignore'}\n")
        work = fixtures.tmpdir(self)
        (work / "a.txt").write_text("a\n")
        baseline = baseline_of(self, work)
        (work / "conftest.py").write_text("evil\n")
        with mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home)}):
            self.assertEqual([c.path for c in hb.collect_changes(baseline)], ["conftest.py"])


class SaveDiffTests(unittest.TestCase):
    def test_diff_contains_new_and_changed_files(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = baseline_of(self, work)
        (work / "a.txt").write_text("changed")
        (work / "new.txt").write_text("n")
        hb.save_diff(baseline, out)
        text = out.read_text()
        self.assertIn("a.txt", text)
        self.assertIn("new.txt", text)

    def test_unknown_baseline_is_reported_in_the_diff_file(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        hb.save_diff(dataclasses.replace(baseline_of(self, work), sha="0" * 40), out)
        self.assertIn("git diff failed", out.read_text())

    def test_a_git_that_cannot_run_is_reported_in_the_diff_file(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = baseline_of(self, work)
        for error in (OSError("no git"), subprocess.TimeoutExpired("git", 1)):
            with mock.patch.object(hb, "staged_diff", side_effect=error):
                hb.save_diff(baseline, out)
            self.assertIn("git diff failed", out.read_text())

    def test_directory_that_is_not_a_repo_is_reported_in_the_diff_file(self) -> None:
        out = fixtures.tmpdir(self) / "d.diff"
        hb.save_diff(hb.Baseline(fixtures.tmpdir(self) / "none.git", fixtures.tmpdir(self), "abc"), out)
        self.assertIn("git diff failed", out.read_text())

    def test_long_diff_is_truncated(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = baseline_of(self, work)
        (work / "a.txt").write_text("b" * 1000)
        hb.save_diff(baseline, out, limit=50)
        self.assertTrue(out.read_text().endswith("[truncated]"))

    def test_a_non_utf8_patch_is_saved_not_raised(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = baseline_of(self, work)
        (work / "a.txt").write_bytes(b"caf\xe9\n")
        hb.save_diff(baseline, out)
        self.assertIn("a.txt", out.read_text())

    def test_a_nested_git_directory_is_noted_in_the_audit_diff(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = baseline_of(self, work)
        sh(work, "mkdir sub && cd sub && git init -q && echo x > f.txt")
        hb.save_diff(baseline, out)
        self.assertIn("nested .git: sub/.git", out.read_text())


class CollectChangesTests(unittest.TestCase):
    def repo(self) -> tuple[Path, hb.Baseline]:
        work = fixtures.tmpdir(self)
        (work / "a.txt").write_text("1\n2\n")
        (work / "gone.txt").write_text("x\n")
        return work, baseline_of(self, work)

    def test_added_and_deleted_lines_are_counted_per_file_including_new_files(self) -> None:
        work, baseline = self.repo()
        (work / "a.txt").write_text("1\nchanged\nmore\n")
        (work / "sub").mkdir()
        (work / "sub" / "new.txt").write_text("n\n")
        got = {c.path: (c.added, c.deleted) for c in hb.collect_changes(baseline)}
        self.assertEqual(got, {"a.txt": (2, 1), "sub/new.txt": (1, 0)})

    def test_a_rename_is_reported_as_a_delete_and_an_add(self) -> None:
        work, baseline = self.repo()
        (work / "gone.txt").rename(work / "moved.txt")
        got = {c.path: (c.added, c.deleted) for c in hb.collect_changes(baseline)}
        self.assertEqual(got, {"gone.txt": (0, 1), "moved.txt": (1, 0)})

    def test_a_binary_file_counts_as_more_than_any_line_limit(self) -> None:
        work, baseline = self.repo()
        (work / "blob.bin").write_bytes(b"\x00\x01\x02")
        self.assertEqual(
            [(c.path, c.added, c.deleted) for c in hb.collect_changes(baseline)], [("blob.bin", hb.BINARY_LINES, 0)]
        )
        self.assertTrue(DiffConstraintsGrader({"max_changed_lines": 10**6}).violations(hb.collect_changes(baseline)))

    def test_a_path_with_a_newline_stays_one_path(self) -> None:
        work, baseline = self.repo()
        (work / "a\nb.txt").write_text("x\n")
        self.assertEqual([c.path for c in hb.collect_changes(baseline)], ["a\nb.txt"])

    def test_a_git_that_cannot_run_fails_closed(self) -> None:
        _, baseline = self.repo()
        for error in (OSError("no git"), subprocess.TimeoutExpired("git", 1)):
            with mock.patch.object(hb, "staged_diff", side_effect=error):
                self.assertEqual(hb.collect_changes(baseline), hb.UNKNOWN_CHANGES)

    def test_a_path_with_a_tab_stays_one_path(self) -> None:
        work, baseline = self.repo()
        (work / "a\tb.txt").write_text("x\n")
        self.assertEqual([(c.path, c.added) for c in hb.collect_changes(baseline)], [("a\tb.txt", 1)])
        self.assertEqual(
            hb.parse_numstat("1\t2\ta\tb\tc.txt\0-\t-\tbin\0"),
            (hb.Change("a\tb\tc.txt", 1, 2), hb.Change("bin", hb.BINARY_LINES, 0)),
        )

    def test_no_changes_gives_an_empty_tuple(self) -> None:
        _, baseline = self.repo()
        self.assertEqual(hb.collect_changes(baseline), ())

    def test_an_unreadable_diff_fails_closed_with_a_path_outside_the_workdir(self) -> None:
        work, baseline = self.repo()
        self.assertEqual(hb.collect_changes(dataclasses.replace(baseline, sha="0" * 40)), hb.UNKNOWN_CHANGES)
        missing = hb.Baseline(fixtures.tmpdir(self) / "none.git", work, "abc")
        self.assertEqual(hb.collect_changes(missing), hb.UNKNOWN_CHANGES)
        self.assertTrue(hb.UNKNOWN_CHANGES[0].path.startswith("../"))


class GitBypassTests(unittest.TestCase):
    """What an agent can do through the git plumbing in its workdir must not hide a change (C1 and C2)."""

    def setUp(self) -> None:
        self.work = fixtures.tmpdir(self)
        (self.work / "tests").mkdir()
        (self.work / "tests" / "test_a.py").write_text("x\n")
        (self.work / "src.py").write_text("a\n")
        self.baseline = baseline_of(self, self.work)

    def paths(self) -> list[str]:
        return sorted(c.path for c in hb.collect_changes(self.baseline))

    def forbidden_hit(self) -> bool:
        grader = DiffConstraintsGrader({"forbidden": ["tests/*", "conftest.py"]})
        return bool(grader.violations(hb.collect_changes(self.baseline)))

    def test_hiding_tricks_through_an_agent_repo_still_fail_diff_constraints(self) -> None:
        # Every script is something the agent can run in its workdir. None touches the baseline git dir.
        scripts = {
            "skip-worktree": "git init -q . && git add -A && git update-index --skip-worktree tests/test_a.py",
            "assume-unchanged": "git init -q . && git add -A && git update-index --assume-unchanged tests/test_a.py",
            "info/exclude": "git init -q . && echo tests/ >> .git/info/exclude",
            "core.excludesFile": "git init -q . && echo tests/ > ../ex && git config core.excludesFile ../ex",
            "git replace": (
                "git init -q . && git add -A && git -c user.name=a -c user.email=a@b commit -qm x"
                " && git replace $(git rev-list --max-parents=0 HEAD) HEAD"
            ),
        }
        for name, script in scripts.items():
            for keep_git in (True, False):
                with self.subTest(name, keep_git=keep_git):
                    sh(self.work, script + " && echo y >> tests/test_a.py")
                    if not keep_git:
                        shutil.rmtree(self.work / ".git")
                    self.assertTrue(self.forbidden_hit())
                    shutil.rmtree(self.work / ".git", ignore_errors=True)
                    (self.work / "tests" / "test_a.py").write_text("x\n")

    def test_a_deleted_agent_repo_leaves_the_edit_visible_by_path(self) -> None:
        sh(self.work, "git init -q . && git add -A && git update-index --skip-worktree tests/test_a.py")
        sh(self.work, "echo y >> tests/test_a.py && rm -rf .git")
        self.assertEqual(self.paths(), ["tests/test_a.py"])

    def test_a_root_gitignore_does_not_hide_a_new_or_edited_file(self) -> None:
        sh(self.work, "echo conftest.py > .gitignore && echo evil > conftest.py && echo y >> tests/test_a.py")
        self.assertEqual(self.paths(), [".gitignore", "conftest.py", "tests/test_a.py"])

    def test_a_nested_gitignore_does_not_hide_a_file_either(self) -> None:
        sh(self.work, "mkdir tests/sub && echo '*' > tests/sub/.gitignore && echo e > tests/sub/evil.py")
        self.assertEqual(self.paths(), ["tests/sub/.gitignore", "tests/sub/evil.py"])

    def test_a_nested_git_directory_fails_closed_and_cannot_hide_its_files(self) -> None:
        sh(self.work, "mkdir tests/vendored && cd tests/vendored && git init -q && echo evil > e.py")
        self.assertEqual(hb.collect_changes(self.baseline), hb.UNKNOWN_CHANGES)

    def test_a_nested_git_file_fails_closed(self) -> None:
        sh(self.work, "echo 'gitdir: /somewhere' > .git")
        self.assertEqual(hb.collect_changes(self.baseline), hb.UNKNOWN_CHANGES)

    def test_a_git_dir_symlink_fails_closed(self) -> None:
        sh(self.work, "mkdir -p a/b && ln -s /tmp a/b/.git")
        self.assertEqual(hb.collect_changes(self.baseline), hb.UNKNOWN_CHANGES)

    def test_the_workdir_git_config_is_never_read(self) -> None:
        marker = self.work.parent / f"{self.work.name}.marker"
        self.addCleanup(marker.unlink, True)
        script = (
            f"git init -q . && git config core.fsmonitor 'touch {marker}; false'"
            f" && git config filter.x.clean 'touch {marker}; cat' && git config diff.x.textconv 'touch {marker}; cat'"
            " && echo '* filter=x diff=x' > .gitattributes && echo y >> src.py"
        )
        sh(self.work, script)
        # The low-level calls run git even though the nested repo makes `collect_changes` fail closed.
        hb.staged_diff(self.baseline, "--numstat")
        hb.staged_diff(self.baseline, "--stat", "--patch")
        self.assertFalse(marker.exists())


class AttributesAndFailuresTests(unittest.TestCase):
    """What the agent can do to make `git add` fail or to skew the counts must never end in "no changes"."""

    def setUp(self) -> None:
        self.work = fixtures.tmpdir(self)
        (self.work / "tests").mkdir()
        (self.work / "tests" / "test_a.py").write_text("x\n")
        (self.work / "src.py").write_text("a\n")
        self.baseline = baseline_of(self, self.work)
        self.grader = DiffConstraintsGrader({"forbidden": ["tests/*"], "max_changed_lines": 5})

    def paths(self) -> list[str]:
        return sorted(c.path for c in hb.collect_changes(self.baseline))

    def test_a_working_tree_encoding_attribute_cannot_make_the_add_fail_open(self) -> None:
        sh(self.work, "printf '* working-tree-encoding=UTF-16\\n' > .gitattributes && echo y >> tests/test_a.py")
        self.assertEqual(self.paths(), [".gitattributes", "tests/test_a.py"])

    def test_a_diff_or_binary_attribute_does_not_zero_the_line_count(self) -> None:
        for attr in ("* -diff", "* binary"):
            with self.subTest(attr):
                sh(
                    self.work,
                    f"printf '{attr}\\n' > .gitattributes && python3 -c \"open('src.py','w').write('l\\\\n'*100)\"",
                )
                changes = hb.collect_changes(self.baseline)
                self.assertEqual(sum(c.added for c in changes if c.path == "src.py"), 100)
                self.assertTrue(self.grader.violations(changes))

    def test_a_failing_add_raises_and_collect_changes_fails_closed(self) -> None:
        sh(
            self.work, "echo y >> tests/test_a.py && git init -q sub && echo e > sub/e.py"
        )  # a nested repo without a commit: git add fails
        with self.assertRaises(hb.GitError) as cm:
            hb.staged_diff(self.baseline, "--stat")
        self.assertIn("git add failed", str(cm.exception))
        self.assertEqual(hb.collect_changes(self.baseline), hb.UNKNOWN_CHANGES)

    def test_a_failing_add_is_written_to_the_audit_diff_with_secrets_redacted(self) -> None:
        sh(self.work, "git init -q sub && echo e > sub/e.py")
        out = fixtures.tmpdir(self) / "d.diff"
        with mock.patch.object(hb, "staged_diff", side_effect=hb.GitError("git add failed: token-0123456789")):
            hb.save_diff(self.baseline, out, secrets=["token-0123456789"])
        self.assertIn("git add failed: ***REDACTED***", out.read_text())
        hb.save_diff(self.baseline, out)
        self.assertIn("git add failed", out.read_text())

    def test_an_unreadable_file_never_ends_as_no_changes(self) -> None:
        sh(self.work, "echo y >> tests/test_a.py && echo s > secret && chmod 000 secret")
        self.addCleanup(os.chmod, self.work / "secret", 0o600)
        changes = hb.collect_changes(self.baseline)
        self.assertTrue(changes == hb.UNKNOWN_CHANGES or "tests/test_a.py" in [c.path for c in changes])
        self.assertTrue(self.grader.violations(changes))

    def test_an_unreadable_tracked_file_never_ends_as_no_changes(self) -> None:
        sh(self.work, "echo y >> tests/test_a.py && chmod 000 src.py")
        self.addCleanup(os.chmod, self.work / "src.py", 0o600)
        self.assertTrue(self.grader.violations(hb.collect_changes(self.baseline)))

    def test_without_the_attributes_file_the_diff_fails_closed_and_says_so(self) -> None:
        sh(self.work, "echo y >> tests/test_a.py")
        out = fixtures.tmpdir(self) / "d.diff"
        (self.baseline.git_dir / "info" / "attributes").unlink()
        self.assertEqual(hb.collect_changes(self.baseline), hb.UNKNOWN_CHANGES)
        hb.save_diff(self.baseline, out)
        self.assertIn("info/attributes", out.read_text())

    def test_a_baseline_that_could_not_write_the_attributes_file_fails_closed(self) -> None:
        work = fixtures.tmpdir(self)
        (work / "a.txt").write_text("a\n")
        with mock.patch.object(hb, "write_info_attributes", side_effect=OSError("read-only")):
            baseline = baseline_of(self, work)
        (work / "a.txt").write_text("edited\n")
        self.assertEqual(hb.collect_changes(baseline), hb.UNKNOWN_CHANGES)

    def test_the_attributes_file_hides_every_attribute_that_changes_how_a_file_is_added(self) -> None:
        (self.work / ".gitattributes").write_text("* filter=x diff=y text eol=crlf working-tree-encoding=UTF-16\n")
        names = ["filter", "diff", "text", "eol", "working-tree-encoding", "ident", "merge"]
        out = subprocess.run(
            [
                "git",
                f"--git-dir={self.baseline.git_dir}",
                f"--work-tree={self.work}",
                "check-attr",
                *names,
                "--",
                "src.py",
            ],
            cwd=self.work,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(out.count("unspecified"), len(names))


class AttributesWithoutAttrSourceTests(AttributesAndFailuresTests):
    """Every attribute test again on a git older than 2.40: `info/attributes` alone must hold."""

    def setUp(self) -> None:
        patcher = mock.patch.object(hb, "git_version", return_value=(2, 34))
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_the_attr_source_option_is_not_passed(self) -> None:
        with mock.patch.object(subprocess, "run") as run:
            hb.hardened_git(self.work.parent, self.work, "status")
        self.assertFalse(any(a.startswith("--attr-source") for a in run.call_args.args[0]))

    def test_a_filter_attribute_does_not_run_or_change_a_file(self) -> None:
        sh(self.work, "printf '* filter=x ident\\n' > .gitattributes && echo y >> src.py")
        self.assertEqual(self.paths(), [".gitattributes", "src.py"])


class GitVersionTests(unittest.TestCase):
    def test_git_version_is_read_once_from_git_version_output(self) -> None:
        hb.git_version.cache_clear()
        self.addCleanup(hb.git_version.cache_clear)
        fake = subprocess.CompletedProcess([], 0, stdout="git version 2.39.5 (Apple Git-154)\n")
        with mock.patch.object(subprocess, "run", return_value=fake) as run:
            self.assertEqual((hb.git_version(), hb.git_version()), ((2, 39), (2, 39)))
        self.assertEqual(run.call_count, 1)
        hb.git_version.cache_clear()
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="weird")):
            self.assertEqual(hb.git_version(), (0, 0))


class FreshIndexAndEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = fixtures.tmpdir(self)
        (self.work / "a.txt").write_text("a\n")
        (self.work / "ignored.txt").write_text("i\n")
        (self.work / ".gitignore").write_text("ignored.txt\n")
        self.baseline = baseline_of(self, self.work)

    def test_skip_worktree_in_the_stored_index_does_not_hide_an_edit(self) -> None:
        git = [f"--git-dir={self.baseline.git_dir}", f"--work-tree={self.work}"]
        subprocess.run(["git", *git, "update-index", "--skip-worktree", "a.txt"], check=True, cwd=self.work)
        subprocess.run(["git", *git, "update-index", "--assume-unchanged", "ignored.txt"], check=True, cwd=self.work)
        (self.work / "a.txt").write_text("edited\n")
        (self.work / "ignored.txt").write_text("edited\n")
        self.assertEqual(sorted(c.path for c in hb.collect_changes(self.baseline)), ["a.txt", "ignored.txt"])

    def test_a_diff_leaves_the_stored_index_untouched(self) -> None:
        stored = self.baseline.git_dir / "index"
        before = stored.read_bytes()
        (self.work / "a.txt").write_text("edited\n")
        hb.collect_changes(self.baseline)
        hb.save_diff(self.baseline, fixtures.tmpdir(self) / "d.diff")
        self.assertEqual(stored.read_bytes(), before)

    def test_inherited_git_variables_do_not_change_the_result(self) -> None:
        (self.work / "a.txt").write_text("edited\n")
        env = {"GIT_DIR": "/nonexistent", "GIT_INDEX_FILE": "/nonexistent/index", "GIT_WORK_TREE": "/nonexistent"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual([c.path for c in hb.collect_changes(self.baseline)], ["a.txt"])

    def test_a_file_a_gitignore_listed_before_the_baseline_is_not_a_change(self) -> None:
        self.assertEqual(hb.collect_changes(self.baseline), ())


class ProtectedPathTests(unittest.TestCase):
    TASK = {"protected_paths": ["tests_dir", "runner.py", "missing.txt"]}

    def setUp(self) -> None:
        self.work, self.snap = fixtures.tmpdir(self), fixtures.tmpdir(self)
        (self.work / "tests_dir").mkdir()
        (self.work / "tests_dir" / "t.py").write_text("orig")
        (self.work / "runner.py").write_text("orig")

    def test_restore_undoes_edits_additions_and_deletions(self) -> None:
        hb.snapshot_protected(self.TASK, self.work, self.snap)
        (self.work / "tests_dir" / "t.py").write_text("hacked")
        (self.work / "tests_dir" / "extra.py").write_text("planted")
        (self.work / "runner.py").unlink()
        hb.restore_protected(self.TASK, self.work, self.snap)
        self.assertEqual(
            (
                (self.work / "tests_dir" / "t.py").read_text(),
                (self.work / "tests_dir" / "extra.py").exists(),
                (self.work / "runner.py").read_text(),
            ),
            ("orig", False, "orig"),
        )

    def test_bytecode_the_agent_leaves_in_a_protected_directory_is_removed_on_restore(self) -> None:
        hb.snapshot_protected(self.TASK, self.work, self.snap)
        (self.work / "tests_dir" / "__pycache__").mkdir()
        (self.work / "tests_dir" / "__pycache__" / "t.cpython-312.pyc").write_bytes(b"poison")
        hb.restore_protected(self.TASK, self.work, self.snap)
        self.assertEqual(sorted(p.name for p in (self.work / "tests_dir").iterdir()), ["t.py"])

    def test_path_replaced_by_a_symlink_is_restored_as_a_real_file(self) -> None:
        hb.snapshot_protected(self.TASK, self.work, self.snap)
        (self.work / "runner.py").unlink()
        (self.work / "runner.py").symlink_to("/etc/hostname")
        hb.restore_protected(self.TASK, self.work, self.snap)
        runner = self.work / "runner.py"
        self.assertEqual((runner.is_symlink(), runner.read_text()), (False, "orig"))

    def test_path_absent_at_snapshot_time_is_removed_on_restore(self) -> None:
        hb.snapshot_protected(self.TASK, self.work, self.snap)
        (self.work / "missing.txt").write_text("planted")
        hb.restore_protected(self.TASK, self.work, self.snap)
        self.assertFalse((self.work / "missing.txt").exists())

    def test_symlinked_protected_path_is_snapshotted_as_a_link(self) -> None:
        (self.work / "runner.py").unlink()
        (self.work / "runner.py").symlink_to("target")
        hb.snapshot_protected(self.TASK, self.work, self.snap)
        self.assertTrue((self.snap / "runner.py").is_symlink())


class CheckLitellmUrlTests(unittest.TestCase):
    def test_https_and_localhost_http_are_allowed(self) -> None:
        for url in ("https://litellm.example.com", "http://localhost:4000", "http://127.0.0.1:1"):
            hb.check_litellm_url(url)

    def test_plain_http_to_a_remote_host_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            hb.check_litellm_url("http://litellm.example.com")


if __name__ == "__main__":
    unittest.main()
```

`tests/test_run.py`:

```python
"""In-process tests for `cmd_run`, `aggregate` and `cmd_report` with the fake agents."""

import argparse
import shutil
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import fixtures
import harness_bench as hb


def args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "reps": None,
        "only": None,
        "keep": False,
        "dry_run": False,
        "allow_unsandboxed": True,
        "no_report": True,
        "cost_source": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def rows_of(base: Path) -> list[dict[str, Any]]:
    return fixtures.read_rows(base / "results" / "results.jsonl")


class RunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = fixtures.tmpdir(self)
        shutil.copytree(fixtures.ROOT / "tasks" / "fizzbuzz", self.base / "tasks" / "fizzbuzz")
        self.cfg = fixtures.fake_config({"FAKE_TAGS_FILE": str(self.base / "tags.txt")})
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}, {"harness": "pi", "model": "p/nofix"}]


class CmdRunTests(RunTestCase):
    def test_refuses_to_run_without_a_sandbox(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(allow_unsandboxed=False), self.cfg, self.base)
        self.assertIn("No sandbox configured", str(cm.exception.code))

    def test_a_git_older_than_2_40_still_runs_with_the_attributes_file_alone(self) -> None:
        with mock.patch.object(hb, "git_version", return_value=(2, 34)):
            fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        self.assertEqual([r["passed"] for r in rows_of(self.base)], [True])

    def test_dry_run_prints_commands_and_writes_no_results(self) -> None:
        out = fixtures.call_quiet(hb.cmd_run, args(dry_run=True, allow_unsandboxed=False), self.cfg, self.base)
        self.assertEqual((out.count("fake_agent.py"), (self.base / "results" / "results.jsonl").exists()), (2, False))

    def test_only_filters_harnesses(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        self.assertEqual([r["harness"] for r in rows_of(self.base)], ["claude"])

    def test_reps_argument_overrides_config(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], reps=2), self.cfg, self.base)
        self.assertEqual([r["rep"] for r in rows_of(self.base)], [1, 2])

    def test_hidden_tests_decide_pass_and_unknown_cost_stays_none(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        rows = {r["harness"]: r for r in rows_of(self.base)}
        self.assertEqual(
            (rows["claude"]["passed"], rows["pi"]["passed"], rows["pi"]["cost_computed"]), (True, False, None)
        )

    def test_null_harness_fails_the_task_at_zero_reported_cost(self) -> None:
        self.cfg["harnesses"]["null"] = {"command": ["true"], "parser": "null"}
        self.cfg["matrix"] = [{"harness": "null", "model": "none"}]
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (row,) = rows_of(self.base)
        self.assertEqual((row["passed"], row["cost_reported"], row["parse_ok"]), (False, 0.0, True))

    def test_finished_runs_are_skipped_on_resume(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        out = fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertEqual((out.count("skip  "), len(rows_of(self.base))), (2, 2))

    def test_agent_timeout_is_recorded_and_fails_the_run(self) -> None:
        # The agent flushes a readiness line before sleeping; 5 s leaves room for slow interpreter start-up.
        code = "import time; print('ready', flush=True); time.sleep(60)"
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, "-c", code]
        self.cfg["tasks"][0]["timeout"] = 5
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        row = rows_of(self.base)[0]
        log = next((self.base / "results" / "logs").glob("*.agent.jsonl")).read_text()
        self.assertEqual(
            (row["agent_timed_out"], row["passed"], row["parse_ok"], "ready" in log), (True, False, False, True)
        )

    def test_setup_command_runs_and_is_logged(self) -> None:
        self.cfg["tasks"][0]["setup"] = "echo prepared"
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        logs = list((self.base / "results" / "logs").glob("*.setup.log"))
        self.assertIn("prepared", logs[0].read_text())

    def test_keep_and_work_dir_leave_the_workdir_in_place(self) -> None:
        work = fixtures.tmpdir(self) / "work"
        self.cfg["work_dir"] = str(work)
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], keep=True), self.cfg, self.base)
        self.assertTrue((work / "fizzbuzz__claude__fix__r1" / "fizzbuzz.py").exists())

    def test_leftover_workdir_and_snapshot_from_a_crashed_run_are_replaced(self) -> None:
        work = fixtures.tmpdir(self) / "work"
        tag = "fizzbuzz__claude__fix__r1"
        (work / tag).mkdir(parents=True)
        (work / tag / "stale.txt").write_text("old")
        (work / f".{tag}.protected").mkdir()
        self.cfg["work_dir"] = str(work)
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], keep=True), self.cfg, self.base)
        self.assertEqual((work / tag / "stale.txt").exists(), False)

    def test_the_baseline_git_dir_is_outside_the_workdir_and_removed_after_the_trial(self) -> None:
        work = fixtures.tmpdir(self) / "work"
        self.cfg["work_dir"] = str(work)
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], keep=True), self.cfg, self.base)
        kept = sorted(p.name for p in work.iterdir())
        self.assertEqual(
            (kept, (work / "fizzbuzz__claude__fix__r1" / ".git").exists()), (["fizzbuzz__claude__fix__r1"], False)
        )

    def test_report_is_written_unless_suppressed(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], no_report=False), self.cfg, self.base)
        self.assertTrue((self.base / "results" / "summary.md").exists())

    def test_task_without_hidden_tests_runs_its_own_test_command(self) -> None:
        task = self.cfg["tasks"][0]
        task.pop("hidden_tests")
        task["test"] = "true"
        fixtures.call_quiet(hb.cmd_run, args(only=["pi"]), self.cfg, self.base)
        self.assertTrue(rows_of(self.base)[0]["passed"])

    def test_protected_file_is_snapshotted_and_restored_to_its_original_content(self) -> None:
        original = (fixtures.ROOT / "tasks" / "fizzbuzz" / "repo" / "fizzbuzz.py").read_text()
        work = fixtures.tmpdir(self) / "work"
        self.cfg["work_dir"] = str(work)
        self.cfg["tasks"][0]["protected_paths"] = ["fizzbuzz.py"]
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"], keep=True), self.cfg, self.base)
        kept = work / "fizzbuzz__claude__fix__r1" / "fizzbuzz.py"
        self.assertEqual(kept.read_text(), original)  # the agent's "fix" was reverted, not deleted

    def test_protected_paths_are_restored_before_the_hidden_tests(self) -> None:
        self.cfg["tasks"][0]["protected_paths"] = ["fizzbuzz.py"]
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        self.assertFalse(rows_of(self.base)[0]["passed"])  # the "fix" was reverted by the restore


def trials_of(base: Path) -> list[dict[str, Any]]:
    (trials,) = (base / "results").glob("*/trials.jsonl")
    return fixtures.read_rows(trials)


def log_names(base: Path) -> set[str]:
    return {p.name.split("__r1.", 1)[1] for p in (base / "results" / "logs").iterdir() if "__r1." in p.name}


class GraderRunTests(RunTestCase):
    """`cmd_run` with the `graders` and `grade` task keys."""

    def setUp(self) -> None:
        super().setUp()
        self.task = self.cfg["tasks"][0]
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}]

    def run_one(self, **overrides: Any) -> dict[str, Any]:
        self.task.update(overrides)
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        return rows_of(self.base)[-1]

    def test_every_grader_result_is_written_to_the_trial(self) -> None:
        self.run_one(graders=[{"type": "hidden_tests"}, {"type": "command", "command": "true"}])
        (trial,) = trials_of(self.base)
        self.assertEqual(
            [(g["grader"], g["passed"], g["score"]) for g in trial["graders"]],
            [("hidden_tests", True, 1.0), ("command", True, 1.0)],
        )

    def test_logs_are_named_test_for_hidden_tests_and_by_index_and_type_for_the_rest(self) -> None:
        self.run_one(graders=[{"type": "hidden_tests"}, {"type": "command", "command": "echo graded"}])
        names = log_names(self.base)
        self.assertTrue({"test.log", "grader-1-command.log"} <= names, names)
        log = next((self.base / "results" / "logs").glob("*grader-1-command.log")).read_text()
        self.assertIn("graded", log)

    def test_mode_all_fails_when_one_grader_fails(self) -> None:
        row = self.run_one(graders=[{"type": "hidden_tests"}, {"type": "command", "command": "false"}])
        self.assertEqual(row["passed"], False)
        self.assertEqual(trials_of(self.base)[0]["status"], "failed")

    def test_mode_any_passes_when_one_grader_passes(self) -> None:
        self.cfg["matrix"] = [{"harness": "pi", "model": "p/nofix"}]  # the hidden tests fail
        row = self.run_one(
            graders=[{"type": "hidden_tests"}, {"type": "command", "command": "true"}], grade={"mode": "any"}
        )
        self.assertEqual(row["passed"], True)

    def test_weighted_mode_uses_the_threshold(self) -> None:
        self.cfg["matrix"] = [{"harness": "pi", "model": "p/nofix"}]
        graders = [{"type": "hidden_tests", "weight": 1}, {"type": "command", "command": "true", "weight": 3}]
        row = self.run_one(graders=graders, grade={"mode": "weighted", "threshold": 0.7})
        self.assertEqual(row["passed"], True)

    def test_a_grader_timeout_marks_the_trial_as_timed_out(self) -> None:
        sleeper = f"{fixtures.PY} -c 'import time; time.sleep(60)'"
        self.run_one(graders=[{"type": "command", "command": sleeper, "timeout": 1}])
        self.assertEqual(trials_of(self.base)[0]["status"], "timeout")

    def test_a_task_without_graders_writes_the_legacy_row_and_grader(self) -> None:
        row = self.run_one()
        (trial,) = trials_of(self.base)
        self.assertEqual(("graders" in row, "score" in row), (False, False))
        self.assertEqual([(g["grader"], g["score"]) for g in trial["graders"]], [("test", None)])

    def test_changes_are_captured_before_protected_paths_are_restored(self) -> None:
        graders = [{"type": "diff_constraints", "forbidden": ["fizzbuzz.py"]}]
        row = self.run_one(graders=graders, protected_paths=["fizzbuzz.py"])  # the agent edits a protected file
        self.assertEqual(row["passed"], False)

    def test_diff_constraints_pass_when_the_agent_stays_inside_the_limits(self) -> None:
        row = self.run_one(graders=[{"type": "diff_constraints", "allowed": ["fizzbuzz.py"], "max_changed_lines": 50}])
        self.assertEqual(row["passed"], True)

    def test_diff_constraints_fail_closed_when_the_agent_leaves_a_git_dir_behind(self) -> None:
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, "-c", "import os; os.mkdir('.git')"]
        row = self.run_one(graders=[{"type": "diff_constraints", "max_changed_lines": 1000}])
        self.assertEqual(row["passed"], False)

    def agent_writing_a_pyc(self) -> None:
        code = (
            "import os; os.makedirs('tests/__pycache__');"
            " open('tests/__pycache__/t.cpython-312.pyc', 'wb').write(b'\\0' * 9)"
        )
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, "-c", code]

    def test_bytecode_left_under_tests_passes_only_with_ignore_lines_and_the_source_glob(self) -> None:
        self.agent_writing_a_pyc()
        limits = {"type": "diff_constraints", "forbidden": ["tests/*.py"], "max_changed_lines": 50}
        ignored = self.run_one(graders=[{**limits, "ignore_lines": ["*/__pycache__/*"]}])
        plain = self.run_one(id="plain", graders=[limits])
        self.assertEqual((ignored["passed"], plain["passed"]), (True, False))

    def test_a_pyc_is_still_a_forbidden_path_when_the_glob_covers_it(self) -> None:
        self.agent_writing_a_pyc()
        row = self.run_one(graders=[{"type": "diff_constraints", "forbidden": ["tests/*"], "ignore_lines": ["*.pyc"]}])
        self.assertEqual(row["passed"], False)

    def test_a_secret_the_agent_writes_to_a_file_is_redacted_in_the_audit_diff(self) -> None:
        secret = "-".join(["abcdef", "fake", "12345"])  # built at runtime so secret scanners skip it
        self.cfg["harnesses"]["claude"]["env"]["MY_SECRET"] = secret
        code = "import os; open('leak.txt', 'w').write(os.environ['MY_SECRET'])"
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, "-c", code]
        self.run_one()
        diff = next((self.base / "results" / "logs").glob("*.diff")).read_text()
        self.assertEqual((secret in diff, "leak.txt" in diff, "***REDACTED***" in diff), (False, True, True))

    def test_an_exception_in_a_grader_fails_that_grader_and_the_matrix_goes_on(self) -> None:
        class Broken:
            name = "broken"
            version = "1"

            def __init__(self, cfg: Any) -> None:
                pass

            def grade(self, ctx: Any) -> Any:
                raise RuntimeError("boom")

        self.task["graders"] = [{"type": "broken"}]
        with mock.patch.dict(hb.GRADERS, {"broken": Broken}):
            out = fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (trial,) = trials_of(self.base)
        self.assertEqual((trial["graders"][0]["passed"], "RuntimeError" in out, "boom" in out), (False, True, False))

    def test_bad_grader_config_exits_naming_the_task_before_any_agent_runs(self) -> None:
        self.task["graders"] = [{"type": "command"}]
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertIn("task fizzbuzz", str(cm.exception.code))
        self.assertFalse((self.base / "results" / "results.jsonl").exists())


class AggregateTests(unittest.TestCase):
    def test_cost_source_litellm_without_data_gives_none_not_zero(self) -> None:
        table = hb.aggregate([fixtures.run_row(cost_computed=1.0)], "litellm")
        self.assertEqual(
            (table[0]["cost_total"], table[0]["cost_per_solved"], table[0]["cost_source"]), (None, None, "n/a")
        )

    def test_auto_prefers_litellm_then_computed_then_reported(self) -> None:
        rows = [
            fixtures.run_row(rep=1, cost_litellm=1.0, cost_computed=2.0, cost_reported=3.0),
            fixtures.run_row(rep=2, cost_computed=2.0, cost_reported=3.0),
            fixtures.run_row(rep=3, cost_reported=3.0),
        ]
        table = hb.aggregate(rows, "auto")
        self.assertEqual((table[0]["cost_total"], table[0]["cost_source"]), (6.0, "computed+litellm+reported"))

    def test_one_unknown_cost_makes_the_pair_unknown(self) -> None:
        table = hb.aggregate([fixtures.run_row(cost_computed=1.0), fixtures.run_row(rep=2)], "auto")
        self.assertIsNone(table[0]["cost_total"])

    def test_pairs_without_cost_sort_last(self) -> None:
        rows = [fixtures.run_row(harness="a"), fixtures.run_row(harness="b", cost_computed=1.0)]
        self.assertEqual([t["harness"] for t in hb.aggregate(rows, "auto")], ["b", "a"])


class CmdReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = fixtures.tmpdir(self)
        (self.base / "results").mkdir()

    def test_missing_results_file_exits_with_a_message(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_report, args(), {}, self.base)
        self.assertIn("no results at", str(cm.exception.code))

    def test_empty_results_file_exits_with_no_results(self) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl")
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_report, args(), {}, self.base)
        self.assertIn("no results in", str(cm.exception.code))

    def test_unknown_cost_source_exits_with_a_message_not_a_traceback(self) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl", fixtures.run_row())
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_report, args(cost_source="bogus"), {}, self.base)
        self.assertIn("auto, litellm, computed, reported", str(cm.exception.code))

    def test_pairs_without_token_data_get_a_warning(self) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl", fixtures.run_row(parse_ok=False))
        out = fixtures.call_quiet(hb.cmd_report, args(), {}, self.base)
        self.assertIn("no token data from the harness for: h/m (1)", out)

    def test_cost_source_comes_from_config_when_not_given(self) -> None:
        fixtures.write_rows(
            self.base / "results" / "results.jsonl", fixtures.run_row(cost_litellm=2.0, cost_computed=1.0)
        )
        fixtures.call_quiet(hb.cmd_report, args(), {"cost_source": "computed"}, self.base)
        self.assertIn("Cost source: `computed`", (self.base / "results" / "summary.md").read_text())

    def test_task_missing_for_a_pair_is_shown_as_dash(self) -> None:
        fixtures.write_rows(
            self.base / "results" / "results.jsonl", fixtures.run_row(task="a"), fixtures.run_row(task="b", harness="g")
        )
        fixtures.call_quiet(hb.cmd_report, args(), {}, self.base)
        self.assertIn("| a | 1/1 | - |", (self.base / "results" / "summary.md").read_text())


if __name__ == "__main__":
    unittest.main()
```

`tests/test_litellm.py`:

```python
"""Tests for LiteLLM access, `reconcile` and the LiteLLM-only wrapper, against a local scripted server."""

import json
import shutil
import sys
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from unittest import mock

import fixtures
import harness_bench as hb
import harness_bench_litellm as hbl

KEY = "k-test"
Reply = tuple[int, Any, dict[str, str]]


class ScriptedServer:
    """Localhost HTTP server answering GET <path> from a table; records (path, params, auth)."""

    def __init__(self) -> None:
        self.routes: dict[str, list[Reply]] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path, _, query = self.path.partition("?")
                outer.requests.append((path, query, self.headers.get("Authorization")))
                replies = outer.routes.get(path)
                status, body, headers = (
                    replies.pop(0) if replies and len(replies) > 1 else (replies or [(404, {}, {})])[0]
                )
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *a: Any) -> None:
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True).start()

    def route(self, path: str, *replies: Reply) -> None:
        self.routes[path] = list(replies)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def ok(body: Any) -> Reply:
    return 200, body, {}


class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ScriptedServer()
        self.addCleanup(self.server.close)


class LitellmGetTests(ServerTestCase):
    def test_sends_bearer_key_and_decodes_json(self) -> None:
        self.server.route("/p", ok({"a": 1}))
        data = hb.litellm_get(self.server.url, KEY, "/p", {"x": "1"})
        self.assertEqual((data, self.server.requests[0]), ({"a": 1}, ("/p", "x=1", f"Bearer {KEY}")))

    def test_redirects_are_refused(self) -> None:
        self.server.route("/p", (302, {}, {"Location": "http://example.invalid/steal"}))
        with self.assertRaises(urllib.error.HTTPError):
            hb.litellm_get(self.server.url, KEY, "/p", {})

    def test_plain_http_to_remote_host_exits(self) -> None:
        with self.assertRaises(SystemExit):
            hb.litellm_get("http://litellm.example.com", KEY, "/p", {})


class LitellmRowsTests(ServerTestCase):
    START = hb.datetime.fromtimestamp(1_700_000_000, hb.UTC)

    def rows(self) -> list[Any]:
        rows: list[Any] = hb.litellm_rows(self.server.url, KEY, self.START, self.START)
        return rows

    def test_follows_pages_until_total_pages(self) -> None:
        self.server.route("/spend/logs/v2", ok({"data": [1], "total_pages": 2}), ok({"data": [2], "total_pages": 2}))
        self.assertEqual(self.rows(), [1, 2])

    def test_stops_on_an_empty_page(self) -> None:
        self.server.route("/spend/logs/v2", ok({"data": [], "total_pages": 5}))
        self.assertEqual(self.rows(), [])

    def test_accepts_a_bare_list_response(self) -> None:
        self.server.route("/spend/logs/v2", ok([7, 8]))
        self.assertEqual(self.rows(), [7, 8])

    def test_falls_back_to_legacy_endpoint_when_v2_fails(self) -> None:
        self.server.route("/spend/logs", ok({"data": ["legacy"]}))
        with fixtures.quiet() as out:
            rows = self.rows()
        self.assertEqual((rows, "trying legacy" in out.getvalue()), (["legacy"], True))

    def test_legacy_endpoint_may_return_a_bare_list(self) -> None:
        self.server.route("/spend/logs", ok(["l1"]))
        with fixtures.quiet():
            self.assertEqual(self.rows(), ["l1"])


class ReconcileTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = fixtures.tmpdir(self)
        (self.base / "results").mkdir()
        self.cfg: dict[str, Any] = {"litellm": {"base_url": self.server.url, "api_key": KEY}}

    def reconcile(self) -> str:
        with fixtures.quiet() as out:
            hb.cmd_reconcile(mock.Mock(cost_source=None), self.cfg, self.base)
        return out.getvalue()

    def test_missing_litellm_section_exits(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            hb.cmd_reconcile(mock.Mock(), {}, self.base)
        self.assertIn("no 'litellm' section", str(cm.exception.code))

    def test_empty_key_exits(self) -> None:
        self.cfg["litellm"]["api_key"] = "${HB_SURELY_UNSET_VAR}"
        with self.assertRaises(SystemExit) as cm:
            hb.cmd_reconcile(mock.Mock(), self.cfg, self.base)
        self.assertIn("empty", str(cm.exception.code))

    def test_results_without_run_tags_exit(self) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl", fixtures.run_row(run_tag=None))
        with self.assertRaises(SystemExit) as cm:
            hb.cmd_reconcile(mock.Mock(), self.cfg, self.base)
        self.assertIn("no runs with run_tag", str(cm.exception.code))

    def test_matching_rows_add_summed_spend_and_token_counts(self) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl", fixtures.run_row())
        logs = [
            {"request_tags": ["tag-1"], "spend": 0.25, "prompt_tokens": 5, "completion_tokens": 1},
            {"request_tags": json.dumps(["tag-1"]), "spend": 0.5, "prompt_tokens": 6, "completion_tokens": 2},
            {"request_tags": ["other"], "spend": 9.0},
        ]
        self.server.route("/spend/logs/v2", ok({"data": logs, "total_pages": 1}))
        out = self.reconcile()
        row = fixtures.read_rows(self.base / "results" / "results.jsonl")[0]
        self.assertEqual(
            (row["cost_litellm"], row["litellm_requests"], row["litellm_prompt_tokens"], "1/1 runs matched" in out),
            (0.75, 2, 11, True),
        )

    def test_unmatched_tagged_run_gets_no_litellm_cost_and_a_hint(self) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl", fixtures.run_row())
        self.server.route("/spend/logs/v2", ok({"data": [], "total_pages": 1}))
        out = self.reconcile()
        self.assertEqual(
            ("cost_litellm" in fixtures.read_rows(self.base / "results" / "results.jsonl")[0], "Unmatched runs" in out),
            (False, True),
        )


class LoadConfigTests(unittest.TestCase):
    def write(self, cfg: dict[str, Any]) -> str:
        path = fixtures.tmpdir(self) / "c.json"
        path.write_text(json.dumps(cfg))
        return str(path)

    def litellm_config(self) -> dict[str, Any]:
        cfg = fixtures.fake_config({})
        cfg["litellm"] = {"base_url": "https://x", "api_key": "${K}"}
        return cfg

    def test_config_without_litellm_section_exits(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            hbl.load_config(self.write(fixtures.fake_config({})))
        self.assertIn("needs a 'litellm' section", str(cm.exception.code))

    def test_cursor_harness_and_its_matrix_entries_are_dropped(self) -> None:
        with fixtures.quiet() as out:
            cfg, _ = hbl.load_config(self.write(self.litellm_config()))
        self.assertEqual(
            ("cursor" in cfg["harnesses"], len(cfg["matrix"]), "ignored 1 matrix entries" in out.getvalue()),
            (False, 4, True),
        )

    def test_defaults_force_litellm_cost_and_separate_output_dir(self) -> None:
        with fixtures.quiet():
            cfg, _ = hbl.load_config(self.write(self.litellm_config()))
        self.assertEqual((cfg["cost_source"], cfg["output_dir"]), ("litellm", "results-litellm"))

    def test_matrix_entry_for_an_undefined_harness_exits(self) -> None:
        cfg = self.litellm_config()
        cfg["matrix"].append({"harness": "ghost", "model": "m"})
        with fixtures.quiet(), self.assertRaises(SystemExit) as cm:
            hbl.load_config(self.write(cfg))
        self.assertIn("undefined harnesses", str(cm.exception.code))


class WrapperMainTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = fixtures.tmpdir(self)
        cfg = fixtures.fake_config({})
        cfg["matrix"] = [{"harness": "claude", "model": "fix"}]
        cfg["litellm"] = {"base_url": self.server.url, "api_key": KEY}
        (self.base / "config.json").write_text(json.dumps(cfg))
        (self.base / "results-litellm").mkdir()

    def main(self, *argv: str) -> str:
        with mock.patch.object(
            sys, "argv", ["harness_bench_litellm.py", *argv, "--config", str(self.base / "config.json")]
        ):
            with fixtures.quiet() as out:
                hbl.main()
        return out.getvalue()

    def argv_run(self, *extra: str) -> str:
        return self.main("run", "--settle", "0", *extra)

    def test_dry_run_prints_commands_and_skips_reconcile(self) -> None:
        out = self.argv_run("--dry-run")
        self.assertEqual(("fake_agent.py" in out, "waiting" in out), (True, False))

    def test_report_command_reads_existing_results(self) -> None:
        fixtures.write_rows(self.base / "results-litellm" / "results.jsonl", fixtures.run_row(cost_litellm=1.0))
        self.assertIn("Cost source: `litellm`", self.main("report"))

    def test_reconcile_command_matches_tagged_runs(self) -> None:
        fixtures.write_rows(self.base / "results-litellm" / "results.jsonl", fixtures.run_row())
        self.server.route("/spend/logs/v2", ok({"data": [{"request_tags": ["tag-1"], "spend": 1.0}], "total_pages": 1}))
        self.assertIn("1/1 runs matched", self.main("reconcile"))

    def test_run_survives_a_failing_reconcile_and_prints_the_retry_command(self) -> None:
        tags = self.base / "tags.txt"
        cfg = json.loads((self.base / "config.json").read_text())
        cfg["harnesses"]["claude"]["env"]["FAKE_TAGS_FILE"] = str(tags)
        cfg["litellm"]["base_url"] = "http://127.0.0.1:1"  # connection refused
        (self.base / "config.json").write_text(json.dumps(cfg))
        shutil.copytree(fixtures.ROOT / "tasks" / "fizzbuzz", self.base / "tasks" / "fizzbuzz")
        out = self.argv_run("--allow-unsandboxed")
        self.assertEqual(("reconcile failed" in out, "Run later" in out), (True, True))


if __name__ == "__main__":
    unittest.main()
```

`tests/test_core_models.py`:

```python
"""Tests for the data contract models in `hbench.core`."""

import copy
import dataclasses
import pickle
import unittest
from types import MappingProxyType
from datetime import UTC, datetime
from typing import Any

import fixtures  # noqa: F401  (puts the repo root on sys.path)
from hbench.core.ids import new_run_id, sha256_json, trial_id
from hbench.core.models import (
    SCHEMA_VERSION,
    ContractError,
    Costs,
    CostSource,
    GraderResult,
    RunInfo,
    Target,
    TrialResult,
    TrialStatus,
    Usage,
)

T0 = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)


def target(**overrides: Any) -> Target:
    values: dict[str, Any] = {
        "harness": "claude",
        "harness_version": "1.2.3",
        "model": "sonnet",
        "params": {"effort": "high", "n": 2, "t": 0.5, "flag": True},
    }
    values.update(overrides)
    return Target(**values)


def usage(**overrides: Any) -> Usage:
    values: dict[str, Any] = {
        "input_tokens": 10,
        "output_tokens": 5,
        "parsed": True,
        "costs": Costs(price_table=0.5),
    }
    values.update(overrides)
    return Usage(**values)


def trial(**overrides: Any) -> TrialResult:
    values: dict[str, Any] = {
        "run_id": "r" * 32,
        "trial_id": "t" * 16,
        "task_id": "fizzbuzz",
        "suite_version": "abc",
        "target": target(),
        "rep": 1,
        "status": TrialStatus.PASSED,
        "graders": (GraderResult(grader="test", grader_version="1", passed=True, score=1.0, details_uri="logs/x"),),
        "usage": usage(),
        "latency_s": 1.5,
        "started_at": T0,
        "agent_exit_code": 0,
        "sandboxed": True,
        "trace_id": "bench-1",
        "artifact_uri": "logs/x",
    }
    values.update(overrides)
    return TrialResult(**values)


def run_info(**overrides: Any) -> RunInfo:
    values: dict[str, Any] = {
        "run_id": "r" * 32,
        "started_at": T0,
        "finished_at": None,
        "harness_bench_version": "0.0.0",
        "config_sha256": "c" * 64,
        "suite_version": "abc",
        "python": "3.12.0",
        "platform": "linux",
        "sandboxed": False,
        "targets": (target(),),
    }
    values.update(overrides)
    return RunInfo(**values)


class RoundTripTests(unittest.TestCase):
    def test_every_model_survives_to_dict_from_dict(self) -> None:
        cases: list[Any] = [
            target(),
            GraderResult(grader="g", grader_version="1", passed=False, score=None, timed_out=True),
            Costs(gateway=1.0, price_table=None, harness=0.0),
            usage(),
            trial(),
            trial(status=TrialStatus.TIMEOUT, agent_exit_code=None, trace_id=None, artifact_uri=None),
            run_info(),
            run_info(finished_at=T0),
        ]
        for obj in cases:
            with self.subTest(obj=type(obj).__name__):
                self.assertEqual(type(obj).from_dict(obj.to_dict()), obj)

    def test_to_dict_is_json_ready(self) -> None:
        d = trial().to_dict()
        self.assertEqual(
            (d["status"], d["started_at"], type(d["graders"]), d["usage"]["costs"]["price_table"]),
            ("passed", "2026-11-24T12:00:00+00:00", list, 0.5),
        )

    def test_usage_serialises_the_picked_cost(self) -> None:
        d = usage(costs=Costs(gateway=2.0, harness=1.0)).to_dict()
        self.assertEqual((d["cost_usd"], d["cost_source"]), (2.0, "gateway"))

    def test_usage_rejects_a_picked_cost_that_disagrees(self) -> None:
        d = usage().to_dict()
        d["cost_usd"] = 99.0
        with self.assertRaises(ContractError):
            Usage.from_dict(d)


class FromDictRejectionTests(unittest.TestCase):
    def test_extra_key_is_rejected_and_named(self) -> None:
        d = trial().to_dict()
        d["surprise"] = 1
        with self.assertRaises(ContractError) as cm:
            TrialResult.from_dict(d)
        self.assertIn("surprise", str(cm.exception))

    def test_missing_key_is_rejected_and_named(self) -> None:
        d = trial().to_dict()
        del d["task_id"]
        with self.assertRaises(ContractError) as cm:
            TrialResult.from_dict(d)
        self.assertIn("task_id", str(cm.exception))

    def test_other_major_version_is_rejected_and_minor_accepted(self) -> None:
        d = trial().to_dict()
        d["schema_version"] = "1.4.0"
        self.assertEqual(TrialResult.from_dict(d).schema_version, "1.4.0")
        d["schema_version"] = "2.0.0"
        with self.assertRaises(ContractError):
            TrialResult.from_dict(d)

    def test_malformed_version_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            trial(schema_version="one")

    def test_unknown_enum_value_is_a_contract_error(self) -> None:
        d = trial().to_dict()
        d["status"] = "exploded"
        with self.assertRaises(ContractError):
            TrialResult.from_dict(d)

    def test_wrong_type_is_a_contract_error(self) -> None:
        d = trial().to_dict()
        d["rep"] = "one"
        with self.assertRaises(ContractError):
            TrialResult.from_dict(d)

    def test_list_field_must_be_a_list(self) -> None:
        d = trial().to_dict()
        d["graders"] = "nope"
        with self.assertRaises(ContractError):
            TrialResult.from_dict(d)

    def test_bad_timestamp_is_a_contract_error(self) -> None:
        for bad in ("yesterday", 12345):
            d = trial().to_dict()
            d["started_at"] = bad
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                TrialResult.from_dict(d)


class TypeCheckTests(unittest.TestCase):
    def test_scalar_fields_are_type_checked(self) -> None:
        bad: list[tuple[str, Any]] = [
            ("task_id", 5),
            ("sandboxed", 1),
            ("rep", True),
            ("rep", 1.0),
            ("latency_s", True),
            ("latency_s", "1"),
            ("status", "passed"),
            ("started_at", "2026-11-24T12:00:00+00:00"),
            ("graders", [GraderResult(grader="g", grader_version="1", passed=True)]),
            ("target", {"harness": "h"}),
            ("agent_exit_code", "0"),
            ("trace_id", 7),
        ]
        for name, value in bad:
            with self.subTest(field=name, value=value), self.assertRaises(ContractError):
                trial(**{name: value})

    def test_nested_and_container_types_are_checked(self) -> None:
        with self.assertRaises(ContractError):
            target(model=["x"])
        with self.assertRaises(ContractError):
            target(params={"k": [1]})
        with self.assertRaises(ContractError):
            target(params={1: "v"})
        with self.assertRaises(ContractError):
            usage(parsed="yes")
        with self.assertRaises(ContractError):
            usage(input_tokens=1.5)
        with self.assertRaises(ContractError):
            usage(input_tokens=True)
        with self.assertRaises(ContractError):
            run_info(targets=[target()])

    def test_from_dict_applies_the_same_checks(self) -> None:
        d = trial().to_dict()
        d["task_id"] = 5
        with self.assertRaises(ContractError):
            TrialResult.from_dict(d)

    def test_non_finite_and_bool_costs_are_rejected(self) -> None:
        bad_values: list[Any] = [float("nan"), float("inf"), True, "1"]
        for bad in bad_values:
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                Costs(**{"harness": bad})

    def test_tuple_elements_are_type_checked(self) -> None:
        with self.assertRaises(ContractError):
            trial(graders=("x",))
        with self.assertRaises(ContractError):
            run_info(targets=(1,))

    def test_non_finite_latency_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            trial(latency_s=float("nan"))

    def test_missing_schema_version_is_rejected_although_it_has_a_default(self) -> None:
        for model in (trial(), run_info()):
            d = model.to_dict()
            del d["schema_version"]
            with self.subTest(model=type(model).__name__), self.assertRaises(ContractError) as cm:
                type(model).from_dict(d)
            self.assertIn("schema_version", str(cm.exception))


class TargetParamsTests(unittest.TestCase):
    def test_models_survive_deepcopy_and_pickle(self) -> None:
        for model in (target(), trial(), run_info()):
            for clone in (copy.deepcopy(model), pickle.loads(pickle.dumps(model))):
                with self.subTest(model=type(model).__name__):
                    self.assertEqual((clone, hash(clone)), (model, hash(model)))
                    self.assertIsNot(clone, model)

    def test_params_are_copied_and_read_only(self) -> None:
        given = {"a": 1}
        t = target(params=given)
        given["a"] = 2
        self.assertEqual((t.params["a"], isinstance(t.params, MappingProxyType)), (1, True))

    def test_models_are_hashable_and_equal_targets_hash_equal(self) -> None:
        self.assertEqual(hash(target(params={"a": 1, "b": 2})), hash(target(params={"b": 2, "a": 1})))
        self.assertEqual(len({trial(), trial()}), 1)
        self.assertNotEqual(hash(target(params={"a": 1})), hash(target(params={"a": 2})))


class ValidationTests(unittest.TestCase):
    def test_costs_pick_prefers_gateway_then_price_table_then_harness(self) -> None:
        self.assertEqual(Costs(gateway=None, price_table=0.5, harness=0.7).pick(), (0.5, CostSource.PRICE_TABLE))
        self.assertEqual(Costs(gateway=0.1, price_table=0.5, harness=0.7).pick(), (0.1, CostSource.GATEWAY))
        self.assertEqual(Costs(harness=0.7).pick(), (0.7, CostSource.HARNESS))

    def test_costs_pick_unknown_is_none_not_zero(self) -> None:
        self.assertEqual(Costs().pick(), (None, CostSource.UNKNOWN))

    def test_costs_pick_can_force_a_source(self) -> None:
        costs = Costs(gateway=None, price_table=0.5)
        self.assertEqual(costs.pick(CostSource.GATEWAY), (None, CostSource.UNKNOWN))
        self.assertEqual(costs.pick(CostSource.PRICE_TABLE), (0.5, CostSource.PRICE_TABLE))
        self.assertEqual(costs.pick(CostSource.UNKNOWN), (None, CostSource.UNKNOWN))

    def test_zero_cost_is_a_known_cost(self) -> None:
        self.assertEqual(Costs(gateway=0.0, harness=1.0).pick(), (0.0, CostSource.GATEWAY))

    def test_negative_cost_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            Costs(harness=-1)

    def test_negative_token_count_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            usage(cache_read_tokens=-1)

    def test_usage_totals_and_picked_cost(self) -> None:
        u = usage(reasoning_tokens=3, cache_read_tokens=4, cache_write_tokens=5, costs=Costs(gateway=2.0))
        self.assertEqual((u.total_tokens, u.cost_usd, u.cost_source), (27, 2.0, CostSource.GATEWAY))

    def test_trial_id_is_stable_and_depends_on_rep(self) -> None:
        a = trial_id("run", "task", "h@v:m", 1)
        self.assertEqual(a, trial_id("run", "task", "h@v:m", 1))
        self.assertNotEqual(a, trial_id("run", "task", "h@v:m", 2))
        self.assertEqual(len(a), 16)

    def test_run_ids_are_unique_hex(self) -> None:
        a, b = new_run_id(), new_run_id()
        self.assertEqual((len(a), a == b, int(a, 16) >= 0), (32, False, True))

    def test_sha256_json_ignores_key_order(self) -> None:
        self.assertEqual(sha256_json({"a": 1, "b": 2}), sha256_json({"b": 2, "a": 1}))
        self.assertEqual(len(sha256_json([])), 64)

    def test_target_id(self) -> None:
        self.assertEqual(target().target_id, "claude@1.2.3:sonnet")

    def test_naive_datetime_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            trial(started_at=datetime(2026, 1, 1))
        with self.assertRaises(ContractError):
            run_info(finished_at=datetime(2026, 1, 1))

    def test_grader_score_must_be_in_unit_interval(self) -> None:
        for bad in (1.5, -0.1):
            with self.assertRaises(ContractError):
                GraderResult(grader="g", grader_version="1", passed=True, score=bad)

    def test_trial_rep_and_latency_bounds(self) -> None:
        with self.assertRaises(ContractError):
            trial(rep=0)
        with self.assertRaises(ContractError):
            trial(latency_s=-0.1)

    def test_passed_property_follows_status(self) -> None:
        self.assertEqual((trial().passed, trial(status=TrialStatus.FAILED).passed), (True, False))

    def test_models_are_frozen(self) -> None:
        field = "rep"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(trial(), field, 2)

    def test_schema_version_default(self) -> None:
        self.assertEqual(trial().schema_version, SCHEMA_VERSION)


class ToolCallsTests(unittest.TestCase):
    def test_tool_calls_default_to_unknown_and_round_trip(self) -> None:
        self.assertIsNone(usage().tool_calls)
        for calls in (None, 0, 7):
            self.assertEqual(Usage.from_dict(usage(tool_calls=calls).to_dict()).tool_calls, calls)

    def test_a_1_0_0_trial_without_tool_calls_still_loads(self) -> None:
        d = trial().to_dict()
        del d["usage"]["tool_calls"]
        d["schema_version"] = "1.0.0"
        loaded = TrialResult.from_dict(d)
        self.assertEqual((loaded.usage.tool_calls, loaded.schema_version), (None, "1.0.0"))

    def test_the_current_contract_version_is_1_1_0(self) -> None:
        self.assertEqual((SCHEMA_VERSION, trial().schema_version), ("1.1.0", "1.1.0"))

    def test_tool_calls_must_be_a_whole_number_of_at_least_zero(self) -> None:
        for bad in (-1, 1.5, True, "3"):
            with self.assertRaises(ContractError, msg=repr(bad)):
                usage(tool_calls=bad)


if __name__ == "__main__":
    unittest.main()
```

`tests/test_core_schema.py`:

```python
"""Tests for the hand-written JSON Schema and its small validator."""

import copy
import unittest
from dataclasses import fields
from collections.abc import Mapping
from typing import Any

import fixtures  # noqa: F401  (puts the repo root on sys.path)
from hbench.core import models
from hbench.core.schema import RUN_SCHEMA, TRIAL_SCHEMA, schema_document, validate
from test_core_models import run_info, trial


class ValidateContractTests(unittest.TestCase):
    def test_a_valid_trial_and_run_have_no_errors(self) -> None:
        self.assertEqual(validate(trial().to_dict(), TRIAL_SCHEMA), [])
        self.assertEqual(validate(run_info().to_dict(), RUN_SCHEMA), [])
        self.assertEqual(validate(run_info(finished_at=trial().started_at).to_dict(), RUN_SCHEMA), [])

    def test_wrong_type_names_the_path(self) -> None:
        d = trial().to_dict()
        d["usage"]["input_tokens"] = "ten"
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.usage.input_tokens: expected integer"])

    def test_missing_required_property(self) -> None:
        d = trial().to_dict()
        del d["usage"]["costs"]
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.usage: missing required property 'costs'"])

    def test_extra_property_is_rejected(self) -> None:
        d = trial().to_dict()
        d["target"]["surprise"] = 1
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.target.surprise: unexpected property"])

    def test_bad_enum_value(self) -> None:
        d = trial().to_dict()
        d["status"] = "exploded"
        self.assertEqual(len(validate(d, TRIAL_SCHEMA)), 1)
        self.assertIn("$.status: expected one of", validate(d, TRIAL_SCHEMA)[0])

    def test_rep_zero_violates_the_minimum(self) -> None:
        d = trial().to_dict()
        d["rep"] = 0
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.rep: must be >= 1"])

    def test_score_above_one_violates_the_maximum(self) -> None:
        d = trial().to_dict()
        d["graders"][0]["score"] = 1.5
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.graders[0].score: must be <= 1"])

    def test_null_is_accepted_only_where_the_type_allows_it(self) -> None:
        d = trial(agent_exit_code=None).to_dict()
        d["task_id"] = None
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.task_id: expected string"])

    def test_bool_is_not_an_integer_or_a_number(self) -> None:
        d = trial().to_dict()
        d["rep"] = True
        d["latency_s"] = False
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.rep: expected integer", "$.latency_s: expected number"])

    def test_non_finite_numbers_are_not_numbers(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertEqual(validate(bad, {"type": "number"}), ["$: expected number"])
        self.assertEqual(validate(10**400, {"type": "integer"}), [])

    def test_non_dict_and_non_list_instances(self) -> None:
        self.assertEqual(validate([], TRIAL_SCHEMA), ["$: expected object"])
        d = trial().to_dict()
        d["graders"] = {}
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.graders: expected array"])

    def test_tool_calls_may_be_missing_null_or_a_count(self) -> None:
        d = trial().to_dict()
        for ok in (None, 0, 5):
            d["usage"]["tool_calls"] = ok
            self.assertEqual(validate(d, TRIAL_SCHEMA), [], repr(ok))
        del d["usage"]["tool_calls"]
        self.assertEqual(validate(d, TRIAL_SCHEMA), [])
        d["usage"]["tool_calls"] = -1
        self.assertEqual(validate(d, TRIAL_SCHEMA), ["$.usage.tool_calls: must be >= 0"])

    def test_params_accept_free_keys(self) -> None:
        d = trial().to_dict()
        d["target"]["params"] = {"anything": 1}
        self.assertEqual(validate(d, TRIAL_SCHEMA), [])

    def test_schema_document_holds_both_schemas_with_ids(self) -> None:
        doc = schema_document()
        self.assertEqual((doc["trial"], doc["run"]), (TRIAL_SCHEMA, RUN_SCHEMA))
        self.assertTrue(TRIAL_SCHEMA["$id"].endswith("/trial/1.1.0"))
        self.assertTrue(RUN_SCHEMA["$id"].endswith("/run/1.1.0"))


class ValidatorKeywordTests(unittest.TestCase):
    def test_unknown_keyword_raises(self) -> None:
        with self.assertRaises(ValueError) as cm:
            validate(1, {"type": "integer", "pattern": "x"})
        self.assertIn("pattern", str(cm.exception))

    def test_ref_resolves_against_defs(self) -> None:
        schema = {"$defs": {"n": {"type": "integer", "minimum": 2}}, "type": "array", "items": {"$ref": "#/$defs/n"}}
        self.assertEqual(validate([2, 1], schema), ["$[1]: must be >= 2"])

    def test_unresolvable_ref_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate(1, {"$ref": "#/$defs/missing"})
        with self.assertRaises(ValueError):
            validate(1, {"$ref": "http://elsewhere/x"})

    def test_type_list_with_null(self) -> None:
        schema = {"type": ["string", "null"]}
        self.assertEqual(validate(None, schema), [])
        self.assertEqual(validate("a", schema), [])
        self.assertEqual(validate(1, schema), ["$: expected string or null"])

    def test_unknown_type_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate(1, {"type": "color"})


def _node(schema: Mapping[str, Any], name: str | None) -> Mapping[str, Any]:
    node: Mapping[str, Any] = schema if name is None else schema["$defs"][name]
    return node


class SchemaMatchesModelsTests(unittest.TestCase):
    """The schema is hand written: fail when a dataclass field is added or removed on one side only."""

    CASES: tuple[tuple[type[models._Model], Mapping[str, Any], str | None], ...] = (
        (models.TrialResult, TRIAL_SCHEMA, None),
        (models.Target, TRIAL_SCHEMA, "target"),
        (models.GraderResult, TRIAL_SCHEMA, "grader_result"),
        (models.Usage, TRIAL_SCHEMA, "usage"),
        (models.Costs, TRIAL_SCHEMA, "costs"),
        (models.RunInfo, RUN_SCHEMA, None),
        (models.Target, RUN_SCHEMA, "target"),
    )

    def test_properties_and_required_keys_match_the_dataclasses(self) -> None:
        for cls, schema, name in self.CASES:
            with self.subTest(model=cls.__name__, node=name):
                node = _node(schema, name)
                expected = {f.name for f in fields(cls)} | set(cls.DERIVED_KEYS)
                self.assertEqual(set(node["properties"]), expected)
                self.assertEqual(set(node["required"]), set(cls.required_keys()))
                self.assertIs(node["additionalProperties"], False)

    def test_schema_is_not_mutated_by_validation(self) -> None:
        before = copy.deepcopy(TRIAL_SCHEMA)
        validate(trial().to_dict(), TRIAL_SCHEMA)
        self.assertEqual(TRIAL_SCHEMA, before)


if __name__ == "__main__":
    unittest.main()
```

`tests/test_contract_output.py`:

```python
"""Tests for the contract output: `trial_from_row`, `RunWriter`, `apply_gateway_costs`, and `run`/`reconcile` files."""

import json
import os
import shutil
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import fixtures
import harness_bench as hb
from hbench.core.convert import trial_from_row, trial_from_row_lenient, trials_from_rows
from hbench.core.models import ContractError, Costs, CostSource, RunInfo, TrialResult, TrialStatus
from hbench.core.schema import RUN_SCHEMA, TRIAL_SCHEMA, validate
from hbench.results import RunWriter, apply_gateway_costs
from test_core_models import run_info, trial
from test_litellm import ScriptedServer, ok
from test_run import RunTestCase, args

API_KEY = "sk-test-agent-key-0123456789"
PROMPT = "PROMPT-CANARY-do-not-store"
MINIMAL_ROW: dict[str, Any] = {
    "task": "t1",
    "harness": "h",
    "model": "m",
    "rep": 1,
    "passed": True,
    "input": 100,
    "output": 10,
    "cache_read": 5,
    "cache_write": 2,
    "turns": 3,
    "wall_s": 1.5,
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return fixtures.read_rows(path)


def convert(row: dict[str, Any], **kw: Any) -> TrialResult:
    return trial_from_row(row, run_id="r" * 32, suite_version="s1", **kw)


class TrialFromRowTests(unittest.TestCase):
    def test_minimal_pre_phase_1_row_converts(self) -> None:
        t = convert(MINIMAL_ROW)
        self.assertEqual(
            (t.status, t.target.target_id, t.usage.total_tokens, t.usage.parsed, t.sandboxed, t.usage.cost_usd),
            (TrialStatus.PASSED, "h@unknown:m", 117, True, False, None),
        )
        self.assertEqual(
            (t.usage.cost_source, t.started_at, t.trace_id, t.artifact_uri),
            (CostSource.UNKNOWN, datetime.fromtimestamp(0, UTC), None, None),
        )
        self.assertEqual(validate(t.to_dict(), TRIAL_SCHEMA), [])

    def test_full_row_maps_every_field(self) -> None:
        row = dict(
            MINIMAL_ROW,
            ts="2026-11-24T10:00:00+00:00",
            reasoning=7,
            parse_ok=False,
            cost_litellm=0.3,
            cost_computed=0.2,
            cost_reported=0.1,
            run_tag="bench-20261124100000-fizz__claude__a-b__r1",
            agent_rc=2,
            sandboxed=True,
            passed=False,
        )
        t = convert(row, harness_version="9.9")
        self.assertEqual(
            (t.status, t.usage.reasoning_tokens, t.usage.parsed, t.usage.costs, t.agent_exit_code, t.sandboxed),
            (TrialStatus.FAILED, 7, False, Costs(gateway=0.3, price_table=0.2, harness=0.1), 2, True),
        )
        self.assertEqual(
            (
                t.trace_id,
                t.artifact_uri,
                t.target.harness_version,
                t.started_at.isoformat(),
                t.latency_s,
                t.suite_version,
            ),
            (row["run_tag"], "logs/fizz__claude__a-b__r1", "9.9", "2026-11-24T10:00:00+00:00", 1.5, "s1"),
        )

    def test_trial_from_row_stays_strict_about_rep(self) -> None:
        with self.assertRaises(ContractError):
            convert(dict(MINIMAL_ROW, rep=0))

    def test_report_path_reads_a_hand_written_rep_zero_as_rep_one(self) -> None:
        (t,) = trials_from_rows([dict(MINIMAL_ROW, rep=0)])
        self.assertEqual(t.rep, 1)

    def test_bad_numbers_from_a_parser_are_contract_errors(self) -> None:
        for key, bad in (("input", -1), ("input", float("nan")), ("output", float("inf")), ("cache_read", True)):
            with self.subTest(key=key, bad=bad), self.assertRaises(ContractError):
                convert(dict(MINIMAL_ROW, **{key: bad}))
        for key, bad in (("cost_reported", True), ("cost_computed", float("nan")), ("cost_litellm", -0.5)):
            with self.subTest(key=key, bad=bad), self.assertRaises(ContractError):
                convert(dict(MINIMAL_ROW, **{key: bad}))

    def test_lenient_conversion_keeps_the_trial_but_drops_the_bad_numbers(self) -> None:
        warnings: list[str] = []
        row = dict(MINIMAL_ROW, cost_reported=True, input=-4)
        t = trial_from_row_lenient(row, run_id="r" * 32, suite_version="s1", warn=warnings.append)
        self.assertEqual(
            (t.usage.parsed, t.usage.total_tokens, t.usage.cost_usd, t.passed, t.latency_s, len(warnings)),
            (False, 0, None, True, 1.5, 2),
        )
        self.assertTrue(all("t1" in w for w in warnings))
        self.assertTrue(any("input" in w for w in warnings) and any("cost_reported" in w for w in warnings))

    def test_tool_calls_come_from_the_row_and_default_to_unknown(self) -> None:
        self.assertEqual(
            [
                convert(dict(MINIMAL_ROW, **extra)).usage.tool_calls
                for extra in ({}, {"tool_calls": None}, {"tool_calls": 3})
            ],
            [None, None, 3],
        )

    def test_a_bad_tool_call_count_becomes_unknown_and_keeps_everything_else(self) -> None:
        for bad in (-1, True, 1.5, "2", float("nan")):
            warnings: list[str] = []
            t = trial_from_row_lenient(
                dict(MINIMAL_ROW, tool_calls=bad), run_id="r" * 32, suite_version="s1", warn=warnings.append
            )
            self.assertEqual((t.usage.tool_calls, t.usage.parsed, t.usage.total_tokens), (None, True, 117), repr(bad))
            self.assertTrue(len(warnings) == 1 and "tool_calls" in warnings[0], repr(bad))

    def test_a_bad_tool_call_count_is_dropped_silently_without_a_warn_callback(self) -> None:
        t = trial_from_row_lenient(dict(MINIMAL_ROW, tool_calls=-1), run_id="r" * 32, suite_version="s1")
        self.assertIsNone(t.usage.tool_calls)

    def test_a_valid_gateway_cost_survives_a_bad_harness_cost(self) -> None:
        warnings: list[str] = []
        row = dict(MINIMAL_ROW, cost_litellm=0.5, cost_computed=0.2, cost_reported=True)
        t = trial_from_row_lenient(row, run_id="r" * 32, suite_version="s1", warn=warnings.append)
        self.assertEqual(
            (t.usage.costs, t.usage.parsed, t.usage.total_tokens, len(warnings)),
            (Costs(gateway=0.5, price_table=0.2, harness=None), True, 117, 1),
        )
        self.assertIn("cost_reported", warnings[0])

    def test_a_bad_count_does_not_wipe_valid_costs(self) -> None:
        row = dict(MINIMAL_ROW, cost_litellm=0.5, cost_computed=2.0, output=float("nan"))
        t = trial_from_row_lenient(row, run_id="r" * 32, suite_version="s1")
        self.assertEqual((t.usage.costs.gateway, t.usage.parsed, t.usage.total_tokens), (0.5, False, 0))

    def test_a_bad_count_drops_the_price_table_cost_but_keeps_the_others(self) -> None:
        warnings: list[str] = []
        row = dict(MINIMAL_ROW, input=-1, cost_computed=1.0, cost_litellm=0.5, cost_reported=0.25)
        t = trial_from_row_lenient(row, run_id="r" * 32, suite_version="s1", warn=warnings.append)
        self.assertEqual(t.usage.costs, Costs(gateway=0.5, price_table=None, harness=0.25))
        self.assertTrue(any("price-table cost dropped" in w for w in warnings))

    def test_a_good_price_table_cost_is_kept_when_only_a_cost_is_bad(self) -> None:
        row = dict(MINIMAL_ROW, cost_computed=1.0, cost_reported=True)
        t = trial_from_row_lenient(row, run_id="r" * 32, suite_version="s1")
        self.assertEqual(t.usage.costs.price_table, 1.0)

    def test_a_long_non_numeric_value_is_not_echoed(self) -> None:
        secret = "SECRET-" + "x" * 300
        bad_value: Any = secret
        warnings: list[str] = []
        trial_from_row_lenient(
            dict(MINIMAL_ROW, input=secret, cost_reported=secret),
            run_id="r" * 32,
            suite_version="s1",
            warn=warnings.append,
        )
        with self.assertRaises(ContractError) as cm:
            convert(dict(MINIMAL_ROW, cache_read=secret))
        with self.assertRaises(ContractError) as cm2:
            Costs(**{"harness": bad_value})
        self.assertEqual(len(warnings), 2)
        self.assertFalse(any("SECRET" in m for m in [*warnings, str(cm.exception), str(cm2.exception)]))

    def test_every_bad_field_gets_its_own_warning(self) -> None:
        warnings: list[str] = []
        row = dict(MINIMAL_ROW, cost_litellm=-1.0, cost_computed=float("inf"), turns=True)
        trial_from_row_lenient(row, run_id="r" * 32, suite_version="s1", warn=warnings.append)
        self.assertEqual(len(warnings), 3)

    def test_integral_floats_are_counts_but_fractions_are_not(self) -> None:
        self.assertEqual(convert(dict(MINIMAL_ROW, input=2.0)).usage.input_tokens, 2)
        with self.assertRaises(ContractError):
            convert(dict(MINIMAL_ROW, input=1.9))
        t = trial_from_row_lenient(dict(MINIMAL_ROW, input=1.9), run_id="r" * 32, suite_version="s1")
        self.assertEqual((t.usage.parsed, t.usage.total_tokens), (False, 0))

    def test_report_path_warns_about_sanitized_rows_when_asked(self) -> None:
        warnings: list[str] = []
        trials_from_rows([dict(MINIMAL_ROW, cost_computed=float("nan"))], warn=warnings.append)
        self.assertEqual(len(warnings), 1)

    def test_report_path_survives_bad_rows(self) -> None:
        (t,) = trials_from_rows([dict(MINIMAL_ROW, cost_computed=float("nan"))])
        self.assertEqual((t.usage.parsed, t.usage.cost_usd), (True, None))

    def test_trial_id_depends_on_the_row_identity(self) -> None:
        self.assertNotEqual(convert(MINIMAL_ROW).trial_id, convert(dict(MINIMAL_ROW, rep=2)).trial_id)

    def test_timeout_mapping(self) -> None:
        failed = dict(MINIMAL_ROW, passed=False)
        self.assertEqual(convert(dict(failed, agent_timed_out=True)).status, TrialStatus.TIMEOUT)
        self.assertEqual(convert(failed, test_timed_out=True).status, TrialStatus.TIMEOUT)
        self.assertEqual(convert(failed).status, TrialStatus.FAILED)
        self.assertEqual(convert(dict(MINIMAL_ROW, agent_timed_out=True)).status, TrialStatus.PASSED)

    def test_test_timeout_is_recorded_on_the_grader(self) -> None:
        t = convert(dict(MINIMAL_ROW, passed=False), test_timed_out=True)
        self.assertEqual((t.graders[0].passed, t.graders[0].timed_out), (False, True))

    def test_started_at_falls_back_from_ts_to_t_start(self) -> None:
        row = dict(MINIMAL_ROW, ts="not a time", t_start=1_700_000_000.0)
        self.assertEqual(convert(row).started_at, datetime.fromtimestamp(1_700_000_000, UTC))

    def test_naive_ts_is_taken_as_utc(self) -> None:
        self.assertEqual(
            convert(dict(MINIMAL_ROW, ts="2026-01-02T03:04:05")).started_at, datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        )

    def test_run_tag_without_the_bench_prefix_gives_no_artifact_uri(self) -> None:
        t = convert(dict(MINIMAL_ROW, run_tag="tag-1"))
        self.assertEqual((t.trace_id, t.artifact_uri), ("tag-1", None))


class RunWriterTests(unittest.TestCase):
    def test_writes_run_json_at_construction_and_appends_trials(self) -> None:
        out = fixtures.tmpdir(self)
        info = run_info()
        writer = RunWriter(out, info)
        run_dir = out / info.run_id
        self.assertEqual(RunInfo.from_dict(json.loads((run_dir / "run.json").read_text())), info)
        writer.append(trial())
        writer.append(trial(rep=2))
        lines = read_jsonl(run_dir / "trials.jsonl")
        self.assertEqual([line["rep"] for line in lines], [1, 2])

    def test_close_records_the_finish_time(self) -> None:
        out = fixtures.tmpdir(self)
        info = run_info()
        writer = RunWriter(out, info)
        finished = datetime(2026, 11, 24, 13, 0, tzinfo=UTC)
        writer.close(finished)
        stored = RunInfo.from_dict(json.loads((out / info.run_id / "run.json").read_text()))
        self.assertEqual(stored.finished_at, finished)
        self.assertEqual((out / info.run_id / "trials.jsonl").read_text(), "")


class ApplyGatewayCostsTests(unittest.TestCase):
    def make(self, out: Path, run_id: str, *traces: str | None) -> Path:
        writer = RunWriter(out, run_info(run_id=run_id))
        for rep, trace in enumerate(traces, 1):
            writer.append(trial(run_id=run_id, trace_id=trace, rep=rep))
        return out / run_id / "trials.jsonl"

    def test_sets_gateway_cost_on_matching_trials_across_run_dirs(self) -> None:
        out = fixtures.tmpdir(self)
        first = self.make(out, "a" * 32, "t1", "t2", None)
        second = self.make(out, "b" * 32, "t1")
        updated = apply_gateway_costs(out, {"t1": 0.25})
        costs = [[line["usage"]["costs"]["gateway"] for line in read_jsonl(p)] for p in (first, second)]
        self.assertEqual((updated, costs), (2, [[0.25, None, None], [0.25]]))
        self.assertEqual(read_jsonl(first)[0]["usage"]["cost_source"], "gateway")

    def test_other_cost_fields_are_kept(self) -> None:
        out = fixtures.tmpdir(self)
        path = self.make(out, "a" * 32, "t1")
        apply_gateway_costs(out, {"t1": 0.25})
        self.assertEqual(read_jsonl(path)[0]["usage"]["costs"], {"gateway": 0.25, "price_table": 0.5, "harness": None})

    def test_output_without_run_dirs_updates_nothing(self) -> None:
        out = fixtures.tmpdir(self)
        (out / "logs").mkdir()
        self.assertEqual(apply_gateway_costs(out, {"t1": 1.0}), 0)

    def test_no_match_leaves_files_untouched(self) -> None:
        out = fixtures.tmpdir(self)
        path = self.make(out, "a" * 32, "t1")
        before = path.read_text()
        self.assertEqual(apply_gateway_costs(out, {"zzz": 1.0}), 0)
        self.assertEqual(path.read_text(), before)

    def test_a_foreign_or_corrupt_file_is_skipped_with_a_warning_and_others_are_updated(self) -> None:
        out = fixtures.tmpdir(self)
        bad = self.make(out, "a" * 32, "t1")
        bad.write_text(bad.read_text() + '{"nope": 1}\n')
        broken = self.make(out, "b" * 32, "t1")
        broken.write_text("not json\n")
        good = self.make(out, "c" * 32, "t1")
        before = (bad.read_text(), broken.read_text())
        with fixtures.quiet() as buf:
            updated = apply_gateway_costs(out, {"t1": 1.0})
        self.assertEqual(updated, 1)
        self.assertEqual((bad.read_text(), broken.read_text()), before)
        self.assertEqual(read_jsonl(good)[0]["usage"]["costs"]["gateway"], 1.0)
        self.assertIn(str(bad), buf.getvalue())
        self.assertIn(str(broken), buf.getvalue())

    def test_a_bad_spend_skips_only_that_tag_with_a_warning(self) -> None:
        out = fixtures.tmpdir(self)
        path = self.make(out, "a" * 32, "t1", "t2", "t3")
        with fixtures.quiet() as buf:
            updated = apply_gateway_costs(out, {"t1": float("nan"), "t2": -2.0, "t3": 0.25})
        gateways = [line["usage"]["costs"]["gateway"] for line in read_jsonl(path)]
        self.assertEqual((updated, gateways), (1, [None, None, 0.25]))
        self.assertIn("t1", buf.getvalue())
        self.assertIn("t2", buf.getvalue())

    def test_a_zero_gateway_cost_is_known_and_applied(self) -> None:
        out = fixtures.tmpdir(self)
        path = self.make(out, "a" * 32, "t1")
        self.assertEqual(apply_gateway_costs(out, {"t1": 0.0}), 1)
        usage = read_jsonl(path)[0]["usage"]
        self.assertEqual((usage["costs"]["gateway"], usage["cost_usd"], usage["cost_source"]), (0.0, 0.0, "gateway"))

    def test_the_count_is_trials_not_files(self) -> None:
        out = fixtures.tmpdir(self)
        self.make(out, "a" * 32, "t1", "t1", "t2")
        self.assertEqual(apply_gateway_costs(out, {"t1": 0.5}), 2)

    def test_a_failed_replace_leaves_no_temp_file_and_the_original(self) -> None:
        out = fixtures.tmpdir(self)
        path = self.make(out, "a" * 32, "t1")
        before = path.read_text()
        with mock.patch("os.replace", side_effect=OSError("disk")), self.assertRaises(OSError):
            apply_gateway_costs(out, {"t1": 1.0})
        self.assertEqual(
            (path.read_text(), sorted(p.name for p in path.parent.iterdir())), (before, ["run.json", "trials.jsonl"])
        )

    def test_rewritten_files_keep_their_permissions(self) -> None:
        out = fixtures.tmpdir(self)
        path = self.make(out, "a" * 32, "t1")
        path.chmod(0o640)
        apply_gateway_costs(out, {"t1": 1.0})
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_new_files_are_world_readable_like_other_results(self) -> None:
        out = fixtures.tmpdir(self)
        RunWriter(out, run_info(run_id="d" * 32))
        self.assertEqual((out / ("d" * 32) / "run.json").stat().st_mode & 0o777, 0o644)


class RunOutputTests(RunTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg["tasks"][0]["prompt"] = PROMPT
        for h in self.cfg["harnesses"].values():
            h["env"]["FAKE_API_KEY"] = "${FAKE_API_KEY}"
        patcher = mock.patch.dict(os.environ, {"FAKE_API_KEY": API_KEY})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.out = self.base / "results"

    def run_dirs(self) -> list[Path]:
        """Run directories, oldest first (their names are random)."""
        dirs = [p for p in self.out.iterdir() if (p / "run.json").exists()]
        return sorted(dirs, key=lambda p: json.loads((p / "run.json").read_text())["started_at"])

    def test_one_run_dir_with_valid_run_json_and_trials(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (run_dir,) = self.run_dirs()
        info = json.loads((run_dir / "run.json").read_text())
        trials = read_jsonl(run_dir / "trials.jsonl")
        legacy = fixtures.read_rows(self.out / "results.jsonl")
        self.assertEqual(validate(info, RUN_SCHEMA), [])
        self.assertEqual([validate(t, TRIAL_SCHEMA) for t in trials], [[], []])
        self.assertEqual((len(trials), len({t["trial_id"] for t in trials})), (len(legacy), len(legacy)))
        self.assertEqual({t["run_id"] for t in trials} | {info["run_id"]}, {run_dir.name})
        self.assertIsNotNone(info["finished_at"])
        self.assertEqual([t["status"] for t in trials], ["passed", "failed"])

    def test_run_info_describes_the_run(self) -> None:
        self.cfg["harnesses"]["claude"]["version"] = "2.0.1"
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        (run_dir,) = self.run_dirs()
        info = RunInfo.from_dict(json.loads((run_dir / "run.json").read_text()))
        self.assertEqual([t.target_id for t in info.targets], ["claude@2.0.1:fix"])
        self.assertEqual(
            (info.sandboxed, len(info.config_sha256), len(info.suite_version), info.harness_bench_version),
            (False, 64, 12, hb.VERSION),
        )

    def test_trials_hold_no_secret_prompt_or_agent_output(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (run_dir,) = self.run_dirs()
        text = (run_dir / "trials.jsonl").read_text() + (run_dir / "run.json").read_text()
        self.assertEqual((API_KEY in text, PROMPT in text), (False, False))

    def test_artifact_uri_points_at_the_logs_of_the_trial(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        (run_dir,) = self.run_dirs()
        uri = read_jsonl(run_dir / "trials.jsonl")[0]["artifact_uri"]
        self.assertEqual(uri, "logs/fizzbuzz__claude__fix__r1")
        self.assertTrue((self.out / f"{uri}.agent.jsonl").exists())

    def test_legacy_row_is_unchanged_by_the_contract(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        row = fixtures.read_rows(self.out / "results.jsonl")[0]
        self.assertEqual(
            sorted(row),
            sorted(
                "ts task harness model rep passed agent_rc agent_timed_out wall_s parse_ok run_tag t_start t_end "
                "sandboxed input output reasoning cache_read cache_write cost_reported turns tool_calls "
                "cost_computed".split()
            ),
        )

    def test_resume_writes_a_new_run_dir_with_no_trials(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        first, second = self.run_dirs()
        info = json.loads((second / "run.json").read_text())
        self.assertEqual(
            (
                len(read_jsonl(first / "trials.jsonl")),
                (second / "trials.jsonl").read_text(),
                info["finished_at"] is None,
            ),
            (2, "", False),
        )

    def test_interrupted_run_is_resumed_into_a_new_run_dir(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        first, second = self.run_dirs()
        self.assertEqual(
            (
                [t["target"]["harness"] for t in read_jsonl(first / "trials.jsonl")],
                [t["target"]["harness"] for t in read_jsonl(second / "trials.jsonl")],
            ),
            (["claude"], ["pi"]),
        )

    def test_dry_run_writes_no_run_dir(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(dry_run=True), self.cfg, self.base)
        self.assertEqual([p.name for p in self.out.iterdir()], ["logs"])

    def test_trials_record_a_test_timeout(self) -> None:
        self.cfg["tasks"][0]["test"] = f"{fixtures.PY} -c 'import time; time.sleep(60)'"
        self.cfg["tasks"][0]["test_timeout"] = 1
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        (run_dir,) = self.run_dirs()
        (line,) = read_jsonl(run_dir / "trials.jsonl")
        self.assertEqual((line["status"], line["graders"][0]["timed_out"]), ("timeout", True))


class HostileAgentOutputTests(RunTestCase):
    """Parser output is untrusted: bad numbers must not abort the matrix or split the two result files."""

    CASES = {
        "nan cost": ('{"type":"result","total_cost_usd":NaN,"usage":{"input_tokens":5},"num_turns":1}', True),
        "bool cost": ('{"type":"result","total_cost_usd":true,"usage":{"input_tokens":5},"num_turns":1}', True),
        "negative tokens": ('{"type":"result","usage":{"input_tokens":-5,"output_tokens":3},"num_turns":1}', False),
    }

    def set_agent(self, output: str) -> None:
        """Claude harness that prints `output` (a script file: command parts go through str.format)."""
        script = self.base / "agent_output.py"
        script.write_text(f"print({output!r})\n")
        self.cfg["harnesses"]["claude"]["command"] = [fixtures.PY, str(script)]

    def test_bad_numbers_keep_both_files_aligned(self) -> None:
        for name, (output, parsed) in self.CASES.items():
            with self.subTest(case=name):
                shutil.rmtree(self.base / "results", ignore_errors=True)
                self.set_agent(output)
                out = fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
                (run_dir,) = [p for p in (self.base / "results").iterdir() if (p / "run.json").exists()]
                lines = read_jsonl(run_dir / "trials.jsonl")
                self.assertEqual(len(fixtures.read_rows(self.base / "results" / "results.jsonl")), len(lines))
                self.assertEqual([validate(t, TRIAL_SCHEMA) for t in lines], [[]])
                self.assertEqual((lines[0]["usage"]["parsed"], lines[0]["usage"]["cost_usd"]), (parsed, None))
                tokens = lines[0]["usage"]
                kept = tokens["input_tokens"] + tokens["output_tokens"] if parsed else 0
                self.assertEqual(kept, 5 if parsed else 0)
                self.assertIn(lines[0]["trace_id"], out)  # the warning names the trial by its run tag
                self.assertIsNotNone(json.loads((run_dir / "run.json").read_text())["finished_at"])
                self.assertIn("warning", out)

    def test_the_matrix_continues_after_a_bad_trial(self) -> None:
        self.set_agent(self.CASES["bool cost"][0])
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertEqual(
            [r["harness"] for r in fixtures.read_rows(self.base / "results" / "results.jsonl")], ["claude", "pi"]
        )

    def test_report_prints_a_warning_naming_the_sanitized_field(self) -> None:
        self.set_agent(self.CASES["nan cost"][0])
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        out = fixtures.call_quiet(hb.cmd_report, args(), self.cfg, self.base)
        self.assertIn("warning: ", out)
        self.assertIn("cost_reported", out)

    def test_report_still_works_over_the_bad_legacy_row(self) -> None:
        self.set_agent(self.CASES["negative tokens"][0])
        fixtures.call_quiet(hb.cmd_run, args(only=["claude"]), self.cfg, self.base)
        out = fixtures.call_quiet(hb.cmd_report, args(), self.cfg, self.base)
        self.assertIn("no token data from the harness for: claude/fix (1)", out)


class ReconcileContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = fixtures.tmpdir(self)
        shutil.copytree(fixtures.ROOT / "tasks" / "fizzbuzz", self.base / "tasks" / "fizzbuzz")
        self.server = ScriptedServer()
        self.addCleanup(self.server.close)
        patcher = mock.patch.dict(os.environ, {"HB_TEST_KEY": "k-test"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = fixtures.fake_config({})
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}]
        self.cfg["litellm"] = {"base_url": self.server.url, "api_key": "${HB_TEST_KEY}"}

    def test_gateway_cost_is_written_into_trials_by_trace_id(self) -> None:
        fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        tag = fixtures.read_rows(self.base / "results" / "results.jsonl")[0]["run_tag"]
        self.server.route(
            "/spend/logs/v2",
            ok({"data": [{"request_tags": [tag], "spend": 0.25}, {"request_tags": [tag], "spend": 0.5}]}),
        )
        out = fixtures.call_quiet(hb.cmd_reconcile, mock.Mock(cost_source=None), self.cfg, self.base)
        (trials_path,) = (self.base / "results").glob("*/trials.jsonl")
        usage = read_jsonl(trials_path)[0]["usage"]
        self.assertEqual((usage["costs"]["gateway"], usage["cost_usd"], usage["cost_source"]), (0.75, 0.75, "gateway"))
        self.assertIn("contract: 1 trials updated", out)

    def test_pre_phase_1_output_without_run_dirs_still_reconciles(self) -> None:
        results = self.base / "results"
        results.mkdir()
        fixtures.write_rows(results / "results.jsonl", fixtures.run_row())
        self.server.route("/spend/logs/v2", ok({"data": [{"request_tags": ["tag-1"], "spend": 0.4}]}))
        out = fixtures.call_quiet(hb.cmd_reconcile, mock.Mock(cost_source=None), self.cfg, self.base)
        self.assertEqual(
            (fixtures.read_rows(results / "results.jsonl")[0]["cost_litellm"], "contract: 0 trials updated" in out),
            (0.4, True),
        )


if __name__ == "__main__":
    unittest.main()
```

`tests/test_stats.py`:

```python
"""Tests for the pure statistics functions (`hbench.stats`)."""

import math
import unittest

from hbench.stats import (
    _splitmix64,
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


def rounded(interval: tuple[float, float]) -> tuple[float, float]:
    return round(interval[0], 4), round(interval[1], 4)


class WilsonTests(unittest.TestCase):
    def test_known_values(self) -> None:
        self.assertEqual(rounded(wilson_interval(8, 10)), (0.4902, 0.9433))
        self.assertEqual(rounded(wilson_interval(0, 10)), (0.0, 0.2775))

    def test_no_data_is_the_whole_range(self) -> None:
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))

    def test_bounds_are_clamped_to_the_unit_interval(self) -> None:
        # Unclamped, the float arithmetic gives -2.8e-17 for (0, 10) and 1.0000000000000002 for (5, 5).
        self.assertGreaterEqual(wilson_interval(0, 10)[0], 0.0)
        self.assertLessEqual(wilson_interval(5, 5)[1], 1.0)


class PassAtKTests(unittest.TestCase):
    def test_pass_at_k(self) -> None:
        self.assertAlmostEqual(pass_at_k(5, 2, 1), 0.4)
        self.assertAlmostEqual(pass_at_k(5, 2, 2), 0.7)
        self.assertEqual(pass_at_k(5, 0, 3), 0.0)

    def test_pass_hat_k(self) -> None:
        self.assertAlmostEqual(pass_hat_k(5, 2, 2), 0.1)
        self.assertEqual(pass_hat_k(5, 5, 3), 1.0)

    def test_bad_arguments_raise(self) -> None:
        for fn in (pass_at_k, pass_hat_k):
            for n, c, k in ((5, 2, 0), (5, 2, 6), (5, 6, 1)):
                with self.subTest(fn=fn.__name__, n=n, c=c, k=k), self.assertRaises(ValueError):
                    fn(n, c, k)

    def test_mean_over_tasks_averages_the_estimator(self) -> None:
        self.assertAlmostEqual(mean_over_tasks([(5, 5), (5, 0)], 1, pass_at_k), 0.5)

    def test_mean_over_no_tasks_is_zero(self) -> None:
        self.assertEqual(mean_over_tasks([], 1, pass_at_k), 0.0)


class BootstrapTests(unittest.TestCase):
    COSTS = [1.0, 1.0, 1.0, 1.0]
    PASSED = [True, True, True, False]

    def test_same_seed_gives_the_same_interval(self) -> None:
        a = bootstrap_ratio_ci(self.COSTS, self.PASSED, "h|m")
        self.assertEqual(a, bootstrap_ratio_ci(self.COSTS, self.PASSED, "h|m"))

    def test_other_seed_gives_another_interval(self) -> None:
        costs = [0.5, 1.0, 2.0, 1.5, 0.7, 3.0]
        passed = [True, False, True, True, False, True]
        self.assertNotEqual(bootstrap_ratio_ci(costs, passed, "a"), bootstrap_ratio_ci(costs, passed, "b"))

    def test_known_interval_for_fixed_input(self) -> None:
        # Expected values come from a separate throwaway script (own SplitMix64 and percentile bootstrap, same
        # sha256 seeding): sorted resample ratios [49..51] are 1.9, 1.9111.., 1.925 and [1949..1951] are
        # 8.475, 8.6667.., 8.7, so moving either percentile index by one changes the result.
        costs = [0.3, 1.7, 2.2, 4.1, 5.9, 0.8, 3.3, 2.9, 1.1]
        passed = [True, False, True, True, False, True, True, True, False]
        self.assertEqual(bootstrap_ratio_ci(costs, passed, "b"), (1.911111111111111, 8.666666666666666))

    def test_splitmix64_matches_the_reference_vector(self) -> None:
        draws = _splitmix64(1234567)
        self.assertEqual(
            [next(draws) for _ in range(3)], [6457827717110365317, 3203168211198807973, 9817491932198370423]
        )

    def test_no_pass_gives_infinite_interval(self) -> None:
        self.assertEqual(bootstrap_ratio_ci([1.0, 2.0], [False, False], "s"), (math.inf, math.inf))

    def test_interval_contains_the_point_estimate(self) -> None:
        low, high = bootstrap_ratio_ci(self.COSTS, self.PASSED, "s")
        self.assertLessEqual(low, 4 / 3)
        self.assertGreaterEqual(high, 4 / 3)

    def test_mismatched_or_empty_input_raises(self) -> None:
        for costs, passed in (([], []), ([1.0], [True, False])):
            with self.subTest(costs=costs), self.assertRaises(ValueError):
                bootstrap_ratio_ci(costs, passed, "s")


class McNemarTests(unittest.TestCase):
    def test_known_values(self) -> None:
        self.assertEqual(mcnemar_exact(0, 6), 0.03125)
        self.assertEqual(mcnemar_exact(3, 3), 1.0)
        self.assertEqual(mcnemar_exact(0, 0), 1.0)


class ParetoTests(unittest.TestCase):
    def test_dominated_points_are_off_the_front(self) -> None:
        self.assertEqual(pareto_front([(1, 0.5), (2, 0.9), (3, 0.8), (1, 0.4)]), [True, True, False, False])

    def test_identical_points_both_stay(self) -> None:
        self.assertEqual(pareto_front([(1, 0.5), (1, 0.5)]), [True, True])


class TieTests(unittest.TestCase):
    def test_groups_by_overlap_with_the_leader(self) -> None:
        self.assertEqual(tie_leaders([(1, 2), (1.5, 3), (4, 5), None]), [1, 1, None, None])

    def test_a_low_bound_equal_to_the_leader_high_bound_is_a_tie(self) -> None:
        self.assertEqual(tie_leaders([(1, 2), (2, 3)]), [1, 1])

    def test_a_later_group_can_tie_too(self) -> None:
        self.assertEqual(tie_leaders([(1, 2), (5, 6), (5.5, 7)]), [None, 2, 2])

    def test_missing_interval_ends_the_group(self) -> None:
        self.assertEqual(tie_leaders([(1, 2), None, (1.5, 3)]), [None, None, None])


class RepsTests(unittest.TestCase):
    def test_smallest_run_count_reaching_the_width(self) -> None:
        # Wilson width at p = 0.5: 0.2002 with 92 runs, 0.1992 with 93.
        self.assertEqual(reps_for_ci_width(0.5, 0.2, 1), 93)
        self.assertEqual(reps_for_ci_width(0.5, 0.2003, 1), 92)

    def test_runs_are_converted_to_reps_per_task(self) -> None:
        self.assertEqual(reps_for_ci_width(0.5, 0.2, 10), 10)

    def test_no_data_assumes_one_half(self) -> None:
        self.assertEqual(reps_for_ci_width(None, 0.2, 1), reps_for_ci_width(0.5, 0.2, 1))

    def test_unreachable_width_gives_none(self) -> None:
        self.assertIsNone(reps_for_ci_width(0.5, 1e-9, 1, max_runs=50))


if __name__ == "__main__":
    unittest.main()
```

`tests/test_report.py`:

```python
"""Tests for the report derived from the contract (`hbench.report`)."""

import math
import re
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

import fixtures
from hbench.core.convert import trials_from_rows
from hbench.report import aggregate, fmt_int, fmt_money, fmt_tool_calls, render_markdown, write_csv
from hbench.stats import pass_at_k, pass_hat_k, reps_for_ci_width, wilson_interval
from test_golden import GOLDEN, mask_summary

NOW = datetime(2026, 11, 24, 12, 30)


def golden_trials() -> list[Any]:
    return trials_from_rows(fixtures.read_rows(GOLDEN / "results.jsonl"))


class GoldenOutputTests(unittest.TestCase):
    def test_markdown_matches_the_golden_summary(self) -> None:
        trials = golden_trials()
        md = render_markdown(aggregate(trials, "auto"), trials, "auto", NOW)
        self.assertEqual(md.split("\n", 1)[0], "# Harness x model — 2026-11-24 12:30")
        self.assertEqual(mask_summary(md), (GOLDEN / "summary.md").read_text())

    def test_csv_matches_the_golden_summary(self) -> None:
        path = fixtures.tmpdir(self) / "summary.csv"
        write_csv(aggregate(golden_trials(), "auto"), path)
        self.assertEqual(path.read_text(), (GOLDEN / "summary.csv").read_text())


class AggregateTests(unittest.TestCase):
    def test_legacy_rows_without_parse_ok_or_costs_aggregate(self) -> None:
        row = {
            "task": "t",
            "harness": "h",
            "model": "m",
            "rep": 1,
            "passed": True,
            "input": 100,
            "output": 10,
            "cache_read": 0,
            "cache_write": 0,
            "turns": 2,
            "wall_s": 3.0,
        }
        (entry,) = aggregate(trials_from_rows([row]), "auto")
        self.assertEqual(
            (entry["cost_total"], entry["cost_source"], entry["parse_failures"], entry["tokens_total"]),
            (None, "n/a", 0, 110),
        )

    def test_forced_gateway_cost_without_data_gives_none_not_zero(self) -> None:
        trials = trials_from_rows([fixtures.run_row(cost_computed=1.0)])
        (entry,) = aggregate(trials, "litellm")
        self.assertEqual((entry["cost_total"], entry["cost_per_solved"], entry["cost_source"]), (None, None, "n/a"))

    def test_forced_source_keeps_the_legacy_name(self) -> None:
        trials = trials_from_rows([fixtures.run_row(cost_computed=1.0)])
        (entry,) = aggregate(trials, "computed")
        self.assertEqual((entry["cost_total"], entry["cost_source"]), (1.0, "computed"))

    def test_unknown_cost_source_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            aggregate(trials_from_rows([fixtures.run_row()]), "bogus")

    def test_unsolved_pair_has_infinite_cost_per_solved(self) -> None:
        trials = trials_from_rows([fixtures.run_row(passed=False, cost_computed=1.0)])
        (entry,) = aggregate(trials, "auto")
        self.assertEqual((entry["cost_per_solved"], entry["tokens_per_solved"]), (float("inf"), float("inf")))


LEGACY_KEYS = [
    "harness",
    "model",
    "cost_source",
    "runs",
    "solved",
    "pass_rate",
    "tokens_total",
    "tokens_per_run",
    "tokens_per_solved",
    "cache_hit",
    "cost_total",
    "cost_per_run",
    "cost_per_solved",
    "cost_per_1m_tokens",
    "avg_turns",
    "avg_wall_s",
    "parse_failures",
]


def pair_trials(
    harness: str, model: str, outcomes: list[bool], costs: list[float | None] | None = None, task: str = "t1"
) -> list[Any]:
    """Trials of one pair on one task: one per outcome, rep 1.. in order."""
    costs = costs if costs is not None else [0.1] * len(outcomes)
    rows = [
        fixtures.run_row(harness=harness, model=model, task=task, rep=i, passed=ok, cost_computed=c)
        for i, (ok, c) in enumerate(zip(outcomes, costs, strict=True), 1)
    ]
    return trials_from_rows(rows)


class StatisticsTests(unittest.TestCase):
    def test_three_reps_get_interval_and_pass_estimators(self) -> None:
        (entry,) = aggregate(pair_trials("h", "m", [True, True, False]), "auto")
        low, high = wilson_interval(2, 3)
        self.assertEqual((entry["pass_ci_low"], entry["pass_ci_high"]), (low, high))
        self.assertEqual(
            (entry["k"], entry["pass_at_k"], entry["pass_hat_k"]), (3, pass_at_k(3, 2, 3), pass_hat_k(3, 2, 3))
        )
        self.assertLessEqual(entry["cost_per_solved_ci_low"], entry["cost_per_solved"])
        self.assertGreaterEqual(entry["cost_per_solved_ci_high"], entry["cost_per_solved"])
        self.assertEqual((entry["null_control"], entry["tie_with"]), (False, None))

    def test_existing_keys_keep_their_order(self) -> None:
        (entry,) = aggregate(pair_trials("h", "m", [True]), "auto")
        self.assertEqual(list(entry)[:17], LEGACY_KEYS)

    def test_k_is_the_smallest_run_count_over_tasks(self) -> None:
        trials = pair_trials("h", "m", [True, False, True], task="a") + pair_trials("h", "m", [True], task="b")
        trials += pair_trials("x", "m", [True], task="a")  # another pair has no runs on task b
        table = {e["harness"]: e for e in aggregate(trials, "auto")}
        self.assertEqual((table["h"]["k"], table["x"]["k"]), (1, 1))
        self.assertAlmostEqual(table["h"]["pass_at_k"], (2 / 3 + 1) / 2)

    def test_unknown_cost_gives_no_interval_tie_or_frontier(self) -> None:
        trials = pair_trials("h", "m", [True, True, True], [0.1, None, 0.1])
        (entry,) = aggregate(trials, "auto")
        self.assertEqual(
            (entry["cost_per_solved"], entry["cost_per_solved_ci_low"], entry["cost_per_solved_ci_high"]),
            (None, None, None),
        )
        self.assertEqual((entry["tie_with"], entry["pareto"]), (None, False))

    def test_zero_passes_have_infinite_cost_and_no_interval(self) -> None:
        (entry,) = aggregate(pair_trials("h", "m", [False, False]), "auto")
        self.assertEqual((entry["cost_per_solved"], entry["cost_per_solved_ci_low"]), (math.inf, None))
        self.assertEqual(entry["tie_with"], None)

    def test_overlapping_cost_intervals_tie_with_the_leader(self) -> None:
        trials = pair_trials("a", "m", [True] * 4, [0.10, 0.10, 0.12, 0.12])
        trials += pair_trials("b", "m", [True] * 4, [0.11, 0.11, 0.12, 0.12])
        table = aggregate(trials, "auto")
        self.assertEqual([e["harness"] for e in table], ["a", "b"])
        self.assertEqual([e["tie_with"] for e in table], [1, 1])

    def test_pareto_marks_pairs_no_other_pair_beats(self) -> None:
        trials = pair_trials("cheap", "m", [True, True], [0.1, 0.1]) + pair_trials(
            "dear", "m", [True, True], [0.2, 0.2]
        )
        trials += pair_trials("weak", "m", [True, False], [0.05, 0.05])
        table = {e["harness"]: e["pareto"] for e in aggregate(trials, "auto")}
        self.assertEqual(table, {"cheap": True, "dear": False, "weak": True})

    def test_null_pairs_are_flagged_and_stay_off_the_frontier(self) -> None:
        trials = pair_trials("null", "none", [True], [0.0]) + pair_trials("h", "m", [True])
        table = {e["harness"]: e for e in aggregate(trials, "auto", null_harnesses={"null"})}
        self.assertEqual((table["null"]["null_control"], table["null"]["pareto"]), (True, False))
        self.assertEqual((table["h"]["null_control"], table["h"]["pareto"]), (False, True))

    def test_target_width_sets_the_repetitions_needed(self) -> None:
        trials = pair_trials("h", "m", [True, False])
        (wide,) = aggregate(trials, "auto", target_ci_width=0.4)
        (narrow,) = aggregate(trials, "auto", target_ci_width=0.1)
        self.assertLess(wide["reps_needed"], narrow["reps_needed"])
        self.assertEqual(narrow["reps_needed"], reps_for_ci_width(0.5, 0.1, 1))


def render(trials: list[Any], null: tuple[str, ...] = (), **kwargs: Any) -> str:
    width = kwargs.get("target_ci_width", 0.2)
    table = aggregate(trials, "auto", null_harnesses=null, target_ci_width=width)
    return render_markdown(table, trials, "auto", NOW, **kwargs)


class RenderStatisticsTests(unittest.TestCase):
    def test_reps_fixture_matches_its_golden(self) -> None:
        trials = trials_from_rows(fixtures.read_rows(GOLDEN / "results_reps.jsonl"))
        self.assertEqual(mask_summary(render(trials, ("null",))), (GOLDEN / "summary_reps.md").read_text())

    def test_main_table_shows_interval_estimators_tie_and_frontier(self) -> None:
        md = render(pair_trials("h", "m", [True, True, False], [0.05, 0.06, 0.07]))
        self.assertIn("| Pass 95% CI | pass@k | pass^k | Cost/solved 95% CI | Tie | Pareto |", md)
        self.assertRegex(md, r"\| 21–94% \| 100% \(k=3\) \| 0% \(k=3\) \| \$0\.0500–inf \| +\| yes \|")

    def test_recommendation_is_the_cheapest_pair_within_the_tolerance(self) -> None:
        trials = pair_trials("best", "m", [True] * 20, [0.5] * 20) + pair_trials(
            "close", "m", [True] * 19 + [False], [0.1] * 20
        )
        trials += pair_trials("far", "m", [True] * 10 + [False] * 10, [0.01] * 20)
        md = render(trials)
        self.assertIn(
            "**Recommendation:** close/m — cheapest per solved task within 5 pp of the best pass rate (100%).", md
        )
        self.assertIn("within 60 pp", render(trials, recommend_within_pp=60))

    def test_unknown_cost_unsolved_and_null_pairs_are_never_recommended(self) -> None:
        trials = pair_trials("nocost", "m", [True], [None]) + pair_trials("nopass", "m", [False])
        trials += pair_trials("null", "none", [True], [0.0])
        self.assertNotIn("Recommendation", render(trials, ("null",)))

    def test_pareto_section_lists_the_frontier(self) -> None:
        md = render(pair_trials("cheap", "m", [True]) + pair_trials("dear", "m", [True], [0.2]))
        self.assertIn("## Pareto frontier (cost per run × pass rate)\n\n- cheap/m: $0.1000 per run, 100%\n", md)
        self.assertNotIn("dear/m: $", md)

    def test_null_control_that_solves_a_task_prints_a_warning(self) -> None:
        md = render(pair_trials("null", "none", [True, False], [0.0, 0.0]) + pair_trials("h", "m", [True]), ("null",))
        self.assertIn("## Negative control\n\n**Warning:** null control solved tasks: null/none (1/2).", md)

    def test_null_control_that_solves_nothing_is_reported_clean(self) -> None:
        md = render(pair_trials("null", "none", [False], [0.0]) + pair_trials("h", "m", [True]), ("null",))
        self.assertIn("## Negative control\n\nnull control solved nothing.", md)

    def test_no_negative_control_section_without_null_pairs(self) -> None:
        self.assertNotIn("Negative control", render(pair_trials("h", "m", [True])))

    def test_discrimination_lists_saturated_and_unsolved_tasks_without_null_pairs(self) -> None:
        trials = pair_trials("a", "m", [True, True], task="easy") + pair_trials("b", "m", [True], task="easy")
        trials += pair_trials("a", "m", [False], task="hard") + pair_trials("b", "m", [False], task="hard")
        trials += pair_trials("a", "m", [True, False], task="mixed") + pair_trials("b", "m", [True], task="mixed")
        trials += pair_trials("null", "none", [False], [0.0], task="easy")
        md = render(trials, ("null",))
        self.assertIn("- Saturated (solved in every run): easy\n- Unsolved (never solved): hard\n", md)

    def test_single_pair_has_discrimination_but_no_paired_comparison(self) -> None:
        md = render(pair_trials("h", "m", [True, False], task="t1") + pair_trials("h", "m", [True], task="t2"))
        self.assertIn("- Saturated (solved in every run): t2", md)
        self.assertNotIn("Paired comparison", md)

    def test_paired_comparison_counts_discordant_runs_against_the_first_pair(self) -> None:
        trials = pair_trials("a", "m", [True] * 6, [0.1] * 6) + pair_trials("b", "m", [False] * 6, [0.2] * 6)
        trials += pair_trials("c", "m", [True] * 6, [0.3] * 6)
        md = render(trials)
        self.assertIn("## Paired comparison vs #1 (McNemar, exact)\n", md)
        self.assertIn("- #3 b/m: b=6, c=0, p=0.0312", md)
        self.assertIn("- #2 c/m: b=0, c=0, p=1.0000", md)

    def test_paired_comparison_skips_pairs_that_share_no_runs(self) -> None:
        md = render(pair_trials("a", "m", [True], task="x") + pair_trials("b", "m", [True], task="y"))
        self.assertNotIn("Paired comparison", md)

    def test_repetitions_section_gives_reps_per_task_for_the_target_width(self) -> None:
        md = render(pair_trials("h", "m", [True, False]), target_ci_width=0.4)
        self.assertIn(
            f"- To reach a pass-rate CI width of 0.40: h/m needs {reps_for_ci_width(0.5, 0.4, 1)} reps per task.", md
        )

    def test_a_report_of_null_pairs_only_has_no_recommendation_or_repetitions(self) -> None:
        md = render(pair_trials("null", "none", [False], [0.0]), ("null",))
        self.assertEqual([h for h in ("Recommendation", "Repetitions", "Discrimination") if h in md], [])
        self.assertIn("null control solved nothing.", md)

    def test_unreachable_repetitions_say_more_than_the_limit(self) -> None:
        trials = pair_trials("h", "m", [True, False], task="a") + pair_trials("h", "m", [True, False], task="b")
        md = render(trials, target_ci_width=1e-9)
        self.assertIn("h/m needs more than 5000 reps per task.", md)

    def test_a_null_control_that_passed_is_ranked_after_every_real_pair(self) -> None:
        trials = pair_trials("null", "none", [True, True], [0.0, 0.0]) + pair_trials("a", "m", [True, True])
        trials += pair_trials("b", "m", [True, False]) + pair_trials("c", "m", [False, False])
        trials += pair_trials("d", "m", [True, True], [None, None])  # unknown cost sorts before the null control
        md = render(trials, ("null",))
        ranks = re.findall(r"^\| (\d) \| (\w+) \|", md, re.M)
        self.assertEqual([h for _, h in ranks], ["a", "b", "c", "d", "null"])
        self.assertIn("- #2 b/m:", md)
        self.assertNotIn("- #1 ", md)
        self.assertIn("**Warning:** null control solved tasks: null/none (2/2).", md)
        self.assertNotIn("**Recommendation:** null", md)

    def test_pairs_with_no_pass_stay_off_the_frontier(self) -> None:
        md = render(pair_trials("good", "m", [True], [0.2]) + pair_trials("free", "m", [False], [0.01]))
        self.assertIn("- good/m:", md)
        self.assertNotIn("- free/m:", md)

    def test_every_review_focus_case_renders(self) -> None:
        trials = pair_trials("h", "m", [False, False], [None, 0.1]) + pair_trials("x", "m", [True], task="other")
        trials += pair_trials("h", "m", [True], [0.1], task="other")
        self.assertIn("## Per task", render(trials))


class FormatTests(unittest.TestCase):
    def test_money_none_inf_small_and_large(self) -> None:
        self.assertEqual([fmt_money(v) for v in (None, math.inf, 0.12345, 12.345)], ["n/a", "inf", "$0.1235", "$12.35"])

    def test_int_none_inf_and_grouping(self) -> None:
        self.assertEqual([fmt_int(v) for v in (None, math.inf, 1234567.4)], ["n/a", "n/a", "1,234,567"])


class ToolCallStatisticsTests(unittest.TestCase):
    @staticmethod
    def trials(*calls: int | None, passed: tuple[bool, ...] | None = None) -> list[Any]:
        outcomes = passed if passed is not None else (True,) * len(calls)
        rows = [
            fixtures.run_row(rep=i, passed=ok, tool_calls=c)
            for i, (c, ok) in enumerate(zip(calls, outcomes, strict=True), 1)
        ]
        return trials_from_rows(rows)

    def test_averages_per_run_and_per_solved_task(self) -> None:
        (entry,) = aggregate(self.trials(4, 6, 8, passed=(True, False, True)), "auto")
        self.assertEqual((entry["tool_calls_per_run"], entry["tool_calls_per_solved"]), (6.0, 9.0))

    def test_one_unknown_trial_makes_the_pair_unknown_not_zero(self) -> None:
        (entry,) = aggregate(self.trials(4, None), "auto")
        self.assertEqual((entry["tool_calls_per_run"], entry["tool_calls_per_solved"]), (None, None))

    def test_a_known_zero_stays_zero(self) -> None:
        (entry,) = aggregate(self.trials(0, 0), "auto")
        self.assertEqual((entry["tool_calls_per_run"], entry["tool_calls_per_solved"]), (0.0, 0.0))

    def test_nothing_solved_gives_infinite_per_solved(self) -> None:
        (entry,) = aggregate(self.trials(3, passed=(False,)), "auto")
        self.assertEqual((entry["tool_calls_per_run"], entry["tool_calls_per_solved"]), (3.0, math.inf))

    def test_the_new_keys_come_last(self) -> None:
        (entry,) = aggregate(self.trials(1), "auto")
        self.assertEqual(list(entry)[-2:], ["tool_calls_per_run", "tool_calls_per_solved"])

    def test_the_table_has_two_new_columns_with_n_a_when_unknown(self) -> None:
        known = render(self.trials(2, 4))
        unknown = render(self.trials(None))
        self.assertIn("| Pareto | Tool calls/run | Tool calls/solved |", known)
        self.assertRegex(known, r"\| 3\.0 \| 3\.0 \|\n")
        self.assertRegex(unknown, r"\| n/a \| n/a \|\n\n")

    def test_nothing_solved_renders_inf_and_not_n_a_in_the_table(self) -> None:
        table = render(self.trials(3, passed=(False,)))
        self.assertRegex(table, r"\| 3\.0 \| inf \|\n")

    def test_formatting(self) -> None:
        self.assertEqual([fmt_tool_calls(v) for v in (None, math.inf, 0.0, 2.55)], ["n/a", "inf", "0.0", "2.5"])


class WriteCsvTests(unittest.TestCase):
    def test_writes_header_and_one_line_per_pair(self) -> None:
        path: Path = fixtures.tmpdir(self) / "s.csv"
        write_csv(aggregate(trials_from_rows([fixtures.run_row()]), "auto"), path)
        self.assertEqual(len(path.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
```

`tests/test_cli.py`:

```python
"""Tests for the single CLI (`python3 -m hbench`), the LiteLLM profile and the two shims."""

import copy
import importlib
import json
import shutil
import subprocess
import sys
import unittest
from typing import Any
from unittest import mock

import fixtures
from hbench import cli
from hbench.core.schema import schema_document
from hbench.profiles import apply_litellm_profile
from test_litellm import ScriptedServer, ok

ROOT = fixtures.ROOT
PY = fixtures.PY


def run_script(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([PY, *argv], cwd=ROOT, capture_output=True, text=True)


class SchemaExportTests(unittest.TestCase):
    def test_module_entry_point_prints_the_schema_document(self) -> None:
        p = run_script("-m", "hbench", "schema", "export")
        self.assertEqual((p.returncode, json.loads(p.stdout)), (0, schema_document()))

    def test_out_writes_the_same_document_to_a_file(self) -> None:
        path = fixtures.tmpdir(self) / "schema.json"
        with fixtures.quiet() as out:
            cli.main(["schema", "export", "--out", str(path)])
        self.assertEqual((json.loads(path.read_text()), out.getvalue()), (schema_document(), ""))

    def test_output_is_stable_and_indented(self) -> None:
        with fixtures.quiet() as out:
            cli.main(["schema", "export"])
        self.assertEqual(out.getvalue(), json.dumps(schema_document(), indent=2, sort_keys=True) + "\n")


class EntryPointTests(unittest.TestCase):
    def test_importing_the_main_module_does_not_run_the_cli(self) -> None:
        with mock.patch.object(sys, "argv", ["hbench"]):  # a run would fail on the missing subcommand
            importlib.import_module("hbench.__main__")

    def test_engine_lookup_puts_the_repo_root_on_the_path(self) -> None:
        with mock.patch.object(sys, "path", ["/nonexistent"]):
            engine = cli._engine()
            self.assertEqual(sys.path[0], str(ROOT))
        self.assertTrue(hasattr(engine, "cmd_run"))


class ProgNameTests(unittest.TestCase):
    def test_usage_names_the_script_that_was_run(self) -> None:
        for script in ("harness_bench.py", "harness_bench_litellm.py"):
            with self.subTest(script=script):
                self.assertIn(f"usage: {script}", run_script(script, "--help").stdout)

    def test_module_invocation_is_shown_as_python_dash_m(self) -> None:
        self.assertIn("usage: python3 -m hbench", run_script("-m", "hbench", "--help").stdout)

    def test_litellm_shim_help_says_the_profile_is_applied_automatically(self) -> None:
        out = run_script("harness_bench_litellm.py", "--help").stdout
        self.assertIn("LiteLLM profile is applied automatically", out)

    def test_plain_help_has_no_litellm_epilog(self) -> None:
        self.assertNotIn("applied automatically", run_script("harness_bench.py", "--help").stdout)


class ShimHelpTests(unittest.TestCase):
    def test_both_shims_list_the_three_commands(self) -> None:
        for script in ("harness_bench.py", "harness_bench_litellm.py"):
            with self.subTest(script=script):
                p = run_script(script, "--help")
                self.assertEqual(p.returncode, 0, p.stderr)
                for command in ("run", "report", "reconcile"):
                    self.assertIn(command, p.stdout)

    def test_missing_subcommand_is_a_usage_error(self) -> None:
        for script in ("harness_bench.py", "harness_bench_litellm.py"):
            with self.subTest(script=script):
                self.assertEqual(run_script(script).returncode, 2)

    def test_litellm_shim_does_not_inject_the_profile_into_other_commands(self) -> None:
        p = run_script("harness_bench_litellm.py", "schema", "export")
        self.assertEqual((p.returncode, json.loads(p.stdout)), (0, schema_document()))


class LitellmProfileTests(unittest.TestCase):
    def config(self) -> dict[str, Any]:
        cfg = fixtures.fake_config({})
        cfg["litellm"] = {"base_url": "https://x", "api_key": "${K}"}
        return cfg

    def test_does_not_mutate_its_input(self) -> None:
        cfg = self.config()
        before = copy.deepcopy(cfg)
        with fixtures.quiet():
            result = apply_litellm_profile(cfg)
        self.assertEqual(cfg, before)
        self.assertIsNot(result, cfg)

    def test_drops_cursor_and_forces_litellm_cost(self) -> None:
        with fixtures.quiet() as out:
            result = apply_litellm_profile(self.config())
        self.assertEqual(
            ("cursor" in result["harnesses"], len(result["matrix"]), result["cost_source"], result["output_dir"]),
            (False, 4, "litellm", "results-litellm"),
        )
        self.assertIn("ignored 1 matrix entries", out.getvalue())

    def test_an_explicit_output_dir_is_kept(self) -> None:
        cfg = self.config()
        cfg["output_dir"] = "mine"
        with fixtures.quiet():
            self.assertEqual(apply_litellm_profile(cfg)["output_dir"], "mine")

    def test_missing_litellm_section_exits(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            apply_litellm_profile(fixtures.fake_config({}))
        self.assertIn("needs a 'litellm' section", str(cm.exception.code))

    def test_matrix_entry_for_an_undefined_harness_exits(self) -> None:
        cfg = self.config()
        cfg["matrix"].append({"harness": "ghost", "model": "m"})
        with fixtures.quiet(), self.assertRaises(SystemExit) as cm:
            apply_litellm_profile(cfg)
        self.assertIn("undefined harnesses", str(cm.exception.code))


class CliDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ScriptedServer()
        self.addCleanup(self.server.close)
        self.base = fixtures.tmpdir(self)
        shutil.copytree(ROOT / "tasks" / "fizzbuzz", self.base / "tasks" / "fizzbuzz")
        self.cfg = fixtures.fake_config({})
        self.cfg["litellm"] = {"base_url": self.server.url, "api_key": "k-test"}
        self.cfg_path = self.base / "config.json"
        self.cfg_path.write_text(json.dumps(self.cfg))
        self.server.route("/spend/logs/v2", ok({"data": [], "total_pages": 1}))

    def cli(self, *argv: str) -> str:
        with fixtures.quiet() as out:
            cli.main([*argv, "--config", str(self.cfg_path)])
        return out.getvalue()

    def rows(self, out_dir: str) -> list[dict[str, Any]]:
        return fixtures.read_rows(self.base / out_dir / "results.jsonl")

    def test_run_with_litellm_profile_drops_cursor_and_forces_gateway_cost(self) -> None:
        # Patch the CLI's own `time` name: a global `time.sleep` patch also catches the polling sleeps that
        # `subprocess.run(timeout=...)` makes while the fake agents run, which makes the call count load-dependent.
        with mock.patch("hbench.cli.time") as clock:
            out = self.cli("run", "--litellm", "--settle", "0", "--allow-unsandboxed")
        clock.sleep.assert_called_once_with(0)
        self.assertEqual(out.count("# Harness x model"), 1)  # one report, printed by reconcile after the wait
        self.assertGreater(out.index("# Harness x model"), out.index("waiting"))
        harnesses = {r["harness"] for r in self.rows("results-litellm")}
        self.assertEqual(harnesses, {"claude", "opencode", "pi", "codex"})
        summary = (self.base / "results-litellm" / "summary.md").read_text()
        self.assertEqual(("Cost source: `litellm`" in summary, "waiting 0s" in out), (True, True))

    def test_run_without_the_profile_keeps_every_harness_and_reports_directly(self) -> None:
        out = self.cli("run", "--allow-unsandboxed")
        self.assertEqual(
            (
                {r["harness"] for r in self.rows("results")} >= {"cursor"},
                "waiting" in out,
                "Cost source: `auto`" in out,
            ),
            (True, False, True),
        )

    def test_report_and_reconcile_commands_dispatch_to_the_engine(self) -> None:
        self.cli("run", "--allow-unsandboxed", "--only", "claude")
        self.assertIn("Cost source: `auto`", self.cli("report"))
        self.assertIn("runs matched by tag", self.cli("reconcile"))

    def test_cost_source_flag_conflicts_with_the_profile(self) -> None:
        with fixtures.quiet(), self.assertRaises(SystemExit) as cm:
            self.cli("report", "--litellm", "--cost-source", "computed")
        self.assertEqual(cm.exception.code, 2)

    def test_report_with_the_profile_forces_the_gateway_cost(self) -> None:
        self.cli("run", "--allow-unsandboxed", "--only", "claude")
        shutil.copytree(self.base / "results", self.base / "results-litellm")
        self.assertIn("Cost source: `litellm`", self.cli("report", "--litellm"))


class ReportOptionsTests(unittest.TestCase):
    """`report` over hand-written rows: `--drop-saturated` and the knobs read from the config."""

    def setUp(self) -> None:
        self.base = fixtures.tmpdir(self)
        (self.base / "results").mkdir()
        self.cfg: dict[str, Any] = {
            "output_dir": "results",
            "harnesses": {"null": {"parser": "null", "command": ["true"]}},
        }

    def report(self, *argv: str) -> str:
        cfg_path = self.base / "config.json"
        cfg_path.write_text(json.dumps(self.cfg))
        with fixtures.quiet() as out:
            cli.main(["report", "--config", str(cfg_path), *argv])
        return out.getvalue()

    def write_rows(self, *rows: dict[str, Any]) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl", *rows)

    def row(self, task: str, harness: str, passed: bool, rep: int = 1) -> dict[str, Any]:
        return fixtures.run_row(task=task, harness=harness, model="m", passed=passed, rep=rep, cost_computed=0.1)

    def test_drop_saturated_removes_the_task_from_the_matrix_and_the_pass_rates(self) -> None:
        self.write_rows(
            self.row("easy", "a", True),
            self.row("hard", "a", True),
            self.row("easy", "b", True),
            self.row("hard", "b", False),
            self.row("easy", "null", False),  # a failing null pair does not keep `easy` from being saturated
        )
        kept = self.report()
        dropped = self.report("--drop-saturated")
        self.assertIn("| easy |", kept)
        self.assertNotIn("| easy |", dropped)
        self.assertIn("| hard |", dropped)
        self.assertIn("| 1 | a | m | 1/1 | 100% |", dropped)
        self.assertIn("| 1 | a | m | 2/2 | 100% |", kept)

    def test_dropping_every_task_says_so_and_exits_cleanly(self) -> None:
        self.write_rows(self.row("easy", "a", True), self.row("easy", "b", True))
        out = self.report("--drop-saturated")
        self.assertIn("no tasks left after --drop-saturated", out)
        self.assertNotIn("| # |", out)

    def test_config_knobs_reach_the_report(self) -> None:
        self.cfg.update(recommend_within_pp=60, target_ci_width=0.4)
        self.write_rows(self.row("t", "a", True), self.row("t", "a", False, rep=2))
        out = self.report()
        self.assertIn("within 60 pp", out)
        self.assertIn("CI width of 0.40", out)


class ShimMainTests(unittest.TestCase):
    def test_engine_main_delegates_to_the_cli(self) -> None:
        import harness_bench as hb

        with mock.patch.object(sys, "argv", ["harness_bench.py", "schema", "export"]), fixtures.quiet() as out:
            hb.main()
        self.assertEqual(json.loads(out.getvalue()), schema_document())

    def test_litellm_shim_adds_the_profile_flag_after_the_subcommand(self) -> None:
        import harness_bench_litellm as hbl

        with mock.patch.object(cli, "main") as cli_main, mock.patch.object(sys, "argv", ["x", "run", "--config", "c"]):
            hbl.main()
        cli_main.assert_called_once_with(["run", "--litellm", "--config", "c"], epilog=mock.ANY)

    def test_litellm_shim_passes_flags_without_a_subcommand_through(self) -> None:
        import harness_bench_litellm as hbl

        with mock.patch.object(cli, "main") as cli_main, mock.patch.object(sys, "argv", ["x", "--help"]):
            hbl.main()
        cli_main.assert_called_once_with(["--help"], epilog=mock.ANY)


if __name__ == "__main__":
    unittest.main()
```

`tests/test_runners.py`:

```python
"""Unit tests for the runner protocol, the template adapter and the runner registry."""

import unittest

import fixtures  # noqa: F401  (puts the repo root on sys.path)
import harness_bench as hb
from hbench.runners import PARSERS, runner_for
from hbench.runners.base import HarnessRunner, TemplateRunner, build_argv


def cfg_for(parser: str, **extra: object) -> dict[str, object]:
    return {"command": ["agent", "--model", "{model}", "{prompt}"], "parser": parser, **extra}


class TemplateRunnerTests(unittest.TestCase):
    def test_command_matches_the_engine_build_cmd_for_provider_and_plain_models(self) -> None:
        template = ["{provider}", "{model_name}", "{model}", "{prompt}"]
        runner = TemplateRunner({"command": template, "parser": "claude"}, PARSERS["claude"])
        for model in ("p/m", "m"):
            self.assertEqual(runner.command(model, "go"), hb.build_cmd(template, model, "go"))

    def test_build_argv_leaves_unknown_braces_to_the_template_fields(self) -> None:
        self.assertEqual(build_argv(["[{provider}]", "{model_name}"], "m", "go"), ["[]", "m"])

    def test_version_defaults_to_unknown_and_reads_the_config(self) -> None:
        plain = TemplateRunner(cfg_for("claude"), PARSERS["claude"])
        pinned = TemplateRunner(cfg_for("claude", version="2.1"), PARSERS["claude"])
        self.assertEqual((plain.version(), pinned.version()), ("unknown", "2.1"))

    def test_name_is_the_parser_name_and_prepare_is_a_no_op(self) -> None:
        runner = TemplateRunner(cfg_for("pi"), PARSERS["pi"])
        runner.prepare(fixtures.ROOT)  # returns nothing and touches nothing
        self.assertEqual(runner.name, "pi")

    def test_parse_usage_delegates_to_the_parser(self) -> None:
        runner = TemplateRunner(cfg_for("null"), PARSERS["null"])
        usage = runner.parse_usage("anything")
        self.assertEqual(usage and usage["cost_reported"], 0.0)


class RunnerForTests(unittest.TestCase):
    def test_every_built_in_parser_has_a_runner(self) -> None:
        for name in ("claude", "opencode", "pi", "cursor", "codex", "null"):
            runner = runner_for(cfg_for(name))
            self.assertIsInstance(runner, HarnessRunner)
            self.assertEqual(runner.name, name)

    def test_parsers_registry_lists_the_built_ins(self) -> None:
        self.assertEqual(set(PARSERS), {"claude", "opencode", "pi", "cursor", "codex", "null"})

    def test_unknown_parser_raises_a_value_error(self) -> None:
        with self.assertRaises(ValueError):
            runner_for(cfg_for("nope"))

    def test_engine_reexports_the_parsers_under_the_same_names(self) -> None:
        self.assertIs(hb.parse_claude, PARSERS["claude"])
        self.assertIs(hb.PARSERS, PARSERS)


if __name__ == "__main__":
    unittest.main()
```

`tests/test_graders.py`:

```python
"""Unit tests for the grader protocol helpers: score parsing and verdict composition."""

import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import fixtures
from hbench.core.models import GraderResult
from hbench.graders.base import Change, GradeContext, Grader, parse_score
from hbench.graders.command import CommandGrader
from hbench.graders.compose import compose
from hbench.graders.diff_constraints import DiffConstraintsGrader
from hbench.graders.hidden_tests import HiddenTestsGrader
from hbench.graders.registry import GRADER_GROUP, grade_plan, grader_for


def result(passed: bool, score: float | None) -> GraderResult:
    return GraderResult(grader="g", grader_version="1", passed=passed, score=score)


class ParseScoreTests(unittest.TestCase):
    def test_the_last_score_line_wins(self) -> None:
        self.assertEqual(parse_score("SCORE: 1.0\nnoise\nSCORE: 0.25\n"), 0.25)

    def test_no_score_line_gives_none(self) -> None:
        self.assertIsNone(parse_score("all tests passed\n"))

    def test_out_of_range_and_malformed_values_give_none_with_a_warning(self) -> None:
        for text in ("SCORE: 1.5", "SCORE: 1.2.3", "SCORE: .", "SCORE: 99"):
            warnings: list[str] = []
            self.assertIsNone(parse_score(text, warnings.append), text)
            self.assertEqual(len(warnings), 1, text)

    def test_a_bad_value_without_a_warn_callback_is_just_ignored(self) -> None:
        self.assertIsNone(parse_score("SCORE: 1.5"))

    def test_text_that_is_not_a_score_line_is_ignored_without_a_warning(self) -> None:
        for text in ("SCORE: abc", "SCORE: -0.1", "SCORE:"):
            warnings: list[str] = []
            self.assertIsNone(parse_score(text, warnings.append), text)
            self.assertEqual(warnings, [], text)

    def test_a_bad_last_line_does_not_fall_back_to_an_earlier_one(self) -> None:
        self.assertIsNone(parse_score("SCORE: 0.9\nSCORE: 1.5"))

    def test_surrounding_whitespace_is_allowed_after_the_prefix(self) -> None:
        self.assertEqual(parse_score("SCORE:   0.5   "), 0.5)

    def test_a_line_that_only_contains_score_text_is_ignored(self) -> None:
        self.assertIsNone(parse_score("my SCORE: 1.0 is great"))


class ComposeTests(unittest.TestCase):
    MIXED = [result(True, 1.0), result(False, 0.0)]

    def test_all_requires_every_grader_and_averages(self) -> None:
        self.assertEqual(compose(self.MIXED, "all", [1, 1], None), (False, 0.5))

    def test_any_needs_one_pass_and_takes_the_best_score(self) -> None:
        self.assertEqual(compose(self.MIXED, "any", [1, 1], None), (True, 1.0))

    def test_weighted_compares_the_weighted_mean_with_the_threshold(self) -> None:
        self.assertEqual(compose(self.MIXED, "weighted", [3, 1], 0.7), (True, 0.75))
        self.assertEqual(compose(self.MIXED, "weighted", [1, 3], 0.7), (False, 0.25))

    def test_a_missing_score_counts_as_one_on_pass_and_zero_otherwise(self) -> None:
        self.assertEqual(compose([result(True, None), result(False, None)], "all", [1, 1], None), (False, 0.5))

    def test_weighted_score_at_the_threshold_passes(self) -> None:
        self.assertEqual(compose([result(True, 0.7)], "weighted", [1], 0.7), (True, 0.7))

    def test_a_failed_grader_keeps_its_partial_score(self) -> None:
        self.assertEqual(compose([result(False, 0.4)], "any", [1], None), (False, 0.4))
        self.assertEqual(compose([result(False, 0.8), result(True, 0.6)], "weighted", [1, 1], 0.7), (True, 0.7))

    def test_weighted_without_a_valid_threshold_raises(self) -> None:
        for threshold in (None, 0.0, 1.5):
            with self.assertRaises(ValueError):
                compose(self.MIXED, "weighted", [1, 1], threshold)

    def test_empty_results_raise(self) -> None:
        with self.assertRaises(ValueError):
            compose([], "all", [], None)

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            compose(self.MIXED, "median", [1, 1], None)

    def test_weights_must_match_the_results_and_be_positive(self) -> None:
        for weights in ([1], [1, 0], [1, -1]):
            with self.assertRaises(ValueError):
                compose(self.MIXED, "weighted", weights, 0.5)


class ProtocolShapeTests(unittest.TestCase):
    def test_change_and_context_are_plain_values(self) -> None:
        change = Change(path="a.py", added=1, deleted=2)
        self.assertEqual((change.path, change.added, change.deleted), ("a.py", 1, 2))
        ctx = GradeContext(
            workdir=fixtures.ROOT,
            task={},
            base=fixtures.ROOT,
            changes=(change,),
            run=lambda c, t, n: (0, False, "", ""),
        )
        self.assertEqual(ctx.run("x", 1, "n"), (0, False, "", ""))

    def test_a_class_with_name_version_and_grade_is_a_grader(self) -> None:
        class Always:
            name = "always"
            version = "1"

            def grade(self, ctx: GradeContext) -> GraderResult:
                return result(True, 1.0)

        self.assertIsInstance(Always(), Grader)
        self.assertNotIsInstance(object(), Grader)


class FakeRun:
    """Stands in for the engine's `run`: records the calls and answers with a fixed outcome."""

    def __init__(self, rc: int | None = 0, timed_out: bool = False, output: str = "", stderr: str = "") -> None:
        self.outcome = (rc, timed_out, output, stderr)
        self.calls: list[tuple[str, int, str]] = []

    def __call__(self, command: str, timeout: int, log_name: str) -> tuple[int | None, bool, str, str]:
        self.calls.append((command, timeout, log_name))
        return self.outcome


def context(
    run: FakeRun,
    task: Mapping[str, Any] | None = None,
    workdir: Path | None = None,
    base: Path | None = None,
    changes: tuple[Change, ...] = (),
) -> GradeContext:
    return GradeContext(
        workdir=workdir or fixtures.ROOT, task=task or {}, base=base or fixtures.ROOT, changes=changes, run=run
    )


class HiddenTestsGraderTests(unittest.TestCase):
    def test_hidden_tests_are_copied_before_the_test_command_runs(self) -> None:
        base, workdir = fixtures.tmpdir(self), fixtures.tmpdir(self)
        (base / "hidden").mkdir()
        (base / "hidden" / "test_hidden.py").write_text("x")
        seen: list[bool] = []

        def run(command: str, timeout: int, log_name: str) -> tuple[int | None, bool, str, str]:
            seen.append((workdir / "test_hidden.py").exists())
            return 0, False, "", ""

        task = {"test": "pytest", "hidden_tests": "hidden"}
        result = HiddenTestsGrader({}).grade(GradeContext(workdir=workdir, task=task, base=base, changes=(), run=run))
        self.assertEqual((seen, result.passed, result.score, result.grader), ([True], True, 1.0, "hidden_tests"))

    def test_without_hidden_tests_only_the_command_runs_with_the_task_timeout(self) -> None:
        run = FakeRun(rc=1)
        result = HiddenTestsGrader({}).grade(context(run, {"test": "t", "test_timeout": 5}))
        self.assertEqual((run.calls, result.passed, result.score), ([("t", 5, "test")], False, 0.0))

    def test_default_timeout_is_900(self) -> None:
        run = FakeRun()
        HiddenTestsGrader({}).grade(context(run, {"test": "t"}))
        self.assertEqual(run.calls[0][1], 900)

    def test_timeout_fails_the_grader_and_is_flagged(self) -> None:
        result = HiddenTestsGrader({}).grade(context(FakeRun(rc=-1, timed_out=True), {"test": "t"}))
        self.assertEqual((result.passed, result.timed_out, result.score), (False, True, 0.0))

    def test_a_missing_exit_code_is_not_a_pass(self) -> None:
        result = HiddenTestsGrader({}).grade(context(FakeRun(rc=None), {"test": "t"}))
        self.assertFalse(result.passed)

    def test_score_line_gives_partial_credit_only_when_the_grader_opts_in(self) -> None:
        run = FakeRun(rc=1, output="3 of 4\nSCORE: 0.75\n")
        opted = HiddenTestsGrader({"score": True}).grade(context(run, {"test": "t"}))
        plain = HiddenTestsGrader({}).grade(context(run, {"test": "t"}))
        self.assertEqual(((opted.passed, opted.score), (plain.passed, plain.score)), ((False, 0.75), (False, 0.0)))

    def test_without_the_opt_in_a_stdout_score_is_ignored_on_a_pass_too(self) -> None:
        result = HiddenTestsGrader({}).grade(context(FakeRun(rc=0, output="SCORE: 0.2\n"), {"test": "t"}))
        self.assertEqual((result.passed, result.score), (True, 1.0))

    def test_an_earlier_score_line_from_agent_code_loses_to_the_last_one(self) -> None:
        out = "SCORE: 1.0\nreal run\nSCORE: 0.5\n"
        result = HiddenTestsGrader({"score": True}).grade(context(FakeRun(rc=1, output=out), {"test": "t"}))
        self.assertEqual(result.score, 0.5)

    def test_a_score_printed_to_stderr_is_never_read(self) -> None:
        run = FakeRun(rc=1, output="SCORE: 0.1\n", stderr="SCORE: 1.0\n")
        result = HiddenTestsGrader({"score": True}).grade(context(run, {"test": "t"}))
        self.assertEqual((result.passed, result.score), (False, 0.1))

    def test_an_out_of_range_score_falls_back_to_pass_fail(self) -> None:
        with fixtures.quiet() as out:
            run = FakeRun(output="SCORE: 7")
            result = HiddenTestsGrader({"score": True}).grade(context(run, {"test": "t"}))
        self.assertEqual((result.passed, result.score, "ignored SCORE" in out.getvalue()), (True, 1.0, True))

    def test_a_timeout_fails_even_when_the_exit_code_is_zero(self) -> None:
        result = HiddenTestsGrader({}).grade(context(FakeRun(rc=0, timed_out=True), {"test": "t"}))
        self.assertEqual((result.passed, result.timed_out), (False, True))

    def test_a_score_option_that_is_not_a_boolean_is_rejected(self) -> None:
        for bad in ("yes", 1, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                HiddenTestsGrader({"score": bad})


class CommandGraderTests(unittest.TestCase):
    def test_runs_the_command_with_its_timeout_and_log_name(self) -> None:
        run = FakeRun()
        result = CommandGrader({"command": "make lint", "timeout": 30}).grade(context(run))
        self.assertEqual((run.calls, result.passed, result.grader), ([("make lint", 30, "command")], True, "command"))

    def test_default_timeout_is_900_and_failure_scores_zero(self) -> None:
        run = FakeRun(rc=2)
        result = CommandGrader({"command": "x"}).grade(context(run))
        self.assertEqual((run.calls[0][1], result.passed, result.score), (900, False, 0.0))

    def test_timeout_and_score_follow_the_same_rules_as_hidden_tests(self) -> None:
        run = FakeRun(rc=-1, timed_out=True, output="SCORE: 0.2")
        result = CommandGrader({"command": "x", "score": True}).grade(context(run))
        self.assertEqual((result.passed, result.timed_out, result.score), (False, True, 0.2))

    def test_stderr_score_with_a_failing_exit_code_does_not_pass_under_weighted(self) -> None:
        run = FakeRun(rc=1, output="1 failed\n", stderr="SCORE: 1.0\n")
        for cfg in ({"command": "x"}, {"command": "x", "score": True}):
            graded = CommandGrader(cfg).grade(context(run))
            self.assertEqual(compose([graded], "weighted", [1], 0.5)[0], False, msg=str(cfg))

    def test_a_stdout_score_without_the_opt_in_cannot_lift_a_failing_grader(self) -> None:
        graded = CommandGrader({"command": "x"}).grade(context(FakeRun(rc=1, output="SCORE: 1.0\n")))
        self.assertEqual((compose([graded], "weighted", [1], 0.5), graded.score), ((False, 0.0), 0.0))

    def test_bad_config_is_rejected(self) -> None:
        for cfg in (
            {},
            {"command": ""},
            {"command": 3},
            {"command": "x", "timeout": 0},
            {"command": "x", "timeout": "5"},
            {"command": "x", "score": "yes"},
        ):
            with self.assertRaises(ValueError, msg=str(cfg)):
                CommandGrader(cfg)


def changes_of(*paths: str, lines: int = 1) -> tuple[Change, ...]:
    return tuple(Change(path=p, added=lines, deleted=0) for p in paths)


class DiffConstraintsGraderTests(unittest.TestCase):
    def grade(self, cfg: Mapping[str, Any], changes: tuple[Change, ...]) -> GraderResult:
        run = FakeRun()
        result = DiffConstraintsGrader(cfg).grade(context(run, changes=changes))
        self.assertEqual(run.calls, [])  # it runs no command
        return result

    def test_no_constraints_pass_everything(self) -> None:
        self.assertEqual(self.grade({}, changes_of("a.py", "b/c.py")).passed, True)

    def test_a_change_to_a_forbidden_path_fails(self) -> None:
        result = self.grade({"forbidden": ["run_tests.py", "tests/*"]}, changes_of("src/a.py", "tests/x.py"))
        self.assertEqual((result.passed, result.score), (False, 0.0))

    def test_only_allowed_paths_may_change(self) -> None:
        cfg = {"allowed": ["src/*.py"]}
        self.assertTrue(self.grade(cfg, changes_of("src/a.py")).passed)
        self.assertFalse(self.grade(cfg, changes_of("src/a.py", "README.md")).passed)

    def test_forbidden_wins_over_allowed(self) -> None:
        cfg = {"allowed": ["src/*"], "forbidden": ["src/secret.py"]}
        self.assertFalse(self.grade(cfg, changes_of("src/secret.py")).passed)

    def test_max_changed_lines_counts_added_and_deleted(self) -> None:
        cfg = {"max_changed_lines": 10}
        self.assertTrue(self.grade(cfg, (Change("a", 6, 4),)).passed)
        self.assertFalse(self.grade(cfg, (Change("a", 6, 5),)).passed)
        self.assertFalse(self.grade(cfg, (Change("a", 6, 0), Change("b", 5, 0))).passed)

    def test_paths_outside_the_workdir_count_as_forbidden_even_without_a_forbidden_list(self) -> None:
        for path in ("../x", "/etc/passwd", "a/../../x", ".."):
            self.assertFalse(self.grade({}, changes_of(path)).passed, path)

    def test_paths_are_normalized_before_matching(self) -> None:
        cfg = {"forbidden": ["run_tests.py"]}
        for path in ("./run_tests.py", "src/../run_tests.py"):
            self.assertFalse(self.grade(cfg, changes_of(path)).passed, path)

    def test_a_rename_is_seen_as_both_sides(self) -> None:
        cfg = {"forbidden": ["run_tests.py"]}
        self.assertFalse(self.grade(cfg, changes_of("run_tests.py", "renamed.py")).passed)

    def test_violations_are_listed_for_the_log(self) -> None:
        grader = DiffConstraintsGrader({"forbidden": ["a"], "max_changed_lines": 0})
        found = grader.violations(changes_of("a"))
        self.assertEqual(len(found), 2)

    def test_ignore_lines_drops_matching_paths_from_the_count_only(self) -> None:
        pyc = Change("tests/__pycache__/t.pyc", 10**9, 0)
        cfg = {"max_changed_lines": 50, "ignore_lines": ["*/__pycache__/*"]}
        self.assertTrue(self.grade(cfg, (pyc, Change("src/a.py", 5, 5))).passed)
        self.assertFalse(self.grade({"max_changed_lines": 50}, (pyc,)).passed)
        self.assertFalse(self.grade({**cfg, "forbidden": ["tests/*"]}, (pyc,)).passed)
        self.assertFalse(self.grade({**cfg, "allowed": ["src/*"]}, (pyc,)).passed)
        self.assertFalse(self.grade(cfg, (pyc, Change("src/a.py", 30, 30))).passed)

    def test_bad_config_is_rejected(self) -> None:
        for cfg in (
            {"ignore_lines": "*.pyc"},
            {"ignore_lines": [1]},
            {"allowed": "src/*"},
            {"forbidden": [1]},
            {"max_changed_lines": -1},
            {"max_changed_lines": True},
            {"max_changed_lines": "5"},
        ):
            with self.assertRaises(ValueError, msg=str(cfg)):
                DiffConstraintsGrader(cfg)


class GradePlanTests(unittest.TestCase):
    TASK = {"id": "t", "test": "true"}

    def test_a_task_without_graders_gets_the_hidden_tests_grader_and_mode_all(self) -> None:
        plan = grade_plan(self.TASK)
        self.assertEqual(
            ([g.name for g in plan.graders], plan.mode, plan.weights, plan.threshold),
            (["hidden_tests"], "all", (1,), None),
        )

    def test_graders_weights_and_grade_block_are_read(self) -> None:
        task = {
            **self.TASK,
            "graders": [{"type": "hidden_tests", "weight": 3}, {"type": "command", "command": "x"}],
            "grade": {"mode": "weighted", "threshold": 0.6},
        }
        plan = grade_plan(task)
        self.assertEqual(
            ([g.name for g in plan.graders], plan.weights, plan.mode, plan.threshold),
            (["hidden_tests", "command"], (3, 1), "weighted", 0.6),
        )

    def test_unknown_type_names_the_type_and_the_built_ins(self) -> None:
        with self.assertRaises(ValueError) as cm:
            grader_for({"type": "nope"})
        self.assertIn("'nope'", str(cm.exception))
        self.assertIn("hidden_tests", str(cm.exception))
        self.assertEqual(GRADER_GROUP, "harness_bench.graders")

    def test_bad_plans_are_rejected(self) -> None:
        bad: list[dict[str, Any]] = [
            {"graders": []},
            {"graders": "hidden_tests"},
            {"graders": ["hidden_tests"]},
            {"graders": [{"kind": "command"}]},
            {"graders": [{"type": "command"}]},
            {"graders": [{"type": "hidden_tests", "weight": 0}]},
            {"graders": [{"type": "hidden_tests", "weight": "2"}]},
            {"grade": "all"},
            {"grade": {"mode": "median"}},
            {"grade": {"mode": "weighted"}},
            {"grade": {"mode": "weighted", "threshold": 2}},
            {"grade": {"mode": "weighted", "threshold": True}},
        ]
        for extra in bad:
            with self.assertRaises(ValueError, msg=str(extra)):
                grade_plan({**self.TASK, **extra})

    def test_hidden_tests_without_a_test_command_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            grade_plan({"id": "t"})
        grade_plan({"id": "t", "graders": [{"type": "command", "command": "x"}]})


if __name__ == "__main__":
    unittest.main()
```

`tests/test_plugins.py`:

```python
"""Entry-point plugins: loading, error reporting and an end-to-end run with a plugin grader and runner."""

import contextlib
import importlib
import sys
import textwrap
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import TypeGuard
from unittest import mock

import fixtures
import harness_bench as hb
from hbench import plugins
from hbench.graders import Grader, grader_for
from hbench.plugins import GRADER_GROUP, RUNNER_GROUP, PluginError, create_plugin, load_plugin
from hbench.runners import runner_for
from test_run import RunTestCase, args, rows_of

DEMO_MODULE = """
from hbench.core.models import GraderResult


class DemoGrader:
    name = "demo"
    version = "9"

    def __init__(self, cfg):
        self.passes = cfg.get("passes", True)

    def grade(self, ctx):
        return GraderResult(grader=self.name, grader_version=self.version, passed=self.passes, score=0.5)


class LogNameGrader:
    name = "logname"
    version = "1"

    def __init__(self, cfg):
        pass

    def grade(self, ctx):
        rc, timed_out, stdout, stderr = ctx.run("true", 30, "../../escape/..//x")
        return GraderResult(grader=self.name, grader_version=self.version, passed=rc == 0, score=1.0)


class DemoRunner:
    name = "demo-runner"

    def __init__(self, cfg):
        self.cfg = cfg

    def prepare(self, workdir):
        (workdir / "prepared.txt").write_text("hello")

    def command(self, model, prompt):
        return [part.format(model=model, model_name=model, provider="", prompt=prompt) for part in self.cfg["command"]]

    def parse_usage(self, stdout):
        return None

    def version(self):
        return "7.7"


def grader_factory(cfg):
    return DemoGrader(cfg)


def logname_factory(cfg):
    return LogNameGrader(cfg)


def runner_factory(cfg):
    return DemoRunner(cfg)


def not_a_grader(cfg):
    return object()


def raising_factory(cfg):
    raise RuntimeError("bad config")
"""


@contextlib.contextmanager
def installed(
    test: unittest.TestCase, entry_points: str, module: str = DEMO_MODULE, dist: str = "demo_plugin"
) -> Iterator[Path]:
    """A throwaway distribution on sys.path: metadata with `entry_points` and a `demo_plugin` module."""
    root = fixtures.tmpdir(test)
    info = root / f"{dist}-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.1\n")
    (info / "entry_points.txt").write_text(textwrap.dedent(entry_points))
    (root / "demo_plugin.py").write_text(module)
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    plugins.reset_announced()
    try:
        yield root
    finally:
        sys.path.remove(str(root))
        sys.modules.pop("demo_plugin", None)
        importlib.invalidate_caches()


def is_grader(obj: object) -> TypeGuard[Grader]:
    return isinstance(obj, Grader)


GRADER_EP = f"[{GRADER_GROUP}]\ndemo = demo_plugin:grader_factory\n"


class LoadPluginTests(unittest.TestCase):
    def test_a_registered_factory_is_loaded_and_announced_once(self) -> None:
        with installed(self, GRADER_EP):
            with fixtures.quiet() as out:
                factory = load_plugin(GRADER_GROUP, "demo", ("command",))
                load_plugin(GRADER_GROUP, "demo", ("command",))
            self.assertTrue(callable(factory))
            self.assertEqual(out.getvalue(), f"plugin {GRADER_GROUP}:demo from demo_plugin 0.1\n")

    def test_an_unknown_type_names_the_type_the_group_and_the_built_ins(self) -> None:
        with self.assertRaises(PluginError) as cm:
            load_plugin(GRADER_GROUP, "nope", ("command", "hidden_tests"))
        message = str(cm.exception)
        self.assertTrue(
            all(part in message for part in ("grader", "'nope'", GRADER_GROUP, "command, hidden_tests")), message
        )

    def test_a_plugin_with_a_built_in_name_is_an_error(self) -> None:
        with installed(self, f"[{GRADER_GROUP}]\ncommand = demo_plugin:grader_factory\n"):
            with self.assertRaises(PluginError) as cm:
                load_plugin(GRADER_GROUP, "other", ("command",))
        self.assertIn("built-in name", str(cm.exception))
        self.assertIn("demo_plugin", str(cm.exception))

    def test_two_plugins_with_the_same_name_are_ambiguous(self) -> None:
        with installed(self, GRADER_EP), installed(self, GRADER_EP, dist="demo_other"):
            with self.assertRaises(PluginError) as cm:
                load_plugin(GRADER_GROUP, "demo", ())
        self.assertIn("more than one", str(cm.exception))

    def test_an_import_error_is_wrapped_with_the_plugin_name(self) -> None:
        with installed(self, GRADER_EP, module="raise RuntimeError('broken import')\n"):
            with self.assertRaises(PluginError) as cm:
                load_plugin(GRADER_GROUP, "demo", ())
        self.assertTrue(all(p in str(cm.exception) for p in (f"{GRADER_GROUP}:demo", "RuntimeError", "broken import")))

    def test_an_entry_point_that_is_not_callable_is_an_error(self) -> None:
        with installed(self, f"[{GRADER_GROUP}]\ndemo = demo_plugin:DEMO\n", module="DEMO = 3\n"):
            with self.assertRaises(PluginError) as cm:
                load_plugin(GRADER_GROUP, "demo", ())
        self.assertIn("not callable", str(cm.exception))

    def test_a_factory_that_raises_is_wrapped(self) -> None:
        with installed(self, f"[{GRADER_GROUP}]\ndemo = demo_plugin:raising_factory\n"):
            with self.assertRaises(PluginError) as cm, fixtures.quiet():
                grader_for({"type": "demo"})
        self.assertTrue(all(p in str(cm.exception) for p in ("demo", "RuntimeError", "bad config")))

    def test_a_result_of_the_wrong_type_is_an_error_naming_the_plugin(self) -> None:
        with installed(self, f"[{GRADER_GROUP}]\ndemo = demo_plugin:not_a_grader\n"):
            with self.assertRaises(PluginError) as cm, fixtures.quiet():
                grader_for({"type": "demo"})
        self.assertTrue(all(p in str(cm.exception) for p in (f"{GRADER_GROUP}:demo", "object", "Grader")))

    def test_create_plugin_checks_the_result_with_the_given_guard(self) -> None:
        with installed(self, GRADER_EP), fixtures.quiet():
            built = create_plugin(GRADER_GROUP, "demo", (), {}, is_grader, "Grader")
        self.assertEqual(built.name, "demo")


class RegistryFallbackTests(unittest.TestCase):
    def test_a_grader_type_that_is_not_built_in_comes_from_a_plugin(self) -> None:
        with installed(self, GRADER_EP), fixtures.quiet():
            grader = grader_for({"type": "demo", "passes": False})
        self.assertEqual((grader.name, grader.version), ("demo", "9"))

    def test_a_runner_parser_that_is_not_built_in_comes_from_a_plugin(self) -> None:
        with installed(self, f"[{RUNNER_GROUP}]\ndemo-runner = demo_plugin:runner_factory\n"), fixtures.quiet():
            runner = runner_for({"parser": "demo-runner", "command": ["x"]})
        self.assertEqual((runner.name, runner.version()), ("demo-runner", "7.7"))

    def test_unknown_runner_or_grader_gives_a_plugin_error_naming_the_group(self) -> None:
        with self.assertRaises(PluginError) as cm:
            runner_for({"parser": "nope", "command": ["x"]})
        self.assertIn(RUNNER_GROUP, str(cm.exception))

    def test_a_harness_without_a_parser_or_a_valid_command_is_rejected(self) -> None:
        for cfg in (
            {"command": ["x"]},
            {"parser": 3, "command": ["x"]},
            {"parser": "pi"},
            {"parser": "pi", "command": "x"},
        ):
            with self.assertRaises(ValueError, msg=str(cfg)):
                runner_for(cfg)


class EndToEndTests(RunTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg["matrix"] = [{"harness": "claude", "model": "fix"}]

    def test_a_plugin_grader_grades_a_task_through_cmd_run_without_core_changes(self) -> None:
        self.cfg["tasks"][0]["graders"] = [{"type": "demo"}]
        with installed(self, GRADER_EP):
            out = fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (row,) = rows_of(self.base)
        trial = fixtures.read_rows(next((self.base / "results").glob("*/trials.jsonl")))[0]
        self.assertEqual(
            (row["passed"], trial["graders"][0]["grader"], trial["graders"][0]["score"]), (True, "demo", 0.5)
        )
        self.assertIn("plugin harness_bench.graders:demo from demo_plugin 0.1", out)

    def test_a_plugin_runner_builds_the_command_prepares_the_workdir_and_names_the_version(self) -> None:
        script = self.base / "agent.py"
        script.write_text("import os, sys\nsys.exit(0 if os.path.exists('prepared.txt') else 3)\n")
        self.cfg["harnesses"]["claude"] = {"parser": "demo-runner", "command": [fixtures.PY, str(script)]}
        self.cfg["tasks"][0]["graders"] = [
            {"type": "command", "command": "test -f prepared.txt"},
            {"type": "diff_constraints", "forbidden": ["prepared.txt"]},
        ]
        with installed(self, f"[{RUNNER_GROUP}]\ndemo-runner = demo_plugin:runner_factory\n"):
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        (row,) = rows_of(self.base)
        run_json = next((self.base / "results").glob("*/run.json")).read_text()
        diff = next((self.base / "results" / "logs").glob("*.diff")).read_text()
        self.assertEqual(
            (row["passed"], row["agent_rc"], row["parse_ok"], '"harness_version": "7.7"' in run_json),
            (True, 0, False, True),
        )
        # `prepare` runs before the baseline, so its file is part of the starting state, not an agent change.
        self.assertNotIn("prepared.txt", diff)

    def test_a_plugin_grader_cannot_write_its_log_outside_the_logs_dir(self) -> None:
        self.cfg["tasks"][0]["graders"] = [{"type": "logname"}]
        with installed(self, f"[{GRADER_GROUP}]\nlogname = demo_plugin:logname_factory\n"):
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        logs = self.base / "results" / "logs"
        written = sorted(p.name for p in logs.glob("*grader-0-*"))
        outside = [p for p in self.base.rglob("*escape*") if not p.is_relative_to(logs)]
        self.assertEqual((len(written), "/" in written[0], outside), (1, False, []))

    def test_an_unknown_grader_type_exits_before_any_agent_runs(self) -> None:
        self.cfg["tasks"][0]["graders"] = [{"type": "nope"}]
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        message = str(cm.exception.code)
        self.assertTrue(all(p in message for p in ("task fizzbuzz", "'nope'", GRADER_GROUP)), message)
        self.assertFalse((self.base / "results" / "results.jsonl").exists())

    def test_an_unknown_runner_exits_before_any_agent_runs(self) -> None:
        self.cfg["harnesses"]["claude"]["parser"] = "nope"
        with self.assertRaises(SystemExit) as cm:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertTrue(all(p in str(cm.exception.code) for p in ("harness claude", "'nope'", RUNNER_GROUP)))
        self.assertFalse((self.base / "results" / "results.jsonl").exists())

    def test_a_plugin_that_fails_to_load_exits_with_a_message_not_a_traceback(self) -> None:
        self.cfg["tasks"][0]["graders"] = [{"type": "demo"}]
        with installed(self, GRADER_EP, module="raise RuntimeError('broken import')\n"):
            with self.assertRaises(SystemExit) as cm:
                fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        self.assertIn("broken import", str(cm.exception.code))

    def test_a_plain_config_never_looks_for_plugins(self) -> None:
        with mock.patch.object(plugins, "entry_points") as entry_points:
            fixtures.call_quiet(hb.cmd_run, args(), self.cfg, self.base)
        entry_points.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

## Sandbox

`sandbox/Dockerfile`:

```dockerfile
# Sandbox image for harness-bench: coding-agent CLIs + git + Python 3.12 + Node.js 22.
#
# Build (from the repo root; UID/GID must match the host user that owns the workdir):
#   docker build -f sandbox/Dockerfile -t harness-bench-agents \
#     --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
#
# Pin CLI versions for reproducible runs, e.g. --build-arg CLAUDE_CODE_VERSION=2.1.0
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

# Node.js runtime from the official image (the npm CLIs below need it).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ARG UID=1000
ARG GID=1000
ARG CLAUDE_CODE_VERSION=latest
ARG PI_VERSION=latest
ARG OPENCODE_VERSION=latest
ARG CODEX_VERSION=latest
ARG INSTALL_CURSOR=false

# git: workdir baseline/diff; tini: PID 1. Python 3.12 comes from the base image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates curl bash tini \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-fund --no-audit \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "opencode-ai@${OPENCODE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
 && npm install -g --no-fund --no-audit --ignore-scripts \
      "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && npm cache clean --force

# Unprivileged user with the host UID/GID so files written in the bind-mounted
# workdir stay owned by the host user.
RUN if getent passwd "${UID}" >/dev/null; then userdel -r "$(getent passwd "${UID}" | cut -d: -f1)"; fi \
 && if ! getent group "${GID}" >/dev/null; then groupadd -g "${GID}" agent; fi \
 && useradd -m -u "${UID}" -g "${GID}" -s /bin/bash agent

# Harness config templates. entrypoint.sh renders them with LITELLM_BASE_URL at start.
COPY pi-models.example.json      /opt/harness-bench/templates/pi-models.json
COPY opencode.example.json       /opt/harness-bench/templates/opencode.json
COPY codex-config.example.toml   /opt/harness-bench/templates/codex-config.toml
COPY sandbox/entrypoint.sh       /usr/local/bin/harness-bench-entrypoint
RUN chmod 0755 /usr/local/bin/harness-bench-entrypoint

USER agent
ENV HOME=/home/agent \
    PATH=/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Cursor CLI is optional: the LiteLLM-only benchmark does not use it.
RUN if [ "${INSTALL_CURSOR}" = "true" ]; then curl -fsSL https://cursor.com/install | bash; fi

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/harness-bench-entrypoint"]
CMD ["bash"]
```

`sandbox/entrypoint.sh`:

```sh
#!/bin/sh
# Render harness configs from templates, then run the given command.
# Needs LITELLM_BASE_URL (proxy root, no /v1), e.g. https://litellm.example.com
set -eu

render() {  # template -> destination, replacing __LITELLM_BASE_URL__
    mkdir -p "$(dirname "$2")"
    sed "s#__LITELLM_BASE_URL__#${LITELLM_BASE_URL%/}#g" "$1" > "$2"
}

if [ -n "${LITELLM_BASE_URL:-}" ]; then
    t=/opt/harness-bench/templates
    render "$t/pi-models.json"     "$HOME/.pi/agent/models.json"
    render "$t/opencode.json"      "$HOME/.config/opencode/opencode.json"
    render "$t/codex-config.toml"  "${CODEX_HOME:-$HOME/.codex}/config.toml"
fi

exec "$@"
```

`.dockerignore`:

```
# Build context allowlist: configs, results and tasks never reach the Docker daemon.
*
!sandbox/
!pi-models.example.json
!opencode.example.json
!codex-config.example.toml
```

`tests/docker_e2e.py`:

```python
#!/usr/bin/env python3
"""End-to-end test of the Docker sandbox with fake agents (no API keys, no network calls).

Needs the image: docker build -f sandbox/Dockerfile -t harness-bench-agents \\
                   --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
Run: python3 tests/docker_e2e.py   (exit 0 = pass)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = os.environ.get("HB_IMAGE", "harness-bench-agents")


def check(cond, msg):
    if not cond:
        sys.exit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def main():
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        sys.exit(f"image {IMAGE} not found; build it first (see docstring)")
    tmp = Path(tempfile.mkdtemp(prefix="hb-docker-"))
    shared = tmp / "shared"  # probe/tag files written from inside the container
    shared.mkdir()
    try:
        shutil.copy(ROOT / "harness_bench.py", tmp)
        shutil.copytree(ROOT / "hbench", tmp / "hbench", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "tasks" / "fizzbuzz", tmp / "tasks" / "fizzbuzz")
        env = dict(
            os.environ,
            FAKE_TAGS_FILE=str(shared / "tags.txt"),
            FAKE_PROBE_FILE=str(shared / "probe.jsonl"),
            SECRET_CANARY="must-not-leak",
            FAKE_API_KEY="sk-test-agent-key-0123456789",
        )
        agent = str(ROOT / "tests" / "fake_agent.py")
        prefix = [
            "docker",
            "run",
            "--rm",
            "--env-file",
            "{env_file}",
            "-v",
            "{workdir}:{workdir}",
            "-w",
            "{workdir}",
            "-v",
            f"{ROOT / 'tests'}:{ROOT / 'tests'}:ro",
            "-v",
            f"{shared}:{shared}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "1g",
            IMAGE,
        ]
        cfg = {
            "reps": 1,
            "sandbox": {"prefix": prefix},
            "harnesses": {
                k: {
                    "parser": k,
                    "command": ["python3", agent, k, mode],
                    "env": {
                        "FAKE_TAGS_FILE": "${FAKE_TAGS_FILE}",
                        "FAKE_PROBE_FILE": "${FAKE_PROBE_FILE}",
                        "FAKE_API_KEY": "${FAKE_API_KEY}",
                    },
                }
                for k, mode in (("claude", "fix"), ("pi", "nofix"))
            },
            "matrix": [{"harness": "claude", "model": "m"}, {"harness": "pi", "model": "p/m"}],
            "tasks": [
                {
                    "id": "fizzbuzz",
                    "repo": "tasks/fizzbuzz/repo",
                    "prompt": "fix it",
                    "hidden_tests": "tasks/fizzbuzz/hidden_tests",
                    "test": "python3 -m unittest -q test_hidden.py",
                }
            ],
        }
        (tmp / "config.json").write_text(json.dumps(cfg))
        p = subprocess.run(
            [sys.executable, "harness_bench.py", "run", "--config", "config.json"],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
        )
        if p.returncode:
            sys.exit(f"FAIL: run\n{p.stdout}\n{p.stderr}")
        rows = {r["harness"]: r for r in map(json.loads, (tmp / "results/results.jsonl").read_text().splitlines())}
        check(rows["claude"]["passed"] and not rows["pi"]["passed"], "tests run inside container decide pass/fail")
        check(all(r["sandboxed"] and r["agent_rc"] == 0 for r in rows.values()), "agents ran sandboxed, no secret leak")
        probes = [json.loads(x) for x in (shared / "probe.jsonl").read_text().splitlines()]
        check(all(pr["home"] == "/home/agent" for pr in probes), "container HOME, not host HOME")
        check(all(pr["uid"] == os.getuid() for pr in probes), "container uid matches host uid")
        check(all(pr["depth"] == "1" for pr in probes), "git baseline visible inside container")
        logs = tmp / "results" / "logs"
        check(
            "sk-test-agent-key" not in "".join(f.read_text() for f in logs.glob("*.agent.stderr")),
            "secrets redacted in logs",
        )
        check(not list(Path(tempfile.gettempdir()).glob("harness-bench-work-*/.*.env")), "env files removed")
        print("\nPASS")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
```

## References

- Claude Code + LiteLLM (headers and tags): https://docs.litellm.ai/docs/tutorials/claude_code_customer_tracking
- LiteLLM spend tracking: https://docs.litellm.ai/docs/proxy/cost_tracking
- Pi custom models / headers: https://cdn.jsdelivr.net/npm/@earendil-works/pi-coding-agent@0.81.1/docs/models.md
- Pi JSON mode: https://cdn.jsdelivr.net/npm/@earendil-works/pi-coding-agent@0.81.1/docs/json.md
- OpenCode + LiteLLM: https://docs.litellm.ai/docs/tutorials/opencode_integration
- OpenCode stream JSON: https://littlebearapps.com/help/untether/opencode-stream-json-cheatsheet/
- OpenSandbox: https://github.com/opensandbox-group/OpenSandbox
- Codex exec JSONL: https://codex.danielvaughan.com/2026/04/08/codex-exec-jsonl-reference/
- Cursor CLI output format: https://cursor.com/docs/cli/reference/output-format
- Cursor CLI parameters: https://cursor.com/docs/cli/reference/parameters

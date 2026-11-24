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
- `summary.md` and `summary.csv`: table sorted by cost per solved task, plus the per-task pass matrix;
- `logs/`: each agent's JSON output, stderr, setup, tests, and the `.diff` of everything the agent changed. Known secrets show as `***REDACTED***`.

`run` only starts with `sandbox.prefix` set (see [Security](#security)). Inside a disposable VM, add `--allow-unsandboxed`.

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
2. **No git history**: the task repository is copied without `.git` and gets a single `baseline` commit. A clone at the commit before the fix would still contain the fix commit, and the remote would point back to the benchmark.
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

import argparse
import csv
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit("Python 3.12+ required")


# ---------------------------------------------------------------- helpers


def num(d, *keys):
    """First numeric value found under any of keys, else 0."""
    if not isinstance(d, dict):
        return 0
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return 0


def json_lines(text):
    """Yield JSON objects from a JSONL stream; skip non-JSON lines."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def empty_usage():
    return {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cost_reported": None,
        "turns": 0,
    }


def expand_env(value):
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


# ---------------------------------------------------------------- parsers
# Each parser takes the agent stdout and returns a usage dict or None.


def parse_claude(stdout):
    """claude -p --output-format json (single result object; stream-json also works)."""
    result = None
    for obj in json_lines(stdout):
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
    if isinstance(result.get("total_cost_usd"), (int, float)):
        u["cost_reported"] = result["total_cost_usd"]
    u["turns"] = num(result, "num_turns")
    return u


def parse_opencode(stdout):
    """opencode run --format json: sum every step_finish event."""
    u = empty_usage()
    cost, seen = 0.0, False
    for obj in json_lines(stdout):
        if str(obj.get("type", "")).replace("-", "_") != "step_finish":
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
    return u


def parse_pi(stdout):
    """pi --mode json: sum usage of assistant messages on message_end events only."""
    u = empty_usage()
    cost, seen, has_cost = 0.0, False, False
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
        if isinstance(c, dict) and isinstance(c.get("total"), (int, float)):
            cost += c["total"]
            has_cost = True
        elif isinstance(c, (int, float)):
            cost += c
            has_cost = True
        u["turns"] += 1
        seen = True
    if not seen:
        return None
    u["cost_reported"] = cost if has_cost else None
    return u


def parse_cursor(stdout):
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
    u["turns"] = 1
    return u


def parse_codex(stdout):
    """codex exec --json: usage on turn.completed is the thread's running total, so take
    the last one. cached_input_tokens is a subset of input_tokens; reasoning_output_tokens
    is a subset of output_tokens (not added again)."""
    last, turns = None, 0
    for obj in json_lines(stdout):
        if obj.get("type") == "turn.completed" and isinstance(obj.get("usage"), dict):
            last = obj["usage"]
            turns += 1
    if last is None:
        return None
    u = empty_usage()
    cached = num(last, "cached_input_tokens")
    u["input"] = max(num(last, "input_tokens") - cached, 0)
    u["cache_read"] = cached
    u["output"] = num(last, "output_tokens")
    u["turns"] = turns
    return u


PARSERS = {
    "claude": parse_claude,
    "opencode": parse_opencode,
    "pi": parse_pi,
    "cursor": parse_cursor,
    "codex": parse_codex,
}


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


def validate_config(cfg):
    for task in cfg["tasks"]:
        if SAFE_NAME.search(task["id"]) or task["id"].startswith("."):
            sys.exit(f"invalid task id {task['id']!r}: use only letters, digits, _ . - (no leading dot)")
        for field in ("repo", "ref"):
            if str(task.get(field, "")).startswith("-"):
                sys.exit(f"task {task['id']}: {field} must not start with '-'")
        for p in task.get("protected_paths", []):
            if Path(p).is_absolute() or ".." in Path(p).parts:
                sys.exit(f"task {task['id']}: protected path {p!r} must be relative, without '..'")
    for name in cfg["harnesses"]:
        if SAFE_NAME.search(name):
            sys.exit(f"invalid harness name {name!r}")
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


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True)


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


def make_baseline(workdir):
    """Single commit with the state the agent starts from (after setup). Returns its SHA."""
    ident = ["-c", "user.name=harness-bench", "-c", "user.email=bench@localhost", "-c", "commit.gpgsign=false"]
    git("init", "--quiet", "-b", "main", cwd=workdir)
    git("add", "-A", cwd=workdir)
    git(*ident, "commit", "--quiet", "--allow-empty", "-m", "baseline", cwd=workdir)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workdir, capture_output=True, text=True, check=True
    ).stdout.strip()


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


def save_diff(workdir, baseline, path, limit=2_000_000):
    """Everything the agent changed vs baseline, including new files (for audit)."""
    try:
        git("add", "-A", cwd=workdir)
        p = subprocess.run(
            ["git", "diff", "--cached", "--stat", "--patch", baseline],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=300,
        )
        text = p.stdout if p.returncode == 0 else f"git diff failed (agent changed .git?):\n{p.stderr}"
    except (subprocess.SubprocessError, OSError) as e:
        text = f"git diff failed (agent changed .git?): {e}"
    path.write_text(text[:limit] + ("\n[truncated]" if len(text) > limit else ""))


def build_cmd(template, model, prompt):
    provider, _, model_name = model.partition("/") if "/" in model else ("", "", model)
    fields = {"model": model, "provider": provider, "model_name": model_name, "prompt": prompt}
    return [part.format(**fields) for part in template]


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


def cmd_run(args, cfg, base):
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
                cmd = build_cmd(h["command"], model, task["prompt"])
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

                def step(command, env, timeout, log_name):
                    write_env_file(env, env_file)
                    try:
                        rc, to, out, err = run_proc(
                            wrap(cfg, ["sh", "-c", command], workdir, env_file), workdir, env, timeout
                        )
                    finally:
                        env_file.unlink(missing_ok=True)
                    (logs_dir / log_name).write_text(redact(out + "\n--- stderr ---\n" + err, secrets))
                    return rc, to

                if task.get("setup"):
                    step(task["setup"], cmd_env, task.get("setup_timeout", 900), f"{tag}.setup.log")
                baseline = make_baseline(workdir)
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

                usage = PARSERS[h["parser"]](stdout)
                parse_ok = usage is not None
                usage = usage or empty_usage()

                save_diff(workdir, baseline, logs_dir / f"{tag}.diff")
                restore_protected(task, workdir, snap)
                rmtree_inside(snap, work_root)
                if task.get("hidden_tests"):
                    src = (base / task["hidden_tests"]).resolve()
                    shutil.copytree(src, workdir, dirs_exist_ok=True)
                test_rc, test_to = step(task["test"], cmd_env, task.get("test_timeout", 900), f"{tag}.test.log")

                row = {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "task": task["id"],
                    "harness": hname,
                    "model": model,
                    "rep": rep,
                    "passed": test_rc == 0 and not test_to,
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
                with results_path.open("a") as f:
                    f.write(json.dumps(row) + "\n")
                print(
                    f"done  {tag} passed={row['passed']} tokens="
                    f"{usage['input'] + usage['output'] + usage['reasoning'] + usage['cache_read'] + usage['cache_write']}"
                    f" cost={row['cost_computed'] if row['cost_computed'] is not None else usage['cost_reported']}",
                    flush=True,
                )
                if not args.keep:
                    rmtree_inside(workdir, work_root)
    if work_root and not args.keep and not cfg.get("work_dir"):
        shutil.rmtree(work_root, ignore_errors=True)
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
    for r in runs:
        hits = by_tag.get(r.get("run_tag"), [])
        if hits:
            matched += 1
            r["cost_litellm"] = sum(num(h, "spend") for h in hits)
            r["litellm_requests"] = len(hits)
            r["litellm_prompt_tokens"] = sum(num(h, "prompt_tokens") for h in hits)
            r["litellm_completion_tokens"] = sum(num(h, "completion_tokens") for h in hits)
    results_path.write_text("".join(json.dumps(r) + "\n" for r in runs))
    print(f"LiteLLM: {len(logs)} log rows, {matched}/{len(runs)} runs matched by tag")
    if matched < len(tagged):
        print(
            "Unmatched runs: harness did not send x-litellm-tags, run not via LiteLLM "
            "(e.g. Cursor), or logs not written yet."
        )
    cmd_report(args, cfg, base)


# ---------------------------------------------------------------- report


def fmt_money(v):
    if v is None:
        return "n/a"
    if math.isinf(v):
        return "inf"
    return f"${v:.4f}" if v < 1 else f"${v:.2f}"


def fmt_int(v):
    return "n/a" if v is None or math.isinf(v) else f"{v:,.0f}"


def aggregate(rows, cost_source):
    groups = {}
    for r in rows:
        groups.setdefault((r["harness"], r["model"]), []).append(r)
    table = []
    for (h, m), rs in groups.items():
        n = len(rs)
        solved = sum(1 for r in rs if r["passed"])
        tokens = sum(r["input"] + r["output"] + r.get("reasoning", 0) + r["cache_read"] + r["cache_write"] for r in rs)
        fresh_in = sum(r["input"] for r in rs)
        cache_read = sum(r["cache_read"] for r in rs)

        def run_cost(r):
            if cost_source != "auto":
                v = r.get(f"cost_{cost_source}")
                return v, (cost_source if v is not None else None)
            for src in ("litellm", "computed", "reported"):
                if r.get(f"cost_{src}") is not None:
                    return r[f"cost_{src}"], src
            return None, None

        picked = [run_cost(r) for r in rs]
        costs = [c for c, _ in picked]
        cost = None if any(c is None for c in costs) else sum(costs)
        sources = sorted({src for _, src in picked if src})
        table.append(
            {
                "harness": h,
                "model": m,
                "cost_source": "+".join(sources) or "n/a",
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
                "avg_turns": sum(r["turns"] for r in rs) / n,
                "avg_wall_s": sum(r["wall_s"] for r in rs) / n,
                "parse_failures": sum(1 for r in rs if not r["parse_ok"]),
            }
        )

    def sort_key(t):
        c = t["cost_per_solved"]
        return (c is None, c if c is not None else 0, -t["pass_rate"])

    table.sort(key=sort_key)
    return table


def cmd_report(args, cfg, base):
    out_dir = base / cfg.get("output_dir", "results")
    results_path = out_dir / "results.jsonl"
    if not results_path.exists():
        sys.exit(f"no results at {results_path}")
    rows = list(json_lines(results_path.read_text()))
    cost_source = getattr(args, "cost_source", None) or cfg.get("cost_source", "auto")
    table = aggregate(rows, cost_source)

    with (out_dir / "summary.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(table[0].keys()))
        w.writeheader()
        w.writerows(table)

    head = (
        "| # | Harness | Model | Solved | Pass rate | Cost/solved | Cost/run "
        "| Cost/1M tokens | Tokens/solved | Tokens/run | Cache hit | Turns | Time (s) "
        "| Cost source |"
    )
    lines = [
        f"# Harness x model — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"Cost source: `{cost_source}` (auto = litellm > computed > reported). Sorted by cost per solved task.",
        "",
        head,
        "|" + "---|" * 14,
    ]
    for i, t in enumerate(table, 1):
        lines.append(
            f"| {i} | {t['harness']} | {t['model']} | {t['solved']}/{t['runs']} "
            f"| {t['pass_rate']:.0%} | {fmt_money(t['cost_per_solved'])} | {fmt_money(t['cost_per_run'])} "
            f"| {fmt_money(t['cost_per_1m_tokens'])} | {fmt_int(t['tokens_per_solved'])} "
            f"| {fmt_int(t['tokens_per_run'])} | {t['cache_hit']:.0%} | {t['avg_turns']:.1f} "
            f"| {t['avg_wall_s']:.0f} | {t['cost_source']} |"
        )
    bad = [t for t in table if t["parse_failures"]]
    if bad:
        lines += [
            "",
            "**Warning:** no token data from the harness for: "
            + ", ".join(f"{t['harness']}/{t['model']} ({t['parse_failures']})" for t in bad)
            + ". See `results/logs/*.agent.jsonl`.",
        ]

    # per-task pass matrix
    tasks = sorted({r["task"] for r in rows})
    pairs = [(t["harness"], t["model"]) for t in table]
    lines += [
        "",
        "## Per task (solved/runs)",
        "",
        "| Task | " + " | ".join(f"{h}<br>{m}" for h, m in pairs) + " |",
        "|---|" + "---|" * len(pairs),
    ]
    for task in tasks:
        cells = []
        for h, m in pairs:
            rs = [r for r in rows if r["task"] == task and r["harness"] == h and r["model"] == m]
            cells.append(f"{sum(r['passed'] for r in rs)}/{len(rs)}" if rs else "-")
        lines.append(f"| {task} | " + " | ".join(cells) + " |")

    md = "\n".join(lines) + "\n"
    (out_dir / "summary.md").write_text(md)
    print("\n" + md)
    print(f"saved {out_dir / 'summary.md'} and {out_dir / 'summary.csv'}")


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--reps", type=int)
    r.add_argument("--only", nargs="*", help="only these harness names")
    r.add_argument("--keep", action="store_true", help="keep work directories")
    r.add_argument("--dry-run", action="store_true", help="print commands only")
    r.add_argument(
        "--allow-unsandboxed", action="store_true", help="run without sandbox.prefix (only inside a disposable VM)"
    )
    p = sub.add_parser("report")
    p.add_argument("--config", required=True)
    p.add_argument("--cost-source", choices=["auto", "litellm", "computed", "reported"])
    c = sub.add_parser("reconcile", help="fetch real spend per run from LiteLLM")
    c.add_argument("--config", required=True)
    args = ap.parse_args()

    cfg_path = Path(args.config).resolve()
    cfg = json.loads(cfg_path.read_text())
    base = cfg_path.parent
    {"run": cmd_run, "report": cmd_report, "reconcile": cmd_reconcile}[args.cmd](args, cfg, base)


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
"""

import argparse
import json
import sys
import time
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit("Python 3.12+ required")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_bench as hb  # noqa: E402

EXCLUDED_PARSERS = {"cursor"}


def load_config(path):
    cfg_path = Path(path).resolve()
    cfg = json.loads(cfg_path.read_text())
    if not cfg.get("litellm"):
        sys.exit("config needs a 'litellm' section (base_url, api_key)")
    excluded = {name for name, h in cfg["harnesses"].items() if h.get("parser") in EXCLUDED_PARSERS}
    cfg["harnesses"] = {k: v for k, v in cfg["harnesses"].items() if k not in excluded}
    dropped = [m for m in cfg["matrix"] if m["harness"] in excluded]
    cfg["matrix"] = [m for m in cfg["matrix"] if m["harness"] not in excluded]
    if dropped:
        print(f"ignored {len(dropped)} matrix entries for non-LiteLLM harnesses: {sorted(excluded)}")
    unknown = {m["harness"] for m in cfg["matrix"]} - set(cfg["harnesses"])
    if unknown:
        sys.exit(f"matrix uses undefined harnesses: {sorted(unknown)}")
    cfg.setdefault("output_dir", "results-litellm")
    cfg["cost_source"] = "litellm"
    return cfg, cfg_path.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--reps", type=int)
    r.add_argument("--only", nargs="*", help="only these harness names")
    r.add_argument("--keep", action="store_true", help="keep work directories")
    r.add_argument("--dry-run", action="store_true", help="print commands only")
    r.add_argument(
        "--allow-unsandboxed", action="store_true", help="run without sandbox.prefix (only inside a disposable VM)"
    )
    r.add_argument(
        "--settle", type=int, default=60, help="seconds to wait for LiteLLM to write spend logs before reconcile"
    )
    for name in ("reconcile", "report"):
        s = sub.add_parser(name)
        s.add_argument("--config", required=True)
    args = ap.parse_args()
    args.cost_source = "litellm"
    args.no_report = True  # report only after reconcile, when LiteLLM cost exists

    cfg, base = load_config(args.config)
    if args.cmd == "run":
        hb.cmd_run(args, cfg, base)
        if args.dry_run:
            return
        print(f"\nwaiting {args.settle}s for LiteLLM spend logs...", flush=True)
        time.sleep(args.settle)
        try:
            hb.cmd_reconcile(args, cfg, base)
        except Exception as e:  # noqa: BLE001
            print(f"reconcile failed: {e}\nRun later: python3 {Path(__file__).name} reconcile --config {args.config}")
    elif args.cmd == "reconcile":
        hb.cmd_reconcile(args, cfg, base)
    else:
        hb.cmd_report(args, cfg, base)


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
    try:
        for f in ("harness_bench.py", "harness_bench_litellm.py"):
            shutil.copy(ROOT / f, tmp)
        shutil.copytree(ROOT / "tasks" / "fizzbuzz", tmp / "tasks" / "fizzbuzz")
        tags_file, probe_file = tmp / "tags.txt", tmp / "probe.jsonl"
        os.environ.update(
            FAKE_TAGS_FILE=str(tags_file),
            FAKE_PROBE_FILE=str(probe_file),
            FAKE_KEY=fake_litellm.KEY,
            SECRET_CANARY="must-not-leak",
            FAKE_API_KEY=API_KEY,
        )

        srv, port = fake_litellm.serve()
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        harness_env = {
            "FAKE_TAGS_FILE": "${FAKE_TAGS_FILE}",
            "FAKE_PROBE_FILE": "${FAKE_PROBE_FILE}",
            "FAKE_API_KEY": "${FAKE_API_KEY}",
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
        check(
            not rows["cursor"]["parse_ok"] and rows["cursor"]["cost_computed"] is None, "cursor: no usage, no fake $0"
        )
        logs = tmp / "results" / "logs"
        stderr_logs = "".join(f.read_text() for f in logs.glob("*.agent.stderr"))
        check(API_KEY not in stderr_logs and "***REDACTED***" in stderr_logs, "logs: harness secrets redacted")
        check(any(f.read_text() for f in logs.glob("fizzbuzz__claude__*.diff")), "audit diff saved")
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
        check(pr["depth"] == "1" and pr["remotes"] == "", "workdir: no git history or remote (fix commit hidden)")
        check(pr["sandbox"] == pr["cwd"], "sandbox prefix wraps the agent command")
        row = results(tmp / "results-sec/results.jsonl")[0]
        check(not row["passed"] and row["sandboxed"], "protected paths restored: test-runner hack does not pass")
        diff = next((tmp / "results-sec/logs").glob("*.diff")).read_text()
        check("run_tests.py" in diff and "conftest.py" in diff, "audit diff shows tampering")

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


if __name__ == "__main__":
    main()
```

`tests/fake_agent.py`:

```python
"""Fake coding-agent CLI for tests. Prints the JSON shape of a real harness.

Usage: fake_agent.py <claude|opencode|pi|codex|cursor> <fix|nofix|hack>
"fix" repairs fizzbuzz.py in the cwd. "hack" leaves the bug and tampers with the test
infrastructure instead (rewrites run_tests.py, adds conftest.py, moves git refs).
Security probes: exits 9 if the LiteLLM spend key or SECRET_CANARY reached the agent,
prints FAKE_API_KEY to stderr (must be redacted in logs), and records git history depth. When BENCH_TAG is set (and kind is not cursor),
the tag is appended to $FAKE_TAGS_FILE, which fake_litellm.py serves as spend logs.
"""

import json
import os
import sys

kind, mode = sys.argv[1], sys.argv[2]
fix = mode == "fix"
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
if fix:
    with open("fizzbuzz.py", "w") as f:
        f.write(
            "def fizzbuzz(n):\n    if n % 15 == 0:\n        return 'FizzBuzz'\n"
            "    if n % 3 == 0:\n        return 'Fizz'\n    if n % 5 == 0:\n"
            "        return 'Buzz'\n    return str(n)\n"
        )

if kind == "claude":
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
    for f in ("harness_bench.py", "harness_bench_litellm.py"):
        shutil.copy(ROOT / f, tmp)
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
import math
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


class FormatTests(unittest.TestCase):
    def test_money_none_inf_small_and_large(self) -> None:
        self.assertEqual(
            [hb.fmt_money(v) for v in (None, math.inf, 0.12345, 12.345)], ["n/a", "inf", "$0.1235", "$12.35"]
        )

    def test_int_none_inf_and_grouping(self) -> None:
        self.assertEqual([hb.fmt_int(v) for v in (None, math.inf, 1234567.4)], ["n/a", "n/a", "1,234,567"])


class RowTagsTests(unittest.TestCase):
    def test_list_is_returned_as_is(self) -> None:
        self.assertEqual(hb.row_tags({"request_tags": ["a", "b"]}), ["a", "b"])

    def test_json_string_is_decoded(self) -> None:
        self.assertEqual(hb.row_tags({"request_tags": '["a"]'}), ["a"])

    def test_comma_separated_string_is_split(self) -> None:
        self.assertEqual(hb.row_tags({"request_tags": "a, b"}), ["a", "b"])

    def test_missing_or_non_list_gives_empty(self) -> None:
        self.assertEqual((hb.row_tags({}), hb.row_tags({"request_tags": "7"})), ([], []))


if __name__ == "__main__":
    unittest.main()
```

`tests/test_workspace.py`:

```python
"""Unit tests for config validation, sandbox wrapping, env files and workdir handling. In-process."""

import os
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import fixtures
import harness_bench as hb

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

    def test_harness_name_with_space_is_rejected(self) -> None:
        cfg = base_config()
        cfg["harnesses"]["bad name"] = cfg["harnesses"]["claude"]
        self.assert_rejected(cfg, "invalid harness name")

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


class MakeBaselineTests(unittest.TestCase):
    def test_returns_sha_of_a_single_commit_with_current_files(self) -> None:
        work = fixtures.tmpdir(self)
        (work / "a.txt").write_text("a")
        sha = hb.make_baseline(work)
        self.assertEqual((sha, run_git(work, "rev-list", "--count", "HEAD")), (run_git(work, "rev-parse", "HEAD"), "1"))


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


class SaveDiffTests(unittest.TestCase):
    def test_diff_contains_new_and_changed_files(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = hb.make_baseline(work)
        (work / "a.txt").write_text("changed")
        (work / "new.txt").write_text("n")
        hb.save_diff(work, baseline, out)
        text = out.read_text()
        self.assertIn("a.txt", text)
        self.assertIn("new.txt", text)

    def test_unknown_baseline_is_reported_in_the_diff_file(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        hb.make_baseline(work)
        hb.save_diff(work, "0" * 40, out)
        self.assertIn("git diff failed", out.read_text())

    def test_directory_that_is_not_a_repo_is_reported_in_the_diff_file(self) -> None:
        out = fixtures.tmpdir(self) / "d.diff"
        hb.save_diff(fixtures.tmpdir(self), "abc", out)
        self.assertIn("git diff failed", out.read_text())

    def test_long_diff_is_truncated(self) -> None:
        work, out = fixtures.tmpdir(self), fixtures.tmpdir(self) / "d.diff"
        (work / "a.txt").write_text("a")
        baseline = hb.make_baseline(work)
        (work / "a.txt").write_text("b" * 1000)
        hb.save_diff(work, baseline, out, limit=50)
        self.assertTrue(out.read_text().endswith("[truncated]"))


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

    def test_empty_results_file_crashes_with_index_error(self) -> None:
        # CURRENT BEHAVIOR (bug, reported in Phase 0, not fixed): table[0] fails on zero rows.
        fixtures.write_rows(self.base / "results" / "results.jsonl")
        with self.assertRaises(IndexError):
            fixtures.call_quiet(hb.cmd_report, args(), {}, self.base)

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
        for f in ("harness_bench.py",):
            shutil.copy(ROOT / f, tmp)
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

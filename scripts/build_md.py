#!/usr/bin/env python3
"""Generate HARNESS_BENCH.md (single-file bundle: docs + all source). Run after any change."""
from pathlib import Path
b=Path(__file__).resolve().parent.parent
R=lambda f:(b/f).read_text()
py,pyl,cfgl,cfg,pim,ocj,cdx,fz,th=map(R,['harness_bench.py','harness_bench_litellm.py','config.litellm.example.json','config.example.json','pi-models.example.json','opencode.example.json','codex-config.example.toml','tasks/fizzbuzz/repo/fizzbuzz.py','tasks/fizzbuzz/hidden_tests/test_hidden.py'])
smoke,fagent,flite,dock,entry,de2e,dign=map(R,['tests/smoke.py','tests/fake_agent.py','tests/fake_litellm.py','sandbox/Dockerfile','sandbox/entrypoint.sh','tests/docker_e2e.py','.dockerignore'])
md=f'''# harness-bench: cost per solved task (harness × model)

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
docker build -f sandbox/Dockerfile -t harness-bench-agents \\
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
- `ANTHROPIC_CUSTOM_HEADERS` = `x-litellm-tags: {{run_tag}}`.

`{{run_tag}}` is replaced with each run's tag. The LiteLLM guides disagree on authentication (`ANTHROPIC_AUTH_TOKEN` or an `x-litellm-api-key` header). Use whichever format already works on your machine.

### Pi

With the Docker image, there is nothing to do: the entrypoint generates `~/.pi/agent/models.json` from `pi-models.example.json`. Without Docker, copy the file to `~/.pi/agent/models.json` and replace `__LITELLM_BASE_URL__` with the proxy URL. Pi accepts `headers` per provider, and `$BENCH_TAG` is read from the environment.

- Claude models: use the `litellm-anthropic` provider (`api: anthropic-messages`). Through the OpenAI-compatible API, Anthropic prompt caching may not work, which raises cost and skews the comparison.
- GPT and Gemini models: use the `litellm` provider (`openai-completions`).
- Model format in the config: `provider/model`. The command uses `--provider {{provider}} --model {{model_name}}`.

```json
{pim}```

### OpenCode

With the Docker image, the entrypoint generates the config. Without Docker, use `opencode.example.json` as the global or project config, replacing `__LITELLM_BASE_URL__`. The official examples show `{{env:VAR}}` in `apiKey`. I found no official example with `{{env:...}}` inside `headers`. **Check in LiteLLM that the tag arrives.** Model in the config: `litellm/MODEL`.

```json
{ocj}```

### Codex CLI

With the Docker image, the entrypoint generates `config.toml`. Without Docker, add the snippet to `~/.codex/config.toml` (or to a dedicated `CODEX_HOME`), replacing `__LITELLM_BASE_URL__`. `env_http_headers` maps header → env var name. **Check these key names in the docs for your Codex version.** The command uses `--dangerously-bypass-approvals-and-sandbox`: isolation comes from the container. Codex's own sandbox relies on kernel features that usually fail inside unprivileged containers.

```toml
{cdx}```

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

`sandbox.prefix` is the command put in front of the agent, `setup` and `test`. Placeholders: `{{workdir}}` and `{{env_file}}`. The `env_file` is a `KEY=value` file with mode 0600, deleted right after each step. The host's `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL` and `TMPDIR` do not go into it: the container uses the image's own.

The `sandbox/Dockerfile` image has:
- Python 3.12, Node.js 22, `git` and `tini`;
- Claude Code, Pi, OpenCode and Codex CLI, through npm. Pi is installed with `--ignore-scripts`, as its docs recommend;
- Cursor CLI, optional, with `--build-arg INSTALL_CURSOR=true`;
- the unprivileged `agent` user, with the same UID/GID as the host. So files in the workdir stay yours;
- an entrypoint that generates the Pi, OpenCode and Codex configs from `LITELLM_BASE_URL`. That is why `LITELLM_BASE_URL` is in `env_passthrough`.

```bash
docker build -f sandbox/Dockerfile -t harness-bench-agents \\
  --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
# pinned versions for reproducible results:
#   --build-arg CLAUDE_CODE_VERSION=... --build-arg PI_VERSION=...
#   --build-arg OPENCODE_VERSION=... --build-arg CODEX_VERSION=...
```

The `prefix` in the example configs mounts only the workdir, at the same path, and limits the container:

```json
"prefix": ["docker", "run", "--rm", "--env-file", "{{env_file}}",
           "-v", "{{workdir}}:{{workdir}}", "-w", "{{workdir}}",
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

`{{prompt}}`, `{{model}}`, `{{provider}}` (part before the `/`), `{{model_name}}` (part after the `/`).
In `env`: `${{VAR}}` is expanded from the environment and `{{run_tag}}` becomes the run's tag. `BENCH_TAG` is always exported.
In `sandbox.prefix`: `{{workdir}}` and `{{env_file}}`.

---

## `harness_bench.py`

```python
{py}```

## `harness_bench_litellm.py`

```python
{pyl}```

## `config.litellm.example.json`

```json
{cfgl}```

## `config.example.json`

```json
{cfg}```

## Example task

`tasks/fizzbuzz/repo/fizzbuzz.py` (deliberate bugs):

```python
{fz}```

`tasks/fizzbuzz/hidden_tests/test_hidden.py`:

```python
{th}```

## Offline tests

Run `python3 tests/smoke.py`. It uses fake agents and a fake LiteLLM to check the parsers, `reconcile` and both scripts, with no network and no real CLIs.

`tests/smoke.py`:

```python
{smoke}```

`tests/fake_agent.py`:

```python
{fagent}```

`tests/fake_litellm.py`:

```python
{flite}```

## Sandbox

`sandbox/Dockerfile`:

```dockerfile
{dock}```

`sandbox/entrypoint.sh`:

```sh
{entry}```

`.dockerignore`:

```
{dign}```

`tests/docker_e2e.py`:

```python
{de2e}```

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
'''
(b/'HARNESS_BENCH.md').write_text(md)
print(f"wrote {b / 'HARNESS_BENCH.md'} ({len(md.splitlines())} lines)")

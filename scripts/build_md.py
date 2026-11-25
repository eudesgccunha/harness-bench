#!/usr/bin/env python3
"""Generate HARNESS_BENCH.md (single-file bundle: docs + all source). Run after any change."""

from pathlib import Path

b = Path(__file__).resolve().parent.parent


def R(f: str) -> str:
    return (b / f).read_text()


py, pyl, cfgl, cfg, pim, ocj, cdx, fz, th = map(
    R,
    [
        "harness_bench.py",
        "harness_bench_litellm.py",
        "config.litellm.example.json",
        "config.example.json",
        "pi-models.example.json",
        "opencode.example.json",
        "codex-config.example.toml",
        "suites/regression/fizzbuzz/repo/fizzbuzz.py",
        "suites/regression/fizzbuzz/hidden_tests/test_hidden.py",
    ],
)
smoke, fagent, flite, tgold, tfix, tpars, twork, trun, tlite, dock, entry, de2e, dign = map(
    R,
    [
        "tests/smoke.py",
        "tests/fake_agent.py",
        "tests/fake_litellm.py",
        "tests/test_golden.py",
        "tests/fixtures.py",
        "tests/test_parsers.py",
        "tests/test_workspace.py",
        "tests/test_run.py",
        "tests/test_litellm.py",
        "sandbox/Dockerfile",
        "sandbox/entrypoint.sh",
        "tests/docker_e2e.py",
        ".dockerignore",
    ],
)
HBENCH_SOURCES = [
    "hbench/__init__.py",
    "hbench/core/__init__.py",
    "hbench/core/ids.py",
    "hbench/core/models.py",
    "hbench/core/schema.py",
    "hbench/core/convert.py",
    "hbench/runners/__init__.py",
    "hbench/runners/base.py",
    "hbench/runners/_usage.py",
    "hbench/runners/claude.py",
    "hbench/runners/opencode.py",
    "hbench/runners/pi.py",
    "hbench/runners/cursor.py",
    "hbench/runners/codex.py",
    "hbench/runners/null.py",
    "hbench/graders/__init__.py",
    "hbench/graders/base.py",
    "hbench/graders/compose.py",
    "hbench/graders/_command.py",
    "hbench/graders/hidden_tests.py",
    "hbench/graders/command.py",
    "hbench/graders/diff_constraints.py",
    "hbench/graders/_files.py",
    "hbench/graders/_options.py",
    "hbench/graders/html_check.py",
    "hbench/graders/structured_output.py",
    "hbench/graders/llm_rubric.py",
    "hbench/judge.py",
    "hbench/calibration.py",
    "hbench/graders/registry.py",
    "hbench/plugins.py",
    "hbench/net.py",
    "hbench/sinks/__init__.py",
    "hbench/sinks/base.py",
    "hbench/sinks/feed.py",
    "hbench/sinks/sqlite.py",
    "hbench/sinks/otlp.py",
    "hbench/sandboxes/__init__.py",
    "hbench/sandboxes/base.py",
    "hbench/sandboxes/prefix.py",
    "hbench/sandboxes/local_tmp.py",
    "hbench/sandboxes/limits.py",
    "hbench/sandboxes/docker.py",
    "hbench/doctor.py",
    "hbench/results.py",
    "hbench/stats.py",
    "hbench/report.py",
    "hbench/profiles.py",
    "hbench/suites.py",
    "hbench/api.py",
    "hbench/cli.py",
    "hbench/__main__.py",
]
HBENCH_TESTS = [
    "tests/test_core_models.py",
    "tests/test_core_schema.py",
    "tests/test_contract_output.py",
    "tests/test_stats.py",
    "tests/test_report.py",
    "tests/test_cli.py",
    "tests/test_runners.py",
    "tests/test_graders.py",
    "tests/test_artifact_graders.py",
    "tests/test_judge.py",
    "tests/test_calibration.py",
    "tests/test_plugins.py",
    "tests/test_suites.py",
    "tests/test_sandboxes.py",
    "tests/fake_docker.py",
    "tests/test_docker_sandbox.py",
    "tests/test_resume.py",
    "tests/test_doctor.py",
    "tests/sink_helpers.py",
    "tests/fake_collector.py",
    "tests/test_sinks.py",
    "tests/test_sqlite_sink.py",
    "tests/test_otlp_sink.py",
    "tests/test_sink_run.py",
    "tests/test_dashboard.py",
    "tests/test_api.py",
]


def file_blocks(paths: list[str]) -> str:
    """One heading-less `path` + fenced Python block per file."""
    return "".join(f"`{p}`:\n\n```python\n{R(p)}```\n\n" for p in paths)


SUITE_FILES = [
    "suites/regression/suite.json",
    "suites/examples-capability/suite.json",
    "suites/examples-artifacts/suite.json",
]


def suite_blocks(paths: list[str]) -> str:
    """One `path` + fenced JSON block per suite file."""
    return "".join(f"`{p}`:\n\n```json\n{R(p)}```\n\n" for p in paths)


def suite_tree() -> str:
    """The files of the example suites, one path per line (the suite files above hold the task definitions)."""
    return "\n".join(
        sorted(str(f.relative_to(b)) for f in (b / "suites").rglob("*") if f.is_file() and "__pycache__" not in f.parts)
    )


SQL_FILES = ["hbench/sinks/schema.sql", "docs/sql/scorecard.sql", "docs/dashboards/grafana-scorecard.json"]


def sql_blocks(paths: list[str]) -> str:
    """One `path` + fenced block per SQL or dashboard file (the sqlite sink loads its schema at runtime)."""
    return "".join(f"`{p}`:\n\n```{'json' if p.endswith('.json') else 'sql'}\n{R(p)}```\n\n" for p in paths)


hbench_md = file_blocks(HBENCH_SOURCES) + sql_blocks(SQL_FILES)
hbench_tests_md = file_blocks(HBENCH_TESTS)
md = f"""# harness-bench: cost per solved task (harness × model)

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
├── hbench/                      # data contract, report, suites, sinks, orchestrator API and single CLI (ADR 0005, 0008, 0011, 0012)
├── adapters/                    # shell, GitHub Actions, Kubernetes and Python examples over plan / run-trial / finish (ADR 0012)
├── docs/sql/                    # reference schema and scorecard queries of the sqlite sink (ADR 0011)
├── docs/dashboards/             # example Grafana dashboard; scripts/synthetic_db.py writes demo data
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
└── suites/                      # versioned suites of tasks (ADR 0008)
    ├── regression/              # kind "regression": tasks every healthy pair solves
    │   └── fizzbuzz/            # repo/, hidden_tests/ (copied in only AFTER the agent runs), solution/
    ├── examples-capability/     # kind "capability": dependency-upgrade, path-traversal-fix, extract-function
    └── examples-artifacts/      # kind "capability": html-report (HTML + JSON artifacts, graded with a judge); rubrics/, schemas/
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
- `summary.md` and `summary.csv`: table sorted by cost per solved task, the statistics sections described in [Reading the report](#reading-the-report) and the per-task pass matrix;
- `logs/`: each agent's JSON output, stderr, setup, tests, and the `.diff` of everything the agent changed. Known secrets show as `***REDACTED***`.

To also send each trial to a SQLite database and OTLP traces, add a `sinks` list to the config (see `docs/running-a-benchmark.md` and `config.example.json`): `{{"type": "sqlite", "path": "results/bench.sqlite"}}` and `{{"type": "otlp", "endpoint": "https://collector:4318", "headers": {{"x-key": "${{OTLP_KEY}}"}}}}`. `trials.jsonl` is written first and stays the source of truth; a sink that fails is warned about once and left out for that run, and `python3 -m hbench export --config config.json --run results/<run_id>` sends a run again (idempotent by `trial_id`). Spans hold ids, status, model, tokens and cost, never a prompt, response or log. The agent gets `BENCH_RUN_ID` and `BENCH_TRIAL_ID` in its environment.

To run the trials of one run on several workers (processes, CI jobs, pods), use the three orchestrator commands: `python3 -m hbench plan --config config.json > specs.jsonl` starts the run and prints one `TrialSpec` JSON per line, `python3 -m hbench run-trial --config config.json --spec -` runs the spec on its stdin and prints the `TrialResult`, and `python3 -m hbench finish --config config.json --run RUN_ID` closes the run and writes the report. A spec names a trial and carries no command, prompt, path, env or secret: each worker resolves everything from its own copy of the config, and a spec that does not match it is refused before anything runs. Running a spec twice is safe (the second returns the first result). Workers on one run directory need a shared filesystem with working `flock`. The Python API is `hbench/api.py` (`load_config`, `start_run`, `plan`, `run_trial`, `finish_run`); examples are in `adapters/` and the guide is `docs/orchestrator-integration.md` ([ADR 0012](docs/adr/0012-plan-and-run-trial-api.md)).

`run` only starts with a sandbox configured (`sandbox.type` `docker`, or a `sandbox.prefix`; see [Security](#security)). Inside a disposable VM, add `--allow-unsandboxed`. To continue a run that was killed, use `run --resume RUN_ID` (see [Sandbox types, limits, resume and doctor](#sandbox-types-limits-resume-and-doctor)).

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
| By suite kind | Only when the trials come from more than one suite kind (`capability`, `regression`): solved/runs per pair for each kind, and a warning for every regression task a non-null pair did not solve. |
| Judges | Only when a trial has an `llm_rubric` result: each rubric version × judge model pair in use, **calibrated** or **experimental** (see [Artifact graders and the LLM judge](#artifact-graders-and-the-llm-judge)). |
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

1. **Workdir outside the benchmark repository**: a new temporary directory, or a `work_dir` outside the repository (a relative path is relative to the config directory; the script rejects a path inside the repository). So the agent does not work next to hidden tests, config and results.
2. **No git history**: the task repository is copied without `.git`. A clone at the commit before the fix would still contain the fix commit, and the remote would point back to the benchmark. The single `baseline` commit lives in a private temporary git directory outside the workdir and `work_dir` (deleted after the trial). Every git call on the workdir ignores its `.git`, `.gitignore`, `.gitattributes`, replace refs and git config, and every diff starts from a fresh index, so the agent cannot hide a change or run a command on the host through git. If the agent leaves a `.git` anywhere in the workdir, or `git add` fails (for example on an unreadable file), its changes count as unknown and `diff_constraints` fails; a binary file counts as more than any `max_changed_lines`. This works on any git version (an `info/attributes` file in the baseline git directory does the work; git 2.40 or newer adds `--attr-source` as a second layer). The guarantee holds for sandboxed runs: an unsandboxed agent runs as your user and could find and edit the temporary git directory.
3. **Minimal environment**: the agent gets only `PATH`, `HOME`, `USER`, locale, `TERM`, `TMPDIR`, the variables in `env_passthrough` and those in the harness `env` block. `setup` and `test` get the same, without the harness `env`. The LiteLLM spend-read key (`litellm.api_key`) **never** reaches the agent: the script rejects a config that tries to pass it through.
4. **Protected files**: before the agent runs, each path in `protected_paths` is copied outside the workdir. Before the test, that content is restored and anything the agent created there is deleted. Use it for the test runner and configs (`tests/`, `package.json`, `pytest.ini`, `conftest.py`, `vitest.config.ts`). Without it, an agent can "solve" the task by making the test always pass.
5. **Audit**: `logs/<run>.diff` keeps everything the agent changed relative to the baseline.
6. **Logs without secrets**: values of the harness `env` variables and the spend key are replaced with `***REDACTED***`.
7. **Config validation**: task ids and harness names accept only letters, digits, `_`, `.` and `-`. `repo` and `ref` cannot start with `-`. `protected_paths` must be relative and without `..`. The script deletes only directories inside the work root.
8. **LiteLLM**: `base_url` must use `https` (http only for localhost), and redirects are not followed, so the key does not go to another host.
9. **Copies never follow a link the agent planted**: the hidden tests (before grading) and the `solution` of `suite validate --run` are copied with `hbench/safe_copy.py`, which replaces any symlink (or hard-linked file) on the destination path with a regular file instead of writing through it. Otherwise an agent that plants `test_hidden.py -> ~/.bashrc` would make the grader overwrite a host file.
10. **Git transports**: `git clone` of a task repo runs with `GIT_ALLOW_PROTOCOL`: `https:ssh:file` for the tasks of your config, `https` only for suite tasks, so a suite can never clone a private local repo or use your SSH keys. The clone from the local cache into the workdir allows `file` alone.

What the script does **not** solve on its own: without a sandbox, the agent runs as your user and can read any of your files (including `~/.ssh`) and tamper with results. That is why `run` requires a sandbox (`docker` or `sandbox.prefix`) or `--allow-unsandboxed`.

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
- **Network**: besides the `reconcile` `GET`, the engine makes one `POST` per judged artifact to `judge.base_url`, and only when a task uses `llm_rubric` (see [Artifact graders and the LLM judge](#artifact-graders-and-the-llm-judge)). The container has open internet egress, because it must reach LiteLLM. To restrict egress to LiteLLM only, use a Docker network with an egress proxy, or OpenSandbox (below).
- Validate with `python3 tests/docker_e2e.py`. The test runs fake agents inside the container, once with the `docker` sandbox type and once with a `sandbox.prefix`, and checks HOME, UID, secrets, the test results, the missing network, the removed containers and the recorded image digest.

### Sandbox types, limits, resume and doctor

`sandbox.type` picks how the commands of a trial run ([ADR 0010](docs/adr/0010-pluggable-sandbox-and-resume.md)). Without a `type`, a `prefix` means `prefix` and nothing means `local-tmp`. A sandbox only builds the command; the engine still starts every process, so the env allowlist, the timeout, the log redaction and the process-group kill are the same for all of them.

| Type | What it does | Needs `--allow-unsandboxed` |
|---|---|---|
| `docker` | `docker run` of an image **pinned by digest** (`name@sha256:<64 hex>`; a tag is a config error). The workdir is mounted at `/work`. `--network none` by default (or the name of a Docker network), `--cap-drop ALL`, `--security-opt no-new-privileges`, the host user, and `limits`: `cpus`, `memory` (`512m`, `2g`; swap off) and `pids`. `mounts` (`["/host:/container[:ro]"]`) adds helpers such as a CA file; never at or under `/work`. Mount read-only: a writable mount of `docker.sock` or a home directory hands it to the agent and breaks the isolation. Each command is a container named `hb-<trial_id>-<n>`; killing the `docker run` client does not stop its container, so the engine runs `docker rm -f` on it as soon as the command ends or times out, before it reads the workdir or starts the next command. If the container cannot be shown to be gone (`rm` fails or times out, or it is still listed after a second `rm -f`), the trial fails closed: it is not graded and counts as not passed. | no |
| `prefix` | The older `sandbox.prefix` template (`{{workdir}}`, `{{env_file}}`), unchanged. | no |
| `local-tmp` | No isolation: commands run as your user. `limits` `memory` (`RLIMIT_AS`), `pids` (`RLIMIT_NPROC`, counted over all your processes) and `cpu_seconds` (`RLIMIT_CPU`) are best effort; `cpus` is ignored. | yes |

```json
"sandbox": {{"type": "docker", "image": "harness-bench-agents@sha256:<64 hex>", "network": "hb-egress",
             "limits": {{"cpus": 2, "memory": "4g", "pids": 512}}}}
```

The digest of a local image is `docker image inspect harness-bench-agents --format '{{{{.Id}}}}'`. The run records the sandbox in `run.json` (`sandbox`) and the digest in every trial (`image_digest`). To let the agents reach only LiteLLM, create an internal network (`docker network create --internal hb-egress`) and attach only the gateway to it ([docs/machine-setup.md](docs/machine-setup.md)). Another package can add a type through the entry-point group `harness_bench.sandboxes` (a factory `(sandbox section) -> Sandbox`).

**Process groups.** Every command runs as the leader of its own session and its whole process group is killed when it ends or times out, so a process the agent forked into the background (`sleep 600 &`) is gone before the graders run. This is best effort outside a container: with `prefix` and `local-tmp`, a process that leaves the group with `setsid` (or a double fork) survives into grading. With `docker` the container's removal stops everything in it.

**Resume.** `run --resume RUN_ID` reopens `<output_dir>/RUN_ID/` (the directory name), keeps the run id and skips every trial already in its `trials.jsonl`; the others run. A trial is written to `trials.jsonl` (and flushed to disk) before its row in `results.jsonl`, so a crash between the two loses only the row, which resume rewrites. It refuses a run whose config changed, a run id that is not a run directory, and a run whose heartbeat (`heartbeat.json`, rewritten every 30 s) is fresh while its process is alive (`--force` overrides). It deletes only the temp dirs (`harness-bench-work-*`, `harness-bench-git-*`) that the killed run's own heartbeat names and whose random token file it still holds. A plain `run` without `--resume` still skips the `(task, harness, model, rep)` rows of `results.jsonl` and writes a new run directory.

**Doctor.** `python3 -m hbench doctor --config config.json [--network]` spends nothing and starts no agent. It checks the config, that every `${{VAR}}` is set (by name, never by value), each harness CLI (`docker run <image> <cli> --version` for `docker`, with the limits and no network of an agent; `PATH` otherwise), the image and its digest, that the network is `none` or internal, and the LiteLLM and judge URLs. `--network` adds one `GET /health/liveliness` and one page of `/spend/logs/v2` to the configured LiteLLM, to see that the spend key can read. It exits 1 when any check fails; a warning does not fail it.

### Why not OpenSandbox (yet)

[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) is a good option when network control matters: it has a per-domain egress allowlist, enforced at DNS and network level. But it does not fit this script's prefix model:

- it needs a server (`opensandbox-server`) and creates sandboxes through an API (`osb sandbox create`, `osb command run <id>`);
- files go in and out by upload/download, not by bind mount (support for host volumes was not confirmed);
- the client is the `opensandbox` Python SDK, an external dependency, and this script uses only the standard library.

Integrating it would need a dedicated backend: create the sandbox, upload the workdir, run, download the result and destroy it. It is worth it when you need to restrict the agents' egress.

## Choosing tasks

A task lives in a suite (see [Suites](#suites)): add it to a `suite.json` with its `solution`, bump the suite `version`, and run `suite validate --run` before using it. The list below applies to every task.

- Use real bugs that were already fixed, with `ref` = the commit before the fix.
- Write the prompt like an issue, with no hint of the solution.
- Use the tests from the fix commit as hidden tests.
- `setup` (e.g. `npm ci`) runs before the agent and does not count toward cost.
- List the repository's tests and test configs in `protected_paths`.

## Graders

A task passes when its graders say so. Without a `graders` key, the single `hidden_tests` grader runs the task's `hidden_tests` copy and `test` command, as before. With one, each entry is an object with a `type` (and an optional `weight`), run in order after the agent finishes:

```json
"graders": [
  {{"type": "hidden_tests"}},
  {{"type": "command", "command": "ruff check .", "timeout": 120}},
  {{"type": "diff_constraints", "forbidden": ["run_tests.py", "tests/*"], "max_changed_lines": 200}}
],
"grade": {{"mode": "all"}}
```

- `hidden_tests`: copies `hidden_tests` into the workdir, then runs `test` (`test_timeout`, default 900 s).
- `command`: runs `command` (`timeout`, default 900 s). Passes on exit code 0.
- `diff_constraints`: runs nothing. It checks what the agent changed, captured right after the agent finishes and before `protected_paths` are restored: every changed path must match `allowed` (empty means any) and none may match `forbidden` (`fnmatch` globs on paths relative to the workdir; `*` also matches `/`), and the added plus deleted lines must not exceed `max_changed_lines`. A path outside the workdir is always forbidden, and so is an unreadable diff. Renames count as a delete and an add. A binary file counts as more than any `max_changed_lines`. Interpreters leave bytecode (`__pycache__/*.pyc`) in the workdir, and a `.pyc` is binary: list `ignore_lines` globs (for example `["*/__pycache__/*"]`) to leave those paths out of the **line count** only (the `allowed` and `forbidden` checks still apply). Do not ignore bytecode in `forbidden` or `allowed`: a crafted `.pyc` can poison the tests. Instead list the test directories in `protected_paths`, because the restore before the tests deletes everything the agent added there (bytecode included), and write `forbidden` with source globs (`tests/*.py`) so harmless bytecode does not trip it. Keep `ignore_lines` to generated artifacts only: a broad glob such as `*.py` turns `max_changed_lines` off.
- `html_check`, `structured_output` and `llm_rubric` grade artifacts other than code: see [Artifact graders and the LLM judge](#artifact-graders-and-the-llm-judge).
- `grade`: `mode` is `all` (default: every grader passes, score is the mean), `any` (one passes, score is the best) or `weighted` (score is the weighted mean using each grader's `weight`, default 1; it passes at `threshold`, required, above 0 and up to 1).
- Partial credit is opt-in: set `"score": true` on a `hidden_tests` or `command` grader and it reads the **last** line `SCORE: <0 to 1>` of the command's **stdout** (stderr is never read). Passing is still exit code 0 without a timeout. Without the option, or without a valid `SCORE` line, the score is 1.0 on pass and 0.0 otherwise (an out-of-range value is ignored with a warning). The test command runs code the agent wrote, which can print a `SCORE` line after the real one, so a score is only as trustworthy as the test output (including a later stdout `SCORE:` line printed by agent code, for example from `atexit`), and a `weighted` verdict with `score: true` inherits exactly that trust. Use `all` or `any` (exit codes only) when the agent can influence the scorer.
- Logs: `<tag>.test.log` for `hidden_tests`, `<tag>.grader-<index>-<type>.log` for the others. Only the engine starts processes, through the same env allowlist, sandbox wrapper and log redaction as the test command.
- `trials.jsonl` lists every grader result. Rows of tasks without `graders` keep the single `test` grader.

### Artifact graders and the LLM judge

Three more graders look at files the agent wrote, not at code. None of them starts a process. Every `file` is a path inside the workdir: it must be relative, without `..`, with no symlink on the way, a regular file with one hard link, and at most 10 MB. Anything else fails the grader.

```json
"graders": [
  {{"type": "html_check", "file": "report.html", "required": ["h1", "table#totals", ".summary"]}},
  {{"type": "structured_output", "file": "summary.json", "schema": "schemas/summary.schema.json"}},
  {{"type": "llm_rubric", "file": "report.html", "rubric": "rubrics/report-quality.json"}}
]
```

- `html_check`: parses the file with `html.parser`. `required` lists simple selectors (`tag`, `#id`, `.class`, `tag#id`, `tag.class`) that must each match an element. `forbid_external_scripts` (default true) fails a `<script src>` with a scheme or `//`. `check_local_links` (default true) needs every relative `href` and `src` to resolve to an existing file (or directory) inside the workdir; remote links are never fetched, and a link with no path (`?page=2`) is skipped. At most 1000 violations are kept, then `… N more`; link and script checks stop once they are full. Unclosed and mismatched non-void elements always fail.
- `structured_output`: the file must be JSON (no `NaN`) and satisfy `schema`, a JSON Schema object written in the config or the path of a schema file. Only the keywords of `hbench/core/schema.py` are supported (`type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, `minimum`, `maximum`, `$defs`, `$ref`); another keyword is a config error (a schema file of a suite is checked when the suite loads, so `suite validate` and `run` find a broken one).
- `llm_rubric`: an LLM judge scores the file against a rubric, through the OpenAI-compatible gateway. It needs a `judge` section in the config.
- Relative paths of `schema` and `rubric` are relative to the suite directory for a suite task and to the config directory for a config task, never to the workdir, so the agent cannot see or change them. They follow the same rule as `file`.
- Violations, and the judge's `reason`, are written to `<tag>.grader-<index>-<type>.log` (redacted), never to `trials.jsonl`, `run.json` or `results.jsonl`.

The `judge` section (read only when a task uses `llm_rubric`):

```json
"judge": {{
  "base_url": "${{JUDGE_BASE_URL}}",
  "api_key": "${{JUDGE_API_KEY}}",
  "model": "judge-model",
  "timeout": 120,
  "max_artifact_bytes": 200000,
  "calibration": "calibration.json",
  "redactor": "my-redactor"
}}
```

`base_url`, `api_key` and `model` are required. `timeout` (seconds, default 120) and `max_artifact_bytes` (default 200000; a larger artifact is cut with a marker) are optional. `api_key` (like `litellm.api_key`) must be exactly a `${{VAR}}` reference; a key written in the config is refused, because only a variable can be redacted from logs and kept from the agent. The key never reaches the agent and is redacted from logs like the LiteLLM key. The whole exchange (status line, headers and reply) has a total deadline of `timeout` seconds. A rubric is a versioned file with `id`, `version` (letters, digits, `_`, `.`, `-`), `criteria` and an optional `pass_threshold` in [0, 1] (default 0.5); the artifact passes when the judge's score reaches it. The result is recorded as `llm_rubric:<id>` at the rubric's version, with the judge model in `judge` (data contract 1.2.0).

How the judge is kept honest about untrusted content:

- The only extra network access is one `POST <judge.base_url>/chat/completions` per artifact, and only when a task uses `llm_rubric`. It follows the LiteLLM rule: https, or http only for localhost, and redirects are never followed (so the key cannot reach another host).
- The request has `temperature: 0`, a JSON-object reply format and a fixed system prompt that says the artifact is untrusted data and that instructions inside it must be ignored. The artifact sits between two marker lines with a random token that changes on every call. A test checks this structure; it cannot prove that a model resists injection.
- The reply must be exactly `{{"score": number in [0, 1], "passed": bool, "reason": str}}`. A reply that is not valid, a timeout or an HTTP error fails the grader (not passed) and the run goes on.
- The artifact is redacted before it leaves the machine: first the engine's secret redaction (the same secrets as the logs), then the optional `redactor`, a plugin in the entry-point group `harness_bench.redactors`: a factory `(config) -> (text: str) -> str`. Content-specific redaction (names, internal hosts) is yours to write. A redactor that raises sends nothing.
- Calibration compares the judge with human scores: `python3 -m hbench judge calibrate --config config.json --labels labels.jsonl --out calibration.json`. Each line of the labels file has `artifact` and `rubric` (paths, relative to the file) and `human_score` in [0, 1]; all labels must use one rubric version. The labels file is trusted input: it names files that are read and sent to the judge, so only use a file you wrote. The output holds the rubric id and version, the judge model, `n`, the mean absolute error, the pass/fail agreement and Cohen's kappa (defined as 0 when both sides only ever give one label). Set `judge.calibration` to the file: the report marks every rubric version × judge pair **experimental** unless the file matches the rubric id, version and judge model with kappa ≥ 0.6 on at least 20 labels.

### Plugins

Another package can add runners, graders and redactors without touching this repository. It registers a factory `(config) -> HarnessRunner | Grader | redactor` under the entry-point group `harness_bench.runners` (the harness `parser` name), `harness_bench.graders` (the grader `type`) or `harness_bench.redactors` (the `judge.redactor` name). Built-in names always win and a plugin that reuses one is an error. Plugins are loaded only when the config names a type that is not built in, and the config is validated before any agent runs. The plugin's package name and version are printed once. A runner builds the argv and parses the output; a grader asks the engine to run commands through `ctx.run`. Neither starts processes.

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

## `hbench/` package

New code lives in the `hbench/` package ([ADR 0005](docs/adr/0005-data-contract-and-package.md)); the two scripts above call into it. `hbench/sandboxes/` holds the sandboxes, `hbench/doctor.py` the `doctor` checks and `hbench/api.py` the API for orchestrators.

{hbench_md}## `config.litellm.example.json`

```json
{cfgl}```

## `config.example.json`

```json
{cfg}```

## Suites

A suite is a directory with a `suite.json` and the files it points to. A config lists them in `suites` (directories relative to the config); their tasks are appended to `tasks` with the id `<suite>/<task>`. A config without `suites` works as before. ([ADR 0008](docs/adr/0008-suites-as-data.md))

`suite.json` has `schema_version` (`"1.0.0"`), `name` (`[a-z0-9-]+`), `version` (`MAJOR.MINOR.PATCH`), `kind` (`"capability"` or `"regression"`), `description` and `tasks`. A task takes the keys of a config task (`id`, `repo`, `ref`, `prompt`, `test`, `hidden_tests`, `setup`, `timeout`, `setup_timeout`, `test_timeout`, `protected_paths`, `graders`, `grade`) and:

- `tags`: a list of strings;
- `solution`: a directory with the reference files, copied over the repo by `suite validate --run`;
- `provenance`: `{{"source": ..., "license": ..., "url": ... or null}}` (required).

Paths are relative to the suite directory and must stay inside it (no absolute path, no `..`). A suite holds **no symlinks** at all in `repo`, `hidden_tests` or `solution` (a link could reach a host path or the hidden tests when the directory is copied). A suite `repo` directory must not be a git repository (no `.git`, no name ending in `.git`): use an `https://` URL with `ref`, or a plain directory. A suite is someone else's data, so `repo` may be a directory of the suite or an `https://` URL: `file://`, `ssh://`, `git@host:...` and `ext::` are rejected. A task in your own config keeps every form. A config task must not set the keys the engine adds to suite tasks (`suite`, `suite_kind`, `suite_label`, `suite_dir`, `solution`). Unknown keys are an error. `capability` suites hold hard tasks with a low pass rate; `regression` suites hold tasks every healthy pair solves, and the report flags a pair that fails one.

```bash
python3 -m hbench suite list     [--config C | PATH...]     # name, version, kind, task count and tags
python3 -m hbench suite validate [--config C] PATH...       # static checks and the engine's config checks
python3 -m hbench suite validate --run --allow-unsandboxed PATH...
```

`--run` is the behavioral check: for every task the graders must **fail** on the untouched repo and **pass** once `solution` is copied in. It runs repo and test code, so it goes through the same engine path as `run` (workdir outside the repo, `wrap()`, env allowlist, redacted logs) and follows the same sandbox rule: set `sandbox.prefix` in a `--config`, or pass `--allow-unsandboxed` inside a disposable VM. One line per task: `ok`, `trivially passing` or `solution fails`; the exit code is 1 when any task is not ok.

The report adds `## By suite kind` (solved/runs per pair for each kind) when the trials come from more than one kind, and a warning for every regression task a non-null pair did not solve. Without suites the report does not change.

`RunInfo.suite_version` records `name@version` of every suite used, then a hash of the tasks (the directories of a suite task enter the hash relative to the suite directory, so it does not depend on the checkout path; a git URL stays in it).

The example suites hold synthetic code written for this repository (MIT), standard library only, no network (except `examples-artifacts`, whose judge needs a gateway): `regression/fizzbuzz`, `examples-artifacts` with `html-report` (turn a small CSV into `report.html` and `summary.json`, graded by `html_check`, `structured_output` and an `llm_rubric` judge; it needs a `judge` section, so validate it with `--config`) and `examples-capability` with `dependency-upgrade` (migrate a caller to the API of a vendored library), `path-traversal-fix` (CWE-22 "zip slip" in an archive extractor) and `extract-function` (a refactor guarded by existing tests). Every hidden test file carries a marker comment. What `tests/smoke.py` proves, exactly: hidden tests are never copied into the workdir before grading, and an unsandboxed fake agent cannot find them in its workdir (searched recursively) or in its parent directories. Positive controls plant a leak at the real depth of a hidden test and the probe must find it. It does **not** prove isolation from the rest of the host: an unsandboxed agent runs as your user and can read anything you can, including the suite sources (the same smoke test shows a probe over the benchmark directory finds them). Real isolation of the hidden tests needs the sandbox, which mounts only the workdir. A clone proves the tasks are sound with:

```bash
python3 -m hbench suite validate --run --allow-unsandboxed suites/regression suites/examples-capability
```

{suite_blocks(SUITE_FILES)}Files of the example suites:

```
{suite_tree()}
```

`suites/regression/fizzbuzz/repo/fizzbuzz.py` (deliberate bugs):

```python
{fz}```

`suites/regression/fizzbuzz/hidden_tests/test_hidden.py`:

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

Golden tests (`python3 -m unittest discover -s tests -p 'test_*.py'`) freeze the `report` and `run` output against files in `tests/golden/`. `tests/test_golden.py`:

```python
{tgold}```

`tests/fixtures.py` (shared by the smoke and golden tests):

```python
{tfix}```

Unit tests call the engine in-process (`python3 -m unittest discover -s tests -p 'test_*.py'`), so `make coverage` sees every line.

`tests/test_parsers.py`:

```python
{tpars}```

`tests/test_workspace.py`:

```python
{twork}```

`tests/test_run.py`:

```python
{trun}```

`tests/test_litellm.py`:

```python
{tlite}```

{hbench_tests_md}## Sandbox

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
"""
(b / "HARNESS_BENCH.md").write_text(md)
print(f"wrote {b / 'HARNESS_BENCH.md'} ({len(md.splitlines())} lines)")

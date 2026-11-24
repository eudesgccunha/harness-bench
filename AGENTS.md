# AGENTS.md

**Harness × model** benchmark for coding agents. Main metric: **cost per solved task**. The real benchmark runs on another machine, with real CLIs and LiteLLM. In this repository, verification is offline, with fake agents and a fake LiteLLM.

## Map

- `harness_bench.py`: the engine and single source of truth. Contains `run`, `reconcile` (real cost from LiteLLM) and `report`; the parsers live in `hbench/runners/` and are re-exported here.
- `harness_bench_litellm.py`: shim over `python3 -m hbench <command> --litellm`. The LiteLLM profile (drops harnesses with `parser: cursor`, forces LiteLLM cost) lives in `hbench/profiles.py`.
- `hbench/`: the package for new code ([ADR 0005](docs/adr/0005-data-contract-and-package.md)), strict mypy. `core/` is the versioned data contract (`models.py`, `ids.py`, `schema.py`, `convert.py`); `runners/` holds the `HarnessRunner` protocol and one parser module per harness; `graders/` holds the `Grader` protocol, the bundled graders and composition; `plugins.py` loads both through entry points ([ADR 0007](docs/adr/0007-runner-and-grader-protocols.md)). Runners and graders never start processes: only the engine does, through `run_step` and `wrap()`; `results.py` writes `<output_dir>/<run_id>/run.json` and `trials.jsonl`; `stats.py` holds the pure statistics (Wilson, pass@k, bootstrap, McNemar, Pareto, ties; [ADR 0006](docs/adr/0006-report-statistics.md)); `report.py` renders `summary.md`/`summary.csv` from trials; `cli.py` is the single CLI (`python3 -m hbench {run,report,reconcile,schema export}`). Run `python3 -m hbench schema export` for the JSON Schema of the output.
- `HARNESS_BENCH.md`: **generated** by `scripts/build_md.py`. Edit the generator and regenerate the file.
- `tests/`: `fake_agent.py` mimics each CLI's JSON output and probes for leaks (env, git history, test tampering). `fake_litellm.py` mimics `/spend/logs/v2`, and `smoke.py` tests everything end to end. `test_golden.py` freezes `run` and `report` output against `tests/golden/`. `test_parsers.py`, `test_workspace.py`, `test_run.py`, `test_litellm.py`, `test_core_models.py`, `test_core_schema.py`, `test_contract_output.py`, `test_report.py`, `test_stats.py` and `test_cli.py` call the engine and `hbench` in-process (coverage is measured; `fixtures.py` holds the shared fake setup). Test files are typed and checked by mypy.
- `sandbox/`: `Dockerfile` for the image with the CLIs, and `entrypoint.sh`, which renders the harness configs from the `*.example.*` files using `LITELLM_BASE_URL`. `.dockerignore` is an allowlist: any new file used in the build must be added to it.
- `docs/evolution-plan.md`: phased plan for the project. Decisions in `docs/adr/`, terms in `CONTEXT.md`, current structure in `docs/architecture.md`, task plans in `docs/superpowers/plans/`.
- `pyproject.toml`: project metadata for uv, dev dependency group and config for ruff, mypy, bandit and coverage (no runtime dependencies). `uv.lock`: pinned dev tools ([ADR 0004](docs/adr/0004-uv-dev-tooling.md)). `Makefile`: common commands, run `make help`. `.github/workflows/ci.yml`: CI. `.pre-commit-config.yaml` and `.gitleaks.toml`: local hooks and secret scanning.
- `*.example.*`: example configs for the benchmark (`config*.json`) and for each harness pointing at LiteLLM (Pi, OpenCode, Codex).

## Rules

- Python 3.12, standard library only. The runtime environment does not install dependencies.
- The only network access is a `GET` to the configured LiteLLM, made by `reconcile`.
- Secrets come in only through env (`${VAR}` in the config). `config*.json` (except examples), `results*/` and `.cache/` stay out of git, because they contain internal URLs, code and agent responses.
- Everything in English: docs, report, CLI messages, example configs, code and comments.
- Unknown cost is `None`, never `0`. A false `0` sends the pair to the top of the cost ranking.
- Follow widely accepted engineering practices and design patterns: small cohesive functions and modules, clear interfaces, DRY, YAGNI, PEP 8, type hints everywhere (mypy strict covers the whole repo). Patterns never override the rules above, and no abstraction goes in before a phase of `docs/evolution-plan.md` needs it.
- The agent and the task repository are **untrusted**. Every change to `cmd_run` keeps the defenses: workdir outside the repository and without git history; the baseline git dir in a private temp dir outside the workdir and `work_dir`, every git call on the workdir hardened (`hardened_git`: no replace refs, no ignore rules, no user/system config, no hooks or fsmonitor, no inherited `GIT_*`, `info/attributes` in the baseline git dir unsets every attribute that changes how a file is added, so the workdir `.gitattributes` is ignored on any git version, plus `--attr-source` with the empty tree as a second layer on git 2.40 or newer) and every diff built from a fresh index, with a `.git` left in the workdir, a failed `git add`/`read-tree` or a missing `info/attributes` failing the changes closed (a binary file counts as more than any `max_changed_lines`). Diff integrity against a hostile agent is guaranteed only in sandboxed runs; environment from an allowlist (`BASE_ENV` + `env_passthrough` + the harness `env`), without the spend key; `protected_paths` restored from a snapshot outside the workdir; audit diff; redacted logs; `rmtree_inside` for deletion; commands without `shell=True` and passed through `wrap()` (sandbox). `tests/smoke.py` has one case per defense. A new defense gets a new case.

## Usage semantics per harness

Checked against documentation and community sources in Nov 2026. The format changes between CLI versions.

- Tool calls are counted where the output exposes them and are `None` (never `0`) otherwise; one unknown trial makes the pair unknown in the report.
- `claude`: sum `modelUsage`, which includes auxiliary models. Without `modelUsage`, use `usage`. Tool calls: unique `tool_use` blocks in `stream-json` `assistant` events; the single JSON result has none (`None`).
- `codex`: the `usage` in `turn.completed` is the cumulative thread total, so use only the last one. `cached_input_tokens` is part of `input_tokens`, and `reasoning_output_tokens` is part of `output_tokens`. Tool calls: completed `command_execution`, `file_change`, `mcp_tool_call` and `web_search` items (`None` until an `item.completed` event is seen, so a CLI version without item events is not reported as 0).
- `pi`: count only `message_end` with `role: assistant`. The same usage also appears in `message_update` and `turn_end`. Tool calls: `toolCall` blocks in the `content` of those messages (`None` when no message has a `content` list).
- `opencode`: sum every `step_finish`. `reasoning` is separate from `output`. Tool calls: `tool_use` events (`None` until the first one, so a run with no tool events is unknown, not 0; the trade-off: an OpenCode run with genuinely zero tool calls also reports `None`).
- `cursor`: the official JSON has no usage. The parser returns `None` and the pair reports only solve rate and time. No tool calls.
- `null`: no model is called, so usage is zero, the cost is a real `0.0` and tool calls are a real `0`.
- Real cost comes from LiteLLM. Each run exports `BENCH_TAG`, the harness sends the tag in the `x-litellm-tags` header, and `reconcile` filters the logs by `request_tags`.

## Adding a harness

1. Write `parse_<name>(stdout) -> dict | None` in `hbench/runners/<name>.py` and register it in `PARSERS` (`hbench/runners/__init__.py`). Count tool calls if the format exposes them, else leave `tool_calls` as `None`.
2. Add the output format to `tests/fake_agent.py` and asserts to `tests/smoke.py`.
3. Add the harness to `config.example.json`. If it goes through LiteLLM, also add it to `config.litellm.example.json`, with an example config that sends `BENCH_TAG`.
4. Document it in `scripts/build_md.py`.

## Done when

`make check` is green (run `make sync` once first; needs [uv](https://docs.astral.sh/uv/)). It runs: `ruff check`, `ruff format --check`, `mypy`, bandit (+ gitleaks if installed), actionlint, `make coverage` (smoke and unittest suite under coverage, with `fail_under = 100`, the measured total; add tests for new code instead of lowering it) and `make docs-check` (regenerates `HARNESS_BENCH.md` and fails if it changed). Without uv, `python3 tests/smoke.py` must end with `PASS` and `python3 -m unittest discover -s tests -p 'test_*.py'` must be OK. If you touched `sandbox/`, `wrap()` or the env file, rebuild the image and run `make docker-e2e` until `PASS`.

Regenerate goldens with `HB_UPDATE_GOLDEN=1` only for an intended behavior change, and say so in the PR.

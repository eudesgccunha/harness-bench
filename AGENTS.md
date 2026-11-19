# AGENTS.md

**Harness × model** benchmark for coding agents. Main metric: **cost per solved task**. The real benchmark runs on another machine, with real CLIs and LiteLLM. In this repository, verification is offline, with fake agents and a fake LiteLLM.

## Map

- `harness_bench.py`: the engine and single source of truth. Contains `run`, `reconcile` (real cost from LiteLLM), `report` and one parser per harness.
- `harness_bench_litellm.py`: thin wrapper over the engine. Drops harnesses with `parser: cursor` and forces LiteLLM cost. New logic goes into the engine; the wrapper only filters the config and chains commands.
- `HARNESS_BENCH.md`: **generated** by `scripts/build_md.py`. Edit the generator and regenerate the file.
- `tests/`: `fake_agent.py` mimics each CLI's JSON output and probes for leaks (env, git history, test tampering). `fake_litellm.py` mimics `/spend/logs/v2`, and `smoke.py` tests everything end to end.
- `sandbox/`: `Dockerfile` for the image with the CLIs, and `entrypoint.sh`, which renders the harness configs from the `*.example.*` files using `LITELLM_BASE_URL`. `.dockerignore` is an allowlist: any new file used in the build must be added to it.
- `*.example.*`: example configs for the benchmark (`config*.json`) and for each harness pointing at LiteLLM (Pi, OpenCode, Codex).

## Rules

- Python 3.12, standard library only. The runtime environment does not install dependencies.
- The only network access is a `GET` to the configured LiteLLM, made by `reconcile`.
- Secrets come in only through env (`${VAR}` in the config). `config*.json` (except examples), `results*/` and `.cache/` stay out of git, because they contain internal URLs, code and agent responses.
- Everything in English: docs, report, CLI messages, example configs, code and comments.
- Unknown cost is `None`, never `0`. A false `0` sends the pair to the top of the cost ranking.
- The agent and the task repository are **untrusted**. Every change to `cmd_run` keeps the defenses: workdir outside the repository and without git history; environment from an allowlist (`BASE_ENV` + `env_passthrough` + the harness `env`), without the spend key; `protected_paths` restored from a snapshot outside the workdir; audit diff; redacted logs; `rmtree_inside` for deletion; commands without `shell=True` and passed through `wrap()` (sandbox). `tests/smoke.py` has one case per defense. A new defense gets a new case.

## Usage semantics per harness

Checked against documentation and community sources in Nov 2026. The format changes between CLI versions.

- `claude`: sum `modelUsage`, which includes auxiliary models. Without `modelUsage`, use `usage`.
- `codex`: the `usage` in `turn.completed` is the cumulative thread total, so use only the last one. `cached_input_tokens` is part of `input_tokens`, and `reasoning_output_tokens` is part of `output_tokens`.
- `pi`: count only `message_end` with `role: assistant`. The same usage also appears in `message_update` and `turn_end`.
- `opencode`: sum every `step_finish`. `reasoning` is separate from `output`.
- `cursor`: the official JSON has no usage. The parser returns `None` and the pair reports only solve rate and time.
- Real cost comes from LiteLLM. Each run exports `BENCH_TAG`, the harness sends the tag in the `x-litellm-tags` header, and `reconcile` filters the logs by `request_tags`.

## Adding a harness

1. Write `parse_<name>(stdout) -> dict | None` in `harness_bench.py` and register it in `PARSERS`.
2. Add the output format to `tests/fake_agent.py` and asserts to `tests/smoke.py`.
3. Add the harness to `config.example.json`. If it goes through LiteLLM, also add it to `config.litellm.example.json`, with an example config that sends `BENCH_TAG`.
4. Document it in `scripts/build_md.py`.

## Done when

`python3 tests/smoke.py` ends with `PASS` and `python3 scripts/build_md.py` has regenerated `HARNESS_BENCH.md`. If you touched `sandbox/`, `wrap()` or the env file, rebuild the image and run `python3 tests/docker_e2e.py` until `PASS`.

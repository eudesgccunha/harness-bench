# Phase 1: Data Contract and Structured Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a versioned data contract (`hbench/core/`), write `run.json` + `trials.jsonl` per run, derive the report from the contract, add a single CLI with `schema export`, and close the Phase 0 follow-ups, without changing the golden report.

**Architecture:** New code lives in the `hbench/` package; the engine stays in `harness_bench.py` and calls into `hbench`. Legacy `results.jsonl` keeps working for resume and reconcile. Decisions: [ADR 0005](../../adr/0005-data-contract-and-package.md).

**Tech Stack:** Python 3.12 stdlib (`dataclasses`, `enum.StrEnum`, `uuid`, `hashlib`, `json`, `argparse`); dev tools via uv (`make check`).

**Spec:** [`docs/evolution-plan.md`](../../evolution-plan.md) section 3 and Phase 1, plus the "Follow-ups from Phase 0 review" list.

## Global Constraints

- Python 3.12, standard library only at runtime. No new runtime dependency.
- Unknown cost is `None`, never `0`.
- Everything in English. Type hints on all new code; `hbench/` is mypy `strict`.
- Every AGENTS.md defense stays, and every `tests/smoke.py` case keeps passing unchanged.
- `make check` green after every task: ruff, format, mypy, bandit, actionlint, coverage `fail_under = 100`, docs-check.
- Goldens (`tests/golden/summary.md`, `summary.csv`) must not change. `tests/golden/run.jsonl` must not change either (the legacy row is untouched).
- Every new source file is added to `scripts/build_md.py` and to the mypy file list in `pyproject.toml`; `HARNESS_BENCH.md` regenerated.
- No commits: the orchestrator commits when the user asks.

## Review Focus

- Old `results.jsonl` rows written before this phase (no `parse_ok`, no `sandboxed`, no `cost_*`): `report` must still work. Pin with a test in Task 5.
- `reconcile` on a results folder with no `<run_id>/` dirs (pre-Phase-1 output): must still update `results.jsonl` and not crash. Test in Task 4.
- Resume (`done_keys`) after a run was interrupted: a second `run` writes a new `<run_id>/` dir and skips finished keys. Test in Task 4.
- `trials.jsonl` must hold no secret, prompt, stdout or diff. Test in Task 4 greps the file for the fake API key and the prompt.
- `python3 harness_bench.py ...` and `python3 harness_bench_litellm.py ...` keep the same flags and exit codes. Covered by smoke and goldens; add a test for `--help` of both shims in Task 6.

---

### Task 1: Phase 0 follow-ups in the engine

**Files:**
- Modify: `harness_bench.py` (`cmd_run` `step`, `git`, `cmd_report`, long lines)
- Modify: `pyproject.toml` (drop the per-file E501 ignore for `harness_bench.py`, drop global B023 ignore, drop bandit B607 skip if no other hit remains)
- Test: `tests/test_run.py`, `tests/test_workspace.py`

**Interfaces:**
- Produces: `GIT: str` module constant (absolute path from `shutil.which("git")`, `sys.exit("git not found on PATH")` at first use if missing — resolve lazily in a helper `git_bin() -> str` so import never fails).

- [x] **Step 1:** Replace test `test_empty_results_file_crashes_with_index_error` with `test_empty_results_file_exits_with_no_results`: `report` on an empty `results.jsonl` raises `SystemExit` whose message contains `no results in`.
- [x] **Step 2:** Run `make unit`; expect that test to FAIL.
- [x] **Step 3:** In `cmd_report`, after reading rows: `if not rows: sys.exit(f"no results in {results_path}")`.
- [x] **Step 4:** Turn `step()` into a module-level function `run_step(cfg, command, env, timeout, log_path, workdir, env_file, secrets) -> tuple[int | None, bool]` with the same body; call it from `cmd_run`. Remove the B023 ignore.
- [x] **Step 5:** Every `git` subprocess call uses `git_bin()`. Add a test that `git_bin()` exits with `git not found on PATH` when `shutil.which` returns `None` (patch it).
- [x] **Step 6:** Wrap lines over 120 chars in `harness_bench.py`; remove its E501 per-file ignore.
- [x] **Step 7:** `make check` green; goldens untouched (`git diff --exit-code tests/golden`). Tick the four follow-ups in `docs/evolution-plan.md`.

### Task 2: Contract models in `hbench/core/`

**Files:**
- Create: `hbench/__init__.py`, `hbench/core/__init__.py`, `hbench/core/models.py`, `hbench/core/ids.py`
- Test: `tests/test_core_models.py`

**Interfaces:**
- Produces (`hbench/core/models.py`):
  - `SCHEMA_VERSION = "1.0.0"`
  - `class ContractError(ValueError)`
  - `class CostSource(StrEnum)`: `GATEWAY="gateway"`, `PRICE_TABLE="price_table"`, `HARNESS="harness"`, `UNKNOWN="unknown"`
  - `class TrialStatus(StrEnum)`: `PASSED`, `FAILED`, `ERROR`, `TIMEOUT` (lowercase values)
  - `Target(harness: str, harness_version: str, model: str, params: Mapping[str, str|int|float|bool])`, property `target_id -> str` = `f"{harness}@{harness_version}:{model}"`
  - `GraderResult(grader: str, grader_version: str, passed: bool, score: float | None = None, timed_out: bool = False, details_uri: str | None = None)`; `score` in `[0, 1]` when set
  - `Costs(gateway: float | None = None, price_table: float | None = None, harness: float | None = None)`; method `pick(source: CostSource | None = None) -> tuple[float | None, CostSource]` (None = order gateway > price_table > harness; unknown gives `(None, UNKNOWN)`); negative cost raises `ContractError`
  - `Usage(input_tokens: int, output_tokens: int, reasoning_tokens: int = 0, cache_read_tokens: int = 0, cache_write_tokens: int = 0, turns: int = 0, parsed: bool, costs: Costs)`; properties `total_tokens`, `cost_usd`, `cost_source` (from `costs.pick()`); negative counts raise `ContractError`
  - `TrialResult(schema_version, run_id, trial_id, task_id, suite_version, target, rep (>=1), status, graders: tuple[GraderResult, ...], usage, latency_s (>=0), started_at: datetime (tz-aware), agent_exit_code: int | None, sandboxed: bool, image_digest: str | None = None, trace_id: str | None = None, artifact_uri: str | None = None)`; property `passed -> bool`
  - `RunInfo(schema_version, run_id, started_at, finished_at: datetime | None, harness_bench_version: str, config_sha256: str, suite_version: str, python: str, platform: str, sandboxed: bool, targets: tuple[Target, ...])`
  - Every model: `to_dict() -> dict[str, Any]` (JSON-ready: enums as values, datetimes ISO 8601, tuples as lists) and `@classmethod from_dict(d) -> Self` that raises `ContractError` on unknown or missing fields and on a `schema_version` with another major version.
- Produces (`hbench/core/ids.py`): `new_run_id() -> str` (UUID4 hex), `trial_id(run_id: str, task_id: str, target_id: str, rep: int) -> str` (first 16 hex of sha256 of `"|".join(...)`), `sha256_json(obj: object) -> str` (sha256 hex of `json.dumps(obj, sort_keys=True, separators=(",", ":"))`).

- [x] **Step 1: Write failing tests** in `tests/test_core_models.py`: round trip `from_dict(to_dict(x)) == x` for each model; `from_dict` rejects an extra key (`ContractError` naming the key) and a missing required key; `schema_version="2.0.0"` rejected, `"1.4.0"` accepted; `Costs(gateway=None, price_table=0.5, harness=0.7).pick() == (0.5, PRICE_TABLE)`; `Costs().pick() == (None, UNKNOWN)`; `Costs(harness=-1)` raises; `trial_id` stable for same inputs and different for another `rep`; naive `started_at` rejected; `GraderResult(score=1.5)` rejected; models are frozen (`dataclasses.FrozenInstanceError`).
- [x] **Step 2:** `make unit`; expect FAIL (import error).
- [x] **Step 3:** Implement. Share the field checking in one helper (`_check_fields(cls, d)`), not per class.
- [x] **Step 4:** `make check` green (add files to mypy and `build_md.py`).

### Task 3: JSON Schema and validator

**Files:**
- Create: `hbench/core/schema.py`
- Test: `tests/test_core_schema.py`

**Interfaces:**
- Consumes: Task 2 models.
- Produces: `TRIAL_SCHEMA: dict[str, Any]`, `RUN_SCHEMA: dict[str, Any]` (draft 2020-12, `$id` `https://harness-bench.local/schema/<name>/1.0.0`, `additionalProperties: false` at every object level); `schema_document() -> dict[str, Any]` (`{"trial": TRIAL_SCHEMA, "run": RUN_SCHEMA}`); `validate(instance: object, schema: Mapping[str, Any]) -> list[str]` returning error paths like `"$.usage.input_tokens: expected integer"`. Supported keywords: `type` (incl. list with `"null"`), `properties`, `required`, `additionalProperties: false`, `items`, `enum`, `minimum`, `maximum`, `$defs` + local `$ref`. Any other keyword in a schema raises `ValueError` (so the schema cannot silently use something unchecked).

- [x] **Step 1: Failing tests:** a valid `TrialResult.to_dict()` gives `[]`; wrong type, missing required, extra key, bad enum and `rep=0` each give one error with the right path; `bool` is not accepted as `integer`; drift test: for every dataclass in `models.py`, the set of `fields()` equals the schema's `properties` keys and required keys equal fields without defaults (+ `schema_version`).
- [x] **Step 2:** `make unit`; FAIL.
- [x] **Step 3:** Implement.
- [x] **Step 4:** `make check` green.

### Task 4: Run and reconcile write the contract

**Files:**
- Create: `hbench/core/convert.py`, `hbench/results.py`
- Modify: `harness_bench.py` (`cmd_run`, `cmd_reconcile`)
- Test: `tests/test_contract_output.py`

**Interfaces:**
- Produces (`hbench/core/convert.py`): `trial_from_row(row: Mapping[str, Any], *, run_id: str, suite_version: str, harness_version: str = "unknown", test_timed_out: bool = False) -> TrialResult`. Mapping: `task→task_id`, `harness/model→Target`, `passed→status/grader("test", "1")`, `agent_timed_out or test_timed_out` and not passed → `TIMEOUT`, `wall_s→latency_s`, `ts→started_at` (fallback `t_start` epoch, then epoch 0 UTC), usage keys `input/output/reasoning/cache_read/cache_write/turns` (missing → 0), `parse_ok→parsed` (missing → True), `cost_litellm→gateway`, `cost_computed→price_table`, `cost_reported→harness`, `run_tag→trace_id`, `agent_rc→agent_exit_code`, `sandboxed` (missing → False), `artifact_uri = f"logs/{tag}"` when `run_tag` present (tag = part after the timestamp prefix) else None.
- Produces (`hbench/results.py`): `RunWriter(out_dir: Path, info: RunInfo)` with `append(trial: TrialResult) -> None` (one JSON line, flush) and `close(finished_at: datetime) -> None` (rewrites `run.json` with `finished_at`); `run.json` is written at construction. `apply_gateway_costs(out_dir: Path, costs_by_tag: Mapping[str, float]) -> int` rewrites every `<out_dir>/*/trials.jsonl`, setting `usage.costs.gateway` for trials whose `trace_id` matches; returns the number of trials updated; writes via a temp file + `os.replace`.
- `cmd_run` builds `RunInfo` (`harness_bench_version` = `"0.0.0"` from a module constant, `config_sha256 = sha256_json(cfg)` — the config holds `${VAR}` references, never expanded values; `suite_version = sha256_json(cfg["tasks"])[:12]`; `platform.platform()`, `platform.python_version()`; targets from the matrix filtered by `--only`; `harness_version` from optional harness config key `version`, default `"unknown"`). Dry run writes nothing new. The legacy row stays byte-for-byte as today.
- `cmd_reconcile` calls `apply_gateway_costs` with the same per-tag spend sums it writes to `results.jsonl`, and prints `contract: N trials updated`.

- [x] **Step 1: Failing tests:** after a fake run (reuse `tests/fixtures.py`), exactly one `<run_id>/` dir with `run.json` and `trials.jsonl`; every line validates against `TRIAL_SCHEMA` and `run.json` against `RUN_SCHEMA`; trial count equals legacy row count; `trial_id`s unique; `trials.jsonl` does not contain the fake API key nor the task prompt; a second run with all keys done creates a new run dir with zero trials and a `run.json` with `finished_at`; reconcile with the fake LiteLLM sets `usage.costs.gateway` and `cost_source == "gateway"`; reconcile on an output dir without run dirs still updates `results.jsonl`; `trial_from_row` on a minimal pre-Phase-1 row (`task, harness, model, rep, passed, input, output, cache_read, cache_write, turns, wall_s`) works; timeout mapping.
- [x] **Step 2:** `make unit`; FAIL.
- [x] **Step 3:** Implement. Keep the defenses: no change to env, workdir, wrap, redaction or protected paths.
- [x] **Step 4:** `make check` green; `git diff --exit-code tests/golden`.

### Task 5: Report from the contract

**Files:**
- Create: `hbench/report.py`
- Modify: `harness_bench.py` (`aggregate`, `cmd_report` become thin calls, or are moved and re-exported under the same names)
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `trial_from_row` (Task 4).
- Produces: `aggregate(trials: Sequence[TrialResult], cost_source: str) -> list[dict[str, Any]]` (same keys and order as today; `cost_source` in `auto|litellm|computed|reported`, mapped to `None|GATEWAY|PRICE_TABLE|HARNESS`, and the table column keeps the legacy names `litellm/computed/reported`); `render_markdown(table, trials, cost_source, now: datetime) -> str`; `write_csv(table, path: Path) -> None`. `harness_bench.aggregate` and `harness_bench.cmd_report` keep their names and signatures for the wrapper and tests (`aggregate(rows, cost_source)` converts rows first).

- [x] **Step 1: Failing tests:** `render_markdown` with a fixed `now` equals `tests/golden/summary.md` byte for byte (no masking needed); legacy rows without `parse_ok`/`cost_*` aggregate without error; forced `cost_source="litellm"` with a missing gateway cost gives `cost_total None`.
- [x] **Step 2:** `make unit`; FAIL.
- [x] **Step 3:** Implement; goldens must pass untouched.
- [x] **Step 4:** `make check` green.

### Task 6: Single CLI, shims and `schema export`

**Files:**
- Create: `hbench/cli.py`, `hbench/__main__.py`, `hbench/profiles.py`
- Modify: `harness_bench.py` (`main` delegates), `harness_bench_litellm.py` (shim), `scripts/build_md.py`, `AGENTS.md` (Map, "Adding a harness" unchanged), `docs/architecture.md`, `CONTEXT.md`, `docs/evolution-plan.md` (tick Phase 1), `docs/README.md` (ADR 0005, this plan)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `hbench.cli.build_parser() -> argparse.ArgumentParser`, `hbench.cli.main(argv: Sequence[str] | None = None) -> None`. Subcommands `run`, `report`, `reconcile` with today's flags plus `--litellm` (and `--settle`, default 60, for `run`); `schema export [--out PATH]` prints `json.dumps(schema_document(), indent=2, sort_keys=True)` or writes it. `hbench.profiles.apply_litellm_profile(cfg: dict[str, Any]) -> dict[str, Any]` holds today's `load_config` filtering (returns a new dict; exits with the same messages). The CLI imports `harness_bench` inside the dispatch function, so `harness_bench.main()` can call `hbench.cli.main()` without an import cycle.
- `harness_bench.py main()` → `hbench.cli.main()`. `harness_bench_litellm.py main()` → `hbench.cli.main([argv[0], "--litellm", *argv[1:]])` after checking a subcommand is present; keep `load_config` as a thin wrapper over `apply_litellm_profile` for compatibility.

- [x] **Step 1: Failing tests:** `python3 -m hbench schema export` stdout parses as JSON equal to `schema_document()`; `--out` writes the same; `python3 harness_bench.py --help` and `python3 harness_bench_litellm.py --help` exit 0 and list `run report reconcile`; `--litellm run` drops cursor harnesses and forces `cost_source == "litellm"` (patch `time.sleep`); `apply_litellm_profile` does not mutate its input.
- [x] **Step 2:** `make unit`; FAIL.
- [x] **Step 3:** Implement; smoke and goldens unchanged.
- [x] **Step 4:** Docs: AGENTS.md Map adds `hbench/` (contract, CLI) and "run `python3 -m hbench schema export` for the JSON Schema"; `docs/architecture.md` adds a "Phase 1" section listing the new modules; `CONTEXT.md` maps Trial → `TrialResult`, Run → `RunInfo`; tick Phase 1 items.
- [x] **Step 5:** `make check` green; `git diff --exit-code tests/golden`.

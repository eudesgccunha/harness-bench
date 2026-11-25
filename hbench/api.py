"""The stable surface for orchestrators (ADR 0012): `load_config`, `start_run`, `plan`, `run_trial`, `finish_run`.

An orchestrator plans a run once, runs each `TrialSpec` anywhere (a process, a CI job, a pod) and finishes the run:

    config = load_config(path)
    run = start_run(config)
    for spec in plan(config, run):
        run_trial(config, spec)  # the specs are independent: run them in parallel, on any worker
    finish_run(config, run.run_id)

A `TrialSpec` names a trial and carries no command, prompt, path, env or secret: the worker resolves everything from
its own copy of the config, and `run_trial` refuses a spec that does not match it (`SpecError`) before anything runs.
Every defense of `run` applies to `run_trial`: it is the same `_run_trial` of the engine, in its own private dirs.
"""

import argparse
import contextlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from hbench.core.models import RunInfo, TrialResult, TrialSpec
from hbench.profiles import apply_litellm_profile
from hbench.results import ResumeError, RunWriter, read_run, trial_lock


class ApiError(Exception):
    """Base of every error of this module; the message is safe to show (it holds no secret)."""


class ConfigError(ApiError):
    """The config cannot be read, is not valid, or an argument (`reps`) is not."""


class SpecError(ApiError):
    """A `TrialSpec` is malformed, or does not match this config or run. Nothing ran and nothing was written."""


class RunError(ApiError):
    """A run directory is missing or invalid, was started with another config, or its report could not be written."""


class SandboxRequiredError(ApiError):
    """No sandbox is configured and the caller did not accept running without one."""


def _engine() -> ModuleType:
    """The engine module. Imported here, not at the top: the CLI imports this module."""
    from hbench.cli import _engine as engine_module

    return engine_module()


@dataclass(frozen=True)
class Config:
    """The expanded, validated config (the tasks of its suites included) and the directory its paths resolve from."""

    cfg: Mapping[str, Any]
    base: Path

    @property
    def out_dir(self) -> Path:
        return self.base / str(self.cfg.get("output_dir", "results"))


@contextlib.contextmanager
def _exits_as(error: type[ApiError]) -> Iterator[None]:
    """The engine reports a bad config or a refusal by exiting with a message; a library raises instead."""
    try:
        yield
    except SystemExit as e:
        raise error(str(e.code)) from None


def config_from(raw: Mapping[str, Any], base: Path) -> Config:
    """The `Config` of an already parsed config file living in `base`."""
    engine = _engine()
    with _exits_as(ConfigError):
        expanded: Mapping[str, Any] = engine.expand_suites(raw, base)
        engine.validate_config(expanded)
    return Config(cfg=expanded, base=base)


def load_config(path: Path | str, *, litellm: bool = False) -> Config:
    """Read, expand (suites) and validate the config file at `path`. With `litellm`, the LiteLLM profile applies."""
    cfg_path = Path(path).resolve()
    try:
        raw = json.loads(cfg_path.read_text())
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read the config {cfg_path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"the config {cfg_path} must be a JSON object")
    if litellm:
        with _exits_as(ConfigError):
            raw = apply_litellm_profile(raw)
    return config_from(raw, cfg_path.parent)


def _reps(config: Config, reps: int | None) -> int:
    count = config.cfg.get("reps", 1) if reps is None else reps
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ConfigError(f"reps must be a whole number of at least 1, got {count!r}")
    return count


def _config_hash(config: Config) -> str:
    result: str = _engine().config_hash(config.cfg)
    return result


def start_run(config: Config, *, only: Sequence[str] | None = None, reps: int | None = None) -> RunInfo:
    """Create `<output_dir>/<run_id>/run.json` and `trials.jsonl`, as `run` does, and return the run."""
    count = _reps(config, reps)  # a bad argument stops before a run directory exists
    engine = _engine()
    config.out_dir.mkdir(parents=True, exist_ok=True)
    (config.out_dir / "logs").mkdir(exist_ok=True)
    info: RunInfo = engine.make_run_info(config.cfg, only, engine.make_sandbox(config.cfg), count)
    return RunWriter(config.out_dir, info).info


def _open_writer(config: Config, run_id: str) -> RunWriter:
    """The writer of a run that exists and was started with this config; `RunError` otherwise."""
    try:
        writer = RunWriter.open_existing(config.out_dir, run_id, repair=False)
    except ResumeError as e:
        raise RunError(str(e)) from e
    if writer.info.config_sha256 != _config_hash(config):
        raise RunError(f"the config changed since run {run_id} started; use the config it started with")
    return writer


def open_run(config: Config, run_id: str) -> RunInfo:
    """A run `start_run` made earlier (to plan it again). Raises `RunError` when it does not exist, is not valid
    contract data or was started with another config."""
    return _open_writer(config, run_id).info


def plan(
    config: Config, run: RunInfo, *, only: Sequence[str] | None = None, reps: int | None = None
) -> list[TrialSpec]:
    """Every trial of the run, in the order `run` executes them: pairs, then tasks, then repetitions. Pure: it reads
    nothing from disk and starts nothing."""
    count = _reps(config, reps if reps is not None else run.reps)
    if run.reps is not None and count > run.reps:
        raise RunError(f"run {run.run_id} plans {run.reps} repetition(s), not {count}")
    sha = _config_hash(config)
    if run.config_sha256 != sha:
        raise RunError(f"the config changed since run {run.run_id} started; use the config it started with")
    engine = _engine()
    return [
        TrialSpec(
            run_id=run.run_id,
            trial_id=engine.planned_trial_id(run.run_id, planned),
            task_id=planned.task["id"],
            harness=planned.hname,
            model=planned.model,
            rep=planned.rep,
            config_sha256=sha,
        )
        for planned in engine.planned_trials(config.cfg, only, count)
    ]


def parse_spec(text: str) -> TrialSpec:
    """The `TrialSpec` in one JSON text. Raises `SpecError` for anything else: bad JSON, unknown or missing keys, a
    value that does not fit (a path-like `run_id`, an empty name)."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("a spec is a JSON object")
        return TrialSpec.from_dict(data)
    except ValueError as e:  # ContractError and json.JSONDecodeError are both ValueErrors
        raise SpecError(f"not a valid trial spec: {e}") from e


def _resolve(config: Config, spec: TrialSpec) -> Any:
    """The engine's planned trial that `spec` names. Raises `SpecError` unless the config hash, the task, the harness,
    the pair and the recomputed `trial_id` all agree with this config. Reads no file."""
    engine, cfg = _engine(), config.cfg
    if spec.config_sha256 != _config_hash(config):
        raise SpecError("config_sha256 is not the hash of this config: the worker must use the config of the plan")
    task = next((t for t in cfg["tasks"] if t["id"] == spec.task_id), None)
    if task is None:
        raise SpecError(f"unknown task {spec.task_id!r}")
    if spec.harness not in cfg["harnesses"]:
        raise SpecError(f"unknown harness {spec.harness!r}")
    pair = next((p for p in cfg["matrix"] if (p["harness"], p["model"]) == (spec.harness, spec.model)), None)
    if pair is None:
        raise SpecError(f"{spec.harness!r} with model {spec.model!r} is not a pair of the matrix")
    planned = engine.planned_trial(cfg, pair, task, spec.rep)
    if engine.planned_trial_id(spec.run_id, planned) != spec.trial_id:
        raise SpecError("trial_id is not the one this config gives the trial")
    return planned


def _recorded(trials: Sequence[TrialResult], spec: TrialSpec) -> TrialResult | None:
    return next((t for t in trials if t.trial_id == spec.trial_id), None)


def _check_in_plan(config: Config, spec: TrialSpec, planned: Any, info: RunInfo) -> None:
    """The trial must belong to the run: its target is one of the run's targets (`--only` left the others out) and its
    repetition is within the reps the run planned (a run of an older version did not record them: the config's)."""
    target = (spec.harness, planned.runner.version(), spec.model)
    if target not in {(t.harness, t.harness_version, t.model) for t in info.targets}:
        raise SpecError(f"{spec.harness!r} with model {spec.model!r} is not a target of run {spec.run_id}")
    reps = info.reps if info.reps is not None else _reps(config, None)
    if spec.rep > reps:
        raise SpecError(f"rep {spec.rep} is beyond the {reps} repetition(s) run {spec.run_id} plans")


def run_trial(config: Config, spec: TrialSpec, *, allow_unsandboxed: bool = False) -> TrialResult:
    """Run the trial `spec` names and return its result, which is also in `trials.jsonl` (and `results.jsonl`).

    A spec that does not match the config or its run is a `SpecError`: nothing runs and nothing is written. A trial
    already in `trials.jsonl` returns that result and runs nothing (a retry is safe). Without a sandbox, a
    `SandboxRequiredError` unless `allow_unsandboxed`. The trial gets its own private work dir, baseline git dir,
    sandbox session and sink feed; they are removed when it returns or raises."""
    engine = _engine()
    planned = _resolve(config, spec)
    try:
        info, recorded = read_run(config.out_dir / spec.run_id)
    except ResumeError as e:
        raise SpecError(f"run {spec.run_id} cannot be used: {e}") from e
    if (info.run_id, info.config_sha256) != (spec.run_id, spec.config_sha256):
        raise SpecError(f"run {spec.run_id} was not started with this config")
    _check_in_plan(config, spec, planned, info)
    done = _recorded(recorded, spec)
    if done:
        return done
    with _exits_as(SandboxRequiredError):
        engine.require_sandbox(config.cfg, allow_unsandboxed)
    run_dir = config.out_dir / spec.run_id
    with trial_lock(run_dir, spec.trial_id):
        # A worker that held the lock first may have recorded the trial while this one waited.
        done = _recorded(read_run(run_dir)[1], spec)
        if done:
            return done
        writer = RunWriter(config.out_dir, info, existing=True)
        with _exits_as(ConfigError):
            result: TrialResult = engine.run_trial_isolated(config.cfg, config.base, writer, planned, spec.trial_id)
        return result


def finish_run(config: Config, run_id: str, *, report: bool = True) -> RunInfo:
    """Mark the run finished (a run that has a finish time keeps it), close its sinks with the finished run (this
    writes the run span) and, with `report`, write `summary.md` and `summary.csv`. Raises `RunError` when the run
    does not exist or was started with another config."""
    engine = _engine()
    writer = _open_writer(config, run_id)
    try:
        # A worker killed between the two files left a trial without its `results.jsonl` row, which the report reads.
        engine.restore_legacy_rows(writer, config.out_dir / "results.jsonl")
    except ResumeError as e:
        raise RunError(str(e)) from e
    if writer.info.finished_at is None:
        writer.close(datetime.now(UTC))
    feed = engine.sink_feed(config.cfg, config.base)
    feed.open(writer.info)
    feed.close(writer.info)
    if report:
        with _exits_as(RunError):
            engine.cmd_report(argparse.Namespace(), config.cfg, config.base)
    return writer.info

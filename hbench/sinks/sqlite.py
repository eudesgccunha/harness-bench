"""The `sqlite` sink: runs, trials and grader results in a SQLite file (ADR 0011).

The tables come from `schema.sql` in this package; `docs/sql/schema.sql` is the same file for readers (a test checks
they are equal).

Every statement is parameterized (a task id is data, never SQL) and every write is an upsert by `run_id` or
`trial_id`, so resume and `export` can send a trial again. Unknown tokens, tool calls and costs are NULL.
"""

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from hbench.core.models import RunInfo, TrialResult
from hbench.sinks.base import SinkError

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_UPSERT_RUN = """
INSERT INTO runs (run_id, started_at, finished_at, harness_bench_version, config_sha256, suite_version, sandbox,
                  sandboxed, schema_version)
VALUES (:run_id, :started_at, :finished_at, :harness_bench_version, :config_sha256, :suite_version, :sandbox,
        :sandboxed, :schema_version)
ON CONFLICT (run_id) DO UPDATE SET
    started_at = excluded.started_at,
    finished_at = excluded.finished_at,
    harness_bench_version = excluded.harness_bench_version,
    config_sha256 = excluded.config_sha256,
    suite_version = excluded.suite_version,
    sandbox = excluded.sandbox,
    sandboxed = excluded.sandboxed,
    schema_version = excluded.schema_version
"""

_UPSERT_TRIAL = """
INSERT INTO trials (trial_id, run_id, task_id, suite_version, harness, harness_version, model, target_id, rep, status,
                    passed, latency_s, started_at, agent_exit_code, sandboxed, image_digest, run_tag, usage_parsed,
                    input_tokens, output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens, turns,
                    tool_calls, cost_usd, cost_source, cost_gateway, cost_price_table, cost_harness)
VALUES (:trial_id, :run_id, :task_id, :suite_version, :harness, :harness_version, :model, :target_id, :rep, :status,
        :passed, :latency_s, :started_at, :agent_exit_code, :sandboxed, :image_digest, :run_tag, :usage_parsed,
        :input_tokens, :output_tokens, :reasoning_tokens, :cache_read_tokens, :cache_write_tokens, :turns,
        :tool_calls, :cost_usd, :cost_source, :cost_gateway, :cost_price_table, :cost_harness)
ON CONFLICT (trial_id) DO UPDATE SET
    run_id = excluded.run_id,
    task_id = excluded.task_id,
    suite_version = excluded.suite_version,
    harness = excluded.harness,
    harness_version = excluded.harness_version,
    model = excluded.model,
    target_id = excluded.target_id,
    rep = excluded.rep,
    status = excluded.status,
    passed = excluded.passed,
    latency_s = excluded.latency_s,
    started_at = excluded.started_at,
    agent_exit_code = excluded.agent_exit_code,
    sandboxed = excluded.sandboxed,
    image_digest = excluded.image_digest,
    run_tag = excluded.run_tag,
    usage_parsed = excluded.usage_parsed,
    input_tokens = excluded.input_tokens,
    output_tokens = excluded.output_tokens,
    reasoning_tokens = excluded.reasoning_tokens,
    cache_read_tokens = excluded.cache_read_tokens,
    cache_write_tokens = excluded.cache_write_tokens,
    turns = excluded.turns,
    tool_calls = excluded.tool_calls,
    cost_usd = excluded.cost_usd,
    cost_source = excluded.cost_source,
    cost_gateway = excluded.cost_gateway,
    cost_price_table = excluded.cost_price_table,
    cost_harness = excluded.cost_harness
"""

_INSERT_GRADER = """
INSERT INTO grader_results (trial_id, position, grader, grader_version, passed, score, timed_out, judge)
VALUES (:trial_id, :position, :grader, :grader_version, :passed, :score, :timed_out, :judge)
"""


def run_row(info: RunInfo) -> dict[str, Any]:
    return {
        "run_id": info.run_id,
        "started_at": info.started_at.isoformat(),
        "finished_at": info.finished_at.isoformat() if info.finished_at else None,
        "harness_bench_version": info.harness_bench_version,
        "config_sha256": info.config_sha256,
        "suite_version": info.suite_version,
        "sandbox": info.sandbox,
        "sandboxed": int(info.sandboxed),
        "schema_version": info.schema_version,
    }


def trial_row(trial: TrialResult) -> dict[str, Any]:
    """The `trials` row: token counts and the tool-call count are NULL when the harness output was not parsed."""
    usage, costs = trial.usage, trial.usage.costs
    known = usage.parsed
    return {
        "trial_id": trial.trial_id,
        "run_id": trial.run_id,
        "task_id": trial.task_id,
        "suite_version": trial.suite_version,
        "harness": trial.target.harness,
        "harness_version": trial.target.harness_version,
        "model": trial.target.model,
        "target_id": f"{trial.target.harness}/{trial.target.model}",
        "rep": trial.rep,
        "status": trial.status.value,
        "passed": int(trial.passed),
        "latency_s": trial.latency_s,
        "started_at": trial.started_at.isoformat(),
        "agent_exit_code": trial.agent_exit_code,
        "sandboxed": int(trial.sandboxed),
        "image_digest": trial.image_digest,
        "run_tag": trial.trace_id,
        "usage_parsed": int(known),
        "input_tokens": usage.input_tokens if known else None,
        "output_tokens": usage.output_tokens if known else None,
        "reasoning_tokens": usage.reasoning_tokens if known else None,
        "cache_read_tokens": usage.cache_read_tokens if known else None,
        "cache_write_tokens": usage.cache_write_tokens if known else None,
        "turns": usage.turns if known else None,
        "tool_calls": usage.tool_calls,
        "cost_usd": usage.cost_usd,
        "cost_source": usage.cost_source.value,
        "cost_gateway": costs.gateway,
        "cost_price_table": costs.price_table,
        "cost_harness": costs.harness,
    }


def grader_rows(trial: TrialResult) -> list[dict[str, Any]]:
    return [
        {
            "trial_id": trial.trial_id,
            "position": position,
            "grader": g.grader,
            "grader_version": g.grader_version,
            "passed": int(g.passed),
            "score": g.score,
            "timed_out": int(g.timed_out),
            "judge": g.judge,
        }
        for position, g in enumerate(trial.graders)
    ]


class SqliteSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: sqlite3.Connection | None = None

    @classmethod
    def from_section(cls, section: Mapping[str, Any], base: Path) -> Self:
        path = section.get("path")
        if not isinstance(path, str) or not path:
            raise SinkError("a sqlite sink needs a path: a non-empty string, relative to the config")
        return cls(base / path)

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            raise SinkError("the sqlite sink is not open")
        return self._db

    def open(self, info: RunInfo) -> None:
        try:
            schema = SCHEMA_PATH.read_text()
        except OSError as e:
            raise SinkError(f"cannot read the schema {SCHEMA_PATH}: {e}") from e
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        self._db = db
        db.executescript(schema)
        with db:
            db.execute(_UPSERT_RUN, run_row(info))

    def write(self, trial: TrialResult) -> None:
        db = self._connection()
        with db:  # one transaction: a trial is never half there
            db.execute(_UPSERT_TRIAL, trial_row(trial))
            db.execute("DELETE FROM grader_results WHERE trial_id = ?", (trial.trial_id,))
            db.executemany(_INSERT_GRADER, grader_rows(trial))

    def close(self, info: RunInfo) -> None:
        db = self._connection()
        try:
            with db:
                db.execute(_UPSERT_RUN, run_row(info))
        finally:
            db.close()
            self._db = None

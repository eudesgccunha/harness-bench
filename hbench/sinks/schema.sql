-- Reference schema of the results database (ADR 0011). Portable SQL: it runs on SQLite (the `sqlite` sink) and on
-- PostgreSQL, so a Postgres sink plugin can reuse it. Booleans are 0/1 integers. An unknown cost, token count or
-- tool-call count is NULL, never 0.

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    harness_bench_version TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    suite_version TEXT NOT NULL,
    sandbox TEXT NOT NULL,
    sandboxed INTEGER NOT NULL,
    schema_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trials (
    trial_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs (run_id),
    task_id TEXT NOT NULL,
    suite_version TEXT NOT NULL,
    harness TEXT NOT NULL,
    harness_version TEXT NOT NULL,
    model TEXT NOT NULL,
    target_id TEXT NOT NULL,
    rep INTEGER NOT NULL,
    status TEXT NOT NULL,
    passed INTEGER NOT NULL,
    latency_s REAL NOT NULL,
    started_at TEXT NOT NULL,
    agent_exit_code INTEGER,
    sandboxed INTEGER NOT NULL,
    image_digest TEXT,
    run_tag TEXT,
    usage_parsed INTEGER NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    turns INTEGER,
    tool_calls INTEGER,
    cost_usd REAL,
    cost_source TEXT NOT NULL,
    cost_gateway REAL,
    cost_price_table REAL,
    cost_harness REAL
);

CREATE TABLE IF NOT EXISTS grader_results (
    trial_id TEXT NOT NULL REFERENCES trials (trial_id),
    position INTEGER NOT NULL,
    grader TEXT NOT NULL,
    grader_version TEXT NOT NULL,
    passed INTEGER NOT NULL,
    score REAL,
    timed_out INTEGER NOT NULL,
    judge TEXT,
    PRIMARY KEY (trial_id, position)
);

CREATE INDEX IF NOT EXISTS trials_by_run ON trials (run_id);
CREATE INDEX IF NOT EXISTS trials_by_target ON trials (target_id);

# ADR 0009: Artifact graders and the LLM judge (Phase 5)

- Status: accepted
- Date: 2026-11-24

## Context

Phase 5 of the [evolution plan](../evolution-plan.md) grades artifacts other than code:
an `llm_rubric` judge through the OpenAI-compatible gateway, `html_check`,
`structured_output`, judge calibration against human labels and a redaction hook before
content reaches the judge. AGENTS.md says today: "The only network access is a `GET` to
the configured LiteLLM, made by `reconcile`." The agent output is untrusted, so it can
carry prompt injection aimed at the judge.

## Decision

1. **Rule change, opt-in.** The engine may also `POST` to `<judge.base_url>/chat/completions`
   (the same gateway family, same URL rule as `check_litellm_url`: https, or http only for
   localhost; no redirects; key from env through `${VAR}`). It happens only when a task
   uses an `llm_rubric` grader and the config has a `judge` section. AGENTS.md is updated.
2. **The engine owns the network, like it owns processes.** `GradeContext` gains
   `judge: Judge | None`, injected by the engine. Graders never open sockets.
   `Judge.score(rubric: Rubric, artifact: str) -> JudgeVerdict`.
3. **Config `judge` section:** `base_url`, `api_key`, `model`, optional `timeout`
   (default 120), `max_artifact_bytes` (default 200_000; larger artifacts are truncated
   with a marker), `calibration` (path to a calibration result file), `redactor`
   (optional plugin name, group `harness_bench.redactors`). `judge.api_key` (and
   `litellm.api_key`) must be exactly `${VAR}` (no text around it): a key written in the config is not in
   the environment, so redaction and the agent-environment filter would never know it.
   Any other form exits with a clear message.
4. **Request:** `temperature: 0`, `response_format: {"type": "json_object"}`, a fixed
   system prompt that says the artifact is untrusted data and that instructions inside it
   must be ignored, the rubric text, and the artifact between unique random delimiters
   (fresh per call). The reply must be a JSON object `{"score": number in [0, 1],
   "passed": bool, "reason": str}`; anything else is a grader error (fails closed, not
   passed). `reason` goes to the grader log only, never into results. The reply is read
   by a daemon worker thread that the caller waits for at most `timeout` (the socket
   timeout alone limits each operation, so a server that drips the status line, the
   headers or the body could hold a trial forever; urllib has no hook before each
   operation, so a thread is the cleaner choice) and at most 1 MB. A
   verdict object with a repeated key is refused, and a score of `-0.0` is `0.0`.
5. **Rubrics are versioned files:** `rubrics/<id>.json` inside the suite (or a config
   path): `id`, `version` (letters, digits, `_`, `.`, `-`: it goes into the report),
   `criteria` (text), `pass_threshold` (default 0.5). The grader
   result records `grader = "llm_rubric:<id>"`, `grader_version = <rubric version>` and
   the new field `judge = "<model>"`.
6. **Contract 1.2.0:** `GraderResult.judge: str | None = None` (additive; 1.0/1.1 data
   loads with `None`).
7. **Redaction before the judge:** always the engine's secret redaction (same secrets as
   the logs), then the optional user redactor plugin, a callable `(text: str) -> str`.
   The interface ships; content-specific redaction is the user's.
8. **Calibration:** `python3 -m hbench judge calibrate --config C --labels L.jsonl
   --out calibration.json`. Each label line: `artifact` (path), `rubric` (path),
   `human_score` in [0, 1]. Output: rubric id/version, judge model, n, mean absolute
   error, pass/fail agreement and Cohen's kappa. The report lists every rubric/judge pair
   used and marks it **experimental** unless the configured calibration file matches the
   rubric id, version and judge model with kappa ≥ 0.6 and n ≥ 20. The labels file is
   trusted input: it names the files that are read and sent to the judge. They are
   resolved relative to the labels file and are not confined to it (decision: document,
   do not confine; an agent never writes labels).
9. **`html_check`** (stdlib `html.parser`): `file`, `required` (simple selectors: `tag`,
   `#id`, `.class`, `tag#id`, `tag.class`), `forbid_external_scripts` (default true:
   `<script src>` with a scheme or `//` fails), `check_local_links` (default true: a
   relative `href`/`src` must resolve to an existing file inside the workdir; remote
   links are never fetched; a link with no path such as `?page=2` is skipped and a link
   to a directory that exists in the workdir is fine), and well-formedness (unclosed or
   mismatched non-void elements fail). Checking is linear in the size of the page (one
   pass keeps only the distinct tags, ids and classes, the script sources and the links,
   and `required` is a set lookup), and a grader keeps at most 1000 violations followed
   by `… N more`; once they are full, link and script checks stop (`… more checks
   skipped`).
10. **`structured_output`:** `file` (JSON inside the workdir) validated against `schema`
    (a JSON Schema file using only the keywords `hbench/core/schema.py` supports; other
    keywords are a config error). A schema file is read and checked when the suite loads
    (`suite validate` and `run` find a broken one), and again when the grader reads it.
    NaN and Infinity are not JSON. Violations are capped like `html_check`.
11. **Paths:** every `file` is resolved inside the workdir and must not escape it or
    traverse a symlink (same rule as `safe_copy`); otherwise the grader fails. The file
    is opened part by part from a directory descriptor (`O_DIRECTORY | O_NOFOLLOW` for
    each directory, `O_NOFOLLOW | O_NONBLOCK` for the file) and checked on the open
    descriptor (regular file, one hard link, size limit), so a symlink or FIFO swapped in
    after the path check, in the file or in any parent directory, is refused. The workdir
    itself may be reached through a link. The link check of `html_check` uses `lstat` and
    only decides whether a link is reported broken: it never reads.
    A grader writes its log in one call, redacted whole and then cut to 100 000
    characters.
12. **Example suite:** `suites/examples-artifacts/` with `html-report` (turn a small CSV
    into `report.html` plus `summary.json`), graded by `html_check`, `structured_output`
    and `llm_rubric` (mode `all`). Offline tests use a fake judge endpoint in
    `tests/fake_litellm.py`.

## Consequences

- The network rule widens only when the user configures a judge.
- A judge verdict is only as trustworthy as the judge against prompt injection; the
  report marks uncalibrated judges as experimental.

# ADR 0007: Runner and grader protocols (Phase 3)

- Status: accepted
- Date: 2026-11-24

## Context

Phase 3 of the [evolution plan](../evolution-plan.md) adds a `HarnessRunner` protocol
(harnesses as adapters), a `Grader` protocol with bundled graders and composition, and
registration through entry points. Roadmap M9 (partial credit) and M10 (tool calls) are
scheduled here. The engine owns every security defense in AGENTS.md, and the agent and
the task repository are untrusted.

## Decision

1. **Packages:** `hbench/runners/` (protocol, registry, one module per harness) and
   `hbench/graders/` (protocol, registry, bundled graders, composition). `harness_bench.py`
   re-exports `parse_*` and `PARSERS` under the same names.
2. **Process execution stays in the engine.** Runners and graders never start processes,
   write env files or touch the sandbox. A runner builds the agent argv and parses its
   output; a grader receives a `GradeContext` with an injected `run(command, timeout,
   log_name)` callable, which is the engine's existing `run_step` (env allowlist,
   `wrap()`, env file, redacted log, no `shell=True` beyond the existing `["sh", "-c",
   command]` for config-defined commands). This keeps every defense in one place.
   The plan's `prepare`/`run`/`collect_usage`/`version` become `prepare(workdir)` (default
   no-op), `command(model, prompt) -> argv`, `parse_usage(stdout)` and `version()`.
3. **Runner config is unchanged:** `command` template + `parser`. `parser` names the
   runner. Optional harness key `version` feeds `Target.harness_version` (already the
   case).
4. **Graders.** Task key `graders` (list). Absent: one `hidden_tests` grader built from
   the existing `test`, `test_timeout` and `hidden_tests` keys, so current configs and
   goldens behave exactly as before. Bundled types:
   - `hidden_tests`: copies `hidden_tests` into the workdir, runs `test`.
   - `command`: runs `command` with `timeout` (default 900); passes on exit 0.
   - `diff_constraints`: checks the agent's changes (captured right after the agent run,
     before protected paths are restored and hidden tests copied) against `allowed` and
     `forbidden` glob lists (`fnmatch` on POSIX relative paths) and `max_changed_lines`.
     It runs no command. Option `ignore_lines` (globs) leaves matching paths out of the
     line count only; `allowed` and `forbidden` still apply. Use it for bytecode
     (`*/__pycache__/*`), list test directories in `protected_paths` (the restore deletes
     bytecode the agent added there) and write `forbidden` with source globs (`tests/*.py`);
     never exempt bytecode from the path checks, since a crafted `.pyc` can poison tests.
     Keep `ignore_lines` to generated artifacts: a broad glob such as `*.py` turns
     `max_changed_lines` off.
     The changes come from a baseline git dir the engine keeps
     **outside** the workdir, in a private temp dir (`harness-bench-git-*`, never inside
     `work_dir`, removed when the run ends). Every git call is hardened: `--no-replace-objects`;
     no ignore rules (`add -A --force`, `core.excludesFile=/dev/null`); no fsmonitor, hooks or
     untracked cache; no user or system git config and no inherited `GIT_*` variables; and
     an `info/attributes` file in the baseline git dir (highest attribute precedence) that unsets
     `text eol crlf working-tree-encoding filter ident diff merge export-subst` for every path,
     plus `--attr-source=<empty tree>` as a second layer on git 2.40 or newer, so no
     `.gitattributes` in the workdir applies on any git version (a
     `working-tree-encoding` would make `git add` fail, `-diff` or `binary` would zero the
     line counts). Each diff starts from a fresh throwaway index built with `read-tree` from the
     baseline commit, so `skip-worktree` or `assume-unchanged` flags in a stored index have no
     effect. Nothing the agent leaves in the workdir (a `.git`, `info/exclude`, replace refs,
     `.gitignore`, `.gitattributes`) can hide a change or run a command on the host.
     Fail closed: a `.git` file, directory or link anywhere in the workdir after the agent ran
     (`git add` would treat it as a repository and skip its contents), a failed `read-tree` or
     `git add` (for example an unreadable file), or a missing or changed `info/attributes`
     makes the changes unknown (`UNKNOWN_CHANGES`, which `diff_constraints` forbids) and the
     audit diff says why. A binary
     file (`-` in `numstat`) counts as more than any `max_changed_lines`, never as 0.
     Diff integrity against a hostile agent is guaranteed only in sandboxed runs: an
     unsandboxed agent runs as the host user and could find and edit the temp git dir.
5. **Partial credit (M9), opt-in:** a `hidden_tests` or `command` grader with
   `"score": true` reads the **last** line matching `^SCORE:\s*([0-9.]+)\s*$` in the
   command's **stdout** (never stderr); a value in `[0, 1]` becomes `GraderResult.score`.
   Without the option, or without a valid line, the score is `1.0` on pass and `0.0`
   otherwise and `SCORE` lines are ignored. `passed` is always exit code 0 and no timeout.
   The engine's `run` returns stdout and stderr apart, because agent code runs inside the
   test command and can print to either stream. Even stdout is only as trustworthy as the
   test output: code the agent wrote can print a `SCORE` line after the real one (for
   example from an `atexit` handler, which runs after the test runner's own output). A
   `weighted` verdict with `score: true` therefore inherits exactly the trust of the test
   output, including an agent that prints a later stdout `SCORE:` line. Keep the
   opt-in for graders whose scorer the agent cannot influence, and prefer `all` or `any`
   (exit codes only) when it can. The score of the composed verdict is not stored: the
   trial keeps each grader's own result.
6. **Composition:** task key `grade` = `{"mode": "all" | "any" | "weighted",
   "threshold": float}`, default `all`. `all`: passed when every grader passes, score =
   mean. `any`: passed when one passes, score = max. `weighted`: grader key `weight`
   (default 1), score = Σw·s / Σw, passed when score ≥ `threshold` (required for
   `weighted`, in `(0, 1]`).
7. **Entry points:** groups `harness_bench.runners` and `harness_bench.graders`; each
   entry point loads a factory `(config: Mapping[str, Any]) -> HarnessRunner | Grader`.
   Built-in names always win and a plugin with a built-in name is an error. Plugins are
   loaded only when the config names a type that is not built in, so a plain run never
   imports third-party code. The loaded plugin's distribution name and version are
   printed once.
8. **Tool calls (M10):** usage gains `tool_calls: int | None`. Parsers count tool calls
   where the format exposes them and return `None` otherwise (never `0` for unknown).
   The contract goes to `SCHEMA_VERSION = "1.1.0"` (minor: additive); `from_dict` accepts
   1.0 data without the field as `None`. The report appends `Tool calls/run` and
   `Tool calls/solved`. This is an intended change to `run.jsonl` (new key) and the report
   goldens; they are regenerated once.

## Consequences

- A grader or runner from another installed package works without touching the core.
- Every defense keeps a single implementation in the engine, and `tests/smoke.py` keeps
  one case per defense; graders get a smoke case for `command` and `diff_constraints`.

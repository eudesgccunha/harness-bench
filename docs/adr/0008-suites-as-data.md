# ADR 0008: Suites as data (Phase 4)

- Status: accepted
- Date: 2026-11-24

## Context

Phase 4 of the [evolution plan](../evolution-plan.md) turns tasks into versioned suites,
adds `suite validate` / `suite list`, ships example suites built only from public or
synthetic code, separates capability from regression suites and adds an automated
contamination test. Roadmap M2 (validate against a reference solution) and M12 (task
packs) are scheduled here.

## Decision

1. **Format:** one directory per suite, `suites/<name>/suite.json`:
   `schema_version` (`"1.0.0"`), `name` (`[a-z0-9-]+`), `version` (semver string),
   `kind` (`"capability"` | `"regression"`), `description`, `tasks` (list). A task has the
   same keys as a config task today (`id`, `repo`, `ref`, `prompt`, `test`,
   `hidden_tests`, `setup`, `timeout`, `setup_timeout`, `test_timeout`,
   `protected_paths`, `graders`, `grade`) plus `tags` (list of strings), `solution`
   (directory with the reference files, copied over the repo) and `provenance`
   (`{"source": str, "license": str, "url": str | null}`). Paths are relative to the
   suite directory. Unknown keys are rejected.
2. **Model:** `hbench/suites.py` with frozen dataclasses `Suite` and `SuiteTask`,
   `load_suite(path) -> Suite` (validates, resolves paths to absolute, checks they stay
   inside the suite directory unless the repo is an `https://` URL, and rejects every
   symlink, see 9), and
   `suite_tasks(suite) -> list[dict]` producing engine task dicts with ids
   `<suite>/<task>`.
3. **Config:** new key `suites` (list of suite directories, relative to the config).
   Tasks from suites are appended to `tasks`; duplicate ids are an error.
   `RunInfo.suite_version` becomes `"<name>@<version>,..."` plus the existing hash of the
   expanded tasks, so a run records exactly which suites it used.
4. **CLI:** `python3 -m hbench suite list [--config C | PATH...]` prints name, version,
   kind, task count and tags. `python3 -m hbench suite validate PATH...` does the static
   checks. `suite validate --run` also does the M2 behavioral check per task, through
   the engine (same workdir preparation, `wrap()`, env allowlist, redaction): the graders
   must **fail** on the untouched repo and **pass** after `solution` is copied in. It
   follows the same sandbox rule as `run` (needs `sandbox.prefix` or
   `--allow-unsandboxed`, with a `--config` for the sandbox and env settings).
5. **Example suites** (synthetic code written for this repo, MIT, stdlib only, offline):
   `suites/examples-capability/` with `dependency-upgrade` (a vendored local library
   changes its API from v1 to v2; the project must migrate), `path-traversal-fix`
   (a synthetic archive extractor with the public CWE-22 "zip slip" flaw) and
   `extract-function` (a refactor guarded by existing tests plus a hidden test on the new
   function); `suites/regression/` with `fizzbuzz`, which moved from `tasks/fizzbuzz` to
   `suites/regression/fizzbuzz`. The old `tasks/fizzbuzz` path is gone: the example
   configs list the suite instead of a `fizzbuzz` task, and `tasks/` is only for tasks of
   your own (it stays out of git).
6. **Report:** when trials come from more than one suite kind, the report appends
   `## By suite kind` with solved/runs per pair for `capability` and `regression`, and
   flags any regression task a non-null pair failed. Without suites the report is
   unchanged, so the goldens stay as they are.
7. **Contamination guard:** a fake agent mode (`probe-hidden`) searches for the hidden
   test file names and their marker string from where an unsandboxed agent runs: its
   whole workdir tree (recursive, with an entry cap), every parent directory up to `/`
   (their own files) and, on request (`FAKE_SCAN_ROOTS`), other roots eight levels down.
   It exits 7 when it finds one. The smoke test proves exactly this: **hidden tests are
   never copied into the workdir before grading, and an unsandboxed fake agent cannot
   find them in its workdir or its parents.** Positive controls plant a leak at the real
   depth of a hidden test (a file name, a marker, six levels down, in a parent) and the
   probe must find each. It does not claim more. An unsandboxed agent runs as the user and
   can read anything the user can, including the suite sources: a control that points the
   probe at the benchmark directory finds them. Real isolation of the hidden tests needs
   the sandbox, which mounts only the workdir.
8. **Deferred:** the M12 importers (Exercism, Terminal-Bench) need network access and
   license review per pack; the format's `provenance` field is the hook they will use.
   They move to Phase 9 (community).

9. **Suites are third-party data** (a review hardened this):
   - A suite holds no symlinks in `repo`, `hidden_tests` or `solution` (any depth, files
     and directories, absolute or relative, inside or outside). The example suites have
     none, so allowing them buys nothing. This closes the copy of a `solution` through a
     link in the workdir, and a link from the repo to the hidden tests.
   - A suite `repo` is a directory of the suite or an `https://` URL. `file://`, `ssh://`,
     `git@host:path`, `git://`, `http://` and `ext::` are rejected, so a suite cannot read
     a private local repo or use the user's SSH keys. Config tasks are the user's own and
     keep every form.
   - As defense in depth the engine's `git clone` of a task repo sets `GIT_ALLOW_PROTOCOL`,
     passed in from the task source: `https:ssh:file` for config tasks, `https` for suite
     tasks (the clone from the local cache into the workdir allows `file` alone).
   - Copies into the workdir (hidden tests at grading, `solution` in `validate --run`) use
     `hbench/safe_copy.py`: any symlink on the destination path, or a hard-linked file
     there, is replaced by a regular file instead of being written through. An agent can
     plant `test_hidden.py -> ~/.bashrc`; the grader no longer overwrites the host file.
   - A config task must not carry the keys the engine adds to suite tasks (`suite`,
     `suite_kind`, `suite_label`, `suite_dir`, `solution`). The hash in
     `RunInfo.suite_version` uses the task directories relative to the suite directory (a
     git URL stays in it), so it does not depend on the checkout path.
   - A suite `repo` directory must not be a git repository (a `.git` entry, or a name
     ending in `.git`): the engine would clone it instead of copying it, which a suite
     may not do. Use an `https://` URL with `ref`, or a plain directory.
   - Copies of `repo` and `solution` skip `__pycache__`. `safe_copy_into` refuses a
     destination root that is a symlink.

## Consequences

- A clone plus `python3 -m hbench suite validate --run --allow-unsandboxed suites/...`
  proves every example task is solvable and not trivially passing, offline.
- Task ids from suites contain `/`; file names use `safe()` as before.

# ADR 0013: `compare`, `recommend`, CI gate, target file and task importers (Phase 9)

- Status: accepted
- Date: 2026-11-25

## Context

Phase 9 of the [evolution plan](../evolution-plan.md) turns a benchmark into a decision:
compare two runs, recommend a target under constraints, gate CI on the regression suite,
export the winning target as a versionable file, and import task packs (roadmap M12:
Exercism, Terminal-Bench) with license and provenance per task. The runtime is the
standard library only ([ADR 0001](0001-stdlib-runtime.md)), the only network access is the
one AGENTS.md lists, and `hbench/report.py` already computes every per-target figure
(`aggregate`: pass rate and Wilson interval, cost per solved, ties, Pareto, null control).

## Decision

1. **Module `hbench/decide.py`**, pure functions over `RunInfo` and `TrialResult` (no file
   or network access; the CLI reads the runs with `results.read_run`). It reuses
   `report.aggregate` for every figure, so `compare`, `recommend` and the report never
   disagree. Null-control pairs are never recommended and are listed apart from the ranking in
   `compare` (see the amendment below).
2. **`python3 -m hbench compare RUN_A RUN_B [--cost-source S] [--alpha 0.05] [--json]
   [--fail-on-regression]`** (run directories). Output, Markdown by default, JSON with
   `--json`, deterministic (same input, same bytes):
   - comparability warnings: different `config_sha256`, `suite_version`, task set or reps;
   - ranking diff: rank, pass rate, cost per solved in A and in B and the delta per
     target; targets only in A or only in B;
   - **regression:** a target in both runs whose outcomes, paired by `(task_id, rep)`
     present in both, have `b > c` (passed in A, failed in B) and McNemar exact
     `p < alpha` (`stats.mcnemar_exact`); per-task regressions (solved at least once in A,
     never in B) are listed as information. An `error` trial counts as failed.
   - Exit 0; with `--fail-on-regression`, exit 1 when there is a regression.
3. **`python3 -m hbench recommend RUN_DIR [--min-pass-rate P] [--max-cost-per-solved USD]
   [--max-cost-per-run USD] [--max-latency-s S] [--cost-source S] [--json]
   [--write-target FILE]`.** Every non-null target is checked against every given
   constraint, on the point estimates of the report (`pass_rate`, `cost_per_solved`,
   `cost_per_run`, `avg_wall_s`). **An unknown cost never meets a cost constraint** (it
   is excluded with the reason `unknown cost`, never read as 0). The pick is the first
   eligible target in the report order (cheapest per solved task, unknown cost after
   known, higher pass rate first on equal cost). The output lists every target with the
   constraints it failed, the pick with its 95% intervals, and a warning when the pick is
   statistically tied (`tie_with`) with another eligible target. No eligible target:
   message and exit 1.
4. **Target file (the "winning target").** `--write-target FILE` writes a
   `TargetDecision` (contract object in `hbench/core/models.py`, frozen, JSON round-trip,
   in the JSON Schema; contract `1.5.0`, additive): `schema_version`, `harness`, `model`,
   `run_id`, `config_sha256`, `suite_version`, `cost_source`, `constraints` (the given
   ones only), and `evidence` (`runs`, `solved`, `pass_rate`, `pass_ci_low`,
   `pass_ci_high`, `cost_per_solved`, `cost_per_run`, `avg_wall_s`; unknown is `null`).
   JSON with sorted keys and a final newline, no timestamp of its own, so the same run
   gives the same bytes and the file diffs cleanly in git. It holds no command, env,
   secret or path. Writing refuses to follow a symlink and replaces the file atomically.
5. **`python3 -m hbench gate RUN_DIR --min-pass-rate P (--target FILE | --harness H
   --model M) [--suite regression]`**: the CI gate. It takes the trials of that target
   whose `task_id` is in the suite (`<suite>/` prefix) and exits 1 with a message when
   the pass rate is below `P`. It **fails closed**: run not finished (`finished_at` is
   null), target not in the run, no trial of the suite for it, a target file that does
   not load as a `TargetDecision`, all exit 1. `error` trials count as failed. Exit 0
   prints the pass rate and its interval. Example: `adapters/github-actions/
   regression-gate.yml` (not under `.github/workflows`, pinned action SHAs).
6. **Task importers** (`hbench/importers/`, CLI `python3 -m hbench suite import
   exercism --source DIR --out SUITE_DIR --name N --version V --license SPDX
   --source-url https://... [--exercise SLUG ...]`). **No network:** the user clones the
   pack and reviews its license; the importer reads only the local checkout and writes a
   new suite directory (refused if `--out` exists; removed again if the import fails).
   - Exercism (Python track layout: `exercises/practice/<slug>/` with
     `.meta/config.json` `files.solution|test|example`, `.docs/instructions.md` and the
     optional `.docs/instructions.append.md`): `repo/` = the solution stubs, `hidden_tests/`
     = the test files with a `# HIDDEN-TEST-MARKER` line appended, `solution/` = the
     example files under the solution file names, `test` = `python3 -m unittest -q
     <test file>`, prompt = the instructions plus which files to edit; `provenance` =
     `{"source": "exercism/python", "license": <--license>, "url": <--source-url>/
     exercises/practice/<slug>}` per task; suite `kind` `capability`, so imported packs
     stay a tier separate from your own suites (one suite per pack).
   - The checkout is untrusted input: a source without a `LICENSE` file at its root is
     refused; the license text is copied to `<out>/LICENSE.<name>`; the slug matches
     `[a-z0-9-]+`; file names from `config.json` are relative, without `..`, and resolve
     inside the exercise; no symlink anywhere, regular files only, a size limit per file
     (1 MB); `--source-url` must be `https://`. The result must pass `suite validate`
     (the importer runs the static validation before it returns; `--run` is up to the user).
   - **Terminal-Bench is dropped** (decided 2026-11-25, after a review of the 2.0 pack):
     all 89 Terminal-Bench 2.0 tasks need their own image (published by date tag, not digest), every test script downloads uv, Python and pytest at verify time, the reference solutions are scripts that often use the network, and tasks carry no license or source of their own. Importing it would need a per-task image, network at verify time
     (or 89 rebuilt images), script solutions and a license review per task: a phase of
     its own for tasks (terminal and ops work) outside the coding-in-a-repo use this
     benchmark measures. Nobody should start it again without a new decision. The importer
     registry is a dict plus the entry-point group `harness_bench.importers`
     (`hbench/plugins.py`), like sandboxes and sinks.
7. **Docs:** `docs/from-benchmark-to-decision.md` (compare, recommend, target file, gate,
   import, and how a switch is justified: two runs, the comparison, the recommendation
   and the target file under version control).

## Consequences

- A model or harness switch is a reviewed change to the target file, backed by the run,
  the comparison and the recommendation, all reproducible from `trials.jsonl`.
- No new network access and no new runtime dependency.
- Terminal-Bench is not imported (dropped, §6): its pack does not fit the suite format or the network rule.

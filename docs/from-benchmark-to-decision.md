# From benchmark to decision

A switch of model or harness is a reviewed change, backed by two runs, a comparison, a recommendation and a target file under version control. Everything here reads the run directories (`run.json`, `trials.jsonl`) and uses the same figures as the report, so the commands and `summary.md` never disagree. Decisions: [ADR 0013](adr/0013-compare-recommend-gate-import.md). None of these commands needs a config or the network.

## 1. Compare two runs

```bash
python3 -m hbench compare results/<run_a> results/<run_b> [--cost-source auto|litellm|computed|reported] [--alpha 0.05] [--json] [--fail-on-regression]
```

A is the baseline. The output has:

- **Comparability warnings:** a different `config_sha256`, `suite_version`, task set or number of reps, or a run that is not finished. A comparison across different setups is still printed, but read it with the warnings.
- **Ranking diff:** rank, pass rate and cost per solved task in A and in B, and the delta, for every target in both runs. Targets only in one run are listed apart. The negative control (`null`) is listed apart and not ranked. The commands find it from the trials (zero tokens, a real 0.0 cost and 0 tool calls: a heuristic, since a run does not record it; the report uses the config), so regressions are checked for every target in both runs: a real target that fails every trial of B, for example on an auth failure, is reported as a regression.
- **Regressions:** a target regressed when, over the `(task, rep)` pairs present in both runs, it passed in A and failed in B more often than the reverse (`b > c`) and the exact McNemar test gives `p < alpha`. An `error` or `timeout` trial counts as failed. Tasks solved at least once in A and never in B are listed as information.

The output is deterministic: the same two runs give the same bytes. The exit code is 0, or 1 on a regression with `--fail-on-regression`. `--litellm` uses the gateway cost, as the LiteLLM report does.

## 2. Recommend a target

```bash
python3 -m hbench recommend results/<run> [--min-pass-rate 0.8] [--max-cost-per-solved 0.50] [--max-cost-per-run 2] [--max-latency-s 300] [--json] [--write-target target.json]
```

Every non-null target is checked against every constraint you give, on the point estimates of the report. The pick is the first eligible target in report order: cheapest per solved task, unknown cost after known cost, higher pass rate first on equal cost. Rules worth knowing:

- **An unknown cost never meets a cost constraint.** It is excluded with the reason `unknown cost`, never read as 0.
- A target that solved nothing is never picked, and the null control (found from the trials, see above) is never recommended.
- The output lists every target with the constraints it failed, the pick with its 95% intervals, and a warning when the pick is statistically tied with another eligible target (run more repetitions before a switch).
- No eligible target: a message and exit 1 (nothing is written).

## 3. The target file

`--write-target FILE` writes the pick as a `TargetDecision` (contract 1.5.0, JSON Schema in `python3 -m hbench schema export` under `target_decision`): harness, model, the run it was measured in (`run_id`, `config_sha256`, `suite_version`), the cost source, the constraints you gave, and the evidence (`runs`, `solved`, `pass_rate`, its interval, costs and average time; unknown is `null`). `cost_source` is the source you asked for (`auto` records `auto`, not the per-trial mix of sources). It has sorted keys, a final newline and no timestamp, so the same run gives the same bytes and the file diffs cleanly in git. It holds no command, env, secret or path. Writing refuses to follow a symlink and replaces the file atomically.

Commit the file. A model or harness switch is then a pull request that changes it, with the two runs, the `compare` output and the `recommend` output as the evidence.

## 4. Gate CI on the regression suite

```bash
python3 -m hbench gate results/<run> --min-pass-rate 0.9 --target target.json [--suite regression]
python3 -m hbench gate results/<run> --min-pass-rate 0.9 --harness claude --model sonnet
```

The gate checks the target you name only (a null target fails because it solves nothing). It takes the trials of the target whose `task_id` starts with `<suite>/` and exits 1 when the pass rate is below the minimum. It **fails closed**: a run that is not finished, a target that is not in the run, no trial of the suite for it, and a target file that does not load as a `TargetDecision` (extra keys, another major version, invalid values) are all failures. `error` trials count as failed. When it passes it prints the pass rate and its interval. An example job is [`adapters/github-actions/regression-gate.yml`](../adapters/github-actions/regression-gate.yml) (copy it to `.github/workflows/`; it sits outside so it does not run here). It downloads the `report` artifact from the GitHub Actions run named by `source_workflow_run` (needs `actions: read`), which is not the hbench run id.

## 5. Import task packs

```bash
git clone https://github.com/exercism/python /path/to/exercism-python   # you review its license
python3 -m hbench suite import exercism --source /path/to/exercism-python --out suites/exercism-python \
  --name exercism-python --version 1.0.0 --license MIT --source-url https://github.com/exercism/python [--exercise two-fer leap]
python3 -m hbench suite validate --run --allow-unsandboxed suites/exercism-python
```

The importer uses **no network**: it reads your local clone and writes a new suite directory (refused if `--out` exists; removed again if the import fails). For each exercise of the Python track (`exercises/practice/<slug>/`) it writes `repo/` (the solution stubs), `hidden_tests/` (the test files with a `# HIDDEN-TEST-MARKER` line appended), `solution/` (the example files under the stub names), a test command `python3 -m unittest -q <test file>`, a prompt built from the instructions, and `provenance` (`exercism/python`, your `--license`, the exercise URL). The suite kind is `capability`, so imported packs stay a tier separate from your own suites (one suite per pack), and the license text is copied to `LICENSE.<name>`.

The checkout is untrusted input: a source without a `LICENSE` file is refused; slugs match `[a-z0-9-]+`; file names from `config.json` must be relative, without `..`, and stay inside the exercise; a symlink anywhere in an exercise, a file over 1 MB and a `--source-url` that is not `https://` are refused. The result is loaded as a suite before the command returns; `--run` (the behavioral check) is up to you.

Importers are a registry (`hbench/importers/`) plus the entry-point group `harness_bench.importers`, like sandboxes and sinks. **Terminal-Bench is dropped** ([ADR 0013](adr/0013-compare-recommend-gate-import.md) §6): all 89 Terminal-Bench 2.0 tasks need their own image (published by date tag, not digest), every test script downloads uv, Python and pytest at verify time, the reference solutions are scripts that often use the network, and tasks carry no license or source of their own.

## How a switch is justified

1. Run the benchmark twice: the current target and the candidates (same config and suite, or the warnings of `compare` will say so).
2. `compare` the baseline run with the new one: no regression on the targets you keep, and the ranking diff shows what changed.
3. `recommend` with the constraints that matter (pass rate floor, cost, latency) and write the target file.
4. Commit the target file; CI runs `gate` with it on the regression suite of the latest run.

Every step is reproducible from `trials.jsonl`.

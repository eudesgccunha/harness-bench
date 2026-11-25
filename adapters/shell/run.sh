#!/usr/bin/env bash
# Run a benchmark with N parallel workers using only `plan`, `run-trial` and `finish` (ADR 0012).
#
#   adapters/shell/run.sh CONFIG [WORKERS]
#
# Set ALLOW_UNSANDBOXED=1 to pass --allow-unsandboxed (only inside a disposable VM). Workers are processes on this
# machine that share the output directory: appends to the result files take a file lock. Needs GNU xargs (-d).
set -euo pipefail

config="${1:?usage: run.sh CONFIG [WORKERS]}"
workers="${2:-4}"
allow=""
if [ "${ALLOW_UNSANDBOXED:-}" = "1" ]; then
    allow="--allow-unsandboxed"
fi

specs="$(mktemp)"
trap 'rm -f "$specs"' EXIT

python3 -m hbench plan --config "$config" >"$specs"
run_id="$(python3 -c 'import json, sys; print(json.loads(sys.stdin.readline())["run_id"])' <"$specs")"

# One spec per line. xargs hands each line to the script as one argument: it is data, never parsed by a shell.
status=0
xargs -d '\n' -n 1 -P "$workers" bash -c \
    'printf "%s\n" "$3" | python3 -m hbench run-trial --config "$1" ${2:+"$2"} --spec - >/dev/null' \
    _ "$config" "$allow" <"$specs" || status=$?

# Finish even when a trial failed to start: the trials that ran are in the report.
python3 -m hbench finish --config "$config" --run "$run_id"
exit "$status"

#!/usr/bin/env python3
"""End-to-end smoke test with fake agents and a fake LiteLLM. No network, no real CLIs.

Run: python3 tests/smoke.py   (exit 0 = pass)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import fake_litellm  # noqa: E402


def check(cond, msg):
    if not cond:
        sys.exit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-smoke-"))
    try:
        for f in ("harness_bench.py", "harness_bench_litellm.py"):
            shutil.copy(ROOT / f, tmp)
        shutil.copytree(ROOT / "tasks" / "fizzbuzz", tmp / "tasks" / "fizzbuzz")
        tags_file = tmp / "tags.txt"
        os.environ["FAKE_TAGS_FILE"] = str(tags_file)
        os.environ["FAKE_KEY"] = fake_litellm.KEY

        srv, port = fake_litellm.serve()
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        agent = str(ROOT / "tests" / "fake_agent.py")
        harness = lambda kind, model_field: {  # noqa: E731
            "parser": kind, "command": [sys.executable, agent, kind, model_field],
            "env": {"ANTHROPIC_CUSTOM_HEADERS": "x-litellm-tags: {run_tag}"}}
        cfg = {
            "reps": 1,
            "litellm": {"base_url": f"http://127.0.0.1:{port}", "api_key": "${FAKE_KEY}"},
            "harnesses": {k: harness(k, "{model_name}") for k in
                          ("claude", "opencode", "pi", "codex", "cursor")},
            "matrix": [
                {"harness": "claude", "model": "fix"},
                {"harness": "opencode", "model": "p/nofix"},
                {"harness": "pi", "model": "p/fix"},
                {"harness": "codex", "model": "fix", "price": "x"},
                {"harness": "cursor", "model": "fix", "price": "x"},
            ],
            "prices": {"x": {"input": 3, "output": 15}},
            "tasks": [{"id": "fizzbuzz", "repo": "tasks/fizzbuzz/repo", "prompt": "fix it",
                       "hidden_tests": "tasks/fizzbuzz/hidden_tests",
                       "test": f"{sys.executable} -m unittest -q test_hidden.py"}],
        }
        (tmp / "config.json").write_text(json.dumps(cfg))

        def run(*args):
            p = subprocess.run([sys.executable, *args, "--config", "config.json"], cwd=tmp,
                               capture_output=True, text=True)
            if p.returncode != 0:
                sys.exit(f"FAIL: {' '.join(args)}\n{p.stdout}\n{p.stderr}")
            return p.stdout

        # --- full script
        run("harness_bench.py", "run")
        rows = {r["harness"]: r for r in map(json.loads, (tmp / "results/results.jsonl").read_text().split("\n")[:-1])}
        check(len(rows) == 5, "full script: 5 runs recorded")
        check(rows["claude"]["passed"] and not rows["opencode"]["passed"], "hidden tests decide pass/fail")
        check(rows["claude"]["input"] == 310 and rows["claude"]["cache_read"] == 40000, "claude: modelUsage summed")
        check(rows["opencode"]["turns"] == 3 and rows["opencode"]["reasoning"] == 150, "opencode: step_finish summed")
        check(rows["pi"]["turns"] == 3 and rows["pi"]["input"] == 2400, "pi: only message_end counted")
        check(rows["codex"]["input"] == 5000 and rows["codex"]["cache_read"] == 15000, "codex: cached is subset of input")
        check(abs(rows["codex"]["cost_computed"] - 0.0735) < 1e-9, "codex: computed cost from prices")
        check(not rows["cursor"]["parse_ok"] and rows["cursor"]["cost_computed"] is None, "cursor: no usage, no fake $0")

        out = run("harness_bench.py", "reconcile")
        check("4/5 runs matched" in out, "reconcile: tagged runs matched, cursor unmatched")
        rows = {r["harness"]: r for r in map(json.loads, (tmp / "results/results.jsonl").read_text().split("\n")[:-1])}
        check(abs(rows["pi"]["cost_litellm"] - 0.02) < 1e-9, "reconcile: unrelated rows ignored")
        check((tmp / "results/summary.md").exists() and (tmp / "results/summary.csv").exists(), "report files written")

        # --- LiteLLM-only script
        tags_file.unlink()
        out = run("harness_bench_litellm.py", "run", "--settle", "0")
        check("ignored 1 matrix entries" in out, "litellm script: cursor dropped")
        check("4/4 runs matched" in out, "litellm script: all runs matched")
        summary = (tmp / "results-litellm/summary.md").read_text()
        check("cursor" not in summary and summary.count("| litellm |") == 4, "litellm script: cost source litellm only")
        print("\nPASS")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

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
sys.path.insert(0, str(ROOT))
import fake_litellm  # noqa: E402
import fixtures  # noqa: E402
import harness_bench as hb  # noqa: E402

PY = fixtures.PY
API_KEY = "sk-test-agent-key-0123456789"


def check(cond, msg):
    if not cond:
        sys.exit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def results(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def run(tmp, *args, config="config.json", expect_ok=True):
    p = subprocess.run([PY, *args, "--config", config], cwd=tmp, capture_output=True, text=True)
    if expect_ok and p.returncode != 0:
        sys.exit(f"FAIL: {' '.join(args)}\n{p.stdout}\n{p.stderr}")
    return p


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True
    )


def make_git_task(tmp):
    """Repo with a buggy commit followed by the fix: the agent must only see the first."""
    repo = tmp / "gitrepo"
    shutil.copytree(ROOT / "tasks" / "fizzbuzz" / "repo", repo)
    (repo / "run_tests.py").write_text(
        "import sys, unittest\n"
        "r = unittest.main(module='test_hidden', exit=False).result\n"
        "sys.exit(0 if r.wasSuccessful() else 1)\n"
    )
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "buggy")
    buggy = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "fizzbuzz.py").write_text("def fizzbuzz(n):\n    return 'SOLUTION'\n")
    git(repo, "commit", "-qam", "the fix")
    return str(repo), buggy


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-smoke-"))
    try:
        fixtures.copy_engine(tmp)
        shutil.copytree(ROOT / "tasks" / "fizzbuzz", tmp / "tasks" / "fizzbuzz")
        tags_file, probe_file = tmp / "tags.txt", tmp / "probe.jsonl"
        os.environ.update(
            FAKE_TAGS_FILE=str(tags_file),
            FAKE_PROBE_FILE=str(probe_file),
            FAKE_KEY=fake_litellm.KEY,
            SECRET_CANARY="must-not-leak",
            FAKE_API_KEY=API_KEY,
        )

        srv, port = fake_litellm.serve()
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        harness_env = {
            "FAKE_TAGS_FILE": "${FAKE_TAGS_FILE}",
            "FAKE_PROBE_FILE": "${FAKE_PROBE_FILE}",
            "FAKE_API_KEY": "${FAKE_API_KEY}",
        }
        fizz = fixtures.fizzbuzz_task()
        cfg = dict(
            fixtures.fake_config(harness_env),
            litellm={"base_url": f"http://127.0.0.1:{port}", "api_key": "${FAKE_KEY}"},
        )
        (tmp / "config.json").write_text(json.dumps(cfg))

        # --- parsers, cost, reconcile (full script)
        p = run(tmp, "harness_bench.py", "run", expect_ok=False)
        check(p.returncode != 0 and "No sandbox configured" in p.stderr, "refuses to run without sandbox")
        run(tmp, "harness_bench.py", "run", "--allow-unsandboxed")
        rows = {r["harness"]: r for r in results(tmp / "results/results.jsonl")}
        check(len(rows) == 5, "full script: 5 runs recorded")
        check(all(r["agent_rc"] == 0 for r in rows.values()), "agent env: spend key and other secrets absent")
        check(rows["claude"]["passed"] and not rows["opencode"]["passed"], "hidden tests decide pass/fail")
        check(rows["claude"]["input"] == 310 and rows["claude"]["cache_read"] == 40000, "claude: modelUsage summed")
        check(rows["opencode"]["turns"] == 3 and rows["opencode"]["reasoning"] == 150, "opencode: step_finish summed")
        check(rows["pi"]["turns"] == 3 and rows["pi"]["input"] == 2400, "pi: only message_end counted")
        check(
            rows["codex"]["input"] == 5000 and rows["codex"]["cache_read"] == 15000, "codex: cached is subset of input"
        )
        check(abs(rows["codex"]["cost_computed"] - 0.0735) < 1e-9, "codex: computed cost from prices")
        check(
            not rows["cursor"]["parse_ok"] and rows["cursor"]["cost_computed"] is None, "cursor: no usage, no fake $0"
        )
        logs = tmp / "results" / "logs"
        stderr_logs = "".join(f.read_text() for f in logs.glob("*.agent.stderr"))
        check(API_KEY not in stderr_logs and "***REDACTED***" in stderr_logs, "logs: harness secrets redacted")
        check(any(f.read_text() for f in logs.glob("fizzbuzz__claude__*.diff")), "audit diff saved")
        (run_dir,) = [p.parent for p in (tmp / "results").glob("*/trials.jsonl")]
        trials_text = (run_dir / "trials.jsonl").read_text()
        check(len(trials_text.splitlines()) == 5 and (run_dir / "run.json").exists(), "contract: run.json + 5 trials")
        check(API_KEY not in trials_text and "fix it" not in trials_text, "contract: no secret or prompt in trials")
        probes = [json.loads(x) for x in probe_file.read_text().splitlines()]
        check(all(not Path(pr["cwd"]).is_relative_to(tmp) for pr in probes), "workdir outside benchmark repo")
        check(not (tmp / "work").exists(), "no work dir created inside repo")

        out = run(tmp, "harness_bench.py", "reconcile").stdout
        check("4/5 runs matched" in out, "reconcile: tagged runs matched, cursor unmatched")
        rows = {r["harness"]: r for r in results(tmp / "results/results.jsonl")}
        check(abs(rows["pi"]["cost_litellm"] - 0.02) < 1e-9, "reconcile: unrelated rows ignored")
        check((tmp / "results/summary.md").exists() and (tmp / "results/summary.csv").exists(), "report files written")

        # --- LiteLLM-only script
        tags_file.unlink()
        out = run(tmp, "harness_bench_litellm.py", "run", "--settle", "0", "--allow-unsandboxed").stdout
        check("ignored 1 matrix entries" in out, "litellm script: cursor dropped")
        check("4/4 runs matched" in out, "litellm script: all runs matched")
        summary = (tmp / "results-litellm/summary.md").read_text()
        check("cursor" not in summary and summary.count("| litellm |") == 4, "litellm script: cost source litellm only")

        # --- git history, protected paths, sandbox prefix
        repo, buggy = make_git_task(tmp)
        probe_file.unlink()
        hist = {
            "id": "hist",
            "repo": repo,
            "ref": buggy,
            "prompt": "fix it",
            "hidden_tests": "tasks/fizzbuzz/hidden_tests",
            "test": f"{PY} run_tests.py",
            "protected_paths": ["run_tests.py", "tests_infra"],
        }
        cfg2 = dict(
            cfg,
            output_dir="results-sec",
            tasks=[hist],
            harnesses={"claude": fixtures.harness("claude", harness_env, "hack")},
            matrix=[{"harness": "claude", "model": "m"}],
            sandbox={"prefix": ["env", "FAKE_SANDBOX={workdir}"]},
        )
        (tmp / "config2.json").write_text(json.dumps(cfg2))
        run(tmp, "harness_bench.py", "run", config="config2.json")
        pr = json.loads(probe_file.read_text().splitlines()[0])
        check(pr["depth"] == "1" and pr["remotes"] == "", "workdir: no git history or remote (fix commit hidden)")
        check(pr["sandbox"] == pr["cwd"], "sandbox prefix wraps the agent command")
        row = results(tmp / "results-sec/results.jsonl")[0]
        check(not row["passed"] and row["sandboxed"], "protected paths restored: test-runner hack does not pass")
        diff = next((tmp / "results-sec/logs").glob("*.diff")).read_text()
        check("run_tests.py" in diff and "conftest.py" in diff, "audit diff shows tampering")

        # --- config validation and URL policy
        for bad, why in (
            (dict(fizz, id="/etc"), "absolute task id"),
            (dict(fizz, id="../x"), "traversal task id"),
            (dict(fizz, ref="--upload-pack=x"), "option-like git ref"),
        ):
            (tmp / "bad.json").write_text(json.dumps(dict(cfg, tasks=[bad])))
            p = run(tmp, "harness_bench.py", "run", "--allow-unsandboxed", config="bad.json", expect_ok=False)
            check(p.returncode != 0, f"rejects {why}")
        leak = dict(cfg, env_passthrough=["FAKE_KEY"])
        (tmp / "bad.json").write_text(json.dumps(leak))
        p = run(tmp, "harness_bench.py", "run", "--allow-unsandboxed", config="bad.json", expect_ok=False)
        check(p.returncode != 0 and "spend key" in p.stderr, "rejects passing spend key to agents")
        for url, ok in (
            ("https://litellm.example.com", True),
            ("http://localhost:4000", True),
            ("http://litellm.example.com", False),
        ):
            try:
                hb.check_litellm_url(url)
                passed = True
            except SystemExit:
                passed = False
            check(passed == ok, f"url policy: {url} {'allowed' if ok else 'blocked'}")
        print("\nPASS")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

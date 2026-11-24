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
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
import fake_litellm  # noqa: E402
import fixtures  # noqa: E402
import harness_bench as hb  # noqa: E402

PY = fixtures.PY
API_KEY = "sk-test-agent-key-0123456789"


def check(cond: object, msg: str) -> None:
    if not cond:
        sys.exit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def results(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def run(tmp: Path, *args: str, config: str = "config.json", expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run([PY, *args, "--config", config], cwd=tmp, capture_output=True, text=True)
    if expect_ok and p.returncode != 0:
        sys.exit(f"FAIL: {' '.join(args)}\n{p.stdout}\n{p.stderr}")
    return p


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True
    )


def make_git_task(tmp: Path) -> tuple[str, str]:
    """Repo with a buggy commit followed by the fix: the agent must only see the first."""
    repo = tmp / "gitrepo"
    shutil.copytree(fixtures.FIZZBUZZ / "repo", repo)
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


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="hb-smoke-"))
    hide_work = Path(tempfile.mkdtemp(prefix="hb-smoke-work-"))
    try:
        fixtures.copy_engine(tmp)
        shutil.copytree(fixtures.FIZZBUZZ, tmp / "tasks" / "fizzbuzz")
        tags_file, probe_file = tmp / "tags.txt", tmp / "probe.jsonl"
        marker = tmp / "host-exec-marker"
        os.environ.update(
            FAKE_TAGS_FILE=str(tags_file),
            FAKE_PROBE_FILE=str(probe_file),
            FAKE_KEY=fake_litellm.KEY,
            SECRET_CANARY="must-not-leak",
            FAKE_API_KEY=API_KEY,
            FAKE_MARKER=str(marker),
        )

        srv, port = fake_litellm.serve()
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        harness_env = {
            "FAKE_TAGS_FILE": "${FAKE_TAGS_FILE}",
            "FAKE_PROBE_FILE": "${FAKE_PROBE_FILE}",
            "FAKE_API_KEY": "${FAKE_API_KEY}",
            "FAKE_MARKER": "${FAKE_MARKER}",
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
        counts = {name: r["tool_calls"] for name, r in rows.items()}
        check(
            counts == {"claude": 2, "opencode": 2, "pi": 3, "codex": 2, "cursor": None},
            "tool calls counted per format, unknown (never 0) for cursor",
        )
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
        check(pr["depth"] == "" and pr["remotes"] == "", "workdir: no git repo, history or remote (fix commit hidden)")
        check(pr["sandbox"] == pr["cwd"], "sandbox prefix wraps the agent command")
        row = results(tmp / "results-sec/results.jsonl")[0]
        check(not row["passed"] and row["sandboxed"], "protected paths restored: test-runner hack does not pass")
        diff = next((tmp / "results-sec/logs").glob("*.diff")).read_text()
        check("run_tests.py" in diff and "conftest.py" in diff, "audit diff shows tampering")

        # --- graders: a failing `command` grader and `diff_constraints` decide the verdict
        open_hist = {key: val for key, val in hist.items() if key != "protected_paths"}
        no_graders = dict(open_hist, id="tampered")  # the hack agent makes run_tests.py exit 0
        forbid = [{"type": "hidden_tests"}, {"type": "diff_constraints", "forbidden": ["run_tests.py"]}]
        guarded = dict(open_hist, id="guarded", graders=forbid)
        failing = dict(fizz, id="failing", graders=[{"type": "hidden_tests"}, {"type": "command", "command": "false"}])
        cfg3 = dict(
            cfg2,
            output_dir="results-grade",
            tasks=[no_graders, guarded],
            matrix=[{"harness": "claude", "model": "m"}],
        )
        (tmp / "config3.json").write_text(json.dumps(cfg3))
        run(tmp, "harness_bench.py", "run", config="config3.json")
        by_task = {r["task"]: r for r in results(tmp / "results-grade/results.jsonl")}
        check(by_task["tampered"]["passed"], "graders: without constraints the test-runner hack passes the tests")
        check(not by_task["guarded"]["passed"], "graders: diff_constraints fails the hack even though the tests pass")
        cfg4 = dict(cfg, output_dir="results-cmd", tasks=[failing], matrix=[{"harness": "claude", "model": "fix"}])
        (tmp / "config4.json").write_text(json.dumps(cfg4))
        run(tmp, "harness_bench.py", "run", "--allow-unsandboxed", config="config4.json")
        row = results(tmp / "results-cmd/results.jsonl")[0]
        check(not row["passed"], "graders: a failing command grader fails the run although the hidden tests pass")
        check(any((tmp / "results-cmd/logs").glob("*grader-1-command.log")), "graders: command log written")

        # --- the agent cannot hide a forbidden change from `diff_constraints` through git, or run host commands
        modes = {"hg": "hide-git", "hgc": "hide-git-clean", "hi": "hide-ignore", "ha": "hide-attr", "hc": "hide-config"}
        guard = [{"type": "hidden_tests"}, {"type": "diff_constraints", "forbidden": ["run_tests.py", "conftest.py"]}]
        cfg5 = dict(
            cfg2,
            output_dir="results-hide",
            work_dir=str(hide_work),
            tasks=[dict(open_hist, id="hide", graders=guard)],
            harnesses={name: fixtures.harness("claude", harness_env, mode) for name, mode in modes.items()},
            matrix=[{"harness": name, "model": "m"} for name in modes],
        )
        (tmp / "config5.json").write_text(json.dumps(cfg5))
        run(tmp, "harness_bench.py", "run", config="config5.json")
        (hide_run,) = [p.parent for p in (tmp / "results-hide").glob("*/trials.jsonl")]
        trials = results(hide_run / "trials.jsonl")  # in matrix order
        check(len(trials) == len(modes), "git hiding: one trial per mode")
        for trial, mode in zip(trials, modes.values(), strict=True):
            by_grader = {g["grader"]: g["passed"] for g in trial["graders"]}
            check(
                by_grader == {"hidden_tests": True, "diff_constraints": False},
                f"git hiding ({mode}): the tests pass but diff_constraints fails the run",
            )
        check(not marker.exists(), "workdir .git/config: fsmonitor, filter and textconv commands never run on the host")
        check(not list(hide_work.glob(".*")), "baseline git dir and snapshot removed after each trial")

        # --- grader logs are redacted like the agent logs
        leak_cmd = f"echo spend={fake_litellm.KEY} agent={API_KEY}"
        leaky = dict(fizz, id="leaky", graders=[{"type": "hidden_tests"}, {"type": "command", "command": leak_cmd}])
        cfg6 = dict(cfg, output_dir="results-leak", tasks=[leaky], matrix=[{"harness": "claude", "model": "fix"}])
        (tmp / "config6.json").write_text(json.dumps(cfg6))
        run(tmp, "harness_bench.py", "run", "--allow-unsandboxed", config="config6.json")
        (grader_log,) = (tmp / "results-leak/logs").glob("*grader-1-command.log")
        text = grader_log.read_text()
        check(
            "***REDACTED***" in text and fake_litellm.KEY not in text and API_KEY not in text,
            "grader logs: spend key and harness secrets redacted",
        )

        # --- contamination guard: hidden tests are never copied into the workdir before grading, and an
        # unsandboxed agent cannot find them from its workdir or its parents. It CAN read the suite sources
        # elsewhere on the host (the sandbox, which mounts only the workdir, is what stops that).
        example_hidden = sorted((ROOT / "suites" / "examples-capability").glob("*/hidden_tests/test_*.py"))
        hidden_names = ",".join(p.name for p in example_hidden)
        probe_env = dict(harness_env, FAKE_HIDDEN_NAMES=hidden_names)
        cfg7 = dict(
            cfg,
            output_dir="results-probe",
            tasks=[fizz],
            harnesses={"claude": fixtures.harness("claude", probe_env, "probe-hidden")},
            matrix=[{"harness": "claude", "model": "m"}],
        )
        (tmp / "config7.json").write_text(json.dumps(cfg7))
        run(tmp, "harness_bench.py", "run", "--allow-unsandboxed", config="config7.json")
        (probe_row,) = results(tmp / "results-probe/results.jsonl")
        check(
            probe_row["agent_rc"] == 0, "contamination guard: no hidden test file or marker in the workdir or parents"
        )

        # positive controls: the probe must find a leak wherever it claims to look, at the depth of the real hidden
        # tests (`tasks/<task>/hidden_tests/<file>`, three levels down, four from the temp dir)
        agent_env = {k: v for k, v in os.environ.items() if k not in ("FAKE_KEY", "SECRET_CANARY")}
        agent_env["FAKE_HIDDEN_NAMES"] = hidden_names
        planted = example_hidden[0]

        def probe_from(cwd: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [PY, str(ROOT / "tests" / "fake_agent.py"), "claude", "probe-hidden"],
                cwd=cwd,
                env=dict(agent_env, **extra_env),
                capture_output=True,
                text=True,
            )

        for label, leak_name, text in (
            ("file name", planted.name, ""),
            ("marker", "notes.py", planted.read_text()),
        ):
            seen = tmp / f"seen-{leak_name}"
            seen.mkdir()
            (seen / leak_name).write_text(text)
            probe = probe_from(seen)
            check(probe.returncode == 7 and leak_name in probe.stderr, f"contamination guard: finds a leaked {label}")

        deep = tmp / "seen-deep"
        (deep / "a" / "b" / "c" / "d" / "e" / "f").mkdir(parents=True)
        (deep / "a" / "b" / "c" / "d" / "e" / "f" / "notes.py").write_text(planted.read_text())
        check(probe_from(deep).returncode == 7, "contamination guard: finds a leak six levels down the workdir")

        parent = tmp / "seen-parent"
        (parent / "work").mkdir(parents=True)
        (parent / planted.name).write_text("")
        check(probe_from(parent / "work").returncode == 7, "contamination guard: finds a leak in a parent directory")

        clean = tmp / "seen-clean"
        clean.mkdir()
        check(probe_from(clean).returncode == 0, "contamination guard: a clean workdir passes")
        reach = probe_from(clean, FAKE_SCAN_ROOTS=str(tmp))
        check(
            reach.returncode == 7 and "tasks/fizzbuzz/hidden_tests" in reach.stderr,
            "unsandboxed agents CAN read the benchmark dir: hidden-test isolation needs the sandbox",
        )

        # --- an agent that plants a symlink at a hidden test path cannot make the grader write a host file
        canary = tmp / "host-canary.txt"
        canary.write_text("host file")
        link_env = dict(harness_env, FAKE_CANARY_FILE="${FAKE_CANARY_FILE}")
        os.environ["FAKE_CANARY_FILE"] = str(canary)
        cfg8 = dict(
            cfg,
            output_dir="results-link",
            tasks=[fizz],
            harnesses={"claude": fixtures.harness("claude", link_env, "plant-link")},
            matrix=[{"harness": "claude", "model": "m"}],
        )
        (tmp / "config8.json").write_text(json.dumps(cfg8))
        run(tmp, "harness_bench.py", "run", "--allow-unsandboxed", config="config8.json")
        (link_row,) = results(tmp / "results-link/results.jsonl")
        check(
            canary.read_text() == "host file", "grader: a symlink planted at a hidden test path is not written through"
        )
        check(link_row["passed"], "grader: the hidden tests still run after the planted symlink is replaced")

        # --- a suite with a symlink is refused, so a solution is never copied through one
        link_suite = fixtures.write_fizzbuzz_suite(tmp, "linked")
        outside = tmp / "outside-suite"
        outside.mkdir()
        (link_suite / "fizzbuzz" / "repo" / "esc").symlink_to(outside, target_is_directory=True)
        (link_suite / "fizzbuzz" / "solution" / "esc").mkdir()
        (link_suite / "fizzbuzz" / "solution" / "esc" / "PWNED").write_text("x")
        p = subprocess.run(
            [PY, "-m", "hbench", "suite", "validate", "--run", "--allow-unsandboxed", str(link_suite)],
            cwd=tmp,
            capture_output=True,
            text=True,
        )
        check(
            p.returncode != 0 and "symlink esc" in p.stdout and not list(outside.iterdir()),
            "suite loader: a symlink in a suite is rejected and nothing is written outside",
        )

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
        shutil.rmtree(hide_work, ignore_errors=True)


if __name__ == "__main__":
    main()

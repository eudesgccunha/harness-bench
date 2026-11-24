#!/usr/bin/env python3
"""End-to-end test of the Docker sandbox with fake agents (no API keys, no network calls).

Needs the image: docker build -f sandbox/Dockerfile -t harness-bench-agents \\
                   --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
Run: python3 tests/docker_e2e.py   (exit 0 = pass)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = os.environ.get("HB_IMAGE", "harness-bench-agents")


def check(cond, msg):
    if not cond:
        sys.exit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def main():
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        sys.exit(f"image {IMAGE} not found; build it first (see docstring)")
    tmp = Path(tempfile.mkdtemp(prefix="hb-docker-"))
    shared = tmp / "shared"  # probe/tag files written from inside the container
    shared.mkdir()
    try:
        for f in ("harness_bench.py",):
            shutil.copy(ROOT / f, tmp)
        shutil.copytree(ROOT / "tasks" / "fizzbuzz", tmp / "tasks" / "fizzbuzz")
        env = dict(
            os.environ,
            FAKE_TAGS_FILE=str(shared / "tags.txt"),
            FAKE_PROBE_FILE=str(shared / "probe.jsonl"),
            SECRET_CANARY="must-not-leak",
            FAKE_API_KEY="sk-test-agent-key-0123456789",
        )
        agent = str(ROOT / "tests" / "fake_agent.py")
        prefix = [
            "docker",
            "run",
            "--rm",
            "--env-file",
            "{env_file}",
            "-v",
            "{workdir}:{workdir}",
            "-w",
            "{workdir}",
            "-v",
            f"{ROOT / 'tests'}:{ROOT / 'tests'}:ro",
            "-v",
            f"{shared}:{shared}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "1g",
            IMAGE,
        ]
        cfg = {
            "reps": 1,
            "sandbox": {"prefix": prefix},
            "harnesses": {
                k: {
                    "parser": k,
                    "command": ["python3", agent, k, mode],
                    "env": {
                        "FAKE_TAGS_FILE": "${FAKE_TAGS_FILE}",
                        "FAKE_PROBE_FILE": "${FAKE_PROBE_FILE}",
                        "FAKE_API_KEY": "${FAKE_API_KEY}",
                    },
                }
                for k, mode in (("claude", "fix"), ("pi", "nofix"))
            },
            "matrix": [{"harness": "claude", "model": "m"}, {"harness": "pi", "model": "p/m"}],
            "tasks": [
                {
                    "id": "fizzbuzz",
                    "repo": "tasks/fizzbuzz/repo",
                    "prompt": "fix it",
                    "hidden_tests": "tasks/fizzbuzz/hidden_tests",
                    "test": "python3 -m unittest -q test_hidden.py",
                }
            ],
        }
        (tmp / "config.json").write_text(json.dumps(cfg))
        p = subprocess.run(
            [sys.executable, "harness_bench.py", "run", "--config", "config.json"],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
        )
        if p.returncode:
            sys.exit(f"FAIL: run\n{p.stdout}\n{p.stderr}")
        rows = {r["harness"]: r for r in map(json.loads, (tmp / "results/results.jsonl").read_text().splitlines())}
        check(rows["claude"]["passed"] and not rows["pi"]["passed"], "tests run inside container decide pass/fail")
        check(all(r["sandboxed"] and r["agent_rc"] == 0 for r in rows.values()), "agents ran sandboxed, no secret leak")
        probes = [json.loads(x) for x in (shared / "probe.jsonl").read_text().splitlines()]
        check(all(pr["home"] == "/home/agent" for pr in probes), "container HOME, not host HOME")
        check(all(pr["uid"] == os.getuid() for pr in probes), "container uid matches host uid")
        check(all(pr["depth"] == "1" for pr in probes), "git baseline visible inside container")
        logs = tmp / "results" / "logs"
        check(
            "sk-test-agent-key" not in "".join(f.read_text() for f in logs.glob("*.agent.stderr")),
            "secrets redacted in logs",
        )
        check(not list(Path(tempfile.gettempdir()).glob("harness-bench-work-*/.*.env")), "env files removed")
        print("\nPASS")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

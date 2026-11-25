#!/usr/bin/env python3
"""End-to-end test of the Docker sandbox with fake agents (no API keys, no network calls).

Two runs against the real Docker: the `docker` sandbox type with the image pinned by the digest of the local image
(limits, no network, containers removed, digest recorded) and the older `sandbox.prefix` form, which must behave as
it always did.

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
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = os.environ.get("HB_IMAGE", "harness-bench-agents")


def check(cond: object, msg: str) -> None:
    if not cond:
        sys.exit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def pinned_image() -> str:
    """The local image pinned by its digest: `NAME@sha256:<id>`. The image id is the digest of its config, which the
    local image store resolves like a manifest digest."""
    inspect = subprocess.run(
        ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], capture_output=True, text=True
    )
    if inspect.returncode:
        sys.exit(f"image {IMAGE} not found; build it first (see docstring)")
    return f"{IMAGE.split('@')[0]}@{inspect.stdout.strip()}"


def containers_left() -> list[str]:
    """Names of the containers of the benchmark (`hb-...`) that still exist, running or not."""
    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=hb-", "--format", "{{.Names}}"], capture_output=True, text=True
    )
    return ps.stdout.split()


def main() -> None:
    image = pinned_image()
    tmp = Path(tempfile.mkdtemp(prefix="hb-docker-"))
    try:
        shutil.copy(ROOT / "harness_bench.py", tmp)
        shutil.copytree(ROOT / "hbench", tmp / "hbench", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "suites" / "regression" / "fizzbuzz", tmp / "tasks" / "fizzbuzz")
        shared = tmp / "shared"  # probe/tag files written from inside the container
        agent = str(ROOT / "tests" / "fake_agent.py")
        mounts = [f"{ROOT / 'tests'}:{ROOT / 'tests'}:ro", f"{shared}:{shared}"]
        docker_sandbox = {
            "type": "docker",
            "image": image,
            "network": "none",
            "limits": {"cpus": 2, "memory": "1g", "pids": 256},
            "mounts": mounts,
        }
        shared.mkdir()
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
            *[part for mount in mounts for part in ("-v", mount)],
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
        env = dict(
            os.environ,
            FAKE_TAGS_FILE=str(shared / "tags.txt"),
            FAKE_PROBE_FILE=str(shared / "probe.jsonl"),
            SECRET_CANARY="must-not-leak",
            FAKE_API_KEY="sk-test-agent-key-0123456789",
        )
        run_case(tmp, env, "docker", docker_sandbox, agent, shared, digest=image.split("@")[1])
        run_case(tmp, env, "prefix", {"prefix": prefix}, agent, shared, digest=None)
        run_timeout_case(tmp, env, docker_sandbox, agent)
        print("\nPASS")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_timeout_case(tmp: Path, env: dict[str, str], sandbox: Mapping[str, object], agent: str) -> None:
    """An agent that outlasts its timeout keeps writing to /work in its container unless the engine removes it: the
    file must have stopped changing when the grader looks, and the container must be gone."""
    steady = (
        "python3 -c \"import os, time; a = os.path.getsize('late.txt'); time.sleep(1); "
        "raise SystemExit(0 if os.path.getsize('late.txt') == a else 1)\""
    )
    cfg = {
        "reps": 1,
        "output_dir": "results-timeout",
        "sandbox": sandbox,
        "harnesses": {
            "writer": {
                "parser": "claude",
                "command": ["python3", agent, "claude", "slow-writer"],
                "env": {"FAKE_TAGS_FILE": "${FAKE_TAGS_FILE}", "FAKE_PROBE_FILE": "${FAKE_PROBE_FILE}"},
            }
        },
        "matrix": [{"harness": "writer", "model": "m"}],
        "tasks": [
            {
                "id": "late",
                "repo": "tasks/fizzbuzz/repo",
                "prompt": "write",
                "timeout": 5,
                "graders": [{"type": "command", "command": steady}],
            }
        ],
    }
    (tmp / "config-timeout.json").write_text(json.dumps(cfg))
    p = subprocess.run(
        [sys.executable, "harness_bench.py", "run", "--config", "config-timeout.json"],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
    )
    if p.returncode:
        sys.exit(f"FAIL: timeout run\n{p.stdout}\n{p.stderr}")
    (row,) = map(json.loads, (tmp / "results-timeout" / "results.jsonl").read_text().splitlines())
    check(row["agent_timed_out"], "docker: the agent outlasted its timeout")
    check(row["passed"], "docker: its container was removed before grading (late.txt had stopped growing)")
    check(not containers_left(), "docker: no container is left after the timeout")


def run_case(
    tmp: Path,
    env: dict[str, str],
    label: str,
    sandbox: Mapping[str, object],
    agent: str,
    shared: Path,
    digest: str | None,
) -> None:
    """One `run` through the real Docker with `sandbox`; `digest` is the image digest the trials must record."""
    for leftover in shared.iterdir():
        leftover.unlink()
    offline = {  # a container without a network cannot connect anywhere
        "id": "offline",
        "repo": "tasks/fizzbuzz/repo",
        "prompt": "do nothing",
        "graders": [
            {
                "type": "command",
                "command": (
                    'python3 -c "import socket, sys; s = socket.socket(); s.settimeout(3); '
                    "sys.exit(0 if s.connect_ex(('1.1.1.1', 53)) else 1)\""
                ),
            }
        ],
    }
    fizzbuzz = {
        "id": "fizzbuzz",
        "repo": "tasks/fizzbuzz/repo",
        "prompt": "fix it",
        "hidden_tests": "tasks/fizzbuzz/hidden_tests",
        "test": "python3 -m unittest -q test_hidden.py",
    }
    cfg = {
        "reps": 1,
        "output_dir": f"results-{label}",
        "sandbox": sandbox,
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
        "tasks": [fizzbuzz, *([offline] if digest else [])],
    }
    (tmp / f"config-{label}.json").write_text(json.dumps(cfg))
    p = subprocess.run(
        [sys.executable, "harness_bench.py", "run", "--config", f"config-{label}.json"],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
    )
    if p.returncode:
        sys.exit(f"FAIL: {label} run\n{p.stdout}\n{p.stderr}")
    out = tmp / f"results-{label}"
    rows = {(r["task"], r["harness"]): r for r in map(json.loads, (out / "results.jsonl").read_text().splitlines())}
    fizz = {h: rows["fizzbuzz", h] for h in ("claude", "pi")}
    check(
        fizz["claude"]["passed"] and not fizz["pi"]["passed"],
        f"{label}: tests run inside the container decide pass/fail",
    )
    check(all(r["sandboxed"] and r["agent_rc"] == 0 for r in rows.values()), f"{label}: agents ran sandboxed, no leak")
    probes = [json.loads(x) for x in (shared / "probe.jsonl").read_text().splitlines()]
    check(all(pr["home"] == "/home/agent" for pr in probes), f"{label}: container HOME, not host HOME")
    check(all(pr["uid"] == os.getuid() for pr in probes), f"{label}: container uid matches host uid")
    check(
        all(pr["depth"] == "" and pr["remotes"] == "" for pr in probes),
        f"{label}: no git history inside the container (the baseline lives outside the workdir)",
    )
    logs = out / "logs"
    check(
        "sk-test-agent-key" not in "".join(f.read_text() for f in logs.glob("*.agent.stderr")),
        f"{label}: secrets redacted in logs",
    )
    check(not list(Path(tempfile.gettempdir()).glob("harness-bench-work-*/.*.env")), f"{label}: env files removed")
    if digest is None:
        return
    check(
        rows["offline", "claude"]["passed"] and rows["offline", "pi"]["passed"],
        "docker: a container with network none cannot connect out",
    )
    (run_dir,) = [d.parent for d in out.glob("*/trials.jsonl")]
    info = json.loads((run_dir / "run.json").read_text())
    trials = [json.loads(x) for x in (run_dir / "trials.jsonl").read_text().splitlines()]
    check(
        info["sandbox"].startswith("docker:") and all(t["image_digest"] == digest for t in trials),
        "docker: run.json names the sandbox and every trial records the image digest",
    )
    check(not containers_left(), "docker: no container of the benchmark is left behind")
    check(not (run_dir / "heartbeat.json").exists(), "docker: the heartbeat file is gone after the run")


if __name__ == "__main__":
    main()

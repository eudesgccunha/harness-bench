"""A fake `docker` executable for tests: it records its argv and answers the few commands the engine uses.

`install(dest)` writes a launcher named `docker` into `dest` (put `dest` first on PATH). The fake reads:
  FAKE_DOCKER_LOG       file that gets one JSON argv per call (required)
  FAKE_DOCKER_PS        text printed by `docker ps` (container names or ids, one per line)
  FAKE_DOCKER_RM_RC     exit code of `docker rm` (default 0); FAKE_DOCKER_RM_ERR is printed to stderr
  FAKE_DOCKER_MISSING   comma separated commands that `docker run ... <image> <cmd> --version` cannot find (127)
  FAKE_DOCKER_VERSION   what `docker run ... <image> <cmd> --version` prints (default 1.2.3)
  FAKE_DOCKER_INSPECT   JSON printed by `docker image inspect` and `docker network inspect`
  FAKE_DOCKER_INSPECT_RC  exit code of both inspect commands (default 0)
  FAKE_DOCKER_STATE     directory: with it, `docker run` starts the command in a session of its own (a container
                        that outlives a killed client) and `docker rm -f NAME` kills it
`docker run -v HOST:/work -w /work --env-file F ... IMAGE CMD...` runs CMD on the host with cwd HOST and the
environment of F: enough to run the engine end to end through the real `docker run` argv of the sandbox.
"""

import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

# `docker run` options that take a value; every other option is a flag.
VALUE_OPTIONS = {
    "--name",
    "--env-file",
    "-v",
    "-w",
    "--network",
    "--cpus",
    "--memory",
    "--memory-swap",
    "--pids-limit",
    "--cap-drop",
    "--security-opt",
    "--user",
}


def install(dest: Path) -> Path:
    """Write the `docker` launcher into `dest`; returns its path."""
    launcher = dest / "docker"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).resolve()}" "$@"\n')
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return launcher


def parse_run(args: list[str]) -> tuple[dict[str, str], list[str], list[str]]:
    """The options with a value, the option list of `-v` mounts and the image-and-command of a `docker run`."""
    options: dict[str, str] = {}
    mounts: list[str] = []
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in VALUE_OPTIONS:
            if args[i] == "-v":
                mounts.append(args[i + 1])
            options[args[i]] = args[i + 1]
            i += 2
        else:
            options[args[i]] = ""
            i += 1
    return options, mounts, args[i:]


def kill_container(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def read_env_file(path: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "")}
    for line in Path(path).read_text().splitlines():
        key, _, value = line.partition("=")
        env[key] = value
    return env


def run(args: list[str]) -> int:
    options, mounts, rest = parse_run(args)
    command = rest[1:]  # rest[0] is the image
    if command and command[-1] == "--version":  # a probe such as `docker run ... IMAGE claude --version`
        if command[0] in os.environ.get("FAKE_DOCKER_MISSING", "").split(","):
            print(f"exec: {command[0]!r}: executable file not found in $PATH", file=sys.stderr)
            return 127
        print(os.environ.get("FAKE_DOCKER_VERSION", "1.2.3"))
        return 0
    workdir = next(m.split(":", 1)[0] for m in mounts if m.endswith(":/work"))
    env = read_env_file(options["--env-file"]) if "--env-file" in options else {"PATH": os.environ.get("PATH", "")}
    state = os.environ.get("FAKE_DOCKER_STATE")
    detached = state is not None and "--name" in options
    child = subprocess.Popen(command, cwd=workdir, env=env, stdin=subprocess.DEVNULL, start_new_session=detached)
    if detached:
        (Path(state or "") / options["--name"]).write_text(str(child.pid))
    return child.wait()


def main(argv: list[str]) -> int:
    with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
        log.write(json.dumps(argv) + "\n")
    command = argv[0] if argv else ""
    if command == "run":
        return run(argv[1:])
    if command == "ps":
        print(os.environ.get("FAKE_DOCKER_PS", ""), end="")
        return 0
    if command == "rm":
        state = os.environ.get("FAKE_DOCKER_STATE")
        for name in argv[2:] if state else []:
            pid_file = Path(state or "") / name
            if pid_file.exists():
                kill_container(int(pid_file.read_text()))
        if os.environ.get("FAKE_DOCKER_RM_ERR"):
            print(os.environ["FAKE_DOCKER_RM_ERR"], file=sys.stderr)
        return int(os.environ.get("FAKE_DOCKER_RM_RC", "0"))
    if command in ("image", "network"):
        print(os.environ.get("FAKE_DOCKER_INSPECT", "[]"))
        return int(os.environ.get("FAKE_DOCKER_INSPECT_RC", "0"))
    print(f"fake docker: unsupported command {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

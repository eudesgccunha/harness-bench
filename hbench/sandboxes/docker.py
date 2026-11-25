"""The `docker` sandbox (ADR 0010): every command runs in a fresh `docker run` of an image pinned by digest.

The container gets the trial's workdir at `/work`, no network unless the config names one, no capabilities, no
new privileges, the host user and the configured CPU, memory and process limits. The engine still starts the `docker`
process, so its timeout and process-group kill apply; killing that client does not stop its container, so
`end_step` removes the container of every command as soon as the command ends or times out.

`mounts` are trusted config: a writable mount of anything sensitive (`/var/run/docker.sock`, a home directory) hands
it to the agent and breaks the isolation. Mount helpers read-only (`:ro`).
"""

import os
import posixpath
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hbench.sandboxes.base import HostSession, ProcRunner, SandboxError, SandboxSession
from hbench.sandboxes.limits import Limits, parse_limits

IMAGE = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._/:-]*)@sha256:(?P<digest>[0-9a-f]{64})")
NETWORK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
MOUNT = re.compile(r"/[^:\s]+:/[^:\s]+(:ro)?")
TRIAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
LIMIT_KEYS = frozenset({"cpus", "memory", "pids"})
CONTAINER_WORKDIR = "/work"
HELPER_TIMEOUT = 60  # seconds for `docker rm` and `docker ps`


def _image(section: Mapping[str, Any]) -> tuple[str, str]:
    """The pinned image and its digest."""
    image = section.get("image")
    if not isinstance(image, str):
        raise SandboxError("sandbox.image must be a string: name@sha256:<64 hex>")
    match = IMAGE.fullmatch(image)
    if match is None:
        raise SandboxError(
            f"sandbox.image {image!r} must be pinned by digest: name@sha256:<64 lowercase hex> "
            "(a tag can change under the benchmark; `docker image inspect NAME --format '{{.Id}}'` gives the digest)"
        )
    return image, f"sha256:{match.group('digest')}"


def _network(section: Mapping[str, Any]) -> str:
    network = section.get("network", "none")
    if not isinstance(network, str) or NETWORK.fullmatch(network) is None or network == "host":
        raise SandboxError(
            "sandbox.network must be 'none' or the name of a Docker network (not 'host': it shares the host network)"
        )
    return network


def _mounts(section: Mapping[str, Any]) -> list[str]:
    """The extra mounts, with each container path normalized (`/x/../work` is `/work`)."""
    raw = section.get("mounts", [])
    problem = SandboxError(
        "sandbox.mounts must be a list of 'HOST:CONTAINER' or 'HOST:CONTAINER:ro' absolute paths, none of them "
        f"{CONTAINER_WORKDIR} or under it (that is the workdir)"
    )
    if not (isinstance(raw, list) and all(isinstance(m, str) and MOUNT.fullmatch(m) for m in raw)):
        raise problem
    mounts = []
    for mount in raw:
        host, target, *mode = mount.split(":")
        target = "/" + posixpath.normpath(target).lstrip("/")  # `//work` is `/work` to Docker
        if target == CONTAINER_WORKDIR or target.startswith(CONTAINER_WORKDIR + "/"):
            raise problem
        mounts.append(":".join([host, target, *mode]))
    return mounts


class DockerSession(HostSession):
    def __init__(self, sandbox: "DockerSandbox", workdir: Path, trial_id: str) -> None:
        super().__init__(workdir)
        self._sandbox = sandbox
        self._trial_id = trial_id
        self._names: list[str] = []  # the containers of the step in progress
        self._started = 0  # commands run so far: names stay unique after `end_step`

    def argv(self, argv: list[str], env_file: Path) -> list[str]:
        name = f"hb-{self._trial_id}-{self._started}"
        self._started += 1
        self._names.append(name)
        s = self._sandbox
        limits = s.limits
        flags = [
            *(["--cpus", f"{limits.cpus:g}"] if limits.cpus is not None else []),
            *(["--memory", limits.memory, "--memory-swap", limits.memory] if limits.memory is not None else []),
            *(["--pids-limit", str(limits.pids)] if limits.pids is not None else []),
        ]
        return [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--env-file",
            str(env_file),
            "-v",
            f"{self.workdir}:{CONTAINER_WORKDIR}",
            *[part for mount in s.mounts for part in ("-v", mount)],
            "-w",
            CONTAINER_WORKDIR,
            "--network",
            s.network,
            *flags,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            s.image,
            *argv,
        ]

    def end_step(self) -> None:
        """Remove the containers of the command that just ended: killing the `docker run` client leaves its container
        running, and it would keep writing to the workdir while the engine reads it."""
        names, self._names = self._names, []
        try:
            self._sandbox.remove(names)
        except SandboxError:
            self._names = names + self._names  # `destroy` tries again
            raise

    def destroy(self) -> None:
        self.end_step()  # whatever a command that never reached its end left


class DockerSandbox:
    isolated = True

    def __init__(self, section: Mapping[str, Any], run: ProcRunner, env: Mapping[str, str]) -> None:
        self.image, self._digest = _image(section)
        self.network = _network(section)
        self.limits: Limits = parse_limits(section.get("limits"), LIMIT_KEYS)
        self.mounts = _mounts(section)
        self._run = run
        self._env = env

    def create(self, workdir: Path, trial_id: str) -> SandboxSession:
        if TRIAL_ID.fullmatch(trial_id) is None:
            raise SandboxError(f"trial id {trial_id!r} cannot name a container: use letters, digits, _ . -")
        return DockerSession(self, workdir, trial_id)

    def _call(self, command: list[str]) -> tuple[int, str, str]:
        """`(returncode, stdout, stderr)` of a helper command. Docker missing or the helper timing out is a
        `SandboxError`: the caller cannot know what is still running."""
        try:
            rc, timed_out, out, err = self._run(command, None, self._env, HELPER_TIMEOUT)
        except OSError as e:
            raise SandboxError(f"{' '.join(command)}: {e.strerror}") from e
        if timed_out:
            raise SandboxError(f"{' '.join(command)} timed out after {HELPER_TIMEOUT} s")
        return rc, out, err

    def _force_remove(self, name: str) -> None:
        command = ["docker", "rm", "-f", name]
        rc, _, err = self._call(command)
        if rc and "No such container" not in err:  # one that is already gone (`--rm` removed it) is what we want
            raise SandboxError(f"{' '.join(command)} failed: {err.strip()}")

    def _exists(self, name: str) -> bool:
        command = ["docker", "ps", "-aq", "--filter", f"name=^{name}$"]
        rc, out, err = self._call(command)
        if rc:
            raise SandboxError(f"{' '.join(command)} failed: {err.strip()}")
        return bool(out.split())

    def remove(self, names: list[str]) -> None:
        """Make sure no container of `names` exists: `docker rm -f` each, check that it is gone, and once more if it
        is not (a container that was still starting when `rm` ran). Fails closed with `SandboxError`: a container
        that cannot be shown to be gone may still be writing to the workdir."""
        for name in names:
            self._force_remove(name)
            if self._exists(name):
                self._force_remove(name)
                if self._exists(name):
                    raise SandboxError(f"container {name} could not be removed")

    def reclaim(self, trial_id: str) -> None:
        command = ["docker", "ps", "-a", "--filter", f"name=hb-{trial_id}-", "--format", "{{.Names}}"]
        rc, out, err = self._call(command)
        if rc:
            raise SandboxError(f"{' '.join(command)} failed: {err.strip()}")
        self.remove(out.split())

    def image_digest(self) -> str | None:
        return self._digest

    def describe(self) -> str:
        return f"docker:{self.image}"

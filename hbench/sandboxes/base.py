"""The sandbox protocol and what the built-in sandboxes share (ADR 0010).

A sandbox only builds argv and manages its own lifecycle. The engine keeps running every process (ADR 0007), so
the env allowlist, the timeout, the process-group kill and the log redaction apply to every sandbox alike.
"""

from collections.abc import Callable, Mapping
from pathlib import Path, PurePath
from typing import Protocol, runtime_checkable

from hbench.safe_copy import copy_out_tree, safe_copy_to

# The engine's `run_proc`: `(argv, cwd, env, timeout) -> (returncode, timed_out, stdout, stderr)`.
ProcRunner = Callable[[list[str], Path | None, Mapping[str, str], float], tuple[int, bool, str, str]]


class SandboxError(ValueError):
    """The sandbox configuration is invalid, or a copy through the sandbox was refused."""


@runtime_checkable
class SandboxSession(Protocol):
    """The sandbox of one trial: it wraps each command of the trial and removes whatever it leaves behind."""

    def argv(self, argv: list[str], env_file: Path) -> list[str]:
        """The command that runs `argv` inside the sandbox. `env_file` holds the environment (Docker format)."""

    def copy_in(self, src: Path, rel: str) -> None:
        """Put the trusted file or tree `src` at `rel` in the workdir, never through a link the agent planted."""

    def copy_out(self, rel: str, dest: Path) -> None:
        """Copy `rel` of the workdir (untrusted) to the host path `dest`; a link in it is refused."""

    def end_step(self) -> None:
        """Stop whatever the command just run left behind (its container, which the engine's kill of the client
        process does not reach). The engine calls it after every command, also after a timeout or an error, before
        it reads the workdir or starts the next command."""

    def destroy(self) -> None:
        """Remove anything the trial left (containers); the engine always calls it, in a `finally`."""


@runtime_checkable
class Sandbox(Protocol):
    """A way to run the commands of a trial in isolation (or, for `local-tmp`, without it)."""

    isolated: bool  # False: agent code runs as the user, so the run needs `--allow-unsandboxed`

    def create(self, workdir: Path, trial_id: str) -> SandboxSession:
        """The session of one trial. `workdir` is the trial's directory, `trial_id` names its containers."""

    def reclaim(self, trial_id: str) -> None:
        """Remove what an interrupted run left for this trial (a container still running); resume calls it."""

    def image_digest(self) -> str | None:
        """The digest of the image the commands run in, or None when there is none."""

    def describe(self) -> str:
        """Short name for `run.json`: the type and, where it is not a secret, what it runs."""


def relative_path(rel: str) -> Path:
    """`rel` as a path inside the workdir: not empty, not absolute, no `..`."""
    path = PurePath(rel)
    if not rel or path.is_absolute() or ".." in path.parts:
        raise SandboxError(f"path {rel!r} must be relative to the workdir, without '..'")
    return Path(rel)


class HostSession:
    """Copies for the sandboxes whose workdir is the host directory itself (a bind mount counts): the host copies
    through `safe_copy`, so it never writes through a link the agent left."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir

    def copy_in(self, src: Path, rel: str) -> None:
        safe_copy_to(src, self.workdir, relative_path(rel))

    def copy_out(self, rel: str, dest: Path) -> None:
        copy_out_tree(self.workdir, relative_path(rel), dest)

    def end_step(self) -> None:
        """Nothing outlives the process group the engine kills."""

    def destroy(self) -> None:
        """Nothing is left behind by default."""

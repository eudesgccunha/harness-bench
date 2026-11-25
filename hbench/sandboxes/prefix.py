"""The `prefix` sandbox: `sandbox.prefix` is a command template put in front of every command (ADR 0010).

This is the form the benchmark has always had, so the argv it builds is byte-identical to the earlier `wrap()`:
`{workdir}` and `{env_file}` are filled in and the prefix goes before the command.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hbench.sandboxes.base import HostSession, SandboxError, SandboxSession


class PrefixSession(HostSession):
    def __init__(self, workdir: Path, prefix: list[str]) -> None:
        super().__init__(workdir)
        self._prefix = prefix

    def argv(self, argv: list[str], env_file: Path) -> list[str]:
        fields = {"workdir": str(self.workdir), "env_file": str(env_file)}
        return [part.format(**fields) for part in self._prefix] + argv


class PrefixSandbox:
    isolated = True

    def __init__(self, section: Mapping[str, Any]) -> None:
        prefix = section.get("prefix")
        if not (isinstance(prefix, list) and prefix and all(isinstance(part, str) for part in prefix)):
            raise SandboxError("sandbox.prefix must be a non-empty list of strings")
        self._prefix: list[str] = prefix

    def create(self, workdir: Path, trial_id: str) -> SandboxSession:
        return PrefixSession(workdir, self._prefix)

    def reclaim(self, trial_id: str) -> None:
        """A prefix cannot name what it started, so there is nothing to remove."""

    def image_digest(self) -> str | None:
        return None

    def describe(self) -> str:
        return "prefix"

"""The `local-tmp` sandbox: commands run on the host as the user, in the trial's temp workdir. It isolates
nothing, so a run with it needs `--allow-unsandboxed` (only inside a disposable VM).

`limits` are best effort resource limits (`RLIMIT_AS`, `RLIMIT_NPROC`, `RLIMIT_CPU`), set by a small launcher that
runs before the command: no `preexec_fn`, which is unsafe while the heartbeat thread runs. Note that `RLIMIT_NPROC`
counts every process of the user, so a low `pids` also stops the benchmark's own `fork`s. `cpus` has no meaning
without a container and is ignored. A process can still reach everything the user can.
"""

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hbench.sandboxes.base import HostSession, SandboxSession
from hbench.sandboxes.limits import Limits, parse_limits

LIMIT_KEYS = frozenset({"cpus", "memory", "pids", "cpu_seconds"})
# `argv[1]` is the JSON object of rlimit names and values, `argv[2:]` the command.
LAUNCHER = (
    "import json, os, resource, sys\n"
    "for name, value in json.loads(sys.argv[1]).items():\n"
    "    resource.setrlimit(getattr(resource, name), (value, value))\n"
    "os.execvp(sys.argv[2], sys.argv[2:])\n"
)


def rlimits(limits: Limits) -> dict[str, int]:
    """The `resource` limits a `Limits` maps to; only the ones that are set."""
    wanted = {"RLIMIT_AS": limits.memory_bytes, "RLIMIT_NPROC": limits.pids, "RLIMIT_CPU": limits.cpu_seconds}
    return {name: value for name, value in wanted.items() if value is not None}


class LocalSession(HostSession):
    def __init__(self, workdir: Path, limits: dict[str, int]) -> None:
        super().__init__(workdir)
        self._limits = limits

    def argv(self, argv: list[str], env_file: Path) -> list[str]:
        if not self._limits:
            return argv
        return [sys.executable, "-I", "-c", LAUNCHER, json.dumps(self._limits), *argv]


class LocalTmpSandbox:
    isolated = False

    def __init__(self, section: Mapping[str, Any]) -> None:
        self._limits = rlimits(parse_limits(section.get("limits"), LIMIT_KEYS))

    def create(self, workdir: Path, trial_id: str) -> SandboxSession:
        return LocalSession(workdir, self._limits)

    def reclaim(self, trial_id: str) -> None:
        """Nothing outlives the process group the engine kills."""

    def image_digest(self) -> str | None:
        return None

    def describe(self) -> str:
        return "local-tmp"

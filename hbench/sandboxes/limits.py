"""The per-trial limits of a sandbox (`sandbox.limits`, ADR 0010): CPUs, memory, processes and, for `local-tmp`, CPU
seconds. A bad value is a config error before any run."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hbench.sandboxes.base import SandboxError

_MEMORY = re.compile(r"[1-9][0-9]*[kmg]")
_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3}


@dataclass(frozen=True)
class Limits:
    """Each limit is None when the config does not set it."""

    cpus: float | None = None
    memory: str | None = None  # `<digits>k|m|g`, as `docker run --memory` takes it
    pids: int | None = None
    cpu_seconds: int | None = None  # CPU time of one process (`local-tmp` only)

    @property
    def memory_bytes(self) -> int | None:
        return None if self.memory is None else int(self.memory[:-1]) * _UNITS[self.memory[-1]]


def _whole(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SandboxError(f"sandbox.limits.{name} must be a whole number >= 1")
    return value


def parse_limits(raw: Any, allowed: frozenset[str]) -> Limits:
    """The limits of a sandbox section (`raw` is its `limits` value, or None). Only the keys in `allowed` may be set."""
    if raw is None:
        return Limits()
    if not isinstance(raw, Mapping):
        raise SandboxError("sandbox.limits must be an object")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise SandboxError(f"sandbox.limits has unknown or unsupported keys {unknown} (allowed: {sorted(allowed)})")
    fields: dict[str, Any] = {}
    if "cpus" in raw:
        cpus = raw["cpus"]
        if isinstance(cpus, bool) or not isinstance(cpus, int | float) or not math.isfinite(cpus) or cpus <= 0:
            raise SandboxError("sandbox.limits.cpus must be a number > 0")
        fields["cpus"] = float(cpus)
    if "memory" in raw:
        if not isinstance(raw["memory"], str) or not _MEMORY.fullmatch(raw["memory"]):
            raise SandboxError("sandbox.limits.memory must look like 512m or 2g (a whole number and k, m or g)")
        fields["memory"] = raw["memory"]
    for name in ("pids", "cpu_seconds"):
        if name in raw:
            fields[name] = _whole(name, raw[name])
    return Limits(**fields)

"""Sandboxes: the protocol, the built-in types and the registry (ADR 0010).

`sandbox_for(cfg, run, env)` is the only way the engine gets a sandbox: the built-in type of `sandbox.type` (default:
`prefix` when `sandbox.prefix` is set, else `local-tmp`), else the plugin of that name in the entry-point group
`harness_bench.sandboxes`.
"""

from collections.abc import Mapping
from typing import Any, TypeGuard

from hbench.plugins import SANDBOX_GROUP, create_plugin
from hbench.sandboxes.base import ProcRunner, Sandbox, SandboxError, SandboxSession
from hbench.sandboxes.docker import DockerSandbox
from hbench.sandboxes.local_tmp import LocalTmpSandbox
from hbench.sandboxes.prefix import PrefixSandbox

__all__ = [
    "BUILTIN_TYPES",
    "ProcRunner",
    "Sandbox",
    "SandboxError",
    "SandboxSession",
    "sandbox_for",
]

BUILTIN_TYPES = ("prefix", "docker", "local-tmp")
_ALLOWED_KEYS: Mapping[str, frozenset[str]] = {
    "prefix": frozenset({"type", "prefix"}),
    "docker": frozenset({"type", "image", "network", "limits", "mounts"}),
    "local-tmp": frozenset({"type", "limits"}),
}


def _is_sandbox(obj: object) -> TypeGuard[Sandbox]:
    return isinstance(obj, Sandbox)


def _check_keys(kind: str, section: Mapping[str, Any]) -> None:
    unknown = sorted(set(section) - _ALLOWED_KEYS[kind] - {"_comment"})
    if unknown:
        raise SandboxError(f"unknown sandbox keys: {unknown} (a {kind} sandbox takes {sorted(_ALLOWED_KEYS[kind])})")


def sandbox_for(cfg: Mapping[str, Any], run: ProcRunner, env: Mapping[str, str]) -> Sandbox:
    """The sandbox the config asks for. `run` and `env` are how a sandbox runs its own helper commands (the engine's
    `run_proc` and its env allowlist). Raises `SandboxError` or `PluginError` (both `ValueError`) for a bad section."""
    section = cfg.get("sandbox") or {}
    if not isinstance(section, Mapping):
        raise SandboxError("sandbox must be an object")
    kind = section.get("type") or ("prefix" if section.get("prefix") else "local-tmp")
    if not isinstance(kind, str):
        raise SandboxError("sandbox.type must be a string")
    if kind in _ALLOWED_KEYS:
        _check_keys(kind, section)
    if kind == "prefix":
        return PrefixSandbox(section)
    if kind == "docker":
        return DockerSandbox(section, run, env)
    if kind == "local-tmp":
        return LocalTmpSandbox(section)
    return create_plugin(SANDBOX_GROUP, kind, BUILTIN_TYPES, section, _is_sandbox, "Sandbox")

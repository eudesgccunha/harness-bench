"""Validation of the options of the artifact graders: a typo in a config is an error, never a silent default."""

from collections.abc import Collection, Mapping
from typing import Any

COMMON_KEYS = frozenset({"type", "weight"})
MAX_VIOLATIONS = 1000  # an artifact can make millions of violations: a grader keeps this many and counts the rest


def cap_violations(messages: list[str]) -> list[str]:
    """`messages` cut to `MAX_VIOLATIONS`, followed by `… N more` when some were dropped."""
    if len(messages) <= MAX_VIOLATIONS:
        return messages
    return [*messages[:MAX_VIOLATIONS], f"… {len(messages) - MAX_VIOLATIONS} more"]


def reject_unknown(cfg: Mapping[str, Any], allowed: Collection[str], grader: str) -> None:
    """Raises ValueError for a key that is neither in `allowed` nor one every grader entry has."""
    unknown = sorted(set(cfg) - set(allowed) - COMMON_KEYS)
    if unknown:
        raise ValueError(f"{grader}: unknown option(s) {', '.join(unknown)}")


def string_option(cfg: Mapping[str, Any], key: str, grader: str) -> str:
    """The required non-empty string option `key`."""
    value = cfg.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{grader}: '{key}' must be a non-empty string")
    return value


def bool_option(cfg: Mapping[str, Any], key: str, default: bool, grader: str) -> bool:
    value = cfg.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{grader}: '{key}' must be true or false, got {value!r}")
    return value

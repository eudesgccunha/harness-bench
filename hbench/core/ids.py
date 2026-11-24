"""Identifiers and content hashes for runs and trials."""

import hashlib
import json
import uuid


def new_run_id() -> str:
    """A fresh run identifier: UUID4, 32 lowercase hex characters."""
    return uuid.uuid4().hex


def trial_id(run_id: str, task_id: str, target_id: str, rep: int) -> str:
    """Deterministic trial identifier: first 16 hex characters of sha256 over the four parts."""
    key = "|".join((run_id, task_id, target_id, str(rep)))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def sha256_json(obj: object) -> str:
    """sha256 hex of the canonical JSON form (sorted keys, no whitespace)."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()

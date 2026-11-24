"""Config profiles. The LiteLLM profile keeps only harnesses that bill through LiteLLM and forces its cost."""

import sys
from typing import Any

# Parsers of harnesses that cannot send the LiteLLM tag header (no per-run spend to match).
EXCLUDED_PARSERS = {"cursor"}


def apply_litellm_profile(cfg: dict[str, Any]) -> dict[str, Any]:
    """A new config for the LiteLLM-only benchmark: no excluded harnesses, cost from LiteLLM, own output dir."""
    if not cfg.get("litellm"):
        sys.exit("config needs a 'litellm' section (base_url, api_key)")
    excluded = {name for name, h in cfg["harnesses"].items() if h.get("parser") in EXCLUDED_PARSERS}
    harnesses = {k: v for k, v in cfg["harnesses"].items() if k not in excluded}
    dropped = [m for m in cfg["matrix"] if m["harness"] in excluded]
    matrix = [m for m in cfg["matrix"] if m["harness"] not in excluded]
    if dropped:
        print(f"ignored {len(dropped)} matrix entries for non-LiteLLM harnesses: {sorted(excluded)}")
    unknown = {m["harness"] for m in matrix} - set(harnesses)
    if unknown:
        sys.exit(f"matrix uses undefined harnesses: {sorted(unknown)}")
    return {
        **cfg,
        "harnesses": harnesses,
        "matrix": matrix,
        "output_dir": cfg.get("output_dir", "results-litellm"),
        "cost_source": "litellm",
    }

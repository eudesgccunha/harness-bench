#!/usr/bin/env python3
"""harness-bench (LiteLLM only): compare harnesses that bill through LiteLLM.

Same engine as harness_bench.py, with these differences:
- Cursor (and any harness with parser "cursor") is removed from the matrix.
- Cost always comes from LiteLLM spend logs (real spend, matched by per-run tag).
- After the run it waits for LiteLLM to write logs, then reconciles and reports.
- Results go to results-litellm/ by default, separate from the full benchmark.

Usage:
  python3 harness_bench_litellm.py run       --config config.litellm.json [--reps 3] [--only pi codex] [--settle 60]
  python3 harness_bench_litellm.py reconcile --config config.litellm.json
  python3 harness_bench_litellm.py report    --config config.litellm.json

This is a shim over `python3 -m hbench <command> --litellm ...`.
"""

import json
import sys
from pathlib import Path
from typing import Any

if sys.version_info < (3, 12):
    sys.exit("Python 3.12+ required")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hbench import cli  # noqa: E402
from hbench.profiles import apply_litellm_profile  # noqa: E402

LITELLM_COMMANDS = {"run", "report", "reconcile", "plan", "run-trial", "finish"}
EPILOG = (
    "The LiteLLM profile is applied automatically to run, report, reconcile, plan, run-trial and finish "
    "(same as passing --litellm)."
)


def load_config(path: str | Path) -> tuple[dict[str, Any], Path]:
    """The config with the LiteLLM profile applied, and its directory."""
    cfg_path = Path(path).resolve()
    return apply_litellm_profile(json.loads(cfg_path.read_text())), cfg_path.parent


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in LITELLM_COMMANDS:
        argv = [argv[0], "--litellm", *argv[1:]]
    cli.main(argv, epilog=EPILOG)


if __name__ == "__main__":
    main()

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
"""

import argparse
import json
import sys
import time
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit("Python 3.12+ required")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_bench as hb  # noqa: E402

EXCLUDED_PARSERS = {"cursor"}


def load_config(path):
    cfg_path = Path(path).resolve()
    cfg = json.loads(cfg_path.read_text())
    if not cfg.get("litellm"):
        sys.exit("config needs a 'litellm' section (base_url, api_key)")
    excluded = {name for name, h in cfg["harnesses"].items() if h.get("parser") in EXCLUDED_PARSERS}
    cfg["harnesses"] = {k: v for k, v in cfg["harnesses"].items() if k not in excluded}
    dropped = [m for m in cfg["matrix"] if m["harness"] in excluded]
    cfg["matrix"] = [m for m in cfg["matrix"] if m["harness"] not in excluded]
    if dropped:
        print(f"ignored {len(dropped)} matrix entries for non-LiteLLM harnesses: {sorted(excluded)}")
    unknown = {m["harness"] for m in cfg["matrix"]} - set(cfg["harnesses"])
    if unknown:
        sys.exit(f"matrix uses undefined harnesses: {sorted(unknown)}")
    cfg.setdefault("output_dir", "results-litellm")
    cfg["cost_source"] = "litellm"
    return cfg, cfg_path.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--reps", type=int)
    r.add_argument("--only", nargs="*", help="only these harness names")
    r.add_argument("--keep", action="store_true", help="keep work directories")
    r.add_argument("--dry-run", action="store_true", help="print commands only")
    r.add_argument(
        "--allow-unsandboxed", action="store_true", help="run without sandbox.prefix (only inside a disposable VM)"
    )
    r.add_argument(
        "--settle", type=int, default=60, help="seconds to wait for LiteLLM to write spend logs before reconcile"
    )
    for name in ("reconcile", "report"):
        s = sub.add_parser(name)
        s.add_argument("--config", required=True)
    args = ap.parse_args()
    args.cost_source = "litellm"
    args.no_report = True  # report only after reconcile, when LiteLLM cost exists

    cfg, base = load_config(args.config)
    if args.cmd == "run":
        hb.cmd_run(args, cfg, base)
        if args.dry_run:
            return
        print(f"\nwaiting {args.settle}s for LiteLLM spend logs...", flush=True)
        time.sleep(args.settle)
        try:
            hb.cmd_reconcile(args, cfg, base)
        except Exception as e:  # noqa: BLE001
            print(f"reconcile failed: {e}\nRun later: python3 {Path(__file__).name} reconcile --config {args.config}")
    elif args.cmd == "reconcile":
        hb.cmd_reconcile(args, cfg, base)
    else:
        hb.cmd_report(args, cfg, base)


if __name__ == "__main__":
    main()

"""The single command line: `python3 -m hbench {run,report,reconcile,schema export}`.

`harness_bench.py` and `harness_bench_litellm.py` are shims over `main`. `--litellm` applies the LiteLLM
profile (see `hbench.profiles`) and, for `run`, waits for the spend logs and reconciles afterwards.
"""

import argparse
import importlib
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from hbench.core.schema import schema_document
from hbench.profiles import apply_litellm_profile

_DESCRIPTION = (
    "harness-bench: compare coding-agent harness x model pairs. Main metric: cost per solved task.\n\n"
    "  python3 -m hbench run       --config config.json [--reps 3] [--only HARNESS] [--keep] [--dry-run]\n"
    "  python3 -m hbench reconcile --config config.json   # pull real spend from LiteLLM\n"
    "  python3 -m hbench report    --config config.json [--drop-saturated]\n"
    "  python3 -m hbench schema export [--out PATH]       # JSON Schema of trials.jsonl and run.json\n\n"
    "Add --litellm to run, report or reconcile for the LiteLLM-only benchmark."
)


def _add_config(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", required=True)
    p.add_argument(
        "--litellm",
        action="store_true",
        help="LiteLLM-only profile: drop harnesses without LiteLLM spend, cost always from LiteLLM",
    )


def _add_drop_saturated(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--drop-saturated",
        action="store_true",
        help="leave out tasks every non-null run solved before building the report",
    )


def _prog() -> str:
    """The name the user typed: the script for the shims, `python3 -m hbench` for the module."""
    name = Path(sys.argv[0]).name
    return "python3 -m hbench" if name == "__main__.py" else name


def build_parser(epilog: str | None = None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=_prog(),
        description=_DESCRIPTION,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the matrix and write results")
    _add_config(run)
    _add_drop_saturated(run)
    run.add_argument("--reps", type=int)
    run.add_argument("--only", nargs="*", help="only these harness names")
    run.add_argument("--keep", action="store_true", help="keep work directories")
    run.add_argument("--dry-run", action="store_true", help="print commands only")
    run.add_argument(
        "--allow-unsandboxed", action="store_true", help="run without sandbox.prefix (only inside a disposable VM)"
    )
    run.add_argument(
        "--settle",
        type=int,
        default=60,
        help="with --litellm: seconds to wait for LiteLLM to write spend logs before reconcile",
    )
    report = sub.add_parser("report", help="write summary.md and summary.csv from the results")
    _add_config(report)
    _add_drop_saturated(report)
    report.add_argument("--cost-source", choices=["auto", "litellm", "computed", "reported"])
    reconcile = sub.add_parser("reconcile", help="fetch real spend per run from LiteLLM")
    _add_config(reconcile)
    _add_drop_saturated(reconcile)
    schema = sub.add_parser("schema", help="JSON Schema of the data contract")
    schema_sub = schema.add_subparsers(dest="schema_cmd", required=True)
    export = schema_sub.add_parser("export", help="print the schema document, or write it with --out")
    export.add_argument("--out", help="write to this file instead of stdout")
    return ap


def _engine() -> ModuleType:
    """The engine module. Imported here, not at module level, because the engine imports this package."""
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("harness_bench")


def _schema_export(out: str | None) -> None:
    text = json.dumps(schema_document(), indent=2, sort_keys=True)
    if out:
        Path(out).write_text(text + "\n")
    else:
        print(text)


def _run_litellm(engine: ModuleType, args: argparse.Namespace, cfg: dict[str, Any], base: Path) -> None:
    """`run` under the LiteLLM profile: report only after reconcile, when the gateway cost exists."""
    engine.cmd_run(args, cfg, base)
    if args.dry_run:
        return
    print(f"\nwaiting {args.settle}s for LiteLLM spend logs...", flush=True)
    time.sleep(args.settle)
    try:
        engine.cmd_reconcile(args, cfg, base)
    except Exception as e:  # noqa: BLE001
        print(f"reconcile failed: {e}\nRun later: python3 -m hbench reconcile --litellm --config {args.config}")


def main(argv: Sequence[str] | None = None, *, epilog: str | None = None) -> None:
    parser = build_parser(epilog)
    args = parser.parse_args(argv)
    if args.cmd == "schema":
        _schema_export(args.out)
        return
    engine = _engine()
    cfg_path = Path(args.config).resolve()
    cfg: dict[str, Any] = json.loads(cfg_path.read_text())
    base = cfg_path.parent
    if args.litellm:
        if getattr(args, "cost_source", None):
            parser.error("--cost-source cannot be combined with --litellm (the cost always comes from LiteLLM)")
        cfg = apply_litellm_profile(cfg)  # sets cfg["cost_source"] = "litellm"
        args.no_report = True
        if args.cmd == "run":
            _run_litellm(engine, args, cfg, base)
            return
    commands = {"run": engine.cmd_run, "report": engine.cmd_report, "reconcile": engine.cmd_reconcile}
    commands[args.cmd](args, cfg, base)

"""The single command line: `python3 -m hbench {run,report,reconcile,schema export,suite list,suite validate}`.

`harness_bench.py` and `harness_bench_litellm.py` are shims over `main`. `--litellm` applies the LiteLLM
profile (see `hbench.profiles`) and, for `run`, waits for the spend logs and reconciles afterwards.
"""

import argparse
import importlib
import json
import shutil
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from hbench.core.schema import schema_document
from hbench.profiles import apply_litellm_profile
from hbench.suites import Suite, SuiteError, load_suite, suite_label, suite_tasks

_DESCRIPTION = (
    "harness-bench: compare coding-agent harness x model pairs. Main metric: cost per solved task.\n\n"
    "  python3 -m hbench run       --config config.json [--reps 3] [--only HARNESS] [--keep] [--dry-run]\n"
    "  python3 -m hbench reconcile --config config.json   # pull real spend from LiteLLM\n"
    "  python3 -m hbench report    --config config.json [--drop-saturated]\n"
    "  python3 -m hbench schema export [--out PATH]       # JSON Schema of trials.jsonl and run.json\n"
    "  python3 -m hbench suite list     [--config C | PATH...]\n"
    "  python3 -m hbench suite validate [--config C] [--run [--allow-unsandboxed]] [PATH...]\n\n"
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
    suite = sub.add_parser("suite", help="list and validate suites of tasks")
    suite_sub = suite.add_subparsers(dest="suite_cmd", required=True)
    suite_list = suite_sub.add_parser("list", help="name, version, kind, task count and tags of each suite")
    suite_list.add_argument("--config", help="list the suites named in this config")
    suite_list.add_argument("paths", nargs="*", metavar="PATH", help="suite directories")
    validate = suite_sub.add_parser("validate", help="check suites; with --run also run each task against its solution")
    validate.add_argument(
        "--config", help="validate the suites named in this config; its sandbox and env settings apply"
    )
    validate.add_argument(
        "--run", action="store_true", help="the graders must fail on the repo and pass with the solution"
    )
    validate.add_argument(
        "--allow-unsandboxed", action="store_true", help="with --run: no sandbox.prefix (only inside a disposable VM)"
    )
    validate.add_argument("paths", nargs="*", metavar="PATH", help="suite directories")
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


def _suite_inputs(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[list[Path], dict[str, Any], Path]:
    """The suite directories to act on, the config (empty without `--config`) and the directory paths resolve from."""
    cfg: dict[str, Any] = {}
    base = Path.cwd()
    if args.config:
        cfg_path = Path(args.config).resolve()
        cfg, base = json.loads(cfg_path.read_text()), cfg_path.parent
    dirs = [Path(p) for p in args.paths] or [base / s for s in cfg.get("suites", [])]
    if not dirs:
        parser.error("give suite directories or --config with a 'suites' list")
    return dirs, cfg, base


def _suite_list(dirs: list[Path]) -> None:
    for path in dirs:
        try:
            suite = load_suite(path)
        except SuiteError as e:
            sys.exit(str(e))
        count = len(suite.tasks)
        tags = ",".join(sorted({tag for task in suite.tasks for tag in task.tags})) or "-"
        print(f"{suite.name}  {suite.version}  {suite.kind}  {count} {'task' if count == 1 else 'tasks'}  tags: {tags}")


def _suite_validate(dirs: list[Path], cfg: dict[str, Any], base: Path, run: bool) -> bool:
    """Static checks of every suite (the tasks must also pass the engine's config checks), then, with `run`, the
    behavioral check of every task. Prints one line per suite or task; returns whether everything is ok."""
    engine = _engine()
    ok = True
    valid: list[Suite] = []
    for path in dirs:
        try:
            suite = load_suite(path)
            engine.validate_config({**cfg, "harnesses": cfg.get("harnesses", {}), "tasks": suite_tasks(suite)})
        except SuiteError as e:
            print(f"FAIL  {e}")
            ok = False
        except SystemExit as e:  # `validate_config` exits with a message
            print(f"FAIL  {path}: {e.code}")
            ok = False
        else:
            count = len(suite.tasks)
            print(f"ok    {suite_label(suite)} ({count} {'task' if count == 1 else 'tasks'})")
            valid.append(suite)
    if run and ok:
        ok = _suite_check_behavior(engine, valid, cfg, base)
    return ok


def _suite_check_behavior(engine: ModuleType, suites: list[Suite], cfg: dict[str, Any], base: Path) -> bool:
    work_root = engine.resolve_work_root(cfg, base)
    ok = True
    try:
        for suite in suites:
            for task in suite_tasks(suite):
                if "solution" not in task:
                    print(f"{task['id']}: no solution")
                    ok = False
                    continue
                check = engine.validate_task_behavior(cfg, task, base, work_root)
                print(f"{check.task_id}: {check.status}")
                ok = ok and check.ok
    finally:
        if not cfg.get("work_dir"):
            shutil.rmtree(work_root, ignore_errors=True)
    return ok


def _suite(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    dirs, cfg, base = _suite_inputs(parser, args)
    if args.suite_cmd == "list":
        _suite_list(dirs)
        return
    if args.run:
        _engine().require_sandbox(cfg, args.allow_unsandboxed)
    if not _suite_validate(dirs, cfg, base, args.run):
        sys.exit(1)


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
    if args.cmd == "suite":
        _suite(parser, args)
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

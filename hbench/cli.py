"""The single command line: `python3 -m hbench {run,report,reconcile,compare,recommend,gate,schema export,suite ...}`.

`harness_bench.py` and `harness_bench_litellm.py` are shims over `main`. `--litellm` applies the LiteLLM
profile (see `hbench.profiles`) and, for `run`, waits for the spend logs and reconciles afterwards.
"""

import argparse
import contextlib
import importlib
import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from hbench import api, decide
from hbench.calibration import calibrate, calibration_status, read_labels, write_calibration
from hbench.core.models import RunInfo, TrialResult
from hbench.core.schema import schema_document
from hbench.doctor import run_checks
from hbench.importers import ImportRequest, run_import
from hbench.judge import JudgeError
from hbench.profiles import apply_litellm_profile
from hbench.report import COST_SOURCES
from hbench.results import ResumeError, read_run
from hbench.sinks.feed import SinkFeed
from hbench.suites import Suite, SuiteError, load_suite, suite_label, suite_tasks

MAX_SPEC_BYTES = 64 * 1024  # a spec is a few hundred bytes; a bigger input is not one

_DESCRIPTION = (
    "harness-bench: compare coding-agent harness x model pairs. Main metric: cost per solved task.\n\n"
    "  python3 -m hbench run       --config config.json [--reps 3] [--only HARNESS] [--keep] [--dry-run]\n"
    "  python3 -m hbench run       --config config.json --resume RUN_ID [--force]   # continue an interrupted run\n"
    "  python3 -m hbench reconcile --config config.json   # pull real spend from LiteLLM\n"
    "  python3 -m hbench report    --config config.json [--drop-saturated]\n"
    "  python3 -m hbench schema export [--out PATH]       # JSON Schema of trials.jsonl and run.json\n"
    "  python3 -m hbench suite list     [--config C | PATH...]\n"
    "  python3 -m hbench suite validate [--config C] [--run [--allow-unsandboxed]] [PATH...]\n"
    "  python3 -m hbench suite import exercism --source DIR --out SUITE_DIR --name N --version V --license L\n"
    "                              --source-url URL [--exercise SLUG ...]   # a local clone in, a new suite out\n"
    "  python3 -m hbench judge calibrate --config C --labels LABELS.jsonl --out calibration.json\n"
    "  python3 -m hbench export    --config C --run RUN_DIR   # replay a run into the configured sinks\n"
    "  python3 -m hbench doctor    --config C [--network]   # check env, CLIs, image and network; spends nothing\n"
    "  python3 -m hbench plan      --config C [--only H...] [--reps N] [--run RUN_ID]   # a TrialSpec per line\n"
    "  python3 -m hbench run-trial --config C [--allow-unsandboxed] --spec FILE|-   # run one spec, print the result\n"
    "  python3 -m hbench finish    --config C --run RUN_ID [--no-report]   # close the run, write the report\n"
    "  python3 -m hbench compare   RUN_A RUN_B [--alpha 0.05] [--json] [--fail-on-regression]   # run directories\n"
    "  python3 -m hbench recommend RUN_DIR [--min-pass-rate P] [--max-cost-per-solved USD] [--write-target FILE]\n"
    "  python3 -m hbench gate      RUN_DIR --min-pass-rate P (--target FILE | --harness H --model M)   # CI gate\n\n"
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


def _add_cost_source(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cost-source", choices=COST_SOURCES, help="which cost to use (default auto)")
    p.add_argument("--litellm", action="store_true", help="use the LiteLLM (gateway) cost, as the LiteLLM report does")


def _add_decision_commands(sub: Any) -> None:
    """`compare`, `recommend` and `gate` (ADR 0013): they read run directories and need no config."""
    compare = sub.add_parser("compare", help="compare two runs: ranking diff and paired regressions")
    compare.add_argument("run_a", metavar="RUN_A", help="the baseline run directory: <output_dir>/<run_id>")
    compare.add_argument("run_b", metavar="RUN_B", help="the run directory to check against the baseline")
    _add_cost_source(compare)
    compare.add_argument("--alpha", type=float, default=0.05, help="significance of the McNemar test (default 0.05)")
    compare.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    compare.add_argument(
        "--fail-on-regression", action="store_true", help="exit 1 when a target regressed (default: exit 0)"
    )
    recommend = sub.add_parser("recommend", help="pick the cheapest target per solved task under constraints")
    recommend.add_argument("run_dir", metavar="RUN_DIR")
    _add_cost_source(recommend)
    recommend.add_argument("--min-pass-rate", type=float, metavar="P", help="from 0 to 1")
    recommend.add_argument("--max-cost-per-solved", type=float, metavar="USD")
    recommend.add_argument("--max-cost-per-run", type=float, metavar="USD")
    recommend.add_argument("--max-latency-s", type=float, metavar="S", help="average wall time per run")
    recommend.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    recommend.add_argument("--write-target", metavar="FILE", help="write the pick as a target file (JSON)")
    gate = sub.add_parser("gate", help="CI gate: exit 1 when the target's pass rate on a suite is too low")
    gate.add_argument("run_dir", metavar="RUN_DIR")
    gate.add_argument("--min-pass-rate", type=float, required=True, metavar="P", help="from 0 to 1")
    gate.add_argument("--suite", default="regression", help="suite name whose tasks are gated (default regression)")
    gate.add_argument("--target", metavar="FILE", help="a target file written by `recommend --write-target`")
    gate.add_argument("--harness")
    gate.add_argument("--model")


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
        "--allow-unsandboxed", action="store_true", help="run without a sandbox (only inside a disposable VM)"
    )
    run.add_argument(
        "--resume",
        metavar="RUN_ID",
        help="continue the run RUN_ID (a directory of the output dir): trials already in its trials.jsonl are skipped",
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="with --resume: resume although the run's heartbeat says it may still be running",
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
        "--allow-unsandboxed", action="store_true", help="with --run: no sandbox (only inside a disposable VM)"
    )
    validate.add_argument("paths", nargs="*", metavar="PATH", help="suite directories")
    import_cmd = suite_sub.add_parser(
        "import", help="turn a local checkout of a task pack into a new suite (no network; review its license first)"
    )
    import_cmd.add_argument("importer", help="exercism, or the name of an importer plugin")
    import_cmd.add_argument("--source", required=True, metavar="DIR", help="your local clone of the pack")
    import_cmd.add_argument("--out", required=True, metavar="SUITE_DIR", help="a new directory; it must not exist")
    import_cmd.add_argument("--name", required=True, help="suite name (a-z, 0-9 and -)")
    import_cmd.add_argument("--version", required=True, metavar="MAJOR.MINOR.PATCH")
    import_cmd.add_argument("--license", required=True, metavar="SPDX", help="license of the pack, for provenance")
    import_cmd.add_argument("--source-url", required=True, metavar="URL", help="https:// URL of the pack's repository")
    import_cmd.add_argument("--exercise", nargs="+", default=[], metavar="SLUG", help="only these exercises")
    doctor = sub.add_parser("doctor", help="check that the benchmark can run; starts no agent and spends nothing")
    doctor.add_argument("--config", required=True)
    doctor.add_argument(
        "--network",
        action="store_true",
        help="also GET the LiteLLM health endpoint and one page of spend logs (the spend key's permission)",
    )
    export_cmd = sub.add_parser("export", help="replay a run into the configured sinks (idempotent)")
    export_cmd.add_argument("--config", required=True)
    export_cmd.add_argument("--run", required=True, metavar="RUN_DIR", help="the run directory: <output_dir>/<run_id>")
    plan_cmd = sub.add_parser("plan", help="start a run (or reuse --run) and print one TrialSpec per line")
    _add_config(plan_cmd)
    plan_cmd.add_argument("--reps", type=int)
    plan_cmd.add_argument("--only", nargs="*", help="only these harness names")
    plan_cmd.add_argument("--run", metavar="RUN_ID", help="plan this run (started by an earlier plan) again")
    trial_cmd = sub.add_parser("run-trial", help="run the trial a TrialSpec names and print its result as JSON")
    _add_config(trial_cmd)
    trial_cmd.add_argument("--spec", required=True, metavar="FILE", help="the spec: a file, or - for stdin")
    trial_cmd.add_argument(
        "--allow-unsandboxed", action="store_true", help="run without a sandbox (only inside a disposable VM)"
    )
    finish_cmd = sub.add_parser("finish", help="mark a planned run finished, close its sinks and write the report")
    _add_config(finish_cmd)
    finish_cmd.add_argument("--run", required=True, metavar="RUN_ID")
    finish_cmd.add_argument("--no-report", action="store_true", help="do not write summary.md and summary.csv")
    _add_decision_commands(sub)
    judge = sub.add_parser("judge", help="LLM judge tools")
    judge_sub = judge.add_subparsers(dest="judge_cmd", required=True)
    calibrate_cmd = judge_sub.add_parser(
        "calibrate", help="compare the configured judge with human scores (agreement, error, kappa)"
    )
    calibrate_cmd.add_argument("--config", required=True, help="config with a 'judge' section")
    calibrate_cmd.add_argument(
        "--labels",
        required=True,
        help=(
            "JSONL: artifact, rubric (paths) and human_score. Trusted input: the files it names are read "
            "and sent to the judge"
        ),
    )
    calibrate_cmd.add_argument("--out", required=True, help="write the calibration result to this file")
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


def _suite_import(args: argparse.Namespace) -> None:
    """`suite import`: write a new suite from a local checkout; nothing is left behind when it fails."""
    request = ImportRequest(
        source=Path(args.source),
        out=Path(args.out),
        name=args.name,
        version=args.version,
        license=args.license,
        source_url=args.source_url,
        exercises=tuple(args.exercise),
    )
    try:
        suite = run_import(args.importer, request)
    except ValueError as e:  # ImporterError and PluginError
        sys.exit(f"suite import: {e}")
    count = len(suite.tasks)
    print(f"imported {count} {'task' if count == 1 else 'tasks'} into {request.out}")
    print(f"next: python3 -m hbench suite validate --run --allow-unsandboxed {request.out}")


def _suite(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.suite_cmd == "import":
        _suite_import(args)
        return
    dirs, cfg, base = _suite_inputs(parser, args)
    if args.suite_cmd == "list":
        _suite_list(dirs)
        return
    if args.run:
        _engine().require_sandbox(cfg, args.allow_unsandboxed)
    if not _suite_validate(dirs, cfg, base, args.run):
        sys.exit(1)


def _judge_calibrate(args: argparse.Namespace) -> None:
    """`judge calibrate`: judge every label with the configured judge and write the calibration file."""
    engine = _engine()
    cfg = json.loads(Path(args.config).resolve().read_text())
    judge = engine.calibration_judge(cfg)
    try:
        result = calibrate(judge, read_labels(Path(args.labels)))
    except (JudgeError, ValueError) as e:
        sys.exit(f"judge calibrate: {e}")
    write_calibration(Path(args.out), result)
    reason = calibration_status(result, result.rubric_id, result.rubric_version, result.judge)
    print(
        f"{result.rubric_id}@{result.rubric_version} judged by {result.judge}: n={result.n} "
        f"mean absolute error {result.mean_absolute_error:.3f}, agreement {result.agreement:.2f}, "
        f"kappa {result.kappa:.2f}"
    )
    print(f"{'calibrated' if reason is None else f'experimental: {reason}'}; saved {args.out}")


def _doctor(args: argparse.Namespace) -> None:
    """`doctor`: print one line per check; exit 1 when any check failed."""
    engine = _engine()
    cfg_path = Path(args.config).resolve()
    checks = run_checks(json.loads(cfg_path.read_text()), cfg_path.parent, args.network, engine)
    for check in checks:
        status = "warn " if check.warning else "ok   " if check.ok else "FAIL "
        print(f"{status} {check.name}: {check.detail}")
    failed = sum(not c.ok for c in checks)
    warned = sum(c.warning for c in checks)
    print(f"{len(checks)} checks, {failed} failed, {warned} warnings")
    if failed:
        sys.exit(1)


def _export(args: argparse.Namespace) -> None:
    """`export`: send `run.json` and `trials.jsonl` of a finished or interrupted run to the sinks of the config."""
    engine = _engine()
    cfg_path = Path(args.config).resolve()
    try:
        cfg: dict[str, Any] = json.loads(cfg_path.read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"export: cannot read the config {cfg_path}: {e}")
    run_dir = Path(args.run).resolve()
    try:
        info, trials = read_run(run_dir)
    except ResumeError as e:
        sys.exit(f"export: {e}")
    if run_dir.name != info.run_id:
        sys.exit(f"export: {run_dir} is not the directory of run {info.run_id}")
    # The trace file of the otlp sink lives in the run directory, wherever the config says output goes.
    sinks = engine.make_sinks(dict(cfg, output_dir=str(run_dir.parent)), cfg_path.parent)
    if not sinks:
        sys.exit("export: the config has no sinks")
    secrets = [os.environ.get(v, "") for v in engine.spend_key_vars(cfg)]
    feed = SinkFeed(sinks, lambda message: print(engine.redact(message, secrets), file=sys.stderr))
    feed.open(info)
    for trial in trials:
        feed.write(trial)
    feed.close(info)
    print(f"exported {len(trials)} trial(s) of run {info.run_id} to {len(sinks) - feed.failed} of {len(sinks)} sink(s)")
    if feed.failed:
        sys.exit(1)


def _load_config(args: argparse.Namespace) -> api.Config:
    """The config of the command. Its warnings go to stderr: stdout of `plan` and `run-trial` is data."""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            return api.load_config(args.config, litellm=args.litellm)
    except api.ApiError as e:
        sys.exit(f"{args.cmd}: {e}")


def _plan(args: argparse.Namespace) -> None:
    """`plan`: start a run, or reuse the run `--run` names, and print one `TrialSpec` per line."""
    config = _load_config(args)
    try:
        run = api.open_run(config, args.run) if args.run else api.start_run(config, only=args.only, reps=args.reps)
        specs = api.plan(config, run, only=args.only, reps=args.reps)
    except api.ApiError as e:
        sys.exit(f"plan: {e}")
    for spec in specs:
        print(json.dumps(spec.to_dict()))


def _run_trial(args: argparse.Namespace) -> None:
    """`run-trial`: run one spec. The engine's progress goes to stderr, so stdout holds only the result."""
    config = _load_config(args)
    try:
        if args.spec == "-":
            raw = sys.stdin.buffer.read(MAX_SPEC_BYTES + 1)
        else:
            with Path(args.spec).open("rb") as f:
                raw = f.read(MAX_SPEC_BYTES + 1)
    except OSError as e:
        sys.exit(f"run-trial: cannot read the spec {args.spec}: {e}")
    if len(raw) > MAX_SPEC_BYTES:
        sys.exit(
            f"run-trial: the spec is larger than {MAX_SPEC_BYTES // 1024} KiB: a spec names a trial and nothing else"
        )
    try:
        text = raw.decode()
    except UnicodeDecodeError:
        sys.exit("run-trial: the spec is not UTF-8 text")
    try:
        spec = api.parse_spec(text)
        with contextlib.redirect_stdout(sys.stderr):
            result = api.run_trial(config, spec, allow_unsandboxed=args.allow_unsandboxed)
    except api.ApiError as e:
        sys.exit(f"run-trial: {e}")
    print(json.dumps(result.to_dict()))


def _finish(args: argparse.Namespace) -> None:
    """`finish`: close the run. Under `--litellm` the report waits for `reconcile`, as it does for `run`."""
    config = _load_config(args)
    try:
        info = api.finish_run(config, args.run, report=not (args.no_report or args.litellm))
    except api.ApiError as e:
        sys.exit(f"finish: {e}")
    print(f"finished run {info.run_id} at {info.finished_at}")
    if args.litellm:
        print(f"reconcile, then report: python3 -m hbench reconcile --litellm --config {args.config}")


def _read_run_or_exit(command: str, path: str) -> tuple[RunInfo, list[TrialResult]]:
    try:
        return read_run(Path(path).resolve())
    except ResumeError as e:
        sys.exit(f"{command}: {e}")


def _cost_source(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """The cost source of `--cost-source` / `--litellm`, which exclude each other."""
    if args.litellm:
        if args.cost_source:
            parser.error("--cost-source cannot be combined with --litellm (the cost always comes from LiteLLM)")
        return "litellm"
    return str(args.cost_source or "auto")


def _compare(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """`compare`: print the comparison; exit 1 on a regression only with `--fail-on-regression`."""
    source = _cost_source(parser, args)
    runs = [_read_run_or_exit("compare", path) for path in (args.run_a, args.run_b)]
    try:
        comparison = decide.compare(runs[0], runs[1], source, args.alpha)
    except ValueError as e:
        sys.exit(f"compare: {e}")
    if args.json:
        print(json.dumps(comparison.to_dict(), indent=2, sort_keys=True, allow_nan=False))
    else:
        print(decide.render_comparison(comparison), end="")
    if args.fail_on_regression and comparison.has_regression:
        sys.exit(1)


def _recommend(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """`recommend`: print every target with the constraints it failed and the pick; exit 1 without an eligible one."""
    source = _cost_source(parser, args)
    info, trials = _read_run_or_exit("recommend", args.run_dir)
    try:
        constraints = decide.Constraints(
            min_pass_rate=args.min_pass_rate,
            max_cost_per_solved=args.max_cost_per_solved,
            max_cost_per_run=args.max_cost_per_run,
            max_latency_s=args.max_latency_s,
        )
        recommendation = decide.recommend(info, trials, constraints, source)
        output = (
            json.dumps(decide.recommendation_json(recommendation), indent=2, sort_keys=True, allow_nan=False)
            if args.json
            else decide.render_recommendation(recommendation).rstrip("\n")
        )
        # A run.json of another tool may carry a run_id the decision file refuses: say so before printing or writing.
        decision = decide.decision_for(recommendation) if recommendation.pick and args.write_target else None
    except ValueError as e:  # ContractError too
        sys.exit(f"recommend: {e}")
    print(output)
    if recommendation.pick is None:
        sys.exit("recommend: no eligible target")
    if decision is not None:
        try:
            decide.write_target(Path(args.write_target), decision)
        except decide.TargetFileError as e:
            sys.exit(f"recommend: {e}")
        print(f"wrote {args.write_target}", file=sys.stderr)


def _gate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """`gate`: exit 1 (with the reason on stderr) unless the target meets the pass rate; it fails closed."""
    if args.target:
        if args.harness or args.model:
            parser.error("give --target or --harness and --model, not both")
        try:
            decision = decide.load_target(Path(args.target))
        except decide.TargetFileError as e:
            sys.exit(f"gate: {e}")
        harness, model = decision.harness, decision.model
    elif args.harness and args.model:
        harness, model = args.harness, args.model
    else:
        parser.error("give --target, or both --harness and --model")
    info, trials = _read_run_or_exit("gate", args.run_dir)
    try:
        result = decide.gate(info, trials, harness, model, args.suite, args.min_pass_rate)
    except ValueError as e:
        sys.exit(f"gate: {e}")
    if not result.ok:
        sys.exit(f"gate: {result.message}")
    print(f"gate: {result.message}")


_DECISION_COMMANDS: Mapping[str, Callable[[argparse.ArgumentParser, argparse.Namespace], None]] = {
    "compare": _compare,
    "recommend": _recommend,
    "gate": _gate,
}


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


_ORCHESTRATOR_COMMANDS: Mapping[str, Callable[[argparse.Namespace], None]] = {
    "plan": _plan,
    "run-trial": _run_trial,
    "finish": _finish,
}


def main(argv: Sequence[str] | None = None, *, epilog: str | None = None) -> None:
    parser = build_parser(epilog)
    args = parser.parse_args(argv)
    if args.cmd == "schema":
        _schema_export(args.out)
        return
    if args.cmd == "suite":
        _suite(parser, args)
        return
    if args.cmd == "judge":
        _judge_calibrate(args)
        return
    if args.cmd == "export":
        _export(args)
        return
    if args.cmd == "doctor":
        _doctor(args)
        return
    if args.cmd in _DECISION_COMMANDS:
        _DECISION_COMMANDS[args.cmd](parser, args)
        return
    if args.cmd in _ORCHESTRATOR_COMMANDS:
        _ORCHESTRATOR_COMMANDS[args.cmd](args)
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

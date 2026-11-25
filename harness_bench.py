#!/usr/bin/env python3
"""harness-bench: compare coding-agent harness x model pairs.

Metrics per pair: pass rate, tokens, cost per run, cost per solved task,
cost per 1M tokens. Python 3.12+, standard library only.

Usage:
  python3 harness_bench.py run    --config config.json [--reps 3] [--only HARNESS] [--keep] [--dry-run]
  python3 harness_bench.py reconcile --config config.json   # pull real spend from LiteLLM
  python3 harness_bench.py report --config config.json
"""

import argparse
import contextlib
import functools
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if sys.version_info < (3, 12):
    sys.exit("Python 3.12+ required")

from hbench import cli, report  # noqa: E402
from hbench import runners as _runners  # noqa: E402
from hbench.calibration import Calibration, read_calibration  # noqa: E402
from hbench.core.convert import row_from_trial, trial_from_row_lenient, trials_from_rows  # noqa: E402
from hbench.core.ids import new_run_id, sha256_json, trial_id  # noqa: E402
from hbench.core.models import GraderResult, RunInfo, Target  # noqa: E402
from hbench.graders import GRADERS as GRADERS, Change, GradeContext, GradePlan, compose, grade_plan  # noqa: E402
from hbench.results import HEARTBEAT_FILE, Heartbeat, ResumeError, RunWriter, apply_gateway_costs  # noqa: E402
from hbench.results import heartbeat_is_live, read_heartbeat, read_trials  # noqa: E402
from hbench.runners import PARSERS as PARSERS, empty_usage as empty_usage, json_lines as json_lines  # noqa: E402
from hbench.runners import num as num, runner_for  # noqa: E402
from hbench.graders.base import LogStep, RunStep, discard_log  # noqa: E402
from hbench.judge import GatewayJudge, Judge, JudgeError  # noqa: E402
from hbench.net import NoRedirect, url_allowed  # noqa: E402
from hbench.plugins import PluginError, redactor_for  # noqa: E402
from hbench.runners.base import HarnessRunner, ParseUsage, build_argv  # noqa: E402
from hbench.sinks import sink_secret_vars, sinks_for  # noqa: E402
from hbench.sinks.base import Sink  # noqa: E402
from hbench.sinks.feed import SinkFeed  # noqa: E402
from hbench.sandboxes import Sandbox, SandboxError, SandboxSession, sandbox_for  # noqa: E402
from hbench.safe_copy import safe_copy_into  # noqa: E402
from hbench.suites import SUITE_ONLY_KEYS, SUITE_PATH_KEYS, Suite, SuiteError, load_suite, suite_tasks  # noqa: E402

VERSION = "0.0.0"

# The parsers live in `hbench.runners`. They are re-exported under the legacy names.
parse_claude: ParseUsage = _runners.parse_claude
parse_opencode: ParseUsage = _runners.parse_opencode
parse_pi: ParseUsage = _runners.parse_pi
parse_cursor: ParseUsage = _runners.parse_cursor
parse_codex: ParseUsage = _runners.parse_codex
parse_null: ParseUsage = _runners.parse_null


# ---------------------------------------------------------------- helpers


def expand_env(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


# ---------------------------------------------------------------- cost


def computed_cost(u: Mapping[str, Any], price: Mapping[str, Any] | None) -> float | None:
    """Cost from token counts and a USD per 1M tokens price entry."""
    if not price or not any(v for k, v in price.items() if not k.startswith("_")):
        return None  # missing or all-zero price entry: fall back to reported cost
    out_price = price.get("output", 0)
    cost: float = (
        u["input"] * price.get("input", 0)
        + (u["output"] + u["reasoning"]) * out_price
        + u["cache_read"] * price.get("cache_read", price.get("input", 0))
        + u["cache_write"] * price.get("cache_write", price.get("input", 0))
    ) / 1e6
    return cost


# ---------------------------------------------------------------- safety helpers

# Environment the agent and the setup/test commands get. Everything else (cloud
# credentials, tokens, SSH agent, the LiteLLM spend key) stays out unless a harness
# names it explicitly in its "env" block.
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR")
SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")
# Stale bytecode in a task source must not reach the workdir (a diff of the agent's work would show it).
PYCACHE = shutil.ignore_patterns("__pycache__")


def safe(s: object) -> str:
    """Filesystem-safe fragment: no separators, no leading dots."""
    return SAFE_NAME.sub("_", str(s)).lstrip(".") or "_"


def env_var_names(value: str | None) -> set[str]:
    return set(re.findall(r"\$\{(\w+)\}", value or ""))


def base_env(cfg: Mapping[str, Any]) -> dict[str, str]:
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    for k in cfg.get("env_passthrough", []):
        if k in os.environ:
            env[k] = os.environ[k]
    for k in spend_key_vars(cfg):
        env.pop(k, None)
    return env


def spend_key_vars(cfg: Mapping[str, Any]) -> set[str]:
    """The environment variables that hold a gateway key (LiteLLM spend key, judge key, the header values of the sinks):
    never given to an agent, always redacted."""
    judge = cfg.get("judge")
    judge_key = judge.get("api_key") if isinstance(judge, Mapping) else None
    return (
        env_var_names((cfg.get("litellm") or {}).get("api_key"))
        | env_var_names(judge_key if isinstance(judge_key, str) else None)
        | sink_secret_vars(cfg)
    )


def require_env_ref(value: object, name: str) -> None:
    """Exits unless `value` is exactly one `${VAR}` reference. A key written into the config is not in the
    environment, so the redaction of logs and the filter of the agent's environment would never know it."""
    if not isinstance(value, str) or not re.fullmatch(r"\$\{\w+\}", value):
        sys.exit(
            f"{name} must name an environment variable, written exactly ${{VAR}}; "
            "a key written in the config is never redacted"
        )


def redact(text: str, secrets: Iterable[str]) -> str:
    for s in secrets:
        if s and len(s) >= 8:
            text = text.replace(s, "***REDACTED***")
    return text


def check_cost_source(name: str) -> None:
    if name not in report.COST_SOURCES:
        sys.exit(f"invalid cost_source {name!r}: use one of {', '.join(report.COST_SOURCES)}")


def report_options(cfg: Mapping[str, Any]) -> tuple[float, float]:
    """`(recommend_within_pp, target_ci_width)` from the config, validated; exits with a message naming the key."""
    pp = cfg.get("recommend_within_pp", 5)
    width = cfg.get("target_ci_width", 0.2)
    if isinstance(pp, bool) or not isinstance(pp, int | float) or not 0 <= pp <= 100:
        sys.exit(f"invalid recommend_within_pp {pp!r}: use a number from 0 to 100")
    if isinstance(width, bool) or not isinstance(width, int | float) or not 0 < width <= 1:
        sys.exit(f"invalid target_ci_width {width!r}: use a number above 0 and up to 1")
    return pp, width


def load_config_suites(cfg: Mapping[str, Any], base: Path) -> list[Suite]:
    """The suites listed in the config's `suites` key (directories relative to the config).
    Raises `SuiteError` naming the entry and the problem."""
    entries = cfg.get("suites", [])
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        raise SuiteError("suites must be a list of suite directories")
    loaded = []
    for entry in entries:
        try:
            loaded.append(load_suite(base / entry))
        except SuiteError as e:
            raise SuiteError(f"suite {entry}: {e}") from e
    return loaded


def check_config_tasks(cfg: Mapping[str, Any]) -> None:
    """Exits when a config task carries a key only a suite task has: the engine reads `suite` to tell suite tasks
    (someone else's data, with a narrower git policy) from the user's own, so a config task must not set it."""
    for task in cfg.get("tasks", []):
        reserved = sorted(SUITE_ONLY_KEYS & task.keys())
        if reserved:
            sys.exit(f"task {task.get('id')!r}: key(s) {', '.join(reserved)} are reserved for suite tasks; remove them")


def expand_suites(cfg: Mapping[str, Any], base: Path) -> Mapping[str, Any]:
    """`cfg` with the tasks of its suites appended to `tasks`. A config without `suites` is returned as it is.
    A task id used twice (two suites of one name, or a config task with the id of a suite task) exits naming both."""
    check_config_tasks(cfg)
    try:
        suites = load_config_suites(cfg, base)
    except SuiteError as e:
        sys.exit(str(e))
    if not suites:
        return cfg
    tasks = list(cfg.get("tasks", []))
    origin = {task["id"]: "config tasks" for task in tasks}
    for suite in suites:
        for task in suite_tasks(suite):
            if task["id"] in origin:
                sys.exit(f"duplicate task id {task['id']!r}: defined in {origin[task['id']]} and in suite {suite.path}")
            origin[task["id"]] = f"suite {suite.path}"
            tasks.append(task)
    return {**cfg, "tasks": tasks}


def valid_task_id(task: Mapping[str, Any]) -> bool:
    """A config task id is one safe name. A suite task id is `<suite>/<task>`, each part a safe name."""
    task_id = str(task["id"])
    parts = task_id.split("/") if "suite" in task else [task_id]
    if "suite" in task and (len(parts) != 2 or not all(parts)):
        return False
    return not any(SAFE_NAME.search(part) or part.startswith(".") for part in parts)


JUDGE_KEYS = frozenset({"base_url", "api_key", "model", "timeout", "max_artifact_bytes", "calibration", "redactor"})
JUDGE_TIMEOUT = 120
JUDGE_MAX_ARTIFACT_BYTES = 200_000
MAX_JUDGE_REPLY_BYTES = 1_000_000
MAX_GRADER_LOG_CHARS = 100_000  # one write of a grader log, after redaction


def uses_judge(cfg: Mapping[str, Any]) -> bool:
    """Whether a task of `cfg` has an `llm_rubric` grader, the only reason the engine ever calls the judge."""
    return any(
        isinstance(grader, Mapping) and grader.get("type") == "llm_rubric"
        for task in cfg.get("tasks", [])
        for grader in (task.get("graders") if isinstance(task.get("graders"), list) else [])
    )


def _judge_text(section: Mapping[str, Any], key: str) -> str:
    """The required string `judge.<key>` with its `${VAR}` references expanded, which must not be empty."""
    value = section.get(key)
    if not isinstance(value, str) or not value:
        sys.exit(f"judge.{key} must be a non-empty string")
    expanded = expand_env(value)
    if not expanded:
        names = ", ".join(sorted(env_var_names(value))) or "the value"
        sys.exit(f"judge.{key} is empty: set the environment variable {names}")
    return expanded


def validate_judge(cfg: Mapping[str, Any]) -> None:
    """`check_judge_section`, but only for a config with an `llm_rubric` task: without one the engine never reads
    the section."""
    if uses_judge(cfg):
        check_judge_section(cfg)


def check_judge_section(cfg: Mapping[str, Any]) -> None:
    """Exits unless the `judge` section of `cfg` is complete and its URL passes the rule of `check_litellm_url`."""
    section = cfg.get("judge")
    if not isinstance(section, Mapping):
        sys.exit(
            "the config needs a 'judge' object with base_url, api_key and model (llm_rubric and judge calibrate use it)"
        )
    unknown = sorted(set(section) - JUDGE_KEYS)
    if unknown:
        sys.exit(f"judge: unknown key(s) {', '.join(unknown)}")
    check_litellm_url(_judge_text(section, "base_url"), "judge.base_url")
    require_env_ref(section.get("api_key"), "judge.api_key")
    _judge_text(section, "api_key")
    if not isinstance(section.get("model"), str) or not section["model"]:
        sys.exit("judge.model must be a non-empty string")
    timeout = section.get("timeout", JUDGE_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        sys.exit(f"judge.timeout must be a number of seconds above 0, got {timeout!r}")
    limit = section.get("max_artifact_bytes", JUDGE_MAX_ARTIFACT_BYTES)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        sys.exit(f"judge.max_artifact_bytes must be a whole number above 0, got {limit!r}")
    for key in ("calibration", "redactor"):
        if key in section and (not isinstance(section[key], str) or not section[key]):
            sys.exit(f"judge.{key} must be a non-empty string")


def validate_config(cfg: Mapping[str, Any]) -> None:
    check_cost_source(cfg.get("cost_source", "auto"))
    if cfg.get("litellm"):
        require_env_ref(cfg["litellm"].get("api_key"), "litellm.api_key")
    report_options(cfg)
    file_names: dict[str, str] = {}
    for task in cfg["tasks"]:
        if not valid_task_id(task):
            sys.exit(f"invalid task id {task['id']!r}: use only letters, digits, _ . - (no leading dot)")
        if file_names.setdefault(safe(task["id"]), task["id"]) != task["id"]:
            sys.exit(f"task ids {file_names[safe(task['id'])]!r} and {task['id']!r} would share the same file names")
        for field in ("repo", "ref"):
            if str(task.get(field, "")).startswith("-"):
                sys.exit(f"task {task['id']}: {field} must not start with '-'")
        for p in task.get("protected_paths", []):
            if Path(p).is_absolute() or ".." in Path(p).parts:
                sys.exit(f"task {task['id']}: protected path {p!r} must be relative, without '..'")
        try:
            grade_plan(task)
        except ValueError as e:
            sys.exit(f"task {task['id']}: {e}")
        if (
            task.get("hidden_tests")
            and "graders" in task
            and not any(g.get("type") == "hidden_tests" for g in task["graders"])
        ):
            print(
                f"warning: task {task['id']} sets 'hidden_tests' but its graders have no hidden_tests grader: ignored"
            )
    for name, harness in cfg["harnesses"].items():
        if SAFE_NAME.search(name):
            sys.exit(f"invalid harness name {name!r}")
        try:
            runner_for(harness)
        except ValueError as e:
            sys.exit(f"harness {name}: {e}")
    validate_judge(cfg)
    make_sandbox(cfg)  # exits with a message for a bad `sandbox` section (a tag instead of a digest, bad limits)
    make_sinks(cfg, Path())  # likewise for `sinks`; building a sink does no I/O, so the base only matters to `run`
    leaked = spend_key_vars(cfg) & (
        set(cfg.get("env_passthrough", []))
        | {v for h in cfg["harnesses"].values() for val in h.get("env", {}).values() for v in env_var_names(val)}
    )
    if leaked:
        sys.exit(f"LiteLLM spend key {sorted(leaked)} must not be passed to agents")


def make_sandbox(cfg: Mapping[str, Any]) -> Sandbox:
    """The sandbox of the config (ADR 0010); exits with a message when its section is invalid. Its helper commands
    (`docker rm`) go through `run_proc` with the env allowlist, like every other command."""
    try:
        return sandbox_for(cfg, run_proc, base_env(cfg))
    except ValueError as e:  # SandboxError and PluginError
        sys.exit(f"sandbox: {e}")


def make_sinks(cfg: Mapping[str, Any], base: Path) -> list[Sink]:
    """The sinks of the config (ADR 0011); exits with a message when a `sinks` entry is invalid."""
    try:
        return sinks_for(cfg, base)
    except ValueError as e:  # SinkError and PluginError
        sys.exit(f"sinks: {e}")


def is_sandboxed(cfg: Mapping[str, Any]) -> bool:
    return make_sandbox(cfg).isolated


def require_sandbox(cfg: Mapping[str, Any], allow_unsandboxed: bool) -> None:
    """Exit unless commands run in the configured sandbox or the user accepted the risk. Every command that
    executes agent or task code (`run`, `suite validate --run`) starts with this check."""
    if not is_sandboxed(cfg) and not allow_unsandboxed:
        sys.exit(
            "No sandbox configured. Agents run without approval prompts and the test\n"
            "command executes agent-written code as your user. Set sandbox (type docker, or a\n"
            "prefix) in the config (see HARNESS_BENCH.md) or pass --allow-unsandboxed inside a disposable VM."
        )


def rmtree_inside(path: Path | str, root: Path | str) -> None:
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or not path.is_relative_to(root):
        raise RuntimeError(f"refusing to delete {path}: outside {root}")
    shutil.rmtree(path, ignore_errors=True)


def wrap(session: SandboxSession, argv: list[str], env_file: Path) -> list[str]:
    """The command that runs `argv` in the trial's sandbox. Every command that executes agent or task code goes
    through here."""
    return session.argv(argv, env_file)


# Host-specific values that would break a container (host PATH, home, user, temp dir).
# The sandbox image provides its own.
HOST_ONLY_ENV = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR"}


def write_env_file(env: Mapping[str, str], path: Path) -> None:
    """Docker --env-file format; 0600; deleted after the run."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for k, v in env.items():
            if k not in HOST_ONLY_ENV and "\n" not in v:
                f.write(f"{k}={v}\n")


# ---------------------------------------------------------------- run


def _kill_group(pgid: int) -> None:
    """SIGKILL every process of the group. Its leader has not been reaped, so the group exists even if every
    process in it has ended (a zombie leader is still a member)."""
    os.killpg(pgid, signal.SIGKILL)


def _wait_unreaped(pid: int, timeout: float) -> bool:
    """Wait until the child `pid` has exited, without reaping it (`WNOWAIT`), so that its process group id stays
    reserved until the caller has killed the group. False when `timeout` seconds pass first."""
    deadline = time.monotonic() + timeout
    while os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def run_proc(argv: list[str], cwd: Path | None, env: Mapping[str, str], timeout: float) -> tuple[int, bool, str, str]:
    """Run argv without a shell; return (rc, timed_out, stdout, stderr).

    The command is the leader of its own session, and its whole process group is killed when it ends or times out, so
    nothing the agent forked into the background survives into grading (ADR 0010). Output goes to private temp files,
    not pipes: a background child that still holds stdout cannot keep this call waiting. A process that leaves the
    group with `setsid` is out of reach; only a container (`end_step`) stops that."""
    with (
        tempfile.TemporaryFile("w+", errors="replace") as out_file,
        tempfile.TemporaryFile("w+", errors="replace") as err_file,
    ):
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out_file, stderr=err_file, start_new_session=True
        )
        try:
            timed_out = not _wait_unreaped(proc.pid, timeout)
        finally:
            # The leader is not reaped yet, so its pid cannot name another process group: kill, then reap. The group
            # can be gone only if `waitid` failed because someone else reaped the leader; that error must not be
            # replaced by a lookup error.
            with contextlib.suppress(ProcessLookupError):
                _kill_group(proc.pid)
            status = proc.wait()
        rc = -1 if timed_out else status
        out_file.seek(0)
        err_file.seek(0)
        return rc, timed_out, out_file.read(), err_file.read()


def git_bin() -> str:
    """Absolute path of the git executable; exits with a clear message when it is missing."""
    return shutil.which("git") or sys.exit("git not found on PATH")


# `GIT_ALLOW_PROTOCOL` of the clone of a task repo (defense in depth, behind the suite loader). A config task is the
# user's own and may use any transport; a suite task is someone else's data and may only use `https`, so a suite can
# never clone a private local repo (`file`) or use the user's SSH keys (`ssh`).
CONFIG_GIT_PROTOCOLS = "https:ssh:file"
SUITE_GIT_PROTOCOLS = "https"
LOCAL_GIT_PROTOCOLS = "file"  # the clone from our own cache into the workdir


def git(*args: str, cwd: Path | None = None, protocols: str = CONFIG_GIT_PROTOCOLS) -> None:
    """Run git with only the transports in `protocols` allowed (`GIT_ALLOW_PROTOCOL`, so `ext::` is off too)."""
    env = {**os.environ, "GIT_ALLOW_PROTOCOL": protocols}
    subprocess.run(
        [git_bin(), *args], cwd=cwd, env=env, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True
    )


def is_git_source(src: str) -> bool:
    return src.endswith(".git") or "://" in src or src.startswith("git@") or (Path(src) / ".git").exists()


def prepare_workdir(task: Mapping[str, Any], base: Path, workdir: Path) -> None:
    """Fresh copy of the task repo, without git history.

    History is dropped on purpose: a clone at a pre-fix ref still holds the fix commit,
    and the origin remote would point back into this repo."""
    src = task["repo"]
    if not re.match(r"^(\w+://|git@)", src) and not Path(src).is_absolute():
        src = str((base / src).resolve())
    if is_git_source(src):
        cache = base / ".cache" / safe(task["id"])
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            allowed = SUITE_GIT_PROTOCOLS if "suite" in task else CONFIG_GIT_PROTOCOLS
            git("clone", "--quiet", "--", src, str(cache), protocols=allowed)
        git("clone", "--quiet", "--local", "--", str(cache), str(workdir), protocols=LOCAL_GIT_PROTOCOLS)
        if task.get("ref"):
            git("-C", str(workdir), "checkout", "--quiet", "--detach", task["ref"])
        shutil.rmtree(workdir / ".git")
    else:
        shutil.copytree(src, workdir, symlinks=True, ignore=PYCACHE)


# Options of every git call on the agent's workdir, so nothing the agent leaves there can steer or hide the diff:
# no replace refs, no ignore rules from any config, no fsmonitor, no hooks, no untracked cache.
# Defense in depth: the baseline git dir is outside the workdir and its config is ours, so most of these are
# already inert. They stay on because each one closes a way to hide a change or run a command on the host if the
# first line of defense ever fails (a git dir moved into the workdir, a config written next to it, a new git default).
GIT_HARDENING = (
    "--no-replace-objects",
    "-c",
    "core.excludesFile=/dev/null",
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.untrackedCache=false",
)


# Attributes the diff must not see. `info/attributes` in the baseline git dir has the highest precedence, so with this
# line no `.gitattributes` in the workdir can change how a file is added or counted: `working-tree-encoding` would
# make `git add` fail, `-diff` or `binary` would turn counts into `-`, `text`/`eol`/`ident`/`filter` would rewrite
# the content. It works on every git version. Real binary detection (NUL bytes) is not affected.
INFO_ATTRIBUTES = "* !text !eol !crlf !working-tree-encoding !filter !ident !diff !merge !export-subst\n"
# The empty tree: with `--attr-source` git also ignores the workdir's `.gitattributes` (git 2.40 or newer).
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MIN_GIT_FOR_ATTR_SOURCE = (2, 40)


class GitError(Exception):
    """A git call the diff depends on failed. Callers fail closed."""


@functools.cache
def git_version() -> tuple[int, int]:
    """`(major, minor)` of the git on PATH."""
    out = subprocess.run([git_bin(), "--version"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def git_env(index_file: Path | str | None = None) -> dict[str, str]:
    """Environment for the hardened git: PATH only, no user or system config, no inherited GIT_* variable.
    `index_file` is the one thing added: a throwaway index for a diff."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        **({"GIT_INDEX_FILE": str(index_file)} if index_file else {}),
    }


def hardened_git(
    git_dir: Path,
    work_tree: Path,
    *args: str,
    check: bool = False,
    timeout: float = 300,
    index_file: Path | str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git against the baseline `git_dir` (outside the workdir) and the agent's `work_tree`.

    The workdir's own `.git` (config, `info/exclude`, index flags, replace refs) is never read: the agent controls
    it, and its config could run commands on the host (`core.fsmonitor`, `filter.*`, `diff.*.textconv`). Its
    `.gitattributes` files are ignored too (`info/attributes` of the git dir, see `make_baseline`)."""
    # Defense in depth: `info/attributes` already outranks the workdir's `.gitattributes`. On git 2.40 or newer
    # `--attr-source` with the empty tree adds a second layer, in case that file is ever missing or changed.
    attr_source = [f"--attr-source={EMPTY_TREE}"] if git_version() >= MIN_GIT_FOR_ATTR_SOURCE else []
    return subprocess.run(
        [
            git_bin(),
            f"--git-dir={git_dir}",
            f"--work-tree={work_tree}",
            *attr_source,
            *GIT_HARDENING,
            *args,
        ],
        cwd=work_tree,
        env=git_env(index_file),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        check=check,
        timeout=timeout,
    )


@dataclass(frozen=True)
class Baseline:
    """The state the agent starts from: a git dir outside the workdir, the workdir and the baseline commit."""

    git_dir: Path
    workdir: Path
    sha: str


def write_info_attributes(git_dir: Path | str) -> None:
    """Write `INFO_ATTRIBUTES` to `<git_dir>/info/attributes`, before the first `git add`."""
    info = Path(git_dir) / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text(INFO_ATTRIBUTES)


def make_baseline(workdir: Path, git_dir: Path) -> Baseline:
    """Single commit with the state the agent starts from (after setup), in a git dir outside the workdir.
    Ignore rules do not apply, so files a `.gitignore` lists are part of the baseline too."""
    ident = ["-c", "user.name=harness-bench", "-c", "user.email=bench@localhost", "-c", "commit.gpgsign=false"]
    hardened_git(git_dir, workdir, "init", "--quiet", "-b", "main", check=True)
    try:
        write_info_attributes(git_dir)
    except OSError:
        pass  # `staged_diff` checks the file before every diff and fails closed when it is missing
    hardened_git(git_dir, workdir, "add", "-A", "--force", check=True)
    hardened_git(git_dir, workdir, *ident, "commit", "--quiet", "--allow-empty", "-m", "baseline", check=True)
    sha = hardened_git(git_dir, workdir, "rev-parse", "HEAD", check=True).stdout.strip()
    return Baseline(git_dir=Path(git_dir), workdir=Path(workdir), sha=sha)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def snapshot_protected(task: Mapping[str, Any], workdir: Path, snap: Path) -> None:
    """Copy protected paths out of the workdir before the agent runs. The snapshot lives
    outside the workdir, so moving git refs or deleting .git cannot alter it."""
    for rel in task.get("protected_paths", []):
        src, dst = workdir / rel, snap / rel
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, dst, symlinks=True)
        elif src.exists() or src.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst, follow_symlinks=False)


def restore_protected(task: Mapping[str, Any], workdir: Path, snap: Path) -> None:
    """Undo agent edits to test infrastructure: put back the snapshot of each protected
    path and delete anything the agent added there."""
    for rel in task.get("protected_paths", []):
        target, saved = workdir / rel, snap / rel
        _remove(target)
        if saved.is_dir() and not saved.is_symlink():
            shutil.copytree(saved, target, symlinks=True)
        elif saved.exists() or saved.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target, follow_symlinks=False)


def staged_diff(baseline: Baseline, *flags: str) -> subprocess.CompletedProcess[str]:
    """`git diff --cached` of everything in the workdir (new files and ignored files included) against baseline.

    Every call builds a fresh index from the baseline commit, so flags kept in a stored index (`skip-worktree`,
    `assume-unchanged`) have no effect. Raises `GitError` when git cannot read the baseline or add the workdir:
    a diff without those files would compare the baseline with itself and show no change at all."""
    try:
        attributes_ok = (baseline.git_dir / "info" / "attributes").read_text() == INFO_ATTRIBUTES
    except OSError:
        attributes_ok = False
    if not attributes_ok:
        raise GitError("git diff failed: info/attributes of the baseline git dir is missing or changed")
    with tempfile.TemporaryDirectory(prefix="harness-bench-index-") as tmp:
        index = Path(tmp) / "index"
        git_dir, workdir = baseline.git_dir, baseline.workdir
        # Defense in depth: `add` into the fresh index would do, this also checks that the baseline sha exists.
        read = hardened_git(git_dir, workdir, "read-tree", baseline.sha, index_file=index)
        if read.returncode != 0:
            raise GitError(f"git diff failed: {read.stderr.strip()}")
        added = hardened_git(git_dir, workdir, "add", "-A", "--force", index_file=index)
        if added.returncode != 0:
            raise GitError(f"git add failed: {added.stderr.strip()}")
        # Defense in depth: the config is ours, so no external diff or textconv is set; these refuse one anyway.
        return hardened_git(
            git_dir,
            workdir,
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            *flags,
            baseline.sha,
            index_file=index,
        )


def nested_git_entries(workdir: Path) -> list[str]:
    """Paths (relative to the workdir) of every `.git` file, directory or link inside it. After the agent ran there
    should be none: `git add` would treat such a directory as a repository and hide what it holds."""
    found = []
    for dirpath, dirnames, filenames in os.walk(workdir):
        found += [str((Path(dirpath) / n).relative_to(workdir)) for n in (*dirnames, *filenames) if n == ".git"]
        dirnames[:] = [d for d in dirnames if d != ".git"]
    return sorted(found)


def save_diff(baseline: Baseline, path: Path, limit: int = 2_000_000, secrets: Iterable[str] = ()) -> None:
    """Everything the agent changed vs baseline, including new files (for audit). A diff that cannot be built is
    reported in the file (stderr redacted with `secrets`), never left empty."""
    nested = nested_git_entries(baseline.workdir)
    note = (
        f"nested .git: {', '.join(nested)} (changes inside them are not shown; counted as unknown)\n" if nested else ""
    )
    try:
        p = staged_diff(baseline, "--stat", "--patch")
        text = p.stdout if p.returncode == 0 else f"git diff failed:\n{p.stderr}"
    except (GitError, subprocess.SubprocessError, OSError) as e:
        text = f"{e}" if isinstance(e, GitError) else f"git diff failed: {e}"
    text = redact(note + text, secrets)
    path.write_text(text[:limit] + ("\n[truncated]" if len(text) > limit else ""))


# A path outside the workdir: `diff_constraints` treats it as forbidden, so an unreadable diff fails closed.
UNKNOWN_CHANGES = (Change(path="../unknown-changes", added=0, deleted=0),)


# What a binary file counts as: more than any `max_changed_lines`. Git prints `-` for it and gives no line count, so
# fail closed; counting it as 0 would let the agent dodge the limit with a blob.
BINARY_LINES = 10**9


def parse_numstat(text: str) -> tuple[Change, ...]:
    """Changes from `git diff --numstat -z --no-renames` output. A binary file (`-`) counts as `BINARY_LINES`."""
    changes = []
    for record in text.split("\0"):
        if not record:
            continue
        added, deleted, path = record.split("\t", 2)
        # A binary file prints `-\t-`: `deleted` stays 0 and `added` carries the whole (huge) count.
        changes.append(
            Change(
                path=path,
                added=int(added) if added.isdigit() else BINARY_LINES,
                deleted=int(deleted) if deleted.isdigit() else 0,
            )
        )
    return tuple(changes)


def collect_changes(baseline: Baseline) -> tuple[Change, ...]:
    """What the agent changed vs baseline as `Change`s (renames show as a delete and an add), for the graders.
    Fails closed with `UNKNOWN_CHANGES` when the diff is unreadable (git cannot add a file, git is too old) or the
    agent left a `.git` in the workdir."""
    if nested_git_entries(baseline.workdir):
        return UNKNOWN_CHANGES
    try:
        p = staged_diff(baseline, "--numstat", "-z", "--no-renames")
        return parse_numstat(p.stdout) if p.returncode == 0 else UNKNOWN_CHANGES
    except (GitError, subprocess.SubprocessError, OSError, ValueError):
        return UNKNOWN_CHANGES


def build_cmd(template: list[str], model: str, prompt: str) -> list[str]:
    return build_argv(template, model, prompt)


def done_keys(results_path: Path) -> set[tuple[Any, ...]]:
    keys = set()
    if results_path.exists():
        for obj in json_lines(results_path.read_text()):
            keys.add((obj["task"], obj["harness"], obj["model"], obj["rep"]))
    return keys


def resolve_work_root(cfg: Mapping[str, Any], base: Path) -> Path:
    """Work directories live outside the benchmark repo, so the agent's cwd does not
    sit next to hidden tests, config and results."""
    if cfg.get("work_dir"):
        root = (base / Path(expand_env(cfg["work_dir"])).expanduser()).resolve()  # relative: to the config dir
        if root.is_relative_to(base.resolve()):
            sys.exit(f"work_dir {root} is inside the benchmark repo; use a path outside it")
        root.mkdir(parents=True, exist_ok=True)
        return root
    root = Path(tempfile.mkdtemp(prefix="harness-bench-work-"))
    plant_token(root)
    return root


OWNER_MARK_FILE = ".hb-owner"


def plant_token(directory: Path) -> str:
    """Put a random token in a private temp dir the engine made, and return it. A killed run's heartbeat names the
    token, and resume deletes only a dir that still holds it: a heartbeat file cannot make it delete another dir."""
    token = secrets.token_hex(16)
    fd = os.open(directory / OWNER_MARK_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


def holds_token(directory: Path, token: object) -> bool:
    """Whether `directory` holds exactly the (non-empty) `token`."""
    try:
        return isinstance(token, str) and bool(token) and (directory / OWNER_MARK_FILE).read_text() == token
    except OSError:
        return False


def run_step(
    session: SandboxSession,
    command: str,
    env: Mapping[str, str],
    timeout: float,
    log_path: Path,
    workdir: Path,
    env_file: Path,
    secrets: Iterable[str],
    lost: list[str] | None = None,
) -> tuple[int, bool, str, str]:
    """Run a setup or grader command in the sandbox wrapper and write its redacted log (stdout, then stderr).
    Returns `(rc, timed_out, stdout, stderr)`, both redacted and kept apart: a score is only read from stdout.
    When the sandbox cannot end the step (its container is not shown to be gone), the reason goes to `lost`; without
    a `lost` list it raises `SandboxError`."""
    write_env_file(env, env_file)
    try:
        rc, to, out, err = run_proc(wrap(session, ["sh", "-c", command], env_file), workdir, env, timeout)
    finally:
        env_file.unlink(missing_ok=True)
        end_step_or_record(session, lost)
    out, err = redact(out, secrets), redact(err, secrets)
    log_path.write_text(out + "\n--- stderr ---\n" + err)
    return rc, to, out, err


def end_step_or_record(session: SandboxSession, lost: list[str] | None) -> None:
    """End the step of the session. A failure (a container that cannot be shown to be gone) is recorded in `lost`
    and printed, so the engine can fail the trial closed; without a `lost` list it is raised."""
    try:
        session.end_step()
    except SandboxError as e:
        if lost is None:
            raise
        print(f"warning: {e}")
        lost.append(str(e))


def destroy_or_warn(session: SandboxSession, what: str) -> None:
    """Destroy the session at the end of a trial. The verdict is already decided, so a container that cannot be
    removed is a warning."""
    try:
        session.destroy()
    except SandboxError as e:
        print(f"warning: could not remove the sandbox of {what}: {e}")


def step_runner(
    session: SandboxSession,
    logs_dir: Path,
    tag: str,
    workdir: Path,
    env_file: Path,
    secrets: Iterable[str],
    env: Mapping[str, str],
    lost: list[str] | None = None,
) -> Callable[[str], RunStep]:
    """`run_for(prefix)` for `grade_trial`: a `RunStep` that goes through `run_step` (sandbox wrapper, env allowlist,
    redacted log named `<tag>.<prefix><log name>.log` in `logs_dir`)."""
    secret_list = list(secrets)

    def run_for(prefix: str) -> RunStep:
        def run(command: str, timeout: int, log_name: str) -> tuple[int, bool, str, str]:
            log = logs_dir / f"{tag}.{prefix}{safe(log_name)}.log"
            return run_step(session, command, env, timeout, log, workdir, env_file, secret_list, lost)

        return run

    return run_for


def grader_logger(logs_dir: Path, tag: str, secrets: Iterable[str]) -> Callable[[str], LogStep]:
    """`log_for(stem)` for `grade_trial`: a `LogStep` that appends redacted text to `<tag>.<stem>.log` in `logs_dir`.
    The file starts empty for each trial. This is the only place a judge's `reason` is ever written."""
    secret_list = list(secrets)

    def log_for(stem: str) -> LogStep:
        path = logs_dir / f"{tag}.{safe(stem)}.log"
        path.unlink(missing_ok=True)

        def log(text: str) -> None:
            clean = redact(text, secret_list)  # redact the whole text first, then cut: no secret is cut in half
            if len(clean) > MAX_GRADER_LOG_CHARS:
                clean = clean[:MAX_GRADER_LOG_CHARS] + "\n[log cut]\n"
            with path.open("a") as f:
                f.write(clean)

        return log

    return log_for


def grade_trial(
    plan: GradePlan,
    task: Mapping[str, Any],
    base: Path,
    workdir: Path,
    changes: tuple[Change, ...],
    run_for: Callable[[str], RunStep],
    judge: Judge | None = None,
    log_for: Callable[[str], LogStep] | None = None,
) -> tuple[bool, list[GraderResult]]:
    """Run the task's graders over the finished workdir and compose them: `(passed, results)`.

    `run_for(prefix)` gives a grader its `run`: the engine's `run_step`, with log names starting at `prefix`.
    `log_for(stem)` gives it a redacted log file (none: the text is dropped) and `judge` the LLM judge of the run.
    A grader that raises fails (only its exception type is printed, since the message may hold agent output)."""
    results = []
    for index, grader in enumerate(plan.graders):
        prefix = "" if grader.name == "hidden_tests" else f"grader-{index}-"
        ctx = GradeContext(
            workdir=workdir,
            task=task,
            base=base,
            changes=changes,
            run=run_for(prefix),
            log=log_for(f"grader-{index}-{grader.name}") if log_for else discard_log,
            judge=judge,
        )
        try:
            results.append(grader.grade(ctx))
        except Exception as e:
            print(f"warning: grader {grader.name} failed with {type(e).__name__}; counted as failed")
            results.append(GraderResult(grader=grader.name, grader_version=grader.version, passed=False, score=0.0))
    passed, _ = compose(results, plan.mode, plan.weights, plan.threshold)
    return passed, results


def stable_task(task: Mapping[str, Any]) -> dict[str, Any]:
    """`task` as it enters the hash of a run. A suite task's directories are relative to its suite directory, so the
    hash does not depend on the checkout path. A git URL stays as it is."""
    if "suite" not in task:
        return dict(task)
    root = Path(task["suite_dir"])
    stable = {k: v for k, v in task.items() if k != "suite_dir"}
    for key in SUITE_PATH_KEYS:
        if key in stable and Path(stable[key]).is_relative_to(root):
            stable[key] = Path(stable[key]).relative_to(root).as_posix()
    return stable


def suite_version_of(tasks: Sequence[Mapping[str, Any]]) -> str:
    """What `RunInfo.suite_version` records: the hash of the tasks, led by `name@version,...` when suites ran."""
    labels = list(dict.fromkeys(t["suite_label"] for t in tasks if "suite" in t))
    digest = sha256_json([stable_task(t) for t in tasks])[:12]
    return f"{','.join(labels)}+{digest}" if labels else digest


@dataclass(frozen=True)
class TaskCheck:
    """Behavioral check of a suite task: the graders must fail on the untouched repo and pass with the solution."""

    task_id: str
    untouched_passed: bool
    solution_passed: bool

    @property
    def ok(self) -> bool:
        return not self.untouched_passed and self.solution_passed

    @property
    def status(self) -> str:
        problems = [
            *(["trivially passing"] if self.untouched_passed else []),
            *([] if self.solution_passed else ["solution fails"]),
        ]
        return ", ".join(problems) or "ok"


def run_task_attempt(
    cfg: Mapping[str, Any],
    task: Mapping[str, Any],
    base: Path,
    work_root: Path,
    git_root: Path,
    tag: str,
    solution: Path | None,
) -> bool:
    """One trial of `task` the way `run` does it (workdir, setup, baseline, protected paths, graders), with the files
    of `solution` copied over the repo in place of an agent. Whether the graders passed."""
    logs_dir = work_root / "logs"
    logs_dir.mkdir(exist_ok=True)
    workdir = work_root / tag
    rmtree_inside(workdir, work_root)  # a leftover of a crashed validation
    prepare_workdir(task, base, workdir)
    env = base_env(cfg)
    secrets = [os.environ.get(v, "") for v in spend_key_vars(cfg)]
    session = make_sandbox(cfg).create(workdir, safe(tag))
    try:
        run_for = step_runner(session, logs_dir, tag, workdir, work_root / f".{tag}.env", secrets, env)
        if task.get("setup"):
            run_for("")(task["setup"], task.get("setup_timeout", 900), "setup")
        git_dir = git_root / f"{tag}.git"
        baseline = make_baseline(workdir, git_dir)
        snap = Path(tempfile.mkdtemp(prefix=f".{tag}.protected-", dir=work_root))
        snapshot_protected(task, workdir, snap)
        if solution is not None:
            safe_copy_into(solution, workdir, ignore=PYCACHE)  # never through a link the repo left in the workdir
        changes = collect_changes(baseline)
        rmtree_inside(git_dir, git_root)
        restore_protected(task, workdir, snap)
        rmtree_inside(snap, work_root)
        with_task = {**cfg, "tasks": [task]}
        judge = make_judge(with_task, secrets, load_redactor(cfg["judge"]) if uses_judge(with_task) else None)
        passed, _ = grade_trial(
            grade_plan(task), task, base, workdir, changes, run_for, judge, grader_logger(logs_dir, tag, secrets)
        )
    finally:
        session.destroy()
    rmtree_inside(workdir, work_root)
    return passed


def validate_task_behavior(cfg: Mapping[str, Any], task: Mapping[str, Any], base: Path, work_root: Path) -> TaskCheck:
    """The M2 check of a suite task (needs its `solution` key): the graders on the untouched repo and with the
    solution. It runs repo and test code only through the engine path of `run`: `prepare_workdir`, `run_step` (so
    `wrap()`, the env allowlist and log redaction) and `rmtree_inside`. The caller has applied `require_sandbox`."""
    slug = safe(task["id"])
    with tempfile.TemporaryDirectory(prefix="harness-bench-git-") as git_root:
        untouched = run_task_attempt(cfg, task, base, work_root, Path(git_root), f"validate-{slug}-untouched", None)
        solved = run_task_attempt(
            cfg, task, base, work_root, Path(git_root), f"validate-{slug}-solution", Path(task["solution"])
        )
    return TaskCheck(task_id=task["id"], untouched_passed=untouched, solution_passed=solved)


def make_run_info(cfg: Mapping[str, Any], only: Sequence[str] | None, sandbox: Sandbox) -> RunInfo:
    """Metadata of this `run`. The config hash covers `${VAR}` references, never expanded values, and the
    config as written: the tasks that came from suites are not part of it."""
    targets = tuple(
        Target(
            harness=pair["harness"],
            harness_version=runner_for(cfg["harnesses"][pair["harness"]]).version(),
            model=pair["model"],
            params={},
        )
        for pair in cfg["matrix"]
        if not only or pair["harness"] in only
    )
    return RunInfo(
        run_id=new_run_id(),
        started_at=datetime.now(UTC),
        finished_at=None,
        harness_bench_version=VERSION,
        config_sha256=config_hash(cfg),
        suite_version=suite_version_of(cfg["tasks"]),
        python=platform.python_version(),
        platform=platform.platform(),
        sandboxed=sandbox.isolated,
        sandbox=sandbox.describe(),
        targets=targets,
    )


def cmd_run(args: argparse.Namespace, cfg: Mapping[str, Any], base: Path) -> None:
    """Run the matrix. The baseline git dirs of the trials live in a private temp dir that is always removed: not in
    `work_dir`, whose names an unsandboxed agent could guess."""
    with tempfile.TemporaryDirectory(prefix="harness-bench-git-") as git_root:
        plant_token(Path(git_root))
        _run_matrix(args, cfg, base, Path(git_root))


@dataclass(frozen=True)
class _Matrix:
    """What every trial of one `run` shares."""

    cfg: Mapping[str, Any]
    base: Path
    work_root: Path
    git_root: Path
    logs_dir: Path
    results_path: Path
    writer: RunWriter
    heartbeat: Heartbeat
    sandbox: Sandbox
    plans: Mapping[str, GradePlan]
    redactor: Callable[[str], str] | None
    secrets: Sequence[str]
    keep: bool
    resuming: bool
    sinks: SinkFeed


@dataclass(frozen=True)
class _Planned:
    """One trial of the matrix, before it runs."""

    task: Mapping[str, Any]
    harness: Mapping[str, Any]
    hname: str
    model: str
    rep: int
    runner: HarnessRunner
    price: Mapping[str, Any] | None


def _planned_trials(cfg: Mapping[str, Any], only: Sequence[str] | None, reps: int) -> Iterator[_Planned]:
    """Every trial of the matrix in run order: pairs, then tasks, then repetitions."""
    prices = cfg.get("prices", {})
    for pair in cfg["matrix"]:
        hname, model = pair["harness"], pair["model"]
        if only and hname not in only:
            continue
        for task in cfg["tasks"]:
            for rep in range(1, reps + 1):
                harness = cfg["harnesses"][hname]
                price = prices.get(pair.get("price", model))
                yield _Planned(task, harness, hname, model, rep, runner_for(harness), price)


def planned_trial_id(run_id: str, planned: _Planned) -> str:
    """The `trial_id` the trial will have in `trials.jsonl`: known before it runs, so resume can match it."""
    target = Target(harness=planned.hname, harness_version=planned.runner.version(), model=planned.model, params={})
    return trial_id(run_id, planned.task["id"], target.target_id, planned.rep)


def config_hash(cfg: Mapping[str, Any]) -> str:
    """The hash of the config as written (`${VAR}` references, not values; suite tasks left out)."""
    return sha256_json({**cfg, "tasks": [t for t in cfg["tasks"] if "suite" not in t]})


def open_resumed_run(out_dir: Path, run_id: str, cfg: Mapping[str, Any], force: bool) -> RunWriter:
    """The writer of the run `run_id` for `--resume`. Exits with a message when the run cannot be resumed: it does not
    exist, its files are invalid, the config is not the one it ran with, or a live process still holds it."""
    try:
        writer = RunWriter.open_existing(out_dir, run_id)
        writer.trials()  # reads trials.jsonl now, so a bad file stops the resume before anything runs
    except ResumeError as e:
        sys.exit(f"resume: {e}")
    if writer.info.config_sha256 != config_hash(cfg):
        sys.exit(f"resume: the config changed since run {run_id} started; resume it with the config it ran with")
    if not force and heartbeat_is_live(writer.run_dir / HEARTBEAT_FILE):
        sys.exit(f"resume: run {run_id} is still running (its heartbeat is fresh and its process alive); use --force")
    return writer


def discard_leftovers(heartbeat_path: Path) -> None:
    """Remove the private temp dirs a killed run left (its baseline git dirs and its work dir, with the env files in
    it). The paths come from its heartbeat file, which an agent may have written, so a dir is removed only when it
    sits directly in the temp dir, is no link, has the engine's name and holds the token the heartbeat names."""
    data = read_heartbeat(heartbeat_path) or {}
    tmp = Path(tempfile.gettempdir())
    for key, prefix in (("git", "harness-bench-git-"), ("work", "harness-bench-work-")):
        value = data.get(f"{key}_root")
        path = Path(value) if isinstance(value, str) else None
        owned = path and path.name.startswith(prefix) and path.parent.resolve() == tmp.resolve()
        if owned and path and not path.is_symlink() and holds_token(path, data.get(f"{key}_token")):
            rmtree_inside(path, tmp)


def restore_legacy_rows(writer: RunWriter, results_path: Path) -> None:
    """Write the `results.jsonl` row of every trial of the run that has none: a run killed between the two files."""
    done = done_keys(results_path)
    for trial in writer.trials():
        if (trial.task_id, trial.target.harness, trial.target.model, trial.rep) not in done:
            with results_path.open("a") as f:
                f.write(json.dumps(row_from_trial(trial)) + "\n")


def _run_matrix(args: argparse.Namespace, cfg: Mapping[str, Any], base: Path, git_root: Path) -> None:
    cfg = expand_suites(cfg, base)
    validate_config(cfg)
    resume_id = getattr(args, "resume", None)
    if resume_id and args.dry_run:
        sys.exit("resume: --resume and --dry-run cannot be combined")
    sandbox = make_sandbox(cfg)
    if not args.dry_run:
        require_sandbox(cfg, args.allow_unsandboxed)
    out_dir = base / cfg.get("output_dir", "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = out_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    results_path = out_dir / "results.jsonl"
    reps = args.reps or cfg.get("reps", 1)
    writer = None
    if resume_id:
        writer = open_resumed_run(out_dir, resume_id, cfg, getattr(args, "force", False))
        discard_leftovers(writer.run_dir / HEARTBEAT_FILE)
        restore_legacy_rows(writer, results_path)
    work_root = None if args.dry_run else resolve_work_root(cfg, base)
    if work_root:
        print(f"work dir: {work_root}")
    if not (args.dry_run or resume_id):
        writer = RunWriter(out_dir, make_run_info(cfg, args.only, sandbox))
    finished = done_keys(results_path)
    finished_ids = writer.finished_trial_ids() if writer else set()
    shared = None
    secrets = [os.environ.get(v, "") for v in spend_key_vars(cfg)]
    if work_root and writer:  # a dry run has neither
        # The temp dirs a killed run leaves are found again by resume through these paths and tokens.
        paths = {"git_root": str(git_root), "git_token": (git_root / OWNER_MARK_FILE).read_text()}
        if not cfg.get("work_dir"):
            paths |= {"work_root": str(work_root), "work_token": (work_root / OWNER_MARK_FILE).read_text()}
        shared = _Matrix(
            cfg=cfg,
            base=base,
            work_root=work_root,
            git_root=git_root,
            logs_dir=logs_dir,
            results_path=results_path,
            writer=writer,
            heartbeat=Heartbeat(writer.run_dir / HEARTBEAT_FILE, extra=paths),
            sandbox=sandbox,
            plans={task["id"]: grade_plan(task) for task in cfg["tasks"]},
            redactor=load_redactor(cfg["judge"]) if uses_judge(cfg) else None,
            secrets=secrets,
            keep=args.keep,
            resuming=bool(resume_id),
            sinks=SinkFeed(
                make_sinks(cfg, base),
                lambda message: print(redact(message, secrets), file=sys.stderr),
            ),
        )
        shared.sinks.open(writer.info)

    try:
        try:
            with shared.heartbeat if shared else contextlib.nullcontext():
                for planned in _planned_trials(cfg, args.only, reps):
                    task, hname, model, rep = planned.task, planned.hname, planned.model, planned.rep
                    key = (task["id"], hname, model, rep)
                    tid = planned_trial_id(writer.info.run_id, planned) if writer else ""
                    if (key in finished or tid in finished_ids) and not args.dry_run:
                        print(f"skip  {key} (done)")
                        continue
                    if shared is None:  # dry run: no work dir and no writer
                        print(shlex.join(planned.runner.command(model, task["prompt"])))
                        continue
                    _run_trial(shared, planned, tid)
        finally:  # also when a trial raises: the private work dir is never left behind
            if work_root and not args.keep and not cfg.get("work_dir"):
                shutil.rmtree(work_root, ignore_errors=True)
        if writer:
            writer.close(datetime.now(UTC))
    finally:  # the sinks see the run as `writer.close` left it (unfinished after an error or Ctrl-C) and are released
        if shared:
            shared.sinks.close(shared.writer.info)
    if not args.dry_run and not getattr(args, "no_report", False):
        cmd_report(args, cfg, base)


def _run_trial(m: _Matrix, planned: _Planned, tid: str) -> None:
    """One trial: workdir, setup, baseline, agent, audit diff, protected paths, graders; then its two result rows
    (`trials.jsonl` first, flushed to disk, so a crash between the two can only lose the legacy row). The trial's
    sandbox session is always destroyed."""
    task, h, hname, model, rep = planned.task, planned.harness, planned.hname, planned.model, planned.rep
    runner, price = planned.runner, planned.price
    cmd = runner.command(model, task["prompt"])
    m.heartbeat.beat(tid, "setup")
    if m.resuming:
        m.sandbox.reclaim(tid)  # a container the killed run left running for this very trial
    tag = f"{safe(task['id'])}__{safe(hname)}__{safe(model)}__r{rep}"
    workdir = m.work_root / tag
    run_tag = f"bench-{datetime.now().strftime('%Y%m%d%H%M%S')}-{tag}"
    cmd_env = base_env(m.cfg)
    # Pi/OpenCode/Codex send BENCH_TAG as x-litellm-tags; the ids let a harness config send them as headers too
    agent_env = dict(cmd_env, BENCH_TAG=run_tag, BENCH_RUN_ID=m.writer.info.run_id, BENCH_TRIAL_ID=tid)
    harness_env = {k: expand_env(v).replace("{run_tag}", run_tag) for k, v in h.get("env", {}).items()}
    agent_env.update(harness_env)
    secrets = list(m.secrets) + [v for v in harness_env.values() if v != run_tag]
    if workdir.exists():
        rmtree_inside(workdir, m.work_root)
    prepare_workdir(task, m.base, workdir)
    env_file = m.work_root / f".{tag}.env"
    env_file.unlink(missing_ok=True)  # the env file of a killed run holds secrets
    session = m.sandbox.create(workdir, tid)
    lost: list[str] = []  # why a step's container could not be shown to be gone: the trial then fails closed
    try:
        if task.get("setup"):
            run_step(
                session,
                task["setup"],
                cmd_env,
                task.get("setup_timeout", 900),
                m.logs_dir / f"{tag}.setup.log",
                workdir,
                env_file,
                secrets,
                lost,
            )
        runner.prepare(workdir)
        git_dir = m.git_root / f"{tag}.git"
        baseline = make_baseline(workdir, git_dir)
        snap = m.work_root / f".{tag}.protected"
        if snap.exists():
            rmtree_inside(snap, m.work_root)
        snap.mkdir()
        snapshot_protected(task, workdir, snap)

        print(f"run   {tag}", flush=True)
        m.heartbeat.beat(tid, "agent")
        t0 = time.time()
        write_env_file(agent_env, env_file)
        try:
            rc, timed_out, stdout, stderr = run_proc(
                wrap(session, cmd, env_file),
                workdir,
                agent_env,
                task.get("timeout", m.cfg.get("timeout", 1800)),
            )
        finally:
            env_file.unlink(missing_ok=True)
            end_step_or_record(session, lost)  # the agent's container must be gone before anything reads the workdir
        wall = time.time() - t0
        (m.logs_dir / f"{tag}.agent.jsonl").write_text(redact(stdout, secrets))
        (m.logs_dir / f"{tag}.agent.stderr").write_text(redact(stderr, secrets))

        usage = runner.parse_usage(stdout)
        parse_ok = usage is not None
        usage = usage or empty_usage()

        # Captured before the protected paths are restored: an edit there must still count as a change.
        changes = UNKNOWN_CHANGES if lost else collect_changes(baseline)
        save_diff(baseline, m.logs_dir / f"{tag}.diff", secrets=secrets)
        rmtree_inside(git_dir, m.git_root)
        restore_protected(task, workdir, snap)
        rmtree_inside(snap, m.work_root)

        run_for = step_runner(session, m.logs_dir, tag, workdir, env_file, secrets, cmd_env, lost)

        m.heartbeat.beat(tid, "grade")
        judge = make_judge(m.cfg, secrets, m.redactor)
        passed = False
        grader_results: list[GraderResult] = []  # a container that may still write to the workdir is never graded
        if not lost:
            passed, grader_results = grade_trial(
                m.plans[task["id"]],
                task,
                m.base,
                workdir,
                changes,
                run_for,
                judge,
                grader_logger(m.logs_dir, tag, secrets),
            )
        passed = passed and not lost  # also when it was a grader's own container
    finally:
        destroy_or_warn(session, tag)
    m.heartbeat.beat(tid, "record")
    test_to = any(r.timed_out for r in grader_results)

    row = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "task": task["id"],
        "harness": hname,
        "model": model,
        "rep": rep,
        "passed": passed,
        "agent_rc": rc,
        "agent_timed_out": timed_out,
        "wall_s": round(wall, 1),
        "parse_ok": parse_ok,
        "run_tag": run_tag,
        "t_start": t0,
        "t_end": t0 + wall,
        "sandboxed": m.sandbox.isolated,
        **usage,
        "cost_computed": computed_cost(usage, price) if parse_ok else None,
    }
    # Built before the legacy row is written, so a bad row cannot leave the two files out of step.
    trial = replace(
        trial_from_row_lenient(
            row,
            run_id=m.writer.info.run_id,
            suite_version=m.writer.info.suite_version,
            harness_version=runner.version(),
            test_timed_out=test_to,
            graders=grader_results if task.get("graders") else None,
            warn=print,
        ),
        image_digest=m.sandbox.image_digest(),
    )
    m.writer.append(trial)
    m.sinks.write(trial)  # after `trials.jsonl`, which is the source of truth; a failing sink never stops the run
    with m.results_path.open("a") as f:
        f.write(json.dumps(row) + "\n")
    tokens = sum(usage[k] for k in ("input", "output", "reasoning", "cache_read", "cache_write"))
    print(
        f"done  {tag} passed={row['passed']} tokens={tokens}"
        f" cost={row['cost_computed'] if row['cost_computed'] is not None else usage['cost_reported']}",
        flush=True,
    )
    if not m.keep:
        rmtree_inside(workdir, m.work_root)


# ---------------------------------------------------------------- LiteLLM


def check_litellm_url(base_url: str, name: str = "litellm.base_url") -> None:
    """Exits unless `base_url` is https, or http to localhost. `name` is the setting shown in the message."""
    if url_allowed(base_url):
        return
    sys.exit(f"{name} must use https (http only for localhost): {base_url}")


def litellm_get(base_url: str, key: str, path: str, params: Mapping[str, Any]) -> Any:
    check_litellm_url(base_url)
    url = base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(req, timeout=120) as r:
        return json.loads(r.read().decode())


def judge_post(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> bytes:
    """The one POST of the engine, for the LLM judge: `<judge.base_url>/chat/completions`. It applies the URL rule
    of `check_litellm_url` and the opener that refuses redirects (the Authorization header must not reach another
    host), and reads at most `MAX_JUDGE_REPLY_BYTES`. Raises `JudgeError` for a refused URL or an oversized reply;
    any other failure (HTTP status, timeout, connection) is raised as it is for the judge to wrap."""
    try:
        check_litellm_url(url, "judge.base_url")
    except SystemExit as e:
        raise JudgeError(str(e.code)) from e
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    # `timeout` alone limits each socket operation, so a server that drips the status line, the headers or the body
    # never times out. urllib gives no hook before each operation, so the whole exchange runs in a daemon thread
    # (it cannot keep the process alive) and the caller waits at most `timeout` for it.
    outcome: list[bytes | BaseException] = []
    abandoned = threading.Event()

    def exchange() -> None:
        try:
            outcome.append(_judge_exchange(request, timeout, abandoned))
        except BaseException as e:  # handed to the caller, which re-raises it
            outcome.append(e)

    worker = threading.Thread(target=exchange, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive() or not outcome:
        abandoned.set()  # the thread stops at its next read; its sockets time out on their own
        raise TimeoutError("the judge reply took too long")
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


def _judge_exchange(request: urllib.request.Request, timeout: float, abandoned: threading.Event) -> bytes:
    """Send `request` and read the reply, at most `MAX_JUDGE_REPLY_BYTES`; stops reading once `abandoned` is set."""
    chunks: list[bytes] = []
    size = 0
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as r:
            while chunk := r.read1(min(65_536, MAX_JUDGE_REPLY_BYTES + 1 - size)):  # one socket read at a time
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_JUDGE_REPLY_BYTES:
                    raise JudgeError("the judge reply is too large")
                if abandoned.is_set():
                    raise TimeoutError("the judge reply took too long")
    except urllib.error.HTTPError as e:
        e.close()  # an error status holds the socket open until it is closed
        raise
    return b"".join(chunks)


def load_redactor(judge_cfg: Mapping[str, Any]) -> Callable[[str], str] | None:
    """The redactor plugin named by `judge.redactor`, or None. Exits with the plugin's message when it fails."""
    name = judge_cfg.get("redactor")
    if not name:
        return None
    try:
        return redactor_for(name, judge_cfg)
    except PluginError as e:
        sys.exit(f"judge.redactor: {e}")


def make_judge(cfg: Mapping[str, Any], secrets: Iterable[str], plugin: Callable[[str], str] | None) -> Judge | None:
    """The judge of a run, or None when no task uses `llm_rubric`. What it is shown passes through the engine's
    secret redaction (the same `secrets` as the logs) and then `plugin`, the user's redactor. The judge is cheap: a
    trial builds its own, because the secrets of an agent's environment differ per harness."""
    return gateway_judge(cfg["judge"], secrets, plugin) if uses_judge(cfg) else None


def gateway_judge(section: Mapping[str, Any], secrets: Iterable[str], plugin: Callable[[str], str] | None) -> Judge:
    """The judge of a checked `judge` section, with the redaction of `make_judge`."""
    secret_list = list(secrets)

    def redact_for_judge(text: str) -> str:
        text = redact(text, secret_list)
        return plugin(text) if plugin else text

    return GatewayJudge(
        base_url=expand_env(section["base_url"]),
        api_key=expand_env(section["api_key"]),
        model=section["model"],
        timeout=section.get("timeout", JUDGE_TIMEOUT),
        max_artifact_bytes=section.get("max_artifact_bytes", JUDGE_MAX_ARTIFACT_BYTES),
        redact=redact_for_judge,
        post=judge_post,
    )


def calibration_judge(cfg: Mapping[str, Any]) -> Judge:
    """The judge for `judge calibrate`: the config must have a valid `judge` section (checked here), whether or not
    a task uses it. Its redaction covers the same secrets as a run."""
    check_judge_section(cfg)
    secrets = [os.environ.get(v, "") for v in spend_key_vars(cfg)]
    return gateway_judge(cfg["judge"], secrets, load_redactor(cfg["judge"]))


def litellm_rows(base_url: str, key: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Individual spend log rows. Tries /spend/logs/v2 (paginated), then legacy /spend/logs."""
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            data = litellm_get(
                base_url,
                key,
                "/spend/logs/v2",
                {"start_date": start.strftime(fmt), "end_date": end.strftime(fmt), "page": page, "page_size": 1000},
            )
            batch = data.get("data", []) if isinstance(data, dict) else data
            rows += batch
            total_pages = data.get("total_pages", 1) if isinstance(data, dict) else 1
            if page >= total_pages or not batch:
                return rows
            page += 1
    except Exception as e:  # noqa: BLE001
        print(f"/spend/logs/v2 failed ({e}); trying legacy /spend/logs")
    data = litellm_get(
        base_url,
        key,
        "/spend/logs",
        {"start_date": start.strftime("%Y-%m-%d"), "end_date": (end).strftime("%Y-%m-%d"), "summarize": "false"},
    )
    fallback: list[dict[str, Any]] = data.get("data", []) if isinstance(data, dict) else data
    return fallback


def row_tags(r: Mapping[str, Any]) -> list[Any]:
    tags = r.get("request_tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = [t.strip() for t in tags.split(",")]
    return tags if isinstance(tags, list) else []


def cmd_reconcile(args: argparse.Namespace, cfg: Mapping[str, Any], base: Path) -> None:
    lcfg = cfg.get("litellm") or sys.exit("config has no 'litellm' section")
    require_env_ref(lcfg.get("api_key"), "litellm.api_key")
    base_url, key = expand_env(lcfg["base_url"]), expand_env(lcfg["api_key"])
    if not base_url or not key:
        sys.exit("litellm.base_url / litellm.api_key empty (check env vars)")
    results_path = base / cfg.get("output_dir", "results") / "results.jsonl"
    runs = list(json_lines(results_path.read_text()))
    tagged = [r for r in runs if r.get("run_tag")]
    if not tagged:
        sys.exit("no runs with run_tag")
    start = datetime.fromtimestamp(min(r["t_start"] for r in tagged) - 3600, UTC)
    end = datetime.fromtimestamp(max(r["t_end"] for r in tagged) + 3600, UTC)
    logs = litellm_rows(base_url, key, start, end)
    by_tag: dict[Any, list[dict[str, Any]]] = {}
    for row in logs:
        for t in row_tags(row):
            by_tag.setdefault(t, []).append(row)
    matched = 0
    spend_by_tag = {}
    for r in runs:
        hits = by_tag.get(r.get("run_tag"), [])
        if hits:
            matched += 1
            r["cost_litellm"] = sum(num(h, "spend") for h in hits)
            spend_by_tag[r["run_tag"]] = r["cost_litellm"]
            r["litellm_requests"] = len(hits)
            r["litellm_prompt_tokens"] = sum(num(h, "prompt_tokens") for h in hits)
            r["litellm_completion_tokens"] = sum(num(h, "completion_tokens") for h in hits)
    results_path.write_text("".join(json.dumps(r) + "\n" for r in runs))
    print(f"LiteLLM: {len(logs)} log rows, {matched}/{len(runs)} runs matched by tag")
    print(f"contract: {apply_gateway_costs(results_path.parent, spend_by_tag)} trials updated")
    if matched < len(tagged):
        print(
            "Unmatched runs: harness did not send x-litellm-tags, run not via LiteLLM "
            "(e.g. Cursor), or logs not written yet."
        )
    cmd_report(args, cfg, base)


# ---------------------------------------------------------------- report


def aggregate(rows: Iterable[Mapping[str, Any]], cost_source: str) -> list[dict[str, Any]]:
    """Per harness x model entries from legacy rows (the work is done over contract trials)."""
    return report.aggregate(trials_from_rows(rows), cost_source)


def load_calibration(cfg: Mapping[str, Any], base: Path) -> Calibration | None:
    """The calibration file named by `judge.calibration` (relative to the config), or None. A file that cannot be
    read is a warning: the judges are then reported as experimental."""
    section = cfg.get("judge")
    path = section.get("calibration") if isinstance(section, Mapping) else None
    if not isinstance(path, str) or not path:
        return None
    try:
        return read_calibration(base / path)
    except ValueError as e:
        print(f"warning: {e}; judges are reported as experimental")
        return None


def cmd_report(args: argparse.Namespace, cfg: Mapping[str, Any], base: Path) -> None:
    out_dir = base / cfg.get("output_dir", "results")
    results_path = out_dir / "results.jsonl"
    if not results_path.exists():
        sys.exit(f"no results at {results_path}")
    rows = list(json_lines(results_path.read_text()))
    if not rows:
        sys.exit(f"no results in {results_path}")
    cost_source = getattr(args, "cost_source", None) or cfg.get("cost_source", "auto")
    check_cost_source(cost_source)
    within_pp, target_width = report_options(cfg)
    null_harnesses = {name for name, h in cfg.get("harnesses", {}).items() if h.get("parser") == "null"}
    trials = trials_from_rows(rows, warn=print)
    if getattr(args, "drop_saturated", False):
        trials = report.drop_saturated(trials, null_harnesses)
        if not trials:
            print("no tasks left after --drop-saturated")
            return
    table = report.aggregate(trials, cost_source, null_harnesses=null_harnesses, target_ci_width=target_width)
    report.write_csv(table, out_dir / "summary.csv")
    try:
        suite_kinds = {suite.name: suite.kind for suite in load_config_suites(cfg, base)}
    except SuiteError as e:
        print(f"warning: cannot read the suites ({e}); the 'By suite kind' section is skipped")
        suite_kinds = {}
    md = report.render_markdown(
        table,
        trials,
        cost_source,
        datetime.now(),
        recommend_within_pp=within_pp,
        target_ci_width=target_width,
        suite_kinds=suite_kinds,
        judged=read_trials(out_dir) if isinstance(cfg.get("judge"), Mapping) else (),
        calibration=load_calibration(cfg, base),
    )
    (out_dir / "summary.md").write_text(md)
    print("\n" + md)
    print(f"saved {out_dir / 'summary.md'} and {out_dir / 'summary.csv'}")


# ---------------------------------------------------------------- main


def main() -> None:
    """Entry point: the single CLI in `hbench.cli` (same commands and flags as before)."""
    cli.main()


if __name__ == "__main__":
    main()

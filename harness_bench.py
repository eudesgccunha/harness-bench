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
import functools
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import IO, Any

if sys.version_info < (3, 12):
    sys.exit("Python 3.12+ required")

from hbench import cli, report  # noqa: E402
from hbench import runners as _runners  # noqa: E402
from hbench.core.convert import trial_from_row_lenient, trials_from_rows  # noqa: E402
from hbench.core.ids import new_run_id, sha256_json  # noqa: E402
from hbench.core.models import GraderResult, RunInfo, Target  # noqa: E402
from hbench.graders import GRADERS as GRADERS, Change, GradeContext, GradePlan, compose, grade_plan  # noqa: E402
from hbench.results import RunWriter, apply_gateway_costs  # noqa: E402
from hbench.runners import PARSERS as PARSERS, empty_usage as empty_usage, json_lines as json_lines  # noqa: E402
from hbench.runners import num as num, runner_for  # noqa: E402
from hbench.graders.base import RunStep  # noqa: E402
from hbench.runners.base import ParseUsage, build_argv  # noqa: E402

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
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


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
    return env_var_names((cfg.get("litellm") or {}).get("api_key"))


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


def validate_config(cfg: Mapping[str, Any]) -> None:
    check_cost_source(cfg.get("cost_source", "auto"))
    report_options(cfg)
    for task in cfg["tasks"]:
        if SAFE_NAME.search(task["id"]) or task["id"].startswith("."):
            sys.exit(f"invalid task id {task['id']!r}: use only letters, digits, _ . - (no leading dot)")
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
    leaked = spend_key_vars(cfg) & (
        set(cfg.get("env_passthrough", []))
        | {v for h in cfg["harnesses"].values() for val in h.get("env", {}).values() for v in env_var_names(val)}
    )
    if leaked:
        sys.exit(f"LiteLLM spend key {sorted(leaked)} must not be passed to agents")


def rmtree_inside(path: Path | str, root: Path | str) -> None:
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or not path.is_relative_to(root):
        raise RuntimeError(f"refusing to delete {path}: outside {root}")
    shutil.rmtree(path, ignore_errors=True)


def wrap(cfg: Mapping[str, Any], argv: list[str], workdir: Path | str, env_file: Path | str) -> list[str]:
    """Prefix a command with the configured sandbox (e.g. docker run ...)."""
    prefix = (cfg.get("sandbox") or {}).get("prefix")
    if not prefix:
        return argv
    fields = {"workdir": str(workdir), "env_file": str(env_file)}
    return [p.format(**fields) for p in prefix] + argv


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


def run_proc(argv: list[str], cwd: Path | None, env: Mapping[str, str], timeout: float) -> tuple[int, bool, str, str]:
    """Run argv without a shell; return (rc, timed_out, stdout, stderr)."""
    try:
        p = subprocess.run(
            argv, cwd=cwd, env=env, timeout=timeout, stdin=subprocess.DEVNULL, capture_output=True, text=True
        )
        return p.returncode, False, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return -1, True, out, err


def git_bin() -> str:
    """Absolute path of the git executable; exits with a clear message when it is missing."""
    return shutil.which("git") or sys.exit("git not found on PATH")


def git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run([git_bin(), *args], cwd=cwd, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True)


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
            git("clone", "--quiet", "--", src, str(cache))
        git("clone", "--quiet", "--local", "--", str(cache), str(workdir))
        if task.get("ref"):
            git("-C", str(workdir), "checkout", "--quiet", "--detach", task["ref"])
        shutil.rmtree(workdir / ".git")
    else:
        shutil.copytree(src, workdir, symlinks=True)


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
        root = Path(expand_env(cfg["work_dir"])).expanduser().resolve()
        if root.is_relative_to(base.resolve()):
            sys.exit(f"work_dir {root} is inside the benchmark repo; use a path outside it")
        root.mkdir(parents=True, exist_ok=True)
        return root
    return Path(tempfile.mkdtemp(prefix="harness-bench-work-"))


def run_step(
    cfg: Mapping[str, Any],
    command: str,
    env: Mapping[str, str],
    timeout: float,
    log_path: Path,
    workdir: Path,
    env_file: Path,
    secrets: Iterable[str],
) -> tuple[int, bool, str, str]:
    """Run a setup or grader command in the sandbox wrapper and write its redacted log (stdout, then stderr).
    Returns `(rc, timed_out, stdout, stderr)`, both redacted and kept apart: a score is only read from stdout."""
    write_env_file(env, env_file)
    try:
        rc, to, out, err = run_proc(wrap(cfg, ["sh", "-c", command], workdir, env_file), workdir, env, timeout)
    finally:
        env_file.unlink(missing_ok=True)
    out, err = redact(out, secrets), redact(err, secrets)
    log_path.write_text(out + "\n--- stderr ---\n" + err)
    return rc, to, out, err


def grade_trial(
    plan: GradePlan,
    task: Mapping[str, Any],
    base: Path,
    workdir: Path,
    changes: tuple[Change, ...],
    run_for: Callable[[str], RunStep],
) -> tuple[bool, list[GraderResult]]:
    """Run the task's graders over the finished workdir and compose them: `(passed, results)`.

    `run_for(prefix)` gives a grader its `run`: the engine's `run_step`, with log names starting at `prefix`.
    A grader that raises fails (only its exception type is printed, since the message may hold agent output)."""
    results = []
    for index, grader in enumerate(plan.graders):
        prefix = "" if grader.name == "hidden_tests" else f"grader-{index}-"
        ctx = GradeContext(workdir=workdir, task=task, base=base, changes=changes, run=run_for(prefix))
        try:
            results.append(grader.grade(ctx))
        except Exception as e:
            print(f"warning: grader {grader.name} failed with {type(e).__name__}; counted as failed")
            results.append(GraderResult(grader=grader.name, grader_version=grader.version, passed=False, score=0.0))
    passed, _ = compose(results, plan.mode, plan.weights, plan.threshold)
    return passed, results


def make_run_info(cfg: Mapping[str, Any], only: Sequence[str] | None, sandboxed: bool) -> RunInfo:
    """Metadata of this `run`. The config hash covers `${VAR}` references, never expanded values."""
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
        config_sha256=sha256_json(cfg),
        suite_version=sha256_json(cfg["tasks"])[:12],
        python=platform.python_version(),
        platform=platform.platform(),
        sandboxed=sandboxed,
        targets=targets,
    )


def cmd_run(args: argparse.Namespace, cfg: Mapping[str, Any], base: Path) -> None:
    """Run the matrix. The baseline git dirs of the trials live in a private temp dir that is always removed: not in
    `work_dir`, whose names an unsandboxed agent could guess."""
    with tempfile.TemporaryDirectory(prefix="harness-bench-git-") as git_root:
        _run_matrix(args, cfg, base, Path(git_root))


def _run_matrix(args: argparse.Namespace, cfg: Mapping[str, Any], base: Path, git_root: Path) -> None:
    validate_config(cfg)
    sandboxed = bool((cfg.get("sandbox") or {}).get("prefix"))
    if not sandboxed and not args.dry_run and not args.allow_unsandboxed:
        sys.exit(
            "No sandbox configured. Agents run without approval prompts and the test\n"
            "command executes agent-written code as your user. Set sandbox.prefix in the\n"
            "config (see HARNESS_BENCH.md) or pass --allow-unsandboxed inside a disposable VM."
        )
    out_dir = base / cfg.get("output_dir", "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = out_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    results_path = out_dir / "results.jsonl"
    finished = done_keys(results_path)
    reps = args.reps or cfg.get("reps", 1)
    harnesses = cfg["harnesses"]
    prices = cfg.get("prices", {})
    work_root = None if args.dry_run else resolve_work_root(cfg, base)
    if work_root:
        print(f"work dir: {work_root}")
    secrets_all = [os.environ.get(v, "") for v in spend_key_vars(cfg)]
    plans = {task["id"]: grade_plan(task) for task in cfg["tasks"]}
    writer = None if args.dry_run else RunWriter(out_dir, make_run_info(cfg, args.only, sandboxed))

    for pair in cfg["matrix"]:
        hname, model = pair["harness"], pair["model"]
        if args.only and hname not in args.only:
            continue
        h = harnesses[hname]
        price = prices.get(pair.get("price", model))
        for task in cfg["tasks"]:
            for rep in range(1, reps + 1):
                key = (task["id"], hname, model, rep)
                if key in finished and not args.dry_run:
                    print(f"skip  {key} (done)")
                    continue
                runner = runner_for(h)
                cmd = runner.command(model, task["prompt"])
                if work_root is None or writer is None:  # dry run: no work dir and no writer
                    print(shlex.join(cmd))
                    continue
                tag = f"{safe(task['id'])}__{safe(hname)}__{safe(model)}__r{rep}"
                workdir = work_root / tag
                run_tag = f"bench-{datetime.now().strftime('%Y%m%d%H%M%S')}-{tag}"
                cmd_env = base_env(cfg)
                agent_env = dict(cmd_env, BENCH_TAG=run_tag)  # Pi/OpenCode/Codex send it as x-litellm-tags
                harness_env = {k: expand_env(v).replace("{run_tag}", run_tag) for k, v in h.get("env", {}).items()}
                agent_env.update(harness_env)
                secrets = secrets_all + [v for v in harness_env.values() if v != run_tag]
                if workdir.exists():
                    rmtree_inside(workdir, work_root)
                prepare_workdir(task, base, workdir)
                env_file = work_root / f".{tag}.env"

                if task.get("setup"):
                    run_step(
                        cfg,
                        task["setup"],
                        cmd_env,
                        task.get("setup_timeout", 900),
                        logs_dir / f"{tag}.setup.log",
                        workdir,
                        env_file,
                        secrets,
                    )
                runner.prepare(workdir)
                git_dir = git_root / f"{tag}.git"
                baseline = make_baseline(workdir, git_dir)
                snap = work_root / f".{tag}.protected"
                if snap.exists():
                    rmtree_inside(snap, work_root)
                snap.mkdir()
                snapshot_protected(task, workdir, snap)

                print(f"run   {tag}", flush=True)
                t0 = time.time()
                write_env_file(agent_env, env_file)
                try:
                    rc, timed_out, stdout, stderr = run_proc(
                        wrap(cfg, cmd, workdir, env_file),
                        workdir,
                        agent_env,
                        task.get("timeout", cfg.get("timeout", 1800)),
                    )
                finally:
                    env_file.unlink(missing_ok=True)
                wall = time.time() - t0
                (logs_dir / f"{tag}.agent.jsonl").write_text(redact(stdout, secrets))
                (logs_dir / f"{tag}.agent.stderr").write_text(redact(stderr, secrets))

                usage = runner.parse_usage(stdout)
                parse_ok = usage is not None
                usage = usage or empty_usage()

                # Captured before the protected paths are restored: an edit there must still count as a change.
                changes = collect_changes(baseline)
                save_diff(baseline, logs_dir / f"{tag}.diff", secrets=secrets)
                rmtree_inside(git_dir, git_root)
                restore_protected(task, workdir, snap)
                rmtree_inside(snap, work_root)

                def run_for(
                    prefix: str,
                    tag: str = tag,
                    workdir: Path = workdir,
                    env_file: Path = env_file,
                    secrets: list[str] = secrets,
                    cmd_env: dict[str, str] = cmd_env,
                ) -> RunStep:
                    def run(command: str, timeout: int, log_name: str) -> tuple[int, bool, str, str]:
                        log = logs_dir / f"{tag}.{prefix}{safe(log_name)}.log"
                        return run_step(cfg, command, cmd_env, timeout, log, workdir, env_file, secrets)

                    return run

                passed, grader_results = grade_trial(plans[task["id"]], task, base, workdir, changes, run_for)
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
                    "sandboxed": sandboxed,
                    **usage,
                    "cost_computed": computed_cost(usage, price) if parse_ok else None,
                }
                # Built before the legacy row is written, so a bad row cannot leave the two files out of step.
                trial = trial_from_row_lenient(
                    row,
                    run_id=writer.info.run_id,  # dry runs never reach this point, so the writer exists
                    suite_version=writer.info.suite_version,
                    harness_version=runner.version(),
                    test_timed_out=test_to,
                    graders=grader_results if task.get("graders") else None,
                    warn=print,
                )
                with results_path.open("a") as f:
                    f.write(json.dumps(row) + "\n")
                writer.append(trial)
                tokens = sum(usage[k] for k in ("input", "output", "reasoning", "cache_read", "cache_write"))
                print(
                    f"done  {tag} passed={row['passed']} tokens={tokens}"
                    f" cost={row['cost_computed'] if row['cost_computed'] is not None else usage['cost_reported']}",
                    flush=True,
                )
                if not args.keep:
                    rmtree_inside(workdir, work_root)
    if work_root and not args.keep and not cfg.get("work_dir"):
        shutil.rmtree(work_root, ignore_errors=True)
    if writer:
        writer.close(datetime.now(UTC))
    if not args.dry_run and not getattr(args, "no_report", False):
        cmd_report(args, cfg, base)


# ---------------------------------------------------------------- LiteLLM


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the Authorization header must not reach another host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        return None


def check_litellm_url(base_url: str) -> None:
    u = urllib.parse.urlparse(base_url)
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in LOCAL_HOSTS):
        return
    sys.exit(f"litellm.base_url must use https (http only for localhost): {base_url}")


def litellm_get(base_url: str, key: str, path: str, params: Mapping[str, Any]) -> Any:
    check_litellm_url(base_url)
    url = base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=120) as r:
        return json.loads(r.read().decode())


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
    md = report.render_markdown(
        table, trials, cost_source, datetime.now(), recommend_within_pp=within_pp, target_ci_width=target_width
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

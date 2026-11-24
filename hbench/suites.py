"""Suites as data (ADR 0008): versioned JSON suites of benchmark tasks.

A suite is one directory with a `suite.json`. `load_suite` validates it and resolves every path, and `suite_tasks`
expands it into the task dicts the engine runs. Every path must stay inside the suite directory (a git URL as `repo`
is the one exception, and only `https://`), because a suite is data from someone else and its hidden tests are
copied into the workdir. A suite holds no symlinks: a link could reach a host path or the hidden tests.
"""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

SUITE_SCHEMA_VERSION = "1.0.0"
SUITE_FILE = "suite.json"

_NAME = re.compile(r"[a-z0-9-]+")
_SEMVER = re.compile(r"\d+\.\d+\.\d+")
# A repo that is not a directory of the suite: any URL, scp-like `git@host:path` or remote-helper `ext::cmd` form.
# Only `https://` is accepted: `file://` and `git@` would read private local repos or use the user's SSH keys,
# and `ext::` runs a command. A suite is data from someone else (config tasks are the user's own and keep all forms).
_GIT_SOURCE = re.compile(r"^([\w.+-]+://|[\w.+-]+::|git@)")
_HTTPS_URL = re.compile(r"https://\S+")
_SUITE_KEYS = frozenset({"schema_version", "name", "version", "kind", "description", "tasks"})
# The task keys the engine reads, validated here and passed through. `id` is prefixed with the suite name.
_STRING_KEYS = ("prompt", "test", "setup", "ref")
_TIMEOUT_KEYS = ("timeout", "setup_timeout", "test_timeout")
_ENGINE_KEYS = frozenset(
    {"id", "repo", "hidden_tests", "protected_paths", "graders", "grade", *_STRING_KEYS, *_TIMEOUT_KEYS}
)
_TASK_KEYS = _ENGINE_KEYS | {"tags", "solution", "provenance"}
_REQUIRED_TASK_KEYS = ("id", "repo", "prompt", "provenance")
# The task keys that hold a location: kept out of the hash of a run, so it does not depend on the checkout path.
SUITE_PATH_KEYS = ("repo", "hidden_tests", "solution")
# The keys `suite_tasks` adds to a task. A config task must not carry them: they mark a task as a suite's.
SUITE_ONLY_KEYS = frozenset({"suite", "suite_kind", "suite_label", "suite_dir", "solution"})
_PROVENANCE_KEYS = frozenset({"source", "license", "url"})


class SuiteError(ValueError):
    """A suite file or a task in it is invalid."""


@dataclass(frozen=True, kw_only=True)
class Provenance:
    """Where a task comes from and under which license."""

    source: str
    license: str
    url: str | None = None


@dataclass(frozen=True, kw_only=True)
class SuiteTask:
    """One task of a suite. `data` holds the validated engine keys (paths absolute), without `id`."""

    id: str
    data: Mapping[str, Any]
    tags: tuple[str, ...]
    solution: Path | None
    provenance: Provenance


@dataclass(frozen=True, kw_only=True)
class Suite:
    name: str
    version: str
    kind: Literal["capability", "regression"]
    description: str
    path: Path
    tasks: tuple[SuiteTask, ...]


def suite_label(suite: Suite) -> str:
    """`name@version`, as recorded in `RunInfo.suite_version`."""
    return f"{suite.name}@{suite.version}"


def _require_string(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise SuiteError(f"{where} must be a string")
    return value


def _reject_unknown(keys: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(keys) - allowed)
    if unknown:
        raise SuiteError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _kind(value: object) -> Literal["capability", "regression"]:
    if value == "capability":
        return "capability"
    if value == "regression":
        return "regression"
    raise SuiteError(f"kind must be 'capability' or 'regression', got {value!r}")


def _check_schema_version(value: object) -> None:
    match = re.fullmatch(r"(\d+)\.\d+\.\d+", value) if isinstance(value, str) else None
    if match is None or match.group(1) != SUITE_SCHEMA_VERSION.split(".")[0]:
        raise SuiteError(f"schema_version must be {SUITE_SCHEMA_VERSION.split('.')[0]}.x.y, got {value!r}")


def _reject_symlinks(target: Path, suite_dir: Path, where: str) -> None:
    """A suite has no symlinks (YAGNI: the example suites need none). The directory is copied into a workdir or
    onto one, and a link could reach a host path outside the suite or the hidden tests inside it."""
    parent = suite_dir
    for part in target.relative_to(suite_dir).parts:
        parent = parent / part
        if parent.is_symlink():
            raise SuiteError(f"{where}: symlink {parent.relative_to(suite_dir)} is not allowed in a suite")
    for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
        for name in (*dirnames, *filenames):
            link = Path(dirpath) / name
            if link.is_symlink():
                raise SuiteError(f"{where}: symlink {link.relative_to(target)} is not allowed in a suite")


def _suite_directory(suite_dir: Path, value: object, where: str) -> Path:
    """The directory `value` names, relative to the suite directory, existing, not leaving it and free of symlinks."""
    rel = _require_string(value, where)
    if not rel or Path(rel).is_absolute():
        raise SuiteError(f"{where}: {rel!r} must be a relative path")
    target = suite_dir / rel
    resolved = target.resolve()
    if not resolved.is_relative_to(suite_dir):
        raise SuiteError(f"{where}: {rel!r} must stay inside the suite directory")
    if not resolved.is_dir():
        raise SuiteError(f"{where}: {rel!r} is not a directory")
    _reject_symlinks(Path(os.path.normpath(target)), suite_dir, where)
    return resolved


def _provenance(value: object, where: str) -> Provenance:
    if not isinstance(value, dict):
        raise SuiteError(f"{where}: provenance must be an object with 'source' and 'license'")
    _reject_unknown(value, _PROVENANCE_KEYS, f"{where} provenance")
    for key in ("source", "license"):
        if not isinstance(value.get(key), str):
            raise SuiteError(f"{where}: provenance needs a string {key!r}")
    url = value.get("url")
    if url is not None and not isinstance(url, str):
        raise SuiteError(f"{where}: provenance url must be a string or null")
    return Provenance(source=value["source"], license=value["license"], url=url)


def _positive_int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SuiteError(f"{where} must be a positive integer")
    return value


def _string_list(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SuiteError(f"{where} must be a list of strings")
    return list(value)


def _repo(suite_dir: Path, repo: str, where: str) -> str:
    """An `https://` URL as it is, or the absolute path of a directory of the suite."""
    if not _GIT_SOURCE.match(repo):
        directory = _suite_directory(suite_dir, repo, f"{where} key 'repo'")
        if directory.name.endswith(".git") or os.path.lexists(directory / ".git"):
            raise SuiteError(
                f"{where} key 'repo': a suite repo directory must not be a git repository "
                "(use an https URL with ref, or a plain directory)"
            )
        return str(directory)
    if not _HTTPS_URL.fullmatch(repo):
        raise SuiteError(f"{where} key 'repo': a suite may only use an https:// URL or a directory, got {repo!r}")
    return repo


def _engine_data(suite_dir: Path, entry: Mapping[str, Any], where: str) -> dict[str, Any]:
    """The engine keys of one task entry, type-checked, with the paths made absolute."""
    data: dict[str, Any] = {}
    for key in _STRING_KEYS:
        if key in entry:
            data[key] = _require_string(entry[key], f"{where} key {key!r}")
    for key in _TIMEOUT_KEYS:
        if key in entry:
            data[key] = _positive_int(entry[key], f"{where} key {key!r}")
    if "protected_paths" in entry:
        data["protected_paths"] = _string_list(entry["protected_paths"], f"{where} key 'protected_paths'")
    if "graders" in entry:
        if not isinstance(entry["graders"], list):
            raise SuiteError(f"{where} key 'graders' must be a list")
        data["graders"] = entry["graders"]
    if "grade" in entry:
        if not isinstance(entry["grade"], dict):
            raise SuiteError(f"{where} key 'grade' must be an object")
        data["grade"] = entry["grade"]
    repo = _require_string(entry["repo"], f"{where} key 'repo'")
    data["repo"] = _repo(suite_dir, repo, where)
    if "hidden_tests" in entry:
        hidden = _suite_directory(suite_dir, entry["hidden_tests"], f"{where} key 'hidden_tests'")
        data["hidden_tests"] = str(hidden)
    return data


def _task(suite_dir: Path, index: int, entry: object) -> SuiteTask:
    where = f"tasks[{index}]"
    if not isinstance(entry, dict):
        raise SuiteError(f"{where} must be an object")
    _reject_unknown(entry, _TASK_KEYS, where)
    task_id = entry.get("id")
    if not isinstance(task_id, str) or not _NAME.fullmatch(task_id):
        raise SuiteError(f"{where}: invalid task id {task_id!r}: use only a-z, 0-9 and -")
    where = f"task {task_id!r}"
    for key in _REQUIRED_TASK_KEYS:
        if key not in entry:
            raise SuiteError(f"{where}: missing key {key!r}")
    solution = None
    if "solution" in entry:
        solution = _suite_directory(suite_dir, entry["solution"], f"{where} key 'solution'")
    return SuiteTask(
        id=task_id,
        data=MappingProxyType(_engine_data(suite_dir, entry, where)),
        tags=tuple(_string_list(entry.get("tags", []), f"{where} key 'tags'")),
        solution=solution,
        provenance=_provenance(entry["provenance"], where),
    )


def _read_document(suite_file: Path) -> dict[str, Any]:
    try:
        document = json.loads(suite_file.read_text())
    except OSError as e:
        raise SuiteError(f"{suite_file}: cannot read {SUITE_FILE} ({e.strerror or e})") from e
    except ValueError as e:
        raise SuiteError(f"{suite_file}: {SUITE_FILE} is not valid JSON ({e})") from e
    if not isinstance(document, dict):
        raise SuiteError(f"{suite_file}: {SUITE_FILE} must be a JSON object")
    return document


def load_suite(path: Path) -> Suite:
    """Load and validate the suite in directory `path`. Raises `SuiteError` naming the problem and the file."""
    suite_dir = Path(path).resolve()
    suite_file = suite_dir / SUITE_FILE
    document = _read_document(suite_file)
    try:
        _reject_unknown(document, _SUITE_KEYS, "suite")
        missing = sorted(_SUITE_KEYS - document.keys())
        if missing:
            raise SuiteError(f"missing key(s) {', '.join(missing)}")
        _check_schema_version(document["schema_version"])
        name = document["name"]
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise SuiteError(f"invalid name {name!r}: use only a-z, 0-9 and -")
        version = document["version"]
        if not isinstance(version, str) or not _SEMVER.fullmatch(version):
            raise SuiteError(f"invalid version {version!r}: use MAJOR.MINOR.PATCH")
        entries = document["tasks"]
        if not isinstance(entries, list) or not entries:
            raise SuiteError("tasks must be a non-empty list")
        tasks = tuple(_task(suite_dir, i, entry) for i, entry in enumerate(entries))
        seen: set[str] = set()
        for task in tasks:
            if task.id in seen:
                raise SuiteError(f"duplicate task id {task.id!r}")
            seen.add(task.id)
        return Suite(
            name=name,
            version=version,
            kind=_kind(document["kind"]),
            description=_require_string(document["description"], "description"),
            path=suite_dir,
            tasks=tasks,
        )
    except SuiteError as e:
        raise SuiteError(f"{suite_file}: {e}") from e


def suite_tasks(suite: Suite) -> list[dict[str, Any]]:
    """The engine task dicts of `suite`, with ids `<suite>/<task>`. The keys `suite`, `suite_kind`, `suite_label`,
    `suite_dir`, `tags` and `solution` are for the report, the run hash and `suite validate`; the engine ignores
    them."""
    tasks = []
    for task in suite.tasks:
        entry = {**task.data, "id": f"{suite.name}/{task.id}", "suite": suite.name, "suite_kind": suite.kind}
        entry["suite_label"] = suite_label(suite)
        entry["suite_dir"] = str(suite.path)
        entry["tags"] = list(task.tags)
        if task.solution is not None:
            entry["solution"] = str(task.solution)
        tasks.append(entry)
    return tasks

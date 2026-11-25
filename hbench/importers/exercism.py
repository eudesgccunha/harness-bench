"""Import the Exercism Python track (`exercises/practice/<slug>/`) from a local checkout as a capability suite.

The checkout is untrusted input and nothing is fetched: files are read through `hbench.graders._files` (no symlink in
any part, regular files with one hard link, a size limit), file names from `config.json` must be relative and stay
inside the exercise, and every file is written exclusively into the fresh output directory.
"""

import json
import os
import re
from pathlib import Path
from typing import Any

from hbench.graders._files import read_artifact, resolve_artifact
from hbench.importers.base import ImporterError, ImportRequest

MAX_FILE_BYTES = 1_000_000
MARKER = "# HIDDEN-TEST-MARKER"
PRACTICE = "exercises/practice"
_SLUG = re.compile(r"[a-z0-9-]+")
# A test file name goes into the test command, so it must not need quoting or look like an option.
_TEST_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")


def _read(source: Path, rel: str) -> str:
    try:
        return read_artifact(source, rel, MAX_FILE_BYTES)
    except ValueError as e:
        raise ImporterError(str(e)) from e


def _write(path: Path, text: str) -> None:
    """Create `path` (and its parents) and write `text`; the file must not exist yet."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as f:
            f.write(text)
    except OSError as e:
        raise ImporterError(f"cannot write {path.name} ({e.strerror or e}): duplicate file names?") from e


def _slugs(source: Path, requested: tuple[str, ...]) -> list[str]:
    """The exercises to import: the requested ones, or every directory of the practice directory."""
    practice = source / PRACTICE
    try:
        resolve_artifact(source, PRACTICE)
    except ValueError as e:
        raise ImporterError(str(e)) from e
    if not practice.is_dir():
        raise ImporterError(f"{source} has no practice directory ({PRACTICE})")
    if requested:
        slugs = list(requested)
    else:
        with os.scandir(practice) as entries:
            found = sorted(entries, key=lambda entry: entry.name)
        slugs = [entry.name for entry in found if entry.is_symlink() or entry.is_dir()]
    for slug in slugs:
        if _SLUG.fullmatch(slug) is None:
            raise ImporterError(f"invalid exercise slug {slug!r}: use only a-z, 0-9 and -")
    if not slugs:
        raise ImporterError(f"no exercises found in {practice}")
    return sorted(set(slugs))


def _reject_links(source: Path, slug: str) -> Path:
    """The exercise directory, after checking that nothing in it, or on the way to it, is a symlink."""
    rel = f"{PRACTICE}/{slug}"
    try:
        resolve_artifact(source, rel)
    except ValueError as e:
        raise ImporterError(str(e)) from e
    exercise = source / rel
    if not exercise.is_dir():
        raise ImporterError(f"exercise {slug!r} is not in the source ({rel})")
    for dirpath, dirnames, filenames in os.walk(exercise, followlinks=False):
        for name in (*dirnames, *filenames):
            if (Path(dirpath) / name).is_symlink():
                raise ImporterError(f"exercise {slug!r}: symlink {name!r} is not allowed")
    return exercise


def _names(files: dict[str, Any], key: str, exercise: Path, where: str) -> list[str]:
    names = files.get(key)
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ImporterError(f"{where}: files.{key} must be a list of file names")
    for name in names:
        try:
            resolve_artifact(exercise, name)
        except ValueError as e:
            raise ImporterError(f"{where}: files.{key}: {e}") from e
    return list(names)


def _config_files(source: Path, slug: str, exercise: Path) -> tuple[list[str], list[str], list[str]]:
    where = f"{PRACTICE}/{slug}/.meta/config.json"
    try:
        config = json.loads(_read(source, where))
    except ValueError as e:
        raise ImporterError(f"{where}: {e}") from e
    files = config.get("files") if isinstance(config, dict) else None
    if not isinstance(files, dict):
        raise ImporterError(f"{where}: no 'files' object")
    solution, tests, examples = (_names(files, key, exercise, where) for key in ("solution", "test", "example"))
    if not solution:
        raise ImporterError(f"{where}: needs at least one solution file")
    if not tests:
        raise ImporterError(f"{where}: needs at least one test file")
    if len(examples) != len(solution):
        raise ImporterError(f"{where}: files.example must list one file per files.solution entry")
    for name in tests:
        if _TEST_NAME.fullmatch(name) is None:
            raise ImporterError(f"{where}: test file name {name!r} must use only letters, digits, _ . / and -")
    return solution, tests, examples


def _prompt(source: Path, slug: str, solution: list[str]) -> str:
    docs = f"{PRACTICE}/{slug}/.docs"
    parts = [_read(source, f"{docs}/instructions.md").strip()]
    if os.path.lexists(source / docs / "instructions.append.md"):
        parts.append(_read(source, f"{docs}/instructions.append.md").strip())
    parts.append(f"Implement the solution in: {', '.join(solution)}. The tests are hidden. Do not ask questions.")
    return "\n\n".join(parts)


def _import_exercise(request: ImportRequest, slug: str) -> dict[str, Any]:
    """Write the task directory of one exercise and return its `suite.json` entry."""
    source = request.source
    exercise = _reject_links(source, slug)
    solution, tests, examples = _config_files(source, slug, exercise)
    base = f"{PRACTICE}/{slug}"
    prompt = _prompt(source, slug, solution)
    for name, example in zip(solution, examples, strict=True):
        _write(request.out / slug / "repo" / name, _read(source, f"{base}/{name}"))
        _write(request.out / slug / "solution" / name, _read(source, f"{base}/{example}"))
    for name in tests:
        text = _read(source, f"{base}/{name}")
        _write(request.out / slug / "hidden_tests" / name, text + ("" if text.endswith("\n") else "\n") + MARKER + "\n")
    return {
        "id": slug,
        "repo": f"{slug}/repo",
        "prompt": prompt,
        "hidden_tests": f"{slug}/hidden_tests",
        "test": f"python3 -m unittest -q {' '.join(tests)}",
        "solution": f"{slug}/solution",
        "tags": ["python", "exercism"],
        "provenance": {
            "source": "exercism/python",
            "license": request.license,
            "url": f"{request.source_url.rstrip('/')}/{base}",
        },
    }


def import_exercism(request: ImportRequest) -> None:
    """Write the suite of `request.exercises` (all when none) of the checkout `request.source` into `request.out`."""
    if not request.source.is_dir():
        raise ImporterError(f"source {request.source} is not a directory")
    slugs = _slugs(request.source, request.exercises)
    license_text = _read(request.source, "LICENSE")
    tasks = [_import_exercise(request, slug) for slug in slugs]
    _write(request.out / f"LICENSE.{request.name}", license_text)
    document = {
        "schema_version": "1.0.0",
        "name": request.name,
        "version": request.version,
        "kind": "capability",
        "description": f"Exercism Python practice exercises from {request.source_url} ({request.license}).",
        "tasks": tasks,
    }
    _write(request.out / "suite.json", json.dumps(document, indent=2) + "\n")

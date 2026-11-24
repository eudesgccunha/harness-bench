"""Read a file of the workdir for an artifact grader, never through a link and never past the workdir.

The workdir belongs to the untrusted agent: it can plant a symlink where a grader expects `report.html` and make
the grader read (and, for the judge, send out) a host file. The rule is the one of `safe_copy`: no symlink on the
path. `read_artifact` enforces it as it opens each part from a directory descriptor, so a swap after the path check
is refused too. A hard link (a second name for a host file) and anything that is not a regular file (a FIFO would
block the read) are refused as well.
"""

import os
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

MAX_ARTIFACT_BYTES = 10_000_000


def resolve_artifact(root: Path, rel: str) -> Path:
    """`root / rel` when `rel` is a relative path without `..` and no part of it under `root` is a symlink.
    Raises `ValueError` otherwise. The file does not have to exist."""
    if not isinstance(rel, str) or not rel or "\0" in rel:
        raise ValueError(f"invalid file name {rel!r}")
    parts = PurePosixPath(rel)
    if parts.is_absolute() or ".." in parts.parts:
        raise ValueError(f"file {rel!r} must be relative and stay inside the working directory")
    current = root
    for part in parts.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"file {rel!r} goes through the symlink {part!r}")
    return current


def artifact_file(root: Path, rel: str) -> Path:
    """The path of the existing regular file `rel` under `root` (see `resolve_artifact`), with one hard link only.
    Raises `ValueError` otherwise."""
    path = resolve_artifact(root, rel)
    try:
        info = os.lstat(path)
    except OSError as e:
        raise ValueError(f"file {rel!r} cannot be read ({e.strerror or e})") from e
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"file {rel!r} is not a regular file")
    if info.st_nlink > 1:
        raise ValueError(f"file {rel!r} has more than one hard link")
    return path


def _open_beneath(root: Path, rel: str) -> int:
    """A read-only descriptor of `rel` under `root`, opened part by part from a directory descriptor: no part of
    `rel` is followed if it is a symlink, so what is opened is what is under `root` at this moment, whatever a
    process does to the directories meanwhile. `root` itself may be reached through a link (the engine chose it).
    Raises OSError (ELOOP for a symlink, ENOTDIR for a swapped-in file). Every descriptor but the result is closed."""
    parts = PurePosixPath(rel).parts
    if not parts:
        raise IsADirectoryError(21, "is a directory")
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            old, dir_fd = dir_fd, next_fd
            os.close(old)
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def read_artifact(root: Path, rel: str, limit: int = MAX_ARTIFACT_BYTES) -> str:
    """The text of the file `rel` under `root` (invalid UTF-8 is replaced). Raises `ValueError` when the file is
    refused by the path rule, is not a regular file with one hard link, is unreadable or is longer than `limit`
    bytes.

    The file is opened part by part without following a symlink in any part (`_open_beneath`) and without blocking,
    and the checks run on the open descriptor, so a process that swaps the file or a parent directory between the
    path check and the read (a symlink, a FIFO) gets nothing: what is read is what was checked."""
    resolve_artifact(root, rel)
    try:
        fd = _open_beneath(root, rel)
    except OSError as e:
        raise ValueError(f"file {rel!r} cannot be read ({e.strerror or e})") from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"file {rel!r} is not a regular file")
        if info.st_nlink > 1:
            raise ValueError(f"file {rel!r} has more than one hard link")
        if info.st_size > limit:
            raise ValueError(f"file {rel!r} is larger than {limit} bytes")
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(limit + 1)
    except OSError as e:
        raise ValueError(f"file {rel!r} cannot be read ({e.strerror or e})") from e
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValueError(f"file {rel!r} is larger than {limit} bytes")
    return data.decode("utf-8", errors="replace")


def asset_root(task: Mapping[str, Any], base: Path) -> Path:
    """The directory a grader's own files (rubrics, schemas) are relative to: the suite directory for a suite task,
    else the benchmark directory. It is never the workdir, so the agent cannot see or change these files."""
    suite_dir = task.get("suite_dir")
    return Path(suite_dir) if isinstance(suite_dir, str) else base

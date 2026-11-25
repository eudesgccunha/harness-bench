"""Copy a tree onto a directory the agent has written to, without ever writing through a link the agent planted.

`shutil.copytree(..., dirs_exist_ok=True)` opens each destination file for writing, so a symlink (or a hard link)
left at the destination by the untrusted agent makes the copy overwrite a host file. `safe_copy_into` removes
whatever stands in the way first: a symlink anywhere on the destination path (a directory or the file itself), a
file where a directory goes, a directory where a file goes. The file is then written fresh, as a regular file
with a new inode, so a hard link to a host file is cut as well.
"""

import os
import shutil
import stat
from collections.abc import Callable, Iterable
from pathlib import Path

Ignore = Callable[[str, list[str]], Iterable[str]]  # the signature of `shutil.ignore_patterns(...)`


def _clear(path: Path) -> None:
    """Remove whatever is at `path`: a directory with its content, anything else (file, link, FIFO, socket, device)
    with `unlink`. A link is removed, never followed."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _real_directory(root: Path, rel: Path) -> Path:
    """`root / rel` as a real directory: a link or file on the way is removed, missing parts are created."""
    root.mkdir(parents=True, exist_ok=True)
    current = root
    for part in rel.parts:
        current = current / part
        if os.path.lexists(current) and (current.is_symlink() or not current.is_dir()):
            _clear(current)
        current.mkdir(exist_ok=True)
    return current


def safe_copy_into(src: Path, dest_root: Path, ignore: Ignore | None = None) -> None:
    """Copy the tree `src` onto `dest_root`, merging with what is there. `src` is trusted (a link inside it is
    followed and copied as its content); `dest_root` itself must be a real directory the caller made, and a symlink
    there raises `ValueError`."""
    if dest_root.is_symlink():
        raise ValueError(f"destination {dest_root} is a symlink")
    for dirpath, dirnames, filenames in os.walk(src, followlinks=True):
        skipped = set(ignore(dirpath, [*dirnames, *filenames])) if ignore else set()
        dirnames[:] = [d for d in dirnames if d not in skipped]
        target = _real_directory(dest_root, Path(dirpath).relative_to(src))
        for name in filenames:
            if name not in skipped:
                _clear(target / name)
                shutil.copy2(Path(dirpath) / name, target / name)


def safe_copy_to(src: Path, dest_root: Path, rel: Path) -> None:
    """Copy the file or tree `src` to `dest_root / rel`, never through a link the agent planted on the way (see
    `safe_copy_into`). A tree is merged with what is there."""
    if src.is_dir():
        safe_copy_into(src, _real_directory(dest_root, rel))
        return
    parent = _real_directory(dest_root, rel.parent)
    _clear(parent / rel.name)
    shutil.copy2(src, parent / rel.name)


def _regular_or_directory(path: Path) -> int:
    """The `st_mode` of `path` if it is a regular file or a directory (a link is never followed), else `ValueError`."""
    mode = os.lstat(path).st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise ValueError(f"{path} is not a regular file or directory (a link or special file is refused)")
    return mode


def copy_out_tree(root: Path, rel: Path, dest: Path) -> None:
    """Copy `root / rel`, written by the untrusted agent, to the host path `dest`. A symlink anywhere on the way or
    inside it, or any special file, raises `ValueError` before that entry is copied: the copy never reads a host
    file through a link the agent planted. A missing `rel` raises `FileNotFoundError`."""
    current = root
    for part in rel.parts:
        current = current / part
        _regular_or_directory(current)
    if not stat.S_ISDIR(_regular_or_directory(current)):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current, dest)
        return
    for dirpath, dirnames, filenames in os.walk(current):
        for name in (*dirnames, *filenames):
            _regular_or_directory(Path(dirpath) / name)
        target = dest / Path(dirpath).relative_to(current)
        target.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            shutil.copy2(Path(dirpath) / name, target / name)

"""Unpack an uploaded zip archive into a directory."""

import zipfile
from pathlib import Path


class UnsafeArchiveError(ValueError):
    """The archive has a member that would be written outside the destination directory."""


def extract_all(archive: Path, dest: Path) -> list[Path]:
    """Extract every member of `archive` into `dest` and return the files written, in archive order.

    Raises `UnsafeArchiveError` before writing anything when a member would land outside `dest`."""
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        members = [(info, (root / info.filename).resolve()) for info in zf.infolist()]
        for info, target in members:
            if not target.is_relative_to(root):
                raise UnsafeArchiveError(f"member {info.filename!r} would be written outside {dest}")
        written: list[Path] = []
        for info, target in members:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            written.append(dest / target.relative_to(root))
    return written

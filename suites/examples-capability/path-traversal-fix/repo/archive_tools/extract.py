"""Unpack an uploaded zip archive into a directory."""

import zipfile
from pathlib import Path


class UnsafeArchiveError(ValueError):
    """The archive has a member that would be written outside the destination directory."""


def extract_all(archive: Path, dest: Path) -> list[Path]:
    """Extract every member of `archive` into `dest` and return the files written, in archive order."""
    written: list[Path] = []
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = dest / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            written.append(target)
    return written

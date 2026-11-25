"""What every importer shares: the request it gets and the error it raises."""

from dataclasses import dataclass
from pathlib import Path


class ImporterError(ValueError):
    """An import was refused or failed; the message says why. Nothing is left in the output directory."""


@dataclass(frozen=True, kw_only=True)
class ImportRequest:
    """One import. `source` is a local checkout the user cloned and whose license the user reviewed; `out` is a new
    suite directory (it must not exist). An importer writes `suite.json` and the task directories into `out`."""

    source: Path
    out: Path
    name: str
    version: str
    license: str
    source_url: str
    exercises: tuple[str, ...] = ()

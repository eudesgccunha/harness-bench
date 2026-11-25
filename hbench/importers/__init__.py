"""Task importers (ADR 0013): turn a local checkout of a task pack into a new suite directory.

An importer is a callable `(ImportRequest) -> None` that writes `suite.json` and the task directories into
`request.out`. Importers never use the network: the user clones the pack and reviews its license. `run_import` checks
the request, creates `out` (it must not exist), runs the importer and loads the result as a suite; on any failure `out`
is removed again. Built-in importers are in `IMPORTERS`; third parties register a factory `(config) -> importer` in the
entry-point group `harness_bench.importers` (`hbench/plugins.py`).
"""

import re
import shutil
from collections.abc import Callable
from typing import TypeGuard

from hbench.importers import exercism
from hbench.importers.base import ImporterError, ImportRequest
from hbench.plugins import IMPORTER_GROUP, create_plugin
from hbench.suites import Suite, SuiteError, load_suite

__all__ = ["IMPORTERS", "ImportRequest", "ImporterError", "Importer", "exercism", "run_import"]

Importer = Callable[[ImportRequest], None]
IMPORTERS: dict[str, Importer] = {"exercism": exercism.import_exercism}

_NAME = re.compile(r"[a-z0-9-]+")
_SEMVER = re.compile(r"\d+\.\d+\.\d+")
_LICENSE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+() -]{0,63}")
_HTTPS_URL = re.compile(r"https://\S+")


def _is_importer(obj: object) -> TypeGuard[Importer]:
    return callable(obj)


def _importer(kind: str) -> Importer:
    if kind in IMPORTERS:
        return IMPORTERS[kind]
    return create_plugin(IMPORTER_GROUP, kind, tuple(IMPORTERS), {}, _is_importer, "callable")


def _check(request: ImportRequest) -> None:
    for label, value, pattern in (
        ("name", request.name, _NAME),
        ("version", request.version, _SEMVER),
        ("license", request.license, _LICENSE),
        ("source URL", request.source_url, _HTTPS_URL),
    ):
        if pattern.fullmatch(value) is None:
            raise ImporterError(f"invalid {label} {value!r}: expected {pattern.pattern}")


def run_import(kind: str, request: ImportRequest) -> Suite:
    """Run the importer `kind`. Returns the loaded suite; raises `ImporterError` (or `PluginError` for an unknown or
    broken plugin) and leaves no output directory behind when anything fails."""
    _check(request)
    importer = _importer(kind)
    try:
        request.out.mkdir()
    except FileExistsError as e:
        raise ImporterError(f"output {request.out} already exists: choose a new directory") from e
    except OSError as e:
        raise ImporterError(f"cannot create {request.out} ({e.strerror or e})") from e
    done = False
    try:
        importer(request)
        suite = load_suite(request.out)
        done = True
        return suite
    except ImporterError:
        raise
    except SuiteError as e:
        raise ImporterError(f"the imported suite is not valid: {e}") from e
    except Exception as e:
        raise ImporterError(f"importer {kind!r} failed: {type(e).__name__}: {e}") from e
    finally:
        if not done:
            shutil.rmtree(request.out, ignore_errors=True)

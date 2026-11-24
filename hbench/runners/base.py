"""The runner protocol and the adapter built from a harness config entry.

A runner builds the agent argv and parses its output. It never starts a process: the engine
runs the argv through `wrap()` and the env allowlist (ADR 0007).
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from hbench.runners._usage import Usage

ParseUsage = Callable[[str], Usage | None]


@runtime_checkable
class HarnessRunner(Protocol):
    """A harness as an adapter: argv builder and usage parser."""

    name: str

    def prepare(self, workdir: Path) -> None:
        """Hook run on the host before the agent starts (default: nothing to do)."""

    def command(self, model: str, prompt: str) -> list[str]:
        """The agent argv for this model and prompt."""

    def parse_usage(self, stdout: str) -> Usage | None:
        """Usage from the agent stdout, or None when the format has none."""

    def version(self) -> str:
        """The harness version recorded in the results."""


def build_argv(template: list[str], model: str, prompt: str) -> list[str]:
    """Fill `{model}`, `{provider}`, `{model_name}` and `{prompt}` in each template part."""
    provider, _, model_name = model.partition("/") if "/" in model else ("", "", model)
    fields = {"model": model, "provider": provider, "model_name": model_name, "prompt": prompt}
    return [part.format(**fields) for part in template]


class TemplateRunner:
    """Runner for a harness config entry: `command` template, `parser` name, optional `version`."""

    def __init__(self, harness_cfg: Mapping[str, Any], parse: ParseUsage) -> None:
        template = harness_cfg.get("command")
        if not isinstance(template, list) or not all(isinstance(part, str) for part in template):
            raise ValueError("a harness needs a 'command' list of strings")
        self.name: str = harness_cfg["parser"]
        self._template: list[str] = template
        self._version: str = harness_cfg.get("version", "unknown")
        self._parse = parse

    def prepare(self, workdir: Path) -> None:
        """Template harnesses need no preparation."""

    def command(self, model: str, prompt: str) -> list[str]:
        return build_argv(self._template, model, prompt)

    def parse_usage(self, stdout: str) -> Usage | None:
        return self._parse(stdout)

    def version(self) -> str:
        return self._version

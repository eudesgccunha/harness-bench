"""Plugins through entry points (ADR 0007, decision 7).

Third-party packages register a factory `(config) -> HarnessRunner | Grader | redactor` in the groups
`harness_bench.runners`, `harness_bench.graders` and `harness_bench.redactors` (a redactor is a callable
`(text: str) -> str` that runs on an artifact before it is sent to the judge). Built-in names always win: callers
only come here for a name that is not built in, so a plain run never imports third-party code. Every failure is a
`PluginError` with a message that names the plugin, never a traceback from the engine.
"""

from collections.abc import Callable, Collection, Mapping
from importlib.metadata import EntryPoint, entry_points
from typing import Any, TypeGuard

RUNNER_GROUP = "harness_bench.runners"
GRADER_GROUP = "harness_bench.graders"
REDACTOR_GROUP = "harness_bench.redactors"


class PluginError(ValueError):
    """A plugin is missing, ambiguous, broken or of the wrong type."""


_announced: set[str] = set()


def reset_announced() -> None:
    """Forget which plugins were already announced (for tests that install several)."""
    _announced.clear()


def _origin(ep: EntryPoint) -> str:
    dist = ep.dist
    return f"{dist.name} {dist.version}" if dist else "an unknown distribution"


def _kind(group: str) -> str:
    return group.rsplit(".", 1)[-1].removesuffix("s")


def _find(group: str, name: str, builtins: Collection[str]) -> EntryPoint:
    points = entry_points(group=group)
    for ep in points:
        if ep.name in builtins:
            raise PluginError(f"plugin {group}:{ep.name} from {_origin(ep)} uses a built-in name; rename it")
    matches = [ep for ep in points if ep.name == name]
    if not matches:
        raise PluginError(
            f"unknown {_kind(group)} type {name!r}: not built in ({', '.join(sorted(builtins))}) "
            f"and no plugin registered in the entry-point group {group!r}"
        )
    if len(matches) > 1:
        origins = ", ".join(_origin(ep) for ep in matches)
        raise PluginError(f"plugin {group}:{name} is registered by more than one package: {origins}")
    return matches[0]


def load_plugin(group: str, name: str, builtins: Collection[str]) -> Callable[[Mapping[str, Any]], object]:
    """The factory registered as `name` in `group`. Prints the plugin's distribution once per process."""
    ep = _find(group, name, builtins)
    try:
        factory = ep.load()
    except Exception as e:
        raise PluginError(f"plugin {group}:{name} failed to load: {type(e).__name__}: {e}") from e
    if not callable(factory):
        raise PluginError(f"plugin {group}:{name} is not callable: expected a factory taking the config")
    label = f"{group}:{name}"
    if label not in _announced:
        _announced.add(label)
        print(f"plugin {label} from {_origin(ep)}")
    plugin_factory: Callable[[Mapping[str, Any]], object] = factory
    return plugin_factory


def create_plugin[T](
    group: str,
    name: str,
    builtins: Collection[str],
    config: Mapping[str, Any],
    accepts: Callable[[object], TypeGuard[T]],
    expected: str,
) -> T:
    """Load the plugin `name`, call its factory with `config` and check the result with `accepts`."""
    factory = load_plugin(group, name, builtins)
    try:
        built = factory(config)
    except Exception as e:
        raise PluginError(f"plugin {group}:{name} failed to build: {type(e).__name__}: {e}") from e
    if not accepts(built):
        raise PluginError(f"plugin {group}:{name} returned a {type(built).__name__}, not a {expected}")
    return built


def _is_redactor(obj: object) -> TypeGuard[Callable[[str], str]]:
    return callable(obj)


def redactor_for(name: str, config: Mapping[str, Any]) -> Callable[[str], str]:
    """The redactor of the plugin `name` (there are no built-in ones), built from `config` (the `judge` section)."""
    return create_plugin(REDACTOR_GROUP, name, (), config, _is_redactor, "callable")

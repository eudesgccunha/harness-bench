"""The `Sink` protocol (ADR 0011): where the trials of a run go besides `trials.jsonl`.

A sink receives only contract objects, which never hold artifact content, agent output or the judge's reason, so no
sink can leak them. `write` must be idempotent by `trial_id`: resume and `export` send a trial again.
"""

from typing import Protocol, runtime_checkable

from hbench.core.models import RunInfo, TrialResult


class SinkError(ValueError):
    """A `sinks` entry is invalid, or a sink could not do its work. The message never holds a secret."""


@runtime_checkable
class Sink(Protocol):
    def open(self, info: RunInfo) -> None:
        """Start receiving the trials of the run `info`. May be called again for a run that was seen before."""

    def write(self, trial: TrialResult) -> None:
        """Record one trial. Sending the same trial again must not duplicate it."""

    def close(self, info: RunInfo) -> None:
        """The run is over (`info.finished_at` is set when it finished): flush and release everything."""

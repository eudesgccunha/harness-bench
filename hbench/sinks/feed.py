"""`SinkFeed`: sends the run and its trials to every sink, and never lets a sink stop the run (ADR 0011, decision 5).

A sink that raises (disk full, a locked database, a collector that answers 500, times out or redirects) is reported
once through `warn` and not used again for this run. Nothing else changes: `trials.jsonl`, `results.jsonl` and the
report are written before and independently of any sink, and `export` replays a run into the sinks later.
"""

import contextlib
from collections.abc import Callable, Sequence

from hbench.core.models import RunInfo, TrialResult
from hbench.sinks.base import Sink


class SinkFeed:
    def __init__(self, sinks: Sequence[Sink], warn: Callable[[str], None]) -> None:
        self._live = list(sinks)
        self._warn = warn
        self._info: RunInfo | None = None
        self.failed = 0

    def _each(self, action: Callable[[Sink], None]) -> None:
        for sink in list(self._live):
            try:
                action(sink)
            except Exception as e:  # a sink is third-party code in the plugin case: nothing it raises ends the run
                self._live.remove(sink)
                self.failed += 1
                self._release(sink)
                self._warn(
                    f"warning: sink {type(sink).__name__} failed and is off for this run "
                    f"(replay later with `python3 -m hbench export`): {type(e).__name__}: {e}"
                )

    def _release(self, sink: Sink) -> None:
        """Best effort: let a dropped sink release its file or connection; its own failure is not reported again."""
        if self._info is not None:
            with contextlib.suppress(Exception):
                sink.close(self._info)

    def open(self, info: RunInfo) -> None:
        self._info = info
        self._each(lambda sink: sink.open(info))

    def write(self, trial: TrialResult) -> None:
        self._each(lambda sink: sink.write(trial))

    def close(self, info: RunInfo) -> None:
        self._each(lambda sink: sink.close(info))

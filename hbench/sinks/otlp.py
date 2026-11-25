"""The `otlp` sink: OpenTelemetry traces in the OTLP/JSON encoding, with no OpenTelemetry package (ADR 0011).

Each trial is one span, child of the span of the run; the trace id is the run id. Spans go to
`<run_dir>/traces.otlp.jsonl` (one `ExportTraceServiceRequest` per line, each span id once) and, when `endpoint` is
set, to `POST <endpoint>/v1/traces`. A span holds ids, status, times, the model, token counts and the cost, and
nothing else: no prompt, response, transcript, artifact or log content, and there is no option to add it. An unknown
number is absent from the span, never 0.
"""

import hashlib
import json
import os
import re
import urllib.error
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from hbench.core.models import RunInfo, TrialResult, TrialStatus
from hbench.net import NetError, post_bounded, url_allowed
from hbench.results import LogCursor, locked_log
from hbench.sinks.base import SinkError

TRACES_FILE = "traces.otlp.jsonl"
DEFAULT_TIMEOUT = 10.0
_ENV_REF = re.compile(r"\$\{(\w+)\}")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SPAN_KIND_INTERNAL = 1
_STATUS_UNSET = 0
_STATUS_ERROR = 2


def run_span_id(run_id: str) -> str:
    """The span id of the run: the first 16 hex characters of `sha256("run:" + run_id)`."""
    return hashlib.sha256(f"run:{run_id}".encode()).hexdigest()[:16]


def _unix_nano(moment: datetime) -> int:
    """Nanoseconds since the epoch, by integer arithmetic (a float has no room for them)."""
    return (moment - _EPOCH) // timedelta(microseconds=1) * 1000


def _attribute(key: str, value: str | bool | int | float) -> dict[str, Any]:
    if isinstance(value, bool):
        encoded: dict[str, Any] = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}  # OTLP/JSON carries 64-bit integers as strings
    elif isinstance(value, float):
        encoded = {"doubleValue": value}
    else:
        encoded = {"stringValue": value}
    return {"key": key, "value": encoded}


def trial_attributes(trial: TrialResult, sandbox: str) -> dict[str, str | bool | int | float]:
    """The attributes of a trial span. Token counts only when the harness output was parsed, the cache count only
    when it is above 0 (a harness that does not report it reads 0), the cost only when known."""
    usage = trial.usage
    attributes: dict[str, str | bool | int | float] = {
        "eval.run_id": trial.run_id,
        "eval.trial_id": trial.trial_id,
        "eval.target_id": f"{trial.target.harness}/{trial.target.model}",
        "eval.task_id": trial.task_id,
        "eval.rep": trial.rep,
        "eval.status": trial.status.value,
        "eval.passed": trial.passed,
        "eval.suite_version": trial.suite_version,
        "eval.sandbox": sandbox,
        "gen_ai.request.model": trial.target.model,
    }
    if usage.parsed:
        attributes["gen_ai.usage.input_tokens"] = usage.input_tokens
        attributes["gen_ai.usage.output_tokens"] = usage.output_tokens
        if usage.cache_read_tokens > 0:
            attributes["gen_ai.usage.cache_read.input_tokens"] = usage.cache_read_tokens
    if usage.cost_usd is not None:
        attributes["eval.cost_usd"] = usage.cost_usd
    return attributes


def _request(info: RunInfo, span: Mapping[str, Any]) -> dict[str, Any]:
    resource = [("service.name", "harness-bench"), ("service.version", info.harness_bench_version)]
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attribute(k, v) for k, v in resource]},
                "scopeSpans": [{"scope": {"name": "hbench.sinks.otlp"}, "spans": [span]}],
            }
        ]
    }


def trial_request(info: RunInfo, trial: TrialResult) -> dict[str, Any]:
    """The `ExportTraceServiceRequest` of one trial. Start and end come from `started_at` and `latency_s`."""
    start = _unix_nano(trial.started_at)
    failed = trial.status is TrialStatus.ERROR
    span = {
        "traceId": trial.run_id,
        "spanId": trial.trial_id,
        "parentSpanId": run_span_id(trial.run_id),
        "name": "eval.trial",
        "kind": _SPAN_KIND_INTERNAL,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + round(trial.latency_s * 1_000_000_000)),
        "attributes": [_attribute(k, v) for k, v in trial_attributes(trial, info.sandbox).items()],
        "status": {"code": _STATUS_ERROR if failed else _STATUS_UNSET},
    }
    return _request(info, span)


def run_request(info: RunInfo, end: datetime) -> dict[str, Any]:
    """The `ExportTraceServiceRequest` of the run span, which ends at `end`."""
    attributes = {"eval.run_id": info.run_id, "eval.suite_version": info.suite_version, "eval.sandbox": info.sandbox}
    span = {
        "traceId": info.run_id,
        "spanId": run_span_id(info.run_id),
        "name": "eval.run",
        "kind": _SPAN_KIND_INTERNAL,
        "startTimeUnixNano": str(_unix_nano(info.started_at)),
        "endTimeUnixNano": str(_unix_nano(end)),
        "attributes": [_attribute(k, v) for k, v in attributes.items()],
        "status": {"code": _STATUS_UNSET},
    }
    return _request(info, span)


def _span_ids(request: object) -> set[str]:
    """The span ids of one request line; anything that does not have the OTLP shape holds none."""
    found: set[str] = set()
    if not isinstance(request, dict):
        return found
    for resource in _items(request.get("resourceSpans")):
        for scope in _items(resource.get("scopeSpans")):
            found |= {s["spanId"] for s in _items(scope.get("spans")) if isinstance(s.get("spanId"), str)}
    return found


def _items(value: object) -> list[dict[str, Any]]:
    """The objects of a JSON array; anything else (a missing key, a string, a number inside) holds none."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


class OtlpSink:
    def __init__(self, out_dir: Path, endpoint: str | None, headers: Mapping[str, str], timeout: float) -> None:
        self.out_dir = out_dir
        self.endpoint = endpoint
        self.headers = dict(headers)
        self.timeout = timeout
        self._opened: tuple[RunInfo, Path] | None = None
        self._cursor = LogCursor()
        self._post_failed = False  # after one failed POST the file is still written but nothing is sent again

    @classmethod
    def from_section(cls, section: Mapping[str, Any], out_dir: Path) -> Self:
        endpoint = section.get("endpoint")
        if endpoint is not None and (not isinstance(endpoint, str) or not url_allowed(endpoint)):
            raise SinkError("an otlp endpoint must be a URL that uses https (http only for localhost)")
        headers = _headers(section.get("headers", {}))
        if headers and endpoint is None:
            raise SinkError("otlp headers need an endpoint: they are only sent with the POST")
        timeout = section.get("timeout", DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise SinkError(f"an otlp timeout must be a number of seconds above 0, got {timeout!r}")
        return cls(out_dir, endpoint, headers, float(timeout))

    def open(self, info: RunInfo) -> None:
        run_dir = self.out_dir / info.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self._post_failed = False
        self._cursor = LogCursor()  # the span ids already in the file: each append reads only what was added
        self._opened = (info, run_dir / TRACES_FILE)

    def _state(self) -> tuple[RunInfo, Path]:
        if self._opened is None:
            raise SinkError("the otlp sink is not open")
        return self._opened

    def _append(self, span_id: str, request: Mapping[str, Any]) -> None:
        """Add the request to the file unless its span is already there (one `write` call: one line). Workers share the
        file (ADR 0012), so what is there is read again under the lock: a span another worker wrote is not repeated, and
        a line cut short by a crash is dropped."""
        with locked_log(self._state()[1], self._cursor) as (f, complete):
            for line in complete.splitlines():  # only the lines added since the last append
                try:
                    self._cursor.ids |= _span_ids(json.loads(line))
                except ValueError:  # a line that is no JSON holds no span
                    continue
            if span_id in self._cursor.ids:
                return
            f.write(f"{json.dumps(request)}\n")
            f.flush()
            os.fsync(f.fileno())
            self._cursor.ids.add(span_id)

    def _send(self, request: Mapping[str, Any]) -> None:
        """POST the request when there is an endpoint. A failure raises a `SinkError` that holds no header."""
        if self.endpoint is None or self._post_failed:
            return
        self._post_failed = True  # cleared below once the POST succeeds, so any failure leaves it set
        url = self.endpoint.rstrip("/") + "/v1/traces"
        headers = {"Content-Type": "application/json", **self.headers}
        try:
            post_bounded(url, headers, json.dumps(request).encode(), self.timeout)
        except urllib.error.HTTPError as e:
            raise SinkError(f"the collector answered HTTP {e.code}") from None
        except (TimeoutError, NetError) as e:
            raise SinkError(f"the collector exchange failed: {e}") from None
        except Exception as e:
            raise SinkError(f"the collector exchange failed: {type(e).__name__}") from None
        self._post_failed = False

    def write(self, trial: TrialResult) -> None:
        info = self._state()[0]
        request = trial_request(info, trial)
        self._append(trial.trial_id, request)
        self._send(request)

    def close(self, info: RunInfo) -> None:
        """Write and send the run span of a finished run. An unfinished run (Ctrl-C, an error, a dropped sink) has no
        end yet: its span would be written with a wrong one and `_append` would then keep it, so nothing is written
        and a later close (resume, `export`) adds the right one."""
        self._state()
        if info.finished_at is None:
            return
        request = run_request(info, info.finished_at)
        self._append(run_span_id(info.run_id), request)
        self._send(request)


def _headers(raw: object) -> dict[str, str]:
    """The headers of the section with every `${VAR}` expanded from the environment. A value must be exactly one
    reference, so the redaction of logs knows the secret it stands for."""
    if not isinstance(raw, Mapping):
        raise SinkError("otlp headers must be an object of name: ${VAR}")
    headers: dict[str, str] = {}
    for name, value in raw.items():
        match = _ENV_REF.fullmatch(value) if isinstance(value, str) else None
        if not name or match is None:
            raise SinkError(
                f"otlp header {name!r}: the value must be an environment variable, written exactly ${{VAR}}"
            )
        expanded = os.environ.get(match.group(1), "")
        if not expanded:
            raise SinkError(f"otlp header {name!r} is empty: set the environment variable {match.group(1)}")
        headers[name] = expanded
    return headers

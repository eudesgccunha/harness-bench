"""Fake OTLP/HTTP collector for tests.

POST /v1/traces: every request is recorded in `Collector.requests` as a `Recorded` (path, headers, parsed JSON body).
The reply is scripted by `Collector.mode`: `ok` (200, the default), `http500`, `redirect` (302 to
`Collector.redirect_url`), `slow` (answers after 2 seconds), `drip` (sends the reply one byte every 50 ms) and `big`
(a 2 MB reply). The server runs in the process of the test, so the engine in a subprocess reaches it over the loopback.
"""

import json
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class Recorded:
    path: str
    headers: dict[str, str]
    body: dict[str, Any]


@dataclass
class Collector:
    requests: list[Recorded] = field(default_factory=list)
    mode: str = "ok"
    redirect_url: str = ""

    def span_ids(self) -> list[str]:
        """The id of every span received, in order (a span received twice is listed twice)."""
        return [
            span["spanId"]
            for r in self.requests
            for resource in r.body["resourceSpans"]
            for scope in resource["scopeSpans"]
            for span in scope["spans"]
        ]


def serve() -> tuple[ThreadingHTTPServer, int, Collector]:
    """Start a collector on a free loopback port; the caller runs `serve_forever` in a thread."""
    collector = Collector()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {k.lower(): v for k, v in self.headers.items()}
            collector.requests.append(Recorded(self.path, headers, json.loads(body)))
            if self.path != "/v1/traces":
                self.send_response(404)
                self.end_headers()
            elif collector.mode == "http500":
                self.send_response(500)
                self.end_headers()
            elif collector.mode == "redirect":
                self.send_response(302)
                self.send_header("Location", collector.redirect_url)
                self.end_headers()
            else:
                self.reply()

        def reply(self) -> None:
            if collector.mode == "slow":
                time.sleep(2)
            out = b"x" * 2_000_000 if collector.mode == "big" else b"{}"
            self.send_response(200)
            self.end_headers()
            try:
                if collector.mode == "drip":
                    for i in range(len(out)):
                        self.wfile.write(out[i : i + 1])
                        self.wfile.flush()
                        time.sleep(0.05)
                else:
                    self.wfile.write(out)
            except (BrokenPipeError, ConnectionResetError):  # the client gave up
                pass

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    return server, server.server_address[1], collector

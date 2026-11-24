"""Fake LiteLLM /spend/logs/v2 for tests: 2 rows of $0.01 per tag in FAKE_TAGS_FILE,
plus one unrelated row that must be ignored."""

import json
import os
from typing import Any
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY = "test-key"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            self.send_response(401)
            self.end_headers()
            return
        path = os.environ["FAKE_TAGS_FILE"]
        tags = open(path).read().split() if os.path.exists(path) else []
        rows = [
            {"request_tags": json.dumps([t]), "spend": 0.01, "prompt_tokens": 100, "completion_tokens": 10}
            for t in tags
            for _ in range(2)
        ]
        rows.append({"request_tags": ["unrelated"], "spend": 9.0, "prompt_tokens": 1})
        body = json.dumps({"data": rows, "total_pages": 1}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def serve(port: int = 0) -> tuple[HTTPServer, int]:
    """Start server; return (server, port)."""
    srv = HTTPServer(("127.0.0.1", port), Handler)
    return srv, srv.server_address[1]

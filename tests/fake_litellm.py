"""Fake LiteLLM for tests.

GET /spend/logs/v2: 2 rows of $0.01 per tag in FAKE_TAGS_FILE, plus one unrelated row that must be ignored.
POST /chat/completions (the LLM judge): every request body is appended as a JSON line to $FAKE_JUDGE_LOG, and the
reply is scripted by $FAKE_JUDGE_MODE: `ok` (score 0.9, the default), `low` (score 0.1), `text` (not JSON),
`extra` (JSON with text around it), `bad-score` (score 7), `bad-passed` (passed is a string), `http500`,
`redirect` (302 to $FAKE_REDIRECT_URL), `slow` (answers after 2 seconds), `drip` (sends the
reply one byte every 50 ms) and `drip-headers` (the same for the status line and a header). The reason is
`JUDGE-REASON-CANARY` unless $FAKE_JUDGE_REASON sets it, so a test can prove it never reaches the results and that
the grader log redacts it.
"""

import json
import os
import time
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

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        if self.path != "/chat/completions" or self.headers.get("Authorization") != f"Bearer {KEY}":
            self.send_response(404 if self.path != "/chat/completions" else 401)
            self.end_headers()
            return
        if os.environ.get("FAKE_JUDGE_LOG"):
            with open(os.environ["FAKE_JUDGE_LOG"], "a") as f:
                f.write(body.replace("\n", " ") + "\n")
        mode = os.environ.get("FAKE_JUDGE_MODE", "ok")
        if mode == "http500":
            self.send_response(500)
            self.end_headers()
            return
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", os.environ["FAKE_REDIRECT_URL"])
            self.end_headers()
            return
        if mode == "drip-headers":  # the status line and a header, a byte at a time: no socket read ever times out
            try:
                for byte in b"HTTP/1.0 200 OK\r\nX-Slow: " + b"a" * 200:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if mode == "slow":
            time.sleep(2)
        reply = {
            "score": 0.1 if mode == "low" else 0.9,
            "passed": mode != "low",
            "reason": os.environ.get("FAKE_JUDGE_REASON", "JUDGE-REASON-CANARY"),
        }
        if mode == "bad-score":
            reply["score"] = 7
        if mode == "bad-passed":
            reply["passed"] = "yes"
        content = json.dumps(reply)
        if mode == "text":
            content = "I think this is good."
        if mode == "extra":
            content = "Sure! " + content
        out = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
        self.send_response(200)
        self.end_headers()
        try:
            if mode == "drip":
                for i in range(len(out)):
                    self.wfile.write(out[i : i + 1])
                    self.wfile.flush()
                    time.sleep(0.05)
            else:
                self.wfile.write(out)
        except (BrokenPipeError, ConnectionResetError):  # the client gave up (the slow modes outlast its timeout)
            pass

    def log_message(self, format: str, *args: Any) -> None:
        pass


def serve(port: int = 0) -> tuple[HTTPServer, int]:
    """Start server; return (server, port)."""
    srv = HTTPServer(("127.0.0.1", port), Handler)
    return srv, srv.server_address[1]

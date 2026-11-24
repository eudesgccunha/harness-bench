"""Tests for LiteLLM access, `reconcile` and the LiteLLM-only wrapper, against a local scripted server."""

import json
import shutil
import sys
import threading
import unittest
import urllib.error
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from unittest import mock

import fixtures
import harness_bench as hb
import harness_bench_litellm as hbl

KEY = "k-test"
Reply = tuple[int, Any, dict[str, str]]


class ScriptedServer:
    """Localhost HTTP server answering GET <path> from a table; records (path, params, auth)."""

    def __init__(self) -> None:
        self.routes: dict[str, list[Reply]] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path, _, query = self.path.partition("?")
                outer.requests.append((path, query, self.headers.get("Authorization")))
                replies = outer.routes.get(path)
                status, body, headers = (
                    replies.pop(0) if replies and len(replies) > 1 else (replies or [(404, {}, {})])[0]
                )
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *a: Any) -> None:
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True).start()

    def route(self, path: str, *replies: Reply) -> None:
        self.routes[path] = list(replies)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def ok(body: Any) -> Reply:
    return 200, body, {}


class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ScriptedServer()
        self.addCleanup(self.server.close)


class LitellmGetTests(ServerTestCase):
    def test_sends_bearer_key_and_decodes_json(self) -> None:
        self.server.route("/p", ok({"a": 1}))
        data = hb.litellm_get(self.server.url, KEY, "/p", {"x": "1"})
        self.assertEqual((data, self.server.requests[0]), ({"a": 1}, ("/p", "x=1", f"Bearer {KEY}")))

    def test_redirects_are_refused(self) -> None:
        self.server.route("/p", (302, {}, {"Location": "http://example.invalid/steal"}))
        with self.assertRaises(urllib.error.HTTPError):
            hb.litellm_get(self.server.url, KEY, "/p", {})

    def test_plain_http_to_remote_host_exits(self) -> None:
        with self.assertRaises(SystemExit):
            hb.litellm_get("http://litellm.example.com", KEY, "/p", {})


class LitellmRowsTests(ServerTestCase):
    START = datetime.fromtimestamp(1_700_000_000, UTC)

    def rows(self) -> list[Any]:
        rows: list[Any] = hb.litellm_rows(self.server.url, KEY, self.START, self.START)
        return rows

    def test_follows_pages_until_total_pages(self) -> None:
        self.server.route("/spend/logs/v2", ok({"data": [1], "total_pages": 2}), ok({"data": [2], "total_pages": 2}))
        self.assertEqual(self.rows(), [1, 2])

    def test_stops_on_an_empty_page(self) -> None:
        self.server.route("/spend/logs/v2", ok({"data": [], "total_pages": 5}))
        self.assertEqual(self.rows(), [])

    def test_accepts_a_bare_list_response(self) -> None:
        self.server.route("/spend/logs/v2", ok([7, 8]))
        self.assertEqual(self.rows(), [7, 8])

    def test_falls_back_to_legacy_endpoint_when_v2_fails(self) -> None:
        self.server.route("/spend/logs", ok({"data": ["legacy"]}))
        with fixtures.quiet() as out:
            rows = self.rows()
        self.assertEqual((rows, "trying legacy" in out.getvalue()), (["legacy"], True))

    def test_legacy_endpoint_may_return_a_bare_list(self) -> None:
        self.server.route("/spend/logs", ok(["l1"]))
        with fixtures.quiet():
            self.assertEqual(self.rows(), ["l1"])


class ReconcileTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = fixtures.tmpdir(self)
        (self.base / "results").mkdir()
        self.cfg: dict[str, Any] = {"litellm": {"base_url": self.server.url, "api_key": KEY}}

    def reconcile(self) -> str:
        with fixtures.quiet() as out:
            hb.cmd_reconcile(mock.Mock(cost_source=None), self.cfg, self.base)
        return out.getvalue()

    def test_missing_litellm_section_exits(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            hb.cmd_reconcile(mock.Mock(), {}, self.base)
        self.assertIn("no 'litellm' section", str(cm.exception.code))

    def test_empty_key_exits(self) -> None:
        self.cfg["litellm"]["api_key"] = "${HB_SURELY_UNSET_VAR}"
        with self.assertRaises(SystemExit) as cm:
            hb.cmd_reconcile(mock.Mock(), self.cfg, self.base)
        self.assertIn("empty", str(cm.exception.code))

    def test_results_without_run_tags_exit(self) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl", fixtures.run_row(run_tag=None))
        with self.assertRaises(SystemExit) as cm:
            hb.cmd_reconcile(mock.Mock(), self.cfg, self.base)
        self.assertIn("no runs with run_tag", str(cm.exception.code))

    def test_matching_rows_add_summed_spend_and_token_counts(self) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl", fixtures.run_row())
        logs = [
            {"request_tags": ["tag-1"], "spend": 0.25, "prompt_tokens": 5, "completion_tokens": 1},
            {"request_tags": json.dumps(["tag-1"]), "spend": 0.5, "prompt_tokens": 6, "completion_tokens": 2},
            {"request_tags": ["other"], "spend": 9.0},
        ]
        self.server.route("/spend/logs/v2", ok({"data": logs, "total_pages": 1}))
        out = self.reconcile()
        row = fixtures.read_rows(self.base / "results" / "results.jsonl")[0]
        self.assertEqual(
            (row["cost_litellm"], row["litellm_requests"], row["litellm_prompt_tokens"], "1/1 runs matched" in out),
            (0.75, 2, 11, True),
        )

    def test_unmatched_tagged_run_gets_no_litellm_cost_and_a_hint(self) -> None:
        fixtures.write_rows(self.base / "results" / "results.jsonl", fixtures.run_row())
        self.server.route("/spend/logs/v2", ok({"data": [], "total_pages": 1}))
        out = self.reconcile()
        self.assertEqual(
            ("cost_litellm" in fixtures.read_rows(self.base / "results" / "results.jsonl")[0], "Unmatched runs" in out),
            (False, True),
        )


class LoadConfigTests(unittest.TestCase):
    def write(self, cfg: dict[str, Any]) -> str:
        path = fixtures.tmpdir(self) / "c.json"
        path.write_text(json.dumps(cfg))
        return str(path)

    def litellm_config(self) -> dict[str, Any]:
        cfg = fixtures.fake_config({})
        cfg["litellm"] = {"base_url": "https://x", "api_key": "${K}"}
        return cfg

    def test_config_without_litellm_section_exits(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            hbl.load_config(self.write(fixtures.fake_config({})))
        self.assertIn("needs a 'litellm' section", str(cm.exception.code))

    def test_cursor_harness_and_its_matrix_entries_are_dropped(self) -> None:
        with fixtures.quiet() as out:
            cfg, _ = hbl.load_config(self.write(self.litellm_config()))
        self.assertEqual(
            ("cursor" in cfg["harnesses"], len(cfg["matrix"]), "ignored 1 matrix entries" in out.getvalue()),
            (False, 4, True),
        )

    def test_defaults_force_litellm_cost_and_separate_output_dir(self) -> None:
        with fixtures.quiet():
            cfg, _ = hbl.load_config(self.write(self.litellm_config()))
        self.assertEqual((cfg["cost_source"], cfg["output_dir"]), ("litellm", "results-litellm"))

    def test_matrix_entry_for_an_undefined_harness_exits(self) -> None:
        cfg = self.litellm_config()
        cfg["matrix"].append({"harness": "ghost", "model": "m"})
        with fixtures.quiet(), self.assertRaises(SystemExit) as cm:
            hbl.load_config(self.write(cfg))
        self.assertIn("undefined harnesses", str(cm.exception.code))


class WrapperMainTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = fixtures.tmpdir(self)
        cfg = fixtures.fake_config({})
        cfg["matrix"] = [{"harness": "claude", "model": "fix"}]
        cfg["litellm"] = {"base_url": self.server.url, "api_key": KEY}
        (self.base / "config.json").write_text(json.dumps(cfg))
        (self.base / "results-litellm").mkdir()

    def main(self, *argv: str) -> str:
        with mock.patch.object(
            sys, "argv", ["harness_bench_litellm.py", *argv, "--config", str(self.base / "config.json")]
        ):
            with fixtures.quiet() as out:
                hbl.main()
        return out.getvalue()

    def argv_run(self, *extra: str) -> str:
        return self.main("run", "--settle", "0", *extra)

    def test_dry_run_prints_commands_and_skips_reconcile(self) -> None:
        out = self.argv_run("--dry-run")
        self.assertEqual(("fake_agent.py" in out, "waiting" in out), (True, False))

    def test_report_command_reads_existing_results(self) -> None:
        fixtures.write_rows(self.base / "results-litellm" / "results.jsonl", fixtures.run_row(cost_litellm=1.0))
        self.assertIn("Cost source: `litellm`", self.main("report"))

    def test_reconcile_command_matches_tagged_runs(self) -> None:
        fixtures.write_rows(self.base / "results-litellm" / "results.jsonl", fixtures.run_row())
        self.server.route("/spend/logs/v2", ok({"data": [{"request_tags": ["tag-1"], "spend": 1.0}], "total_pages": 1}))
        self.assertIn("1/1 runs matched", self.main("reconcile"))

    def test_run_survives_a_failing_reconcile_and_prints_the_retry_command(self) -> None:
        tags = self.base / "tags.txt"
        cfg = json.loads((self.base / "config.json").read_text())
        cfg["harnesses"]["claude"]["env"]["FAKE_TAGS_FILE"] = str(tags)
        cfg["litellm"]["base_url"] = "http://127.0.0.1:1"  # connection refused
        (self.base / "config.json").write_text(json.dumps(cfg))
        shutil.copytree(fixtures.FIZZBUZZ, self.base / "tasks" / "fizzbuzz")
        out = self.argv_run("--allow-unsandboxed")
        self.assertEqual(("reconcile failed" in out, "Run later" in out), (True, True))


if __name__ == "__main__":
    unittest.main()

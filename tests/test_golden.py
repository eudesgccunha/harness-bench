"""Golden (characterization) tests: freeze current CLI output so refactors stay safe.

Run: python3 -m unittest discover -s tests -p 'test_*.py'
Regenerate goldens only for an intended behavior change: HB_UPDATE_GOLDEN=1.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

ROOT = fixtures.ROOT
GOLDEN = ROOT / "tests" / "golden"
PY = fixtures.PY


def make_tmp(test: unittest.TestCase) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="hb-golden-"))
    test.addCleanup(shutil.rmtree, tmp, True)
    fixtures.copy_engine(tmp)
    (tmp / "results").mkdir()
    (tmp / "config.json").write_text('{"output_dir": "results"}')
    return tmp


def run_cli(tmp: Path, script: str, *args: str, config: str = "config.json") -> subprocess.CompletedProcess[str]:
    return subprocess.run([PY, script, *args, "--config", config], cwd=tmp, capture_output=True, text=True)


def mask_summary(md: str) -> str:
    lines = md.split("\n")
    lines[0] = "# Harness x model — <masked>"
    return "\n".join(lines)


def mask_row(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    for k in ("ts", "wall_s", "run_tag", "t_start", "t_end"):
        if k in row:
            row[k] = "<masked>"
    return row


class GoldenCase(unittest.TestCase):
    def assert_golden(self, name: str, actual: str) -> None:
        path = GOLDEN / name
        if os.environ.get("HB_UPDATE_GOLDEN") == "1":
            path.write_text(actual)
            return
        self.assertTrue(path.exists(), f"missing golden {path}; create with HB_UPDATE_GOLDEN=1")
        self.assertEqual(path.read_text(), actual)


class GoldenReport(GoldenCase):
    def test_summary_matches_golden(self) -> None:
        tmp = make_tmp(self)
        shutil.copy(GOLDEN / "results.jsonl", tmp / "results" / "results.jsonl")
        p = run_cli(tmp, "harness_bench.py", "report")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assert_golden("summary.md", mask_summary((tmp / "results/summary.md").read_text()))
        self.assert_golden("summary.csv", (tmp / "results/summary.csv").read_text())


class GoldenRun(GoldenCase):
    def test_results_match_golden(self) -> None:
        tmp = make_tmp(self)
        shutil.copytree(fixtures.FIZZBUZZ, tmp / "tasks" / "fizzbuzz")
        cfg = fixtures.fake_config({"FAKE_TAGS_FILE": str(tmp / "tags.txt")})
        (tmp / "config.json").write_text(json.dumps(cfg))
        p = run_cli(tmp, "harness_bench.py", "run", "--allow-unsandboxed")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = [json.loads(x) for x in (tmp / "results/results.jsonl").read_text().splitlines() if x.strip()]
        self.assertEqual(len(rows), 5)
        actual = "".join(json.dumps(mask_row(r), sort_keys=True) + "\n" for r in rows)
        self.assert_golden("run.jsonl", actual)


if __name__ == "__main__":
    unittest.main()

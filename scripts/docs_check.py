#!/usr/bin/env python3
"""Fail if HARNESS_BENCH.md is stale: regenerate it and compare with the previous content.

Works with uncommitted changes too, unlike `git diff --exit-code` (which compares with the index).
Run: python3 scripts/docs_check.py   (exit 0 = up to date)
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "HARNESS_BENCH.md"


def main() -> int:
    before = TARGET.read_bytes()
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_md.py")], check=True, capture_output=True)
    if TARGET.read_bytes() == before:
        print("HARNESS_BENCH.md is up to date")
        return 0
    print("HARNESS_BENCH.md was stale and is now regenerated: review and commit it")
    return 1


if __name__ == "__main__":
    sys.exit(main())

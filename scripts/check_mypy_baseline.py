#!/usr/bin/env python3
"""Check mypy errors against a frozen baseline.

Runs the pinned mypy (from the active interpreter) on the project source
files, normalises each error line by stripping line/column numbers (so the
baseline survives unrelated edits), and fails if any error is not present
in the baseline file. Fixing errors is always allowed (the baseline is a
ratchet); refresh it with --update when you intentionally change the set.

Usage:
    python scripts/check_mypy_baseline.py            # check (used by CI)
    python scripts/check_mypy_baseline.py --update   # rewrite the baseline
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = PROJECT_ROOT / "mypy_baseline.txt"

# mypy emits 'file:line: error:' (mypy 2.x) or 'file:line:col: error:' with
# --show-column-numbers; accept both.
ERROR_RE = re.compile(r"^(?P<file>.+?):\d+(?::\d+)?:\s*error:\s*(?P<msg>.*)$")


def source_files() -> list[Path]:
    """Project source: every top-level module plus the ui package.

    Never pass '.' — that would crawl workspace/ (extracted third-party
    sources) on a machine that has run a build.
    """
    return sorted(PROJECT_ROOT.glob("*.py")) + [PROJECT_ROOT / "ui"]


def run_mypy() -> tuple[list[str], int]:
    """Run mypy; return (normalised error lines, raw error count).

    Normalised lines are 'file: error: message' — line numbers stripped so
    the baseline survives unrelated edits, duplicates (same file+message on
    different lines) collapsed so the baseline stays small.
    """
    result = subprocess.run(
        [sys.executable, "-m", "mypy", *[str(p) for p in source_files()]],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    errors = {
        f"{m['file']}: error: {m['msg'].strip()}"
        for line in result.stdout.splitlines()
        if (m := ERROR_RE.match(line.strip()))
    }
    if not errors and "Success" not in result.stdout:
        sys.exit(f"mypy produced no recognisable output:\n{result.stdout}\n{result.stderr}")
    found = re.search(r"^Found (\d+) errors?", result.stdout, re.MULTILINE)
    return sorted(errors), (int(found.group(1)) if found else 0)


def load_baseline() -> set[str]:
    if not BASELINE_FILE.exists():
        return set()
    return {line.strip() for line in BASELINE_FILE.read_text().splitlines() if line.strip()}


def main() -> int:
    if "--update" in sys.argv[1:]:
        current, raw_count = run_mypy()
        BASELINE_FILE.write_text("\n".join(current) + ("\n" if current else ""))
        print(
            f"Baseline updated: {len(current)} entries ({raw_count} raw mypy errors) -> {BASELINE_FILE.name}"
        )
        return 0

    current, raw_count = run_mypy()
    baseline = load_baseline()
    new_errors = [e for e in current if e not in baseline]
    if new_errors:
        print(f"FAIL: {len(new_errors)} new mypy error(s) not in baseline:")
        for error in new_errors:
            print(f"  {error}")
        print(f"Fix the errors, or if intentional run: python {Path(__file__).name} --update")
        return 1
    fixed = len(baseline - set(current))
    suffix = f", {fixed} fixed since baseline" if fixed else ""
    print(
        f"OK: {raw_count} mypy error(s) ({len(current)} unique), "
        f"all within baseline ({len(baseline)} entries){suffix}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

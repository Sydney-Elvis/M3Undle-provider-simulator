#!/usr/bin/env python3
"""
Stage A8's "fixture hash verification script": checks every media fixture
documented in fixtures/synthetic/README.md against its recorded SHA-256, so
a clean checkout (or the built simulator image) can prove its fixtures
weren't corrupted, silently regenerated, or drifted from what the README
claims -- the exit gate this stage's Public Assets exist to support
("execute all public scenarios" only means something if the fixtures
behind them are provably the right bytes).

The README is the single source of truth for expected hashes (Stage A6's
"Every Fixture Must Record" requirement already puts a SHA-256 there for
every fixture); this script never hardcodes a hash of its own.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MEDIA_DIR = REPO_ROOT / "fixtures" / "synthetic"
README_PATH = MEDIA_DIR / "README.md"

# Matches "| `filename.ext` | ... | `64-hex-char-sha256` |" table rows,
# regardless of how many columns sit between them.
_ROW_PATTERN = re.compile(r"\|\s*`([^`]+\.(?:ts|mkv))`.*?\|\s*`([0-9a-f]{64})`\s*\|")


def parse_recorded_hashes(readme_text: str) -> dict[str, str]:
    return dict(_ROW_PATTERN.findall(readme_text))


def verify(media_dir: Path, recorded: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """Returns (ok, mismatched, missing) filename lists."""
    ok: list[str] = []
    mismatched: list[str] = []
    missing: list[str] = []
    for filename, expected_hash in recorded.items():
        path = media_dir / filename
        if not path.is_file():
            missing.append(filename)
            continue
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash == expected_hash:
            ok.append(filename)
        else:
            mismatched.append(filename)
    return ok, mismatched, missing


def main() -> int:
    if not README_PATH.is_file():
        print(f"ERROR: {README_PATH} not found")
        return 1

    recorded = parse_recorded_hashes(README_PATH.read_text(encoding="utf-8"))
    if not recorded:
        print(f"ERROR: no `filename` / `sha256` table rows found in {README_PATH}")
        return 1

    ok, mismatched, missing = verify(MEDIA_DIR, recorded)

    for filename in sorted(ok):
        print(f"OK       {filename}")
    for filename in sorted(mismatched):
        print(f"MISMATCH {filename}")
    for filename in sorted(missing):
        print(f"MISSING  {filename}")

    print(f"\n{len(ok)} OK, {len(mismatched)} mismatched, {len(missing)} missing (of {len(recorded)} recorded)")
    return 0 if not mismatched and not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())

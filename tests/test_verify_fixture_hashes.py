from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from verify_fixture_hashes import README_PATH, parse_recorded_hashes, verify


class ParseRecordedHashesTests(unittest.TestCase):
    def test_extracts_filename_and_hash_pairs(self) -> None:
        text = """
| File | Notes | SHA-256 |
|---|---|---|
| `clip-a.ts` | some notes | `""" + ("a" * 64) + """` |
| `clip-b.mkv` | other notes here, with `backticks` inside | `""" + ("b" * 64) + """` |
"""
        result = parse_recorded_hashes(text)
        self.assertEqual(result, {"clip-a.ts": "a" * 64, "clip-b.mkv": "b" * 64})

    def test_ignores_non_media_backtick_spans(self) -> None:
        text = "| `not_a_media_file.py` | | `" + ("c" * 64) + "` |"
        self.assertEqual(parse_recorded_hashes(text), {})


class VerifyTests(unittest.TestCase):
    def test_ok_mismatch_and_missing_are_classified_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "good.ts").write_bytes(b"hello world")
            (tmp_path / "bad.ts").write_bytes(b"wrong content")
            recorded = {
                "good.ts": hashlib.sha256(b"hello world").hexdigest(),
                "bad.ts": hashlib.sha256(b"expected content").hexdigest(),
                "missing.ts": "a" * 64,
            }
            ok, mismatched, missing = verify(tmp_path, recorded)
            self.assertEqual(ok, ["good.ts"])
            self.assertEqual(mismatched, ["bad.ts"])
            self.assertEqual(missing, ["missing.ts"])


class RealReadmeTests(unittest.TestCase):
    """Guards against the script silently finding nothing (e.g. the README
    table format changes) and against the recorded fixtures actually
    drifting from their documented hashes."""

    def test_real_readme_fixtures_all_verify(self) -> None:
        recorded = parse_recorded_hashes(README_PATH.read_text(encoding="utf-8"))
        self.assertGreater(len(recorded), 0, "expected to find at least one recorded fixture hash")
        ok, mismatched, missing = verify(README_PATH.parent, recorded)
        self.assertEqual(mismatched, [])
        self.assertEqual(missing, [])
        self.assertEqual(len(ok), len(recorded))


if __name__ == "__main__":
    unittest.main()

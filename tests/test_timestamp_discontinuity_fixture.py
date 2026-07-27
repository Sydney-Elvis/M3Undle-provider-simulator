from __future__ import annotations

import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from provider_sim import ProviderHandler, ProviderServer, load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
SPLIT_BYTE_OFFSET = 6000 * 188  # packet-aligned split point used at generation time

# The real scenario file paces at chunk_interval_ms: 20 (realistic for a
# demo), which would make a test reading past the splice point take ~19s.
# This test needs the same fixture at effectively unthrottled speed --
# duplicating just the pacing-relevant fields inline rather than mutating
# the real scenario file's demo-appropriate pacing.
FAST_SCENARIO = """
provider:
  name: timestamp-discontinuity-fast-test
  channels:
    timestamp-discontinuity:
      payload_mode: file
      payload_file: fixtures/synthetic/clean-relay-timestamp-discontinuity.ts
      payload_chunk_bytes: 1316
      chunk_count: 1200
      chunk_interval_ms: 0
      failure_mode: none
"""


class TimestampDiscontinuityFixtureTests(unittest.TestCase):
    """
    Covers the real TSDuck-produced fixture (see
    fixtures/synthetic/README.md for the generation command and the
    ffprobe/pcrextract verification of the actual PTS/DTS discontinuity).
    This test only proves the engine serves the fixture's bytes correctly
    end-to-end -- the discontinuity's own correctness was verified against
    TSDuck's own tools (ffprobe, pcrextract) at generation time, not
    re-derived here.
    """

    def test_scenario_loads(self) -> None:
        load_scenario(REPO_ROOT / "scenarios" / "core" / "timestamp-discontinuity-backward-unsignaled.yaml")

    def test_served_bytes_match_the_source_fixture_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fast.yaml"
            path.write_text(FAST_SCENARIO, encoding="utf-8")
            state = load_scenario(path)
        server = ProviderServer(("127.0.0.1", 0), ProviderHandler, state, "http://127.0.0.1")
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/channels/timestamp-discontinuity") as response:
                # Only need enough to observe both sides of the splice, not
                # the full stream.
                served = response.read(SPLIT_BYTE_OFFSET + 100_000)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        source = (REPO_ROOT / "fixtures" / "synthetic" / "clean-relay-timestamp-discontinuity.ts").read_bytes()
        self.assertGreater(len(served), SPLIT_BYTE_OFFSET, "test needs to observe both sides of the splice")
        self.assertEqual(served, source[:len(served)])
        # Bytes on both sides of the splice boundary specifically, not just
        # "the whole prefix matched" -- the actual point this fixture exists
        # to exercise.
        self.assertEqual(
            served[SPLIT_BYTE_OFFSET - 1000:SPLIT_BYTE_OFFSET],
            source[SPLIT_BYTE_OFFSET - 1000:SPLIT_BYTE_OFFSET],
        )
        self.assertEqual(
            served[SPLIT_BYTE_OFFSET:SPLIT_BYTE_OFFSET + 1000],
            source[SPLIT_BYTE_OFFSET:SPLIT_BYTE_OFFSET + 1000],
        )


if __name__ == "__main__":
    unittest.main()

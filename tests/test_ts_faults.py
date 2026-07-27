from __future__ import annotations

import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from provider_sim import (
    ProviderHandler,
    ProviderServer,
    TS_CONTINUITY_FAULT_AFTER_VIDEO_PACKETS,
    TS_DELAYED_PAT_PMT_CYCLES,
    TS_MALFORMED_PACKET_AFTER_CHUNKS,
    _TS_SEQUENCE,
    _TS_SEQUENCE_NO_IDR,
    _ts_dynamic_packet,
    _ts_packet,
    _ts_video_pid_occurrences_before,
    load_scenario,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pid_of(packet: bytes) -> int:
    return ((packet[1] & 0x1F) << 8) | packet[2]


def _cc_of(packet: bytes) -> int:
    return packet[3] & 0x0F


class TsPacketRegressionTests(unittest.TestCase):
    """Stage A7 baseline #10/11/12 added a `continuity_counter` parameter to
    _ts_packet() and extracted the NAL byte strings into named constants --
    both must leave the existing static sequences completely unchanged."""

    def test_ts_packet_default_continuity_counter_is_zero(self) -> None:
        pkt = _ts_packet(256, b"\x00" * 4, payload_unit_start=True)
        self.assertEqual(_cc_of(pkt), 0)

    def test_static_sequences_still_have_cc_zero_everywhere(self) -> None:
        for pkt in _TS_SEQUENCE + _TS_SEQUENCE_NO_IDR:
            self.assertEqual(_cc_of(pkt), 0)
            self.assertEqual(pkt[0], 0x47)


class VideoPidOccurrenceMathTests(unittest.TestCase):
    def test_matches_brute_force_count(self) -> None:
        def brute_force(chunk_index: int) -> int:
            return sum(1 for i in range(chunk_index) if i % 5 >= 2)

        for chunk_index in range(0, 60):
            self.assertEqual(_ts_video_pid_occurrences_before(chunk_index), brute_force(chunk_index))


class ContinuityCounterFaultTests(unittest.TestCase):
    def test_counter_increments_normally_before_the_fault(self) -> None:
        video_indices = [i for i in range(20) if i % 5 >= 2][:TS_CONTINUITY_FAULT_AFTER_VIDEO_PACKETS]
        for occurrence, chunk_index in enumerate(video_indices):
            pkt, event = _ts_dynamic_packet("continuity_counter_fault", chunk_index)
            self.assertEqual(_cc_of(pkt), occurrence)
            self.assertIsNone(event)

    def test_fault_skips_exactly_one_count_then_resumes(self) -> None:
        # Video occurrence TS_CONTINUITY_FAULT_AFTER_VIDEO_PACKETS is the
        # fault packet; find its chunk_index.
        fault_chunk_index = next(
            ci for ci in range(60)
            if ci % 5 >= 2 and _ts_video_pid_occurrences_before(ci) == TS_CONTINUITY_FAULT_AFTER_VIDEO_PACKETS
        )
        pkt, event = _ts_dynamic_packet("continuity_counter_fault", fault_chunk_index)
        self.assertEqual(_cc_of(pkt), (TS_CONTINUITY_FAULT_AFTER_VIDEO_PACKETS + 1) % 16)
        self.assertEqual(event, "ts_continuity_discontinuity_injected")

        # Next video packet continues incrementing from the new value, no
        # second fault event.
        next_video_chunk_index = next(ci for ci in range(fault_chunk_index + 1, 60) if ci % 5 >= 2)
        pkt2, event2 = _ts_dynamic_packet("continuity_counter_fault", next_video_chunk_index)
        self.assertEqual(_cc_of(pkt2), (TS_CONTINUITY_FAULT_AFTER_VIDEO_PACKETS + 2) % 16)
        self.assertIsNone(event2)

    def test_counter_wraps_at_16(self) -> None:
        # Far enough out that the running count (including the +1 fault
        # offset) has wrapped past 15 at least once.
        for ci in range(0, 120):
            pkt, _ = _ts_dynamic_packet("continuity_counter_fault", ci)
            self.assertLessEqual(_cc_of(pkt), 15)

    def test_fault_event_fires_exactly_once(self) -> None:
        events = [event for ci in range(80) for _, event in [_ts_dynamic_packet("continuity_counter_fault", ci)] if event]
        self.assertEqual(events, ["ts_continuity_discontinuity_injected"])


class DelayedPatPmtFaultTests(unittest.TestCase):
    def test_no_pat_or_pmt_before_the_delay_ends(self) -> None:
        for ci in range(TS_DELAYED_PAT_PMT_CYCLES * 5):
            pkt, _ = _ts_dynamic_packet("delayed_pat_pmt", ci)
            self.assertNotIn(_pid_of(pkt), (0, 100))

    def test_pat_pmt_resume_on_the_normal_cadence_after_the_delay(self) -> None:
        delay_end = TS_DELAYED_PAT_PMT_CYCLES * 5
        pat_pkt, event = _ts_dynamic_packet("delayed_pat_pmt", delay_end)
        self.assertEqual(_pid_of(pat_pkt), 0)
        self.assertEqual(event, "ts_pat_pmt_delay_ended")
        pmt_pkt, _ = _ts_dynamic_packet("delayed_pat_pmt", delay_end + 1)
        self.assertEqual(_pid_of(pmt_pkt), 100)

    def test_fault_event_fires_exactly_once(self) -> None:
        events = [event for ci in range(60) for _, event in [_ts_dynamic_packet("delayed_pat_pmt", ci)] if event]
        self.assertEqual(events, ["ts_pat_pmt_delay_ended"])


class MalformedPacketFaultTests(unittest.TestCase):
    def test_transport_error_indicator_set_only_on_the_trigger_chunk(self) -> None:
        for ci in range(40):
            pkt, event = _ts_dynamic_packet("malformed_packet", ci)
            has_tei = bool(pkt[1] & 0x80)
            if ci == TS_MALFORMED_PACKET_AFTER_CHUNKS:
                self.assertTrue(has_tei)
                self.assertEqual(event, "ts_malformed_packet_injected")
            else:
                self.assertFalse(has_tei)
                self.assertIsNone(event)


class ScenarioLoadingTests(unittest.TestCase):
    def test_all_three_baseline_scenarios_load_cleanly(self) -> None:
        for name in (
            "baseline-10-malformed-ts-packet-sequence.yaml",
            "baseline-11-continuity-counter-errors.yaml",
            "baseline-12-delayed-pat-pmt.yaml",
        ):
            with self.subTest(name=name):
                load_scenario(REPO_ROOT / "scenarios" / "core" / name)

    def test_unknown_ts_variant_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("""
provider:
  name: bad
  channels:
    chan-a:
      payload_mode: ts
      ts_variant: some_future_fault_not_built_yet
""", encoding="utf-8")
            with self.assertRaises(SystemExit):
                load_scenario(path)


class LiveStreamTests(unittest.TestCase):
    """One real HTTP round trip per fault, catching wiring bugs between
    _ts_dynamic_packet and payload_chunk_for_behavior/log_event that the
    pure-function tests above wouldn't see."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        path = Path(cls._tmpdir.name) / "scenario.yaml"
        path.write_text("""
provider:
  name: ts-fault-live-test
  channels:
    cc-fault:
      payload_mode: ts
      ts_variant: continuity_counter_fault
      chunk_count: 25
      chunk_interval_ms: 1
    delayed-pat-pmt:
      payload_mode: ts
      ts_variant: delayed_pat_pmt
      chunk_count: 20
      chunk_interval_ms: 1
    malformed:
      payload_mode: ts
      ts_variant: malformed_packet
      chunk_count: 25
      chunk_interval_ms: 1
""", encoding="utf-8")
        state = load_scenario(path)
        cls.server = ProviderServer(("127.0.0.1", 0), ProviderHandler, state, "http://127.0.0.1")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls._tmpdir.cleanup()

    def _stream(self, channel: str) -> bytes:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/channels/{channel}") as response:
            return response.read()

    def _events(self) -> list[dict]:
        import json
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/debug/state") as response:
            return json.loads(response.read())["recent_events"]

    def test_continuity_counter_fault_live_matches_pure_function(self) -> None:
        stream = self._stream("cc-fault")
        packets = [stream[i:i + 188] for i in range(0, len(stream), 188)]
        for i, actual in enumerate(packets):
            expected, _ = _ts_dynamic_packet("continuity_counter_fault", i)
            self.assertEqual(actual, expected, f"mismatch at chunk {i}")
        self.assertTrue(any(e["event"] == "ts_continuity_discontinuity_injected" for e in self._events()))

    def test_delayed_pat_pmt_live_matches_pure_function(self) -> None:
        stream = self._stream("delayed-pat-pmt")
        packets = [stream[i:i + 188] for i in range(0, len(stream), 188)]
        for i, actual in enumerate(packets):
            expected, _ = _ts_dynamic_packet("delayed_pat_pmt", i)
            self.assertEqual(actual, expected, f"mismatch at chunk {i}")
        self.assertTrue(any(e["event"] == "ts_pat_pmt_delay_ended" for e in self._events()))

    def test_malformed_packet_live_matches_pure_function(self) -> None:
        stream = self._stream("malformed")
        packets = [stream[i:i + 188] for i in range(0, len(stream), 188)]
        for i, actual in enumerate(packets):
            expected, _ = _ts_dynamic_packet("malformed_packet", i)
            self.assertEqual(actual, expected, f"mismatch at chunk {i}")
        self.assertTrue(any(e["event"] == "ts_malformed_packet_injected" for e in self._events()))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from provider_sim import ProviderState, ScenarioSequenceRunner, build_behavior


def _make_state() -> ProviderState:
    return ProviderState(
        provider_name="trigger-test",
        max_streams=4,
        channels={"chan": build_behavior("chan", {}, {})},
    )


class ScenarioTriggerMetricTests(unittest.TestCase):
    """
    Covers the after.chunks / after.bytes triggers added alongside
    after.seconds / after.connections -- see the "Count/byte triggers are
    in v1" decision in docs/M3Undle_Provider_Simulator_Implementation_Plan.md
    Stage A5 status notes.
    """

    def test_record_chunk_sent_tracks_chunks_and_bytes_per_channel(self) -> None:
        state = _make_state()
        state.record_chunk_sent("chan", 100)
        state.record_chunk_sent("chan", 50)
        self.assertEqual(state.chunks_sent_by_channel["chan"], 2)
        self.assertEqual(state.bytes_sent_by_channel["chan"], 150)

    def test_reset_metrics_clears_chunk_and_byte_counters(self) -> None:
        state = _make_state()
        state.record_chunk_sent("chan", 100)
        state.reset_metrics()
        self.assertEqual(state.chunks_sent_by_channel["chan"], 0)
        self.assertEqual(state.bytes_sent_by_channel["chan"], 0)

    def test_chunks_trigger_waits_until_target_chunk_count(self) -> None:
        state = _make_state()
        sequence = [{
            "action": "set_channel_behavior",
            "channel": "chan",
            "trigger": {"after": {"chunks": 3}},
            "stall_seconds": 1,
        }]
        runner = ScenarioSequenceRunner(state, sequence)
        runner.start()
        try:
            time.sleep(0.2)
            self.assertEqual(state.channel_overrides, {}, "override applied before the chunk target was reached")

            for _ in range(3):
                state.record_chunk_sent("chan", 10)
            runner.join(timeout=2)
            self.assertFalse(runner.is_alive())
            self.assertEqual(state.channel_overrides.get("chan", {}).get("stall_seconds"), 1.0)
        finally:
            runner.join(timeout=2)

    def test_bytes_trigger_waits_until_target_byte_count(self) -> None:
        state = _make_state()
        sequence = [{
            "action": "set_channel_behavior",
            "channel": "chan",
            "trigger": {"after": {"bytes": 250}},
            "stall_seconds": 2,
        }]
        runner = ScenarioSequenceRunner(state, sequence)
        runner.start()
        try:
            time.sleep(0.2)
            self.assertEqual(state.channel_overrides, {}, "override applied before the byte target was reached")

            state.record_chunk_sent("chan", 300)
            runner.join(timeout=2)
            self.assertFalse(runner.is_alive())
            self.assertEqual(state.channel_overrides.get("chan", {}).get("stall_seconds"), 2.0)
        finally:
            runner.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

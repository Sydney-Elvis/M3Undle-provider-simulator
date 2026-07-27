from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from provider_sim import (
    ProviderState,
    ScenarioSequenceRunner,
    build_behavior,
    load_scenario,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_state() -> ProviderState:
    return ProviderState(
        provider_name="switch-source-test",
        max_streams=4,
        channels={"chan": build_behavior("chan", {}, {})},
    )


class SetLiveSourceTests(unittest.TestCase):
    """
    Stage A7 baseline #13 (midstream source replacement): switch_source is a
    separate mechanism from set_channel_behavior specifically so it can
    apply live to an already-open connection without changing
    set_channel_behavior's existing next-connection-only contract (relied
    on by TS-SAFE-03 and the CLEAN-RELAY-05 reimplementation).
    """

    def test_unknown_channel_is_rejected(self) -> None:
        state = _make_state()
        ok, error = state.set_live_source("does-not-exist", {"payload_mode": "ts"})
        self.assertFalse(ok)
        self.assertIn("unknown channel", error)

    def test_unknown_field_is_rejected(self) -> None:
        state = _make_state()
        ok, error = state.set_live_source("chan", {"chunk_interval_ms": 5})
        self.assertFalse(ok)
        self.assertIn("unknown switch_source field", error)

    def test_invalid_payload_mode_is_rejected(self) -> None:
        state = _make_state()
        ok, error = state.set_live_source("chan", {"payload_mode": "smoke-signal"})
        self.assertFalse(ok)
        self.assertIn("payload_mode", error)

    def test_missing_payload_file_is_rejected(self) -> None:
        state = _make_state()
        with self.assertRaises(SystemExit):
            state.set_live_source("chan", {"payload_file": "fixtures/media/nope.ts"})

    def test_negative_payload_chunk_bytes_is_rejected(self) -> None:
        state = _make_state()
        ok, error = state.set_live_source("chan", {"payload_chunk_bytes": 0})
        self.assertFalse(ok)
        self.assertIn("payload_chunk_bytes", error)

    def test_valid_switch_updates_current_live_source_and_logs_event(self) -> None:
        state = _make_state()
        self.assertIsNone(state.current_live_source("chan"))
        ok, error = state.set_live_source("chan", {"payload_mode": "ts", "ts_variant": "no_idr"}, action_id="step-2")
        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertEqual(state.current_live_source("chan"), {"payload_mode": "ts", "ts_variant": "no_idr"})
        entry = state.events[-1]
        self.assertEqual(entry["event"], "source_switched")
        self.assertEqual(entry["action_id"], "step-2")

    def test_new_connections_pick_up_the_switched_source(self) -> None:
        state = _make_state()
        state.set_live_source("chan", {"ts_variant": "no_idr"})
        behavior = state.behavior_for_channel("chan")
        self.assertEqual(behavior.ts_variant, "no_idr")

    def test_reset_metrics_clears_live_source_overrides(self) -> None:
        state = _make_state()
        state.set_live_source("chan", {"ts_variant": "no_idr"})
        state.reset_metrics()
        self.assertIsNone(state.current_live_source("chan"))
        self.assertEqual(state.behavior_for_channel("chan").ts_variant, "idr")

    def test_set_channel_behavior_next_connection_contract_is_unaffected(self) -> None:
        """switch_source must not change set_channel_behavior's existing
        semantics: an override there still only affects the *next*
        connection's captured behavior, not a live-in-flight one -- there is
        no live per-chunk re-check for channel_overrides, only for
        live_source_overrides."""
        state = _make_state()
        state.set_channel_override("chan", {"stall_seconds": 9.0})
        # behavior_for_channel() (used to open a *new* connection) reflects it...
        self.assertEqual(state.behavior_for_channel("chan").stall_seconds, 9.0)
        # ...but nothing in ProviderState re-applies channel_overrides to an
        # already-captured behavior snapshot the way current_live_source()
        # does -- confirmed by current_live_source() staying empty, since
        # set_channel_override() never touches live_source_overrides.
        self.assertIsNone(state.current_live_source("chan"))


class ScenarioSequenceRunnerSwitchSourceTests(unittest.TestCase):
    def test_switch_source_action_invokes_set_live_source(self) -> None:
        state = _make_state()
        sequence = [{
            "action": "switch_source",
            "channel": "chan",
            "id": "swap-1",
            "payload_mode": "ts",
            "ts_variant": "no_idr",
        }]
        runner = ScenarioSequenceRunner(state, sequence)
        runner.run()
        self.assertEqual(
            state.current_live_source("chan"),
            {"payload_mode": "ts", "ts_variant": "no_idr"},
        )
        self.assertEqual(state.events[-1]["action_id"], "swap-1")


class SwitchSourceValidationTests(unittest.TestCase):
    def test_valid_switch_source_scenario_loads(self) -> None:
        load_scenario(REPO_ROOT / "scenarios" / "core" / "baseline-13-midstream-source-replacement.yaml")

    def test_switch_source_step_with_unknown_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("""
provider:
  name: bad
  channels:
    chan-a:
      failure_mode: none
sequence:
  - action: switch_source
    channel: chan-a
    trigger:
      after:
        connections: 1
    chunk_interval_ms: 5
""", encoding="utf-8")
            with self.assertRaises(SystemExit):
                load_scenario(path)

    def test_switch_source_step_referencing_unknown_channel_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("""
provider:
  name: bad
  channels:
    chan-a:
      failure_mode: none
sequence:
  - action: switch_source
    channel: does-not-exist
    trigger:
      after:
        connections: 1
    payload_mode: ts
""", encoding="utf-8")
            with self.assertRaises(SystemExit):
                load_scenario(path)


if __name__ == "__main__":
    unittest.main()

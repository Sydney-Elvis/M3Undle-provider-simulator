from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from provider_sim import ENGINE_VERSION, ProviderState, load_scenario


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(tmpdir: str, name: str, text: str) -> Path:
    path = Path(tmpdir) / name
    path.write_text(text, encoding="utf-8")
    return path


VALID_BASE = """
provider:
  name: validation-test
  channels:
    chan-a:
      failure_mode: none
"""


class FailClosedLoadingTests(unittest.TestCase):
    """
    Covers the Stage A5 "fail-closed atomic loading" decision: a scenario
    with an unknown action, missing referenced media, or duplicate channel
    ID must refuse to open ports, not start half-configured. See
    docs/M3Undle_Provider_Simulator_Implementation_Plan.md Stage A5 status
    notes for the decision this closes.
    """

    def test_valid_scenario_loads_cleanly(self) -> None:
        for path in (REPO_ROOT / "scenarios" / "core").glob("*.yaml"):
            with self.subTest(path=path.name):
                load_scenario(path)  # must not raise

    def test_unknown_sequence_action_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", VALID_BASE + """
sequence:
  - action: teleport_the_provider
    channel: chan-a
""")
            with self.assertRaises(SystemExit):
                load_scenario(path)

    def test_duplicate_channel_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", """
provider:
  name: validation-test
  channels:
    chan-a:
      failure_mode: none
    chan-a:
      failure_mode: close_after_chunks
""")
            with self.assertRaises(SystemExit):
                load_scenario(path)

    def test_missing_payload_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", """
provider:
  name: validation-test
  channels:
    chan-a:
      payload_mode: file
      payload_file: fixtures/media/does-not-exist-anywhere.ts
""")
            with self.assertRaises(SystemExit):
                load_scenario(path)

    def test_engine_version_too_new_is_rejected(self) -> None:
        too_new = ".".join(str(p) for p in (ENGINE_VERSION[0] + 1, 0, 0))
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", f"minimum_engine_version: {too_new}\n" + VALID_BASE)
            with self.assertRaises(SystemExit):
                load_scenario(path)

    def test_schema_version_unsupported_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", "schema_version: 99\n" + VALID_BASE)
            with self.assertRaises(SystemExit):
                load_scenario(path)

    def test_unknown_override_field_in_sequence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", VALID_BASE + """
sequence:
  - action: set_channel_behavior
    channel: chan-a
    trigger:
      after:
        connections: 1
    not_a_real_field: 123
""")
            with self.assertRaises(SystemExit):
                load_scenario(path)

    def test_unknown_trigger_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", VALID_BASE + """
sequence:
  - action: wait
    duration_s: 1
    trigger:
      after:
        light_years: 4
""")
            with self.assertRaises(SystemExit):
                load_scenario(path)

    def test_set_channel_behavior_referencing_unknown_channel_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", VALID_BASE + """
sequence:
  - action: set_channel_behavior
    channel: does-not-exist
    trigger:
      after:
        connections: 1
    stall_seconds: 1
""")
            with self.assertRaises(SystemExit):
                load_scenario(path)

    def test_expected_simulator_events_object_with_unknown_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", VALID_BASE + """
expected_simulator_events:
  - event: stream_opened
    not_a_real_field: 123
""")
            with self.assertRaises(SystemExit):
                load_scenario(path)

    def test_expected_simulator_events_object_with_action_id_loads_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "ok.yaml", VALID_BASE + """
expected_simulator_events:
  - stream_opened
  - event: source_switched
    action_id: swap-1
""")
            load_scenario(path)  # must not raise


class GenerateBlockTests(unittest.TestCase):
    """
    Covers the generate: wiring in build_behavior(): payload_file is
    computed automatically from a generate: block (a pure hash computation,
    reused from materialize_fixtures.py -- never invokes Docker). These
    tests mock materialize_fixtures.target_path so they stay Docker-free,
    like every other test in this repo.
    """

    def test_payload_file_resolved_automatically_from_generate_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resolved = Path(tmp) / "resolved.ts"
            resolved.write_bytes(b"X" * 50)
            path = _write(tmp, "ok.yaml", """
provider:
  name: validation-test
  channels:
    chan-a:
      payload_mode: file
      generate:
        worker: ts
        base: clean-relay-loop-long.ts
        args: ["-P", "pcredit", "--pid", "257"]
""")
            with mock.patch("materialize_fixtures.target_path", return_value=resolved):
                load_scenario(path)  # must not raise

    def test_generate_and_payload_file_together_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", """
provider:
  name: validation-test
  channels:
    chan-a:
      payload_mode: file
      payload_file: fixtures/synthetic/clean-relay-loop-long.ts
      generate:
        worker: ts
        base: clean-relay-loop-long.ts
        args: ["-P", "pcredit", "--pid", "257"]
""")
            with self.assertRaises(SystemExit):
                load_scenario(path)

    def test_generate_with_unmaterialized_target_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", """
provider:
  name: validation-test
  channels:
    chan-a:
      payload_mode: file
      generate:
        worker: ts
        base: clean-relay-loop-long.ts
        args: ["-P", "pcredit", "--pid", "257"]
""")
            not_yet_materialized = Path(tmp) / "not-there.ts"
            with mock.patch("materialize_fixtures.target_path", return_value=not_yet_materialized):
                with self.assertRaises(SystemExit):
                    load_scenario(path)

    def test_generate_unknown_field_is_rejected_by_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "bad.yaml", """
provider:
  name: validation-test
  channels:
    chan-a:
      payload_mode: file
      generate:
        worker: ts
        base: clean-relay-loop-long.ts
        args: []
        not_a_real_field: 123
""")
            with self.assertRaises(SystemExit):
                load_scenario(path)


class EventEnvelopeTests(unittest.TestCase):
    """
    Covers the Stage A5 "event log is a versioned public API" decision:
    every event carries event_schema_version, scenario_id, a monotonic
    sequence number, virtual_time_ms, and an action_id slot (null unless
    the event came from executing a scenario sequence step).
    """

    def _make_state(self) -> ProviderState:
        from provider_sim import build_behavior
        return ProviderState(
            provider_name="envelope-test",
            max_streams=4,
            channels={"chan": build_behavior("chan", {}, {})},
            scenario_id="my-scenario",
        )

    def test_organic_event_carries_full_envelope_with_null_action_id(self) -> None:
        state = self._make_state()
        state.log_event("something_happened", channel="chan")
        entry = state.events[-1]
        self.assertEqual(entry["scenario_id"], "my-scenario")
        self.assertEqual(entry["event_schema_version"], 1)
        self.assertIsInstance(entry["sequence"], int)
        self.assertIsInstance(entry["virtual_time_ms"], int)
        self.assertIsNone(entry["action_id"])

    def test_explicit_action_id_overrides_the_null_default(self) -> None:
        state = self._make_state()
        state.log_event("channel_override_changed", channel="chan", action_id="step-3")
        self.assertEqual(state.events[-1]["action_id"], "step-3")

    def test_sequence_number_increments_monotonically(self) -> None:
        state = self._make_state()
        state.log_event("a")
        state.log_event("b")
        state.log_event("c")
        seqs = [e["sequence"] for e in state.events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_reset_metrics_restarts_the_sequence_counter(self) -> None:
        state = self._make_state()
        state.log_event("a")
        state.log_event("b")
        state.reset_metrics()
        # reset_metrics() itself logs "metrics_reset" as the first
        # post-reset event, so the counter should be back at 1.
        self.assertEqual(state.events[-1]["sequence"], 1)

    def test_scenario_id_falls_back_to_provider_name_when_absent(self) -> None:
        from provider_sim import build_behavior
        state = ProviderState(
            provider_name="fallback-provider",
            max_streams=4,
            channels={"chan": build_behavior("chan", {}, {})},
        )
        self.assertEqual(state.scenario_id, "fallback-provider")


if __name__ == "__main__":
    unittest.main()

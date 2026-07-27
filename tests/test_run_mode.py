from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from provider_sim import cli_run, run_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_scenario(tmp_dir: str, text: str) -> Path:
    path = Path(tmp_dir) / "scenario.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class RunModeBasicTests(unittest.TestCase):
    """
    Stage A8.5: --run drives a scenario against the engine's own built-in
    client (no external test harness) and checks expected_simulator_events
    as a real contract instead of dead config.
    """

    def test_minimal_scenario_passes_and_reports_counters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_scenario(tmp, """
schema_version: 1
name: run-mode-minimal
provider:
  name: run-mode-minimal
  channels:
    chan:
      payload_mode: text
      chunk_count: 5
      chunk_interval_ms: 5
expected_simulator_events:
  - stream_opened
  - stream_closed
""")
            result = run_scenario(path, settle_seconds=0.3)
            self.assertTrue(result["passed"])
            self.assertIsNone(result["failure_reason"])
            self.assertFalse(result["sequence_aborted"])
            self.assertEqual(result["counters"]["chunks_sent_by_channel"]["chan"], 5)
            self.assertEqual(len(result["connection_timeline"]), 1)
            timeline = next(iter(result["connection_timeline"].values()))
            self.assertEqual(timeline["first_chunk_index"], 0)
            self.assertEqual(timeline["last_chunk_index"], 4)
            self.assertFalse(timeline["wraparound_detected"])

    def test_deliberately_broken_expectation_fails_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_scenario(tmp, """
schema_version: 1
name: run-mode-broken
provider:
  name: run-mode-broken
  channels:
    chan:
      payload_mode: text
      chunk_count: 3
      chunk_interval_ms: 5
expected_simulator_events:
  - stream_opened
  - this_event_never_fires
""")
            result = run_scenario(path, settle_seconds=0.3)
            self.assertFalse(result["passed"])
            self.assertIn("this_event_never_fires", result["failure_reason"])
            outcomes = {o["event"]: o["matched"] for o in result["expected_events"]}
            self.assertTrue(outcomes["stream_opened"])
            self.assertFalse(outcomes["this_event_never_fires"])

    def test_cli_run_exit_code_reflects_pass_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            passing = _write_scenario(tmp, """
schema_version: 1
name: cli-pass
provider:
  name: cli-pass
  channels:
    chan:
      payload_mode: text
      chunk_count: 2
      chunk_interval_ms: 5
expected_simulator_events:
  - stream_opened
""")
            self.assertEqual(cli_run(passing, run_id=None, bind="127.0.0.1", events_file=None, result_file=None, settle_seconds=0.3), 0)

            failing = Path(tmp) / "failing.yaml"
            failing.write_text("""
schema_version: 1
name: cli-fail
provider:
  name: cli-fail
  channels:
    chan:
      payload_mode: text
      chunk_count: 2
      chunk_interval_ms: 5
expected_simulator_events:
  - never_happens
""", encoding="utf-8")
            self.assertEqual(cli_run(failing, run_id=None, bind="127.0.0.1", events_file=None, result_file=None, settle_seconds=0.3), 1)


class RunModeAuthenticationTests(unittest.TestCase):
    def test_authenticated_channel_still_streams_via_playlist_credentials(self) -> None:
        """The built-in client must fetch /playlist.m3u with provider.authentication's
        own credentials -- the same way a real downstream client would already be
        configured -- otherwise every authenticated baseline scenario would fail
        run mode simply because the client never authenticated."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_scenario(tmp, """
schema_version: 1
name: run-mode-auth
provider:
  name: run-mode-auth
  authentication:
    mode: static
    username: run-user
    password: run-pass
  channels:
    chan:
      payload_mode: text
      chunk_count: 3
      chunk_interval_ms: 5
expected_simulator_events:
  - stream_opened
""")
            result = run_scenario(path, settle_seconds=0.3)
            self.assertTrue(result["passed"], result)


class RunModeSequenceTriggerTests(unittest.TestCase):
    def test_switch_source_trigger_fires_and_is_tagged_with_action_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_scenario(tmp, """
schema_version: 1
name: run-mode-sequence
run:
  max_duration_s: 15
  trigger_timeout_s: 5
provider:
  name: run-mode-sequence
  channels:
    chan:
      payload_mode: text
      chunk_count: 20
      chunk_interval_ms: 10
sequence:
  - action: switch_source
    channel: chan
    id: swap-1
    trigger:
      after:
        chunks: 5
    payload: switched
expected_simulator_events:
  - stream_opened
  - source_switched
""")
            result = run_scenario(path, settle_seconds=0.3)
            self.assertTrue(result["passed"], result)
            source_switched = next(o for o in result["expected_events"] if o["event"] == "source_switched")
            self.assertEqual(source_switched["action_id"], "swap-1")


class RunModeExpectedEventActionIdTests(unittest.TestCase):
    """expected_simulator_events entries may be an object pinning
    action_id, in addition to the original bare-string shape."""

    _SCENARIO_TEMPLATE = """
schema_version: 1
name: run-mode-action-id-{suffix}
run:
  max_duration_s: 15
  trigger_timeout_s: 5
provider:
  name: run-mode-action-id-{suffix}
  channels:
    chan:
      payload_mode: text
      chunk_count: 20
      chunk_interval_ms: 10
sequence:
  - action: switch_source
    channel: chan
    id: swap-1
    trigger:
      after:
        chunks: 5
    payload: switched
expected_simulator_events:
  - stream_opened
  - event: source_switched
    action_id: "{action_id}"
"""

    def test_object_form_with_correct_action_id_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_scenario(
                tmp, self._SCENARIO_TEMPLATE.format(suffix="correct", action_id="swap-1")
            )
            result = run_scenario(path, settle_seconds=0.3)
            self.assertTrue(result["passed"], result)
            outcome = next(o for o in result["expected_events"] if o["event"] == "source_switched")
            self.assertTrue(outcome["matched"])
            self.assertEqual(outcome["action_id"], "swap-1")

    def test_object_form_with_wrong_action_id_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_scenario(
                tmp, self._SCENARIO_TEMPLATE.format(suffix="wrong", action_id="swap-does-not-exist")
            )
            result = run_scenario(path, settle_seconds=0.3)
            self.assertFalse(result["passed"], result)
            outcome = next(o for o in result["expected_events"] if o["event"] == "source_switched")
            self.assertFalse(outcome["matched"])

    def test_existing_bare_string_scenario_still_passes_unchanged(self) -> None:
        result = run_scenario(
            REPO_ROOT / "scenarios" / "core" / "baseline-05-abrupt-connection-close.yaml",
            settle_seconds=0.3,
        )
        self.assertTrue(result["passed"], result)


class RunModeWraparoundTests(unittest.TestCase):
    def test_file_payload_wraparound_is_detected_per_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp) / "tiny.ts"
            media_path.write_bytes(b"X" * 50)
            path = _write_scenario(tmp, f"""
schema_version: 1
name: run-mode-wraparound
provider:
  name: run-mode-wraparound
  channels:
    chan:
      payload_mode: file
      payload_file: {media_path}
      payload_chunk_bytes: 20
      chunk_count: 10
      chunk_interval_ms: 5
expected_simulator_events:
  - stream_opened
""")
            result = run_scenario(path, settle_seconds=0.3)
            timeline = next(iter(result["connection_timeline"].values()))
            self.assertTrue(timeline["wraparound_detected"])


class RunModeEventEnvelopeTests(unittest.TestCase):
    def test_run_id_is_stable_across_every_event_and_overridable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_scenario(tmp, """
schema_version: 1
name: run-mode-run-id
provider:
  name: run-mode-run-id
  channels:
    chan:
      payload_mode: text
      chunk_count: 2
      chunk_interval_ms: 5
expected_simulator_events:
  - stream_opened
""")
            events_file = Path(tmp) / "events.jsonl"
            result = run_scenario(path, run_id="ci-correlation-id-123", events_file=events_file, settle_seconds=0.3)
            self.assertEqual(result["run_id"], "ci-correlation-id-123")

            lines = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertGreaterEqual(len(lines), 3)
            self.assertTrue(all(entry["run_id"] == "ci-correlation-id-123" for entry in lines))
            events_seen = {entry["event"] for entry in lines}
            self.assertIn("stream_opened", events_seen)
            self.assertIn("scenario_completed", events_seen)

    def test_stream_events_carry_client_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_scenario(tmp, """
schema_version: 1
name: run-mode-client-address
provider:
  name: run-mode-client-address
  channels:
    chan:
      payload_mode: text
      chunk_count: 2
      chunk_interval_ms: 5
expected_simulator_events:
  - stream_opened
""")
            events_file = Path(tmp) / "events.jsonl"
            run_scenario(path, events_file=events_file, settle_seconds=0.3)
            lines = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            opened = next(entry for entry in lines if entry["event"] == "stream_opened")
            self.assertEqual(opened["client_address"], "127.0.0.1")


class RunModeRealScenarioTests(unittest.TestCase):
    """Runs against real, currently-published scenarios/core files -- picked
    for being fast (fault fires before/near the start of the chunk loop) so
    this stays within the regular unit-test budget."""

    def test_baseline_08_temporary_503_passes(self) -> None:
        result = run_scenario(
            REPO_ROOT / "scenarios" / "core" / "baseline-08-temporary-http-503.yaml",
            settle_seconds=0.3,
        )
        self.assertTrue(result["passed"], result)

    def test_baseline_05_abrupt_close_passes(self) -> None:
        result = run_scenario(
            REPO_ROOT / "scenarios" / "core" / "baseline-05-abrupt-connection-close.yaml",
            settle_seconds=0.3,
        )
        self.assertTrue(result["passed"], result)

    def test_baseline_10_malformed_ts_packet_passes(self) -> None:
        result = run_scenario(
            REPO_ROOT / "scenarios" / "core" / "baseline-10-malformed-ts-packet-sequence.yaml",
            settle_seconds=0.3,
        )
        self.assertTrue(result["passed"], result)


if __name__ == "__main__":
    unittest.main()

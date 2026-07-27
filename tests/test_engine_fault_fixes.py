from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import provider_sim
from provider_sim import (
    ProviderHandler,
    ProviderServer,
    ProviderState,
    ScenarioSequenceRunner,
    VALID_AUTH_MODES,
    VALID_FAILURE_MODES,
    VALID_PAYLOAD_MODES,
    VALID_PLAYLIST_FORMATS,
    VALID_SEQUENCE_ACTIONS,
    VALID_TRIGGER_AFTER_KEYS,
    VALID_TS_VARIANTS,
    build_behavior,
    load_scenario,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

FAULT_SCENARIO = """
provider:
  name: fault-fix-test
  max_streams: 8
  channels:
    reset-chan:
      payload_mode: ts
      failure_mode: reset_before_headers
    fin-chan:
      payload_mode: ts
      failure_mode: disconnect_before_headers
    stall-chan:
      payload_mode: ts
      failure_mode: stall_after_chunks
      failure_after_chunks: 2
      chunk_count: 10
      chunk_interval_ms: 0
      stall_seconds: 0.2
"""


class LiveFaultServerTests(unittest.TestCase):
    """
    reset_before_headers vs disconnect_before_headers must be observably
    different on the wire: reset sends a TCP RST (SO_LINGER zero-timeout
    close), disconnect sends an ordinary FIN. Before the fix both modes
    executed the identical shutdown+close path and every client saw a
    graceful EOF either way.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        path = Path(cls._tmpdir.name) / "scenario.yaml"
        path.write_text(FAULT_SCENARIO, encoding="utf-8")
        state = load_scenario(path)
        cls.server = ProviderServer(("127.0.0.1", 0), ProviderHandler, state, "http://127.0.0.1")
        cls.port = cls.server.server_address[1]
        cls.server.public_base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls._tmpdir.cleanup()

    def _raw_get_outcome(self, channel: str) -> str:
        """Issue a GET on a raw socket and report how the connection died:
        'eof' for a graceful FIN, 'reset' for a TCP RST."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(
                f"GET /channels/{channel} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode()
            )
            try:
                while True:
                    if sock.recv(65536) == b"":
                        return "eof"
            except ConnectionResetError:
                return "reset"

    def test_disconnect_before_headers_is_graceful_eof(self) -> None:
        self.assertEqual(self._raw_get_outcome("fin-chan"), "eof")

    def test_reset_before_headers_is_tcp_rst(self) -> None:
        self.assertEqual(self._raw_get_outcome("reset-chan"), "reset")

    def test_stall_emits_bracketing_events_with_real_duration(self) -> None:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/channels/stall-chan", timeout=10
        ) as response:
            response.read()
        events = [
            entry
            for entry in self.server.state.snapshot()["recent_events"]
            if entry.get("channel") == "stall-chan"
        ]
        names = [entry["event"] for entry in events]
        self.assertIn("stall_started", names)
        self.assertIn("stall_ended", names)
        self.assertLess(names.index("stream_broken"), names.index("stall_started"))
        self.assertLess(names.index("stall_started"), names.index("stall_ended"))
        self.assertLess(names.index("stall_ended"), names.index("stream_closed"))
        started = next(entry for entry in events if entry["event"] == "stall_started")
        ended = next(entry for entry in events if entry["event"] == "stall_ended")
        self.assertEqual(started["stall_seconds"], 0.2)
        self.assertGreaterEqual(ended["virtual_time_ms"] - started["virtual_time_ms"], 200)


def _make_state() -> ProviderState:
    behavior = build_behavior(
        "chan", {"payload_mode": "text", "chunk_count": 5, "chunk_interval_ms": 0}, {}
    )
    return ProviderState(provider_name="p", max_streams=1000, channels={"chan": behavior})


class ConnectionsTriggerCounterTests(unittest.TestCase):
    def test_trigger_counts_survive_event_deque_rotation(self) -> None:
        """250 open/close cycles rotate the maxlen-200 event deque; the old
        implementation counted stream_opened entries still in the deque and
        undercounted, so a connections trigger could silently never fire."""
        state = _make_state()
        for _ in range(250):
            state.try_open_stream("chan")
            state.log_event("stream_opened", channel="chan")
            state.close_stream("chan", broken=False)
            state.log_event("stream_closed", channel="chan")

        deque_count = sum(
            1 for entry in state.events if entry.get("event") == "stream_opened"
        )
        self.assertLess(deque_count, 250)  # the deque really did rotate
        self.assertEqual(state.opened_by_channel["chan"], 250)

        runner = ScenarioSequenceRunner(
            state,
            [
                {
                    "action": "set_channel_behavior",
                    "channel": "chan",
                    "trigger": {"after": {"connections": 250}},
                    "failure_mode": "http_error",
                }
            ],
        )
        runner.trigger_timeout_seconds = 2.0
        runner.run()
        self.assertEqual(state.channel_overrides.get("chan", {}).get("failure_mode"), "http_error")

    def test_reset_metrics_clears_opened_counter(self) -> None:
        state = _make_state()
        state.try_open_stream("chan")
        self.assertEqual(state.opened_by_channel["chan"], 1)
        state.reset_metrics()
        self.assertEqual(state.opened_by_channel["chan"], 0)


class TriggerTimeoutAbortsSequenceTests(unittest.TestCase):
    def test_timed_out_trigger_skips_action_and_aborts_remaining_steps(self) -> None:
        state = _make_state()
        runner = ScenarioSequenceRunner(
            state,
            [
                {
                    "action": "set_channel_behavior",
                    "channel": "chan",
                    "trigger": {"after": {"connections": 5}},
                    "failure_mode": "http_error",
                },
                {
                    "action": "set_channel_behavior",
                    "channel": "chan",
                    "stall_seconds": 3,
                },
            ],
        )
        runner.trigger_timeout_seconds = 0.3
        start = time.monotonic()
        runner.run()
        self.assertLess(time.monotonic() - start, 5)
        # Neither the timed-out step nor the later untriggered step applied.
        self.assertEqual(state.channel_overrides, {})
        names = [entry["event"] for entry in state.events]
        self.assertIn("scenario_trigger_timeout", names)
        self.assertIn("scenario_sequence_aborted", names)
        aborted = next(
            entry for entry in state.events if entry["event"] == "scenario_sequence_aborted"
        )
        self.assertEqual(aborted["action_id"], "step-0")


class PayloadFileCacheTests(unittest.TestCase):
    def test_cache_hit_and_mtime_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "payload.bin"
            path.write_bytes(b"first-contents")
            first = provider_sim._read_payload_file(str(path))
            self.assertEqual(first, b"first-contents")
            # Second call is served from cache: the identical object, no re-read.
            self.assertIs(provider_sim._read_payload_file(str(path)), first)
            # Editing the file on disk still takes effect (mtime_ns check).
            time.sleep(0.01)
            path.write_bytes(b"second-contents")
            self.assertEqual(provider_sim._read_payload_file(str(path)), b"second-contents")


class SchemaEnumSyncTests(unittest.TestCase):
    """The JSON Schema's enums are hand-maintained copies of the engine's
    frozensets (the ts_variant enum already needed a manual edit once, for
    the Stage A7 TS faults). This pins them together so they can't drift."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            (REPO_ROOT / "schemas" / "provider-scenario.schema.json").read_text(encoding="utf-8")
        )
        cls.channel = cls.schema["$defs"]["channelBehavior"]["properties"]
        cls.step = cls.schema["$defs"]["sequenceStep"]["properties"]

    def test_channel_behavior_enums_match_engine(self) -> None:
        self.assertEqual(set(self.channel["failure_mode"]["enum"]), VALID_FAILURE_MODES)
        self.assertEqual(set(self.channel["payload_mode"]["enum"]), VALID_PAYLOAD_MODES)
        self.assertEqual(set(self.channel["ts_variant"]["enum"]), VALID_TS_VARIANTS)
        self.assertEqual(set(self.channel["playlist_format"]["enum"]), VALID_PLAYLIST_FORMATS)

    def test_sequence_step_vocabulary_matches_engine(self) -> None:
        self.assertEqual(set(self.step["action"]["enum"]), VALID_SEQUENCE_ACTIONS)
        after = self.step["trigger"]["properties"]["after"]["properties"]
        self.assertEqual(set(after), VALID_TRIGGER_AFTER_KEYS)

    def test_authentication_mode_enum_matches_engine(self) -> None:
        auth = self.schema["properties"]["provider"]["properties"]["authentication"]
        self.assertEqual(set(auth["properties"]["mode"]["enum"]), VALID_AUTH_MODES)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from provider_sim import ProviderHandler, ProviderServer, load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]

AUTH_SCENARIO = """
provider:
  name: auth-test
  authentication:
    mode: static
    username: theuser
    password: thepass
  channels:
    chan-a:
      payload_mode: ts
      failure_mode: none
      chunk_count: 3
      chunk_interval_ms: 1
"""

NO_AUTH_SCENARIO = """
provider:
  name: no-auth-test
  channels:
    chan-a:
      payload_mode: ts
      failure_mode: none
      chunk_count: 3
      chunk_interval_ms: 1
"""


class _LiveServerCase(unittest.TestCase):
    """
    Stage A7 baseline #15: the provider.authentication block gates
    /playlist.m3u and /channels/{id} the same way _xtream_auth_ok() already
    gates the Xtream surface. Needs a real HTTP server (unlike
    switch_source, which is reachable directly through ProviderState) since
    the check lives in ProviderHandler.
    """

    scenario_text: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        path = Path(cls._tmpdir.name) / "scenario.yaml"
        path.write_text(cls.scenario_text, encoding="utf-8")
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

    def _get(self, path: str) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


class AuthenticationConfiguredTests(_LiveServerCase):
    scenario_text = AUTH_SCENARIO

    def test_playlist_without_credentials_is_401(self) -> None:
        status, _ = self._get("/playlist.m3u")
        self.assertEqual(status, 401)

    def test_playlist_with_wrong_credentials_is_401(self) -> None:
        status, _ = self._get("/playlist.m3u?username=nope&password=nope")
        self.assertEqual(status, 401)

    def test_playlist_with_correct_credentials_succeeds_and_embeds_them(self) -> None:
        status, body = self._get("/playlist.m3u?username=theuser&password=thepass")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn(f"/channels/chan-a?username=theuser&password=thepass", text)

    def test_channel_stream_without_credentials_is_401(self) -> None:
        status, _ = self._get("/channels/chan-a")
        self.assertEqual(status, 401)

    def test_channel_stream_with_correct_credentials_succeeds(self) -> None:
        status, body = self._get("/channels/chan-a?username=theuser&password=thepass")
        self.assertEqual(status, 200)
        self.assertTrue(len(body) > 0)

    def test_unauthorized_channel_attempt_does_not_count_against_max_streams(self) -> None:
        self._get("/channels/chan-a")  # rejected, no credentials
        self._get("/channels/chan-a?username=nope&password=nope")  # rejected, wrong credentials
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/debug/state") as response:
            import json
            snapshot = json.loads(response.read())
        self.assertEqual(snapshot["active_streams"], 0)
        self.assertEqual(snapshot["total_rejected"], 0)  # rejected by auth, not by try_open_stream


class NoAuthenticationConfiguredTests(_LiveServerCase):
    """Control group: a scenario with no provider.authentication block must
    behave exactly as it did before this feature existed."""

    scenario_text = NO_AUTH_SCENARIO

    def test_playlist_with_no_credentials_at_all_still_succeeds(self) -> None:
        status, body = self._get("/playlist.m3u")
        self.assertEqual(status, 200)
        self.assertNotIn("username=", body.decode("utf-8"))

    def test_channel_stream_with_no_credentials_at_all_still_succeeds(self) -> None:
        status, body = self._get("/channels/chan-a")
        self.assertEqual(status, 200)
        self.assertTrue(len(body) > 0)


class AuthConfigLoadingTests(unittest.TestCase):
    def test_unknown_auth_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("""
provider:
  name: bad
  authentication:
    mode: oauth2
  channels:
    chan-a:
      failure_mode: none
""", encoding="utf-8")
            with self.assertRaises(SystemExit):
                load_scenario(path)

    def test_valid_baseline_15_scenario_loads(self) -> None:
        state = load_scenario(REPO_ROOT / "scenarios" / "core" / "baseline-15-authentication-success-and-failure.yaml")
        self.assertIsNotNone(state.auth)
        self.assertEqual(state.auth.username, "baseline-user")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from provider_sim import ProviderHandler, ProviderServer, load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]


class _LiveServerCase(unittest.TestCase):
    scenario_name: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        path = REPO_ROOT / "scenarios" / "core" / cls.scenario_name
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


class Baseline05AbruptCloseTests(_LiveServerCase):
    scenario_name = "baseline-05-abrupt-connection-close.yaml"

    def test_connection_drops_after_exactly_15_chunks_with_no_http_error(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/channels/abrupt-close") as response:
            self.assertEqual(response.status, 200)
            body = response.read()
        self.assertEqual(len(body), 15 * 188)


class Baseline08Http503Tests(_LiveServerCase):
    scenario_name = "baseline-08-temporary-http-503.yaml"

    def test_503_with_no_retry_after_header(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/channels/temporary-503")
        try:
            self.assertEqual(raised.exception.code, 503)
            self.assertIsNone(raised.exception.headers.get("Retry-After"))
        finally:
            raised.exception.close()


class Baseline09Http429Tests(_LiveServerCase):
    scenario_name = "baseline-09-http-429-retry-after.yaml"

    def test_429_with_retry_after_20(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/channels/rate-limited")
        try:
            self.assertEqual(raised.exception.code, 429)
            self.assertEqual(raised.exception.headers.get("Retry-After"), "20")
        finally:
            raised.exception.close()


if __name__ == "__main__":
    unittest.main()

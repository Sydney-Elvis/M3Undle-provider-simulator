from __future__ import annotations

import sys
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playback_contract import deterministic_media, parse_single_range, playback_media
from provider_sim import ProviderHandler, ProviderServer, load_scenario


class ByteRangeTests(unittest.TestCase):
    def test_closed_range(self) -> None:
        result = parse_single_range("bytes=100-199", 1000)
        self.assertIsNotNone(result)
        self.assertEqual((100, 199, 100), (result.start, result.end, result.length))

    def test_open_ended_range(self) -> None:
        result = parse_single_range("bytes=900-", 1000)
        self.assertEqual((900, 999, 100), (result.start, result.end, result.length))

    def test_suffix_range(self) -> None:
        result = parse_single_range("bytes=-100", 1000)
        self.assertEqual((900, 999, 100), (result.start, result.end, result.length))

    def test_end_is_clamped(self) -> None:
        result = parse_single_range("bytes=950-2000", 1000)
        self.assertEqual((950, 999, 50), (result.start, result.end, result.length))

    def test_beyond_eof_is_unsatisfiable(self) -> None:
        with self.assertRaises(ValueError):
            parse_single_range("bytes=1000-", 1000)

    def test_multiple_ranges_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_single_range("bytes=0-1,3-4", 1000)

    def test_media_is_deterministic_and_distinct(self) -> None:
        movie = deterministic_media("movie")
        self.assertEqual(movie, deterministic_media("movie"))
        self.assertNotEqual(movie, deterministic_media("episode"))
        self.assertTrue(movie.startswith(b"\x1aE\xdf\xa3"))

    def test_media_hash_is_stable(self) -> None:
        self.assertEqual(
            "c3ca0e5f32704bbb96ace97a63c6cb7be2512a6080a22eeaa0a33f1503db37ae",
            __import__("hashlib").sha256(deterministic_media("movie:30001")).hexdigest(),
        )

    def test_valid_playback_media_hashes_are_stable(self) -> None:
        import hashlib

        self.assertEqual(
            "96e82e36a84bc11e97956f831ba4df814521185c5318d7961459664d0e299c63",
            hashlib.sha256(playback_media("movie:30001")).hexdigest(),
        )
        self.assertEqual(
            "5a404a8a868fe44794f001196110a9fc90e93a057a6099482e02aed675c62d91",
            hashlib.sha256(playback_media("series:50001")).hexdigest(),
        )


# Self-contained equivalent of what used to be a private lab fixture
# (fixtures/providers/provider-xtream.json) -- same VOD/series IDs and
# xtream credentials the playback_contract.py demo mapping expects
# (movie:30001/30002, series:50001/50002), inlined so this test has no
# dependency on lab-private fixture files. The live-stream channels aren't
# exercised by any test below, so they're trimmed to the minimum
# load_scenario() requires (at least one channel).
XTREAM_SCENARIO = """
provider:
  name: playback-contract-demo
  max_streams: 4
  channels:
    channel-demo:
      display_name: Demo Channel
      failure_mode: none
xtream:
  username: xtreamuser
  password: xtreampass
  categories:
    - {category_id: "20", category_name: "Lab Movies", parent_id: 0}
  vod_categories:
    - {category_id: "20", category_name: "Lab Movies", parent_id: 0}
  vod_streams:
    - {stream_id: 30001, name: "Lab Movie One", stream_type: movie, category_id: "20", container_extension: mkv, added: "0", direct_source: ""}
    - {stream_id: 30002, name: "Lab Movie Cap Hold", stream_type: movie, category_id: "20", container_extension: mkv, added: "0", direct_source: ""}
  series_categories:
    - {category_id: "30", category_name: "Lab Series", parent_id: 0}
  series:
    - {series_id: 40001, name: "Lab Show One", category_id: "30", last_modified: "1783900800", cover: "", plot: "Deterministic lab series"}
  series_info:
    "40001":
      info: {name: "Lab Show One", category_id: "30"}
      seasons:
        - {season_number: 1, name: "Season 1"}
      episodes:
        "1":
          - {id: 50001, episode_num: 1, season: 1, title: "Lab Show One - S01E01", container_extension: mkv, info: {}}
          - {id: 50002, episode_num: 2, season: 1, title: "Lab Show One - S01E02", container_extension: mkv, info: {}}
"""


class ProviderSimulatorPlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        path = Path(cls._tmpdir.name) / "xtream-demo.yaml"
        path.write_text(XTREAM_SCENARIO, encoding="utf-8")
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

    def test_xtream_discovery_exposes_movie_and_episodes(self) -> None:
        query = "username=xtreamuser&password=xtreampass&action=get_vod_streams"
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/player_api.php?{query}") as response:
            movies = json.loads(response.read())
        self.assertEqual(["Lab Movie One", "Lab Movie Cap Hold"], [movie["name"] for movie in movies])

        query = "username=xtreamuser&password=xtreampass&action=get_series_info&series_id=40001"
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/player_api.php?{query}") as response:
            series = json.loads(response.read())
        self.assertEqual([50001, 50002], [episode["id"] for episode in series["episodes"]["1"]])

    def test_movie_range_response_is_a_valid_oracle(self) -> None:
        url = f"http://127.0.0.1:{self.port}/movie/xtreamuser/xtreampass/30001.mkv"
        request = urllib.request.Request(url, headers={"Range": "bytes=65536-66559"})
        with urllib.request.urlopen(request) as response:
            body = response.read()
            self.assertEqual(206, response.status)
            self.assertEqual("bytes", response.headers["Accept-Ranges"])
            self.assertEqual(f"bytes 65536-66559/{len(playback_media('movie:30001'))}", response.headers["Content-Range"])
            self.assertEqual("1024", response.headers["Content-Length"])
        self.assertEqual(playback_media("movie:30001")[65536:66560], body)

    def test_suffix_and_open_ended_ranges(self) -> None:
        url = f"http://127.0.0.1:{self.port}/series/xtreamuser/xtreampass/50001.mkv"
        payload = playback_media("series:50001")
        expected = payload[-1024:]
        start = len(payload) - 1024
        for value in ("bytes=-1024", f"bytes={start}-"):
            with self.subTest(value=value):
                request = urllib.request.Request(url, headers={"Range": value})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(206, response.status)
                    self.assertEqual(f"bytes {start}-{len(payload) - 1}/{len(payload)}", response.headers["Content-Range"])
                    self.assertEqual(expected, response.read())

    def test_if_range_valid_and_stale(self) -> None:
        url = f"http://127.0.0.1:{self.port}/movie/xtreamuser/xtreampass/30001.mkv"
        valid = urllib.request.Request(url, headers={
            "Range": "bytes=0-1023",
            "If-Range": '"lab-movie-30001-v1"',
        })
        with urllib.request.urlopen(valid) as response:
            self.assertEqual(206, response.status)
            self.assertEqual(playback_media("movie:30001")[:1024], response.read())

        stale = urllib.request.Request(url, headers={"Range": "bytes=0-1023", "If-Range": '"stale"'})
        with urllib.request.urlopen(stale) as response:
            self.assertEqual(200, response.status)
            self.assertEqual(playback_media("movie:30001"), response.read())

    def test_unsatisfiable_range_returns_416_with_total(self) -> None:
        url = f"http://127.0.0.1:{self.port}/movie/xtreamuser/xtreampass/30001.mkv"
        total = len(playback_media("movie:30001"))
        request = urllib.request.Request(url, headers={"Range": f"bytes={total}-"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        try:
            self.assertEqual(416, raised.exception.code)
            self.assertEqual(f"bytes */{total}", raised.exception.headers["Content-Range"])
        finally:
            raised.exception.close()

    def test_hold_fixture_survives_long_enough_for_periodic_byte_reporting(self) -> None:
        state = self.server.state
        started = __import__("time").monotonic()
        url = f"http://127.0.0.1:{self.port}/movie/xtreamuser/xtreampass/30002.mkv"
        with urllib.request.urlopen(url) as response:
            self.assertEqual(200, response.status)
            response.read()

        self.assertGreaterEqual(__import__("time").monotonic() - started, 2.0)


if __name__ == "__main__":
    unittest.main()

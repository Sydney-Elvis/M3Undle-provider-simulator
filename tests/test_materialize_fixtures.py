from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from materialize_fixtures import (
    MaterializationError,
    cache_key,
    find_generate_blocks,
    materialize_one,
)


class CacheKeyTests(unittest.TestCase):
    def test_same_inputs_produce_same_key(self) -> None:
        self.assertEqual(
            cache_key("ts", "abc123", ["--pid", "256"]),
            cache_key("ts", "abc123", ["--pid", "256"]),
        )

    def test_different_args_produce_different_keys(self) -> None:
        self.assertNotEqual(
            cache_key("ts", "abc123", ["--pid", "256"]),
            cache_key("ts", "abc123", ["--pid", "257"]),
        )

    def test_different_base_hash_produces_different_key(self) -> None:
        self.assertNotEqual(
            cache_key("ts", "abc123", ["--pid", "256"]),
            cache_key("ts", "def456", ["--pid", "256"]),
        )


class FindGenerateBlocksTests(unittest.TestCase):
    def test_finds_generate_block_on_a_channel(self) -> None:
        config = {
            "provider": {
                "channels": {
                    "chan-a": {"payload_mode": "file", "generate": {"worker": "ts", "base": "x.ts", "args": []}},
                    "chan-b": {"payload_mode": "text"},
                }
            }
        }
        blocks = find_generate_blocks(config)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["channel"], "chan-a")
        self.assertEqual(blocks[0]["worker"], "ts")

    def test_no_generate_blocks_returns_empty_list(self) -> None:
        config = {"provider": {"channels": {"chan-a": {"payload_mode": "text"}}}}
        self.assertEqual(find_generate_blocks(config), [])


class MaterializeOneTests(unittest.TestCase):
    """Every test in this repo runs Docker-free -- these mock subprocess/
    shutil rather than invoking real Docker."""

    def test_skips_docker_when_target_already_cached(self) -> None:
        from materialize_fixtures import GENERATED_DIR

        fake_target = GENERATED_DIR / "deadbeefdeadbeef.ts"
        with mock.patch("materialize_fixtures.subprocess.run") as mock_run, \
             mock.patch("materialize_fixtures.is_cached", return_value=True), \
             mock.patch("materialize_fixtures.target_path", return_value=fake_target):
            materialize_one("ts", "clean-relay-loop-long.ts", ["--pid", "256"])
        mock_run.assert_not_called()

    def test_missing_docker_cli_fails_closed_with_clear_message(self) -> None:
        with mock.patch("materialize_fixtures.is_cached", return_value=False), \
             mock.patch("materialize_fixtures.target_path", return_value=Path("/fake/target.ts")), \
             mock.patch("materialize_fixtures.shutil.which", return_value=None):
            with self.assertRaises(MaterializationError) as ctx:
                materialize_one("ts", "clean-relay-loop-long.ts", ["--pid", "256"])
            self.assertIn("docker", str(ctx.exception).lower())

    def test_worker_failure_raises_with_stderr_context(self) -> None:
        fake_result = mock.Mock(returncode=1, stderr="tsp: error: bad plugin")
        with mock.patch("materialize_fixtures.is_cached", return_value=False), \
             mock.patch("materialize_fixtures.target_path", return_value=Path("/fake/target.ts")), \
             mock.patch("materialize_fixtures.shutil.which", return_value="/usr/bin/docker"), \
             mock.patch("materialize_fixtures.subprocess.run", return_value=fake_result):
            with self.assertRaises(MaterializationError) as ctx:
                materialize_one("ts", "clean-relay-loop-long.ts", ["--pid", "256"])
            self.assertIn("bad plugin", str(ctx.exception))

    def test_unknown_worker_fails_closed(self) -> None:
        with mock.patch("materialize_fixtures.is_cached", return_value=False), \
             mock.patch("materialize_fixtures.target_path", return_value=Path("/fake/target.ts")), \
             mock.patch("materialize_fixtures.shutil.which", return_value="/usr/bin/docker"):
            with self.assertRaises(MaterializationError):
                materialize_one("bogus", "clean-relay-loop-long.ts", [])

    def test_base_fixture_not_found_fails_closed(self) -> None:
        with self.assertRaises(MaterializationError):
            materialize_one("ts", "does-not-exist-anywhere.ts", ["--pid", "256"])


if __name__ == "__main__":
    unittest.main()

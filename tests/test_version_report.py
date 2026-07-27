from __future__ import annotations

import subprocess
import sys
import json
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from provider_sim import ENGINE_VERSION, SUPPORTED_SCHEMA_VERSIONS, report_version

REPO_ROOT = Path(__file__).resolve().parents[1]


class ReportVersionTests(unittest.TestCase):
    def test_report_contains_expected_keys(self) -> None:
        report = report_version()
        self.assertEqual(report["engine_version"], ".".join(str(p) for p in ENGINE_VERSION))
        self.assertEqual(report["supported_schema_versions"], sorted(SUPPORTED_SCHEMA_VERSIONS))
        self.assertIn("python_version", report)
        self.assertEqual(report["dependencies"]["pyyaml"], __import__("yaml").__version__)
        self.assertIsInstance(report["dependencies"]["jsonschema"], str)


class VersionCliTests(unittest.TestCase):
    """--version must exit 0, print exactly one JSON object, and not
    require --bind/--port/a fixture -- same contract as --validate."""

    def test_version_flag_prints_json_and_exits_zero(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "src" / "provider_sim.py"), "--version"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        report = json.loads(result.stdout.strip())
        self.assertIn("engine_version", report)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "usage-check.py")


class TestUsageCheck(unittest.TestCase):
    def run_check(self, five, seven, age_minutes=0, collected_age_minutes=None, reset_age_minutes=-60, raw=None):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            now = datetime.now(timezone.utc)
            payload = raw if raw is not None else {
                "generated_at": (now - timedelta(minutes=age_minutes)).isoformat(),
                "claude": {"windows": {
                    "five_hour": {"used_percent": five, "resets_at": (now - timedelta(minutes=reset_age_minutes)).timestamp()},
                    "seven_day": {"used_percent": seven, "resets_at": (now - timedelta(minutes=reset_age_minutes)).timestamp()},
                }},
            }
            if collected_age_minutes is not None:
                payload["claude"]["authoritative_collected_at_unix"] = (now - timedelta(minutes=collected_age_minutes)).timestamp()
            json.dump(payload, f)
            path = f.name
        try:
            env = dict(os.environ, USAGE_CHECK_LATEST_PATH=path)
            return subprocess.run([sys.executable, SCRIPT], env=env, text=True, capture_output=True, check=False)
        finally:
            os.unlink(path)

    def test_ok_caution_stop_and_unknown(self):
        result = self.run_check(3, 70)
        self.assertEqual(result.returncode, 0)
        self.assertIn("OK", result.stdout)
        result = self.run_check(80, 3)
        self.assertEqual(result.returncode, 0)
        self.assertIn("OPATLAN", result.stdout)
        result = self.run_check(3, 90)
        self.assertEqual(result.returncode, 2)
        self.assertIn("STOP", result.stdout)
        result = self.run_check(3, 70, age_minutes=26)
        self.assertEqual(result.returncode, 3)
        self.assertIn("ISMERETLEN", result.stdout)

    def test_real_collection_age_wins_and_boundary_statuses_match_display(self):
        result = self.run_check(79.99, 3, collected_age_minutes=24)
        self.assertEqual(result.returncode, 0)
        self.assertIn("5h=79.99%", result.stdout)
        self.assertIn("friss(24p) OK", result.stdout)
        result = self.run_check(80, 3)
        self.assertEqual(result.returncode, 0)
        self.assertIn("OPATLAN", result.stdout)
        result = self.run_check(89.99, 3)
        self.assertEqual(result.returncode, 0)
        self.assertIn("OPATLAN", result.stdout)
        result = self.run_check(90, 3)
        self.assertEqual(result.returncode, 2)
        self.assertIn("STOP", result.stdout)
        result = self.run_check(3, 70, collected_age_minutes=26)
        self.assertEqual(result.returncode, 3)

    def test_expired_reset_and_invalid_inputs_are_unknown(self):
        self.assertEqual(self.run_check(3, 70, reset_age_minutes=1).returncode, 3)
        self.assertEqual(self.run_check(True, 70).returncode, 3)
        self.assertEqual(self.run_check(float('nan'), 70).returncode, 3)
        self.assertEqual(self.run_check("70", 3).returncode, 3)
        self.assertEqual(self.run_check(3, 70, collected_age_minutes=-1).returncode, 3)
        # Subprocess startup advances wall time; this is the inclusive boundary
        # immediately before 25:00, while the implementation compares > 25m.
        self.assertEqual(self.run_check(3, 70, collected_age_minutes=24.999).returncode, 0)

    def test_missing_or_corrupt_file_is_unknown(self):
        env = dict(os.environ, USAGE_CHECK_LATEST_PATH="/nonexistent/usage-latest.json")
        result = subprocess.run([sys.executable, SCRIPT], env=env, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 3)
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("not-json")
            path = f.name
        try:
            result = subprocess.run([sys.executable, SCRIPT], env=dict(os.environ, USAGE_CHECK_LATEST_PATH=path), text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 3)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()

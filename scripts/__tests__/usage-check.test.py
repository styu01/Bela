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
        result = self.run_check(90, 3)
        self.assertEqual(result.returncode, 0)
        self.assertIn("FIGYELMEZTETÉS", result.stdout)
        result = self.run_check(3, 90)
        self.assertEqual(result.returncode, 2)
        self.assertIn("STOP", result.stdout)
        result = self.run_check(3, 70, age_minutes=26)
        self.assertEqual(result.returncode, 3)
        self.assertIn("ISMERETLEN", result.stdout)

    def test_real_collection_age_wins_and_boundary_statuses_match_display(self):
        result = self.run_check(79.99, 3, collected_age_minutes=24)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Claude 5 órás keret=79.99%", result.stdout)
        self.assertIn("friss(24p) OK", result.stdout)
        result = self.run_check(90, 3)
        self.assertEqual(result.returncode, 0)
        self.assertIn("FIGYELMEZTETÉS", result.stdout)
        result = self.run_check(97.99, 3)
        self.assertEqual(result.returncode, 0)
        self.assertIn("FIGYELMEZTETÉS", result.stdout)
        result = self.run_check(98, 3)
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


class TestWeeklyOnlyAccount(unittest.TestCase):
    """Thresholds per account (owner decision 2026-11-28, BELA 6580): info (Ultra) has no 5-hour limit and is judged
    on the weekly window at 90/96; styu01 keeps 5-hour 90/98, weekly 80/90."""

    def run_raw(self, claude, age_minutes=0):
        now = datetime.now(timezone.utc)
        payload = {"generated_at": now.isoformat(), "claude": dict(
            {"authoritative_collected_at_unix": (now - timedelta(minutes=age_minutes)).timestamp()}, **claude)}
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            json.dump(payload, f)
            path = f.name
        try:
            env = dict(os.environ, USAGE_CHECK_LATEST_PATH=path)
            return subprocess.run([sys.executable, SCRIPT], env=env, text=True, capture_output=True, check=False)
        finally:
            os.unlink(path)

    def weekly(self, seven, account="info", five=None, **extra):
        reset = datetime.now(timezone.utc).timestamp() + 86400
        claude = {"account": account, "windows_not_reported": ["five_hour"],
                  "windows": {"seven_day": {"used_percent": seven, "resets_at": reset}}}
        if five is not None:
            claude["windows"]["five_hour"] = {"used_percent": five, "resets_at": reset - 3600}
            del claude["windows_not_reported"]
        claude.update(extra)
        return claude

    def test_info_is_measured_and_says_it_has_no_5h_limit(self):
        result = self.run_raw(self.weekly(35), age_minutes=3)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(),
                         "USAGE info Claude 5 órás keret=n/a (ennél a fióknál nincs 5 órás keret) heti keret=35% friss(3p) OK")

    def test_info_weekly_thresholds_90_96(self):
        for seven, code, status in ((89.99, 0, "OK"), (90, 0, "FIGYELMEZTETÉS"), (95.99, 0, "FIGYELMEZTETÉS"), (96, 2, "STOP")):
            with self.subTest(seven=seven):
                result = self.run_raw(self.weekly(seven))
                self.assertEqual(result.returncode, code)
                self.assertTrue(result.stdout.strip().endswith(status), result.stdout)
                self.assertTrue(result.stdout.startswith("USAGE info Claude"), result.stdout)

    def test_info_ignores_a_5h_window_even_if_one_came(self):
        result = self.run_raw(self.weekly(35, five=99))
        self.assertEqual(result.returncode, 0)
        self.assertIn("5 órás keret=n/a (ennél a fióknál nincs 5 órás keret)", result.stdout)
        claude = self.weekly(35)
        del claude["windows_not_reported"]
        self.assertEqual(self.run_raw(claude).returncode, 0)  # no marker needed: the account has no 5-hour limit

    def test_styu01_keeps_the_fleet_rule(self):
        for five, seven, code, status in ((89.99, 79.99, 0, "OK"), (90, 3, 0, "FIGYELMEZTETÉS"), (3, 80, 0, "FIGYELMEZTETÉS"),
                                          (98, 3, 2, "STOP"), (3, 90, 2, "STOP")):
            with self.subTest(five=five, seven=seven):
                result = self.run_raw(self.weekly(seven, account="styu01", five=five))
                self.assertEqual(result.returncode, code)
                self.assertTrue(result.stdout.strip().endswith(status), result.stdout)
                self.assertTrue(result.stdout.startswith("USAGE styu01 Claude"), result.stdout)
        # weekly 90 STOPs styu01, while the same 90 is only a warning on info
        self.assertEqual(self.run_raw(self.weekly(90, account="styu01", five=3)).returncode, 2)
        self.assertEqual(self.run_raw(self.weekly(90)).returncode, 0)

    def test_styu01_missing_5h_window_is_unknown_marker_or_not(self):
        """KISPROGI 6584 K1: only info may lack the 5-hour window; on styu01 (and an unknown account) it is
        ISMERETLEN even with a windows_not_reported marker."""
        for account in ("styu01", "ismeretlen"):
            with self.subTest(account=account):
                result = self.run_raw(self.weekly(35, account=account))  # marked
                self.assertEqual(result.returncode, 3)
                self.assertIn(f"USAGE {account} Claude 5 órás/heti keret ISMERETLEN", result.stdout)
                claude = self.weekly(35, account=account)
                del claude["windows_not_reported"]
                self.assertEqual(self.run_raw(claude).returncode, 3)

    def test_weekly_window_stays_required_and_fresh(self):
        for account in ("info", "styu01"):
            claude = self.weekly(35, account=account)
            claude["windows"] = {}
            self.assertEqual(self.run_raw(claude).returncode, 3)
        result = self.run_raw(self.weekly(35), age_minutes=26)
        self.assertEqual(result.returncode, 3)
        self.assertIn("ISMERETLEN (nincs friss mérés)", result.stdout)

    def test_unknown_account_is_labelled_ismeretlen_with_the_fleet_rule(self):
        """KISPROGI 6584 L1: no / an unknown account label is "ismeretlen" (never silently styu01), judged by the
        stricter fleet rule with both windows required."""
        for account in (None, "other", 5, "ismeretlen"):
            with self.subTest(account=account):
                result = self.run_raw(self.weekly(85, account=account, five=3))
                self.assertTrue(result.stdout.startswith("USAGE ismeretlen Claude"), result.stdout)
                self.assertTrue(result.stdout.strip().endswith("FIGYELMEZTETÉS"), result.stdout)  # weekly 80
                self.assertEqual(self.run_raw(self.weekly(85, account=account)).returncode, 3)  # no 5h window

    def test_output_has_no_dash_separators(self):
        for claude in (self.weekly(35), self.weekly(97), dict(self.weekly(35), windows={})):
            out = self.run_raw(claude).stdout
            self.assertNotIn(" -- ", out)
            self.assertNotIn("\u2014", out)
            self.assertNotIn("\u2013", out)

if __name__ == "__main__":
    unittest.main()

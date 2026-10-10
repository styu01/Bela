#!/usr/bin/env python3
import importlib.util
import os
import unittest
from datetime import datetime, timedelta, timezone

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "usage-monitor-shortcircuit.py")
spec = importlib.util.spec_from_file_location("usage_monitor_shortcircuit", PATH)
um = importlib.util.module_from_spec(spec)
spec.loader.exec_module(um)


class TestFreshness(unittest.TestCase):
    def test_authoritative_timestamp_prefers_real_age_at_25_minute_boundary(self):
        now = datetime(2026, 10, 10, tzinfo=timezone.utc)
        fresh_file_time = now.isoformat()
        self.assertEqual(um.classify_freshness(fresh_file_time, (now - timedelta(minutes=25)).timestamp(), now), "fresh")
        self.assertEqual(um.classify_freshness(fresh_file_time, (now - timedelta(minutes=25, seconds=1)).timestamp(), now), "stale")

    def test_legacy_snapshot_falls_back_to_generated_at(self):
        now = datetime(2026, 10, 10, tzinfo=timezone.utc)
        self.assertEqual(um.classify_freshness((now - timedelta(minutes=24)).isoformat(), None, now), "fresh")
        self.assertEqual(um.classify_freshness((now - timedelta(minutes=26)).isoformat(), None, now), "stale")


if __name__ == "__main__":
    unittest.main()

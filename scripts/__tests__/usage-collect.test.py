#!/usr/bin/env python3
"""Unit tests for scripts/usage-collect.py.

Covers the pure-logic pieces: time formatting (fmt_reset/hours_until), the
generic de-dup gate (_should_fire), and compute_alerts() end-to-end against
synthetic snapshots -- no real network calls, no real ~/.codex or
~/.claude filesystem reads, no writes to store/.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "usage-collect.py",
)

_spec = importlib.util.spec_from_file_location("usage_collect", _MODULE_PATH)
uc = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(uc)  # type: ignore[union-attr]


def _future_ts(**kwargs):
    return int((datetime.now(timezone.utc) + timedelta(**kwargs)).timestamp())


class TestFmtReset(unittest.TestCase):
    def test_none_returns_unknown(self):
        self.assertEqual(uc.fmt_reset(None), ("unknown", "unknown"))

    def test_garbage_returns_unknown(self):
        self.assertEqual(uc.fmt_reset("not-a-timestamp"), ("unknown", "unknown"))

    def test_days_away_relative_format(self):
        ts = _future_ts(days=6, hours=4)
        _, rel = uc.fmt_reset(ts)
        self.assertIn("days", rel)
        self.assertIn("hours", rel)

    def test_hours_away_relative_format(self):
        ts = _future_ts(hours=3, minutes=10)
        _, rel = uc.fmt_reset(ts)
        self.assertIn("hours", rel)
        self.assertIn("minutes", rel)
        self.assertNotIn("days", rel)

    def test_minutes_away_relative_format(self):
        ts = _future_ts(minutes=20)
        _, rel = uc.fmt_reset(ts)
        self.assertIn("minutes", rel)
        self.assertNotIn("hours", rel)

    def test_past_timestamp_is_most(self):
        ts = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
        _, rel = uc.fmt_reset(ts)
        self.assertEqual(rel, "now")

    def test_local_string_has_budapest_offset_marker(self):
        ts = _future_ts(days=1)
        local_str, _ = uc.fmt_reset(ts)
        # e.g. "2026-07-23 14:45 CEST" -- must contain a plausible tz abbrev
        self.assertRegex(local_str, r"CES?T$")


class TestParseIsoToEpoch(unittest.TestCase):
    """_parse_iso_to_epoch() -- the Claude usage endpoint returns resets_at
    as ISO-8601 strings (e.g. "2026-07-22T16:29:59.627285+00:00"), not unix
    epochs like Codex does."""

    def test_none_returns_none(self):
        self.assertIsNone(uc._parse_iso_to_epoch(None))

    def test_non_string_returns_none(self):
        self.assertIsNone(uc._parse_iso_to_epoch(1784737800))

    def test_garbage_string_returns_none(self):
        self.assertIsNone(uc._parse_iso_to_epoch("not-a-date"))

    def test_iso_with_offset_parses_to_expected_epoch(self):
        epoch = uc._parse_iso_to_epoch("2026-07-22T16:29:59.627285+00:00")
        expected = datetime(2026, 7, 22, 16, 29, 59, 627285, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(epoch, expected, delta=0.001)

    def test_iso_without_offset_assumes_utc(self):
        epoch = uc._parse_iso_to_epoch("2026-07-22T16:29:59")
        expected = datetime(2026, 7, 22, 16, 29, 59, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(epoch, expected, delta=0.001)

    def test_result_feeds_fmt_reset_without_crashing(self):
        # end-to-end: an ISO string converted here must still format cleanly.
        future_iso = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        epoch = uc._parse_iso_to_epoch(future_iso)
        local_str, rel = uc.fmt_reset(epoch)
        self.assertNotEqual(local_str, "unknown")
        self.assertIn("hours", rel)


class TestHoursUntil(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(uc.hours_until(None))

    def test_garbage_returns_none(self):
        self.assertIsNone(uc.hours_until("nope"))

    def test_computes_approximately(self):
        ts = _future_ts(hours=10)
        hrs = uc.hours_until(ts)
        self.assertAlmostEqual(hrs, 10, delta=0.1)

    def test_negative_for_past(self):
        ts = int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp())
        hrs = uc.hours_until(ts)
        self.assertLess(hrs, 0)


class TestShouldFire(unittest.TestCase):
    def test_cross_fires_only_on_new_crossing(self):
        state = {}
        now = 1000.0
        self.assertTrue(uc._should_fire(state, "k", True, "cross", now))
        # still true next run -- must NOT re-fire
        self.assertFalse(uc._should_fire(state, "k", True, "cross", now + 10))
        # drops false -- resets
        self.assertFalse(uc._should_fire(state, "k", False, "cross", now + 20))
        # true again -- new crossing, fires
        self.assertTrue(uc._should_fire(state, "k", True, "cross", now + 30))

    def test_cross_refire_respects_interval(self):
        state = {}
        now = 1000.0
        self.assertTrue(uc._should_fire(state, "k", True, "cross_refire", now, refire_seconds=100))
        # too soon -- no re-fire
        self.assertFalse(uc._should_fire(state, "k", True, "cross_refire", now + 50, refire_seconds=100))
        # interval elapsed -- re-fires while still ongoing
        self.assertTrue(uc._should_fire(state, "k", True, "cross_refire", now + 150, refire_seconds=100))

    def test_throttle_fires_once_then_waits_full_interval(self):
        state = {}
        now = 1000.0
        self.assertTrue(uc._should_fire(state, "k", True, "throttle", now, refire_seconds=200))
        self.assertFalse(uc._should_fire(state, "k", True, "throttle", now + 100, refire_seconds=200))
        self.assertTrue(uc._should_fire(state, "k", True, "throttle", now + 250, refire_seconds=200))

    def test_condition_false_never_fires(self):
        state = {}
        self.assertFalse(uc._should_fire(state, "k", False, "cross", 1000.0))
        self.assertFalse(uc._should_fire(state, "k", False, "throttle", 1000.0, refire_seconds=1))


class TestCollectClaudeUsageHeaders(unittest.TestCase):
    def _response(self, headers):
        cm = MagicMock()
        cm.__enter__.return_value.headers = headers
        cm.__enter__.return_value.read.return_value = b'{}'
        cm.__exit__.return_value = False
        return cm

    def test_post_headers_and_fraction_conversion(self):
        headers = {
            "anthropic-ratelimit-unified-5h-utilization": "0.02",
            "anthropic-ratelimit-unified-7d-utilization": "0.7",
            "anthropic-ratelimit-unified-5h-reset": "1800000000",
            "anthropic-ratelimit-unified-7d-reset": "1800500000",
        }
        with patch.object(uc, "_read_claude_token", return_value=("secret-token", "test")), \
             patch.object(uc, "_claude_cli_version", return_value="1.2.3"), \
             patch("urllib.request.urlopen", return_value=self._response(headers)) as open_url:
            windows, err, kind = uc._collect_claude_usage_headers()
        self.assertIsNone(err)
        self.assertIsNone(kind)
        self.assertEqual(windows["five_hour"]["used_percent"], 2.0)
        self.assertEqual(windows["seven_day"]["used_percent"], 70.0)
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(json.loads(request.data)["max_tokens"], 1)
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")

    def test_invalid_header_is_transient(self):
        with patch.object(uc, "_read_claude_token", return_value=("secret-token", "test")), \
             patch("urllib.request.urlopen", return_value=self._response({})):
            windows, _, kind = uc._collect_claude_usage_headers()
        self.assertIsNone(windows)
        self.assertEqual(kind, "transient")

    def test_429_with_valid_headers_is_authoritative(self):
        headers = {
            "anthropic-ratelimit-unified-5h-utilization": "1.2",
            "anthropic-ratelimit-unified-7d-utilization": "0.89996",
            "anthropic-ratelimit-unified-5h-reset": "1800000000",
            "anthropic-ratelimit-unified-7d-reset": "1800500000",
        }
        error = urllib.error.HTTPError("url", 429, "busy", headers, None)
        with patch.object(uc, "_read_claude_token", return_value=("secret-token", "test")), \
             patch("urllib.request.urlopen", side_effect=error):
            windows, err, kind = uc._collect_claude_usage_headers()
        self.assertIsNone(err)
        self.assertIsNone(kind)
        self.assertEqual(windows["five_hour"]["used_percent"], 100.0)
        self.assertEqual(windows["seven_day"]["used_percent"], 90.0)

    def test_negative_nan_inf_or_partial_headers_are_transient(self):
        valid = {
            "anthropic-ratelimit-unified-5h-utilization": "0.2",
            "anthropic-ratelimit-unified-7d-utilization": "0.7",
            "anthropic-ratelimit-unified-5h-reset": "1800000000",
            "anthropic-ratelimit-unified-7d-reset": "1800500000",
        }
        for bad in ("-0.1", "nan", "inf"):
            headers = dict(valid, **{"anthropic-ratelimit-unified-5h-utilization": bad})
            with patch.object(uc, "_read_claude_token", return_value=("secret-token", "test")), \
                 patch("urllib.request.urlopen", return_value=self._response(headers)):
                windows, _, kind = uc._collect_claude_usage_headers()
            self.assertIsNone(windows)
            self.assertEqual(kind, "transient")
        del valid["anthropic-ratelimit-unified-7d-reset"]
        with patch.object(uc, "_read_claude_token", return_value=("secret-token", "test")), \
             patch("urllib.request.urlopen", return_value=self._response(valid)):
            windows, _, kind = uc._collect_claude_usage_headers()
        self.assertIsNone(windows)
        self.assertEqual(kind, "transient")

    def test_http_failures_and_exception_do_not_leak_token(self):
        for code, expected in ((400, "transient"), (401, "permanent"), (403, "permanent"), (404, "transient"), (429, "transient"), (503, "transient")):
            err = urllib.error.HTTPError("https://api.anthropic.com", code, "boom", {}, None)
            with patch.object(uc, "_read_claude_token", return_value=("secret-token", "test")), \
                 patch("urllib.request.urlopen", side_effect=err):
                _, message, kind = uc._collect_claude_usage_headers()
            self.assertEqual(kind, expected)
            self.assertNotIn("secret-token", message)
        with patch.object(uc, "_read_claude_token", return_value=("secret-token", "test")), \
             patch("urllib.request.urlopen", side_effect=TimeoutError("secret-token")):
            _, message, kind = uc._collect_claude_usage_headers()
        self.assertEqual(kind, "transient")
        self.assertNotIn("secret-token", message)


class TestCollectClaudeAuthoritative(unittest.TestCase):
    """_collect_claude_authoritative() against the REAL api/oauth/usage
    response shape: windows are TOP-LEVEL keys (not nested under
    "rate_limits"), percent comes from "utilization", resets_at is an
    ISO-8601 string, and a window may be null."""

    def _mock_response(self, payload):
        body = json.dumps(payload).encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = body
        cm.__exit__.return_value = False
        return cm

    def test_parses_top_level_windows_with_utilization_and_iso_resets(self):
        payload = {
            "five_hour": {
                "utilization": 10.0,
                "resets_at": "2026-07-22T16:29:59.627285+00:00",
                "limit_dollars": 1, "used_dollars": 0.1, "remaining_dollars": 0.9,
            },
            "seven_day": {
                "utilization": 57.0,
                "resets_at": "2026-07-25T13:59:59.000000+00:00",
            },
            "seven_day_opus": None,
            "seven_day_sonnet": None,
            "seven_day_oauth_apps": {"utilization": 3.0, "resets_at": "2026-07-25T13:59:59Z"},
            "spend": {"amount": 12.3},
            "limits": {"foo": "bar"},
        }
        with patch.object(uc, "_read_claude_token", return_value=("fake-token", "test")), \
             patch.object(uc, "_claude_cli_version", return_value="1.2.3"), \
             patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            windows, err, err_kind = uc._collect_claude_authoritative()

        self.assertIsNone(err)
        self.assertIsNone(err_kind)
        self.assertIn("five_hour", windows)
        self.assertIn("seven_day", windows)
        # nulls skipped gracefully, no crash
        self.assertNotIn("seven_day_opus", windows)
        self.assertNotIn("seven_day_sonnet", windows)
        # unrelated top-level keys ignored
        self.assertNotIn("seven_day_oauth_apps", windows)
        self.assertNotIn("spend", windows)

        self.assertEqual(windows["five_hour"]["used_percent"], 10.0)
        self.assertIsInstance(windows["five_hour"]["resets_at"], float)
        expected_epoch = datetime(2026, 7, 22, 16, 29, 59, 627285, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(windows["five_hour"]["resets_at"], expected_epoch, delta=0.001)
        self.assertEqual(windows["seven_day"]["used_percent"], 57.0)

    def test_all_windows_null_returns_error_not_crash(self):
        payload = {"five_hour": None, "seven_day": None, "seven_day_opus": None, "seven_day_sonnet": None}
        with patch.object(uc, "_read_claude_token", return_value=("fake-token", "test")), \
             patch.object(uc, "_claude_cli_version", return_value="1.2.3"), \
             patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            windows, err, err_kind = uc._collect_claude_authoritative()
        self.assertIsNone(windows)
        self.assertIsNotNone(err)
        self.assertEqual(err_kind, "transient")

    def test_no_token_returns_no_token_kind(self):
        with patch.object(uc, "_read_claude_token", return_value=(None, None)):
            windows, err, err_kind = uc._collect_claude_authoritative()
        self.assertIsNone(windows)
        self.assertIn("no oauth token", err)
        self.assertEqual(err_kind, "no_token")

    def test_403_returns_permanent_kind(self):
        http_err = urllib.error.HTTPError("https://api.anthropic.com/api/oauth/usage", 403, "Forbidden", {}, None)
        with patch.object(uc, "_read_claude_token", return_value=("fake-token", "test")), \
             patch.object(uc, "_claude_cli_version", return_value="1.2.3"), \
             patch("urllib.request.urlopen", side_effect=http_err):
            windows, err, err_kind = uc._collect_claude_authoritative()
        self.assertIsNone(windows)
        self.assertEqual(err_kind, "permanent")
        self.assertIn("403", err)

    def test_429_returns_transient_kind(self):
        http_err = urllib.error.HTTPError("https://api.anthropic.com/api/oauth/usage", 429, "Too Many Requests", {}, None)
        with patch.object(uc, "_read_claude_token", return_value=("fake-token", "test")), \
             patch.object(uc, "_claude_cli_version", return_value="1.2.3"), \
             patch("urllib.request.urlopen", side_effect=http_err):
            windows, err, err_kind = uc._collect_claude_authoritative()
        self.assertIsNone(windows)
        self.assertEqual(err_kind, "transient")
        self.assertIn("429", err)

    def test_5xx_returns_transient_kind(self):
        http_err = urllib.error.HTTPError("https://api.anthropic.com/api/oauth/usage", 503, "Service Unavailable", {}, None)
        with patch.object(uc, "_read_claude_token", return_value=("fake-token", "test")), \
             patch.object(uc, "_claude_cli_version", return_value="1.2.3"), \
             patch("urllib.request.urlopen", side_effect=http_err):
            windows, err, err_kind = uc._collect_claude_authoritative()
        self.assertIsNone(windows)
        self.assertEqual(err_kind, "transient")

    def test_timeout_returns_transient_kind(self):
        with patch.object(uc, "_read_claude_token", return_value=("fake-token", "test")), \
             patch.object(uc, "_claude_cli_version", return_value="1.2.3"), \
             patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            windows, err, err_kind = uc._collect_claude_authoritative()
        self.assertIsNone(windows)
        self.assertEqual(err_kind, "transient")


class TestCollectClaudeSourceOrder(unittest.TestCase):
    def test_direct_headers_stamp_collection_time(self):
        windows = {"five_hour": {"used_percent": 2, "resets_at": 1}, "seven_day": {"used_percent": 70, "resets_at": 2}}
        with patch.object(uc, "_collect_claude_usage_headers", return_value=(windows, None, None)):
            result = uc.collect_claude()
        self.assertEqual(result["source"], "authoritative")
        self.assertIsInstance(result.get("authoritative_collected_at_unix"), float)

    def test_default_never_calls_legacy_after_header_failure(self):
        with patch.object(uc, "_collect_claude_usage_headers", return_value=(None, "timeout", "transient")), \
             patch.object(uc, "_collect_claude_authoritative") as legacy, \
             patch.object(uc, "_read_statusline_cache", return_value=(None, None)), \
             patch.object(uc, "_collect_claude_estimate", return_value={}):
            uc.collect_claude()
        legacy.assert_not_called()

    def test_config_kill_switch_restores_legacy_statusline_first_order_and_ceilings(self):
        windows = {"five_hour": {"used_percent": 2, "resets_at": 1}, "seven_day": {"used_percent": 70, "resets_at": 2}}
        config = dict(uc.CONFIG, claude_usage_headers_enabled=False)
        with patch.object(uc, "CONFIG", config), \
             patch.object(uc, "_collect_claude_usage_headers") as headers, \
             patch.object(uc, "_read_statusline_cache", return_value=(windows, 20.0)) as statusline, \
             patch.object(uc, "_collect_claude_authoritative") as legacy:
            result = uc.collect_claude()
        headers.assert_not_called()
        legacy.assert_not_called()
        statusline.assert_called_once_with(15, {"five_hour": 45, "seven_day": 8 * 60})
        self.assertEqual(result["source"], "authoritative_statusline")
        self.assertIsInstance(result.get("authoritative_collected_at_unix"), float)

    def test_marker_kill_switch_uses_legacy_after_statusline_miss(self):
        windows = {"five_hour": {"used_percent": 2, "resets_at": 1}, "seven_day": {"used_percent": 70, "resets_at": 2}}
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, ".usage-headers-disabled")
            open(marker, "w").close()
            with patch.object(uc, "USAGE_HEADERS_DISABLED_PATH", marker), \
                 patch.object(uc, "_collect_claude_usage_headers") as headers, \
                 patch.object(uc, "_read_statusline_cache", return_value=(None, None)), \
                 patch.object(uc, "_collect_claude_authoritative", return_value=(windows, None, None)) as legacy:
                result = uc.collect_claude()
        headers.assert_not_called()
        legacy.assert_called_once()
        self.assertEqual(result["source"], "authoritative")

    def test_statusline_and_cached_paths_stamp_real_collection_time(self):
        windows = {"five_hour": {"used_percent": 2, "resets_at": 1}, "seven_day": {"used_percent": 70, "resets_at": 2}}
        with patch.object(uc, "_collect_claude_usage_headers", return_value=(None, "timeout", "transient")), \
             patch.object(uc, "_read_statusline_cache", return_value=(windows, 4.0)):
            statusline = uc.collect_claude()
        self.assertAlmostEqual(datetime.now(timezone.utc).timestamp() - statusline["authoritative_collected_at_unix"], 240, delta=2)
        with patch.object(uc, "_collect_claude_usage_headers", return_value=(None, "timeout", "transient")), \
             patch.object(uc, "_read_statusline_cache", return_value=(None, None)), \
             patch.object(uc, "_read_cached_claude_authoritative", return_value=(windows, 5.0)):
            cached = uc.collect_claude()
        self.assertAlmostEqual(datetime.now(timezone.utc).timestamp() - cached["authoritative_collected_at_unix"], 300, delta=2)


class TestReadCachedClaudeAuthoritative(unittest.TestCase):
    """_read_cached_claude_authoritative() -- reused by collect_claude() to
    avoid losing real numbers to a transient 429/5xx during the freshness
    window."""

    def _write_latest(self, path, source, windows, generated_at, collected_at=None):
        with open(path, "w", encoding="utf-8") as f:
            claude = {"source": source, "windows": windows}
            if collected_at is not None:
                claude["authoritative_collected_at_unix"] = collected_at
            json.dump({"generated_at": generated_at, "claude": claude}, f)

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope.json")
            with patch.object(uc, "LATEST_PATH", missing):
                windows, age = uc._read_cached_claude_authoritative(90)
        self.assertIsNone(windows)
        self.assertIsNone(age)

    def test_fresh_authoritative_cache_returned_with_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage-latest.json")
            ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            windows = {"five_hour": {"used_percent": 11.0, "resets_at": 123.0}}
            self._write_latest(path, "authoritative", windows, ts)
            with patch.object(uc, "LATEST_PATH", path):
                got_windows, age = uc._read_cached_claude_authoritative(90)
        self.assertEqual(got_windows, windows)
        self.assertAlmostEqual(age, 5, delta=0.5)

    def test_prefers_real_collection_time_and_accepts_25_minute_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage-latest.json")
            now = datetime.now(timezone.utc)
            windows = {"five_hour": {"used_percent": 11.0, "resets_at": 123.0}}
            self._write_latest(path, "authoritative", windows, now.isoformat(), (now - timedelta(minutes=25)).timestamp())
            with patch.object(uc, "LATEST_PATH", path):
                got, age = uc._read_cached_claude_authoritative(25, now)
            self.assertEqual(got, windows)
            self.assertAlmostEqual(age, 25, delta=0.01)

    def test_future_or_over_25_minute_collection_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage-latest.json")
            now = datetime.now(timezone.utc)
            for offset in (timedelta(minutes=26), timedelta(minutes=-1)):
                self._write_latest(path, "authoritative", {"five_hour": {}}, now.isoformat(), (now - offset).timestamp())
                with patch.object(uc, "LATEST_PATH", path):
                    self.assertEqual(uc._read_cached_claude_authoritative(25, now), (None, None))

    def test_nonfinite_collection_time_is_rejected_without_generated_at_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage-latest.json")
            now = datetime.now(timezone.utc)
            for collected_at in (float("nan"), float("inf"), float("-inf")):
                self._write_latest(path, "authoritative", {"five_hour": {}}, now.isoformat(), collected_at)
                with patch.object(uc, "LATEST_PATH", path):
                    self.assertEqual(uc._read_cached_claude_authoritative(25, now), (None, None))

    def test_stale_cache_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage-latest.json")
            ts = (datetime.now(timezone.utc) - timedelta(minutes=200)).isoformat()
            self._write_latest(path, "authoritative", {"five_hour": {"used_percent": 1}}, ts)
            with patch.object(uc, "LATEST_PATH", path):
                got_windows, age = uc._read_cached_claude_authoritative(90)
        self.assertIsNone(got_windows)
        self.assertIsNone(age)

    def test_estimate_source_cache_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage-latest.json")
            ts = datetime.now(timezone.utc).isoformat()
            self._write_latest(path, "estimate", {}, ts)
            with patch.object(uc, "LATEST_PATH", path):
                got_windows, age = uc._read_cached_claude_authoritative(90)
        self.assertIsNone(got_windows)
        self.assertIsNone(age)

    def test_previously_cached_source_is_itself_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage-latest.json")
            ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            windows = {"seven_day": {"used_percent": 58.0, "resets_at": 456.0}}
            self._write_latest(path, "authoritative_cached", windows, ts)
            with patch.object(uc, "LATEST_PATH", path):
                got_windows, age = uc._read_cached_claude_authoritative(90)
        self.assertEqual(got_windows, windows)

    def test_corrupt_json_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage-latest.json")
            with open(path, "w") as f:
                f.write("{not valid json")
            with patch.object(uc, "LATEST_PATH", path):
                got_windows, age = uc._read_cached_claude_authoritative(90)
        self.assertIsNone(got_windows)
        self.assertIsNone(age)


class TestCollectClaudeCacheFallback(unittest.TestCase):
    """collect_claude() end-to-end: a transient failure (429) must reuse a
    fresh cached authoritative snapshot instead of dropping to the token
    estimate; a stale/absent cache, a 403, or no token must still fall
    back to the estimate."""

    def setUp(self):
        # Isolation fix (2026-09-02, BÉLA, found while verifying the
        # seven_day override change): this class never patched
        # STATUSLINE_CACHE_PATH, so collect_claude()'s statusline check --
        # which runs BEFORE the network path these tests are exercising --
        # was reading the REAL store/usage-statusline-latest.json. That was
        # harmless while the live tracker session didn't exist yet, but
        # once it started actually running (2026-09-01/02) the real file
        # started holding genuinely fresh, valid data, which made these
        # tests short-circuit into authoritative_statusline before ever
        # reaching the mocked 429/403/no-token network paths they're
        # supposed to test -- a real, silent test-isolation gap, not
        # something caused by today's override change (reproduced on a
        # clean git stash of this class too). Point it at a path that
        # never exists so this class stays hermetic regardless of what the
        # live tracker is doing on this host.
        self._statusline_patch = patch.object(uc, "STATUSLINE_CACHE_PATH", "/nonexistent/statusline-cache-for-tests.json")
        self._statusline_patch.start()
        self.addCleanup(self._statusline_patch.stop)

    def _write_latest(self, path, source, windows, generated_at):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"generated_at": generated_at, "claude": {"source": source, "windows": windows}}, f)

    def test_429_with_fresh_cache_reuses_authoritative_cached_no_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest_path = os.path.join(tmp, "usage-latest.json")
            fresh_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            cached_windows = {"five_hour": {"used_percent": 11.0, "resets_at": 1234.0}}
            self._write_latest(latest_path, "authoritative", cached_windows, fresh_ts)

            http_err = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
            with patch.object(uc, "LATEST_PATH", latest_path), \
                 patch.object(uc, "_read_claude_token", return_value=("fake-token", "test")), \
                 patch.object(uc, "_claude_cli_version", return_value="1.2.3"), \
                 patch("urllib.request.urlopen", side_effect=http_err), \
                 patch.object(uc, "_collect_claude_estimate") as mock_estimate:
                result = uc.collect_claude()

            mock_estimate.assert_not_called()
            self.assertEqual(result["source"], "authoritative_cached")
            self.assertEqual(result["windows"], cached_windows)
            self.assertIn("cache_age_minutes", result)
            self.assertLess(result["cache_age_minutes"], 10)

    def test_429_with_stale_cache_falls_back_to_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest_path = os.path.join(tmp, "usage-latest.json")
            stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=200)).isoformat()
            self._write_latest(latest_path, "authoritative", {"five_hour": {"used_percent": 1}}, stale_ts)

            http_err = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
            with patch.object(uc, "LATEST_PATH", latest_path), \
                 patch.object(uc, "_read_claude_token", return_value=("fake-token", "test")), \
                 patch.object(uc, "_claude_cli_version", return_value="1.2.3"), \
                 patch("urllib.request.urlopen", side_effect=http_err), \
                 patch.object(uc, "_collect_claude_estimate", return_value={"seven_day_est_tokens": 42}) as mock_estimate:
                result = uc.collect_claude()

            mock_estimate.assert_called_once()
            self.assertEqual(result["source"], "estimate")
            self.assertEqual(result.get("seven_day_est_tokens"), 42)

    def test_429_with_no_cache_file_falls_back_to_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest_path = os.path.join(tmp, "usage-latest.json")  # never written
            http_err = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
            with patch.object(uc, "LATEST_PATH", latest_path), \
                 patch.object(uc, "_read_claude_token", return_value=("fake-token", "test")), \
                 patch.object(uc, "_claude_cli_version", return_value="1.2.3"), \
                 patch("urllib.request.urlopen", side_effect=http_err), \
                 patch.object(uc, "_collect_claude_estimate", return_value={}) as mock_estimate:
                result = uc.collect_claude()

            mock_estimate.assert_called_once()
            self.assertEqual(result["source"], "estimate")

    def test_403_skips_cache_even_if_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest_path = os.path.join(tmp, "usage-latest.json")
            fresh_ts = datetime.now(timezone.utc).isoformat()
            self._write_latest(latest_path, "authoritative", {"five_hour": {"used_percent": 11}}, fresh_ts)

            http_err = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
            with patch.object(uc, "LATEST_PATH", latest_path), \
                 patch.object(uc, "_read_claude_token", return_value=("fake-token", "test")), \
                 patch.object(uc, "_claude_cli_version", return_value="1.2.3"), \
                 patch("urllib.request.urlopen", side_effect=http_err), \
                 patch.object(uc, "_collect_claude_estimate", return_value={}) as mock_estimate:
                result = uc.collect_claude()

            mock_estimate.assert_called_once()
            self.assertEqual(result["source"], "estimate")
            self.assertIn("403", result["auth_error"])

    def test_no_token_skips_cache_even_if_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest_path = os.path.join(tmp, "usage-latest.json")
            fresh_ts = datetime.now(timezone.utc).isoformat()
            self._write_latest(latest_path, "authoritative", {"five_hour": {"used_percent": 11}}, fresh_ts)

            with patch.object(uc, "LATEST_PATH", latest_path), \
                 patch.object(uc, "_read_claude_token", return_value=(None, None)), \
                 patch.object(uc, "_collect_claude_estimate", return_value={}) as mock_estimate:
                result = uc.collect_claude()

            mock_estimate.assert_called_once()
            self.assertEqual(result["source"], "estimate")


def _resets_at_for_elapsed(now, window_len_sec, elapsed_fraction):
    """Build a resets_at epoch such that, measured from `now`, exactly
    elapsed_fraction of a window_len_sec-long window has passed."""
    return (now + timedelta(seconds=window_len_sec * (1 - elapsed_fraction))).timestamp()


class TestComputePace(unittest.TestCase):
    """compute_pace() -- the time-proportional usage-vs-elapsed model shared
    by every window of every provider."""

    NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    WEEK = 7 * 86400

    def test_missing_used_percent_returns_none(self):
        resets_at = _resets_at_for_elapsed(self.NOW, self.WEEK, 0.2)
        self.assertIsNone(uc.compute_pace(None, resets_at, self.WEEK, self.NOW))

    def test_missing_resets_at_returns_none(self):
        self.assertIsNone(uc.compute_pace(50.0, None, self.WEEK, self.NOW))

    def test_missing_window_length_returns_none(self):
        resets_at = _resets_at_for_elapsed(self.NOW, self.WEEK, 0.2)
        self.assertIsNone(uc.compute_pace(50.0, resets_at, None, self.NOW))
        self.assertIsNone(uc.compute_pace(50.0, resets_at, 0, self.NOW))

    def test_50_percent_at_20_percent_elapsed(self):
        # Example from the spec: 50% used at 20% elapsed -> overuse pace.
        resets_at = _resets_at_for_elapsed(self.NOW, self.WEEK, 0.2)
        pace = uc.compute_pace(50.0, resets_at, self.WEEK, self.NOW)
        self.assertAlmostEqual(pace["elapsed_fraction"], 0.2, places=6)
        self.assertAlmostEqual(pace["used_fraction"], 0.5, places=6)
        self.assertAlmostEqual(pace["pace_ratio"], 2.5, places=6)
        self.assertIsNotNone(pace["projected_exhaustion_epoch"])
        # constant-burn-rate exhaustion happens at elapsed/used_fraction == 2x elapsed time
        expected_epoch = self.NOW.timestamp() + self.WEEK * 0.2
        self.assertAlmostEqual(pace["projected_exhaustion_epoch"], expected_epoch, delta=1.0)
        self.assertLess(pace["projected_exhaustion_epoch"], pace["resets_at"])

    def test_10_percent_at_57_percent_elapsed(self):
        # Example from the spec: 10% used at 57% elapsed -> underuse pace.
        resets_at = _resets_at_for_elapsed(self.NOW, self.WEEK, 0.57)
        pace = uc.compute_pace(10.0, resets_at, self.WEEK, self.NOW)
        self.assertAlmostEqual(pace["elapsed_fraction"], 0.57, places=6)
        self.assertAlmostEqual(pace["pace_ratio"], 10.0 / 57.0, places=4)

    def test_elapsed_fraction_clamped_to_zero_for_future_looking_reset(self):
        # resets_at further out than a full window away -- degenerate/stale
        # input, must clamp instead of going negative.
        resets_at = (self.NOW + timedelta(seconds=self.WEEK * 2)).timestamp()
        pace = uc.compute_pace(10.0, resets_at, self.WEEK, self.NOW)
        self.assertEqual(pace["elapsed_fraction"], 0.0)
        self.assertIsNone(pace["pace_ratio"])  # guarded div-by-~0

    def test_elapsed_fraction_clamped_to_one_when_overdue(self):
        resets_at = (self.NOW - timedelta(hours=1)).timestamp()  # already past reset
        pace = uc.compute_pace(80.0, resets_at, self.WEEK, self.NOW)
        self.assertEqual(pace["elapsed_fraction"], 1.0)
        self.assertAlmostEqual(pace["pace_ratio"], 0.8, places=6)

    def test_zero_used_percent_has_no_projection(self):
        resets_at = _resets_at_for_elapsed(self.NOW, self.WEEK, 0.5)
        pace = uc.compute_pace(0.0, resets_at, self.WEEK, self.NOW)
        self.assertEqual(pace["used_fraction"], 0.0)
        self.assertIsNone(pace["projected_exhaustion_epoch"])


class TestWindowLengthAndIteration(unittest.TestCase):
    """_window_length_seconds() / _iter_pace_windows() -- how windows are
    discovered and sized for the pace engine."""

    def test_claude_window_lengths(self):
        self.assertEqual(uc._window_length_seconds("claude_five_hour", {}), 5 * 3600)
        self.assertEqual(uc._window_length_seconds("claude_seven_day", {}), 7 * 86400)
        self.assertEqual(uc._window_length_seconds("claude_seven_day_opus", {}), 7 * 86400)
        self.assertEqual(uc._window_length_seconds("claude_seven_day_sonnet", {}), 7 * 86400)

    def test_claude_unknown_window_key_returns_none(self):
        self.assertIsNone(uc._window_length_seconds("claude_some_new_window", {}))

    def test_codex_window_length_from_window_minutes(self):
        self.assertEqual(uc._window_length_seconds("codex_weekly", {"window_minutes": 10080}), 10080 * 60)
        self.assertEqual(uc._window_length_seconds("codex_five_hour", {"window_minutes": 300}), 300 * 60)

    def test_codex_missing_window_minutes_returns_none(self):
        self.assertIsNone(uc._window_length_seconds("codex_weekly", {}))

    def test_iter_skips_estimate_source(self):
        snap = {
            "claude": {"ok": True, "source": "estimate", "windows": {
                "seven_day": {"used_percent": 1, "resets_at": 1},
            }},
            "codex": {"ok": True, "source": "authoritative", "windows": {
                "weekly": {"used_percent": 1, "resets_at": 1, "window_minutes": 10080},
            }},
        }
        keys = [k for k, _, _ in uc._iter_pace_windows(snap)]
        self.assertIn("codex_weekly", keys)
        self.assertNotIn("claude_seven_day", keys)

    def test_iter_includes_authoritative_cached_claude(self):
        snap = {
            "claude": {"ok": True, "source": "authoritative_cached", "windows": {
                "seven_day": {"used_percent": 1, "resets_at": 1},
            }},
            "codex": {"ok": False, "source": "authoritative", "windows": {}},
        }
        keys = [k for k, _, _ in uc._iter_pace_windows(snap)]
        self.assertIn("claude_seven_day", keys)

    def test_iter_skips_not_ok(self):
        snap = {
            "claude": {"ok": False, "source": "authoritative", "windows": {
                "seven_day": {"used_percent": 1, "resets_at": 1},
            }},
            "codex": {"ok": False, "source": "authoritative", "windows": {}},
        }
        self.assertEqual(list(uc._iter_pace_windows(snap)), [])


class TestComputeAlerts(unittest.TestCase):
    """compute_alerts() end-to-end: OVERUSE / UNDERUSE / near-exhaustion
    bands, the early-window guard, provider-agnostic pace engine, and
    de-dup throttling -- all against synthetic snapshots with a fixed
    `now` so nothing depends on real wall-clock time."""

    NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    WEEK = 7 * 86400
    FIVE_H = 5 * 3600

    def _resets_at(self, window_len_sec, elapsed_fraction, now=None):
        return _resets_at_for_elapsed(now or self.NOW, window_len_sec, elapsed_fraction)

    def _snapshot(self, claude_ok=True, claude_source="authoritative", claude_windows=None,
                  codex_ok=True, codex_source="authoritative", codex_windows=None):
        return {
            "claude": {"ok": claude_ok, "source": claude_source, "windows": claude_windows or {}},
            "codex": {"ok": codex_ok, "source": codex_source, "windows": codex_windows or {}},
        }

    # --- silence when on-pace / too early ---------------------------------

    def test_no_alerts_when_on_pace(self):
        snap = self._snapshot(
            claude_windows={"seven_day": {"used_percent": 20, "resets_at": self._resets_at(self.WEEK, 0.2)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertEqual(alerts, [])

    def test_early_window_stays_silent_even_if_off_pace(self):
        # 50% used at 5% elapsed would normally be wildly over-pace, but the
        # min-elapsed guard (0.15) suppresses early-window noise.
        snap = self._snapshot(
            claude_windows={"seven_day": {"used_percent": 50, "resets_at": self._resets_at(self.WEEK, 0.05)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertEqual(alerts, [])

    def test_estimate_source_never_alerts(self):
        snap = self._snapshot(
            claude_source="estimate",
            claude_windows={"seven_day": {"used_percent": 99, "resets_at": self._resets_at(self.WEEK, 0.99)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertEqual(alerts, [])

    # --- OVERUSE ------------------------------------------------------------

    def test_overuse_fires_with_projection_50pct_at_20pct_elapsed(self):
        snap = self._snapshot(
            claude_windows={"seven_day": {"used_percent": 50, "resets_at": self._resets_at(self.WEEK, 0.2)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertIn("OVERUSE", a)
        self.assertIn("weekly limit", a)
        self.assertIn("50%", a)
        self.assertIn("20%", a)
        self.assertIn("2.5x", a)
        self.assertIn("run out", a)
        self.assertIn("reset", a)

    def test_overuse_silent_below_pace_ratio_threshold(self):
        # 25% used at 20% elapsed -> pace 1.25, under the 1.5 over-threshold.
        snap = self._snapshot(
            claude_windows={"seven_day": {"used_percent": 25, "resets_at": self._resets_at(self.WEEK, 0.2)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertEqual(alerts, [])

    def test_overuse_works_on_five_hour_window_too(self):
        snap = self._snapshot(
            claude_windows={"five_hour": {"used_percent": 60, "resets_at": self._resets_at(self.FIVE_H, 0.3)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertTrue(any("OVERUSE" in a and "5-hour limit" in a for a in alerts))

    def test_overuse_authoritative_cached_still_fires(self):
        snap = self._snapshot(
            claude_source="authoritative_cached",
            claude_windows={"seven_day": {"used_percent": 50, "resets_at": self._resets_at(self.WEEK, 0.2)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertTrue(any("OVERUSE" in a for a in alerts))

    # --- UNDERUSE -----------------------------------------------------------

    def test_underuse_fires_10pct_at_57pct_elapsed(self):
        snap = self._snapshot(
            claude_windows={"seven_day": {"used_percent": 10, "resets_at": self._resets_at(self.WEEK, 0.57)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertIn("UNDERUSE", a)
        self.assertIn("weekly limit", a)
        self.assertIn("10%", a)
        self.assertIn("57%", a)
        self.assertIn("big project", a)

    def test_underuse_silent_above_pace_ratio_threshold(self):
        # 40% used at 57% elapsed -> pace ~0.70, above the 0.5 under-threshold.
        snap = self._snapshot(
            claude_windows={"seven_day": {"used_percent": 40, "resets_at": self._resets_at(self.WEEK, 0.57)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertEqual(alerts, [])

    def test_underuse_flows_through_codex_window_too(self):
        snap = self._snapshot(
            claude_windows={},
            codex_windows={"weekly": {
                "used_percent": 0, "window_minutes": 10080,
                "resets_at": self._resets_at(10080 * 60, 0.5),
            }},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertTrue(any("UNDERUSE" in a and "Codex weekly limit" in a for a in alerts))

    def test_five_hour_window_never_fires_underuse(self):
        # The 5h window resets constantly -- there's no "stuck" quota to
        # rescue, so under-pace there is the normal resting state, not a
        # signal. Same low pace (10% used at 57% elapsed) that fires
        # UNDERUSE on a weekly window must stay silent on the 5h window.
        snap = self._snapshot(
            claude_windows={"five_hour": {"used_percent": 10, "resets_at": self._resets_at(self.FIVE_H, 0.57)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertFalse(any("UNDERUSE" in a for a in alerts))
        self.assertEqual(alerts, [])

    def test_weekly_window_still_fires_underuse_at_the_same_pace(self):
        # Control case for the above: the exact same 10%@57%-elapsed pace
        # on a weekly-class window still fires -- proves the gate is about
        # window length, not the pace numbers themselves.
        snap = self._snapshot(
            claude_windows={"seven_day": {"used_percent": 10, "resets_at": self._resets_at(self.WEEK, 0.57)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertTrue(any("UNDERUSE" in a for a in alerts))

    def test_five_hour_window_underuse_dedup_state_never_created(self):
        # A band that can never fire on this window shouldn't even grow a
        # dedup-state entry.
        state = {}
        snap = self._snapshot(
            claude_windows={"five_hour": {"used_percent": 10, "resets_at": self._resets_at(self.FIVE_H, 0.57)}},
        )
        uc.compute_alerts(snap, state, now_utc=self.NOW)
        self.assertNotIn("claude_five_hour_under", state)

    def test_codex_five_hour_window_also_exempt_from_underuse(self):
        snap = self._snapshot(
            claude_windows={},
            codex_windows={"five_hour": {
                "used_percent": 5, "window_minutes": 300,
                "resets_at": self._resets_at(300 * 60, 0.57),
            }},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertEqual(alerts, [])

    def test_five_hour_overuse_and_near_exhaustion_still_fire(self):
        # OVERUSE and the near-exhaustion backstop must NOT be gated by
        # underuse_min_window_sec -- only UNDERUSE is.
        snap = self._snapshot(
            claude_windows={"five_hour": {"used_percent": 95, "resets_at": self._resets_at(self.FIVE_H, 0.5)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertTrue(any("OVERUSE" in a and "5-hour limit" in a for a in alerts))
        self.assertTrue(any("nearly exhausted" in a for a in alerts))

    # --- BACKSTOP: near-exhaustion -------------------------------------------

    def test_near_exhaustion_backstop_fires_regardless_of_pace(self):
        # 95% used at 95% elapsed -> pace ~1.0 (neither over nor under band),
        # but the hard backstop (>=92% used) must still fire.
        snap = self._snapshot(
            claude_windows={"seven_day": {"used_percent": 95, "resets_at": self._resets_at(self.WEEK, 0.95)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertTrue(any("nearly exhausted" in a for a in alerts))
        self.assertFalse(any("OVERUSE" in a for a in alerts))
        self.assertFalse(any("UNDERUSE" in a for a in alerts))

    def test_near_exhaustion_and_overuse_can_both_fire(self):
        # 95% used at 50% elapsed -> pace 1.9 (over) AND used >= 92% (near-exhaustion).
        snap = self._snapshot(
            claude_windows={"seven_day": {"used_percent": 95, "resets_at": self._resets_at(self.WEEK, 0.5)}},
        )
        alerts = uc.compute_alerts(snap, {}, now_utc=self.NOW)
        self.assertTrue(any("nearly exhausted" in a for a in alerts))
        self.assertTrue(any("OVERUSE" in a for a in alerts))

    # --- de-dup --------------------------------------------------------------

    def test_overuse_dedup_suppresses_immediate_repeat_then_refires_after_45min(self):
        resets_at = self._resets_at(self.WEEK, 0.2)  # fixed absolute reset instant
        window = {"seven_day": {"used_percent": 50, "resets_at": resets_at}}
        state = {}

        first = uc.compute_alerts(self._snapshot(claude_windows=window), state, now_utc=self.NOW)
        self.assertTrue(any("OVERUSE" in a for a in first))

        # 40 min later: still ongoing, under the 45 min refire interval -- silent.
        soon = self.NOW + timedelta(minutes=40)
        second = uc.compute_alerts(self._snapshot(claude_windows=window), state, now_utc=soon)
        self.assertEqual(second, [])

        # 50 min after the first fire: interval elapsed -- re-fires.
        later = self.NOW + timedelta(minutes=50)
        third = uc.compute_alerts(self._snapshot(claude_windows=window), state, now_utc=later)
        self.assertTrue(any("OVERUSE" in a for a in third))

    def test_underuse_dedup_throttles_for_12_hours(self):
        resets_at = self._resets_at(self.WEEK, 0.57)
        window = {"seven_day": {"used_percent": 10, "resets_at": resets_at}}
        state = {}

        first = uc.compute_alerts(self._snapshot(claude_windows=window), state, now_utc=self.NOW)
        self.assertTrue(any("UNDERUSE" in a for a in first))

        soon = self.NOW + timedelta(hours=6)
        second = uc.compute_alerts(self._snapshot(claude_windows=window), state, now_utc=soon)
        self.assertEqual(second, [])

        later = self.NOW + timedelta(hours=13)
        third = uc.compute_alerts(self._snapshot(claude_windows=window), state, now_utc=later)
        self.assertTrue(any("UNDERUSE" in a for a in third))

    def test_near_exhaustion_dedup_refires_after_45min(self):
        resets_at = self._resets_at(self.WEEK, 0.95)
        window = {"seven_day": {"used_percent": 95, "resets_at": resets_at}}
        state = {}

        first = uc.compute_alerts(self._snapshot(claude_windows=window), state, now_utc=self.NOW)
        self.assertTrue(any("nearly exhausted" in a for a in first))

        soon = self.NOW + timedelta(minutes=30)
        second = uc.compute_alerts(self._snapshot(claude_windows=window), state, now_utc=soon)
        self.assertEqual(second, [])

        later = self.NOW + timedelta(minutes=50)
        third = uc.compute_alerts(self._snapshot(claude_windows=window), state, now_utc=later)
        self.assertTrue(any("nearly exhausted" in a for a in third))


class TestClaudeTokenSources(unittest.TestCase):
    """Token source resolution. The regression this guards: on macOS there is
    no ~/.claude/.credentials.json (Claude Code stores the session token in
    the login Keychain), so the collector fell through to the long-lived
    `claude setup-token` value in .env -- which the oauth/usage endpoint
    rejects with 403. Result: authoritative usage silently degraded to a
    token-count estimate with no percentages and no reset times."""

    KEYCHAIN_PAYLOAD = json.dumps({"claudeAiOauth": {"accessToken": "keychain-tok"}})

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        self._tmp.write("CLAUDE_CODE_OAUTH_TOKEN=env-tok\n")
        self._tmp.close()
        self.addCleanup(os.unlink, self._tmp.name)

    def _security_ok(self, stdout=None):
        return MagicMock(returncode=0, stdout=stdout if stdout is not None else self.KEYCHAIN_PAYLOAD)

    def test_keychain_used_when_credentials_file_absent(self):
        with patch.object(uc.sys, "platform", "darwin"), \
             patch.object(uc.os.path, "exists", side_effect=lambda p: p == self._tmp.name), \
             patch.object(uc, "ENV_PATH", self._tmp.name), \
             patch.object(uc.subprocess, "run", return_value=self._security_ok()):
            token, source = uc._read_claude_token()
        self.assertEqual(token, "keychain-tok")
        self.assertEqual(source, "keychain")

    def test_keychain_beats_env_file(self):
        """The .env token answers 403, so it must never win over the keychain."""
        with patch.object(uc.sys, "platform", "darwin"), \
             patch.object(uc.os.path, "exists", return_value=True), \
             patch.object(uc, "ENV_PATH", self._tmp.name), \
             patch.object(uc.subprocess, "run", return_value=self._security_ok()):
            token, source = uc._read_claude_token()
        self.assertEqual(token, "keychain-tok")
        self.assertEqual(source, "keychain")

    def test_credentials_file_still_wins_over_keychain(self):
        cred = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        cred.write(json.dumps({"claudeAiOauth": {"accessToken": "file-tok"}}))
        cred.close()
        self.addCleanup(os.unlink, cred.name)
        with patch.object(uc.sys, "platform", "darwin"), \
             patch.object(uc.os.path, "expanduser", return_value=cred.name), \
             patch.object(uc.subprocess, "run", return_value=self._security_ok()) as run:
            token, source = uc._read_claude_token()
        self.assertEqual(token, "file-tok")
        self.assertEqual(source, "credentials_file")
        run.assert_not_called()

    def test_non_darwin_never_shells_out_to_security(self):
        with patch.object(uc.sys, "platform", "linux"), \
             patch.object(uc.subprocess, "run") as run:
            self.assertIsNone(uc._read_keychain_token())
        run.assert_not_called()

    def test_security_failure_falls_through_to_env_file(self):
        with patch.object(uc.sys, "platform", "darwin"), \
             patch.object(uc.os.path, "exists", side_effect=lambda p: p == self._tmp.name), \
             patch.object(uc, "ENV_PATH", self._tmp.name), \
             patch.object(uc.subprocess, "run", return_value=MagicMock(returncode=1, stdout="")):
            token, source = uc._read_claude_token()
        self.assertEqual(token, "env-tok")
        self.assertEqual(source, "env_file")

    def test_unparseable_keychain_payload_is_silent(self):
        with patch.object(uc.sys, "platform", "darwin"), \
             patch.object(uc.subprocess, "run", return_value=MagicMock(returncode=0, stdout="not json")):
            self.assertIsNone(uc._read_keychain_token())

    def test_keychain_prompt_timeout_does_not_raise(self):
        """A headless/launchd run can hit a blocking GUI prompt; the bounded
        subprocess must degrade to None, never crash the monitor."""
        with patch.object(uc.sys, "platform", "darwin"), \
             patch.object(uc.subprocess, "run", side_effect=subprocess.TimeoutExpired("security", 10)):
            self.assertIsNone(uc._read_keychain_token())

    def test_no_source_at_all_returns_none_pair(self):
        with patch.object(uc.sys, "platform", "linux"), \
             patch.object(uc.os.path, "exists", return_value=False):
            self.assertEqual(uc._read_claude_token(), (None, None))


class TestStatuslineCache(unittest.TestCase):
    """_read_statusline_cache() -- the dedicated tracker session's statusLine
    export (statusline-usage-export.py), read as a scope-restriction-free
    alternative to the 403'ing /api/oauth/usage call. See
    docs/statusline-usage-tracker-dev-spec-20260901.md."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self._tmp.close()

        def _cleanup_tmp():
            # test_missing_file_returns_none_pair unlinks it itself to
            # exercise the "no cache file at all" path -- tolerate that.
            try:
                os.unlink(self._tmp.name)
            except FileNotFoundError:
                pass

        self.addCleanup(_cleanup_tmp)
        self._patch = patch.object(uc, "STATUSLINE_CACHE_PATH", self._tmp.name)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write(self, payload):
        with open(self._tmp.name, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    @staticmethod
    def _window(pct, resets_at=1234, age_seconds=60):
        """Codex review round 3, 2026-09-01: each window carries its OWN
        collected_at_unix now, not one shared file-level timestamp -- see
        the docstring on _read_statusline_cache for why (a stale window
        must not be able to ride to "fresh" under a sibling's timestamp)."""
        now = datetime.now(timezone.utc).timestamp()
        return {"used_percent": pct, "resets_at": resets_at, "collected_at_unix": now - age_seconds}

    def test_missing_file_returns_none_pair(self):
        os.unlink(self._tmp.name)
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_fresh_valid_cache_returned_with_age(self):
        self._write({
            "source": "statusline",
            "windows": {
                "five_hour": self._window(42, 1234, age_seconds=120),  # 2 min old
                "seven_day": self._window(8, 5678, age_seconds=120),
            },
        })
        windows, age = uc._read_statusline_cache(15)
        self.assertEqual(windows["five_hour"]["used_percent"], 42)
        self.assertEqual(windows["seven_day"]["used_percent"], 8)
        self.assertAlmostEqual(age, 2.0, delta=0.1)

    def test_age_is_the_staler_of_the_two_required_windows(self):
        """The returned age must be the MAX (most conservative), not the
        fresher window's age -- otherwise a genuinely stale window could
        report as fresh via its fresher sibling."""
        self._write({
            "windows": {
                "five_hour": self._window(42, 1234, age_seconds=60),    # 1 min old
                "seven_day": self._window(8, 5678, age_seconds=600),    # 10 min old
            },
        })
        windows, age = uc._read_statusline_cache(15)
        self.assertIsNotNone(windows)
        self.assertAlmostEqual(age, 10.0, delta=0.1)

    def test_one_window_individually_stale_rejects_whole_cache(self):
        """The core fix for the release-blocker: even though BOTH windows
        are present and BOTH values are individually valid, one of them
        being older than max_age_minutes must reject the whole cache --
        not just quietly serve the stale one under a shared/fresher
        timestamp."""
        self._write({
            "windows": {
                "five_hour": self._window(42, 1234, age_seconds=60),        # 1 min old -- fresh
                "seven_day": self._window(8, 5678, age_seconds=20 * 60),    # 20 min old -- stale
            },
        })
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_seven_day_override_lets_slow_moving_window_through(self):
        """2026-09-02 false-stale finding (live usage-monitor heartbeat,
        BÉLA): seven_day's collected_at_unix only advances when its VALUE
        changes (statusline-usage-export.py's documented tradeoff), and the
        weekly percentage can go well over an hour without moving even on a
        perfectly healthy tracker. A per-window override must let a stale-
        LOOKING but otherwise valid seven_day window through without
        touching the five_hour default."""
        self._write({
            "windows": {
                "five_hour": self._window(42, 1234, age_seconds=60),          # 1 min old
                "seven_day": self._window(8, 5678, age_seconds=34 * 60),      # 34 min old
            },
        })
        windows, age = uc._read_statusline_cache(15, {"seven_day": 8 * 60})
        self.assertIsNotNone(windows)
        self.assertEqual(windows["seven_day"]["used_percent"], 8)
        self.assertAlmostEqual(age, 34.0, delta=0.2)

    def test_seven_day_override_does_not_relax_five_hour_ceiling(self):
        """The override is per-window, not global -- a stale five_hour
        window must still reject the whole cache even when a seven_day
        override is configured."""
        self._write({
            "windows": {
                "five_hour": self._window(42, 1234, age_seconds=20 * 60),     # 20 min old -- stale
                "seven_day": self._window(8, 5678, age_seconds=34 * 60),      # 34 min old, but overridden
            },
        })
        self.assertEqual(
            uc._read_statusline_cache(15, {"seven_day": 8 * 60}),
            (None, None),
        )

    def test_seven_day_still_stale_beyond_its_own_override_ceiling(self):
        """A synthetic non-production override still has a hard ceiling."""
        self._write({
            "windows": {
                "five_hour": self._window(42, 1234, age_seconds=60),
                "seven_day": self._window(8, 5678, age_seconds=9 * 60 * 60),  # 9h old > 8h override
            },
        })
        self.assertEqual(
            uc._read_statusline_cache(15, {"seven_day": 8 * 60}),
            (None, None),
        )

    def test_generic_five_hour_override_lets_old_value_through(self):
        """The generic override mechanism remains testable independently of
        the production configuration, which now uses 25 minutes for both
        windows because statusline is a deliberately rare fallback."""
        self._write({
            "windows": {
                "five_hour": self._window(44, 1234, age_seconds=20 * 60),     # 20 min old
                "seven_day": self._window(8, 5678, age_seconds=60),           # 1 min old
            },
        })
        windows, age = uc._read_statusline_cache(15, {"five_hour": 45})
        self.assertIsNotNone(windows)
        self.assertEqual(windows["five_hour"]["used_percent"], 44)
        self.assertAlmostEqual(age, 20.0, delta=0.2)

    def test_five_hour_still_stale_beyond_its_own_override_ceiling(self):
        """A synthetic non-production override still rejects over-age data."""
        self._write({
            "windows": {
                "five_hour": self._window(44, 1234, age_seconds=46 * 60),     # 46 min old > 45 min override
                "seven_day": self._window(8, 5678, age_seconds=60),
            },
        })
        self.assertEqual(
            uc._read_statusline_cache(15, {"five_hour": 45}),
            (None, None),
        )

    def test_five_hour_override_does_not_affect_seven_day_override(self):
        """Synthetic per-window overrides coexist independently; production
        pins both to 25 minutes."""
        self._write({
            "windows": {
                "five_hour": self._window(44, 1234, age_seconds=20 * 60),      # 20 min old, under 45 min
                "seven_day": self._window(8, 5678, age_seconds=34 * 60),       # 34 min old, under 8h
            },
        })
        windows, age = uc._read_statusline_cache(15, {"five_hour": 45, "seven_day": 8 * 60})
        self.assertIsNotNone(windows)
        self.assertEqual(windows["five_hour"]["used_percent"], 44)
        self.assertEqual(windows["seven_day"]["used_percent"], 8)

    def test_configured_five_hour_override_is_25_minutes(self):
        """Pins the actual production CONFIG value (not just the function's
        generic override mechanism) -- catches an accidental edit of the
        constant itself going unnoticed."""
        self.assertEqual(
            uc.CONFIG["claude_statusline_cache_max_age_min_by_window"]["five_hour"],
            25,
        )

    def test_combined_stale_five_hour_and_fresh_seven_day_resolves_authoritative(self):
        """End-to-end (real production CONFIG, not a passed-in override):
        a five_hour window that is fresh under its 25-minute
        ceiling, alongside a fresh seven_day window, must still resolve
        collect_claude() to source=authoritative_statusline -- both windows
        independently satisfy their own configured ceiling, so neither
        should drag the other down."""
        self._write({
            "windows": {
                "five_hour": self._window(44, 1234, age_seconds=20 * 60),   # 20 min old, under 25 min ceiling
                "seven_day": self._window(8, 5678, age_seconds=120),        # 2 min old, under 25 min ceiling
            },
        })
        with patch.object(uc, "_collect_claude_usage_headers", return_value=(None, "auth", "permanent")):
            result = uc.collect_claude()
        self.assertEqual(result["source"], "authoritative_statusline")
        self.assertEqual(result["windows"]["five_hour"]["used_percent"], 44)
        self.assertEqual(result["windows"]["seven_day"]["used_percent"], 8)

    def test_no_override_argument_preserves_prior_default_behavior(self):
        """Backward-compat: every pre-existing call site passes only the
        first argument -- max_age_overrides must default to None and behave
        exactly as before."""
        self._write({
            "windows": {
                "five_hour": self._window(42, 1234, age_seconds=60),
                "seven_day": self._window(8, 5678, age_seconds=20 * 60),
            },
        })
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_configured_seven_day_override_is_25_minutes(self):
        """Pins the actual production CONFIG value (not just the function's
        generic override mechanism) -- catches an accidental edit of the
        constant itself going unnoticed."""
        self.assertEqual(
            uc.CONFIG["claude_statusline_cache_max_age_min_by_window"]["seven_day"],
            25,
        )

    def test_only_five_hour_present_rejected(self):
        """Codex hardening review, 2026-09-01: a partial snapshot (only one
        of the two primary windows) must not be trusted as authoritative --
        the statusLine payload's rate_limits block fills in incrementally,
        not atomically."""
        self._write({"windows": {"five_hour": self._window(42, 1234)}})
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_only_seven_day_present_rejected(self):
        self._write({"windows": {"seven_day": self._window(8, 5678)}})
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_out_of_range_percent_rejected(self):
        self._write({
            "windows": {
                "five_hour": self._window(150, 1234),
                "seven_day": self._window(8, 5678),
            },
        })
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_bool_percent_rejected(self):
        """bool is a subclass of int in Python -- must not sneak past an
        isinstance(x, (int, float)) check as a fake 100%/0%."""
        self._write({
            "windows": {
                "five_hour": self._window(True, 1234),
                "seven_day": self._window(8, 5678),
            },
        })
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_non_numeric_percent_rejected(self):
        self._write({
            "windows": {
                "five_hour": self._window("forty-two", 1234),
                "seven_day": self._window(8, 5678),
            },
        })
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_stale_cache_rejected(self):
        self._write({
            "windows": {
                "five_hour": self._window(42, 1234, age_seconds=20 * 60),  # 20 min old
                "seven_day": self._window(8, 5678, age_seconds=20 * 60),
            },
        })
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_future_timestamp_rejected(self):
        """Clock skew / corrupt data producing a negative age must not be
        treated as 'infinitely fresh'."""
        self._write({
            "windows": {
                "five_hour": self._window(42, 1234, age_seconds=-3600),
                "seven_day": self._window(8, 5678, age_seconds=60),
            },
        })
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_missing_per_window_timestamp_rejected(self):
        """A window dict without its own collected_at_unix (e.g. an old-
        schema file from before this fix, or a malformed write) must not be
        trusted -- there's no shared file-level timestamp to fall back on
        anymore, and treating "no timestamp" as "infinitely fresh" would be
        exactly the bug this schema change fixes."""
        self._write({
            "windows": {
                "five_hour": {"used_percent": 42, "resets_at": 1234},  # no collected_at_unix
                "seven_day": self._window(8, 5678),
            },
        })
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_unparseable_json_returns_none_pair(self):
        with open(self._tmp.name, "w", encoding="utf-8") as f:
            f.write("not json")
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_missing_windows_key_returns_none_pair(self):
        self._write({"source": "statusline"})
        self.assertEqual(uc._read_statusline_cache(15), (None, None))

    def test_empty_windows_dict_returns_none_pair(self):
        self._write({"windows": {}})
        self.assertEqual(uc._read_statusline_cache(15), (None, None))


class TestCollectClaudeStatuslinePriority(unittest.TestCase):
    """collect_claude() must prefer a fresh statusline cache over the
    network fetch entirely -- no network call at all when it's fresh, so a
    dedicated tracker session's export never pays for (or gets masked by) a
    setup-token 403/429 on the /api/oauth/usage endpoint."""

    def test_fresh_statusline_cache_follows_direct_header_failure(self):
        fake_windows = {"five_hour": {"used_percent": 7, "resets_at": 1234}}
        with patch.object(uc, "_read_statusline_cache", return_value=(fake_windows, 1.2)), \
             patch.object(uc, "_collect_claude_usage_headers", return_value=(None, "auth", "permanent")), \
             patch.object(uc, "_collect_claude_authoritative") as mock_network:
            result = uc.collect_claude()
        mock_network.assert_not_called()
        self.assertEqual(result["source"], "authoritative_statusline")
        self.assertEqual(result["windows"], fake_windows)
        self.assertEqual(result["statusline_age_min"], 1.2)

    def test_stale_or_absent_statusline_does_not_retry_legacy_network(self):
        with patch.object(uc, "_collect_claude_usage_headers", return_value=(None, "timeout", "transient")), \
             patch.object(uc, "_read_statusline_cache", return_value=(None, None)), \
             patch.object(uc, "_collect_claude_authoritative") as mock_network, \
             patch.object(uc, "_collect_claude_estimate", return_value={}) as estimate:
            result = uc.collect_claude()
        mock_network.assert_not_called()
        estimate.assert_called_once()
        self.assertEqual(result["source"], "estimate")

    def test_iter_windows_includes_authoritative_statusline_source(self):
        snapshot = {
            "claude": {
                "ok": True,
                "source": "authoritative_statusline",
                "windows": {"five_hour": {"used_percent": 10, "resets_at": _future_ts(hours=2)}},
            },
            "codex": {"ok": False},
        }
        scope_keys = [sk for sk, _, _ in uc._iter_pace_windows(snapshot)]
        self.assertIn("claude_five_hour", scope_keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)

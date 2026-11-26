#!/usr/bin/env python3
"""Isolated unittest suite; never contacts the dashboard or Telegram."""
import importlib.util
import json
import io
import multiprocessing
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parent.parent / "usage-fleet-monitor.py"
spec = importlib.util.spec_from_file_location("usage_fleet_monitor", PATH)
um = importlib.util.module_from_spec(spec)
spec.loader.exec_module(um)
NOW = datetime(2026, 11, 4, 5, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.attempted = []
        self.fail = set()
        self.inactive = set()

    def send_agent(self, recipient, content):
        self.attempted.append((recipient, content))
        if recipient in self.fail:
            raise RuntimeError("secret dashboard-token MUST NOT BE LOGGED")
        self.sent.append((recipient, content))
        return "inactive" if recipient in self.inactive else "sent"

    def send_telegram(self, content):
        return self.send_agent("telegram", content)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)
        self.transport = FakeTransport()

    def usage(self, five=79, seven=50, source="authoritative", age=0, **kwargs):
        data = {"generated_at": (NOW - timedelta(seconds=age)).isoformat(),
                "claude": {"source": source, "windows": {
                    "five_hour": {"used_percent": five, "resets_at": NOW.timestamp() + 3600},
                    "seven_day": {"used_percent": seven, "resets_at": NOW.timestamp() + 86400}}}}
        data["claude"].update(kwargs)
        self.write("usage-latest.json", data)
        return data

    def write(self, name, data):
        (self.store / name).write_text(json.dumps(data), encoding="utf-8")

    def read(self, name=um.STATE):
        return json.loads((self.store / name).read_text(encoding="utf-8"))

    def run_monitor(self, dry=False, now=NOW):
        return um.Monitor(self.store, now, dry, self.transport).run()

    def assert_batch(self, kind):
        self.assertEqual([r for r, _ in self.transport.sent[-5:]], [*um.AGENTS, "telegram"])
        self.assertEqual(self.read()["level"], {"warning": 80, "pause": 90, "resume": 0}[kind])

    def test_stale_estimate_missing_and_invalid_are_silent(self):
        for source, age in (("estimate", 0), ("authoritative", 1501), ("authoritative", -1)):
            with self.subTest(source=source, age=age):
                self.usage(source=source, age=age)
                self.assertEqual(self.run_monitor(), "STALE silent")
                self.assertTrue(self.read(um.STALE)["alerted"])
        (self.store / "usage-latest.json").unlink()
        self.assertEqual(self.run_monitor(), "STALE silent")
        (self.store / "usage-latest.json").write_text("{corrupt", encoding="utf-8")
        self.assertEqual(self.run_monitor(), "STALE silent")
        self.assertEqual(self.transport.sent, [])

    def test_stale_keeps_episode_start_and_last_reliable_metrics(self):
        self.usage(23, 42)
        self.run_monitor()
        self.usage(99, 99, source="estimate")
        self.run_monitor()
        stale = self.read(um.STALE)
        self.assertEqual(stale["last_known_percent_five_hour"], 23)
        self.assertEqual(stale["last_known_percent_seven_day"], 42)
        self.run_monitor(now=NOW + timedelta(hours=2))
        self.assertEqual(stale, self.read(um.STALE))
        self.usage()
        self.run_monitor()
        self.assertFalse((self.store / um.STALE).exists())

    def test_all_authoritative_sources_and_freshness_boundary(self):
        for source in um.SOURCES:
            self.usage(source=source, age=1500)
            self.assertEqual(self.run_monitor(), "OK level=0 silent")

    def test_real_collection_age_cannot_be_rejuvenated_by_generated_at(self):
        self.usage(99, authoritative_collected_at_unix=NOW.timestamp() - 1501)
        self.assertEqual(self.run_monitor(), "STALE silent")
        self.assertEqual(self.transport.sent, [])

    def test_79_to_90_warning(self):
        self.usage()
        self.run_monitor()
        self.usage(90)
        self.assertEqual(self.run_monitor(), "SENT warning level=80")
        self.assert_batch("warning")
        self.assertFalse((self.store / um.PAUSE).exists())

    def test_90_to_98_to_95_to_89(self):
        for value, expected in ((90, "SENT warning level=80"), (98, "SENT pause level=90"),
                                (95, "UNCHANGED level=90"), (89, "SENT resume level=0")):
            self.usage(value)
            self.assertEqual(self.run_monitor(), expected)
        self.assertEqual(len(self.transport.sent), 15)
        self.assertFalse((self.store / um.PAUSE).exists())

    def test_warning_only_clears_silently(self):
        self.usage(90)
        self.run_monitor()
        self.usage(79)
        self.run_monitor()
        self.assertEqual(len(self.transport.sent), 5)
        self.assertEqual(self.read()["level"], 0)

    def test_weekly_threshold_alone_and_both_must_drop(self):
        self.usage(20, 90)
        self.run_monitor()
        self.assertEqual(self.read(um.PAUSE)["metric"], "seven_day")
        self.usage(90, 79)
        self.assertEqual(self.run_monitor(), "UNCHANGED level=90")
        self.usage(79, 79)
        self.assertEqual(self.run_monitor(), "SENT resume level=0")

    def test_both_metrics_together_make_one_batch(self):
        self.usage(98, 99)
        self.run_monitor()
        self.assertEqual(len(self.transport.sent), 5)
        self.assertEqual(self.read()["metric"], "seven_day")
        self.assertIn("Claude 5 órás keret 98%", self.transport.sent[0][1])
        self.assertIn("Claude heti keret 99%", self.transport.sent[0][1])
        self.run_monitor()
        self.assertEqual(len(self.transport.sent), 5)

    def test_duplicate_runs_and_refreshes_do_not_duplicate_messages(self):
        for value in (90, 98):
            self.usage(value)
            self.run_monitor()
            count = len(self.transport.sent)
            for _ in range(5):
                self.run_monitor()
            self.assertEqual(len(self.transport.sent), count)

    def test_material_new_metric_and_five_point_high_water_prevent_spam(self):
        for five, seven, total in ((98, 50, 5), (98.01, 50, 5), (100, 50, 5),
                                    (95, 90, 10), (95, 94.99, 10), (95, 95, 15),
                                    (95, 90, 15), (98, 95, 15), (98, 100, 20)):
            self.usage(five, seven)
            self.run_monitor()
            self.assertEqual(len(self.transport.sent), total)
        self.assertEqual(self.read()["metric"], "seven_day")

    def test_retry_only_failed_agent_and_complete_level_after_success(self):
        self.usage(98)
        self.transport.fail = {"okoska"}
        self.assertIn("RETRY pause", self.run_monitor())
        self.assertEqual(self.read()["level"], 0)
        self.assertEqual(len(self.transport.sent), 4)
        self.transport.fail.clear()
        self.assertEqual(self.run_monitor(), "SENT pause level=90")
        self.assertEqual(len(self.transport.sent), 5)
        self.assertEqual(self.transport.sent[-1][0], "okoska")

    def test_telegram_failure_retries_without_repeating_agents(self):
        self.usage(98)
        self.transport.fail = {"telegram"}
        self.run_monitor()
        self.assertEqual(self.read()["level"], 90)
        self.assertTrue(self.read(um.PAUSE)["paused"])
        self.assertNotIn("pending", self.read())
        self.transport.fail.clear()
        self.run_monitor()
        self.assertEqual(len(self.transport.sent), 5)
        self.assertEqual(self.transport.sent[-1][0], "telegram")

    def test_defensive_mock_inactive_reply_is_not_error(self):
        # Compatibility branch only: production POST queues with HTTP 200.
        self.usage(98)
        self.transport.inactive = {"progi", "kisprogi"}
        self.assertEqual(self.run_monitor(), "SENT pause level=90")
        self.assert_batch("pause")

    def test_corrupt_or_missing_state_starts_at_zero(self):
        self.usage(98)
        for raw in ("broken", "[]", '{"level":7}', '{"level":true}'):
            (self.store / um.STATE).write_text(raw, encoding="utf-8")
            self.transport.sent.clear()
            self.run_monitor()
            self.assert_batch("pause")

    def test_legacy_pause_without_state_can_be_released(self):
        self.usage(79, 79)
        (self.store / um.PAUSE).write_text("paused", encoding="utf-8")
        self.assertEqual(self.run_monitor(), "SENT resume level=0")
        self.assertFalse((self.store / um.PAUSE).exists())

    def test_dry_run_does_not_write_send_or_even_create_store(self):
        self.usage(98)
        before = {p.name: p.read_bytes() for p in self.store.iterdir()}
        self.assertIn('"action": "pause"', self.run_monitor(dry=True))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.store.iterdir()})
        self.assertEqual(self.transport.sent, [])
        absent = self.store / "absent"
        um.Monitor(absent, NOW, True, self.transport).run()
        self.assertFalse(absent.exists())

    def test_messages_are_accented_and_gate_safe(self):
        for value in (90, 98, 89):
            self.usage(value)
            self.run_monitor()
        for recipient, text in self.transport.sent:
            self.assertNotIn("\u2014", text)
            self.assertNotIn(" -- ", text)
            if recipient != "telegram":
                self.assertNotIn("email", text.lower())
        self.assertIn("figyelmeztetés", self.transport.sent[0][1])
        self.assertIn("új", self.transport.sent[0][1])


class StressTests(unittest.TestCase):
    setUp = MonitorTests.setUp
    usage = MonitorTests.usage
    write = MonitorTests.write
    read = MonitorTests.read
    run_monitor = MonitorTests.run_monitor
    def test_exact_thresholds_without_rounding(self):
        for value, level in ((89.99, 0), (89.99999999999999, 0), (90.0, 80),
                             (97.99999999999999, 80), (98.0, 90)):
            with self.subTest(value=value):
                for name in (um.STATE, um.PAUSE):
                    (self.store / name).unlink(missing_ok=True)
                self.usage(value)
                self.run_monitor()
                self.assertEqual(self.read()["level"], level)

    def test_invalid_snapshot_shapes_and_numbers_fail_closed_without_notice(self):
        for value in (None, True, "90", float("nan"), float("inf"), -1, 101, 10**500):
            with self.subTest(value=str(value)[:30]):
                self.usage(value)
                self.assertEqual(self.run_monitor(), "STALE silent")
        for value in ([], None, {"claude": []}, {"claude": {"windows": []}}):
            self.write("usage-latest.json", value)
            self.assertEqual(self.run_monitor(), "STALE silent")
        self.assertEqual(self.transport.sent, [])

    def test_invalid_state_metadata_recovers_as_level_zero(self):
        self.usage(98)
        for bad in ({"level": 90}, {"level": 90, "metric": [], "percent": "oops",
                                   "source": "authoritative", "since": 42}):
            self.write(um.STATE, bad)
            self.assertEqual(um.load_state(self.store / um.STATE)["level"], 0)
            self.transport.sent.clear()
            self.run_monitor()
            self.assertEqual(len(self.transport.sent), 5)

    def test_corrupt_pending_metadata_is_reset_before_any_delivery(self):
        self.usage(98)
        for pending in ([], {"kind": []}, {"kind": [], "target": {"level": 90,
                "metric": "five_hour", "percent": 90, "source": "authoritative", "since": 42}},
                {"kind": "pause", "target": {"level": 1}},
                {"kind": "pause", "delivered": {"telegram": "inactive"}}):
            state = um.empty_state()
            state["pending"] = pending
            self.write(um.STATE, state)
            self.assertEqual(um.load_state(self.store / um.STATE)["level"], 0)
            self.transport.sent.clear()
            self.assertEqual(self.run_monitor(), "SENT pause level=90")
            self.assertEqual(len(self.transport.sent), 5)

    def test_corrupt_notice_metadata_does_not_crash(self):
        self.usage(98)
        self.run_monitor()
        for notices in ([], "bad", {"five_hour": "secret", "seven_day": 10**500}):
            state = self.read()
            state["notified_90"] = notices
            self.write(um.STATE, state)
            self.usage(99)
            self.run_monitor()
            self.assertEqual(self.read()["level"], 90)

    def test_epoch_iso_timezone_and_dst(self):
        self.assertEqual(um.parse_time(str(NOW.timestamp())), NOW)
        self.assertEqual(um.parse_time("2026-11-04T06:00:00+01:00"), NOW)
        self.assertEqual(um.parse_time("2026-11-04T05:00:00Z"), NOW)
        self.assertEqual(um.parse_time("2026-11-04T05:00:00"), NOW)
        self.assertIn("07:00:00 CET", um.reset_time(NOW.timestamp() + 3600))
        summer = datetime(2026, 7, 1, 5, tzinfo=timezone.utc).timestamp()
        self.assertIn("07:00:00 CEST", um.reset_time(summer))
        first = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp()
        second = first + 3600
        self.assertIn("02:30:00 CEST", um.reset_time(first))
        self.assertIn("02:30:00 CET", um.reset_time(second))
        self.assertEqual(um.reset_time(NOW.timestamp() * 1000), "ismeretlen")

    def test_stale_during_pending_sends_nothing_until_reliable(self):
        self.usage(98)
        self.transport.fail = {"iris"}
        self.run_monitor()
        self.transport.fail.clear()
        self.usage(98, source="estimate")
        count = len(self.transport.sent)
        self.run_monitor()
        self.assertEqual(len(self.transport.sent), count)
        self.usage(98)
        self.run_monitor()
        self.assertEqual(len(self.transport.sent), 5)

    def test_reset_while_pause_pending_sends_release_not_old_pause(self):
        self.usage(98)
        self.transport.fail = {"iris"}
        self.run_monitor()
        self.transport.fail.clear()
        self.usage(79)
        self.run_monitor()
        self.assertEqual(len(self.transport.sent), 9)
        self.assertTrue(all("feloldva" in text for _, text in self.transport.sent[-5:]))
        self.assertEqual(self.read()["level"], 0)

    def test_escalation_replaces_pending_warning(self):
        self.usage(90)
        self.transport.fail = {"iris"}
        self.run_monitor()
        self.transport.fail.clear()
        self.usage(98)
        self.run_monitor()
        self.assertEqual(len(self.transport.sent), 9)
        self.assertIn("Claude keret miatti flotta-szünet", self.transport.sent[-5][1])

    def test_fresh_high_usage_during_partial_release_reasserts_pause(self):
        self.usage(98)
        self.run_monitor()
        self.usage(79)
        self.transport.fail = {"iris"}
        self.run_monitor()
        self.transport.fail.clear()
        self.usage(95)
        self.run_monitor()
        self.assertEqual(self.read()["level"], 90)
        self.assertTrue(self.read(um.PAUSE)["paused"])
        self.assertIn("továbbra is", self.transport.sent[-5][1])
        self.assertNotIn(">=90%", self.transport.sent[-5][1])
        total = len(self.transport.sent)
        self.run_monitor()
        self.assertEqual(len(self.transport.sent), total)

    def test_atomic_replace_failure_keeps_original_and_cleans_tmp(self):
        self.write("test.json", {"original": True})
        with patch.object(um.os, "replace", side_effect=OSError("disk full secret")):
            with self.assertRaises(OSError):
                um.atomic_json(self.store / "test.json", {"new": True})
        self.assertEqual(self.read("test.json"), {"original": True})
        self.assertEqual(list(self.store.glob("*.tmp")), [])

    def test_preflight_write_failure_prevents_all_network_effects(self):
        self.usage(98)
        with patch.object(um, "atomic_json", side_effect=OSError("ENOSPC secret")):
            with self.assertRaises(OSError):
                self.run_monitor()
        self.assertEqual(self.transport.sent, [])

    def test_pause_write_failure_prevents_any_delivery_until_enforcement_exists(self):
        self.usage(98)
        original = um.atomic_json

        def fail_pause(path, data):
            if path.name == um.PAUSE:
                raise OSError("ENOSPC")
            return original(path, data)

        with patch.object(um, "atomic_json", side_effect=fail_pause):
            with self.assertRaises(OSError):
                self.run_monitor()
        self.assertEqual(self.read()["level"], 0)
        self.assertEqual(len(self.read()["pending"]["delivered"]), 0)
        self.assertEqual(self.transport.sent, [])
        self.run_monitor()
        self.assertEqual(len(self.transport.sent), 5)
        self.assertEqual(self.read()["level"], 90)

    def test_final_state_write_failure_recovers_without_resending(self):
        self.usage(98)
        original = um.atomic_json

        def fail_final(path, data):
            if path.name == um.STATE and data["level"] == 90 and "pending" not in data:
                raise OSError("EIO")
            return original(path, data)

        with patch.object(um, "atomic_json", side_effect=fail_final):
            with self.assertRaises(OSError):
                self.run_monitor()
        self.assertEqual(self.read()["level"], 0)
        self.assertTrue(self.read(um.PAUSE)["paused"])
        self.run_monitor()
        self.assertEqual(len(self.transport.sent), 5)

    def test_release_unlink_failure_retries_commit_without_resending(self):
        self.usage(98)
        self.run_monitor()
        self.usage(79)
        original = um.remove

        def fail_unlink(path):
            if path.name == um.PAUSE:
                raise PermissionError("denied")
            return original(path)

        with patch.object(um, "remove", side_effect=fail_unlink):
            with self.assertRaises(PermissionError):
                self.run_monitor()
        self.assertEqual(self.read()["level"], 90)
        self.run_monitor()
        self.assertEqual(len(self.transport.sent), 10)
        self.assertFalse((self.store / um.PAUSE).exists())

    def test_receipt_failure_stops_remaining_deliveries(self):
        self.usage(98)
        original = um.atomic_json

        def fail_receipt(path, data):
            if path.name == um.STATE and data.get("pending", {}).get("delivered"):
                raise OSError("EIO")
            return original(path, data)

        with patch.object(um, "atomic_json", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.run_monitor()
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.read()["level"], 0)

    def test_state_read_io_failure_does_not_reset_and_resend(self):
        self.usage(98)
        self.run_monitor()
        original = um.read_json

        def fail_state(path):
            if path.name == um.STATE:
                raise PermissionError("secret")
            return original(path)

        with patch.object(um, "read_json", side_effect=fail_state):
            with self.assertRaises(PermissionError):
                self.run_monitor()
        self.assertEqual(len(self.transport.sent), 5)

    def test_log_redaction_and_rotation(self):
        self.usage(98)
        self.transport.fail = {"iris"}
        self.run_monitor()
        log = self.store / "logs" / "usage-fleet-monitor.log"
        self.assertNotIn("secret", log.read_text())
        self.assertIn("delivery_failure", log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        log.write_text("x" * (1024 * 1024 + 1))
        self.run_monitor()
        self.assertTrue(log.with_suffix(".log.1").exists())
        self.assertLess(log.stat().st_size, 1000)

    def test_even_delivery_error_with_secret_looking_like_code_is_redacted(self):
        self.usage(98)
        with patch.object(self.transport, "send_agent", side_effect=um.DeliveryError("secrettoken")):
            self.run_monitor()
        log = (self.store / "logs" / "usage-fleet-monitor.log").read_text()
        self.assertNotIn("secrettoken", log)
        self.assertIn("delivery_failure", log)

    def test_thousand_oscillations_and_source_changes_have_bounded_notices(self):
        rng = random.Random(4967)
        self.usage(98, 90)
        self.run_monitor()
        for i in range(1000):
            self.usage(rng.choice((85, 89.99, 90, 90.01, 95, 99.99, 100)),
                       rng.choice((80, 85, 90, 95, 100)), source=um.SOURCES[i % 3])
            self.run_monitor()
        # Both windows stay >=80: no repeated crossing notices or release,
        # at most 90/95/100 high-water notices for each of two windows.
        self.assertLessEqual(len(self.transport.sent), 6 * 5)
        self.assertEqual(self.read()["level"], 90)

    def test_dry_run_pending_stale_cleanup_and_release_never_mutate(self):
        self.usage(98)
        self.transport.fail = {"iris"}
        self.run_monitor()
        self.write(um.STALE, {"alerted": True, "since": 42})
        self.usage(79)
        def contents():
            return {str(p.relative_to(self.store)): p.read_bytes()
                    for p in self.store.rglob("*") if p.is_file()}
        before = contents()
        count = len(self.transport.sent)
        self.run_monitor(dry=True)
        self.assertEqual(contents(), before)
        self.assertEqual(len(self.transport.sent), count)

    def test_usage_read_io_error_is_silent_stale(self):
        original = um.read_json
        def fail_usage(path):
            if path.name == "usage-latest.json":
                raise PermissionError("denied")
            return original(path)
        with patch.object(um, "read_json", side_effect=fail_usage):
            self.assertEqual(self.run_monitor(), "STALE silent")
        self.assertEqual(self.transport.sent, [])

    def test_lock_symlink_cannot_overwrite_or_read_another_file(self):
        self.usage(98)
        victim = self.store / "private"
        victim.write_text("secret")
        (self.store / ".usage-fleet-monitor.lock").symlink_to(victim)
        with self.assertRaises(OSError):
            self.run_monitor()
        self.assertEqual(victim.read_text(), "secret")
        self.assertEqual(self.transport.sent, [])

    def test_cli_one_line_nonzero_retry_and_invalid_now(self):
        self.usage(98)
        output = io.StringIO()
        with patch.object(um, "Transport", return_value=self.transport), patch("sys.stdout", output):
            self.transport.fail = {"telegram"}
            code = um.main(["--state-dir", str(self.store), "--now", str(NOW.timestamp())])
        self.assertEqual(code, 1)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        output = io.StringIO()
        with patch("sys.stdout", output):
            code = um.main(["--state-dir", str(self.store), "--now", "secret-invalid"])
        self.assertEqual(code, 1)
        self.assertNotIn("secret", output.getvalue())

    def test_two_processes_only_one_sends(self):
        self.usage(98)
        ctx = multiprocessing.get_context("fork")
        entered, release = ctx.Event(), ctx.Event()
        results = ctx.Queue()
        store = self.store

        class BlockingTransport(FakeTransport):
            def send_agent(self, recipient, content):
                if recipient == "progi":
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test release timeout")
                with (store / "sent.jsonl").open("a") as stream:
                    stream.write(json.dumps({"recipient": recipient}) + "\n")
                return "sent"

        def worker():
            results.put(um.Monitor(store, NOW, transport=BlockingTransport()).run())

        first = ctx.Process(target=worker)
        first.start()
        try:
            self.assertTrue(entered.wait(5))
            second = ctx.Process(target=worker)
            second.start()
            second.join(5)
            self.assertFalse(second.is_alive())
            self.assertEqual(results.get(timeout=2), "BUSY another_run_holds_lock")
        finally:
            release.set()
            first.join(5)
            if first.is_alive():
                first.terminate()
                first.join()
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(results.get(timeout=2), "SENT pause level=90")
        self.assertEqual(len((store / "sent.jsonl").read_text().splitlines()), 5)
        self.assertIn("UNCHANGED", self.run_monitor())
        self.assertEqual(self.transport.sent, [])


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / "store"
        self.store.mkdir()
        (self.store / ".dashboard-token").write_text("dashboard-secret")
        (self.root / ".env").write_text("TELEGRAM_BOT_TOKEN=123:test-secret\nALLOWED_CHAT_ID=wrong\n")
        self.transport = um.Transport(self.store, self.root)

    def respond(self, data, status=200):
        response = unittest.mock.MagicMock()
        response.status = status
        response.read.return_value = json.dumps(data).encode()
        response.__enter__.return_value = response
        self.transport.opener.open = unittest.mock.Mock(return_value=response)

    def test_dashboard_pending_api_receipt_is_not_runtime_delivery_ack(self):
        self.respond({"id": 123, "status": "pending"})
        self.assertEqual(self.transport.send_agent("progi", "szöveg"), "sent")
        req = self.transport.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "http://localhost:3420/api/messages")
        self.assertEqual(req.get_header("Authorization"), "Bearer dashboard-secret")
        self.assertEqual(json.loads(req.data), {"from": "bela", "to": "progi", "content": "szöveg"})

    def test_fixed_telegram_chat_and_plain_text(self):
        self.respond({"ok": True, "result": {"message_id": 42}})
        self.assertEqual(self.transport.send_telegram("szöveg"), "sent")
        req = self.transport.opener.open.call_args.args[0]
        self.assertEqual(json.loads(req.data), {"chat_id": "6209177290", "text": "szöveg"})

    def test_telegram_http_200_with_ok_false_is_failure(self):
        self.respond({"ok": False, "description": "token secret"})
        with self.assertRaisesRegex(um.DeliveryError, "telegram_rejected"):
            self.transport.send_telegram("szöveg")

    def test_defensive_inactive_mock_requires_specific_recipient_rejection(self):
        # No such recipient rejection exists in the current production API.
        for payload, status, skipped in (({"code": "AGENT_NOT_RUNNING"}, 409, True),
                                         ({"error": "Agent progi is not running"}, 400, True),
                                         ({"error": "unauthorized"}, 401, False),
                                         ({"error": "not found"}, 404, False),
                                         ({"error": "sender not running"}, 400, False)):
            self.respond(payload, status)
            if skipped:
                self.assertEqual(self.transport.send_agent("progi", "szöveg"), "inactive")
            else:
                with self.assertRaises(um.DeliveryError):
                    self.transport.send_agent("progi", "szöveg")

    def test_transport_network_exception_contains_no_url_or_secret(self):
        self.transport.opener.open = unittest.mock.Mock(side_effect=RuntimeError(
            "https://api.telegram.org/bot123:test-secret/sendMessage dashboard-secret"))
        with self.assertRaisesRegex(um.DeliveryError, "^network_failure$"):
            self.transport.send_telegram("szöveg")

    def test_redirect_is_not_followed(self):
        self.assertIsNone(um.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))


class ReviewFixTests(unittest.TestCase):
    setUp = MonitorTests.setUp
    usage = MonitorTests.usage
    write = MonitorTests.write
    read = MonitorTests.read
    run_monitor = MonitorTests.run_monitor

    def test_pause_file_exists_before_first_agent_or_telegram_attempt(self):
        self.usage(98)
        original = self.transport.send_agent
        def check_before_send(recipient, text):
            self.assertTrue(self.read(um.PAUSE)["paused"])
            state = self.read()
            self.assertTrue("pending" in state or state["level"] == 90)
            return original(recipient, text)
        with patch.object(self.transport, "send_agent", side_effect=check_before_send):
            self.run_monitor()
        self.assertEqual(len(self.transport.sent), 5)

    def test_all_remote_failures_leave_technical_pause_enabled(self):
        self.usage(98)
        self.transport.fail = {*um.AGENTS, "telegram"}
        result = self.run_monitor()
        self.assertIn("RETRY pause", result)
        self.assertTrue(self.read(um.PAUSE)["paused"])
        self.assertEqual(self.read()["level"], 0)
        self.assertEqual(self.read()["telegram_pending"]["attempts"], 1)

    def test_pause_telegram_failure_does_not_hold_level(self):
        self.usage(98)
        self.transport.fail = {"telegram"}
        self.assertIn("telegram=retry(1/3)", self.run_monitor())
        self.assertEqual(self.read()["level"], 90)
        self.assertNotIn("pending", self.read())
        self.assertTrue(um.pause_is_active(self.store / um.PAUSE))

    def test_resume_telegram_failure_does_not_hold_level_or_pause(self):
        self.usage(98)
        self.run_monitor()
        self.transport.fail = {"telegram"}
        self.usage(79)
        result = self.run_monitor()
        self.assertIn("SENT resume level=0", result)
        self.assertIn("telegram=retry(1/3)", result)
        self.assertEqual(self.read()["level"], 0)
        self.assertFalse((self.store / um.PAUSE).exists())
        self.assertNotIn("pending", self.read())
        self.assertEqual(self.read()["telegram_pending"]["kind"], "resume")
        self.run_monitor()
        agents = [r for r, _ in self.transport.attempted if r in um.AGENTS]
        self.assertEqual(len(agents), 8)

    def test_resume_agents_must_close_before_release_and_owner_confirmation(self):
        self.usage(98)
        self.run_monitor()
        self.usage(79)
        self.transport.fail = {"iris"}
        self.assertIn("telegram=deferred_until_agents", self.run_monitor())
        self.assertTrue((self.store / um.PAUSE).exists())
        self.assertEqual(self.read()["level"], 90)
        self.assertEqual(self.read()["telegram_pending"]["attempts"], 0)
        self.assertEqual(len([r for r, _ in self.transport.attempted if r == "telegram"]), 1)
        self.transport.fail.clear()
        self.run_monitor()
        self.assertEqual(self.read()["level"], 0)
        self.assertFalse((self.store / um.PAUSE).exists())
        self.assertEqual(len([r for r, _ in self.transport.attempted if r == "telegram"]), 2)

    def test_telegram_is_abandoned_after_three_runs_without_control_repetition(self):
        self.usage(98)
        self.transport.fail = {"telegram"}
        for attempt in (1, 2):
            self.assertIn(f"telegram=retry({attempt}/3)", self.run_monitor())
            self.assertEqual(self.read()["telegram_pending"]["attempts"], attempt)
        self.assertIn("telegram=abandoned(3/3)", self.run_monitor())
        self.assertNotIn("telegram_pending", self.read())
        for _ in range(5):
            self.run_monitor()
        self.assertEqual(len([r for r, _ in self.transport.attempted if r == "telegram"]), 3)
        self.assertEqual(len([r for r, _ in self.transport.attempted if r in um.AGENTS]), 4)
        self.assertEqual(self.read()["level"], 90)
        self.assertIn("telegram_abandoned", (self.store / "logs" / "usage-fleet-monitor.log").read_text())

    def test_telegram_limit_survives_persistent_agent_failure(self):
        self.usage(98)
        self.transport.fail = {"iris", "telegram"}
        for _ in range(5):
            self.run_monitor()
        self.assertEqual(len([r for r, _ in self.transport.attempted if r == "telegram"]), 3)
        self.assertTrue(self.read()["pending"]["telegram_queued"])
        self.assertNotIn("telegram_pending", self.read())
        self.transport.fail.clear()
        self.run_monitor()
        self.assertEqual(self.read()["level"], 90)
        self.assertNotIn("pending", self.read())
        self.assertEqual(len([r for r, _ in self.transport.attempted if r == "telegram"]), 3)

    def test_expired_numeric_reset_in_either_window_is_stale(self):
        for metric in um.METRICS:
            for reset in (NOW.timestamp(), NOW.timestamp() - 0.001, 0, -10**500, float("-inf")):
                with self.subTest(metric=metric, reset=str(reset)[:30]):
                    data = self.usage(99)
                    data["claude"]["windows"][metric]["resets_at"] = reset
                    self.write("usage-latest.json", data)
                    self.assertEqual(self.run_monitor(), "STALE silent")
        self.assertEqual(self.transport.attempted, [])

    def test_missing_reset_is_allowed_and_displayed_as_unknown(self):
        data = self.usage(98)
        for metric in um.METRICS:
            data["claude"]["windows"][metric].pop("resets_at")
        self.write("usage-latest.json", data)
        self.run_monitor()
        self.assertEqual(self.read()["level"], 90)
        self.assertIn("reset: ismeretlen", self.transport.sent[0][1])

    def test_future_reset_and_exact_now_boundary(self):
        data = self.usage(98)
        data["claude"]["windows"]["five_hour"]["resets_at"] = NOW.timestamp() + 0.001
        self.write("usage-latest.json", data)
        self.assertIsNotNone(um.snapshot(self.store / "usage-latest.json", NOW))
        data["claude"]["windows"]["five_hour"]["resets_at"] = NOW.timestamp()
        self.write("usage-latest.json", data)
        self.assertIsNone(um.snapshot(self.store / "usage-latest.json", NOW))

    def test_pause_read_semantics_match_router_including_empty_legacy_file(self):
        for raw, expected in (("", False), ("  \n", False), ("garbage", False), ("[]", False),
                              ("{}", False), ('{"paused":false}', False), ('{"paused":1}', False),
                              ('{"paused":"true"}', False), ('"paused"', False),
                              ("paused", True), (" \npaused\n", True), ('{"paused":true}', True)):
            with self.subTest(raw=raw):
                (self.store / um.PAUSE).write_text(raw)
                self.assertEqual(um.pause_is_active(self.store / um.PAUSE), expected)
        with patch.object(um.Path, "read_text", side_effect=PermissionError("secret")):
            self.assertFalse(um.pause_is_active(self.store / um.PAUSE))

    def test_empty_corrupt_or_false_pause_file_does_not_cause_false_resume(self):
        self.usage(79)
        for raw in ("", " \n", "bad json", '{"paused":false}', "[]"):
            (self.store / um.PAUSE).write_text(raw)
            self.assertEqual(self.run_monitor(), "OK level=0 silent")
        self.assertEqual(self.transport.attempted, [])

    def test_stale_snapshot_suspends_telegram_retries_without_spending_budget(self):
        self.usage(98)
        self.transport.fail = {"telegram"}
        self.run_monitor()
        self.usage(98, source="estimate")
        for _ in range(5):
            self.assertEqual(self.run_monitor(), "STALE silent")
        self.assertEqual(self.read()["telegram_pending"]["attempts"], 1)
        self.assertEqual(len([r for r, _ in self.transport.attempted if r == "telegram"]), 1)

    def test_new_resume_supersedes_failed_pause_telegram(self):
        self.usage(98)
        self.transport.fail = {"telegram"}
        self.run_monitor()
        self.transport.fail.clear()
        self.usage(79)
        self.run_monitor()
        telegrams = [text for recipient, text in self.transport.attempted if recipient == "telegram"]
        self.assertEqual(len(telegrams), 2)
        self.assertIn("feloldva", telegrams[-1])
        self.assertNotIn("telegram_pending", self.read())

    def test_warning_only_release_discards_obsolete_telegram(self):
        self.usage(90)
        self.transport.fail = {"telegram"}
        self.run_monitor()
        self.usage(79)
        self.run_monitor()
        self.assertNotIn("telegram_pending", self.read())
        self.assertEqual(len([r for r, _ in self.transport.attempted if r == "telegram"]), 1)

    def test_failed_resume_telegram_does_not_reassert_pause_at_95(self):
        self.usage(98)
        self.run_monitor()
        self.usage(79)
        self.transport.fail = {"telegram"}
        self.run_monitor()
        self.usage(95)
        self.run_monitor()
        self.assertEqual(self.read()["level"], 80)
        self.assertFalse((self.store / um.PAUSE).exists())
        self.assertEqual(self.read()["telegram_pending"]["kind"], "warning")

    def test_old_pending_event_with_telegram_receipt_migrates_without_resending(self):
        self.usage(98)
        monitor = um.Monitor(self.store, NOW, transport=self.transport)
        event = monitor.event("pause", um.snapshot(self.store / "usage-latest.json", NOW), um.empty_state())
        event["delivered"] = {r: "sent" for r in (*um.AGENTS, "telegram")}
        self.write(um.STATE, {**um.empty_state(), "pending": event})
        self.run_monitor()
        self.assertEqual(self.read()["level"], 90)
        self.assertTrue(um.pause_is_active(self.store / um.PAUSE))
        self.assertEqual(self.transport.attempted, [])

    def test_dry_run_existing_telegram_retry_does_not_spend_attempt(self):
        self.usage(98)
        self.transport.fail = {"telegram"}
        self.run_monitor()
        before = {str(p): p.read_bytes() for p in self.store.rglob("*") if p.is_file()}
        self.assertIn("telegram=would_try(2/3)", self.run_monitor(dry=True))
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.store.rglob("*") if p.is_file()})

    def test_telegram_attempt_reservation_failure_prevents_network(self):
        self.usage(98)
        self.transport.fail = {"telegram"}
        self.run_monitor()
        original = um.atomic_json
        def fail_reserve(path, data):
            if path.name == um.STATE and data.get("telegram_pending", {}).get("attempts") == 2:
                raise OSError("ENOSPC")
            return original(path, data)
        with patch.object(um, "atomic_json", side_effect=fail_reserve):
            with self.assertRaises(OSError):
                self.run_monitor()
        self.assertEqual(len([r for r, _ in self.transport.attempted if r == "telegram"]), 1)
        self.assertEqual(self.read()["telegram_pending"]["attempts"], 1)

    def test_recover_pause_decision_before_freshness_check_after_crash(self):
        self.usage(98)
        original = um.atomic_json
        def fail_pause(path, data):
            if path.name == um.PAUSE:
                raise OSError("ENOSPC")
            return original(path, data)
        with patch.object(um, "atomic_json", side_effect=fail_pause):
            with self.assertRaises(OSError):
                self.run_monitor()
        self.assertFalse((self.store / um.PAUSE).exists())
        self.usage(98, source="estimate")
        self.assertEqual(self.run_monitor(), "STALE silent")
        self.assertTrue(um.pause_is_active(self.store / um.PAUSE))
        self.assertEqual(self.transport.attempted, [])

    def test_pause_and_resume_recover_after_every_durable_write_boundary(self):
        class Crash(BaseException):
            pass
        original = um.atomic_json
        original_store, original_transport = self.store, self.transport
        try:
            for kind, count in (("pause", 9), ("resume", 8)):
                for stop in range(1, count + 1):
                    with self.subTest(kind=kind, stop=stop), tempfile.TemporaryDirectory() as scratch:
                        self.store, self.transport = Path(scratch), FakeTransport()
                        self.usage(98)
                        if kind == "resume":
                            self.run_monitor()
                            self.usage(79)
                        writes = 0
                        def crash_after_write(path, data):
                            nonlocal writes
                            original(path, data)
                            writes += 1
                            if writes == stop:
                                raise Crash()
                        with patch.object(um, "atomic_json", side_effect=crash_after_write):
                            with self.assertRaises(Crash):
                                self.run_monitor()
                        self.run_monitor()
                        self.run_monitor()
                        self.assertEqual(self.read()["level"], 90 if kind == "pause" else 0)
                        self.assertEqual(um.pause_is_active(self.store / um.PAUSE), kind == "pause")
                        self.assertNotIn("pending", self.read())
                        self.assertNotIn("telegram_pending", self.read())
                        self.assertEqual(len(self.transport.sent), 5 if kind == "pause" else 10)
        finally:
            self.store, self.transport = original_store, original_transport

    def test_resume_recovers_crash_after_pause_unlink_without_resending_agents(self):
        class Crash(BaseException):
            pass
        self.usage(98)
        self.run_monitor()
        self.usage(79)
        original = um.remove
        def crash_after_remove(path):
            original(path)
            if path.name == um.PAUSE:
                raise Crash()
        with patch.object(um, "remove", side_effect=crash_after_remove):
            with self.assertRaises(Crash):
                self.run_monitor()
        self.assertFalse((self.store / um.PAUSE).exists())
        self.assertEqual(self.read()["level"], 90)
        self.run_monitor()
        self.assertEqual(self.read()["level"], 0)
        self.assertEqual(len(self.transport.sent), 10)

    def test_third_attempt_crash_is_not_a_fourth_send_after_restart(self):
        class Crash(BaseException):
            pass
        self.usage(98)
        self.transport.fail = {"telegram"}
        self.run_monitor()
        self.run_monitor()
        with patch.object(self.transport, "send_telegram", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.run_monitor()
        self.assertEqual(self.read()["telegram_pending"]["attempts"], 3)
        count = len(self.transport.attempted)
        self.assertIn("telegram=abandoned(3/3)", self.run_monitor())
        self.assertEqual(len(self.transport.attempted), count)
        self.assertNotIn("telegram_pending", self.read())

    def test_corrupt_optional_telegram_does_not_reset_completed_pause(self):
        self.usage(98)
        self.run_monitor()
        state = self.read()
        state["telegram_pending"] = {"kind": "pause", "text": "bad", "attempts": True}
        self.write(um.STATE, state)
        self.run_monitor()
        self.assertEqual(self.read()["level"], 90)
        self.assertEqual(len(self.transport.sent), 5)

    def test_reserved_attempts_are_not_reset_after_abandon_write_failure(self):
        self.usage(98)
        self.transport.fail = {"telegram"}
        self.run_monitor()
        self.run_monitor()
        original = um.atomic_json
        def fail_drop(path, data):
            if path.name == um.STATE and "telegram_pending" not in data:
                raise OSError("EIO")
            return original(path, data)
        with patch.object(um, "atomic_json", side_effect=fail_drop):
            with self.assertRaises(OSError):
                self.run_monitor()
        self.assertEqual(self.read()["telegram_pending"]["attempts"], 3)
        self.run_monitor()
        self.assertEqual(len([r for r, _ in self.transport.attempted if r == "telegram"]), 3)

    def test_existing_outbox_budget_survives_missing_migration_marker(self):
        self.usage(98)
        self.transport.fail = {"iris", "telegram"}
        self.run_monitor()
        self.run_monitor()
        state = self.read()
        state["pending"].pop("telegram_queued")
        self.write(um.STATE, state)
        self.assertIn("telegram=abandoned(3/3)", self.run_monitor())
        self.assertNotIn("telegram_pending", self.read())
        self.assertEqual(len([r for r, _ in self.transport.attempted if r == "telegram"]), 3)


if __name__ == "__main__":
    unittest.main()

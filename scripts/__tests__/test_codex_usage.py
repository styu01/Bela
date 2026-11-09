"""Synthetic quota tests: no real sessions, credentials, network or store writes."""

import copy
import random
import errno
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collect = load("codex-usage-collect")
check = load("codex-usage-check")
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def row(age=0, used=12, weekly=35):
    return {"timestamp": (NOW - timedelta(seconds=age)).isoformat(),
            "payload": {"rate_limits": {
                "limit_id": "codex", "plan_type": "plus",
                "primary": {"used_percent": used, "window_minutes": 300,
                            "resets_at": int(NOW.timestamp()) + 18000},
                "secondary": {"used_percent": weekly, "window_minutes": 10080,
                              "resets_at": int(NOW.timestamp()) + 604800},
                "credits": {"has_credits": False, "unlimited": False, "balance": "0"}}}}


class CodexUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / "sessions"

    def write(self, name="one", rows=None, mtime=100, prefix=""):
        path = self.sessions / "2026/11/01" / ("rollout-" + name + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        content = prefix + "".join(json.dumps(item) + "\n" for item in (rows or []))
        path.write_text(content, encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def snapshot(self):
        return collect.collect(self.sessions, NOW)

    def test_fresh_and_allowlisted_schema(self):
        event = row()
        event["payload"]["conversation"] = "DO-NOT-EXPORT"
        event["payload"]["rate_limits"]["credits"]["secret"] = "DO-NOT-EXPORT"
        self.write(rows=[event])
        snapshot = self.snapshot()
        self.assertEqual(snapshot["freshness"], "fresh")
        self.assertEqual(snapshot["source"], "codex-session-log")
        self.assertEqual(snapshot["observed_at"], event["timestamp"])
        self.assertEqual(snapshot["plan_type"], "plus")
        self.assertEqual(snapshot["windows"]["five_hour"]["used_percent"], 12)
        self.assertEqual(snapshot["credits"], {"has_credits": False, "unlimited": False, "balance": "0"})
        self.assertNotIn("DO-NOT-EXPORT", json.dumps(snapshot))

    def test_stale_and_exact_freshness_boundary(self):
        for age, expected in ((1800, "fresh"), (1801, "stale"), (-1, "stale")):
            with self.subTest(age=age):
                self.write(rows=[row(age=age)])
                self.assertEqual(self.snapshot()["freshness"], expected)

    def test_missing_directory(self):
        self.assertEqual(self.snapshot()["freshness"], "missing")

    def test_empty_file(self):
        self.write()
        self.assertEqual(self.snapshot()["freshness"], "missing")

    def test_corrupt_only(self):
        self.write(prefix='{"payload":{"rate_limits": broken\n')
        self.assertEqual(self.snapshot()["freshness"], "missing")

    def test_corrupt_lines_and_partial_last_line_do_not_hide_valid_row(self):
        path = self.write(rows=[row()], prefix='{"rate_limits": invalid}\n')
        with path.open("a") as stream:
            stream.write('{"payload":{"rate_limits":')
        self.assertEqual(self.snapshot()["freshness"], "fresh")

    def test_non_object_json_and_payload(self):
        self.write(rows=[["rate_limits"], {"payload": ["rate_limits"]},
                         {"payload": {"rate_limits": []}}])
        self.assertEqual(self.snapshot()["freshness"], "missing")

    def test_fallback_when_latest_modified_file_has_no_limits(self):
        self.write("older", [row()], mtime=100)
        self.write("newer", [], mtime=200)
        self.assertEqual(self.snapshot()["freshness"], "fresh")

    def test_latest_timestamp_wins_over_file_mtime_and_line_order(self):
        self.write("newest-mtime", [row(age=500, used=10)], mtime=300)
        self.write("older-mtime", [row(age=10, used=20), row(age=100, used=30)], mtime=100)
        self.assertEqual(self.snapshot()["windows"]["five_hour"]["used_percent"], 20)

    def test_only_eight_latest_files_are_opened(self):
        for i in range(12):
            self.write(str(i), [row(used=i)], mtime=i)
        with patch.object(collect.os, "open", wraps=os.open) as opened:
            self.snapshot()
        self.assertEqual(opened.call_count, collect.MAX_FILES)
        self.assertEqual({Path(call.args[0]).stem for call in opened.call_args_list},
                         {"rollout-" + str(i) for i in range(4, 12)})

    def test_window_names_do_not_determine_duration(self):
        event = row()
        limits = event["payload"]["rate_limits"]
        limits["primary"], limits["secondary"] = limits["secondary"], limits["primary"]
        self.write(rows=[event])
        self.assertEqual(self.snapshot()["windows"]["seven_day"]["used_percent"], 35)

    def test_invalid_percentages_rejected(self):
        for used in (-1, 101, float("nan"), float("inf"), True, "12", None):
            with self.subTest(used=used):
                self.write(rows=[row(used=used)])
                self.assertEqual(self.snapshot()["freshness"], "missing")

    def test_invalid_window_fields_rejected(self):
        for key, value in (("resets_at", -1), ("resets_at", True),
                           ("resets_at", float("inf")), ("resets_at", 10**1000),
                           ("window_minutes", 60), ("window_minutes", 300.0)):
            with self.subTest(key=key, value=value):
                event = row()
                event["payload"]["rate_limits"]["primary"][key] = value
                self.write(rows=[event])
                self.assertEqual(self.snapshot()["freshness"], "missing")

    def test_invalid_timestamp_missing_window_and_other_limit(self):
        bad_time, missing_window, missing_primary, other_limit = row(), row(), row(), row()
        bad_time["timestamp"] = "not-a-time"
        del missing_window["payload"]["rate_limits"]["secondary"]
        del missing_primary["payload"]["rate_limits"]["primary"]
        other_limit["payload"]["rate_limits"]["limit_id"] = "other-model"
        for event in (bad_time, missing_window, missing_primary, other_limit):
            with self.subTest(event=event):
                self.write(rows=[event])
                self.assertEqual(self.snapshot()["freshness"], "missing")

    def test_auth_and_symlinks_never_opened(self):
        # A fake forbidden file; no real credential file is touched.
        forbidden = self.root / "auth.json"
        forbidden.write_text("DO-NOT-READ", encoding="utf-8")
        link = self.write()
        link.unlink()
        link.symlink_to(forbidden)
        with patch.object(collect.os, "open", side_effect=AssertionError("unexpected read")):
            self.assertEqual(self.snapshot()["freshness"], "missing")

    def test_unreadable_log_is_missing(self):
        self.write(rows=[row()])
        with patch.object(collect.os, "open", side_effect=PermissionError):
            self.assertEqual(self.snapshot()["freshness"], "missing")

    def test_bounded_lines_skip_oversize_invalid_encoding_nul_and_partial(self):
        valid = json.dumps(row()).encode() + b"\n"
        data = (b'x' * (collect.MAX_LINE_BYTES * 3) + b'\n'
                + valid.replace(b'"plus"', b'"\xff"')
                + b'\x00' + valid + valid + valid[:-1])
        stream = io.BytesIO(data)
        self.assertEqual(list(collect.quota_rows(stream, len(data))), [row()])

    def test_reader_ignores_growth_after_initial_size(self):
        valid = json.dumps(row()).encode() + b"\n"
        newer = json.dumps(row(age=-100)).encode() + b"\n"
        self.assertEqual(list(collect.quota_rows(io.BytesIO(valid + newer), len(valid))), [row()])

    def test_directory_symlink_never_opened(self):
        self.sessions.mkdir()
        outside = self.root / "outside/11/01"
        outside.mkdir(parents=True)
        (outside / "rollout-forbidden.jsonl").write_text(json.dumps(row()) + "\n")
        (self.sessions / "2026").symlink_to(self.root / "outside", target_is_directory=True)
        with patch.object(collect.os, "open", side_effect=AssertionError("unexpected open")):
            self.assertEqual(self.snapshot()["freshness"], "missing")

    def test_etc_directory_symlink_is_not_traversed(self):
        self.sessions.mkdir()
        (self.sessions / "2026").symlink_to("/etc", target_is_directory=True)
        scanned = []
        original_scandir = os.scandir
        def scan(path):
            scanned.append(Path(path))
            return original_scandir(path)
        with patch.object(collect.os, "scandir", side_effect=scan):
            self.assertEqual(self.snapshot()["freshness"], "missing")
        self.assertEqual(scanned, [self.sessions.absolute()])

    def test_fifo_never_opened(self):
        path = self.write()
        path.unlink()
        os.mkfifo(path)
        with patch.object(collect.os, "open", side_effect=AssertionError("unexpected open")):
            self.assertEqual(self.snapshot()["freshness"], "missing")

    def test_disk_full_preserves_previous_snapshot(self):
        output = self.root / "snapshot.json"
        output.write_text("old", encoding="utf-8")
        with patch.object(collect.os, "fsync", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError):
                collect.atomic_write(output, self.snapshot())
        self.assertEqual(output.read_text(), "old")
        self.assertEqual(list(self.root.glob(".snapshot.json.*")), [])

    def test_disk_full_main_reports_unknown_without_traceback(self):
        self.write(rows=[row()])
        output = self.root / "snapshot.json"
        output.write_text("previous-good", encoding="utf-8")
        with patch.object(sys, "argv", ["collect", "--sessions-dir", str(self.sessions),
                                        "--output", str(output)]), \
             patch.object(collect.os, "fsync", side_effect=OSError(errno.ENOSPC, "disk full")), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(collect.main(), 1)
        self.assertIn("ISMERETLEN", stdout.getvalue())
        self.assertEqual(output.read_text(), "previous-good")
        self.assertEqual(list(self.root.glob(".snapshot.json.*")), [])

    def test_source_failure_preserves_previous_snapshot_and_reports_unknown(self):
        output = self.root / "snapshot.json"
        output.write_text("previous-good", encoding="utf-8")
        with patch.object(sys, "argv", ["collect", "--sessions-dir", str(self.sessions),
                                        "--output", str(output)]), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(collect.main(), 1)
        self.assertIn("ISMERETLEN", stdout.getvalue())
        self.assertEqual(output.read_text(), "previous-good")

    def test_clock_jump_backwards_is_unknown(self):
        self.write(rows=[row()])
        self.assertIn("ISMERETLEN", check.format_snapshot(self.snapshot(), NOW - timedelta(hours=1)))

    def test_atomic_write_replaces_snapshot_and_cleans_temp(self):
        output = self.root / "snapshot.json"
        output.write_text("old", encoding="utf-8")
        snapshot = self.snapshot()
        original_replace = os.replace
        def verify_replace(source, target):
            self.assertEqual(Path(source).parent, output.parent)
            self.assertEqual(output.read_text(), "old")
            self.assertEqual(json.loads(Path(source).read_text()), snapshot)
            original_replace(source, target)
        with patch.object(collect.os, "replace", side_effect=verify_replace) as replaced:
            collect.atomic_write(output, snapshot)
        replaced.assert_called_once()
        self.assertEqual(json.loads(output.read_text()), snapshot)
        self.assertEqual(list(self.root.glob(".snapshot.json.*")), [])

    def test_failed_replace_keeps_previous_file_and_cleans_temp(self):
        output = self.root / "snapshot.json"
        output.write_text("old", encoding="utf-8")
        with patch.object(collect.os, "replace", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                collect.atomic_write(output, self.snapshot())
        self.assertEqual(output.read_text(), "old")
        self.assertEqual(list(self.root.glob(".snapshot.json.*")), [])

    def test_checker_thresholds_for_both_windows(self):
        for used, label in ((0, "OK"), (79.9, "OK"), (80, "OPATLAN"),
                            (89.9, "OPATLAN"), (90, "STOP"), (100, "STOP")):
            with self.subTest(used=used):
                self.write(rows=[row(used=used, weekly=used)])
                line = check.format_snapshot(self.snapshot(), NOW)
                self.assertEqual(line.count(label), 2)
                self.assertNotIn("\n", line)
                self.assertIn("reset:", line)

    def test_checker_recomputes_staleness_without_another_collection(self):
        self.write(rows=[row()])
        snapshot = self.snapshot()
        self.assertIn("nem friss", check.format_snapshot(snapshot, NOW + timedelta(minutes=31)))
        snapshot["freshness"] = "stale"
        self.assertIn("nem friss", check.format_snapshot(snapshot, NOW))

    def test_checker_invalid_snapshots(self):
        self.write(rows=[row()])
        invalid = copy.deepcopy(self.snapshot())
        invalid["windows"]["seven_day"]["used_percent"] = 101
        for snapshot in (None, [], {}, invalid, self.snapshot() | {"windows": []}):
            with self.subTest(snapshot=snapshot):
                self.assertIn("ISMERETLEN", check.format_snapshot(snapshot, NOW))

    def test_cli_end_to_end_and_missing_input(self):
        # Both subprocesses use tmp paths, never the default store or sessions.
        fresh_row = row()
        current = datetime.now(timezone.utc)
        fresh_row["timestamp"] = current.isoformat()
        for key, seconds in (("primary", 18000), ("secondary", 604800)):
            fresh_row["payload"]["rate_limits"][key]["resets_at"] = int(current.timestamp()) + seconds
        self.write(rows=[fresh_row])
        output = self.root / "output.json"
        result = subprocess.run([sys.executable, str(SCRIPTS / "codex-usage-collect.py"),
                                 "--sessions-dir", str(self.sessions), "--output", str(output)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, expected in ((output, "Codex 5 órás keret 12% OK"), (self.root / "absent", "ISMERETLEN")):
            result = subprocess.run([sys.executable, str(SCRIPTS / "codex-usage-check.py"),
                                     "--input", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertIn(expected, result.stdout)
        output.write_text("broken", encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPTS / "codex-usage-check.py"),
                                 "--input", str(output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("ISMERETLEN", result.stdout)


class FreshnessRegressionTests(unittest.TestCase):
    setUp = CodexUsageTests.setUp
    write = CodexUsageTests.write
    snapshot = CodexUsageTests.snapshot

    def test_old_creation_date_resumed_rollout_contains_fresh_quota(self):
        event = row()
        current = datetime(2026,11,9,14,20,tzinfo=timezone.utc)
        event["timestamp"] = current.isoformat()
        for key, seconds in (("primary",18000),("secondary",604800)):
            event["payload"]["rate_limits"][key]["resets_at"] = int(current.timestamp()) + seconds
        path = self.sessions / "2026/11/07/rollout-2026-11-07T05-32-06.jsonl"
        path.parent.mkdir(parents=True);path.write_text(json.dumps(event)+"\n")
        result = collect.collect(self.sessions,current)
        self.assertEqual(result["freshness"],"fresh")
        self.assertEqual(result["observed_at"],current.isoformat())
        self.assertEqual(result["observation_age_seconds"],0)

    def test_six_hour_gap_keeps_observation_timestamp_and_reports_age(self):
        self.write(rows=[row()])
        snapshot = collect.collect(self.sessions,NOW+timedelta(hours=6))
        self.assertEqual(snapshot["freshness"],"stale")
        self.assertEqual(snapshot["observed_at"],NOW.isoformat())
        self.assertEqual(snapshot["observation_age_seconds"],21600)
        line = check.format_snapshot(snapshot,NOW+timedelta(hours=6))
        self.assertIn("mérés kora: 6 ó 0 p",line)
        self.assertIn("Codex 5 órás keret: aktuális ISMERETLEN; előző mérés 12% (reset előtt)",line)
        self.assertIn("Codex heti keret: legalább 35% (nem friss)",line)
        self.assertNotIn("ISMERETLEN (stale)",line)

    def test_idle_31_minutes_shows_both_values_without_claiming_freshness(self):
        self.write(rows=[row()])
        now = NOW+timedelta(minutes=31)
        snapshot = collect.collect(self.sessions,now)
        line = check.format_snapshot(snapshot,now)
        self.assertEqual(snapshot["freshness"],"stale")
        self.assertEqual(line.count("nem friss"),2)
        self.assertNotIn("OK",line)
        self.assertIn("31 p 0 mp",line)
        self.assertNotIn("ISMERETLEN",line)

    def test_stale_status_80_90_thresholds_are_unchanged(self):
        for used, status in ((0,"OK"),(10,"OK"),(79.999,"OK"),(80,"OPATLAN"),(85,"OPATLAN"),(89.999,"OPATLAN"),(90,"STOP"),(92,"STOP"),(95,"STOP"),(100,"STOP")):
            self.write(rows=[row(used=used,weekly=used)])
            now = NOW+timedelta(minutes=31)
            line = check.format_snapshot(collect.collect(self.sessions,now),now)
            self.assertEqual(check.status(used),status)
            self.assertNotIn("OK",line)
            expected = f"{status}-szint, " if used >= 80 else ""
            self.assertEqual(line.count(f"legalább {used:g}% ({expected}nem friss)"),2)
            self.assertNotIn("STOP-szint" if used < 90 else "OPATLAN-szint",line)
            self.assertEqual(line.count("legalább"),2)
            self.assertEqual(line.count("nem friss"),2)

    def test_generated_time_and_cached_age_cannot_make_old_measurement_fresh(self):
        self.write(rows=[row()]);snapshot = self.snapshot()
        snapshot["generated_at"] = (NOW+timedelta(hours=4)).isoformat()
        snapshot["observation_age_seconds"] = 0
        line = check.format_snapshot(snapshot,NOW+timedelta(hours=4))
        self.assertIn("nem friss",line)
        self.assertIn("4 ó 0 p",line)

    def test_reset_boundary_for_each_window_does_not_infer_zero(self):
        for name in ("five_hour","seven_day"):
            for delta in (-1,0,1):
                self.write(rows=[row(used=90,weekly=90)])
                snapshot = self.snapshot()
                snapshot["windows"][name]["resets_at"] = NOW.timestamp()+delta
                line = check.format_snapshot(snapshot,NOW)
                self.assertEqual(line.count("aktuális ISMERETLEN"),int(delta<=0))
                self.assertIn("90% STOP",line);self.assertNotIn(" 0%",line)

    def test_stale_snapshot_tag_stays_historical_even_with_zero_age(self):
        self.write(rows=[row()]);snapshot = self.snapshot();snapshot["freshness"] = "stale"
        self.assertEqual(check.format_snapshot(snapshot,NOW).count("nem friss"),2)

    def test_future_observation_is_unknown_not_negative_age_or_current_percent(self):
        self.write(rows=[row(age=-1)])
        snapshot = self.snapshot();line = check.format_snapshot(snapshot,NOW)
        self.assertIsNone(snapshot["observation_age_seconds"])
        self.assertIn("ISMERETLEN",line);self.assertNotIn("12%",line)

    def test_missing_and_malformed_snapshots_never_show_known_percent(self):
        self.write(rows=[row()])
        original = self.snapshot()
        for value in (None,[],{},"FAKE_SECRET",float("nan")):
            snapshot = copy.deepcopy(original);snapshot["windows"] = value
            self.assertIn("ISMERETLEN",check.format_snapshot(snapshot,NOW))
        for tag in (None,"missing","FAKE_SECRET"):
            snapshot = copy.deepcopy(original);snapshot["freshness"] = tag
            line = check.format_snapshot(snapshot,NOW)
            self.assertIn("ISMERETLEN",line);self.assertNotIn("FAKE_SECRET",line)

    def test_stale_collection_cli_is_success_and_quiet_but_json_records_age(self):
        self.write(rows=[row()]);snapshot = collect.collect(self.sessions,NOW+timedelta(hours=6))
        output = self.root/"output.json"
        with patch.object(sys,"argv",["collect","--sessions-dir",str(self.sessions),"--output",str(output)]), \
             patch.object(collect,"collect",return_value=snapshot),patch("sys.stdout",new_callable=io.StringIO) as stdout:
            self.assertEqual(collect.main(),0)
        self.assertEqual(stdout.getvalue(),"")
        saved = json.loads(output.read_text());self.assertEqual(saved["freshness"],"stale")
        self.assertEqual(saved["observed_at"],NOW.isoformat())

    def test_missing_source_without_snapshot_returns_failure(self):
        output = self.root/"missing.json"
        with patch.object(sys,"argv",["collect","--sessions-dir",str(self.sessions),"--output",str(output)]),patch("sys.stdout",new_callable=io.StringIO):
            self.assertEqual(collect.main(),1)
        self.assertEqual(json.loads(output.read_text())["freshness"],"missing")

    def test_codex_home_precedes_home_but_sessions_option_remains_explicit(self):
        with patch.dict(os.environ,{"CODEX_HOME":str(self.root/"bridge-home"),"HOME":"/not-the-codex-account"}):
            self.assertEqual(collect.default_sessions_dir(),self.root/"bridge-home/sessions")
            self.write(rows=[row()]);output = self.root/"explicit.json"
            with patch.object(sys,"argv",["collect","--sessions-dir",str(self.sessions),"--output",str(output)]),patch("sys.stdout",new_callable=io.StringIO):
                self.assertEqual(collect.main(),0)
            self.assertEqual(json.loads(output.read_text())["windows"]["five_hour"]["used_percent"],12)

    def test_unix_account_home_fallback_ignores_cron_home(self):
        with patch.dict(os.environ,{"CODEX_HOME":"","HOME":"/not-real-home"}), \
             patch.object(collect.pwd,"getpwuid") as get:
            get.return_value.pw_dir = str(self.root)
            self.assertEqual(collect.default_sessions_dir(),self.root/".codex/sessions")

    def test_one_thousand_age_reset_status_cases(self):
        rng = random.Random(5196)
        for index in range(1000):
            used = rng.choice((0,79.99,80,89.99,90,100))
            age = rng.choice((0,1799,1800,1801,21600,604801))
            self.write(rows=[row(used=used,weekly=used)])
            now = NOW+timedelta(seconds=age)
            snapshot = collect.collect(self.sessions,now)
            line = check.format_snapshot(snapshot,now)
            self.assertEqual(snapshot["freshness"],"fresh" if age<=1800 else "stale")
            expired = int(age>=18000)+int(age>=604800)
            expected_labels = (2-expired if used>=80 else 0) if age>1800 else 2
            self.assertEqual(line.count(check.status(used)),expected_labels,index)
            if age>1800:
                self.assertEqual(line.count("nem friss"),2-expired,index)
                self.assertEqual(line.count("előző mérés"),expired,index)
                self.assertNotIn("OK",line)
                expected_level = "STOP" if used>=90 else "OPATLAN" if used>=80 else None
                for level in ("STOP","OPATLAN"):
                    self.assertEqual(line.count(f"{level}-szint"),2-expired if level==expected_level else 0,index)
            self.assertEqual(line.count("aktuális ISMERETLEN"),expired,index)

    def test_stale_windows_report_their_own_severity_and_reset_independently(self):
        for used, weekly in ((95,92),(95,85),(85,10),(10,95)):
            for expired in (None,"five_hour","seven_day"):
                with self.subTest(used=used,weekly=weekly,expired=expired):
                    self.write(rows=[row(used=used,weekly=weekly)])
                    now = NOW+timedelta(minutes=31)
                    snapshot = collect.collect(self.sessions,now)
                    if expired:snapshot["windows"][expired]["resets_at"] = now.timestamp()-1
                    line = check.format_snapshot(snapshot,now)
                    self.assertNotIn("OK",line)
                    for part,name,value in zip(line.split(" | "),("five_hour","seven_day"),(used,weekly)):
                        if name==expired:
                            self.assertIn(f"aktuális ISMERETLEN; előző mérés {value}% (reset előtt)",part)
                            self.assertNotIn("-szint",part)
                        else:
                            prefix = "STOP-szint, " if value>=90 else "OPATLAN-szint, " if value>=80 else ""
                            self.assertIn(f"legalább {value}% ({prefix}nem friss)",part)

    def test_past_reset_never_calls_old_usage_a_lower_bound_or_work_permission(self):
        for used in (0,10,79.9,80,85,90,92,95,100):
            self.write(rows=[row(used=used,weekly=used)])
            snapshot = self.snapshot()
            for window in snapshot["windows"].values():window["resets_at"] = NOW.timestamp()-1
            line = check.format_snapshot(snapshot,NOW)
            self.assertEqual(line.count("előző mérés"),2)
            self.assertEqual(line.count("aktuális ISMERETLEN"),2)
            self.assertNotIn("legalább",line)
            for label in ("OK","OPATLAN","STOP"):self.assertNotIn(label,line)

    def test_24_hours_of_ten_minute_idle_polls_are_silent(self):
        self.write(rows=[row()]);output = self.root/"idle.json"
        snapshot = collect.collect(self.sessions,NOW+timedelta(hours=6))
        with patch.object(sys,"argv",["collect","--sessions-dir",str(self.sessions),"--output",str(output)]), \
             patch.object(collect,"collect",return_value=snapshot),patch("sys.stdout",new_callable=io.StringIO) as stdout:
            for index in range(144):
                snapshot["observation_age_seconds"] = 21600+index*600
                snapshot["generated_at"] = (NOW+timedelta(seconds=21600+index*600)).isoformat()
                self.assertEqual(collect.main(),0)
        self.assertEqual(stdout.getvalue(),"")
        saved = json.loads(output.read_text())
        self.assertEqual(saved["freshness"],"stale")
        self.assertEqual(saved["observation_age_seconds"],107400)
        self.assertEqual(saved["observed_at"],NOW.isoformat())

    def test_missing_source_error_stays_visible_on_first_and_repeat_poll(self):
        output = self.root/"missing.json"
        with patch.object(sys,"argv",["collect","--sessions-dir",str(self.sessions),"--output",str(output)]),patch("sys.stdout",new_callable=io.StringIO) as stdout:
            self.assertEqual(collect.main(),1)
            first = output.read_bytes()
            self.assertEqual(collect.main(),1)
        self.assertEqual(output.read_bytes(),first)
        self.assertEqual(stdout.getvalue().count("ISMERETLEN (missing)"),2)




if __name__ == "__main__":
    unittest.main()

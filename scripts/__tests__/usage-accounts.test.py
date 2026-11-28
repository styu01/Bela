#!/usr/bin/env python3
"""The ONE account-rule table (scripts/usage_accounts.py, BELA 6587 / KISPROGI 6585) and its four readers:
usage-collect, usage-check, usage-fleet-monitor, usage-check-info. Table agreement, the exact boundaries
(info 89/90/95/96; styu01 5-hour 89/90/97/98 and weekly 79/80/89/90), release, an account switch during a
pause, and snapshots without a marker or with a wrong / unknown account. Nothing here calls a real API."""
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent


def load(name, mod):
    spec = importlib.util.spec_from_file_location(mod, SCRIPTS / name)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


UA = load("usage_accounts.py", "usage_accounts_t")
UC = load("usage-collect.py", "usage_collect_t")
UM = load("usage-fleet-monitor.py", "usage_fleet_monitor_t")
CHECK = SCRIPTS / "usage-check.py"
INFO = SCRIPTS / "usage-check-info.py"
CHECK_MOD = load("usage-check.py", "usage_check_t")
NOW = datetime(2026, 11, 28, 15, tzinfo=timezone.utc)


class TableTests(unittest.TestCase):
    def test_the_owner_decision(self):
        self.assertEqual(UA.ACCOUNT_RULES["styu01"], {"five_hour": {"warn": 90, "stop": 98}, "seven_day": {"warn": 80, "stop": 90}})
        self.assertEqual(UA.ACCOUNT_RULES["info"], {"seven_day": {"warn": 90, "stop": 96}})
        self.assertEqual(UA.ACCOUNT_RULES["ismeretlen"], UA.ACCOUNT_RULES["styu01"])
        self.assertEqual(UA.account_or_unknown("other"), "ismeretlen")
        self.assertEqual(UA.account_or_unknown(None), "ismeretlen")

    def test_every_reader_uses_the_same_table(self):
        for a in UA.ACCOUNTS:
            self.assertEqual(UM.THRESHOLDS[a]["warn"], UA.warn_values(a), a)
            self.assertEqual(UM.THRESHOLDS[a]["pause"], UA.stop_values(a), a)
            self.assertEqual(set(CHECK_MOD.ACCOUNT_THRESHOLDS[a]), set(UA.required_windows(a)), a)
            for w, (warn, stop) in CHECK_MOD.ACCOUNT_THRESHOLDS[a].items():
                self.assertEqual((warn, stop), (UA.warn_values(a)[w], UA.stop_values(a)[w]), (a, w))
        self.assertEqual(UC.ACCOUNTS_WITHOUT_5H, ("info",))
        self.assertEqual(set(UC.ACCOUNTS), set(UA.ACCOUNTS))
        self.assertEqual(UM.DEFAULT_ACCOUNT, "ismeretlen")
        self.assertEqual(CHECK_MOD.DEFAULT_ACCOUNT, "ismeretlen")
        info_src = INFO.read_text(encoding="utf-8")
        self.assertIn("usage_accounts.py", info_src)
        self.assertNotRegex(info_src, r">=\s*9\d")  # no private threshold copy left in the diagnostic

    def test_collector_accepts_a_missing_5h_window_only_on_info(self):
        headers = {"anthropic-ratelimit-unified-7d-utilization": "0.5", "anthropic-ratelimit-unified-7d-reset": "1800000000",
                   "anthropic-ratelimit-unified-representative-claim": "seven_day"}
        self.assertEqual(set(UC._parse_claude_usage_headers(headers, "info")[0]), {"seven_day"})
        for a in ("styu01", "ismeretlen", None):
            self.assertIsNone(UC._parse_claude_usage_headers(headers, a)[0], a)


class CheckBoundaryTests(unittest.TestCase):
    def run_check(self, account, five=None, seven=None, marker=False):
        now = datetime.now(timezone.utc)
        windows = {}
        if five is not None:
            windows["five_hour"] = {"used_percent": five, "resets_at": (now + timedelta(hours=1)).timestamp()}
        if seven is not None:
            windows["seven_day"] = {"used_percent": seven, "resets_at": (now + timedelta(days=1)).timestamp()}
        claude = {"account": account, "windows": windows, "authoritative_collected_at_unix": now.timestamp()}
        if marker:
            claude["windows_not_reported"] = ["five_hour"]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"generated_at": now.isoformat(), "claude": claude}, f)
        try:
            r = subprocess.run([sys.executable, str(CHECK)], env=dict(os.environ, USAGE_CHECK_LATEST_PATH=f.name),
                               capture_output=True, text=True, check=False)
        finally:
            os.unlink(f.name)
        return r.returncode, r.stdout.strip()

    def assert_status(self, result, code, status, account):
        self.assertEqual(result[0], code, result[1])
        self.assertTrue(result[1].startswith(f"USAGE {account} Claude"), result[1])
        self.assertTrue(result[1].endswith(status), result[1])

    def test_info_weekly_89_90_95_96(self):
        for seven, code, status in ((89, 0, "OK"), (90, 0, "FIGYELMEZTETÉS"), (95, 0, "FIGYELMEZTETÉS"), (96, 2, "STOP")):
            with self.subTest(seven=seven):
                self.assert_status(self.run_check("info", seven=seven, marker=True), code, status, "info")
                self.assert_status(self.run_check("info", seven=seven), code, status, "info")  # no marker needed
                self.assert_status(self.run_check("info", five=99, seven=seven), code, status, "info")  # 5h ignored

    def test_styu01_and_unknown_5h_89_90_97_98_weekly_79_80_89_90(self):
        for account in ("styu01", "ismeretlen"):
            for five, code, status in ((89, 0, "OK"), (90, 0, "FIGYELMEZTETÉS"), (97, 0, "FIGYELMEZTETÉS"), (98, 2, "STOP")):
                with self.subTest(account=account, five=five):
                    self.assert_status(self.run_check(account, five=five, seven=10), code, status, account)
            for seven, code, status in ((79, 0, "OK"), (80, 0, "FIGYELMEZTETÉS"), (89, 0, "FIGYELMEZTETÉS"), (90, 2, "STOP")):
                with self.subTest(account=account, seven=seven):
                    self.assert_status(self.run_check(account, five=10, seven=seven), code, status, account)

    def test_missing_5h_window_or_wrong_account(self):
        for account in ("styu01", "ismeretlen"):
            for marker in (True, False):
                code, out = self.run_check(account, seven=10, marker=marker)
                self.assertEqual(code, 3, out)
                self.assertIn(f"USAGE {account} Claude 5 órás/heti keret ISMERETLEN", out)
        code, out = self.run_check("bogus", five=10, seven=85)
        self.assertEqual((code, out.split()[1]), (0, "ismeretlen"))
        self.assertTrue(out.endswith("FIGYELMEZTETÉS"))  # the fleet rule, not info's 90


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)
        self.sent = []
        outer = self

        class T:
            def send_agent(self, recipient, content):
                outer.sent.append((recipient, content))
                return "sent"

            def send_telegram(self, content):
                return self.send_agent("telegram", content)
        self.transport = T()

    def snap(self, account, five=None, seven=None, marker=False):
        windows = {}
        if five is not None:
            windows["five_hour"] = {"used_percent": five, "resets_at": NOW.timestamp() + 3600}
        if seven is not None:
            windows["seven_day"] = {"used_percent": seven, "resets_at": NOW.timestamp() + 86400}
        claude = {"source": "authoritative", "account": account, "windows": windows}
        if marker:
            claude["windows_not_reported"] = ["five_hour"]
        (self.store / "usage-latest.json").write_text(json.dumps({"generated_at": NOW.isoformat(), "claude": claude}))
        return UM.Monitor(self.store, NOW, False, self.transport).run()

    def test_info_boundaries_and_release_below_90(self):
        for seven, expected in ((89, "OK level=0 silent"), (90, "SENT warning level=80"), (95, "UNCHANGED level=80"),
                                (96, "SENT pause level=90"), (90, "UNCHANGED level=90"), (89, "SENT resume level=0")):
            self.assertEqual(self.snap("info", seven=seven), expected, seven)

    def test_styu01_boundaries_and_release_needs_5h_below_90_and_weekly_below_80(self):
        self.assertEqual(self.snap("styu01", five=89, seven=79), "OK level=0 silent")
        self.assertEqual(self.snap("styu01", five=97, seven=79), "SENT warning level=80")
        self.assertEqual(self.snap("styu01", five=98, seven=79), "SENT pause level=90")
        self.assertEqual(self.snap("styu01", five=89, seven=80), "UNCHANGED level=90")
        self.assertEqual(self.snap("styu01", five=90, seven=79), "UNCHANGED level=90")
        self.assertEqual(self.snap("styu01", five=89, seven=79), "SENT resume level=0")
        self.assertEqual(self.snap("styu01", five=10, seven=90), "SENT pause level=90")  # weekly 90 pauses styu01
        self.assertTrue(all("(styu01 fiók)" in c for _, c in self.sent))

    def test_account_switch_during_a_pause(self):
        self.assertEqual(self.snap("styu01", five=98, seven=50), "SENT pause level=90")
        self.assertEqual(self.snap("info", seven=92), "UNCHANGED level=90")   # info: 92 is not below its 90
        self.assertEqual(self.snap("info", seven=89), "SENT resume level=0")
        self.assertEqual(self.snap("info", seven=96), "SENT pause level=90")
        self.assertEqual(self.snap("styu01", five=10, seven=85), "UNCHANGED level=90")  # styu01: weekly 85 not below 80
        self.assertEqual(self.snap("styu01", five=10, seven=79), "SENT resume level=0")

    def test_marker_and_account_mismatches_are_stale(self):
        for account in ("styu01", "ismeretlen", "bogus", None):
            self.assertEqual(self.snap(account, seven=50, marker=True), "STALE silent", account)
            self.assertEqual(self.snap(account, seven=50), "STALE silent", account)
        self.assertEqual(self.snap("info", seven=50), "OK level=0 silent")
        self.assertEqual(self.sent, [])


class InfoDiagnosticTests(unittest.TestCase):
    """usage-check-info.py with a stand-in API (USAGE_CHECK_INFO_URL): the shared table decides."""

    def run_info(self, headers):
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with tempfile.NamedTemporaryFile("w", delete=False) as tok:
            tok.write("fake-token\n")
        try:
            env = dict(os.environ, USAGE_CHECK_INFO_TOKEN_PATH=tok.name, USAGE_CHECK_INFO_URL=f"http://127.0.0.1:{srv.server_port}/")
            r = subprocess.run([sys.executable, str(INFO)], env=env, capture_output=True, text=True, timeout=30)
        finally:
            srv.shutdown()
            os.unlink(tok.name)
        return r.returncode, r.stdout.strip()

    def test_info_boundaries_and_5h_ignored(self):
        for seven, code, status in ((0.89, 0, "OK"), (0.90, 0, "FIGYELMEZTETÉS"), (0.95, 0, "FIGYELMEZTETÉS"), (0.96, 2, "STOP")):
            with self.subTest(seven=seven):
                code_, out = self.run_info({"anthropic-ratelimit-unified-7d-utilization": str(seven),
                                            "anthropic-ratelimit-unified-7d-reset": "1800000000",
                                            "anthropic-ratelimit-unified-5h-utilization": "0.99",
                                            "anthropic-ratelimit-unified-5h-reset": "1800000000"})
                self.assertEqual(code_, code, out)
                self.assertTrue(out.endswith(status), out)
                self.assertIn("5 órás keret=n/a", out)
        code, out = self.run_info({"anthropic-ratelimit-unified-5h-utilization": "0.1"})
        self.assertEqual(code, 3, out)
        self.assertNotIn("fake-token", out)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Only fake agy processes and synthetic snapshots; never call the real provider."""
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gemini_tested", SCRIPTS / "gemini-usage-collect.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
spec = importlib.util.spec_from_file_location("gemini_checker_tested", SCRIPTS / "gemini-usage-check.py")
k = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k)
NOW = datetime(2026, 11, 6, 6, tzinfo=timezone.utc)
GOOD = ("Gemini Models\tWeekly Limit Remaining\t83%\t2026-11-10T10:00:49Z\n"
        "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-11-13T09:45:34Z\n")


class GeminiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = self.base / "store"
        self.fake = self.base / "fake-agy"
        provider_home = patch.object(c, "PROVIDER_HOME", str(self.base / "provider-home"))
        provider_home.start()
        self.addCleanup(provider_home.stop)
        self.make_fake("import sys\nsys.stdout.write(" + repr(GOOD) + ")\n")

    def make_fake(self, body):
        self.fake.write_text("#!/usr/bin/python3\n" + body)
        self.fake.chmod(0o700)

    def snapshot(self, text=GOOD, now=NOW):
        return {"collectedAt": c.utc(now), "groups": c.parse_usage(text)}

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            rc = c.main(["--state-dir", str(self.store), "--agy", str(self.fake), "--now", c.utc(NOW), *args])
        return rc, out.getvalue(), err.getvalue()

    def check(self, path=None, now=NOW):
        out = io.StringIO()
        with patch("sys.stdout", out):
            rc = k.main(["--input", str(path or self.store / c.SNAPSHOT), "--now", c.utc(now)])
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_valid_tsv_both_groups_percentages_and_resets(self):
        groups = c.parse_usage(GOOD)
        self.assertEqual(groups["gemini"], {"remaining_pct": 83, "used_pct": 17, "resets_at": "2026-11-10T10:00:49Z"})
        self.assertEqual(groups["claude_gpt"]["used_pct"], 0)

    def test_group_order_irrelevant_and_crlf_supported(self):
        reversed_rows = "\r\n".join(reversed(GOOD.splitlines())) + "\r\n"
        self.assertEqual(c.parse_usage(reversed_rows), c.parse_usage(GOOD))

    def test_missing_duplicate_extra_and_empty_rows_rejected(self):
        first, second = GOOD.splitlines()
        for text in ("", first, first + "\n" + first, GOOD + first, GOOD + "\n", "\n" + GOOD):
            with self.subTest(text=text), self.assertRaisesRegex(c.UsageError, "invalid_output"):
                c.parse_usage(text)

    def test_unknown_group_and_wrong_limit_kind_rejected(self):
        for text in (GOOD.replace("Gemini Models", "Unknown Models"), GOOD.replace("Weekly Limit Remaining", "Monthly Limit", 1)):
            with self.assertRaises(c.UsageError):
                c.parse_usage(text)

    def test_tab_count_is_strict(self):
        for text in (GOOD.replace("\t", "\t\t", 1), GOOD.replace("\t", " ", 1), GOOD.replace("83%", "83%\textra")):
            with self.assertRaises(c.UsageError):
                c.parse_usage(text)

    def test_percent_rejects_bad_number_range_and_controls(self):
        for value in ("x%", "101%", "-1%", "NaN%", "inf%", "83", " 83%", "83 %", "100.1%", "83%\x1b[0m"):
            with self.subTest(value=value), self.assertRaises(c.UsageError):
                c.parse_usage(GOOD.replace("83%", value))

    def test_percent_zero_hundred_and_fractional(self):
        for value, used in (("0%", 100), ("100%", 0), ("20.01%", 79.99), ("10.0%", 90)):
            self.assertEqual(c.parse_usage(GOOD.replace("83%", value))["gemini"]["used_pct"], used)

    def test_invalid_iso_time_rejected(self):
        for value in ("bad", "2026-11-10", "2026-11-10T10:00:49", "2026-02-30T10:00:49Z", "2026-11-10T25:00:49Z",
                      "2026-11-10T10:00:49+00:99", "2026-11-10T10:00:49+24:00"):
            with self.subTest(value=value), self.assertRaises(c.UsageError):
                c.parse_usage(GOOD.replace("2026-11-10T10:00:49Z", value))

    def test_iso_timezone_normalized_and_fraction_supported(self):
        text = GOOD.replace("2026-11-10T10:00:49Z", "2026-11-10T11:00:49.123456789+01:00")
        self.assertEqual(c.parse_usage(text)["gemini"]["resets_at"], "2026-11-10T10:00:49.123456Z")

    def test_fake_receives_only_usage_and_required_child_home(self):
        record = self.base / "command.json"
        self.make_fake("import os,sys,json\nfrom pathlib import Path\nPath(" + repr(str(record)) + ").write_text(json.dumps({'argv':sys.argv[1:],'home':os.environ['HOME']}))\nsys.stdout.write(" + repr(GOOD) + ")\n")
        parent_home = os.environ.get("HOME")
        result = c.collect(self.store, self.fake, NOW)
        observed = json.loads(record.read_text())
        self.assertEqual(observed["argv"], ["--print", "/usage", "--print-timeout", "30s"])
        self.assertEqual(observed["home"], c.PROVIDER_HOME)
        self.assertEqual(os.environ.get("HOME"), parent_home)
        self.assertEqual(result, json.loads((self.store / c.SNAPSHOT).read_text()))
        self.assertEqual(result["collectedAt"], "2026-11-06T06:00:00Z")

    def test_atomic_write_replaces_complete_json_with_private_permissions(self):
        self.store.mkdir()
        output = self.store / c.SNAPSHOT
        output.write_text("old")
        original = c.os.replace
        def checked(source, target):
            self.assertEqual(json.loads(Path(source).read_text()), self.snapshot())
            self.assertEqual(output.read_text(), "old")
            return original(source, target)
        with patch.object(c.os, "replace", side_effect=checked):
            c.atomic_write(output, self.snapshot())
        self.assertEqual(json.loads(output.read_text()), self.snapshot())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(list(self.store.glob("*.tmp")), [])

    def test_atomic_write_failure_preserves_previous_snapshot(self):
        self.store.mkdir()
        output = self.store / c.SNAPSHOT
        output.write_text("old good bytes")
        with patch.object(c.os, "replace", side_effect=OSError("SECRET disk")):
            with self.assertRaisesRegex(c.UsageError, "snapshot_write_failed"):
                c.atomic_write(output, self.snapshot())
        self.assertEqual(output.read_text(), "old good bytes")
        self.assertEqual(list(self.store.glob("*.tmp")), [])

    def test_invalid_empty_and_nonzero_provider_keep_previous_good_data(self):
        for body in ("print('SECRET invalid')", "pass", "import sys\nprint('SECRET stdout')\nprint('SECRET stderr', file=sys.stderr)\nsys.exit(3)"):
            self.store.mkdir(exist_ok=True)
            (self.store / c.SNAPSHOT).write_text("old good bytes")
            self.make_fake(body)
            rc, out, err = self.main()
            self.assertEqual(rc, 1)
            self.assertEqual((self.store / c.SNAPSHOT).read_text(), "old good bytes")
            self.assertNotIn("SECRET", out + err + (self.store / c.LOG).read_text())

    def test_timeout_kills_fake_and_preserves_data(self):
        self.store.mkdir()
        (self.store / c.SNAPSHOT).write_text("old good bytes")
        self.make_fake("import time\ntime.sleep(5)\n")
        with self.assertRaisesRegex(c.UsageError, "provider_timeout"):
            c.collect(self.store, self.fake, NOW, timeout=0.05)
        self.assertEqual((self.store / c.SNAPSHOT).read_text(), "old good bytes")
        with patch.object(c, "run_usage", side_effect=c.UsageError("provider_timeout")):
            rc, out, err = self.main()
        self.assertEqual(rc, 1)
        self.assertIn("provider_timeout", (self.store / c.LOG).read_text())

    def test_missing_executable_fixed_error_and_log(self):
        self.fake.unlink()
        rc, out, err = self.main()
        self.assertEqual(rc, 1)
        self.assertIn("provider_unavailable", out)
        self.assertIn("provider_unavailable", (self.store / c.LOG).read_text())
        self.assertNotIn(str(self.fake), out + err)

    def test_lock_blocks_second_collector_without_provider_call(self):
        import fcntl
        self.store.mkdir()
        with (self.store / c.LOCK).open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(c, "run_usage", side_effect=AssertionError("must not invoke provider")):
                rc, out, err = self.main()
        self.assertEqual(rc, 75)
        self.assertIn("busy", out)
        self.assertFalse((self.store / c.SNAPSHOT).exists())

    def test_checker_ok_warning_stop_thresholds(self):
        for remaining, expected in (("20.01%", "OK"), ("20%", "FIGYELMEZTETÉS"), ("10%", "STOP"), ("0%", "STOP")):
            line = k.format_snapshot(self.snapshot(GOOD.replace("83%", remaining)), NOW)
            self.assertIn("% " + expected + ",", line)
            self.assertNotIn("\n", line)

    def test_other_group_never_controls_gemini_status(self):
        line = k.format_snapshot(self.snapshot(GOOD.replace("100%", "0%")), NOW)
        self.assertIn("17% OK", line)
        self.assertIn("Gemini szolgáltatói Claude/GPT heti keret 100% használt", line)
        self.assertNotIn("STOP", line)

    def test_stale_boundary_future_and_missing(self):
        snapshot = self.snapshot()
        self.assertIn("17% OK", k.format_snapshot(snapshot, NOW + timedelta(seconds=5400)))
        for now in (NOW + timedelta(seconds=5400.001), NOW - timedelta(seconds=1)):
            self.assertEqual(k.format_snapshot(snapshot, now), "Gemini heti keret: ISMERETLEN (stale)")
        self.assertEqual(k.format_snapshot(None, NOW), "Gemini heti keret: ISMERETLEN (missing)")

    def test_corrupt_snapshots_and_percent_consistency_are_unknown(self):
        for data in ({}, [], {"collectedAt": c.utc(NOW), "groups": {}}):
            self.assertIn("ISMERETLEN", k.format_snapshot(data, NOW))
        for value in (True, float("inf"), -1, 101, "17"):
            snapshot = self.snapshot()
            snapshot["groups"]["gemini"]["used_pct"] = value
            self.assertIn("ISMERETLEN", k.format_snapshot(snapshot, NOW))
        snapshot = self.snapshot()
        snapshot["groups"]["gemini"]["used_pct"] = 20
        self.assertIn("ISMERETLEN", k.format_snapshot(snapshot, NOW))

    def test_checker_cli_missing_corrupt_and_valid_file(self):
        self.assertIn("ISMERETLEN", self.check())
        self.store.mkdir()
        (self.store / c.SNAPSHOT).write_text("bad json")
        self.assertIn("ISMERETLEN", self.check())
        (self.store / c.SNAPSHOT).write_text(json.dumps(self.snapshot()))
        self.assertIn("17% OK", self.check())

    def test_output_log_and_checker_have_no_dashes_or_provider_secrets(self):
        rc, out, err = self.main()
        self.assertEqual(rc, 0)
        line = self.check()
        for text in (out, err, line):
            for forbidden in ("\u2013", "\u2014", " -- "):
                self.assertNotIn(forbidden, text)

    def test_oversize_stdout_stderr_and_invalid_encoding_rejected(self):
        for body, code in (("import sys\nsys.stdout.write('x'*10000)", "provider_output_too_large"),
                           ("import sys\nsys.stderr.write('SECRET'*2000)", "provider_output_too_large"),
                           ("import os\nos.write(1,b'\\xff')", "invalid_output")):
            self.make_fake(body)
            rc, out, err = self.main()
            self.assertEqual(rc, 1)
            self.assertIn(code, out)
            self.assertNotIn("SECRET", out + err + (self.store / c.LOG).read_text())

    def test_symlink_store_log_lock_and_snapshot_do_not_escape(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.base / "store-link"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(c.UsageError, "store_failed"):
            c.collect(link, self.fake, NOW)
        self.store.mkdir()
        (self.store / c.SNAPSHOT).symlink_to(outside / "data")
        self.assertIn("ISMERETLEN", self.check())
        (self.store / c.LOCK).symlink_to(outside / "lock")
        rc, out, err = self.main()
        self.assertNotEqual(rc, 0)
        (self.store / c.LOCK).unlink()
        (self.store / c.LOG).unlink()
        (self.store / c.LOG).symlink_to(outside / "log")
        self.make_fake("print('bad')")
        rc, out, err = self.main()
        self.assertIn("log_write_failed", err)
        self.assertEqual(list(outside.iterdir()), [])

    def test_creating_nested_store_cannot_follow_symlink_parent(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.base / "linked"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(c.UsageError, "store_failed"):
            c.collect(link / "new-store", self.fake, NOW)
        self.assertEqual(list(outside.iterdir()), [])

    def test_partial_stdout_before_timeout_never_replaces_snapshot(self):
        self.store.mkdir()
        (self.store / c.SNAPSHOT).write_text("old good bytes")
        self.make_fake("import sys,time\nsys.stdout.write(" + repr(GOOD) + ")\nsys.stdout.flush()\ntime.sleep(5)\n")
        with self.assertRaisesRegex(c.UsageError, "provider_timeout"):
            c.collect(self.store, self.fake, NOW, timeout=0.05)
        self.assertEqual((self.store / c.SNAPSHOT).read_text(), "old good bytes")

    def test_stdout_closed_but_child_running_still_times_out(self):
        self.make_fake("import os,time\nos.close(1)\nos.close(2)\ntime.sleep(5)\n")
        with self.assertRaisesRegex(c.UsageError, "provider_timeout"):
            c.run_usage(self.fake, timeout=0.05)

    def test_second_group_invalid_also_preserves_complete_snapshot(self):
        self.store.mkdir()
        (self.store / c.SNAPSHOT).write_text("old good bytes")
        self.make_fake("print(" + repr(GOOD.replace("100%", "101%")) + ", end='')")
        self.assertEqual(self.main()[0], 1)
        self.assertEqual((self.store / c.SNAPSHOT).read_text(), "old good bytes")

    def test_parse_does_not_execute_injected_group_or_percentage(self):
        for text in (GOOD.replace("Gemini Models", "$(touch SECRET)"), GOOD.replace("83%", "83%; rm -rf SECRET")):
            self.make_fake("import sys\nsys.stdout.write(" + repr(text) + ")")
            rc, out, err = self.main()
            self.assertEqual(rc, 1)
            self.assertNotIn("SECRET", out + err + (self.store / c.LOG).read_text())

    def test_invalid_now_is_fixed_error_before_provider(self):
        with patch.object(c, "run_usage", side_effect=AssertionError("provider forbidden")):
            rc, out, err = self.main("--now", "SECRET invalid")
        self.assertEqual(rc, 1)
        self.assertIn("invalid_now", out)
        self.assertNotIn("SECRET", out + err)

    def test_snapshot_serialization_failure_preserves_previous_bytes(self):
        self.store.mkdir()
        output = self.store / c.SNAPSHOT
        output.write_text("old")
        with self.assertRaises(ValueError):
            c.atomic_write(output, {"used_pct": float("nan")})
        self.assertEqual(output.read_text(), "old")
        self.assertEqual(list(self.store.glob("*.tmp")), [])

    def test_past_reset_is_valid_iso_but_not_a_freshness_override(self):
        text = GOOD.replace("2026-11-10T10:00:49Z", "2020-01-01T00:00:00Z")
        self.assertEqual(c.parse_usage(text)["gemini"]["remaining_pct"], 83)
        self.assertEqual(k.format_snapshot(self.snapshot(text), NOW), "Gemini heti keret: ISMERETLEN (reset)")
        self.assertIn("ISMERETLEN", k.format_snapshot(self.snapshot(text), NOW + timedelta(hours=2)))

    def test_checker_rejects_oversized_and_fifo_input_without_blocking(self):
        self.store.mkdir()
        (self.store / c.SNAPSHOT).write_text("x" * (c.MAX_OUTPUT + 1))
        self.assertIn("ISMERETLEN", self.check())
        (self.store / c.SNAPSHOT).unlink()
        os.mkfifo(self.store / c.SNAPSHOT)
        self.assertIn("ISMERETLEN", self.check())

    def test_review_all_non_lf_line_separators_are_invalid(self):
        for separator in ("\x0c", "\x0b", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\r"):
            with self.subTest(separator=repr(separator)), self.assertRaisesRegex(c.UsageError, "invalid_output"):
                c.parse_usage(GOOD.replace("\n", separator))
        self.assertEqual(c.parse_usage(GOOD.rstrip("\n")), c.parse_usage(GOOD))
        with self.assertRaises(c.UsageError):
            c.parse_usage(GOOD + "\n")

    def test_review_env_has_only_four_allowlisted_variables(self):
        original = c.subprocess.Popen
        with patch.dict(os.environ, {"GEMINI_API_KEY": "SECRET", "GOOGLE_API_KEY": "SECRET", "XDG_CONFIG_HOME": "SECRET", "EXTRA": "SECRET"}):
            with patch.object(c.subprocess, "Popen", wraps=original) as process:
                self.assertEqual(c.run_usage(self.fake), GOOD)
        env = process.call_args.kwargs["env"]
        self.assertEqual(set(env), {"PATH", "HOME", "LANG", "TZ"})
        self.assertEqual(env["HOME"], c.PROVIDER_HOME)
        self.assertNotIn("SECRET", json.dumps(env))

    def test_review_killpg_called_on_success_too(self):
        with patch.object(c.os, "killpg", wraps=c.os.killpg) as kill:
            self.assertEqual(c.run_usage(self.fake), GOOD)
        kill.assert_called_once()
        self.assertEqual(kill.call_args.args[1], c.signal.SIGKILL)

    def test_review_success_kills_detached_stdio_grandchild(self):
        import time
        pidfile = self.base / "grandchild.pid"
        body = ("import subprocess,sys\nfrom pathlib import Path\n"
                "p=subprocess.Popen(['/usr/bin/python3','-c','import time; time.sleep(20)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                "Path(" + repr(str(pidfile)) + ").write_text(str(p.pid))\n"
                "sys.stdout.write(" + repr(GOOD) + ")\n")
        self.make_fake(body)
        self.assertEqual(c.run_usage(self.fake), GOOD)
        pid = int(pidfile.read_text())
        try:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    raw = Path('/proc') / str(pid) / 'stat'
                    state = raw.read_text().rsplit(')', 1)[1].split()[0]
                except FileNotFoundError:
                    break
                if state == 'Z':
                    break
                time.sleep(0.01)
            else:
                self.fail('grandchild remained running')
        finally:
            try:
                os.kill(pid, c.signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_review_rounding_and_status_use_identical_value(self):
        for remaining, shown, state in (("20.001%", "80", "FIGYELMEZTETÉS"), ("20.005%", "80", "FIGYELMEZTETÉS"),
                                        ("20.006%", "79.99", "OK"), ("10.001%", "90", "STOP"), ("10.005%", "90", "STOP"),
                                        ("10.006%", "89.99", "FIGYELMEZTETÉS"), ("99.999%", "0", "OK")):
            with self.subTest(remaining=remaining):
                line = k.format_snapshot(self.snapshot(GOOD.replace('83%', remaining)), NOW)
                self.assertIn('Gemini heti keret ' + shown + '% ' + state, line)
        self.assertEqual(k.display(17.1), '17.1')
        self.assertEqual(k.display(0.000001), '0')
        self.assertEqual(k.display(100), '100')

    def test_expired_gemini_reset_unknown_other_reset_informational(self):
        for reset in (NOW, NOW - timedelta(seconds=1)):
            data = self.snapshot()
            data['groups']['gemini']['resets_at'] = c.utc(reset)
            self.assertEqual(k.format_snapshot(data, NOW), 'Gemini heti keret: ISMERETLEN (reset)')
            data = self.snapshot()
            data['groups']['claude_gpt']['resets_at'] = c.utc(reset)
            line = k.format_snapshot(data, NOW)
            self.assertIn('17% OK', line)
            self.assertIn('(reset elmúlt)', line)
            self.assertNotIn('ISMERETLEN', line)
        data = self.snapshot()
        data['groups']['gemini']['resets_at'] = c.utc(NOW + timedelta(seconds=1))
        self.assertIn('17% OK', k.format_snapshot(data, NOW))

    def test_review_cleanup_only_fixed_old_regular_cli_logs(self):
        root = Path(c.PROVIDER_HOME)
        logs = root / '.gemini/antigravity-cli/log'
        logs.mkdir(parents=True)
        cutoff = NOW.timestamp() - 30 * 86400
        outside = root / 'outside.log'
        outside.write_text('SECRET')
        files = {'cli-old.log': cutoff - 1, 'cli-boundary.log': cutoff, 'cli-new.log': cutoff + 1,
                 'other.log': cutoff - 1, 'cli-old.txt': cutoff - 1, 'auth.json': cutoff - 1}
        for name, stamp in files.items():
            file = logs / name
            file.write_text('SECRET')
            os.utime(file, (stamp, stamp))
        (logs / 'cli-link.log').symlink_to(outside)
        (logs / 'cli-directory.log').mkdir()
        parent_log = logs.parent / 'cli-parent.log'
        parent_log.write_text('SECRET')
        os.utime(parent_log, (cutoff - 1, cutoff - 1))
        with patch.object(c, "datetime") as clock:
            clock.now.return_value = NOW
            c.cleanup_provider_logs()
        self.assertFalse((logs / 'cli-old.log').exists())
        for name in files:
            if name != 'cli-old.log':
                self.assertTrue((logs / name).exists())
        self.assertTrue((logs / 'cli-link.log').is_symlink())
        self.assertTrue((logs / 'cli-directory.log').is_dir())
        self.assertEqual(outside.read_text(), 'SECRET')
        self.assertTrue(parent_log.exists())

    def test_review_cleanup_errors_silent_and_symlink_parent_refused(self):
        logs = Path(c.PROVIDER_HOME) / '.gemini/antigravity-cli/log'
        logs.mkdir(parents=True)
        file = logs / 'cli-old.log'
        file.write_text('SECRET')
        old = NOW.timestamp() - 31 * 86400
        os.utime(file, (old, old))
        out, err = io.StringIO(), io.StringIO()
        with patch.object(c.os, 'unlink', side_effect=PermissionError('SECRET')), patch('sys.stdout', out), patch('sys.stderr', err):
            with patch.object(c, "datetime") as clock:
                clock.now.return_value = NOW
                c.cleanup_provider_logs()
        self.assertEqual(out.getvalue() + err.getvalue(), '')
        file.unlink()
        logs.rmdir()
        outside = self.base / 'outside-logs'
        outside.mkdir()
        protected = outside / 'cli-old.log'
        protected.write_text('SECRET')
        os.utime(protected, (old, old))
        logs.symlink_to(outside, target_is_directory=True)
        with patch.object(c, "datetime") as clock:
            clock.now.return_value = NOW
            c.cleanup_provider_logs()
        self.assertTrue(protected.exists())

    def test_cleanup_runs_for_default_binary_on_success_and_failure_only(self):
        with patch.object(c, 'run_usage', return_value=GOOD), patch.object(c, 'cleanup_provider_logs') as cleanup:
            self.assertEqual(self.main('--agy', str(c.DEFAULT_AGY))[0], 0)
        cleanup.assert_called_once_with()
        with patch.object(c, 'run_usage', side_effect=c.UsageError('provider_failed')), patch.object(c, 'cleanup_provider_logs') as cleanup:
            self.assertEqual(self.main('--agy', str(c.DEFAULT_AGY))[0], 1)
        cleanup.assert_called_once_with()

    def test_review_store_failed_distinct_from_lock_failed(self):
        with patch.object(c, 'open_store', side_effect=PermissionError('SECRET')):
            rc, out, err = self.main()
        self.assertEqual(rc, 1)
        self.assertIn('store_failed', out)
        self.assertNotIn('SECRET', out + err)
        self.assertIn('store_failed', c.ERRORS)
        self.store.mkdir(exist_ok=True)
        with patch.object(c.os, 'mkdir', side_effect=PermissionError('SECRET')):
            with self.assertRaisesRegex(c.UsageError, 'store_failed'):
                c.collect(self.store / 'nested', self.fake, NOW)
        self.assertEqual(c.FRESH_SECONDS, 5400)

    def test_fake_binary_custom_store_never_calls_cleanup(self):
        logs = Path(c.PROVIDER_HOME) / '.gemini/antigravity-cli/log'
        logs.mkdir(parents=True)
        file = logs / 'cli-old.log'
        file.write_text('fixture')
        stamp = datetime.now(timezone.utc).timestamp() - 40 * 86400
        os.utime(file, (stamp, stamp))
        with patch.object(c, 'cleanup_provider_logs') as cleanup:
            self.assertEqual(self.main('--now', '2030-01-01T00:00:00Z')[0], 0)
        cleanup.assert_not_called()
        self.assertTrue(file.exists())
        self.assertEqual(json.loads((self.store / c.SNAPSHOT).read_text())['collectedAt'], '2030-01-01T00:00:00Z')

    def test_future_now_does_not_advance_cleanup_clock(self):
        logs = Path(c.PROVIDER_HOME) / '.gemini/antigravity-cli/log'
        logs.mkdir(parents=True)
        actual = datetime.now(timezone.utc)
        fresh, old = logs / 'cli-29days.log', logs / 'cli-31days.log'
        for file, days in ((fresh, 29), (old, 31)):
            file.write_text('fixture')
            stamp = actual.timestamp() - days * 86400
            os.utime(file, (stamp, stamp))
        with patch.object(c, 'run_usage', return_value=GOOD):
            rc, _, _ = self.main('--agy', str(c.DEFAULT_AGY), '--now', '2030-01-01T00:00:00Z')
        self.assertEqual(rc, 0)
        self.assertTrue(fresh.exists())
        self.assertFalse(old.exists())
        self.assertEqual(json.loads((self.store / c.SNAPSHOT).read_text())['collectedAt'], '2030-01-01T00:00:00Z')

    def test_error_log_clock_not_overridden_by_now(self):
        with patch.object(c, 'run_usage', side_effect=c.UsageError('provider_failed')):
            rc, _, _ = self.main('--now', '2030-01-01T00:00:00Z')
        self.assertEqual(rc, 1)
        stamp = (self.store / c.LOG).read_text().split()[0]
        observed = c.parse_time(stamp)
        self.assertLess(abs((datetime.now(timezone.utc) - observed).total_seconds()), 10)
        self.assertFalse(stamp.startswith('2030-'))

    def test_service_timer_templates_only(self):
        import configparser
        service = configparser.ConfigParser(interpolation=None)
        service.read(SCRIPTS / "systemd/gemini-usage-collect.service")
        self.assertEqual(service["Service"]["Type"], "oneshot")
        self.assertEqual(service["Service"]["SuccessExitStatus"], "75")
        self.assertEqual(service["Service"].getint("TimeoutStartSec"), 60)
        self.assertEqual(service["Service"]["ExecStart"], "/usr/bin/python3 /path/to/marveen/scripts/gemini-usage-collect.py")
        timer = configparser.ConfigParser(interpolation=None)
        timer.read(SCRIPTS / "systemd/gemini-usage-collect.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "*-*-* *:00,30:00")
        self.assertTrue(timer["Timer"].getboolean("Persistent"))


# Real agy /usage output since the 11-26 build (captured 2026-11-28).
THREE_ROWS = ("Gemini Models\tWeekly Limit Remaining\t100%\t2026-12-05T11:19:54Z\n"
              "Claude and GPT models\tWeekly Limit Remaining\t100%\t1970-01-01T00:00:00Z\n"
              "Claude and GPT models\tFive Hour Limit Remaining\t100%\t1970-01-01T00:00:00Z\n")


class ThreeRowOutputTests(unittest.TestCase):
    setUp = GeminiTests.setUp
    make_fake = GeminiTests.make_fake
    main = GeminiTests.main
    check = GeminiTests.check

    def test_real_three_row_output_parses_with_separate_five_hour_group(self):
        details = {}
        groups = c.parse_usage(THREE_ROWS, details)
        self.assertEqual(set(groups), {"gemini", "claude_gpt", "claude_gpt_five_hour"})
        self.assertEqual(groups["gemini"], {"remaining_pct": 100, "used_pct": 0, "resets_at": "2026-12-05T11:19:54Z"})
        self.assertEqual(groups["claude_gpt"]["resets_at"], None)
        self.assertEqual(groups["claude_gpt_five_hour"], {"remaining_pct": 100, "used_pct": 0, "resets_at": None})
        self.assertEqual(details["unrecognized_rows"], [])

    def test_epoch_zero_in_any_offset_is_no_window_other_times_stay(self):
        text = THREE_ROWS.replace("1970-01-01T00:00:00Z", "1970-01-01T01:00:00+01:00", 1)
        self.assertIsNone(c.parse_usage(text)["claude_gpt"]["resets_at"])
        text = THREE_ROWS.replace("1970-01-01T00:00:00Z", "1970-01-01T00:00:01Z", 1)
        self.assertEqual(c.parse_usage(text)["claude_gpt"]["resets_at"], "1970-01-01T00:00:01Z")

    def test_collector_and_checker_end_to_end_on_three_rows(self):
        self.make_fake("import sys\nsys.stdout.write(" + repr(THREE_ROWS) + ")\n")
        rc, out, _ = self.main()
        self.assertEqual(rc, 0, out)
        snapshot = json.loads((self.store / c.SNAPSHOT).read_text())
        self.assertNotIn("unrecognizedRows", snapshot)
        line = self.check()
        self.assertEqual(line.strip(),
                         "Gemini heti keret 0% OK, maradt: 100%, reset: 2026-12-05T11:19:54Z | "
                         "Gemini szolgáltatói Claude/GPT heti keret 0% használt, maradt: 100%, reset: nincs aktív ablak | "
                         "Gemini szolgáltatói Claude/GPT 5 órás keret 0% használt, maradt: 100%, reset: nincs aktív ablak")
        self.assertNotIn(" -- ", line)
        self.assertNotIn("\u2014", line)

    def test_two_row_output_still_valid(self):
        self.assertEqual(set(c.parse_usage(GOOD)), {"gemini", "claude_gpt"})
        line = k.format_snapshot({"collectedAt": c.utc(NOW), "groups": c.parse_usage(GOOD)}, NOW)
        self.assertNotIn("5 órás", line)

    def test_gemini_without_window_is_ok_only_when_untouched(self):
        text = THREE_ROWS.replace("2026-12-05T11:19:54Z", "1970-01-01T00:00:00Z")
        line = k.format_snapshot({"collectedAt": c.utc(NOW), "groups": c.parse_usage(text)}, NOW)
        self.assertIn("Gemini heti keret 0% OK, maradt: 100%, reset: nincs aktív ablak", line)
        text = text.replace("100%", "60%", 1)
        line = k.format_snapshot({"collectedAt": c.utc(NOW), "groups": c.parse_usage(text)}, NOW)
        self.assertEqual(line, "Gemini heti keret: ISMERETLEN (reset)")

    def test_unknown_well_formed_row_is_recorded_not_fatal(self):
        text = THREE_ROWS + "Gemini Models\tFive Hour Limit Remaining\t90%\t2026-11-28T15:00:00Z\n"
        details = {}
        groups = c.parse_usage(text, details)
        self.assertEqual(details["unrecognized_rows"], ["Gemini Models / Five Hour Limit Remaining"])
        self.assertNotIn("gemini_five_hour", groups)
        self.make_fake("import sys\nsys.stdout.write(" + repr(text) + ")\n")
        rc, _, _ = self.main()
        self.assertEqual(rc, 0)
        snapshot = json.loads((self.store / c.SNAPSHOT).read_text())
        self.assertEqual(snapshot["unrecognizedRows"], ["Gemini Models / Five Hour Limit Remaining"])
        self.assertIn("| fel nem dolgozott új sor a kimenetben: 1", self.check())

    def test_malformed_unsafe_duplicate_or_too_many_rows_still_fail(self):
        extra = "Gemini Models\tFive Hour Limit Remaining\t90%\t2026-11-28T15:00:00Z\n"
        bad = (THREE_ROWS + THREE_ROWS.splitlines(True)[2],                    # duplicate known optional row
               THREE_ROWS + extra + extra,                                       # duplicate unknown row
               THREE_ROWS + "New $(id) row\tWeekly\t5%\t2026-11-28T15:00:00Z\n",  # unsafe label
               THREE_ROWS + "New row\tWeekly\t5\t2026-11-28T15:00:00Z\n",       # bad percent
               THREE_ROWS + "New row\tWeekly\t5%\tsoon\n",                      # bad time
               THREE_ROWS + "New row\tWeekly\t5%\n",                             # field count
               THREE_ROWS + "".join(f"Row {i}\tWeekly\t5%\t2026-11-28T15:00:00Z\n" for i in range(6)),
               THREE_ROWS.splitlines(True)[0] + THREE_ROWS.splitlines(True)[2])  # required claude_gpt missing
        for text in bad:
            with self.subTest(text=text), self.assertRaisesRegex(c.UsageError, "invalid_output"):
                c.parse_usage(text)

    def test_checker_rejects_unknown_group_and_bad_unrecognized_list(self):
        base = {"collectedAt": c.utc(NOW), "groups": c.parse_usage(THREE_ROWS)}
        data = json.loads(json.dumps(base))
        data["groups"]["mystery"] = dict(data["groups"]["gemini"])
        self.assertEqual(k.format_snapshot(data, NOW), "Gemini heti keret: ISMERETLEN (missing)")
        for rows in ("x", [1], ["no separator"], ["A / B", "A / B"], ["$(x) / y"]):
            with self.subTest(rows=rows):
                data = json.loads(json.dumps(base))
                data["unrecognizedRows"] = rows
                self.assertEqual(k.format_snapshot(data, NOW), "Gemini heti keret: ISMERETLEN (missing)")
        data = json.loads(json.dumps(base))
        del data["groups"]["claude_gpt"]
        self.assertEqual(k.format_snapshot(data, NOW), "Gemini heti keret: ISMERETLEN (missing)")


if __name__ == "__main__":
    unittest.main()

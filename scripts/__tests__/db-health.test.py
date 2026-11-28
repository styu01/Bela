#!/usr/bin/env python3
"""Isolated stdlib unittest suite for scripts/db-health.py.

Scratch databases and a fake "dashboard" process only: no real dashboard, no
production DB, no network. Run it through the sandbox wrapper, e.g.
  sandbox-run.sh SCRATCH python3 -I scripts/__tests__/db-health.test.py
DB_HEALTH_SCRIPT may point at another copy of the script (mutation runs).
"""
import fcntl
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

PATH = Path(os.environ.get("DB_HEALTH_SCRIPT")
            or Path(__file__).resolve().parent.parent / "db-health.py")
spec = importlib.util.spec_from_file_location("db_health", PATH)
dh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dh)
dh.RECHECK_DELAY = 0

SCHEMA = """
CREATE TABLE agent_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL);
CREATE TABLE conversation_log (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT);
CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL);
CREATE TABLE kanban_cards (id TEXT PRIMARY KEY, title TEXT NOT NULL);
"""
T0 = 1795860000.0


def make_db(path, rows=50, wal=True):
    db = sqlite3.connect(path, isolation_level=None)
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.execute("BEGIN")
    for i in range(rows):
        db.execute("INSERT INTO agent_messages(content) VALUES (?)", ("m" * 200 + str(i),))
        db.execute("INSERT INTO conversation_log(text) VALUES (?)", ("c" * 200 + str(i),))
        db.execute("INSERT INTO memories(content) VALUES (?)", ("x" * 200 + str(i),))
        db.execute("INSERT INTO kanban_cards(id, title) VALUES (?, ?)", (f"k{i:04d}", "t"))
    db.execute("COMMIT")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)") if wal else None
    db.close()


class FakeTransport:
    def __init__(self, agent_ok=True, telegram_ok=True, raise_text=None):
        self.agent_ok, self.telegram_ok, self.raise_text = agent_ok, telegram_ok, raise_text
        self.sent = []

    def send_agent(self, content):
        if self.raise_text:
            raise RuntimeError(self.raise_text)
        if not self.agent_ok:
            raise dh.DeliveryError("network_failure")
        self.sent.append(("agent", content))
        return "agent"

    def send_telegram(self, content):
        if not self.telegram_ok:
            raise dh.DeliveryError("telegram_credentials_unavailable")
        self.sent.append(("telegram", content))
        return "telegram"


FAKE_DASHBOARD = textwrap.dedent("""
    import sqlite3, sys, time
    db = sqlite3.connect(sys.argv[1], isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    for i in range(int(sys.argv[2])):
        db.execute("INSERT INTO agent_messages(content) VALUES (?)", ("ghost-%d" % i,))
    db.execute("SELECT count(*) FROM agent_messages").fetchone()
    print("ready", flush=True)
    sys.stdin.readline()          # stay alive with the files open until told
    n = db.execute("SELECT count(*) FROM agent_messages").fetchone()[0]
    print(n, flush=True)
""")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "store"
        self.store.mkdir()
        self.db = str(self.store / "claudeclaw.db")
        # Only ever a private temp dir (a private tmpfs under sandbox-run.sh).
        self.assertTrue(os.path.realpath(self.db).startswith(os.path.realpath(tempfile.gettempdir()) + os.sep))
        self.now = T0
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait(5)
            for stream in (p.stdin, p.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
        self.tmp.cleanup()

    def run_once(self, transport=None, pid=0, **extra):
        args = SimpleNamespace(store=str(self.store), db=None, unit="x.service", dashboard_pid=pid,
                               dashboard_url="http://127.0.0.1:9", chat_id="1", main_agent="mainx", quick=False,
                               dry_run=False, force_rescue=False)
        for k, v in extra.items():
            setattr(args, k, v)
        self.transport = transport or FakeTransport()
        runner = dh.Runner(args, transport=self.transport, clock=lambda: self.now)
        return runner.run()

    def state(self):
        return json.loads((self.store / ".db-health-state.json").read_text())

    def log_lines(self):
        path = self.store / "logs" / "db-health.log"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def start_dashboard(self, rows=30):
        p = subprocess.Popen([sys.executable, "-I", "-c", FAKE_DASHBOARD, self.db, str(rows)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        return p


class HealthyAndPathChecks(Base):
    def test_healthy_run_is_silent_and_records_baseline(self):
        make_db(self.db)
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.transport.sent, [])
        st = self.state()
        self.assertEqual(st["baseline"]["seq"]["agent_messages"], 50)
        self.assertEqual(st["baseline"]["counts"]["kanban_cards"], 50)
        self.assertEqual(self.log_lines()[-1]["result"], "ok")
        self.assertEqual(oct(os.stat(self.store / ".db-health-state.json").st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(self.store / "logs" / "db-health.log").st_mode & 0o777), "0o600")

    def test_stopped_dashboard_db_gets_no_wal_or_shm_created(self):
        make_db(self.db)          # WAL mode; the last close removed -wal/-shm
        self.assertFalse(os.path.exists(self.db + "-wal") or os.path.exists(self.db + "-shm"))
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.transport.sent, [])
        self.assertFalse(os.path.exists(self.db + "-wal"), "check must not create -wal")
        self.assertFalse(os.path.exists(self.db + "-shm"), "check must not create -shm")
        self.assertTrue(self.state()["baseline"]["seq"]["agent_messages"] == 50)

    def test_running_writer_wal_is_read_not_ignored(self):
        make_db(self.db)
        dash = self.start_dashboard(rows=30)    # 30 rows only in the live WAL
        self.run_once()
        self.assertEqual(self.state()["baseline"]["seq"]["agent_messages"], 80)
        self.assertEqual(self.log_lines()[-1]["result"], "ok")

    def test_missing_db_alerts_main_agent(self):
        self.assertEqual(self.run_once(), 0)
        (kind, text), = self.transport.sent
        self.assertEqual(kind, "agent")
        self.assertIn("[DB-HEALTH]", text)
        self.assertIn("nem létezik", text)
        self.assertIn("nem indítottam újra", text)
        self.assertFalse(os.path.exists(self.db), "the check must never create the DB")

    def test_empty_file_alerts(self):
        Path(self.db).write_bytes(b"")
        self.run_once()
        self.assertIn("0 bájtos", self.transport.sent[0][1])

    def test_symlink_is_not_followed(self):
        make_db(str(self.store / "real.db"))
        os.symlink("real.db", self.db)
        self.run_once()
        self.assertIn("nem szabályos fájl", self.transport.sent[0][1])

    def test_fresh_empty_db_alerts_and_does_not_become_baseline(self):
        make_db(self.db)
        self.run_once()
        os.unlink(self.db)
        stray = sqlite3.connect(self.db)   # what a stray opener would create
        stray.execute("CREATE TABLE unrelated (x)")
        stray.close()
        self.now += 3600
        self.run_once()
        self.assertIn("hiányzó fő tábla", self.transport.sent[0][1])
        self.assertEqual(self.state()["baseline"]["seq"]["agent_messages"], 50)

    def test_garbage_file_is_reported(self):
        Path(self.db).write_bytes(b"not a database" * 400)
        self.run_once()
        self.assertIn("sem nyitható meg", self.transport.sent[0][1])

    def test_corrupted_page_is_reported(self):
        make_db(self.db, rows=400, wal=False)
        db = sqlite3.connect(self.db)
        root = db.execute("SELECT rootpage FROM sqlite_master WHERE name='memories'").fetchone()[0]
        db.close()
        data = bytearray(Path(self.db).read_bytes())
        data[(root - 1) * 4096] = 0x01          # invalid b-tree page type
        Path(self.db).write_bytes(bytes(data))
        self.run_once()
        text = self.transport.sent[0][1]
        self.assertTrue("integrity_check" in text or "sem nyitható meg" in text, text)

    def test_index_mismatch_hits_the_integrity_branch(self):
        make_db(self.db, rows=50, wal=False)
        db = sqlite3.connect(self.db)
        root = db.execute("SELECT rootpage FROM sqlite_master WHERE name='sqlite_autoindex_kanban_cards_1'"
                          ).fetchone()[0]
        db.close()
        data = bytearray(Path(self.db).read_bytes())
        start = (root - 1) * 4096
        page = data[start:start + 4096]
        at = page.index(b"k0007")
        data[start + at:start + at + 5] = b"k9x07"
        Path(self.db).write_bytes(bytes(data))
        self.run_once()
        text = self.transport.sent[0][1]
        self.assertIn("PRAGMA integrity_check hibát jelzett", text)
        self.assertNotIn("sem nyitható meg", text)

    def test_transient_open_failure_is_rechecked_before_alerting(self):
        make_db(self.db)
        real, calls = dh.inspect_db, []

        def flaky(path, quick=False):
            calls.append(1)
            if len(calls) == 1:
                return {"exists": True, "size": 1}, [("db_open_failed", "x")]
            return real(path, quick)
        dh.inspect_db = flaky
        try:
            self.assertEqual(self.run_once(), 0)
        finally:
            dh.inspect_db = real
        self.assertEqual((len(calls), self.transport.sent), (2, []))

    def test_sequence_decrease_is_an_event_announced_once_then_recovered(self):
        make_db(self.db)
        self.run_once()
        os.unlink(self.db)
        make_db(self.db, rows=40)
        self.now += 3600
        self.run_once()
        self.assertIn("legnagyobb kiosztott id 50 helyett most 40", self.transport.sent[0][1])
        self.now += 3600
        self.run_once()
        (kind, text), = self.transport.sent
        self.assertIn("újra rendben", text)
        self.assertIn("magától nem szűnik meg", text)
        self.now += 3600
        self.run_once()
        self.assertEqual(self.transport.sent, [], "healthy again: silent")

    def test_size_drop_and_row_drop(self):
        make_db(self.db, rows=400, wal=False)
        self.run_once()
        db = sqlite3.connect(self.db, isolation_level=None)
        db.execute("DELETE FROM kanban_cards WHERE rowid > 20")
        db.execute("DELETE FROM memories WHERE id > 20")
        db.execute("DELETE FROM conversation_log")
        db.execute("DELETE FROM agent_messages WHERE id < 390")
        db.execute("VACUUM")
        db.close()
        self.now += 3600
        self.run_once()
        text = self.transport.sent[0][1]
        self.assertIn("több mint 50%", text)
        self.assertIn("kanban_cards: a sorok száma 400 helyett 20", text)
        self.assertNotIn("legnagyobb kiosztott id", text, "deletes keep sqlite_sequence")

    def test_small_deletes_are_not_alarms(self):
        make_db(self.db)
        self.run_once()
        db = sqlite3.connect(self.db, isolation_level=None)
        db.execute("DELETE FROM kanban_cards WHERE id = 'k0049'")
        db.execute("DELETE FROM memories WHERE id = 50")
        db.close()
        self.now += 3600
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.transport.sent, [])


class AlertDelivery(Base):
    def test_same_problem_reminded_only_after_six_hours(self):
        self.run_once()
        self.assertEqual(len(self.transport.sent), 1)
        for _ in range(5):
            self.now += 3600
            self.run_once()
            self.assertEqual(self.transport.sent, [])
        self.now += 3600
        self.run_once()
        self.assertEqual(len(self.transport.sent), 1)

    def test_changed_problem_is_announced_at_once(self):
        self.run_once()
        Path(self.db).write_bytes(b"")
        self.now += 60
        self.run_once()
        self.assertIn("0 bájtos", self.transport.sent[0][1])

    def test_agent_api_down_falls_back_to_telegram(self):
        self.run_once(FakeTransport(agent_ok=False))
        (kind, _), = self.transport.sent
        self.assertEqual(kind, "telegram")
        self.assertEqual(self.log_lines()[-1]["delivery_codes"], ["network_failure"])

    def test_agent_accepted_means_no_telegram(self):
        self.run_once()
        self.assertEqual([k for k, _ in self.transport.sent], ["agent"])

    def test_all_channels_down_fails_unit_and_retries_next_run(self):
        self.assertEqual(self.run_once(FakeTransport(agent_ok=False, telegram_ok=False)), 1)
        self.assertEqual(self.state()["alert"], {})
        self.now += 3600
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(len(self.transport.sent), 1, "undelivered alert retried, not deduped")

    def test_recovery_notice_retried_when_undelivered(self):
        self.run_once()
        make_db(self.db)
        self.now += 3600
        self.assertEqual(self.run_once(FakeTransport(agent_ok=False, telegram_ok=False)), 1)
        self.now += 3600
        self.run_once()
        self.assertIn("újra rendben", self.transport.sent[0][1])

    def test_exception_text_never_reaches_the_log(self):
        self.run_once(FakeTransport(raise_text="Bearer SECRET-TOKEN https://x/bot123:abc"))
        raw = (self.store / "logs" / "db-health.log").read_text()
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("bot123", raw)
        self.assertIn("delivery_failure", raw)

    def test_dry_run_writes_nothing_and_sends_nothing(self):
        self.assertEqual(self.run_once(dry_run=True), 0)
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(sorted(os.listdir(self.store)), [])

    def test_second_instance_is_busy_and_silent(self):
        fd = os.open(self.store / ".db-health.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(self.run_once(), 0)
        finally:
            os.close(fd)
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.log_lines()[-1]["result"], "busy")
        self.assertFalse((self.store / ".db-health-state.json").exists())


class GhostRescue(Base):
    def rescue_dirs(self):
        return sorted(p for p in self.store.iterdir() if p.name.startswith("recovery-db-"))

    def test_deleted_db_and_wal_are_rescued_with_wal_only_rows(self):
        make_db(self.db)
        dash = self.start_dashboard(rows=30)      # 30 rows live only in the WAL
        self.assertGreater(os.path.getsize(self.db + "-wal"), 0)
        self.run_once(pid=dash.pid)
        self.assertEqual(self.transport.sent, [], "healthy while the files are linked")
        os.unlink(self.db)
        os.unlink(self.db + "-wal")
        self.now += 3600
        self.run_once(pid=dash.pid)
        text = self.transport.sent[0][1]
        self.assertIn("törölt vagy lecserélt", text)
        rescue, = self.rescue_dirs()
        self.assertIn(str(rescue), text)
        self.assertEqual(oct(rescue.stat().st_mode & 0o777), "0o700")
        manifest = json.loads((rescue / "MANIFEST.json").read_text())
        self.assertTrue(manifest["verified"]["ok"])
        self.assertEqual(manifest["verified"]["journal_mode"], "delete")
        self.assertEqual(manifest["raw"]["claudeclaw.db"]["nlink"], 0)
        self.assertTrue((rescue / "raw" / "claudeclaw.db-wal").exists())
        v = sqlite3.connect((rescue / "claudeclaw.verified.db").as_uri() + "?mode=ro", uri=True)
        self.assertEqual(v.execute("SELECT count(*) FROM agent_messages").fetchone()[0], 80)
        self.assertEqual(v.execute("SELECT count(*) FROM agent_messages WHERE content LIKE 'ghost-%'")
                         .fetchone()[0], 30)
        v.close()
        self.assertFalse(os.path.exists(self.db), "live path must not be rewritten")
        self.assertIsNone(dash.poll(), "dashboard must not be touched")
        dash.stdin.write("\n"); dash.stdin.flush()
        self.assertEqual(dash.stdout.readline().strip(), "80")
        self.assertFalse(list(rescue.glob("work-*")), "work dir cleaned up")
        for f in [rescue / "claudeclaw.verified.db", rescue / "MANIFEST.json", *(rescue / "raw").iterdir()]:
            self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600", f)

    def test_db_replaced_by_empty_new_file(self):
        make_db(self.db)
        dash = self.start_dashboard()
        self.run_once(pid=dash.pid)
        os.unlink(self.db)
        sqlite3.connect(self.db).close()
        Path(self.db).write_bytes(b"")
        self.now += 3600
        self.run_once(pid=dash.pid)
        text = self.transport.sent[0][1]
        self.assertIn("0 bájtos", text)
        self.assertIn("törölt vagy lecserélt", text)
        self.assertEqual(len(self.rescue_dirs()), 1)

    def test_only_wal_deleted_is_rescued_as_a_pair(self):
        make_db(self.db)
        dash = self.start_dashboard(rows=10)
        os.unlink(self.db + "-wal")
        self.run_once(pid=dash.pid)
        self.assertIn("a WAL-naplót", self.transport.sent[0][1])
        rescue, = self.rescue_dirs()
        v = sqlite3.connect((rescue / "claudeclaw.verified.db").as_uri() + "?mode=ro", uri=True)
        self.assertEqual(v.execute("SELECT count(*) FROM agent_messages").fetchone()[0], 60)
        v.close()

    def test_same_ghost_is_not_recopied_every_hour_but_after_six(self):
        make_db(self.db)
        dash = self.start_dashboard()
        os.unlink(self.db)
        self.run_once(pid=dash.pid)
        first, = self.rescue_dirs()
        for _ in range(5):
            self.now += 3600
            self.run_once(pid=dash.pid)
        self.assertEqual(self.rescue_dirs(), [first])
        self.now += 3600
        self.run_once(pid=dash.pid)
        self.assertEqual(len(self.rescue_dirs()), 2)
        self.assertIn(str(self.rescue_dirs()[-1]), self.transport.sent[-1][1])

    def test_force_rescue_takes_a_fresh_copy(self):
        make_db(self.db)
        dash = self.start_dashboard()
        os.unlink(self.db)
        self.run_once(pid=dash.pid)
        self.now += 60
        self.run_once(pid=dash.pid, force_rescue=True)
        self.assertEqual(len(self.rescue_dirs()), 2)

    def test_wal_restart_during_every_copy_is_flagged(self):
        make_db(self.db)
        dash = self.start_dashboard()
        os.unlink(self.db)
        real, calls = dh.wal_header, []

        def moving(pid, fd):
            calls.append(fd)
            return real(pid, fd) + str(len(calls)).encode()   # a new header every read
        dh.wal_header = moving
        try:
            self.run_once(pid=dash.pid)
        finally:
            dh.wal_header = real
        rescue, = self.rescue_dirs()
        manifest = json.loads((rescue / "MANIFEST.json").read_text())
        self.assertEqual((manifest["attempts"], manifest["consistent_copy"]), (dh.COPY_ATTEMPTS, False))
        self.assertTrue(manifest["verified"]["ok"])
        self.assertIn("checkpoint futott", self.transport.sent[0][1])

    def test_db_file_write_during_copy_is_not_consistent(self):
        make_db(self.db)
        dash = self.start_dashboard()
        os.unlink(self.db)
        real, calls = dh.fd_signature, []

        def moving(pid, fd):
            calls.append(fd)
            size, mtime = real(pid, fd)
            return size, mtime + len(calls)          # a checkpoint between every read
        dh.fd_signature = moving
        try:
            self.run_once(pid=dash.pid)
        finally:
            dh.fd_signature = real
        manifest = json.loads((self.rescue_dirs()[0] / "MANIFEST.json").read_text())
        self.assertEqual((manifest["attempts"], manifest["consistent_copy"]), (dh.COPY_ATTEMPTS, False))

    def test_stable_wal_header_is_a_consistent_first_attempt(self):
        make_db(self.db)
        dash = self.start_dashboard()
        os.unlink(self.db)
        self.run_once(pid=dash.pid)
        rescue, = self.rescue_dirs()
        manifest = json.loads((rescue / "MANIFEST.json").read_text())
        self.assertEqual((manifest["attempts"], manifest["consistent_copy"]), (1, True))
        self.assertNotIn("checkpoint futott", self.transport.sent[0][1])

    def test_dry_run_with_ghost_does_not_copy(self):
        make_db(self.db)
        dash = self.start_dashboard()
        os.unlink(self.db)
        self.run_once(pid=dash.pid, dry_run=True)
        self.assertEqual(self.rescue_dirs(), [])
        self.assertEqual(self.transport.sent, [])

    def test_dry_run_with_force_rescue_still_writes_nothing(self):
        make_db(self.db)
        dash = self.start_dashboard()
        os.unlink(self.db)
        before = sorted(os.listdir(self.store))
        self.assertEqual(self.run_once(pid=dash.pid, dry_run=True, force_rescue=True), 0)
        self.assertEqual(self.rescue_dirs(), [])
        self.assertEqual(sorted(os.listdir(self.store)), before)
        self.assertEqual(self.transport.sent, [])

    def test_failed_rescue_is_not_reannounced_every_hour(self):
        make_db(self.db)
        dash = self.start_dashboard()
        os.unlink(self.db)
        real = dh.shutil.disk_usage
        dh.shutil.disk_usage = lambda p: SimpleNamespace(free=1, total=1, used=0)
        try:
            self.run_once(pid=dash.pid)
            self.assertIn("no_space", self.transport.sent[0][1])
            for _ in range(5):
                self.now += 3600
                self.run_once(pid=dash.pid)
                self.assertEqual(self.transport.sent, [], "same failed rescue: quiet within 6 h")
            self.now += 3600
            self.run_once(pid=dash.pid)
            self.assertEqual(len(self.transport.sent), 1, "reminder after 6 h")
        finally:
            dh.shutil.disk_usage = real
        self.now += 60
        self.run_once(pid=dash.pid)            # space is back: the rescue works
        self.assertEqual(len(self.rescue_dirs()), 1)
        self.assertIn(str(self.rescue_dirs()[0]), self.transport.sent[0][1], "success announced at once")

    def test_failed_repeat_rescue_points_at_the_earlier_one(self):
        make_db(self.db)
        dash = self.start_dashboard()
        os.unlink(self.db)
        self.run_once(pid=dash.pid)
        first, = self.rescue_dirs()
        self.now += 6 * 3600
        real = dh.shutil.disk_usage
        dh.shutil.disk_usage = lambda p: SimpleNamespace(free=1, total=1, used=0)
        try:
            self.run_once(pid=dash.pid)
        finally:
            dh.shutil.disk_usage = real
        text = self.transport.sent[0][1]
        self.assertIn("no_space", text)
        self.assertIn(f"Korábbi kimentés ugyanerről a példányról: {first}", text)

    def test_dashboard_gone_with_missing_db_says_so(self):
        self.run_once(pid=999999999)
        self.assertIn("nem fut vagy nem olvasható", self.transport.sent[0][1])

    def test_unrelated_deleted_files_of_the_dashboard_are_ignored(self):
        make_db(self.db)
        dash = self.start_dashboard()
        other = self.store / "other.db"
        make_db(str(other))
        holder = open(other, "rb")
        try:
            os.unlink(other)
            self.run_once(pid=dash.pid)
        finally:
            holder.close()
        self.assertEqual(self.transport.sent, [])

    def test_no_space_is_reported_not_crashed(self):
        make_db(self.db)
        dash = self.start_dashboard()
        os.unlink(self.db)
        real = dh.shutil.disk_usage
        dh.shutil.disk_usage = lambda p: SimpleNamespace(free=1, total=1, used=0)
        try:
            self.run_once(pid=dash.pid)
        finally:
            dh.shutil.disk_usage = real
        self.assertIn("Kimentés NEM sikerült (no_space)", self.transport.sent[0][1])
        self.assertEqual(self.rescue_dirs(), [])


class TransportUnit(unittest.TestCase):
    def test_agent_payload_and_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            (store / ".dashboard-token").write_text("tok123\n")
            t = dh.Transport(store, store, "http://127.0.0.1:9", "1", "mainx")
            seen = {}

            def fake(url, payload, headers):
                seen.update(url=url, payload=payload, headers=headers)
                return {"id": 7}
            t._post = fake
            self.assertEqual(t.send_agent("hi"), "agent")
            self.assertEqual(seen["payload"], {"from": "mainx", "to": "mainx", "content": "hi"})
            self.assertEqual(seen["headers"]["Authorization"], "Bearer tok123")
            t._post = lambda *a: {"ok": True}
            with self.assertRaises(dh.DeliveryError):
                t.send_agent("hi")

    def test_telegram_requires_ok_and_valid_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("TELEGRAM_BOT_TOKEN=12:ab_C\n")
            t = dh.Transport(root, root, "http://127.0.0.1:9", "-100", "mainx")
            seen = {}
            t._post = lambda url, payload, headers: seen.update(url=url, payload=payload) or {"ok": True}
            self.assertEqual(t.send_telegram("x"), "telegram")
            self.assertEqual(seen["payload"], {"chat_id": "-100", "text": "x"})
            t._post = lambda *a: {"ok": False}
            with self.assertRaises(dh.DeliveryError):
                t.send_telegram("x")
            t.chat_id = ""
            t._post = lambda *a: {"ok": True}
            with self.assertRaisesRegex(dh.DeliveryError, "telegram_chat_unavailable"):
                t.send_telegram("x")

    def test_install_values_come_from_env_at_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("MAIN_AGENT_ID=zed\nWEB_PORT=4555\nALLOWED_CHAT_ID='42'\n"
                                       "TELEGRAM_BOT_TOKEN=1:x\n")
            cfg = dh.install_settings(root, {})
            self.assertEqual(cfg, {"main": "zed", "url": "http://localhost:4555", "chat": "42",
                                   "unit": "zed-dashboard.service"})
            cfg = dh.install_settings(root, {"MARVEEN_ALERT_CHAT_ID": "7",
                                             "DB_HEALTH_DASHBOARD_UNIT": "u.service"})
            self.assertEqual((cfg["chat"], cfg["unit"]), ("7", "u.service"))
            (root / ".env").write_text("MAIN_AGENT_ID=a b;rm\nWEB_PORT=x\nALLOWED_CHAT_ID=abc\n")
            self.assertEqual(dh.install_settings(root, {}),
                             {"main": "marveen", "url": "http://localhost:3420", "chat": "",
                              "unit": "marveen-dashboard.service"})

    def test_texts_have_no_dash_tokens(self):
        text = dh.alert_text([("db_missing", "x")], "/s/claudeclaw.db",
                             {"dir": "/s/recovery-db-1", "verified": {"integrity": "ok", "counts": {"a": 1}},
                              "consistent_copy": False}, 1, "/b", T0, True)
        for bad in ("—", "–", " -- "):
            self.assertNotIn(bad, text)
            self.assertNotIn(bad, dh.recovered_text(["seq_decreased"], T0))

    def test_dashboard_pid_zero_means_none(self):
        self.assertIsNone(dh.dashboard_pid(0, "x"))


if __name__ == "__main__":
    unittest.main(verbosity=1)

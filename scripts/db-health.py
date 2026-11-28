#!/usr/bin/env python3
"""Hourly, model-free health check of the live dashboard database.

Owner decision 2026-11-28 (after the 10:11 incident in which a test run deleted
store/claudeclaw.db while the dashboard kept writing into the unlinked inode):
the nightly backup stays the only backup; this script only LOOKS once an hour
and, if the file at the path is gone/emptied/replaced, rescues the copy the
running dashboard still holds open through /proc/<pid>/fd.

What one run does (no model, stdlib only, Linux):
  1. Path checks on the DB (read-only, mode=ro, connection closed right away):
     exists, regular file, not 0 bytes, size not collapsed (>50% drop vs the
     last healthy run), opens read-only, the core tables exist, PRAGMA
     integrity_check is ok (0.3 s on the 61 MB live DB, measured 2026-11-28;
     a --quick switch exists for a larger future DB), the AUTOINCREMENT high
     water marks (sqlite_sequence) of agent_messages / conversation_log /
     memories did not go down, and no core table lost more than half its rows.
  2. Dashboard check: the fds of the dashboard MainPID that point at the DB,
     its -wal or its -shm. An fd whose file has no link left (st_nlink == 0) or
     whose inode differs from the file now at the path means the dashboard is
     writing into a ghost.
  3. Rescue (only in that ghost case): the ghost DB + WAL (+ SHM, forensic)
     are copied TOGETHER from /proc/<pid>/fd into store/recovery-db-<stamp>/
     (mode 0700), then a WAL-free copy is built with the SQLite backup API and
     verified (integrity_check + core tables). The live path is never written,
     the dashboard is never restarted: restoring is the main agent's decision.
  4. Alert: one inter-agent message to the main agent (.env MAIN_AGENT_ID,
     sender = the same id, like kanban-audit) through the dashboard API; if
     that API does not accept it, a plain-text Telegram message to the owner
     chat (MARVEEN_ALERT_CHAT_ID, else .env ALLOWED_CHAT_ID). A healthy run is silent (one JSON line in store/logs/db-health.log).
     The same problem is re-announced at most every REMIND_SECONDS; one
     "back to normal" notice follows an announced problem.
  5. State (store/.db-health-state.json): last healthy size, inode, sequence
     values and row counts, plus alert/rescue bookkeeping.

Concurrency: an exclusive non-blocking flock on store/.db-health.lock; a second
instance logs "busy" and exits 0. The live DB is never locked for writing.
No token, URL with a token, exception text or response body is ever logged.

Exit codes: 0 = ran (healthy, or problem announced/already announced),
1 = a problem exists and no alert channel accepted the message (the unit turns
failed, the next run retries), 2 = usage/internal error.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DB_NAME = "claudeclaw.db"
STATE = ".db-health-state.json"
LOCK = ".db-health.lock"
LOG = "db-health.log"
TABLES = ("agent_messages", "conversation_log", "memories", "kanban_cards")
SEQ_TABLES = ("agent_messages", "conversation_log", "memories")
SIZE_DROP = 0.5          # a drop to below half of the last healthy size
ROWS_DROP = 0.5          # a core table losing more than half of its rows ...
ROWS_DROP_MIN = 10       # ... and at least this many rows
REMIND_SECONDS = 6 * 3600
RESCUE_REPEAT_SECONDS = 6 * 3600
COPY_ATTEMPTS = 10
RECHECK_DELAY = 2.0      # an open/read/integrity finding is re-checked once
RECHECK_CODES = {"db_open_failed", "integrity_failed", "tables_missing"}
TZ = ZoneInfo("Europe/Budapest")
STATE_VERSION = 1
# Findings that describe the CURRENT state of the file. The others are events
# (something went down since the last run) and do not "heal" by themselves.
STATE_CODES = {"db_missing", "db_not_regular", "db_empty", "db_open_failed",
               "tables_missing", "integrity_failed", "dashboard_ghost_db",
               "dashboard_ghost_wal", "dashboard_ghost_shm"}
GHOST_CODES = {"dashboard_ghost_db", "dashboard_ghost_wal", "dashboard_ghost_shm"}


class DeliveryError(Exception):
    """Carries only a fixed, secret-free code."""


def now_local(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ)


def stamp(ts):
    return now_local(ts).strftime("%Y-%m-%d %H:%M:%S %Z")


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, data):
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def read_state(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, UnicodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return {}
    return data


def env_values(root):
    """KEY=value pairs from the install's .env, parsed as data (never sourced)."""
    values = {}
    try:
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), value.strip().strip("\"'"))
    except (OSError, UnicodeError):
        pass
    return values


def install_settings(root, environ=os.environ):
    """Every install-specific value is resolved at runtime (portability contract)."""
    env = env_values(root)
    main = env.get("MAIN_AGENT_ID") or "marveen"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", main):
        main = "marveen"
    port = env.get("WEB_PORT") or "3420"
    if not re.fullmatch(r"[0-9]{1,5}", port):
        port = "3420"
    chat = environ.get("MARVEEN_ALERT_CHAT_ID") or env.get("ALLOWED_CHAT_ID") or ""
    if not re.fullmatch(r"-?[0-9]{1,20}", chat):
        chat = ""
    return {"main": main, "url": f"http://localhost:{port}", "chat": chat,
            "unit": environ.get("DB_HEALTH_DASHBOARD_UNIT") or f"{main}-dashboard.service"}


def int_or_none(value):
    return value if type(value) is int and value >= 0 else None


# ---------------------------------------------------------------- DB checks

def inspect_db(db_path, quick=False):
    """Read-only inspection. Returns (facts, findings). Never raises sqlite errors."""
    facts = {"exists": False}
    findings = []
    try:
        st = os.lstat(db_path)
    except FileNotFoundError:
        findings.append(("db_missing", "az adatbázis-fájl nem létezik az útvonalon"))
        return facts, findings
    except OSError:
        findings.append(("db_open_failed", "az adatbázis-fájl nem olvasható (stat hiba)"))
        return facts, findings
    facts.update(exists=True, size=st.st_size, inode=st.st_ino, dev=st.st_dev)
    if not stat.S_ISREG(st.st_mode):
        findings.append(("db_not_regular", "az útvonalon nem szabályos fájl van (pl. symlink vagy mappa)"))
        return facts, findings
    if st.st_size == 0:
        findings.append(("db_empty", "az adatbázis-fájl 0 bájtos"))
        return facts, findings
    db = None
    try:
        # mode=ro: never creates the DB, never writes it; WAL readers do not
        # block the dashboard's writer, and the live WAL must count, so no
        # immutable=1 while the dashboard runs. But with no -shm and no WAL
        # content (dashboard stopped, SQLite removed both on its last close) a
        # plain mode=ro open of a WAL-mode DB CREATES empty -wal/-shm files next
        # to it (PROGI 6582, measured). immutable=1 then reads the file as it is
        # and creates nothing; a dashboard starting in that very moment can at
        # worst cause a bogus finding, which the 2 s re-check resolves.
        try:
            wal_size = os.stat(db_path + "-wal").st_size
        except FileNotFoundError:
            wal_size = 0
        quiet = wal_size == 0 and not os.path.lexists(db_path + "-shm")
        facts["immutable"] = quiet
        db = sqlite3.connect(Path(db_path).as_uri() + ("?mode=ro&immutable=1" if quiet else "?mode=ro"),
                             uri=True, timeout=5, isolation_level=None)
        db.execute("PRAGMA query_only=ON")
        names = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in TABLES if t not in names]
        if missing:
            findings.append(("tables_missing", "hiányzó fő tábla: " + ", ".join(missing)))
        counts = {t: db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                  for t in TABLES if t in names}
        seq = {}
        if "sqlite_sequence" in names:
            for name, value in db.execute("SELECT name, seq FROM sqlite_sequence"):
                if name in SEQ_TABLES and type(value) is int:
                    seq[name] = value
        started = time.monotonic()
        pragma = "quick_check" if quick else "integrity_check"
        rows = db.execute(f"PRAGMA {pragma}").fetchmany(5)
        facts["check"] = pragma
        facts["check_ms"] = int((time.monotonic() - started) * 1000)
        if not (len(rows) == 1 and rows[0][0] == "ok"):
            findings.append(("integrity_failed", f"PRAGMA {pragma} hibát jelzett"))
        facts.update(opened=True, counts=counts, seq=seq)
    except sqlite3.Error:
        findings.append(("db_open_failed", "az adatbázis csak olvasva sem nyitható meg vagy nem olvasható"))
    finally:
        if db is not None:
            db.close()
    return facts, findings


def compare_with_baseline(facts, base):
    findings = []
    if not base or not facts.get("opened"):
        return findings
    size, old_size = facts.get("size"), int_or_none(base.get("size"))
    if old_size and size is not None and size < old_size * (1 - SIZE_DROP):
        findings.append(("size_drop", f"a fájlméret {old_size} bájtról {size} bájtra esett (több mint 50%)"))
    old_seq = base.get("seq") if isinstance(base.get("seq"), dict) else {}
    for table in SEQ_TABLES:
        old = int_or_none(old_seq.get(table))
        new = facts.get("seq", {}).get(table)
        if old is not None and (new is None or new < old):
            findings.append(("seq_decreased",
                             f"{table}: a legnagyobb kiosztott id {old} helyett most {new if new is not None else 'nincs'}"))
    old_counts = base.get("counts") if isinstance(base.get("counts"), dict) else {}
    for table in TABLES:
        old = int_or_none(old_counts.get(table))
        new = facts.get("counts", {}).get(table)
        if old is None or new is None:
            continue
        if old - new >= ROWS_DROP_MIN and new < old * (1 - ROWS_DROP):
            findings.append(("rows_dropped", f"{table}: a sorok száma {old} helyett {new}"))
    return findings


# --------------------------------------------------------- dashboard ghost

def dashboard_pid(explicit, unit):
    if explicit is not None:
        return explicit if explicit > 0 else None
    try:
        out = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", unit],
                             capture_output=True, text=True, timeout=10, check=False).stdout.strip()
        pid = int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return pid if pid > 0 else None


def dashboard_fds(pid, db_path):
    """{'db'|'wal'|'shm': [{fd, ino, dev, nlink, size, mtime_ns}]} for the DB family."""
    wanted = {db_path: "db", db_path + "-wal": "wal", db_path + "-shm": "shm"}
    result = {}
    base = f"/proc/{pid}/fd"
    try:
        entries = os.listdir(base)
    except OSError:
        return None
    for name in entries:
        link = f"{base}/{name}"
        try:
            target = os.readlink(link)
        except OSError:
            continue
        if target.endswith(" (deleted)"):
            target = target[: -len(" (deleted)")]
        kind = wanted.get(target)
        if kind is None:
            continue
        try:
            st = os.stat(link)  # follows the magic link to the open file itself
        except OSError:
            continue
        result.setdefault(kind, []).append({"fd": int(name), "ino": st.st_ino, "dev": st.st_dev,
                                            "nlink": st.st_nlink, "size": st.st_size,
                                            "mtime_ns": st.st_mtime_ns})
    return result


def ghost_findings(fds, db_path):
    findings, ghosts = [], {}
    labels = {"db": ("dashboard_ghost_db", "az adatbázist"),
              "wal": ("dashboard_ghost_wal", "a WAL-naplót"),
              "shm": ("dashboard_ghost_shm", "a megosztott memória-fájlt (-shm)")}
    for kind, suffix in (("db", ""), ("wal", "-wal"), ("shm", "-shm")):
        try:
            st = os.stat(db_path + suffix)
            here = (st.st_dev, st.st_ino)
        except OSError:
            here = None
        for entry in fds.get(kind, []):
            if entry["nlink"] == 0 or here != (entry["dev"], entry["ino"]):
                ghosts.setdefault(kind, entry)
        if kind in ghosts:
            code, what = labels[kind]
            findings.append((code, f"a futó dashboard {what} egy már törölt vagy lecserélt fájlba írja"))
    return findings, ghosts


# ----------------------------------------------------------------- rescue

def copy_fd(pid, fd, dest):
    with open(f"/proc/{pid}/fd/{fd}", "rb") as src:
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(out, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())


def fd_signature(pid, fd):
    st = os.stat(f"/proc/{pid}/fd/{fd}")
    return (st.st_size, st.st_mtime_ns)


def wal_header(pid, fd):
    """The 32-byte WAL header: checkpoint sequence + salts change on a WAL restart."""
    with open(f"/proc/{pid}/fd/{fd}", "rb") as stream:
        return stream.read(32)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def new_recovery_dir(store, ts):
    base = store / ("recovery-db-" + now_local(ts).strftime("%Y%m%d-%H%M%S"))
    path, n = base, 1
    while True:
        try:
            os.mkdir(path, 0o700)
            os.chmod(path, 0o700)
            return path
        except FileExistsError:
            n += 1
            path = Path(f"{base}-{n}")


def verify_copy(path):
    """Integrity + core tables of a standalone copy. Returns (ok, detail dict)."""
    db = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        rows = db.execute("PRAGMA integrity_check").fetchmany(5)
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {t: db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in TABLES if t in names}
        seq = {n: v for n, v in db.execute("SELECT name, seq FROM sqlite_sequence")
               if n in SEQ_TABLES} if "sqlite_sequence" in names else {}
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        db.close()
    ok = len(rows) == 1 and rows[0][0] == "ok" and all(t in counts for t in TABLES)
    return ok, {"integrity": "ok" if len(rows) == 1 and rows[0][0] == "ok" else "hiba",
                "counts": counts, "seq": seq, "journal_mode": mode}


def rescue(store, pid, ghosts, fds, ts):
    """Copy the dashboard's open DB family, then build a verified WAL-free copy."""
    db_entry = ghosts.get("db") or (fds.get("db") or [None])[0]
    wal_entry = ghosts.get("wal") or (fds.get("wal") or [None])[0]
    shm_entry = ghosts.get("shm") or (fds.get("shm") or [None])[0]
    if db_entry is None:
        return {"ok": False, "reason": "no_db_fd"}
    need = db_entry["size"] + (wal_entry["size"] if wal_entry else 0)
    free = shutil.disk_usage(store).free
    if free < 3 * need + 256 * 1024 * 1024:
        return {"ok": False, "reason": "no_space", "need": need, "free": free}
    target = new_recovery_dir(store, ts)
    raw = target / "raw"
    os.mkdir(raw, 0o700)
    # WAL first, then the DB. In WAL mode only a checkpoint writes the DB file,
    # so if the DB file (size, mtime) AND the WAL header (checkpoint sequence,
    # salts) are the same before the WAL copy and after the DB copy, the pair
    # is one consistent state: frames appended meanwhile only make the copy a
    # little older (SQLite stops at the last intact commit frame). A checkpoint
    # during the window could put pages NEWER than the copied WAL into the DB
    # copy, so such an attempt is retried. Measured on the live DB 2026-11-28:
    # no checkpoint in 2 minutes, so the first attempt normally succeeds.
    family = [("wal", wal_entry, DB_NAME + "-wal"), ("db", db_entry, DB_NAME),
              ("shm", shm_entry, DB_NAME + "-shm")]

    def marker():
        return (wal_header(pid, wal_entry["fd"]) if wal_entry else b"",
                fd_signature(pid, db_entry["fd"]))

    consistent = False
    for attempt in range(1, COPY_ATTEMPTS + 1):
        for child in raw.iterdir():
            child.unlink()
        before = marker()
        for _, entry, name in family[:2]:
            if entry:
                copy_fd(pid, entry["fd"], raw / name)
        if before == marker():
            consistent = True
            break
        time.sleep(0.1 * attempt)
    if shm_entry:
        copy_fd(pid, shm_entry["fd"], raw / family[2][2])  # forensic only, never replayed
    manifest = {"created_at": stamp(ts), "source_pid": pid, "attempts": attempt,
                "consistent_copy": consistent, "raw": {}, "note":
                "raw/ = a dashboard altal nyitva tartott fajlok byte-masolata (db+wal egyutt "
                "hasznalando); claudeclaw.verified.db = WAL-mentes, ellenorzott masolat."}
    for kind, entry, name in family:
        if entry:
            manifest["raw"][name] = {"fd": entry["fd"], "inode": entry["ino"], "nlink": entry["nlink"],
                                     "bytes": (raw / name).stat().st_size, "sha256": sha256(raw / name)}
    verified = target / "claudeclaw.verified.db"
    work = Path(tempfile.mkdtemp(prefix="work-", dir=target))
    try:
        for _, entry, name in family[:2]:
            if entry:
                shutil.copyfile(raw / name, work / name)  # raw/ itself stays untouched
        src = sqlite3.connect(str(work / DB_NAME), isolation_level=None)  # replays the WAL copy
        try:
            out = os.open(verified, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(out)
            dst = sqlite3.connect(str(verified), isolation_level=None)
            try:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        finally:
            src.close()
        ok, detail = verify_copy(verified)
    except (sqlite3.Error, OSError):
        ok, detail = False, {"integrity": "nem ellenorizheto"}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    manifest["verified"] = {"ok": ok, **detail}
    if verified.exists():
        manifest["verified"]["sha256"] = sha256(verified)
    atomic_json(target / "MANIFEST.json", manifest)
    os.chmod(target / "MANIFEST.json", 0o600)
    return {"ok": ok, "dir": str(target), "consistent_copy": consistent, "verified": detail}


# --------------------------------------------------------------- delivery

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self, root, store, dashboard_url, chat_id, main_agent):
        self.root, self.store, self.url, self.chat_id = root, store, dashboard_url, chat_id
        self.main = main_agent
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _post(self, url, payload, headers):
        req = urllib.request.Request(url, json.dumps(payload, ensure_ascii=False).encode(),
                                     {"Content-Type": "application/json", **headers}, method="POST")
        try:
            with self.opener.open(req, timeout=10) as response:
                code, raw = response.status, response.read(65537)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise DeliveryError(f"http_{exc.code}") from None
        except Exception:
            raise DeliveryError("network_failure") from None
        if not 200 <= code < 300:
            raise DeliveryError(f"http_{code}")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise DeliveryError("invalid_response") from None

    def send_agent(self, content):
        try:
            token = (self.store / ".dashboard-token").read_text(encoding="utf-8").strip()
            if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ValueError()
        except (OSError, ValueError, UnicodeError):
            raise DeliveryError("dashboard_credentials_unavailable") from None
        # The main agent id is the existing system sender (kanban-audit,
        # usage-fleet-monitor): POST /api/messages 403s unregistered from ids.
        data = self._post(self.url.rstrip("/") + "/api/messages",
                          {"from": self.main, "to": self.main, "content": content},
                          {"Authorization": "Bearer " + token})
        receipt = data.get("id") if isinstance(data, dict) else None
        if not ((type(receipt) is int and receipt > 0) or (isinstance(receipt, str) and receipt)):
            raise DeliveryError("missing_message_receipt")
        return "agent"

    def send_telegram(self, content):
        token = None
        for env in (self.root / ".env", Path.home() / ".claude/channels/telegram/.env"):
            try:
                for line in env.read_text(encoding="utf-8").splitlines():
                    if line.startswith("TELEGRAM_BOT_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip("\"'")
                        break
            except (OSError, UnicodeError):
                continue
            if token:
                break
        if not token or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise DeliveryError("telegram_credentials_unavailable")
        if not self.chat_id:
            raise DeliveryError("telegram_chat_unavailable")
        data = self._post(f"https://api.telegram.org/bot{token}/sendMessage",
                          {"chat_id": self.chat_id, "text": content}, {})
        if not (isinstance(data, dict) and data.get("ok") is True):
            raise DeliveryError("telegram_rejected")
        return "telegram"


def deliver(transport, content):
    """Agent API first, Telegram only when the API did not accept. Returns (channel, codes)."""
    codes = []
    for method in (transport.send_agent, transport.send_telegram):
        try:
            return method(content), codes
        except DeliveryError as exc:
            codes.append(str(exc))
        except Exception:
            codes.append("delivery_failure")
    return None, codes


# ---------------------------------------------------------------- message

def alert_text(findings, db_path, rescue_result, pid, backups, ts, dashboard_seen):
    lines = [f"[DB-HEALTH] HIBA az éles adatbázisnál ({stamp(ts)}).", f"Fájl: {db_path}"]
    for _, text in findings:
        lines.append(f"- {text}")
    if rescue_result:
        if rescue_result.get("dir"):
            v = rescue_result.get("verified", {})
            counts = v.get("counts", {})
            lines.append(f"Kimentve: {rescue_result['dir']} (raw/: db+wal együtt; claudeclaw.verified.db: "
                         f"WAL-mentes másolat, integrity_check: {v.get('integrity', '?')}).")
            if counts:
                lines.append("Kimentett sorok: " + ", ".join(f"{k} {counts[k]}" for k in sorted(counts)))
            if not rescue_result.get("consistent_copy"):
                lines.append("Figyelem: minden másolási kísérlet alatt checkpoint futott, a nyers db+wal pár "
                             "nem garantáltan egy pillanat állapota; a verified példány ellenőrzése a mérvadó, "
                             "friss kimentés: db-health.py --force-rescue.")
        else:
            lines.append(f"Kimentés NEM sikerült ({rescue_result.get('reason', 'ismeretlen ok')}); "
                         "óránként újrapróbálom.")
            if rescue_result.get("earlier_dir"):
                lines.append(f"Korábbi kimentés ugyanerről a példányról: {rescue_result['earlier_dir']}")
    elif not dashboard_seen:
        lines.append("A dashboard folyamata nem fut vagy nem olvasható, nyitott példányból nincs mit kimenteni.")
    lines.append(f"Utolsó napi mentések: {backups}")
    lines.append("Az éles útvonalat nem írtam át, a dashboardot nem indítottam újra. A helyreállítás a te döntésed.")
    return "\n".join(lines)[:3500]


def recovered_text(previous_codes, ts):
    text = f"[DB-HEALTH] Az éles adatbázis ellenőrzése újra rendben ({stamp(ts)})."
    if any(c not in STATE_CODES for c in previous_codes):
        text += (" Az előző riasztás egy csökkenés-esemény volt (" + ", ".join(sorted(previous_codes)) +
                 "), ez magától nem szűnik meg: nézd meg, szándékos volt-e.")
    return text


# ------------------------------------------------------------------- main

class Runner:
    def __init__(self, args, transport=None, clock=time.time):
        self.args = args
        self.store = Path(args.store).resolve()
        self.db_path = os.path.join(os.path.realpath(self.store), DB_NAME) if args.db is None \
            else os.path.abspath(args.db)
        self.clock = clock
        self.transport = transport or Transport(ROOT, self.store, args.dashboard_url, args.chat_id,
                                                args.main_agent)

    def log(self, entry):
        if self.args.dry_run:
            print(json.dumps(entry, ensure_ascii=False, sort_keys=True))
            return
        directory = self.store / "logs"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / LOG
        try:
            if path.stat().st_size > 1024 * 1024:
                os.replace(path, path.with_suffix(".log.1"))
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def run(self):
        if self.args.dry_run:
            # Dry run writes nothing at all, not even the lock file.
            return self._run_locked()
        lock_path = self.store / LOCK
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.log({"at": stamp(self.clock()), "result": "busy"})
                return 0
            return self._run_locked()
        finally:
            os.close(fd)

    def _run_locked(self):
        ts = self.clock()
        state_path = self.store / STATE
        state = read_state(state_path)
        base = state.get("baseline") if isinstance(state.get("baseline"), dict) else {}
        facts, findings = inspect_db(self.db_path, quick=self.args.quick)
        if any(code in RECHECK_CODES for code, _ in findings):
            # A transient busy/IO error must not page anyone: the second look decides.
            time.sleep(RECHECK_DELAY)
            facts, findings = inspect_db(self.db_path, quick=self.args.quick)
        findings += compare_with_baseline(facts, base)

        pid = dashboard_pid(self.args.dashboard_pid, self.args.unit)
        fds = dashboard_fds(pid, self.db_path) if pid else None
        ghosts = {}
        if fds is not None:
            gf, ghosts = ghost_findings(fds, self.db_path)
            findings += gf

        rescue_result = None
        prev_rescue = state.get("rescue") if isinstance(state.get("rescue"), dict) else {}
        ghost_key = None
        if ghosts:
            entry = ghosts.get("db") or ghosts.get("wal") or ghosts.get("shm")
            ghost_key = f"{pid}:{entry['dev']}:{entry['ino']}"
            repeat_due = (prev_rescue.get("key") != ghost_key
                          or ts - (prev_rescue.get("at") or 0) >= RESCUE_REPEAT_SECONDS
                          or not Path(prev_rescue.get("dir") or "/nonexistent").is_dir())
            if not self.args.dry_run and (self.args.force_rescue or repeat_due):
                try:
                    rescue_result = rescue(self.store, pid, ghosts, fds, ts)
                except OSError:
                    rescue_result = {"ok": False, "reason": "copy_failed"}
                if rescue_result.get("dir"):
                    prev_rescue = {"key": ghost_key, "at": int(ts), "dir": rescue_result["dir"]}
                elif prev_rescue.get("key") == ghost_key and prev_rescue.get("dir"):
                    rescue_result["earlier_dir"] = prev_rescue["dir"]
            elif prev_rescue.get("key") == ghost_key:
                rescue_result = {"dir": prev_rescue.get("dir"), "verified": {"integrity": "lásd MANIFEST.json"},
                                 "consistent_copy": True, "earlier": True}

        codes = sorted({c for c, _ in findings})
        fingerprint = ",".join(codes) + (f"|{ghost_key}" if ghost_key else "")
        if rescue_result and not rescue_result.get("dir"):
            fingerprint += f"|rescue_failed:{rescue_result.get('reason', 'unknown')}"
        prev_alert = state.get("alert") if isinstance(state.get("alert"), dict) else {}
        exit_code, delivered, delivery_codes = 0, None, []

        if findings:
            due = (prev_alert.get("fingerprint") != fingerprint
                   or ts - (prev_alert.get("sent_at") or 0) >= REMIND_SECONDS
                   or bool(rescue_result and rescue_result.get("dir") and not rescue_result.get("earlier")))
            if due and not self.args.dry_run:
                text = alert_text(findings, self.db_path, rescue_result, pid,
                                  str(ROOT / "backups"), ts, fds is not None)
                delivered, delivery_codes = deliver(self.transport, text)
                if delivered:
                    prev_alert = {"fingerprint": fingerprint, "codes": codes, "sent_at": int(ts)}
                else:
                    exit_code = 1
        elif prev_alert.get("codes") and not self.args.dry_run:
            delivered, delivery_codes = deliver(self.transport, recovered_text(prev_alert["codes"], ts))
            if delivered:
                prev_alert = {}
            else:
                exit_code = 1

        # The baseline moves only on a run whose file is readable and not in a
        # broken STATE (an emptied/new file must never become the reference).
        # Event findings (a drop) move it, so one drop is announced once.
        if facts.get("opened") and not any(c in STATE_CODES for c in codes):
            base = {"size": facts["size"], "inode": facts["inode"], "seq": facts.get("seq", {}),
                    "counts": facts.get("counts", {}), "at": int(ts)}

        entry = {"at": stamp(ts), "result": "ok" if not findings else "problem", "codes": codes,
                 "size": facts.get("size"), "check_ms": facts.get("check_ms"),
                 "immutable": facts.get("immutable"),
                 "dashboard_pid": pid, "dashboard_fds_seen": fds is not None,
                 "dashboard_db_fds": sum(len(v) for v in fds.values()) if fds else 0}
        if rescue_result:
            entry["rescue"] = {k: rescue_result.get(k) for k in ("ok", "dir", "reason", "consistent_copy", "earlier")}
        if delivered or delivery_codes:
            entry["delivered"] = delivered
            entry["delivery_codes"] = delivery_codes
        self.log(entry)
        if not self.args.dry_run:
            atomic_json(state_path, {"version": STATE_VERSION, "checked_at": int(ts),
                                     "baseline": base, "alert": prev_alert, "rescue": prev_rescue})
            os.chmod(state_path, 0o600)
        return exit_code


def parse_args(argv):
    cfg = install_settings(ROOT)
    p = argparse.ArgumentParser(description="Hourly read-only health check of store/claudeclaw.db")
    p.add_argument("--store", default=str(ROOT / "store"), help="store directory (state, lock, logs, recovery dirs)")
    p.add_argument("--db", default=None, help="database path (default: <store>/claudeclaw.db)")
    p.add_argument("--unit", default=cfg["unit"], help="systemd --user unit of the dashboard "
                   "(default <MAIN_AGENT_ID>-dashboard.service or DB_HEALTH_DASHBOARD_UNIT)")
    p.add_argument("--dashboard-pid", type=int, default=None, help="dashboard PID override (0 = none)")
    p.add_argument("--dashboard-url", default=cfg["url"], help="default http://localhost:<WEB_PORT>")
    p.add_argument("--main-agent", default=cfg["main"], help="alert sender/recipient (.env MAIN_AGENT_ID)")
    p.add_argument("--chat-id", default=cfg["chat"],
                   help="Telegram fallback chat (MARVEEN_ALERT_CHAT_ID, else .env ALLOWED_CHAT_ID)")
    p.add_argument("--quick", action="store_true", help="PRAGMA quick_check instead of integrity_check")
    p.add_argument("--dry-run", action="store_true",
                   help="print the result line; no state, no rescue, no alert")
    p.add_argument("--force-rescue", action="store_true",
                   help="take a fresh rescue copy now if the dashboard holds a ghost DB "
                        "(ignored with --dry-run, which never writes)")
    args = p.parse_args(argv)
    if args.chat_id and not re.fullmatch(r"-?[0-9]{1,20}", args.chat_id):
        p.error("invalid --chat-id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.main_agent):
        p.error("invalid --main-agent")
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return Runner(args).run()
    except Exception as exc:  # never print exception text: it may hold paths/tokens
        print(f"db-health: internal error ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

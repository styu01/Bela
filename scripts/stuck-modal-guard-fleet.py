#!/usr/bin/env python3
"""Implementation of stuck-modal-guard.sh fleet, not a second live guard.

Default shadow mode only captures and audits; no key or notification effects.
Pane text is untrusted. Only an exact, unique stop option can produce an ASCII
digit. Never pass pane text to a command, log, notification, shell or model.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The fleet list has one source of truth. --sessions overrides it as a whole.
SESSIONS = tuple("agent-" + name for name in
                 ("progi", "okoska", "kisprogi", "iris", "vizsla", "autobot", "deepseek"))
STATE_FILE = ".stuck-menu-guard-state.json"
LOCK_FILE = ".stuck-menu-guard.lock"
KEY_COOLDOWN = 600
ALERT_COOLDOWN = 7200
CHAT_ID = "6209177290"
RESUME_TEXT = "Folytasd onnan ahol elakadtál."
STOP = "Stop and wait for limit to reset"
UPGRADE = "Upgrade your plan"
QUESTION = "What do you want to do?"
OPTION = re.compile(r"^\s*(?:[❯›]\s*)?([0-9]+)[.)]\s+(.+?)\s*$")
COLORS = re.compile(r"\x1b\[[0-9;]*m")
FOOTER = re.compile(r"(?:Enter to (?:select|confirm)|Esc to (?:cancel|go back)|"
                    r"Use (?:the )?arrow keys|[↑↓↵/ ]+to (?:navigate|select|move))", re.I)
CURSOR_OPTION = re.compile(r"^\s*[❯›]\s*[0-9]+[.)]\s+")
LIMIT_HEADER = re.compile(r"\bsession limit\b|\bhit your\b", re.I)
# PROGI 6026 (55288137 c) / BELA 6054: the healthy shapes follow the bash guard's classify_pane (the primary
# reader of a live footer for recovery). Measured 2026-11-27 on the fleet's 80-column 2.1.326 panes: the live
# idle footer is "⏵⏵ bypass permissions on (shift+tab to cycle) · install gh for PR status", which the old
# end-anchored mode pattern did not take, nor a turn past one minute "(2m 39s ·" or the clipped "· esc to inte…".
# Healthy here only ever means: no keys (the strict stop-menu recognizer below needs a pane with no live footer
# anyway), so a wider healthy set is the safe direction.
HEALTHY = {
    "busy": re.compile(r"esc to interrupt|\((?:[0-9]+h ?)?(?:[0-9]+m ?)?[0-9]+s [·.]|"
                       r"· e(?:s(?:c(?: (?:t(?:o(?: (?:i[a-z]*)?)?)?)?)?)?)?…", re.I),
    "shortcuts": re.compile(r"^\s*\? for shortcuts(?:\s+.*)?$", re.I),
    **{mode.replace(" ", "_").replace("'", ""): re.compile(
        r"^\s*[⏵⏸▶»]*\s*" + re.escape(mode) + r"(?:\s+\([^\r\n)]*\))?(?:\s+·\s+[^\r\n]*)?\s*$", re.I)
       for mode in ("bypass permissions on", "accept edits on", "plan mode on", "auto mode on",
                    "don't ask on", "manual mode on")},
}


class GuardError(Exception):
    """Never include arbitrary exception text in user output or audit."""


def finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def valid_session(value):
    return isinstance(value, str) and re.fullmatch(r"agent-[a-z][a-z0-9_-]{0,47}", value) is not None


def navigation_footer(line):
    parts = re.split(r"\s*[·,]\s*", line.strip())
    return bool(parts) and all(FOOTER.fullmatch(part) for part in parts)


def classify(pane):
    """Pure last-15-line recognizer; returned values never contain pane text."""
    # Only LF is a terminal line separator here. splitlines() would silently
    # turn VT/NEL/Unicode line separators into a forged menu layout.
    text = "\n".join(line.removesuffix("\r") for line in pane.split("\n")[-15:])
    text = COLORS.sub("", text)
    lines = text.split("\n")
    options = [(i, match.group(1), " ".join(match.group(2).split()))
               for i, line in enumerate(lines) if (match := OPTION.fullmatch(line))]
    questions = [i for i, line in enumerate(lines) if line.strip() == QUESTION]
    hints = [line.strip().strip("│┃║").strip() for line in lines]
    has_ui_footer = any(navigation_footer(line) for line in hints if line)
    has_prompt = any(re.fullmatch(r"(?:What .+\?|Do you .+\?|(?:Choose|Select) .+[:?]|Manage MCP servers)",
                                 line, re.I) for line in hints)
    hint_options = [line for line in hints if OPTION.fullmatch(line)]
    looks_menu = bool(questions or has_ui_footer or (options and has_prompt) or
                      (has_prompt and hint_options) or re.search(r"\[[yYnN]/[yYnN]\]", text) or
                      any(line in (STOP, UPGRADE) for line in hints) or
                      (QUESTION in text and STOP in text) or
                      (len(options) >= 2 and any("❯" in line or "›" in line for line in lines)) or
                      any(label in (STOP, UPGRADE) for _, _, label in options))
    nonempty = [line for line in lines if line.strip()]
    healthy_types = sorted({kind for kind, pattern in HEALTHY.items()
                            if any(pattern.search(line) for line in nonempty[-2:])})
    question = questions[0] if questions else len(lines)
    cursor_option = any(CURSOR_OPTION.match(lines[i]) for i, _, _ in options if i > question)
    header_before = any(LIMIT_HEADER.search(line) for line in lines[:question]) if questions else False
    fingerprint = {
        "line_count": len(lines), "cursor_option": any(CURSOR_OPTION.match(lines[i]) for i, _, _ in options),
        "cursor_in_menu": cursor_option, "header_line": any(LIMIT_HEADER.search(line) for line in lines),
        "header_before_question": header_before, "question_count": len(questions),
        "options": [{"number": n if len(n) <= 2 else "multi_digit",
                     "type": "stop" if label == STOP else "upgrade" if label == UPGRADE else "other",
                     "cursor": bool(CURSOR_OPTION.match(lines[i]))} for i, n, label in options],
        "healthy_footer": bool(healthy_types), "healthy_footer_types": healthy_types,
        "pane_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    }
    def outcome(category, key, reason):
        return {"class": category, "key": key, "reason": reason, "fingerprint": fingerprint,
                "menu_markers_with_healthy_footer": bool(looks_menu and healthy_types)}
    result = outcome("unknown_menu" if looks_menu else "none", None,
                     "unrecognized_menu" if looks_menu else "no_menu")
    if healthy_types:
        # A live-looking menu sharing a permission/busy footer must not turn
        # into a silent false negative. It needs manual review, never keys.
        return outcome("unknown_menu" if looks_menu else "none", None,
                       "healthy_footer_with_menu_markers" if looks_menu else "healthy_footer")
    # Quoted code and terminal cursor/control sequences are not a live menu.
    if ("```" in text or "~~~" in text or "\x1b" in text or
            any((ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) < 160
                or char in "\u2028\u2029" for char in text)):
        return result
    if len(questions) != 1:
        return result
    question = questions[0]
    if not cursor_option and not header_before:
        return outcome("unknown_menu", None, "missing_selection_evidence")
    menu = [(n, label) for i, n, label in options if i > question]
    # Every nonblank line after the question must be an exact known option
    # or navigation footer. A changed label/number/extra instruction is unknown.
    if not menu or any(line.strip() and not OPTION.fullmatch(line)
                       and not navigation_footer(line) for line in lines[question + 1:]):
        return result
    if (any(label not in (STOP, UPGRADE) or not re.fullmatch(r"[1-9]", n) for n, label in menu)
            or len({n for n, _ in menu}) != len(menu)
            or len([1 for _, label in menu if label == STOP]) != 1
            or len([1 for _, label in menu if label == UPGRADE]) > 1):
        return result
    key = next(n for n, label in menu if label == STOP)
    # Multi-digit shortcuts are ambiguous in single-key TUIs. Alert instead.
    if not re.fullmatch(r"[1-9]", key):
        return result
    return outcome("known_session_limit", key, "unique_stop_option")


def decision_signature(observed):
    """Ignore unrelated pane/hash changes when checking a second capture."""
    return (observed["class"], observed["key"],
            tuple((item["number"], item["type"], item["cursor"])
                  for item in observed["fingerprint"]["options"]))


def record_error(record, code):
    codes = record.setdefault("error_codes", [])
    for item in (record.get("error"), code):
        if item and item not in codes:
            codes.append(item)
    record.setdefault("error", code)


def atomic_state(path, state):
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def load_state(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "windows": {}}
    except (ValueError, UnicodeError):
        raise GuardError("invalid_state") from None
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or value["version"] != 1 or not isinstance(value.get("windows"), dict)):
        raise GuardError("invalid_state")
    for session, entry in value["windows"].items():
        if not valid_session(session) or not isinstance(entry, dict):
            raise GuardError("invalid_state")
        for field in ("last_key_at", "last_unknown_alert_at"):
            if field in entry and (not finite(entry[field]) or entry[field] < 0):
                raise GuardError("invalid_state")
        recovery = entry.get("recovery")
        if recovery is not None and (not isinstance(recovery, dict)
                or recovery.get("phase") not in ("reserved", "notify_pending")
                or not isinstance(recovery.get("pane_id"), str)
                or not re.fullmatch(r"%[0-9]+", recovery["pane_id"])
                or not isinstance(recovery.get("pane_pid"), str)
                or not re.fullmatch(r"[0-9]+", recovery["pane_pid"])):
            raise GuardError("invalid_state")
    return value


def due(last, now, interval):
    # A backward clock jump holds the cooldown instead of creating spam.
    return last is None or now - last >= interval


class Tmux:
    def command(self, args):
        try:
            return subprocess.run(["tmux", *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise GuardError("tmux_unavailable") from None

    def inspect(self, session):
        if not valid_session(session):
            raise GuardError("invalid_session")
        fmt = "#{pane_id}|#{pane_pid}|#{pane_current_command}|#{pane_in_mode}|#{pane_dead}|#{session_name}"
        proc = self.command(["display-message", "-p", "-t", f"={session}:", fmt])
        if proc.returncode:
            if re.search(r"can't find session:|no server running|no sessions|"
                         r"error connecting to .+\(No such file or directory\)", proc.stderr):
                return None
            raise GuardError("tmux_inspect_failed")
        fields = [field.strip() for field in proc.stdout.strip().split("|")]
        # tmux 3.6 can return rc=0 with six empty format fields for a missing
        # exact target. This is absence, not malformed live-pane metadata.
        if not proc.stdout.strip() or (len(fields) == 6 and not any(fields)):
            return None
        if (len(fields) != 6 or not re.fullmatch(r"%[0-9]+", fields[0])
                or not re.fullmatch(r"[0-9]+", fields[1]) or fields[3] not in ("0", "1")
                or fields[4] not in ("0", "1") or fields[5] != session):
            raise GuardError("tmux_invalid_target")
        return dict(zip(("pane_id", "pane_pid", "command", "in_mode", "dead", "session"), fields))

    def capture(self, pane_id):
        if not re.fullmatch(r"%[0-9]+", pane_id):
            raise GuardError("tmux_invalid_target")
        proc = self.command(["capture-pane", "-t", pane_id, "-p"])
        if proc.returncode or len(proc.stdout) > 128 * 1024:
            raise GuardError("tmux_capture_failed")
        return "\n".join(proc.stdout.split("\n")[-15:])

    def send_stop(self, pane_id, key):
        if not re.fullmatch(r"%[0-9]+", pane_id) or not re.fullmatch(r"[1-9]", key):
            raise GuardError("invalid_stop_key")
        # Exactly one validated digit plus Enter, one tmux command, no shell.
        proc = self.command(["send-keys", "-t", pane_id, key, "Enter"])
        if proc.returncode:
            raise GuardError("tmux_key_failed")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self, store, root=ROOT):
        self.store, self.root = Path(store), Path(root)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def post(self, url, payload, headers=None, recipient=None):
        request = urllib.request.Request(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    {"Content-Type": "application/json", **(headers or {})}, method="POST")
        try:
            try:
                with self.opener.open(request, timeout=10) as response:
                    code, raw = response.status, response.read(65537)
            except urllib.error.HTTPError as exc:
                try:
                    code, raw = exc.code, exc.read(65537)
                finally:
                    exc.close()
        except Exception:
            raise GuardError("notification_failed") from None
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise GuardError("notification_failed") from None
        if not isinstance(data, dict):
            raise GuardError("notification_failed")
        if recipient and code in (400, 404, 409, 422, 503):
            # Defensive compatibility only: the real POST /api/messages queues
            # with 200 + pending id even when the target session is absent.
            error = data.get("error")
            if (data.get("code") in ("agent_not_running", "AGENT_NOT_RUNNING", "recipient_not_running")
                or (isinstance(error, str) and re.fullmatch(
                    r"(?:Agent|Recipient|Target agent) '?" + re.escape(recipient) + r"'? (?:is )?not running\.?",
                    error, re.I))):
                return "inactive"
        if not 200 <= code < 300 or data.get("error"):
            raise GuardError("notification_failed")
        if recipient:
            if not data.get("id"):
                raise GuardError("notification_failed")
        elif (data.get("ok") is not True or not isinstance(data.get("result"), dict)
              or not data["result"].get("message_id")):
            raise GuardError("notification_failed")
        return "sent"

    def send_agent(self, recipient):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", recipient):
            raise GuardError("invalid_recipient")
        try:
            token = (self.store / ".dashboard-token").read_text(encoding="utf-8").strip()
            if not token or "\r" in token or "\n" in token:
                raise ValueError()
        except (OSError, ValueError):
            raise GuardError("credentials_unavailable") from None
        return self.post("http://localhost:3420/api/messages",
                         {"from": "bela", "to": recipient, "content": RESUME_TEXT},
                         {"Authorization": "Bearer " + token}, recipient=recipient)

    def send_unknown(self, session):
        if not valid_session(session):
            raise GuardError("invalid_session")
        try:
            token = None
            for line in (self.root / ".env").read_text(encoding="utf-8").splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip("\"'")
                    break
            if not token or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
                raise ValueError()
        except (OSError, ValueError):
            raise GuardError("credentials_unavailable") from None
        text = (f"Ismeretlen interaktív menü látszik a(z) {session} ablakban. "
                "Nem nyomtam billentyűt. Kérlek, ellenőrizd kézzel.")
        return self.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         {"chat_id": CHAT_ID, "text": text})


class FleetGuard:
    def __init__(self, store, now, mode="shadow", dry=False, sessions=SESSIONS, tmux=None, transport=None):
        if mode not in ("shadow", "active") or not finite(now) or now < 0:
            raise GuardError("invalid_config")
        if not sessions or len(set(sessions)) != len(sessions) or not all(map(valid_session, sessions)):
            raise GuardError("invalid_config")
        self.store, self.now, self.mode, self.dry = Path(store), now, mode, dry
        self.sessions = tuple(sessions)
        self.tmux, self.transport = tmux or Tmux(), transport or Transport(store)
        self.state = {"version": 1, "windows": {}}
        self.state_unavailable = False

    def save(self):
        try:
            atomic_state(self.store / STATE_FILE, self.state)
        except (OSError, ValueError, TypeError):
            raise GuardError("state_write_failed") from None

    def reload_after_failure(self):
        # Never let a failed window's uncommitted in-memory changes get saved
        # accidentally as part of a subsequent window's state transaction.
        try:
            self.state = load_state(self.store / STATE_FILE)
        except (GuardError, OSError):
            self.state_unavailable = True

    def audit(self, record):
        if self.dry:
            return
        directory = self.store / "logs"
        directory.mkdir(exist_ok=True)
        path = directory / ("stuck-menu-guard-shadow.jsonl" if self.mode == "shadow" else "stuck-menu-guard.jsonl")
        if path.exists() and path.stat().st_size >= 4 * 1024 * 1024:
            os.replace(path, path.with_suffix(".jsonl.1"))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("audit_not_regular")
            payload = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            try:
                if os.write(fd, payload) != len(payload):
                    raise OSError("audit_short_write")
            except Exception:
                # Under the fleet lock no sibling guard appends concurrently.
                # Roll back this failed row so later windows cannot append to
                # a half-written JSON object after ENOSPC/EFBIG/short write.
                try:
                    os.ftruncate(fd, info.st_size)
                except OSError:
                    pass
                raise
        finally:
            os.close(fd)

    def notify_resume(self, session, entry, record):
        try:
            status = self.transport.send_agent(session.removeprefix("agent-"))
            if status not in ("sent", "inactive"):
                raise GuardError("notification_failed")
        except Exception:
            record["actions"].append("resume_failed")
            record_error(record, "notification_failed")
            return
        record["actions"].append("resume_api_accepted" if status == "sent" else "resume_compat_inactive")
        entry.pop("recovery", None)
        self.save()

    def new_record(self, session):
        return {"at": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                "window": session, "mode": self.mode, "class": "none", "would_do": "none", "actions": [],
                "fingerprint": None, "menu_markers_with_healthy_footer": False}

    def scan_window(self, session, record=None):
        record = self.new_record(session) if record is None else record
        target = self.tmux.inspect(session)
        if target is None:
            record["reason"] = "session_absent"
            return record
        observed = classify(self.tmux.capture(target["pane_id"]))
        record.update(observed)
        # Never type a digit into a shell, copy mode or a dead/reused pane.
        eligible = (target["command"] in ("claude", "claude.exe", "node")
                    and target["in_mode"] == "0" and target["dead"] == "0")
        if observed["class"] == "known_session_limit" and not eligible:
            record.update({"class": "unknown_menu", "key": None, "reason": "unsafe_target"})
        if self.state_unavailable:
            record["would_do"] = "blocked_state_unavailable"
            if self.mode == "active":
                record_error(record, "state_unavailable")
            return record
        entry = self.state["windows"].get(session, {})
        recovery = entry.get("recovery")
        if recovery and recovery["phase"] == "notify_pending":
            same = all(recovery[k] == target[k] for k in ("pane_id", "pane_pid"))
            record["would_do"] = "retry_resume" if same else "discard_obsolete_resume"
            if self.mode == "active" and not self.dry:
                if same:
                    self.notify_resume(session, entry, record)
                else:
                    entry.pop("recovery", None)
                    self.save()
                    record["actions"].append("obsolete_resume_discarded")
            return record
        if record["class"] == "known_session_limit":
            record["would_do"] = "stop_key_then_resume" if due(entry.get("last_key_at"), self.now, KEY_COOLDOWN) else "key_cooldown"
            if self.mode != "active" or self.dry or record["would_do"] != "stop_key_then_resume":
                return record
            # Resolve the exact active pane again and recapture immediately
            # before input. Changed session/pid/menu/selection means no action.
            if (self.tmux.inspect(session) != target or decision_signature(
                    classify(self.tmux.capture(target["pane_id"]))) != decision_signature(observed)):
                record["actions"].append("changed_before_key")
                return record
            self.state["windows"][session] = entry
            entry.update({"last_key_at": self.now, "recovery": {
                "pane_id": target["pane_id"], "pane_pid": target["pane_pid"], "phase": "reserved"}})
            self.save()  # Persist rate reservation BEFORE the external effect.
            try:
                self.tmux.send_stop(target["pane_id"], observed["key"])
            except Exception:
                record_error(record, "tmux_key_failed")
                record["actions"].append("key_attempt_uncertain")
                return record
            record["actions"].append("stop_key_sent")
            entry["recovery"]["phase"] = "notify_pending"
            self.save()
            self.notify_resume(session, entry, record)
        elif record["class"] == "unknown_menu":
            record["would_do"] = "alert_owner" if due(entry.get("last_unknown_alert_at"), self.now, ALERT_COOLDOWN) else "alert_cooldown"
            if self.mode != "active" or self.dry or record["would_do"] != "alert_owner":
                return record
            self.state["windows"][session] = entry
            entry["last_unknown_alert_at"] = self.now
            # Reserve before send, including ambiguous timeout failures: hard
            # maximum one attempt per window per 2h is safer than alert spam.
            self.save()
            try:
                if self.transport.send_unknown(session) != "sent":
                    raise GuardError("notification_failed")
                record["actions"].append("owner_alert_sent")
            except Exception:
                record_error(record, "notification_failed")
                record["actions"].append("owner_alert_failed")
        return record

    def scan(self):
        self.state_unavailable = False
        try:
            self.state = load_state(self.store / STATE_FILE)
        except (GuardError, OSError):
            if self.mode == "active":
                raise GuardError("state_unavailable") from None
            # Shadow is observational only; a corrupt active state must not
            # stop its classifications, but never silently repair that state.
            self.state = {"version": 1, "windows": {}}
            self.state_unavailable = True
        records = []
        for session in self.sessions:
            record = self.new_record(session)
            try:
                self.scan_window(session, record)
            except GuardError as exc:
                code = "state_write_failed" if str(exc) == "state_write_failed" else "tmux_observation_failed"
                record_error(record, code)
                if code == "state_write_failed":
                    self.reload_after_failure()
            except Exception:
                # A single window must not abort inspection of the rest.
                record_error(record, "window_failed")
                if self.mode == "active" and not self.dry:
                    self.reload_after_failure()
            try:
                self.audit(record)
            except Exception:
                # The complete sanitized record still reaches stdout/journal.
                record_error(record, "audit_failed")
            records.append(record)
        return {"mode": self.mode, "dry_run": self.dry, "records": records,
                "errors": sum("error" in record for record in records)}

    def run(self):
        if self.dry:
            return self.scan()
        self.store.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.store / LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"mode": self.mode, "status": "busy", "errors": 0}
            return self.scan()


def parse_now(value):
    if value is None:
        return datetime.now(timezone.utc).timestamp()
    try:
        result = float(value)
    except ValueError:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timezone required")
        result = dt.timestamp()
    if not finite(result) or result < 0:
        raise ValueError("invalid time")
    datetime.fromtimestamp(result, timezone.utc)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("shadow", "active"), default="shadow")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "store")
    parser.add_argument("--now", help="epoch seconds or timezone-qualified ISO time")
    parser.add_argument("--sessions", help="comma-separated exact agent-session names")
    args = parser.parse_args(argv)
    try:
        sessions = tuple(args.sessions.split(",")) if args.sessions is not None else SESSIONS
        guard = FleetGuard(args.state_dir, parse_now(args.now), args.mode, args.dry_run, sessions)
        result = guard.run()
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("errors") else 0
    except Exception:
        # Tokens can appear in urllib exception URLs; pane output in subprocess
        # exceptions. Neither the exception nor stderr is ever printed.
        print(json.dumps({"mode": args.mode, "error": "guard_failed"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Model-free quota monitor. No scheduling or collector calls.

Run with --dry-run first. --state-dir points to a store directory, --now
accepts epoch seconds or timezone-qualified ISO time. Linux flock serializes
the entire transaction. A durable pending ledger retries failed agent API
acceptances. The pause file is enforced at decision time; level commits after
agent API acceptance. Telegram has a separate, capped three-attempt outbox.

Exactly-once remote delivery is impossible without receiver-side idempotency:
a crash after remote acceptance but before saving its receipt can duplicate
that one message. No exception/HTTP body/URL/token is written to logs.
"""

import argparse
import fcntl
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ("progi", "okoska", "iris", "kisprogi")
METRICS = ("five_hour", "seven_day")
WARN = {"five_hour": 90, "seven_day": 80}
PAUSE_AT = {"five_hour": 98, "seven_day": 90}
LABELS = {"five_hour": "5 órás", "seven_day": "heti"}
POLICY_VERSION = 2
SOURCES = ("authoritative", "authoritative_cached", "authoritative_statusline")
STATE = ".usage-fleet-monitor-state.json"
PAUSE = ".usage-fleet-pause"
STALE = ".usage-stale-alert-state.json"
MAX_AGE = 25 * 60
# A metric crossing its own pause threshold always matters; repeated notices need
# at least 5 percentage points above its last notified high-water mark.
REFIRE_DELTA = 5
TELEGRAM_MAX_ATTEMPTS = 3
CHAT_ID = "6209177290"


class DeliveryError(Exception):
    """Only a fixed, secret-free code may be exposed."""


def safe_error(exc):
    code = str(exc) if isinstance(exc, DeliveryError) else "delivery_failure"
    safe_codes = {"network_failure", "invalid_response", "missing_message_receipt",
                  "telegram_rejected", "missing_telegram_receipt",
                  "dashboard_credentials_unavailable", "telegram_credentials_unavailable",
                  "invalid_delivery_status", "delivery_failure"}
    return code if code in safe_codes or re.fullmatch(r"http_[0-9]{3}", code) else "delivery_failure"


def number(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    except OverflowError:
        return False


def percent(value):
    return number(value) and 0 <= value <= 100


def pct(value):
    return str(int(value)) if float(value).is_integer() else str(value)


def parse_time(value):
    if number(value):
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str):
        raise ValueError("invalid time")
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except ValueError:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Same legacy generated_at convention as usage-check.py.
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, UnicodeError):
        return None
    # Permission/I/O errors are not corrupt state: fail rather than resend.


def atomic_json(path, data):
    """Private unique tempfile, fsync, atomic replace, durable directory entry."""
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
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


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove(path):
    try:
        path.unlink()
    except FileNotFoundError:
        return
    sync_dir(path.parent)


def empty_state():
    return {"level": 0, "metric": None, "percent": None, "source": None, "since": None}


def load_state(path):
    state = read_json(path)
    if (not isinstance(state, dict) or type(state.get("level")) is not int
            or state["level"] not in (0, 80, 90)):
        return empty_state()
    if state["level"] and not valid_target(state):
        return empty_state()
    state["notified_90"] = notice_marks(state)
    if "pending" in state and not valid_event(state["pending"]):
        return empty_state()
    if "telegram_pending" in state and not valid_telegram(state["telegram_pending"]):
        # A broken optional notification must not reset fleet control state.
        state.pop("telegram_pending", None)
    return state


def valid_telegram(notice):
    return (isinstance(notice, dict) and isinstance(notice.get("kind"), str)
            and notice["kind"] in ("warning", "pause", "resume", "reassert")
            and isinstance(notice.get("text"), str)
            and type(notice.get("attempts")) is int
            and 0 <= notice["attempts"] <= TELEGRAM_MAX_ATTEMPTS)


def pause_is_active(path):
    """Mirror readFleetPauseState: empty/corrupt/unreadable is not paused."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if raw == "paused":
            return True
        data = json.loads(raw)
        return isinstance(data, dict) and data.get("paused") is True
    except (OSError, ValueError, UnicodeError):
        return False


def valid_target(target):
    return (isinstance(target, dict) and type(target.get("level")) is int
            and target["level"] in (0, 80, 90) and target.get("metric") in METRICS
            and percent(target.get("percent")) and target.get("source") in SOURCES
            and number(target.get("since")) and target["since"] >= 0)


def valid_event(event):
    if not isinstance(event, dict) or not valid_target(event.get("target")):
        return False
    levels = {"warning": 80, "pause": 90, "resume": 0, "reassert": 90}
    if not isinstance(event.get("kind"), str) or event["kind"] not in levels or event["target"]["level"] != levels[event["kind"]]:
        return False
    if "telegram_queued" in event and type(event["telegram_queued"]) is not bool:
        return False
    delivered = event.get("delivered")
    return (isinstance(event.get("agent_text"), str)
            and isinstance(event.get("telegram_text"), str)
            and isinstance(delivered, dict)
            and all(k in (*AGENTS, "telegram") and v in ("sent", "inactive")
                    and (k != "telegram" or v == "sent") for k, v in delivered.items()))


def notice_marks(state):
    marks = state.get("notified_90")
    if not isinstance(marks, dict):
        return {}
    return {m: marks[m] for m in METRICS if percent(marks.get(m)) and marks[m] >= PAUSE_AT[m]}


def snapshot(path, now):
    try:
        data = read_json(path)
        claude = data["claude"]
        source = claude["source"]
        if source not in SOURCES:
            return None
        age = now.timestamp() - parse_time(data["generated_at"]).timestamp()
        if not 0 <= age <= MAX_AGE:
            return None
        # A freshly rewritten cached snapshot must not rejuvenate old readings.
        if "authoritative_collected_at_unix" in claude:
            collected = claude["authoritative_collected_at_unix"]
            if not number(collected) or not 0 <= now.timestamp() - collected <= MAX_AGE:
                return None
        windows = claude["windows"]
        if not all(percent(windows[m]["used_percent"]) for m in METRICS):
            return None
        for metric in METRICS:
            reset = windows[metric].get("resets_at")
            # Missing/non-numeric resets retain the documented unknown-time
            # policy. Any numeric reset at/before now invalidates both windows.
            if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset <= now.timestamp():
                return None
        return {"source": source, "windows": {m: dict(windows[m]) for m in METRICS}}
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def reset_time(value):
    try:
        if not number(value):
            return "ismeretlen"
        return datetime.fromtimestamp(value, timezone.utc).astimezone(
            ZoneInfo("Europe/Budapest")).strftime("%Y-%m-%d %H:%M:%S %Z (Europe/Budapest)")
    except (ValueError, OverflowError, OSError):
        return "ismeretlen"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def inactive_reply(data):
    # Defensive compatibility only. The current POST /api/messages returns
    # 200 + a queued message id, not an inactive-recipient response. The router
    # handles absent sessions later; HTTP acceptance is not actual delivery.
    if not isinstance(data, dict):
        return False
    if data.get("code") in ("AGENT_NOT_RUNNING", "agent_not_running", "recipient_not_running"):
        return True
    error = data.get("error")
    return (isinstance(error, str) and bool(re.fullmatch(
        r"(?:Recipient|Target agent|Agent) (?:'?(?:progi|okoska|iris|kisprogi)'? )?"
        r"(?:is )?not running\.?", error, flags=re.IGNORECASE)))


class Transport:
    def __init__(self, store, root=ROOT):
        self.store, self.root = store, root
        # Never follow redirects with the dashboard Authorization header.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def post(self, url, payload, headers, agent=False):
        req = urllib.request.Request(url, json.dumps(payload, ensure_ascii=False).encode(),
                                     {"Content-Type": "application/json", **headers}, method="POST")
        try:
            with self.opener.open(req, timeout=10) as response:
                code = response.status
                raw = response.read(65537)
        except urllib.error.HTTPError as exc:
            code = exc.code
            try:
                raw = exc.read(65537)
            finally:
                exc.close()
        except Exception:
            raise DeliveryError("network_failure") from None
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            data = None
        if agent and code in (400, 404, 409, 422, 503) and inactive_reply(data):
            return "inactive"
        if not 200 <= code < 300:
            raise DeliveryError(f"http_{code}")
        if not isinstance(data, dict) or data.get("error"):
            raise DeliveryError("invalid_response")
        if agent:
            if not data.get("id"):
                raise DeliveryError("missing_message_receipt")
        elif data.get("ok") is not True:
            raise DeliveryError("telegram_rejected")
        elif not isinstance(data.get("result"), dict) or not data["result"].get("message_id"):
            raise DeliveryError("missing_telegram_receipt")
        return "sent"

    def send_agent(self, recipient, content):
        try:
            token = (self.store / ".dashboard-token").read_text(encoding="utf-8").strip()
            if not token or "\n" in token or "\r" in token:
                raise ValueError()
        except (OSError, ValueError):
            raise DeliveryError("dashboard_credentials_unavailable") from None
        return self.post("http://localhost:3420/api/messages",
                         {"from": "bela", "to": recipient, "content": content},
                         {"Authorization": "Bearer " + token}, agent=True)

    def send_telegram(self, content):
        # Same .env credential source as notify.sh; parse data, never shell it.
        try:
            token = None
            for line in (self.root / ".env").read_text(encoding="utf-8").splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip("\"'")
                    break
            if not token or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
                raise ValueError()
        except (OSError, ValueError):
            raise DeliveryError("telegram_credentials_unavailable") from None
        # Plain text: no HTML interpolation or parse_mode, fixed owner chat.
        return self.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         {"chat_id": CHAT_ID, "text": content}, {})


class Monitor:
    def __init__(self, store, now, dry_run=False, transport=None):
        self.store, self.now, self.dry = Path(store), now, dry_run
        self.transport = transport or Transport(self.store)

    def write(self, name, data):
        if not self.dry:
            atomic_json(self.store / name, data)

    def delete(self, name):
        if not self.dry:
            remove(self.store / name)

    def log(self, recipient, code):
        # Never log raw exception strings, response bodies, request URLs or secrets.
        if self.dry:
            return
        directory = self.store / "logs"
        directory.mkdir(exist_ok=True)
        path = directory / "usage-fleet-monitor.log"
        if path.exists() and path.stat().st_size > 1024 * 1024:
            os.replace(path, path.with_suffix(".log.1"))
        line = json.dumps({"at": self.now.isoformat(), "recipient": recipient, "code": code}) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(line)

    def event(self, kind, data, state, changed=None):
        values = {m: data["windows"][m]["used_percent"] for m in METRICS}
        thresholds = PAUSE_AT if kind == "pause" else WARN
        selected = [m for m in METRICS if values[m] >= thresholds[m]]
        if kind in ("resume", "reassert"):
            selected = list(METRICS)
        if not selected:
            selected = list(METRICS)  # Legacy in-flight control decision still needs named windows.
        metric = max(changed or selected or METRICS, key=lambda m: values[m])
        detail = "; ".join(f"Claude {LABELS[m]} keret {pct(values[m])}%, reset: "
                           f"{reset_time(data['windows'][m].get('resets_at'))}" for m in selected)
        if kind == "resume":
            content = f"Claude keret miatti szünet feloldva. {detail}. Folytathatjátok az új munkát."
            telegram = f"Claude keret miatti szünet feloldva. {detail}. Új munka indítható."
        elif kind == "warning":
            content = (f"Claude óvatos figyelmeztetés: {detail}. A munka folytatható, "
                       "ez nem flotta-szünet. Az új nagy feladatokat mérlegeljétek.")
            telegram = f"Claude óvatos figyelmeztetés: {detail}. A munka folytatható, nincs flotta-szünet."
        else:
            title = "Claude keret miatti flotta-szünet" if kind == "pause" else "Claude keret miatti szünet továbbra is érvényben"
            content = (f"{title} ({detail}). NE indíts ÚJ munkát amíg nem jelzem hogy feloldva, "
                       "amit csinálsz fejezd be, de utána várj.")
            telegram = f"{title}: {detail}. Új munka indítása leállítva."
        notices = notice_marks(state)
        for m in METRICS:
            if values[m] >= PAUSE_AT[m]:
                notices[m] = max(notices.get(m, 0), values[m])
        target = {"level": 0 if kind == "resume" else 80 if kind == "warning" else 90,
                  "metric": metric, "percent": values[metric], "source": data["source"],
                  "since": int(self.now.timestamp()), "notified_90": {} if kind == "resume" else notices}
        return {"kind": kind, "target": target, "agent_text": content,
                "telegram_text": telegram, "delivered": {}, "policy_version": POLICY_VERSION}

    def refresh_legacy_text(self, state, data):
        """Upgrade unsent legacy wording without discarding receipts or retry budgets."""
        pending = state.get("pending")
        notice = state.get("telegram_pending")
        if pending and pending.get("policy_version") != POLICY_VERSION:
            fresh = self.event(pending["kind"], data, state)
            pending.update({k: fresh[k] for k in ("agent_text", "telegram_text", "policy_version")})
        if notice and notice.get("policy_version") != POLICY_VERSION:
            fresh = self.event(notice["kind"], data, state)
            notice.update(text=fresh["telegram_text"], policy_version=POLICY_VERSION)

    def deliver(self, state, event):
        if self.dry:
            remaining = [r for r in AGENTS if r not in event["delivered"]]
            return "DRY_RUN " + json.dumps({"action": event["kind"],
                "remaining_recipients": remaining,
                "agent_text": event["agent_text"], "telegram_chat": CHAT_ID,
                "telegram_text": event["telegram_text"], "target_level": event["target"]["level"],
                "pause_file": "write" if event["target"]["level"] == 90 else
                              "remove" if event["kind"] == "resume" else "unchanged",
                "state_file": "write", "stale_file": "remove" if self.clear_stale else "unchanged"},
                ensure_ascii=False)
        state["pending"] = event
        if not event.get("telegram_queued"):
            # Also migrate old pending events which may already contain a
            # Telegram receipt from the original all-recipient transaction.
            if event["delivered"].get("telegram") != "sent":
                existing = state.get("telegram_pending")
                if not existing or existing["kind"] != event["kind"] or existing["text"] != event["telegram_text"]:
                    state["telegram_pending"] = {"kind": event["kind"],
                        "text": event["telegram_text"], "attempts": 0, "policy_version": POLICY_VERSION}
            else:
                state.pop("telegram_pending", None)
            event["telegram_queued"] = True
        self.write(STATE, state)  # Must be durable BEFORE any network effect.
        target = event["target"]
        if target["level"] == 90:
            # Technical enforcement cannot depend on Telegram or agent health.
            self.write(PAUSE, {"paused": True, **{k: target[k] for k in
                        ("metric", "percent", "source", "since")}})
        failures = []
        for recipient in AGENTS:
            if recipient in event["delivered"]:
                continue
            try:
                status = self.transport.send_agent(recipient, event["agent_text"])
                if status not in ("sent", "inactive"):
                    raise DeliveryError("invalid_delivery_status")
            except Exception as exc:
                self.log(recipient, safe_error(exc))
                failures.append(recipient)
                continue
            event["delivered"][recipient] = status
            # If writing the receipt fails, STOP; do not send to more recipients.
            self.write(STATE, state)
        if failures:
            return f"RETRY {event['kind']} failed={','.join(failures)} level={state['level']}"
        if event["kind"] == "resume":
            # Only agent API acknowledgements gate release, never Telegram.
            self.delete(PAUSE)
        state.update(target)
        state.pop("pending", None)
        self.write(STATE, state)
        return f"SENT {event['kind']} level={state['level']}"

    def flush_telegram(self):
        state = load_state(self.store / STATE)
        notice = state.get("telegram_pending")
        if not notice:
            return None
        pending = state.get("pending")
        if notice["kind"] == "resume" and pending and pending["kind"] == "resume":
            # Do not tell the owner release succeeded before agents are queued
            # and the technical pause is removed. No retry budget is spent.
            return "telegram=deferred_until_agents"
        if self.dry:
            return ("telegram=would_abandon(3/3)" if notice["attempts"] >= TELEGRAM_MAX_ATTEMPTS
                    else f"telegram=would_try({notice['attempts'] + 1}/3)")
        if notice["attempts"] >= TELEGRAM_MAX_ATTEMPTS:
            # Crash recovery after the third pre-reserved attempt.
            self.log("telegram", "telegram_abandoned")
            state.pop("telegram_pending", None)
            self.write(STATE, state)
            return "telegram=abandoned(3/3)"
        notice["attempts"] += 1
        self.write(STATE, state)  # Reserve attempt durably BEFORE contacting API.
        try:
            if self.transport.send_telegram(notice["text"]) != "sent":
                raise DeliveryError("invalid_delivery_status")
        except Exception as exc:
            self.log("telegram", safe_error(exc))
            if notice["attempts"] >= TELEGRAM_MAX_ATTEMPTS:
                self.log("telegram", "telegram_abandoned")
                state.pop("telegram_pending", None)
                result = "telegram=abandoned(3/3)"
            else:
                result = f"telegram=retry({notice['attempts']}/3)"
            self.write(STATE, state)
            return result
        state.pop("telegram_pending", None)
        self.write(STATE, state)
        return None

    def finish(self, result):
        # Stale snapshots are always silent, including Telegram retries.
        if result.startswith(("STALE", "DRY_RUN stale")):
            return result
        if self.dry and result.startswith("DRY_RUN {"):
            return result  # New control event's preview already describes it.
        telegram = self.flush_telegram()
        return result + (" " + telegram if telegram else "")

    def evaluate(self):
        self.clear_stale = False
        state = load_state(self.store / STATE)
        pending_recovery = state.get("pending")
        if pending_recovery and pending_recovery["target"]["level"] == 90 and not pause_is_active(self.store / PAUSE):
            # Recover a crash between the durable decision and pause-file
            # creation, even if the collector has gone stale since that run.
            target = pending_recovery["target"]
            self.write(PAUSE, {"paused": True, **{k: target[k] for k in
                        ("metric", "percent", "source", "since")}})
        data = snapshot(self.store / "usage-latest.json", self.now)
        if data is None:
            old = read_json(self.store / STALE)
            old = old if isinstance(old, dict) else {}
            since = old.get("since") if old.get("alerted") is True else None
            stale = {"alerted": True, "since": since if number(since) else int(self.now.timestamp())}
            for m in METRICS:
                key = "last_known_percent_" + m
                value = state.get(key, old.get(key))
                stale[key] = value if percent(value) else None
            self.write(STALE, stale)
            return "DRY_RUN stale would_update_stale_state" if self.dry else "STALE silent"
        stale = read_json(self.store / STALE)
        if isinstance(stale, dict) and stale.get("alerted") is True:
            self.clear_stale = True
            self.delete(STALE)
        values = {m: data["windows"][m]["used_percent"] for m in METRICS}
        for m in METRICS:
            state["last_known_percent_" + m] = values[m]
        self.refresh_legacy_text(state, data)
        pause_exists = pause_is_active(self.store / PAUSE)
        pending = state.get("pending")
        if not valid_event(pending):
            pending = None
            state.pop("pending", None)
        both_low = all(values[m] < WARN[m] for m in METRICS)
        at_pause = any(values[m] >= PAUSE_AT[m] for m in METRICS)
        # In-flight pause counts as paused: some recipients may already have
        # stopped, including an ambiguous timeout without a saved receipt.
        was_paused = state["level"] == 90 or pause_exists or (
            pending and pending["kind"] in ("pause", "reassert", "resume"))
        if both_low:
            if was_paused:
                if not pending or pending["kind"] != "resume":
                    pending = self.event("resume", data, state)
                return self.deliver(state, pending)
            state.update(empty_state())
            state["notified_90"] = {}
            state.pop("pending", None)
            if state.get("telegram_pending", {}).get("kind") == "warning":
                state.pop("telegram_pending", None)  # An obsolete warning.
            self.write(STATE, state)
            return ("DRY_RUN write_state_level=0 no_message stale_file=" +
                    ("remove" if self.clear_stale else "unchanged")) if self.dry else "OK level=0 silent"
        if pending:
            if pending["kind"] == "resume":
                # Some agents may have resumed; fresh data no longer permits
                # release. Reassert once, even if the metrics are between their resume and pause thresholds.
                return self.deliver(state, self.event("pause" if at_pause
                                    else "reassert", data, state))
            if pending["kind"] != "warning" or not at_pause:
                return self.deliver(state, pending)
            # Escalation supersedes an incomplete warning.
        notices = notice_marks(state)
        changed = [m for m in METRICS if values[m] >= PAUSE_AT[m] and
                   (not percent(notices.get(m)) or values[m] >= notices[m] + REFIRE_DELTA)]
        if at_pause and (state["level"] < 90 or changed):
            return self.deliver(state, self.event("pause", data, state, changed))
        if state["level"] < 80 and not was_paused:
            return self.deliver(state, self.event("warning", data, state))
        if was_paused and not pause_exists:
            # Recover an externally removed pause file without repeating notices.
            self.write(PAUSE, {"paused": True, **{k: state.get(k) for k in
                        ("metric", "percent", "source", "since")}})
        self.write(STATE, state)
        return ("DRY_RUN write_state no_message pause_file=" +
                ("restore" if was_paused and not pause_exists else "unchanged") +
                " stale_file=" + ("remove" if self.clear_stale else "unchanged")) if self.dry else f"UNCHANGED level={state['level']}"

    def run(self):
        if self.dry:
            return self.finish(self.evaluate())
        self.store.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.store / ".usage-fleet-monitor.lock",
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "BUSY another_run_holds_lock"
            return self.finish(self.evaluate())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "store")
    parser.add_argument("--now", help="epoch seconds or ISO timestamp (legacy naive ISO is UTC)")
    args = parser.parse_args(argv)
    monitor = None
    try:
        now = parse_time(args.now) if args.now is not None else datetime.now(timezone.utc)
        monitor = Monitor(args.state_dir, now, args.dry_run)
        result = monitor.run()
        print("USAGE_FLEET_MONITOR " + result)
        return 1 if result.startswith("RETRY ") or "telegram=retry(" in result else 0
    except Exception as exc:
        # No exception message: urllib errors can contain a full bot-token URL.
        code = "io_failure" if isinstance(exc, OSError) else "monitor_failure"
        if monitor is not None:
            try:
                monitor.log("monitor", code)
            except Exception:
                code += "_log_unavailable"
        print("USAGE_FLEET_MONITOR ERROR " + code)
        return 1


if __name__ == "__main__":
    sys.exit(main())

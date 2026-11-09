#!/usr/bin/env python3
"""Collect Codex quota from at most eight recent session logs; stdlib only.

Never reads auth.json, calls a model, or exports conversation content.
Only the timestamp and allowlisted rate_limits fields leave the parser.
"""

import sys
sys.dont_write_bytecode = True
import argparse
import heapq
import json
import math
import os
import pwd
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "store" / "codex-usage-latest.json"
MAX_FILES = 8
MAX_LINE_BYTES = 1024 * 1024
FRESH_SECONDS = 30 * 60
WINDOWS = {300: "five_hour", 10080: "seven_day"}


def default_sessions_dir():
    """Honor the runtime's explicit Codex home; otherwise use the Unix account.

    Rollout filename/date is creation time, not observation freshness. Discovery
    below sorts by current file mtime, including resumed older sessions.
    """
    codex_home = os.environ.get("CODEX_HOME")
    home = Path(codex_home) if codex_home else Path(pwd.getpwuid(os.getuid()).pw_dir) / ".codex"
    return home / "sessions"


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (ValueError, OverflowError):
        return None


def number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def valid_window(value):
    if not isinstance(value, dict):
        return False
    used, reset = value.get("used_percent"), value.get("resets_at")
    minutes = value.get("window_minutes")
    if not (number(used) and 0 <= used <= 100 and number(reset) and reset >= 0):
        return False
    if type(minutes) is not int or minutes not in WINDOWS:
        return False
    try:
        datetime.fromtimestamp(reset, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return False
    return True


def extract(row):
    """Return a validated snapshot fragment, never the original row/payload."""
    if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
        return None
    limits = row["payload"].get("rate_limits")
    stamp = parse_time(row.get("timestamp"))
    if stamp is None or not isinstance(limits, dict):
        return None
    if limits.get("limit_id", "codex") != "codex":
        return None
    windows = {}
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not valid_window(window):
            return None
        label = WINDOWS[window["window_minutes"]]
        windows[label] = {k: window[k] for k in ("used_percent", "resets_at", "window_minutes")}
    if set(windows) != set(WINDOWS.values()):
        return None
    plan = limits.get("plan_type")
    if not isinstance(plan, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", plan):
        plan = None
    credits = limits.get("credits")
    clean_credits = None
    if isinstance(credits, dict):
        clean_credits = {k: credits[k] for k in ("has_credits", "unlimited")
                         if type(credits.get(k)) is bool}
        balance = credits.get("balance")
        if isinstance(balance, str) and re.fullmatch(r"\d{1,30}(?:\.\d{1,30})?", balance):
            clean_credits["balance"] = balance
        elif number(balance) and balance >= 0:
            clean_credits["balance"] = balance
    return {"observed_at": stamp.isoformat(), "plan_type": plan,
            "windows": windows, "credits": clean_credits}


def recent_files(sessions_dir):
    """Stat matching filenames, but read contents from only MAX_FILES files."""
    root = Path(sessions_dir).absolute()

    def directories(parent, digits):
        with os.scandir(parent) as entries:
            for entry in entries:
                if (len(entry.name) == digits and entry.name.isascii()
                        and entry.name.isdigit() and entry.is_dir(follow_symlinks=False)):
                    yield Path(entry.path)

    def days():
        for year in directories(root, 4):
            try:
                for month in directories(year, 2):
                    try:
                        yield from directories(month, 2)
                    except OSError:
                        continue
            except OSError:
                continue

    def candidates():
        try:
            for day in days():
                try:
                    with os.scandir(day) as entries:
                        for entry in entries:
                            if not (entry.name.startswith("rollout-") and entry.name.endswith(".jsonl")):
                                continue
                            try:
                                info = entry.stat(follow_symlinks=False)
                                if stat.S_ISREG(info.st_mode):
                                    yield (info.st_mtime_ns, entry.path, Path(entry.path))
                            except OSError:
                                continue
                except OSError:
                    continue
        except OSError:
            return

    return [item[2] for item in heapq.nlargest(MAX_FILES, candidates())]


def quota_rows(stream, size):
    """Bound memory and read only bytes present at open, even during appends.

    Scan all bounded lines: timestamps need not follow physical line order.
    Oversized (>1 MiB), invalid UTF-8 and unterminated lines are discarded.
    """
    remaining = size
    oversized = False
    while remaining > 0:
        line = stream.readline(min(MAX_LINE_BYTES + 1, remaining))
        if not line:
            break
        remaining -= len(line)
        complete = line.endswith(b"\n")
        if oversized or len(line) > MAX_LINE_BYTES or not complete:
            oversized = not complete
            continue
        if b'"rate_limits"' not in line:
            continue
        try:
            yield json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            continue


def collect(sessions_dir, now=None):
    now = now or datetime.now(timezone.utc)
    result = {"generated_at": now.isoformat(), "source": "codex-session-log",
              "observed_at": None, "plan_type": None, "windows": {},
              "credits": None, "freshness": "missing", "observation_age_seconds": None}
    latest, latest_time = None, None
    for path in recent_files(sessions_dir):
        try:
            # Refuse symlinks even if the candidate changed since discovery.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    continue
                for row in quota_rows(stream, info.st_size):
                    try:
                        fragment = extract(row)
                    except (ValueError, TypeError, OverflowError, RecursionError):
                        continue
                    if fragment is None:
                        continue
                    observed = parse_time(fragment["observed_at"])
                    if latest_time is None or observed > latest_time:
                        latest, latest_time = fragment, observed
        except OSError:
            continue
    if latest is not None:
        result.update(latest)
        age = (now - latest_time).total_seconds()
        result["freshness"] = "fresh" if 0 <= age <= FRESH_SECONDS else "stale"
        result["observation_age_seconds"] = age if age >= 0 else None
    return result


def atomic_write(output, snapshot):
    output = Path(output)
    # Serialize before opening any output; reject non-standard JSON numbers.
    content = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix="." + output.name + ".", delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # CODEX_HOME matches the bridge; cron HOME alone must not change identity.
    parser.add_argument("--sessions-dir", type=Path,
                        default=default_sessions_dir())
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        snapshot = collect(args.sessions_dir)
        if snapshot["freshness"] == "missing":
            print("Codex 5 órás/heti keret: ISMERETLEN (missing)")
            if args.output.exists():
                return 1  # Keep the last usable snapshot on a source failure.
        atomic_write(args.output, snapshot)
    except (OSError, ValueError):
        # Fixed message: never expose paths or log content via exception text.
        print("Codex 5 órás/heti keret: ISMERETLEN (a pillanatkép nem írható)")
        return 1
    return 1 if snapshot["freshness"] == "missing" else 0


if __name__ == "__main__":
    raise SystemExit(main())

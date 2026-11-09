#!/usr/bin/env python3
"""Print a single Codex quota line; reads only the collector snapshot."""

import sys
sys.dont_write_bytecode = True
import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "codex_usage_collect", Path(__file__).with_name("codex-usage-collect.py"))
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)


def status(used):
    return "STOP" if used >= 90 else "OPATLAN" if used >= 80 else "OK"


def format_snapshot(snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(snapshot, dict) or snapshot.get("source") != "codex-session-log":
        return "Codex 5 órás/heti keret: ISMERETLEN (missing)"
    observed = collector.parse_time(snapshot.get("observed_at"))
    freshness = snapshot.get("freshness")
    if freshness not in ("fresh", "stale", "missing"):
        freshness = "missing"
    if observed is None:
        return "Codex 5 órás/heti keret: ISMERETLEN (missing)"
    age = (now - observed).total_seconds()
    if freshness == "missing":
        return "Codex 5 órás/heti keret: ISMERETLEN (missing)"
    if age < 0:
        return "Codex 5 órás/heti keret: ISMERETLEN (jövőbeli mérési idő)"
    historical = freshness == "stale" or age > collector.FRESH_SECONDS
    windows = snapshot.get("windows")
    if not isinstance(windows, dict):
        return "Codex 5 órás/heti keret: ISMERETLEN (missing)"
    parts = []
    for name, minutes, label in (("five_hour", 300, "5 órás"), ("seven_day", 10080, "heti")):
        window = windows.get(name)
        if not collector.valid_window(window) or window["window_minutes"] != minutes:
            return "Codex 5 órás/heti keret: ISMERETLEN (missing)"
        reset = datetime.fromtimestamp(window["resets_at"], timezone.utc).isoformat()
        used = window["used_percent"]
        if window["resets_at"] <= now.timestamp():
            parts.append(f"Codex {label} keret: aktuális ISMERETLEN; előző mérés {used:g}% (reset előtt), reset: {reset}")
        elif historical:
            level = f"{status(used)}-szint, " if used >= 80 else ""
            parts.append(f"Codex {label} keret: legalább {used:g}% ({level}nem friss), reset: {reset}")
        else:
            parts.append(f"Codex {label} keret {used:g}% {status(used)}, reset: {reset}")
    seconds = int(age)
    age_text = (f"{seconds // 3600} ó {seconds % 3600 // 60} p" if seconds >= 3600
                else f"{seconds // 60} p {seconds % 60} mp")
    return " | ".join(parts) + f" | mérés kora: {age_text}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=collector.DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        with args.input.open(encoding="utf-8") as stream:
            snapshot = json.load(stream)
        line = format_snapshot(snapshot)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        line = "Codex 5 órás/heti keret: ISMERETLEN (missing)"
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

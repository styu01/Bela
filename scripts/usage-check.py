#!/usr/bin/env python3
"""One-line Claude admission hint from store/usage-latest.json.

Exit 0: OK or OPATLAN; 2: STOP; 3: ISMERETLEN.
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LATEST_PATH = os.environ.get("USAGE_CHECK_LATEST_PATH", os.path.join(REPO_ROOT, "store", "usage-latest.json"))
FRESH_AFTER_SECONDS = 25 * 60


def unknown():
    print("USAGE ISMERETLEN")
    return 3


def valid_percent(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100


def snapshot_age_seconds(data, claude, now):
    """Prefer the real upstream collection time over snapshot write time."""
    collected = claude.get("authoritative_collected_at_unix")
    if isinstance(collected, (int, float)) and not isinstance(collected, bool) and math.isfinite(collected):
        return now.timestamp() - collected
    # First migration run can still read a legacy snapshot with only
    # generated_at; retain that one-run compatibility path.
    generated = datetime.fromisoformat(data.get("generated_at"))
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    return (now - generated).total_seconds()


def valid_future_reset(window, now):
    reset = window.get("resets_at")
    return isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset) and reset > now.timestamp()


def main():
    try:
        with open(LATEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        now = datetime.now(timezone.utc)
        claude = data.get("claude") or {}
        age_seconds = snapshot_age_seconds(data, claude, now)
        windows = claude.get("windows") or {}
        five_window = windows.get("five_hour") or {}
        seven_window = windows.get("seven_day") or {}
        five = five_window.get("used_percent")
        seven = seven_window.get("used_percent")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return unknown()
    if age_seconds < 0 or age_seconds > FRESH_AFTER_SECONDS or not valid_percent(five) or not valid_percent(seven) or not valid_future_reset(five_window, now) or not valid_future_reset(seven_window, now):
        return unknown()
    # Display and threshold decisions share exactly the same rounded values.
    five, seven = round(five, 2), round(seven, 2)
    status = "STOP" if max(five, seven) >= 90 else "OPATLAN" if max(five, seven) >= 80 else "OK"
    print(f"USAGE 5h={five:g}% 7d={seven:g}% friss({int(age_seconds // 60)}p) {status}")
    return 2 if status == "STOP" else 0


if __name__ == "__main__":
    sys.exit(main())

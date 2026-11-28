#!/usr/bin/env python3
"""One-line Claude admission hint from store/usage-latest.json.

Exit 0: OK or FIGYELMEZTETÉS; 2: STOP; 3: ISMERETLEN.

The line names the measured account (snapshot claude.account: styu01 or info;
a snapshot without it is the default styu01). An account whose API responses
carry no 5-hour window (claude.windows_not_reported contains "five_hour", see
usage-collect.py) is judged on the weekly window alone and the line says so;
a 5-hour window missing WITHOUT that explicit marker is still ISMERETLEN.
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LATEST_PATH = os.environ.get("USAGE_CHECK_LATEST_PATH", os.path.join(REPO_ROOT, "store", "usage-latest.json"))
FRESH_AFTER_SECONDS = 25 * 60
ACCOUNTS = ("styu01", "info")
DEFAULT_ACCOUNT = "styu01"


def account_label(claude):
    account = claude.get("account") if isinstance(claude, dict) else None
    return account if isinstance(account, str) and account in ACCOUNTS else DEFAULT_ACCOUNT


def unknown(account=DEFAULT_ACCOUNT, reason=None):
    suffix = f" ({reason})" if reason else ""
    print(f"USAGE {account} Claude 5 órás/heti keret ISMERETLEN{suffix}")
    return 3


def five_hour_not_reported(claude, windows):
    absent = claude.get("windows_not_reported")
    return isinstance(absent, list) and "five_hour" in absent and "five_hour" not in windows


def valid_percent(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100 and math.isfinite(value)


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
    try:
        return isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset) and reset > now.timestamp()
    except OverflowError:
        return False


def main():
    account = DEFAULT_ACCOUNT
    try:
        with open(LATEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        now = datetime.now(timezone.utc)
        claude = data.get("claude") or {}
        account = account_label(claude)
        age_seconds = snapshot_age_seconds(data, claude, now)
        windows = claude.get("windows") or {}
        no_five = five_hour_not_reported(claude, windows)
        five_window = windows.get("five_hour") or {}
        seven_window = windows.get("seven_day") or {}
        five = five_window.get("used_percent")
        seven = seven_window.get("used_percent")
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return unknown(account)
    if age_seconds < 0 or age_seconds > FRESH_AFTER_SECONDS:
        return unknown(account, "nincs friss mérés")
    five_ok = no_five or (valid_percent(five) and valid_future_reset(five_window, now))
    if not five_ok or not valid_percent(seven) or not valid_future_reset(seven_window, now):
        return unknown(account, "hiányos vagy érvénytelen mérés")
    # Display and threshold decisions share exactly the same rounded values.
    seven = round(seven, 2)
    if no_five:
        status = "STOP" if seven >= 90 else "FIGYELMEZTETÉS" if seven >= 80 else "OK"
        five_txt = "n/a (ez a fiók nem ad 5 órás ablakot)"
    else:
        five = round(five, 2)
        status = "STOP" if five >= 98 or seven >= 90 else "FIGYELMEZTETÉS" if five >= 90 or seven >= 80 else "OK"
        five_txt = f"{five:g}%"
    print(f"USAGE {account} Claude 5 órás keret={five_txt} heti keret={seven:g}% friss({int(age_seconds // 60)}p) {status}")
    return 2 if status == "STOP" else 0


if __name__ == "__main__":
    sys.exit(main())

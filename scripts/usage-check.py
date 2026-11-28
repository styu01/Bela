#!/usr/bin/env python3
"""One-line Claude admission hint from store/usage-latest.json.

Exit 0: OK or FIGYELMEZTETÉS; 2: STOP; 3: ISMERETLEN.

The line names the measured account (snapshot claude.account: styu01, info or
ismeretlen; a snapshot without a valid one is "ismeretlen"), and the account
decides the thresholds (ACCOUNT_THRESHOLDS): the info account has no 5-hour
limit, so it is judged on the weekly window alone. styu01 and an unknown
account need both windows: a missing 5-hour window there is ISMERETLEN, with or
without a windows_not_reported marker (KISPROGI 6584 K1).
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LATEST_PATH = os.environ.get("USAGE_CHECK_LATEST_PATH", os.path.join(REPO_ROOT, "store", "usage-latest.json"))
FRESH_AFTER_SECONDS = 25 * 60
ACCOUNTS = ("styu01", "info", "ismeretlen")
DEFAULT_ACCOUNT = "ismeretlen"
# Owner decision 2026-11-28 (Telegram 8728, BELA 6580): the thresholds belong to the ACCOUNT, not to which headers a
# response happened to carry. (warning, STOP) per window; a window an account has no limit for is not judged.
#   styu01: the fleet rule, 5-hour 90/98, weekly 80/90.
#   info (Ultra): no 5-hour limit at all, weekly 90/96 (as usage-check-info.py since 11-24).
#   ismeretlen (a token matching neither account): the stricter fleet rule, both windows required.
ACCOUNT_THRESHOLDS = {
    "styu01": {"five_hour": (90, 98), "seven_day": (80, 90)},
    "info": {"seven_day": (90, 96)},
    "ismeretlen": {"five_hour": (90, 98), "seven_day": (80, 90)},
}


def account_label(claude):
    account = claude.get("account") if isinstance(claude, dict) else None
    return account if isinstance(account, str) and account in ACCOUNTS else DEFAULT_ACCOUNT


def unknown(account=DEFAULT_ACCOUNT, reason=None):
    suffix = f" ({reason})" if reason else ""
    print(f"USAGE {account} Claude 5 órás/heti keret ISMERETLEN{suffix}")
    return 3


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
        five_window = windows.get("five_hour") or {}
        seven_window = windows.get("seven_day") or {}
        five = five_window.get("used_percent")
        seven = seven_window.get("used_percent")
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return unknown(account)
    if age_seconds < 0 or age_seconds > FRESH_AFTER_SECONDS:
        return unknown(account, "nincs friss mérés")
    limits = ACCOUNT_THRESHOLDS[account]
    five_judged = "five_hour" in limits  # the account has a 5-hour limit: the window must be there
    five_ok = not five_judged or (valid_percent(five) and valid_future_reset(five_window, now))
    if not five_ok or not valid_percent(seven) or not valid_future_reset(seven_window, now):
        return unknown(account, "hiányos vagy érvénytelen mérés")
    # Display and threshold decisions share exactly the same rounded values.
    values = {"seven_day": round(seven, 2)}
    if five_judged:
        values["five_hour"] = round(five, 2)
    if any(values[m] >= limits[m][1] for m in values):
        status = "STOP"
    elif any(values[m] >= limits[m][0] for m in values):
        status = "FIGYELMEZTETÉS"
    else:
        status = "OK"
    seven = values["seven_day"]
    five_txt = f"{values['five_hour']:g}%" if five_judged else "n/a (ennél a fióknál nincs 5 órás keret)"
    print(f"USAGE {account} Claude 5 órás keret={five_txt} heti keret={seven:g}% friss({int(age_seconds // 60)}p) {status}")
    return 2 if status == "STOP" else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""One-line quota hint for the info@mohokandur.hu Claude account (Ultra, "info keret").

Queries the Messages API response headers live with store/.claude-oauth-token.info.
Never prints the token. Exit 0: OK or FIGYELMEZTETÉS; 2: STOP; 3: ISMERETLEN.
Ultra thresholds (Istvan 2026-11-24): FIGYELMEZTETÉS (warning) at >=90, STOP at >=96, on either window.
The Ultra plan may return only the weekly window; a missing 5h window is reported as "n/a".
"""
import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_PATH = os.environ.get("USAGE_CHECK_INFO_TOKEN_PATH", os.path.join(REPO_ROOT, "store", ".claude-oauth-token.info"))
URL = os.environ.get("USAGE_CHECK_INFO_URL", "https://api.anthropic.com/v1/messages")


def unknown(reason):
    print(f"USAGE info Claude keret ISMERETLEN ({reason})")
    return 3


def number(headers, name):
    try:
        value = float(headers.get(name))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def window(headers, key):
    util = number(headers, f"anthropic-ratelimit-unified-{key}-utilization")
    reset = number(headers, f"anthropic-ratelimit-unified-{key}-reset")
    if util is None:
        return None
    return round(min(util, 1.0) * 100.0, 2), reset


def main():
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return unknown("nincs token")
    if not token:
        return unknown("üres token")
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1,
        "system": [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}],
        "messages": [{"role": "user", "content": "hi"}],
    }).encode("utf-8")
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "content-type": "application/json",
        "User-Agent": "claude-code/2.1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            headers = resp.headers
            resp.read()
    except urllib.error.HTTPError as e:
        headers = e.headers
        if e.code in (401, 403) and window(headers, "7d") is None:
            return unknown(f"HTTP {e.code}")
    except Exception as e:
        return unknown(type(e).__name__)
    five, seven = window(headers, "5h"), window(headers, "7d")
    if seven is None:
        return unknown("nincs heti adat")
    five_pct = five[0] if five else None
    seven_pct = seven[0]
    if (five_pct is not None and five_pct >= 96) or seven_pct >= 96:
        status = "STOP"
    elif (five_pct is not None and five_pct >= 90) or seven_pct >= 90:
        status = "FIGYELMEZTETÉS"
    else:
        status = "OK"
    five_txt = f"{five_pct:g}%" if five_pct is not None else "n/a"
    reset_txt = ""
    if seven[1]:
        reset_txt = " heti reset=" + datetime.fromtimestamp(seven[1]).astimezone().strftime("%m-%d %H:%M")
    print(f"USAGE info Claude 5 órás keret={five_txt} heti keret={seven_pct:g}%{reset_txt} friss(0p) {status}")
    return 2 if status == "STOP" else 0


if __name__ == "__main__":
    sys.exit(main())

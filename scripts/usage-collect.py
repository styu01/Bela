#!/usr/bin/env python3
"""
usage-collect.py -- subscription QUOTA-WINDOW monitor for this fleet.

Reads current usage/quota status for two providers:
  - Codex (OpenAI Codex CLI): authoritative, parsed from the newest
    ~/.codex/sessions/*/*/*/rollout-*.jsonl rate_limits event.
  - Claude (Claude Code subscription): gets the authoritative utilization
    headers from a minimal api.anthropic.com/v1/messages request first, authenticating with
    the session OAuth token (~/.claude/.credentials.json, or the macOS
    login Keychain entry "Claude Code-credentials" -- on darwin the file
    does not exist). On a transient
    failure (429/5xx/timeout) it reuses the last authoritative snapshot
    from store/usage-latest.json if it's fresh enough (source becomes
    "authoritative_cached") instead of losing real numbers. Only falls
    back to a token-total ESTIMATE built from local
    ~/.claude/projects/*/*.jsonl transcripts when there's no usable
    cache: missing/expired token, a 403 (real scope problem), or a
    stale/absent cache.

Default run prints a human-readable summary, prints any
"ALERT: ..." lines (de-duplicated via store/usage-alert-state.json),
appends one JSON snapshot line to store/usage-history.jsonl, and
overwrites store/usage-latest.json with the latest snapshot.

--json prints only the snapshot JSON (no alerts, no side effects) --
for programmatic consumption (e.g. a future dashboard widget).

Stdlib only, no pip installs. Never prints token/secret values.
Exit code is always 0 -- this is a monitor, it must never crash the
scheduler that calls it. Source failures degrade gracefully and are
noted in the summary/snapshot instead of raising.
"""

import argparse
import glob
import json
import math
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Config -- tune thresholds here.
# --------------------------------------------------------------------------

CONFIG = {
    # Time-proportional "pace" model: for every window, compare how much of
    # the quota is used vs. how much of the window has elapsed. Alerts on
    # both over- and under-use, not just last-minute depletion.
    "pace_over_ratio": 1.5,                 # used_fraction/elapsed_fraction >= this -> OVERUSE candidate
    "pace_under_ratio": 0.5,                # ... <= this -> UNDERUSE candidate
    "pace_min_elapsed_fraction": 0.15,      # below this elapsed fraction, skip OVER/UNDER (early-window noise)
    "hard_near_exhaustion": 0.92,           # used_fraction >= this -> backstop alert regardless of pace
    "pace_over_refire_minutes": 45,         # min gap between repeated ongoing OVERUSE alerts
    "near_exhaustion_refire_minutes": 45,   # min gap between repeated ongoing near-exhaustion alerts
    "pace_under_refire_hours": 12,          # min gap between repeated ongoing UNDERUSE alerts
    "underuse_min_window_sec": 86400,       # UNDERUSE only makes sense for windows where unused
                                             # quota is actually lost at reset (weekly-class, >=24h).
                                             # Short windows (e.g. Claude 5h) reset constantly --
                                             # under-pace there is normal, not a signal.
    # The collector runs every 10 minutes. 25 minutes admits one missed run
    # plus scheduler jitter, but never presents a much older quota value as
    # current after an upstream incident.
    "claude_authoritative_cache_max_age_min": 25,
    # Operational kill-switch. A False value restores the complete legacy
    # collection order; store/.usage-headers-disabled does the same per run.
    "claude_usage_headers_enabled": True,
    "claude_statusline_cache_max_age_min": 25,
    "claude_statusline_cache_max_age_min_by_window": {
        # Direct headers are primary. A seven_day statusline value can remain
        # unchanged for hours, so this strict fallback is normally rejected;
        # that is intentional rather than a tracker failure.
        "seven_day": 25,
        "five_hour": 25,
    },
}

# Window length in seconds, keyed by the Claude window's dict key. Codex
# windows carry their own "window_minutes" field so no static table is
# needed for them.
CLAUDE_WINDOW_SECONDS = {
    "five_hour": 5 * 3600,
    "seven_day": 7 * 86400,
    "seven_day_opus": 7 * 86400,
    "seven_day_sonnet": 7 * 86400,
}

# Canonical display names used in ALERT lines, keyed by the
# "<provider>_<window_key>" scope key used for de-dup state too.
ALERT_WINDOW_NAMES = {
    "claude_five_hour": "5-hour limit",
    "claude_seven_day": "weekly limit",
    "claude_seven_day_opus": "Fable weekly limit",
    "claude_seven_day_sonnet": "Sonnet weekly limit",
    "codex_weekly": "Codex weekly limit",
    "codex_five_hour": "Codex 5-hour limit",
}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_DIR = os.path.join(REPO_ROOT, "store")
HISTORY_PATH = os.path.join(STORE_DIR, "usage-history.jsonl")
LATEST_PATH = os.path.join(STORE_DIR, "usage-latest.json")
STATUSLINE_CACHE_PATH = os.path.join(STORE_DIR, "usage-statusline-latest.json")
USAGE_HEADERS_DISABLED_PATH = os.path.join(STORE_DIR, ".usage-headers-disabled")
STATE_PATH = os.path.join(STORE_DIR, "usage-alert-state.json")
ENV_PATH = os.path.join(REPO_ROOT, ".env")

BUDAPEST = ZoneInfo("Europe/Budapest")

# Historical statusline limits used only by the complete legacy kill-switch
# path. Direct header collection uses the stricter 25-minute values above.
LEGACY_STATUSLINE_CACHE_MAX_AGE_MIN = 15
LEGACY_AUTHORITATIVE_CACHE_MAX_AGE_MIN = 90
LEGACY_STATUSLINE_CACHE_MAX_AGE_MIN_BY_WINDOW = {
    "five_hour": 45,
    "seven_day": 8 * 60,
}


def _claude_usage_headers_enabled():
    """Evaluate both operational kill-switches on every collector run."""
    return bool(CONFIG.get("claude_usage_headers_enabled", True)) and not os.path.exists(USAGE_HEADERS_DISABLED_PATH)

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def fmt_reset(resets_at, now_utc=None):
    """Return (local date+time string, relative 'in X days Y hours' string)."""
    if resets_at is None:
        return "unknown", "unknown"
    try:
        dt_utc = datetime.fromtimestamp(int(resets_at), tz=timezone.utc)
    except (ValueError, OSError, OverflowError, TypeError):
        return "unknown", "unknown"
    now_utc = now_utc or datetime.now(timezone.utc)
    local_str = dt_utc.astimezone(BUDAPEST).strftime("%Y-%m-%d %H:%M %Z")
    secs = (dt_utc - now_utc).total_seconds()
    if secs <= 0:
        rel = "now"
    else:
        days = int(secs // 86400)
        hours = int((secs % 86400) // 3600)
        minutes = int((secs % 3600) // 60)
        if days > 0:
            rel = f"in {days} days {hours} hours"
        elif hours > 0:
            rel = f"in {hours} hours {minutes} minutes"
        else:
            rel = f"in {minutes} minutes"
    return local_str, rel


def _parse_iso_to_epoch(value):
    """Parse an ISO-8601 string (as returned by the Claude usage endpoint's
    resets_at fields) into a unix epoch float. Returns None on failure."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def hours_until(resets_at, now_utc=None):
    if resets_at is None:
        return None
    try:
        dt_utc = datetime.fromtimestamp(int(resets_at), tz=timezone.utc)
    except (ValueError, OSError, OverflowError, TypeError):
        return None
    now_utc = now_utc or datetime.now(timezone.utc)
    return (dt_utc - now_utc).total_seconds() / 3600.0


# --------------------------------------------------------------------------
# Pace model -- time-proportional usage vs. elapsed-window comparison,
# shared by every window of every provider.
# --------------------------------------------------------------------------

def compute_pace(used_percent, resets_at, window_len_sec, now_utc=None):
    """Return a dict describing usage pace for one window, or None if the
    inputs aren't usable (missing percent/resets_at/window length).

    Keys:
      elapsed_fraction          0..1, how much of the window has passed
      used_fraction             0..1, utilization/100
      pace_ratio                used_fraction/elapsed_fraction, or None if
                                 elapsed_fraction is ~0 (guard div-by-zero)
      time_remaining_sec        resets_at - now, seconds (may be negative)
      resets_at                 float epoch, passed through for convenience
      projected_exhaustion_epoch
                                 at the current burn rate, when used_fraction
                                 would hit 1.0 -- None if used_fraction is 0
                                 or elapsed is too small to extrapolate from
    """
    if not isinstance(used_percent, (int, float)):
        return None
    if resets_at is None or not window_len_sec:
        return None
    try:
        resets_dt = datetime.fromtimestamp(float(resets_at), tz=timezone.utc)
    except (ValueError, OSError, OverflowError, TypeError):
        return None

    now_utc = now_utc or datetime.now(timezone.utc)
    time_remaining_sec = (resets_dt - now_utc).total_seconds()
    elapsed_sec_raw = window_len_sec - time_remaining_sec
    elapsed_fraction = max(0.0, min(1.0, elapsed_sec_raw / window_len_sec))
    elapsed_sec = elapsed_fraction * window_len_sec
    used_fraction = max(0.0, used_percent / 100.0)

    pace_ratio = None
    if elapsed_fraction > 1e-6:
        pace_ratio = used_fraction / elapsed_fraction

    projected_exhaustion_epoch = None
    if elapsed_sec > 1e-6 and used_fraction > 0:
        burn_per_sec = used_fraction / elapsed_sec
        if burn_per_sec > 0:
            projected_exhaustion_epoch = now_utc.timestamp() + (1.0 - used_fraction) / burn_per_sec

    return {
        "elapsed_fraction": elapsed_fraction,
        "used_fraction": used_fraction,
        "pace_ratio": pace_ratio,
        "time_remaining_sec": time_remaining_sec,
        "resets_at": float(resets_at),
        "projected_exhaustion_epoch": projected_exhaustion_epoch,
    }


def _window_length_seconds(scope_key, window_data):
    """scope_key looks like 'claude_five_hour' or 'codex_weekly'."""
    if scope_key.startswith("claude_"):
        return CLAUDE_WINDOW_SECONDS.get(scope_key[len("claude_"):])
    if scope_key.startswith("codex_"):
        wm = window_data.get("window_minutes") if window_data else None
        return wm * 60 if isinstance(wm, (int, float)) else None
    return None


def _iter_pace_windows(snapshot):
    """Yield (scope_key, window_dict, window_len_sec) for every window with
    a usable length, across both providers -- but only from a source worth
    alerting on (authoritative / authoritative_cached / authoritative_statusline
    for Claude, authoritative for Codex). Never yields from the token-count
    estimate."""
    claude = snapshot.get("claude") or {}
    if claude.get("ok") and claude.get("source") in ("authoritative", "authoritative_cached", "authoritative_statusline"):
        for key, w in (claude.get("windows") or {}).items():
            scope_key = f"claude_{key}"
            length = _window_length_seconds(scope_key, w)
            if length:
                yield scope_key, w, length

    codex = snapshot.get("codex") or {}
    if codex.get("ok") and codex.get("source") == "authoritative":
        for key, w in (codex.get("windows") or {}).items():
            scope_key = f"codex_{key}"
            length = _window_length_seconds(scope_key, w)
            if length:
                yield scope_key, w, length


# --------------------------------------------------------------------------
# Codex CLI collector -- authoritative only
# --------------------------------------------------------------------------

def collect_codex():
    result = {"provider": "codex", "source": "authoritative", "ok": True}
    try:
        files = glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl"))
        if not files:
            raise FileNotFoundError("no ~/.codex/sessions rollout files found")
        newest = max(files, key=os.path.getmtime)

        rate_limits = None
        with open(newest, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"rate_limits"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rl = obj.get("payload", {}).get("rate_limits")
                if rl:
                    rate_limits = rl  # keep the LAST match

        if rate_limits is None:
            raise ValueError("no rate_limits entries in newest rollout file")

        windows = {}
        for key in ("primary", "secondary"):
            w = rate_limits.get(key)
            if not w:
                continue
            wm = w.get("window_minutes")
            if wm == 10080:
                label = "weekly"
            elif wm == 300:
                label = "five_hour"
            else:
                label = f"window_{wm}"
            windows[label] = {
                "used_percent": w.get("used_percent"),
                "window_minutes": wm,
                "resets_at": w.get("resets_at"),
            }

        result["plan_type"] = rate_limits.get("plan_type")
        result["windows"] = windows
        result["source_file"] = newest
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
    return result


# --------------------------------------------------------------------------
# Claude collector -- authoritative endpoint, else local-transcript estimate
# --------------------------------------------------------------------------

def _read_keychain_token():
    """macOS only: Claude Code keeps its OAuth credentials in the login
    Keychain, NOT in ~/.claude/.credentials.json (that file simply does not
    exist on darwin). Returns the access token or None. Never logs the value.

    Failures are silent by design: on a headless/launchd run the `security`
    call can be denied or block on a GUI prompt, so it is bounded by a
    timeout and falls through to the next source instead of raising."""
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout.strip())
    except Exception:
        return None
    oauth = data.get("claudeAiOauth") or {}
    return oauth.get("accessToken") or data.get("accessToken") or None


def _read_claude_token():
    """Return (token, token_source) or (None, None). Never logs the value.

    Source order matters: the long-lived `claude setup-token` value that
    typically lands in the .env file is NOT accepted by the oauth/usage
    endpoint (it answers 403), while the session token from the Keychain /
    credentials file is. Cheapest authoritative source first, .env last.

    2026-09-01: added the fleet setup-token file (store/.claude-oauth-token,
    see src/web/agent-process.ts FLEET_OAUTH_TOKEN_PATH / claude-credentials-
    guard.ts) as a fallback BEFORE the .env check. Root cause of needing this
    fallback: the shared ~/.claude/.credentials.json this function checks
    first can go empty/dead (rotating-token race, see claude-credentials-
    guard.ts header) while the separate fleet token stays valid for actual
    inference -- exactly the case that motivated per-agent config isolation.

    CONFIRMED ROOT CAUSE (2026-09-01, kanban "Pontos usage-szazalek" card):
    this is not a transient rate-limit and not account-specific. `claude
    setup-token`'s OAuth authorization request hardcodes `inferenceOnly:
    true` -- confirmed by extracting strings from the installed Claude Code
    binary itself (bin/claude.exe): the client's own log/UI text reads
    "env-var and setup-token sessions default to user:inference only", and
    CLAUDE_AI_OAUTH_SCOPES (the scope set the interactive /login flow
    requests) separately lists user:profile -- a scope setup-token never
    asks for. /api/oauth/usage requires user:profile and 403s
    ("permission_error", "OAuth token does not meet scope requirement
    user:profile") for any token that lacks it, setup-token-minted or not.
    Independently corroborated by 8+ open reports on the anthropics/
    claude-code GitHub tracker (e.g. #22450, #21328, #23703) describing the
    identical symptom back to the same OAuth authorization URL
    (`scope=user%3Ainference`); one reporter confirms this is a deliberate
    tightening (setup-token used to request broader scope in an earlier CLI
    release), not an always-existing limitation.

    A 429 (seen 2026-09-01, `x-should-retry: false`) can ALSO show up on
    the same token/endpoint pair -- that is Anthropic's edge throttling
    layered on top of repeated polling, a separate symptom of the same
    restricted access pattern, not a competing explanation for the 403.

    Practical consequence, unchanged: there is no supported way to mint a
    long-lived, headless-suitable token that also carries user:profile --
    the 1-year TTL and inferenceOnly appear to be bundled by Anthropic's own
    authorization server, not something this fleet's token-handling code can
    separate. This fallback is kept because the fleet token IS valid for its
    real purpose (inference), just never for this specific endpoint; the
    5-hour/weekly % figures stay in the "estimate" fallback (a raw local
    token-count, not a true percentage -- the actual plan limit is not
    knowable locally either) until a real interactive session token exists
    in ~/.claude/.credentials.json again (which reintroduces the original
    rotating-token fragility -- a real tradeoff, not a bug to chase further
    here)."""
    cred_path = os.path.expanduser("~/.claude/.credentials.json")
    if os.path.exists(cred_path):
        try:
            with open(cred_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            oauth = data.get("claudeAiOauth") or {}
            token = oauth.get("accessToken") or data.get("accessToken")
            if token:
                return token, "credentials_file"
        except Exception:
            pass

    keychain_token = _read_keychain_token()
    if keychain_token:
        return keychain_token, "keychain"

    fleet_token_path = os.path.join(REPO_ROOT, "store", ".claude-oauth-token")
    if os.path.exists(fleet_token_path):
        try:
            with open(fleet_token_path, "r", encoding="utf-8") as f:
                token = f.read().strip()
            if token:
                return token, "fleet_token_file"
        except Exception:
            pass

    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if token:
                            return token, "env_file"
        except Exception:
            pass

    return None, None


def _claude_cli_version():
    try:
        out = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return out.split()[0] if out else "unknown"
    except Exception:
        return "unknown"


def _read_statusline_cache(max_age_minutes, max_age_overrides=None):
    """Return (windows_dict, age_minutes) from a dedicated tracker session's
    statusLine export (statusline-usage-export.py), or (None, None) if it
    doesn't exist, is unparseable, or either required window is missing/
    invalid/individually stale.

    max_age_overrides: optional {window_key: minutes} dict. Both primary
    windows currently use the same 25-minute direct-collector horizon. The
    weekly statusline percentage may be unchanged longer, making this an
    intentionally rare fallback, not a tracker-health error.

    This exists because /api/oauth/usage 403s for any setup-token-scoped
    fleet token (see docs/usage-percent-oauth-scope-root-cause-20260901.md)
    -- there is no legitimate way to widen that token's scope. Claude Code's
    own statusLine feature receives the SAME five_hour/seven_day utilization
    numbers directly in the JSON it feeds a configured status-line script,
    sourced from the session's own normal inference responses, not the
    restricted endpoint -- so a session with statusLine wired up (a
    DEDICATED, non-critical tracker session only; see
    docs/statusline-usage-tracker-dev-spec-20260901.md for why not any
    fleet-critical session) sidesteps the scope problem entirely, no network
    call needed here at all.

    Checked after the direct header query in collect_claude().

    PER-WINDOW freshness (Codex review round 3, 2026-09-01, confirmed by
    BÉLA independently re-reading the code): an earlier version checked one
    FILE-level collected_at_unix for the whole snapshot. That let a stale
    window silently pass as fresh: statusline-usage-export.py merges
    partial payloads (only one primary window validly parsed in a given
    stdin invocation is normal, not an edge case), and a shared timestamp
    meant writing a genuinely-new five_hour value alongside a carried-over,
    actually-old seven_day value stamped BOTH as "just written". Each
    window now carries its own collected_at_unix (written by
    statusline-usage-export.py); this function checks each REQUIRED
    window's own age independently -- a window individually older than
    max_age_minutes fails the whole cache, exactly as if it were missing.
    The returned age_minutes is the MAX (staler) of the two required
    windows' ages -- the honest, conservative number to surface, not the
    fresher one."""
    if not os.path.exists(STATUSLINE_CACHE_PATH):
        return None, None
    try:
        with open(STATUSLINE_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, None

    windows = data.get("windows")
    if not isinstance(windows, dict) or not windows:
        return None, None

    now_ts = datetime.now(timezone.utc).timestamp()
    ages = []
    # A statusLine payload can legitimately carry only ONE of the two
    # primary windows in a given invocation (the stdin JSON's rate_limits
    # block fills in as the session's turns happen, not atomically) --
    # trusting a partial/stale snapshot as "authoritative" risks silently
    # under-reporting (e.g. showing a fine 5-hour number while the weekly
    # window is simply absent or stale, not actually fine). Require BOTH
    # five_hour AND seven_day to be present with a real numeric percentage
    # AND individually fresh before this source is trusted at all;
    # otherwise fall through to the network/cached/estimate chain same as
    # if the cache didn't exist. Never synthesize a 0% for a missing/
    # invalid window -- that reads as "no usage" instead of "unknown",
    # which is worse than admitting we don't know.
    for required_key in ("five_hour", "seven_day"):
        w = windows.get(required_key)
        if not isinstance(w, dict):
            return None, None
        pct = w.get("used_percent")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool) or not (0 <= pct <= 100):
            return None, None
        collected_at = w.get("collected_at_unix")
        if not isinstance(collected_at, (int, float)):
            return None, None
        age_minutes = (now_ts - collected_at) / 60.0
        window_ceiling = max_age_minutes
        if isinstance(max_age_overrides, dict) and required_key in max_age_overrides:
            window_ceiling = max_age_overrides[required_key]
        if age_minutes < 0 or age_minutes > window_ceiling:
            return None, None
        ages.append(age_minutes)

    return windows, max(ages)


def _parse_claude_usage_headers(headers):
    """Return quota windows from Messages response headers, or an error.

    Anthropic includes these headers on a 429 too; at the important boundary
    the HTTP status must therefore not discard otherwise valid measurements.
    """
    def header_number(name):
        try:
            value = float(headers.get(name))
        except (AttributeError, TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    windows = {}
    for key, header_key in (("five_hour", "5h"), ("seven_day", "7d")):
        utilization = header_number(f"anthropic-ratelimit-unified-{header_key}-utilization")
        resets_at = header_number(f"anthropic-ratelimit-unified-{header_key}-reset")
        # A slight value above one is a server-side rounding/saturation value,
        # not a reason to discard the result. Negative/non-finite values are
        # malformed and fail conservatively.
        if utilization is None or utilization < 0 or resets_at is None or resets_at <= 0:
            return None, f"messages response missing or invalid {header_key} usage headers"
        windows[key] = {
            "used_percent": round(min(utilization, 1.0) * 100.0, 2),
            "resets_at": resets_at,
        }
    return windows, None


def _collect_claude_usage_headers():
    """Read Claude quota utilization from a minimal Messages API response.

    The usage is carried in response headers. Error text deliberately omits
    both response bodies and credentials.
    """
    token, token_source = _read_claude_token()
    if not token:
        return None, "no oauth token available", "no_token"

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1,
        "system": [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}],
        "messages": [{"role": "user", "content": "hi"}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
            "User-Agent": f"claude-code/{_claude_cli_version()}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            headers = resp.headers
            resp.read()
    except urllib.error.HTTPError as e:
        windows, parse_error = _parse_claude_usage_headers(e.headers)
        if windows is not None:
            return windows, None, None
        kind = "permanent" if e.code in (401, 403) else "transient"
        return None, f"messages HTTP {e.code} ({token_source} token): {parse_error}", kind
    except Exception as e:
        return None, f"messages {type(e).__name__}", "transient"
    windows, parse_error = _parse_claude_usage_headers(headers)
    if windows is None:
        return None, parse_error, "transient"
    return windows, None, None


def _collect_claude_authoritative():
    """Return (windows_dict, None, None) on success.
    On failure: (None, error_str, error_kind) where error_kind is one of:
      'no_token'  -- no credentials at all, never worth a cache/retry
      'permanent' -- HTTP 403 (real scope/auth problem, not transient)
      'transient' -- 429 / 5xx / timeout / network error / unparseable
                     response -- safe to serve from cache if fresh enough
    """
    token, token_source = _read_claude_token()
    if not token:
        return None, "no oauth token available (no credentials.json, no keychain entry, no .env token)", "no_token"

    version = _claude_cli_version()
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": f"claude-code/{version}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        kind = "permanent" if e.code in (401, 403) else "transient"
        return None, f"HTTP {e.code} ({token_source} token)", kind
    except Exception as e:
        return None, type(e).__name__, "transient"

    # Windows are TOP-LEVEL keys of the response (not nested under
    # "rate_limits"), e.g. {"five_hour": {...}, "seven_day": {...}, ...,
    # "seven_day_oauth_apps": ..., "spend": ..., "limits": ...}. A window
    # can be null (e.g. seven_day_opus/seven_day_sonnet on non-tiered
    # accounts) -- skip those gracefully. resets_at is an ISO-8601 string,
    # not a unix epoch; used_percent comes from "utilization".
    windows = {}
    for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
        w = data.get(key)
        if not w:
            continue
        used = w.get("utilization")
        if used is None:
            used = w.get("used_percentage")  # tolerate older/alt shapes
        resets_raw = w.get("resets_at")
        resets_epoch = resets_raw if isinstance(resets_raw, (int, float)) else _parse_iso_to_epoch(resets_raw)
        windows[key] = {"used_percent": used, "resets_at": resets_epoch}

    # The "Fable only" weekly limit is NOT a top-level window here
    # (seven_day_opus stays null on this account); the app reads it from the
    # "limits" array as a group=="weekly" entry scoped to model display_name
    # "Fable". Surface it under seven_day_opus so it reuses the Fable weekly
    # name + 7-day window length + pace alerting.
    if not windows.get("seven_day_opus"):
        for lim in (data.get("limits") or []):
            if not isinstance(lim, dict):
                continue
            scope = lim.get("scope") if isinstance(lim.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            name = (model.get("display_name") or "").lower()
            if lim.get("group") == "weekly" and name == "fable" and lim.get("percent") is not None:
                resets_raw = lim.get("resets_at")
                resets_epoch = resets_raw if isinstance(resets_raw, (int, float)) else _parse_iso_to_epoch(resets_raw)
                windows["seven_day_opus"] = {"used_percent": lim.get("percent"), "resets_at": resets_epoch}
                break

    if not windows:
        return None, "authoritative response had no usable rate_limits windows", "transient"
    return windows, None, None


def _read_cached_claude_authoritative(max_age_minutes, now_utc=None):
    """Return (windows_dict, age_minutes) from the last snapshot written to
    store/usage-latest.json IF its Claude entry was authoritative (or itself
    already authoritative_cached) and younger than max_age_minutes.
    Returns (None, None) if there's no usable cache."""
    if not os.path.exists(LATEST_PATH):
        return None, None
    try:
        with open(LATEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, None

    claude = data.get("claude") or {}
    if claude.get("source") not in ("authoritative", "authoritative_cached", "authoritative_statusline"):
        return None, None
    windows = claude.get("windows")
    if not windows:
        return None, None

    now_utc = now_utc or datetime.now(timezone.utc)
    collected_at = claude.get("authoritative_collected_at_unix")
    if collected_at is not None:
        if not isinstance(collected_at, (int, float)) or isinstance(collected_at, bool) or not math.isfinite(collected_at):
            return None, None
        age_minutes = (now_utc.timestamp() - collected_at) / 60.0
    else:
        # On the first post-migration run a legacy snapshot has no upstream
        # collection timestamp, so use generated_at once for compatibility.
        generated_at = data.get("generated_at")
        if not generated_at:
            return None, None
        try:
            gen_dt = datetime.fromisoformat(generated_at)
        except (ValueError, TypeError):
            return None, None
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=timezone.utc)
        age_minutes = (now_utc - gen_dt).total_seconds() / 60.0
    if age_minutes < 0 or age_minutes > max_age_minutes:
        return None, None
    return windows, age_minutes


def _collect_claude_estimate():
    now = datetime.now(timezone.utc)
    five_h_cutoff = now - timedelta(hours=5)
    seven_d_cutoff = now - timedelta(days=7)

    five_hour_tokens = 0
    seven_day_tokens = 0
    seven_day_opus_tokens = 0
    files_scanned = 0

    files = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    for path in files:
        files_scanned += 1
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"type":"assistant"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "assistant":
                        continue
                    ts_raw = obj.get("timestamp")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts < seven_d_cutoff:
                        continue

                    msg = obj.get("message") or {}
                    usage = msg.get("usage") or {}
                    total = sum(int(usage.get(k) or 0) for k in TOKEN_FIELDS)
                    if total == 0:
                        continue

                    seven_day_tokens += total
                    model = (msg.get("model") or "").lower()
                    if "opus" in model:
                        seven_day_opus_tokens += total
                    if ts >= five_h_cutoff:
                        five_hour_tokens += total
        except Exception:
            continue

    return {
        "five_hour_est_tokens": five_hour_tokens,
        "seven_day_est_tokens": seven_day_tokens,
        "seven_day_opus_est_tokens": seven_day_opus_tokens,
        "files_scanned": files_scanned,
    }


def collect_claude():
    result = {"provider": "claude", "source": "estimate", "ok": True}

    headers_enabled = _claude_usage_headers_enabled()
    if headers_enabled:
        windows, err, err_kind = _collect_claude_usage_headers()
        if windows is not None:
            result["source"] = "authoritative"
            result["windows"] = windows
            result["authoritative_collected_at_unix"] = datetime.now(timezone.utc).timestamp()
            return result
        errors = [err] if err else []
        saw_transient = err_kind == "transient"
    else:
        # Complete v1-compatible order: statusline first with its historical
        # per-window ceilings, then the legacy endpoint and its fallbacks.
        sl_windows, sl_age = _read_statusline_cache(
            LEGACY_STATUSLINE_CACHE_MAX_AGE_MIN,
            LEGACY_STATUSLINE_CACHE_MAX_AGE_MIN_BY_WINDOW,
        )
        if sl_windows is not None:
            result["source"] = "authoritative_statusline"
            result["windows"] = sl_windows
            result["statusline_age_min"] = round(sl_age, 1)
            result["authoritative_collected_at_unix"] = datetime.now(timezone.utc).timestamp() - (sl_age * 60)
            return result
        errors = []
        saw_transient = False
        windows, legacy_err, legacy_kind = _collect_claude_authoritative()
        if windows is not None:
            result["source"] = "authoritative"
            result["windows"] = windows
            result["authoritative_collected_at_unix"] = datetime.now(timezone.utc).timestamp()
            return result
        if legacy_err:
            errors.append(legacy_err)
        saw_transient = saw_transient or legacy_kind == "transient"

    result["auth_error"] = "; ".join(errors)

    if headers_enabled:
        # The tracker export remains fallback-only after the direct query.
        sl_windows, sl_age = _read_statusline_cache(
            CONFIG["claude_statusline_cache_max_age_min"],
            CONFIG.get("claude_statusline_cache_max_age_min_by_window"),
        )
        if sl_windows is not None:
            result["source"] = "authoritative_statusline"
            result["windows"] = sl_windows
            result["statusline_age_min"] = round(sl_age, 1)
            result["authoritative_collected_at_unix"] = datetime.now(timezone.utc).timestamp() - (sl_age * 60)
            return result

    # A transient failure (429/5xx/timeout/unparseable) must not throw away
    # real numbers -- reuse the last authoritative snapshot if it's fresh
    # enough. A missing token or a 403 (real scope problem) skips the cache
    # and goes straight to the estimate.
    if saw_transient:
        cached_windows, age_minutes = _read_cached_claude_authoritative(
            CONFIG["claude_authoritative_cache_max_age_min"] if headers_enabled else LEGACY_AUTHORITATIVE_CACHE_MAX_AGE_MIN
        )
        if cached_windows is not None:
            result["source"] = "authoritative_cached"
            result["windows"] = cached_windows
            result["cache_age_minutes"] = round(age_minutes, 1)
            result["authoritative_collected_at_unix"] = datetime.now(timezone.utc).timestamp() - (age_minutes * 60)
            return result

    try:
        result.update(_collect_claude_estimate())
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
    return result


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------

def build_snapshot():
    now = datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "generated_at_local": now.astimezone(BUDAPEST).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "codex": collect_codex(),
        "claude": collect_claude(),
    }


# --------------------------------------------------------------------------
# Human-readable summary
# --------------------------------------------------------------------------

def _format_pace_suffix(pace):
    """' | pace: 1.23x (elapsed: NN%)' -- shown even when nothing is
    alert-worthy, so the log/dashboard stays informative."""
    if pace is None:
        return ""
    elapsed_pct = pace["elapsed_fraction"] * 100
    if pace["pace_ratio"] is None:
        return f" | pace: n/a (elapsed: {elapsed_pct:.0f}%, too early)"
    return f" | pace: {pace['pace_ratio']:.2f}x (elapsed: {elapsed_pct:.0f}%)"


def render_summary(snapshot):
    lines = []
    lines.append(f"Quota status -- {snapshot['generated_at_local']}")
    lines.append("")

    codex = snapshot["codex"]
    lines.append("Codex (Codex CLI):")
    if not codex.get("ok"):
        lines.append(f"  error: {codex.get('error')}")
    else:
        plan = codex.get("plan_type") or "?"
        windows = codex.get("windows", {})
        if not windows:
            lines.append("  no rate_limits data available")
        for label, w in windows.items():
            local_str, rel = fmt_reset(w.get("resets_at"))
            pct = w.get("used_percent")
            pct_str = f"{pct:.1f}%" if isinstance(pct, (int, float)) else "?"
            name = {"weekly": "weekly", "five_hour": "5-hour"}.get(label, label)
            pace = compute_pace(pct, w.get("resets_at"), _window_length_seconds(f"codex_{label}", w))
            lines.append(
                f"  {name} limit: {pct_str} used (plan: {plan}, reset: {local_str}, {rel})"
                f"{_format_pace_suffix(pace)}"
            )

    lines.append("")
    claude = snapshot["claude"]
    lines.append(f"Claude (source: {claude.get('source')}):")
    if not claude.get("ok"):
        lines.append(f"  error: {claude.get('error')}")
    elif claude.get("source") in ("authoritative", "authoritative_cached", "authoritative_statusline"):
        if claude.get("source") == "authoritative_cached":
            age = claude.get("cache_age_minutes")
            age_str = f"{age:.0f}" if isinstance(age, (int, float)) else "?"
            lines.append(f"  (from cache, {age_str} min ago -- live endpoint error: {claude.get('auth_error')})")
        elif claude.get("source") == "authoritative_statusline":
            age = claude.get("statusline_age_min")
            age_str = f"{age:.1f}" if isinstance(age, (int, float)) else "?"
            lines.append(f"  (from dedicated tracker session's statusLine export, {age_str} min ago)")
        windows = claude.get("windows", {})
        names = {
            "five_hour": "5-hour",
            "seven_day": "weekly",
            "seven_day_opus": "weekly (Opus / Fable-tier)",
            "seven_day_sonnet": "weekly (Sonnet)",
        }
        for label, w in windows.items():
            local_str, rel = fmt_reset(w.get("resets_at"))
            pct = w.get("used_percent")
            pct_str = f"{pct:.1f}%" if isinstance(pct, (int, float)) else "?"
            pace = compute_pace(pct, w.get("resets_at"), _window_length_seconds(f"claude_{label}", w))
            lines.append(
                f"  {names.get(label, label)} limit: {pct_str} used (reset: {local_str}, {rel})"
                f"{_format_pace_suffix(pace)}"
            )
    else:
        if claude.get("auth_error"):
            lines.append(f"  (authoritative endpoint unavailable: {claude['auth_error']})")
        lines.append(f"  5-hour estimated token volume: {claude.get('five_hour_est_tokens', '?')}")
        lines.append(f"  weekly estimated token volume: {claude.get('seven_day_est_tokens', '?')}")
        lines.append(f"  weekly Opus estimated token volume: {claude.get('seven_day_opus_est_tokens', '?')}")
        lines.append(f"  ({claude.get('files_scanned', 0)} transcript files scanned, no %/reset info from estimate)")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Alerts + de-dup
# --------------------------------------------------------------------------

def _load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    os.makedirs(STORE_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _should_fire(state, key, condition, kind, now_ts, refire_seconds=None):
    """Generic de-dup gate. kind: 'cross' | 'cross_refire' | 'throttle'."""
    entry = state.get(key, {"active": False, "last_fired": None})
    fire = False

    if condition:
        if kind == "cross":
            fire = not entry["active"]
        elif kind == "cross_refire":
            if not entry["active"]:
                fire = True
            elif entry["last_fired"] is not None and (now_ts - entry["last_fired"]) >= refire_seconds:
                fire = True
        elif kind == "throttle":
            fire = entry["last_fired"] is None or (now_ts - entry["last_fired"]) >= refire_seconds
        entry["active"] = True
        if fire:
            entry["last_fired"] = now_ts
    else:
        entry["active"] = False
        entry["last_fired"] = None

    state[key] = entry
    return fire


def compute_alerts(snapshot, state, now_utc=None):
    """Time-proportional pace alerts for every window of every provider.

    For each window: compare used_fraction to elapsed_fraction (pace_ratio).
    Below pace_min_elapsed_fraction it's too early in the window to judge --
    stay silent. Otherwise:
      - OVERUSE: pace_ratio >= pace_over_ratio AND, extrapolated at the
        current burn rate, the window would run out before it resets.
      - UNDERUSE: pace_ratio <= pace_under_ratio (plenty of unused quota).
        Only meaningful for weekly-class windows (>= underuse_min_window_sec,
        default 24h) -- unused quota is actually lost at reset there. Short
        windows like Claude's 5h reset constantly, so under-pace is the
        normal resting state, not a signal; UNDERUSE is skipped for them.
      - near-exhaustion backstop: used_fraction >= hard_near_exhaustion,
        independent of pace, so we never silently hit 100%.
    OVERUSE and the near-exhaustion backstop apply to every window,
    including short ones (e.g. "you'll run out of your 5h window in ~2h
    at this pace" is exactly the kind of early warning that's wanted).
    Only evaluated on authoritative/authoritative_cached sources -- never
    on the token-count estimate (compute_pace has no %/reset to work with
    there anyway, and _iter_pace_windows already excludes it).
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    now_ts = now_utc.timestamp()
    alerts = []

    for scope_key, window, window_len_sec in _iter_pace_windows(snapshot):
        pace = compute_pace(window.get("used_percent"), window.get("resets_at"), window_len_sec, now_utc)
        if pace is None:
            continue

        underuse_eligible = window_len_sec >= CONFIG["underuse_min_window_sec"]
        name = ALERT_WINDOW_NAMES.get(scope_key, scope_key)
        used_pct = pace["used_fraction"] * 100
        elapsed_pct = pace["elapsed_fraction"] * 100
        pace_ratio = pace["pace_ratio"]
        reset_local, reset_rel = fmt_reset(pace["resets_at"], now_utc)

        # --- BACKSTOP: near-exhaustion, independent of pace/elapsed ---
        near_cond = pace["used_fraction"] >= CONFIG["hard_near_exhaustion"]
        if _should_fire(state, f"{scope_key}_near_exhaustion", near_cond, "cross_refire", now_ts,
                         refire_seconds=CONFIG["near_exhaustion_refire_minutes"] * 60):
            alerts.append(f"ALERT: {name} nearly exhausted ({used_pct:.0f}%), reset {reset_local}.")

        # Too early in the window to judge pace meaningfully -- skip
        # OVER/UNDER, but reset their dedup state so a genuine crossing
        # right after the guard lifts (or in the next window cycle) fires
        # as a fresh crossing instead of staying suppressed by stale state.
        if pace_ratio is None or pace["elapsed_fraction"] < CONFIG["pace_min_elapsed_fraction"]:
            _should_fire(state, f"{scope_key}_over", False, "cross_refire", now_ts)
            if underuse_eligible:
                _should_fire(state, f"{scope_key}_under", False, "throttle", now_ts)
            continue

        # --- OVERUSE: over-pace AND projected to exhaust before the reset ---
        proj_epoch = pace.get("projected_exhaustion_epoch")
        cond_over = (
            pace_ratio >= CONFIG["pace_over_ratio"]
            and proj_epoch is not None
            and proj_epoch < pace["resets_at"]
        )
        if _should_fire(state, f"{scope_key}_over", cond_over, "cross_refire", now_ts,
                         refire_seconds=CONFIG["pace_over_refire_minutes"] * 60):
            proj_local, _ = fmt_reset(proj_epoch, now_utc)
            alerts.append(
                f"ALERT: OVERUSE: {name} {used_pct:.0f}% used at {elapsed_pct:.0f}% elapsed "
                f"(pace {pace_ratio:.1f}x). At this pace it will run out around {proj_local} (reset {reset_local})."
            )

        # --- UNDERUSE: plenty of unused quota, worth spending (weekly-class windows only) ---
        if underuse_eligible:
            cond_under = pace_ratio <= CONFIG["pace_under_ratio"]
            if _should_fire(state, f"{scope_key}_under", cond_under, "throttle", now_ts,
                             refire_seconds=CONFIG["pace_under_refire_hours"] * 3600):
                alerts.append(
                    f"ALERT: UNDERUSE: {name} only {used_pct:.0f}% at {elapsed_pct:.0f}% elapsed "
                    f"(pace {pace_ratio:.1f}x) -- plenty of unused quota, worth starting a big project now "
                    f"(reset {reset_local}, {reset_rel})."
                )

    return alerts


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _run():
    parser = argparse.ArgumentParser(description="Subscription usage/quota monitor")
    parser.add_argument("--json", action="store_true", help="print only the snapshot JSON, no alerts/history")
    args = parser.parse_args()

    snapshot = build_snapshot()

    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return

    print(render_summary(snapshot))

    state = _load_state()
    alerts = compute_alerts(snapshot, state)
    _save_state(state)
    for line in alerts:
        print(line)

    os.makedirs(STORE_DIR, exist_ok=True)
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    tmp_latest = LATEST_PATH + ".tmp"
    with open(tmp_latest, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    os.replace(tmp_latest, LATEST_PATH)


def main():
    try:
        _run()
    except Exception as e:
        print(f"usage-collect.py: unhandled error, degrading silently: {e}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()

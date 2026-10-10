#!/usr/bin/env python3
"""
usage-monitor heartbeat short-circuit check (kanban 240e6747, 2026-09-12).

The usage-monitor scheduled task (cron 0,30 * * * *) re-reads
store/usage-latest.json and re-evaluates the pause/stop thresholds on EVERY
run -- even when nothing about the underlying usage state or pause status
has changed since the previous run. This script gives that heartbeat's
USAGE-SPECIFIC logic (NOT the stuck-menu scan -- see the note below) a
cheap, deterministic early-exit signal.

IMPORTANT DEVIATION FROM THE LITERAL REQUEST #1, documented per KISPROGI's
own "say so, don't silently build the wrong thing" rule: the request as
phrased ("ha a generated_at mezo NEM valtozott") would compare
usage-latest.json's own `generated_at` timestamp VALUE. That field is
unconditionally re-stamped to `now` by scripts/usage-collect.py's
build_snapshot() on EVERY run of its own 10-minute cron, REGARDLESS of
whether the underlying quota numbers moved. Since this heartbeat runs every
30 minutes (6x the collector's own cadence), generated_at is virtually
guaranteed to differ between any two consecutive heartbeat runs -- comparing
it verbatim would make this short-circuit a permanent no-op. This script
instead compares the MEANINGFUL fields: claude.source, both window
used_percent/resets_at values, a normalized view of store/.usage-fleet-pause,
AND (Codex review, 2026-09-12, 1st round) a FRESHNESS CLASS derived from
the upstream authoritative_collected_at_unix when present (otherwise
generated_at; fresh: <=25min old, matching the collector's current
staleness threshold; stale: older; missing/invalid otherwise) -- the class,
never the raw timestamp. This closes the gap the first draft had: without
tracking freshness at all, a FROZEN collector (crashed but its last output
file still sitting there with old-but-otherwise-unchanged numbers) would
short-circuit FOREVER and the heartbeat's own stale-episode handling (which
depends on noticing >25min staleness) would never get to run even once.

IMPORTANT DEVIATION #2 (Codex review, 2026-09-12, 1st round): the original
draft of this short-circuit also gated the heartbeat's STUCK-MENU tmux scan
behind the same skip/proceed decision. That scan is a separate, explicitly
"kotelezo" (mandatory) safety control introduced 2026-07-24 after a real
incident (an agent stuck in an interactive menu for hours undetected) and
widened to full fleet coverage 2026-09-08 after a SECOND such incident. It
must not silently start skipping runs as a side effect of a usage-tracking
optimization. The scan has been extracted into its own, always-running
scheduled task ("stuck-menu-scan") and is no longer part of usage-monitor's
prompt at all -- this script's skip/proceed verdict now only ever affects
the usage-specific portion of that heartbeat.

Prints exactly one line to stdout:
  USAGE_MONITOR_SKIP    -- nothing meaningful changed since the last run;
                           the heartbeat prompt should stop here, no further
                           tool calls for the usage-tracking logic.
  USAGE_MONITOR_PROCEED -- something changed (or this is the first run ever,
                           or the state could not be read/written) -- run
                           the full existing usage logic unchanged.

Always overwrites the last-check state file with what THIS run observed,
regardless of the verdict, so the NEXT run compares against it. Any
unexpected error (Codex review: e.g. an OSError writing the state file)
fails toward PROCEED, never toward a silent, unverifiable SKIP -- an
untracked exception must not look identical to "nothing changed".

Store paths default to the live checkout but can be overridden via env vars
for isolated testing (USAGE_MONITOR_LATEST_PATH / USAGE_MONITOR_PAUSE_PATH /
USAGE_MONITOR_LAST_CHECK_PATH) -- this script never needs a database
connection or network access, so it is trivially runnable against a scratch
copy of the three files without touching the live store/ at all.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STORE = Path("/home/kisss/marveen/store")

USAGE_LATEST_PATH = Path(os.environ.get("USAGE_MONITOR_LATEST_PATH", str(DEFAULT_STORE / "usage-latest.json")))
FLEET_PAUSE_PATH = Path(os.environ.get("USAGE_MONITOR_PAUSE_PATH", str(DEFAULT_STORE / ".usage-fleet-pause")))
LAST_CHECK_PATH = Path(os.environ.get("USAGE_MONITOR_LAST_CHECK_PATH", str(DEFAULT_STORE / ".usage-monitor-last-check.json")))

# Same threshold the original usage-monitor prompt itself uses for its own
# AG.1 ("kiesesi epizod") stale-source branch -- kept in sync deliberately,
# not re-derived, so the two can never silently drift apart.
# One missed ten-minute collection plus scheduler jitter remains acceptable.
STALE_THRESHOLD_SECONDS = 25 * 60

# Fields compared for the skip decision. generated_at itself is recorded in
# the state file for humans debugging this script, but MUST NOT be added
# here -- see the module docstring for why. `freshness` (derived from it) IS
# compared -- that is the whole point of tracking it separately.
COMPARE_KEYS = [
    "usage_latest_status",
    "freshness",
    "source",
    "five_hour_percent",
    "seven_day_percent",
    "five_hour_resets_at",
    "seven_day_resets_at",
    "pause_state",
]


def classify_freshness(generated_at_iso, authoritative_collected_at_unix, now):
    """fresh / stale / missing / invalid, preferring upstream data age."""
    if isinstance(authoritative_collected_at_unix, (int, float)) and not isinstance(authoritative_collected_at_unix, bool):
        age_seconds = now.timestamp() - authoritative_collected_at_unix
    else:
        if not generated_at_iso:
            return "missing"
        try:
            gen_dt = datetime.fromisoformat(generated_at_iso)
        except (ValueError, TypeError):
            return "invalid"
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=timezone.utc)
        age_seconds = (now - gen_dt).total_seconds()
    if age_seconds < 0:
        # Clock skew or a future timestamp -- do not trust it as "fresh".
        return "invalid"
    return "fresh" if age_seconds <= STALE_THRESHOLD_SECONDS else "stale"


def read_pause_state():
    """
    Normalized view of store/.usage-fleet-pause for comparison. Codex review
    (2026-09-12, 1st round, minor hardening): comparing the RAW file bytes is
    safe but over-eager -- a metadata-only rewrite of the same logical state
    (e.g. a "since" field, or key ordering) would force an unnecessary
    PROCEED. Parse it as the documented JSON shape and compare only the
    fields that matter to the actual pause/resume decision; fall back to the
    raw string for the legacy plain-text "paused" format (still explicitly
    supported by the original prompt) or anything else unparseable, which
    stays exact-match (conservative: an unrecognized shape always forces
    PROCEED rather than risk masking a real change).
    """
    try:
        raw = FLEET_PAUSE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"kind": "absent"}
    except OSError as e:
        return {"kind": "unreadable", "detail": str(e)}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"kind": "raw", "content": raw}
    if isinstance(parsed, dict):
        return {
            "kind": "json",
            "paused": parsed.get("paused"),
            "metric": parsed.get("metric"),
            "percent": parsed.get("percent"),
            "source": parsed.get("source"),
        }
    return {"kind": "raw", "content": raw}


def build_current_state(now):
    try:
        raw = json.loads(USAGE_LATEST_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # Missing/unreadable/corrupt is its own stable, comparable state.
        state = {
            "usage_latest_status": "unavailable",
            "freshness": "missing",
        }
    else:
        claude = raw.get("claude") or {}
        windows = claude.get("windows") or {}
        five = windows.get("five_hour") or {}
        seven = windows.get("seven_day") or {}
        generated_at = raw.get("generated_at")
        collected_at = claude.get("authoritative_collected_at_unix")
        state = {
            "usage_latest_status": "ok",
            "freshness": classify_freshness(generated_at, collected_at, now),
            "source": claude.get("source"),
            "five_hour_percent": five.get("used_percent"),
            "seven_day_percent": seven.get("used_percent"),
            "five_hour_resets_at": five.get("resets_at"),
            "seven_day_resets_at": seven.get("resets_at"),
            "generated_at": generated_at,  # diagnostics only, not in COMPARE_KEYS
            "authoritative_collected_at_unix": collected_at,  # diagnostics only
        }
    state["pause_state"] = read_pause_state()
    return state


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def run() -> str:
    now = datetime.now(timezone.utc)
    current = build_current_state(now)

    previous = None
    try:
        previous = json.loads(LAST_CHECK_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        previous = None

    unchanged = previous is not None and all(
        previous.get(k) == current.get(k) for k in COMPARE_KEYS
    )

    current["checked_at"] = int(time.time())  # diagnostics only, not in COMPARE_KEYS
    atomic_write_json(LAST_CHECK_PATH, current)

    return "USAGE_MONITOR_SKIP" if unchanged else "USAGE_MONITOR_PROCEED"


def main() -> int:
    # Codex review (2026-09-12, 1st round): an unexpected error (e.g. an
    # OSError writing the state file) must not surface as an uncaught
    # traceback with NO verdict line at all -- the calling prompt would have
    # nothing to key off. Fail toward PROCEED (run the full logic), never
    # toward a SKIP that cannot be verified.
    try:
        verdict = run()
    except Exception as e:
        # The verdict line stays exact-match-able on its own; the error
        # detail goes on a separate line so a caller matching purely on
        # "USAGE_MONITOR_PROCEED"/"USAGE_MONITOR_SKIP" is never confused by
        # trailing text, while the detail is still visible for debugging.
        print(f"error in short-circuit check itself, failing open: {e}", file=sys.stderr)
        print("USAGE_MONITOR_PROCEED")
        return 0
    print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())

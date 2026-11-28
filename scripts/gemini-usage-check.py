#!/usr/bin/env python3
"""Print one Gemini quota line, reading only the local usage snapshot."""
import sys
sys.dont_write_bytecode = True
import argparse
import importlib.util
import json
import os
import stat
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

_spec = importlib.util.spec_from_file_location("gemini_usage_collect", Path(__file__).with_name("gemini-usage-collect.py"))
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)


def rounded(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def display(value):
    value = rounded(value)
    return format(value, "f").rstrip("0").rstrip(".") if value % 1 else str(int(value))


def status(used):
    used = rounded(used)
    return "STOP" if used >= 90 else "FIGYELMEZTETÉS" if used >= 80 else "OK"


def reset_text(group, now):
    reset = group["resets_at"]
    if reset is None:
        return "nincs aktív ablak"
    return reset + (" (reset elmúlt)" if collector.parse_time(reset) <= now else "")


def format_snapshot(snapshot, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None or not isinstance(snapshot, dict):
        return "Gemini heti keret: ISMERETLEN (missing)"
    observed = collector.parse_time(snapshot.get("collectedAt"))
    groups = snapshot.get("groups")
    unrecognized = snapshot.get("unrecognizedRows", [])
    if (observed is None or not isinstance(groups, dict)
            or not set(collector.REQUIRED_GROUPS) <= set(groups) <= set(collector.KNOWN_GROUPS)
            or not all(collector.valid_group(v) for v in groups.values())
            or not collector.valid_unrecognized(unrecognized)):
        return "Gemini heti keret: ISMERETLEN (missing)"
    if not 0 <= (now - observed).total_seconds() <= collector.FRESH_SECONDS:
        return "Gemini heti keret: ISMERETLEN (stale)"
    gemini, other = groups["gemini"], groups["claude_gpt"]
    used = rounded(gemini["used_pct"])
    if gemini["resets_at"] is None:
        # agy reports epoch 0 when no window is in progress; that is only
        # consistent with an untouched quota.
        if used != 0:
            return "Gemini heti keret: ISMERETLEN (reset)"
    elif collector.parse_time(gemini["resets_at"]) <= now:
        return "Gemini heti keret: ISMERETLEN (reset)"
    line = (f"Gemini heti keret {display(used)}% {status(used)}, "
            f"maradt: {display(gemini['remaining_pct'])}%, reset: {reset_text(gemini, now)} | "
            f"Gemini szolgáltatói Claude/GPT heti keret {display(other['used_pct'])}% használt, maradt: {display(other['remaining_pct'])}%, "
            f"reset: {reset_text(other, now)}")
    five = groups.get("claude_gpt_five_hour")
    if five is not None:
        line += (f" | Gemini szolgáltatói Claude/GPT 5 órás keret {display(five['used_pct'])}% használt, "
                 f"maradt: {display(five['remaining_pct'])}%, reset: {reset_text(five, now)}")
    if unrecognized:
        line += f" | fel nem dolgozott új sor a kimenetben: {len(unrecognized)}"
    return line


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=collector.DEFAULT_STORE / collector.SNAPSHOT)
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    now = collector.parse_time(args.now) if args.now else datetime.now(timezone.utc)
    try:
        if now is None:
            raise ValueError()
        directory = collector.open_store(args.input.parent)
        try:
            fd = os.open(args.input.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > collector.MAX_OUTPUT:
                raise ValueError()
            raw = stream.read(collector.MAX_OUTPUT + 1)
            if len(raw) > collector.MAX_OUTPUT:
                raise ValueError()
        line = format_snapshot(json.loads(raw.decode("utf-8")), now)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        line = "Gemini heti keret: ISMERETLEN (missing)"
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

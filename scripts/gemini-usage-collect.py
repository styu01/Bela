#!/usr/bin/env python3
"""Collect the agy /usage quota rows without a model call or auth-file reads."""
import sys
sys.dont_write_bytecode = True
import argparse
import fcntl
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STORE = ROOT / "store"
DEFAULT_AGY = Path("/home/kisss/.local/bin/agy")
PROVIDER_HOME = "/home/kisss/.local/share/marveen-gemini-bridge/provider-home"
SNAPSHOT = "gemini-usage-latest.json"
LOG = "gemini-usage-collect.log"
LOCK = ".gemini-usage-collect.lock"
PROCESS_TIMEOUT = 45
MAX_OUTPUT = 8192
FRESH_SECONDS = 90 * 60
# (row label, limit kind) -> snapshot group key. Since agy's 11-26 build the
# output also carries a "Claude and GPT models / Five Hour Limit Remaining" row.
ROWS = {
    ("Gemini Models", "Weekly Limit Remaining"): "gemini",
    ("Claude and GPT models", "Weekly Limit Remaining"): "claude_gpt",
    ("Claude and GPT models", "Five Hour Limit Remaining"): "claude_gpt_five_hour",
}
REQUIRED_GROUPS = ("gemini", "claude_gpt")
KNOWN_GROUPS = tuple(ROWS.values())
# Backwards-compatible name: the groups every snapshot must contain.
GROUPS = {"Gemini Models": "gemini", "Claude and GPT models": "claude_gpt"}
# A well-formed row with an unknown label/kind is not a parser failure (a new
# CLI row must not blind the whole measurement again, as it did 11-26..11-28);
# it is recorded in the snapshot so the checker can say it was not processed.
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ()/&.,+-]{0,63}")
MAX_ROWS = 8
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)")
ERRORS = {"provider_unavailable", "provider_failed", "provider_timeout", "provider_output_too_large",
          "invalid_output", "snapshot_write_failed", "store_failed", "lock_failed", "busy", "invalid_now"}


class UsageError(Exception):
    pass


def parse_time(value):
    if not isinstance(value, str) or not TIME.fullmatch(value):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def number(value):
    return type(value) is int or type(value) is float and math.isfinite(value)


def parse_usage(text, details=None):
    """Parse agy /usage TSV into {group_key: {...}}.

    Every row must be well-formed (four tab-separated fields, a 0..100 percent,
    a timezone-aware ISO time). The two required groups must be present exactly
    once. A known optional row is parsed into its own group; a well-formed row
    with an unknown label/kind is skipped and, when a dict is passed as
    details, listed in details["unrecognized_rows"] ("label / kind").
    A reset of exactly 1970-01-01T00:00:00Z (epoch 0) is agy's "no window in
    progress" value and is stored as resets_at = null.
    """
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_OUTPUT:
        raise UsageError("invalid_output")
    if any(c in text for c in "\x0c\x0b\x1c\x1d\x1e\x85\u2028\u2029"):
        raise UsageError("invalid_output")
    rows = text.replace("\r\n", "\n").split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    if not len(REQUIRED_GROUPS) <= len(rows) <= MAX_ROWS:
        raise UsageError("invalid_output")
    result = {}
    unrecognized = []
    for row in rows:
        fields = row.split("\t")
        if len(fields) != 4:
            raise UsageError("invalid_output")
        label, kind, percentage, reset = fields
        if not SAFE_LABEL.fullmatch(label) or not SAFE_LABEL.fullmatch(kind):
            raise UsageError("invalid_output")
        if not re.fullmatch(r"(?:0|[1-9][0-9]?|100)(?:\.[0-9]{1,6})?%", percentage):
            raise UsageError("invalid_output")
        remaining = float(percentage[:-1]) if "." in percentage else int(percentage[:-1])
        stamp = parse_time(reset)
        if not 0 <= remaining <= 100 or stamp is None:
            raise UsageError("invalid_output")
        key = ROWS.get((label, kind))
        if key is None:
            name = label + " / " + kind
            if name in unrecognized:
                raise UsageError("invalid_output")
            unrecognized.append(name)
            continue
        if key in result:
            raise UsageError("invalid_output")
        result[key] = {"remaining_pct": remaining, "used_pct": round(100 - remaining, 6),
                       "resets_at": None if stamp.timestamp() == 0 else utc(stamp)}
    if any(key not in result for key in REQUIRED_GROUPS):
        raise UsageError("invalid_output")
    if details is not None:
        details["unrecognized_rows"] = unrecognized
    return result


def valid_group(group):
    if not isinstance(group, dict):
        return False
    remaining, used = group.get("remaining_pct"), group.get("used_pct")
    reset = group.get("resets_at")
    return (number(remaining) and number(used) and 0 <= remaining <= 100 and 0 <= used <= 100
            and abs(remaining + used - 100) <= 0.000001
            and (reset is None or parse_time(reset) is not None))


def valid_unrecognized(rows):
    return (isinstance(rows, list) and len(rows) <= MAX_ROWS
            and all(isinstance(r, str) and len(r) <= 131 and " / " in r
                    and all(SAFE_LABEL.fullmatch(p) for p in r.split(" / ", 1)) for r in rows)
            and len(set(rows)) == len(rows))


def run_usage(agy=DEFAULT_AGY, timeout=PROCESS_TIMEOUT):
    """Fixed command only; bounded pipes and a process-group deadline, no shell."""
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": PROVIDER_HOME,
           "LANG": os.environ.get("LANG", "C.UTF-8"), "TZ": os.environ.get("TZ", "Europe/Budapest")}
    try:
        child = subprocess.Popen([str(agy), "--print", "/usage", "--print-timeout", "30s"],
                                 env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        raise UsageError("provider_unavailable") from None
    output = bytearray()
    total = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (child.stdout, child.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise UsageError("provider_timeout")
                for key, _ in selector.select(left):
                    chunk = os.read(key.fileobj.fileno(), MAX_OUTPUT + 1)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > MAX_OUTPUT:
                        raise UsageError("provider_output_too_large")
                    if key.fileobj is child.stdout:
                        output.extend(chunk)
            try:
                returncode = child.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise UsageError("provider_timeout") from None
        if returncode != 0:
            raise UsageError("provider_failed")
        try:
            return output.decode("utf-8")
        except UnicodeError:
            raise UsageError("invalid_output") from None
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        child.stdout.close()
        child.stderr.close()


def open_store(store, create=False):
    """Refuse symlinked ancestors; no auth/token path is read by this script."""
    path = Path(os.path.abspath(store))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def cleanup_provider_logs():
    """Best effort, fixed log directory and cli-*.log names only; never read content."""
    directory = None
    try:
        directory = open_store(Path(PROVIDER_HOME) / ".gemini" / "antigravity-cli" / "log")
        cutoff = datetime.now(timezone.utc).timestamp() - 30 * 24 * 3600
        with os.scandir(directory) as entries:
            for entry in entries:
                if not (entry.name.startswith("cli-") and entry.name.endswith(".log")):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                        os.unlink(entry.name, dir_fd=directory)
                except OSError:
                    pass
    except (OSError, ValueError, OverflowError):
        pass
    finally:
        if directory is not None:
            try:
                os.close(directory)
            except OSError:
                pass


def atomic_write(output, snapshot):
    content = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix="." + output.name + ".", suffix=".tmp", dir=output.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
        directory = open_store(output.parent)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, ValueError):
        raise UsageError("snapshot_write_failed") from None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def log_error(store, code, now):
    try:
        directory = open_store(store)
        try:
            fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "wb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError()
            stream.write((utc(now) + " " + code + "\n").encode("ascii"))
    except OSError:
        print("Gemini heti keret: ISMERETLEN (log_write_failed)", file=sys.stderr)


def collect(store=DEFAULT_STORE, agy=DEFAULT_AGY, now=None, timeout=PROCESS_TIMEOUT):
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise UsageError("invalid_now")
    store = Path(store)
    try:
        directory = open_store(store, create=True)
    except OSError:
        raise UsageError("store_failed") from None
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "r+") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise UsageError("lock_failed")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UsageError("busy") from None
        try:
            details = {}
            groups = parse_usage(run_usage(agy, timeout), details)
            snapshot = {"collectedAt": utc(now), "groups": groups}
            if details.get("unrecognized_rows"):
                snapshot["unrecognizedRows"] = details["unrecognized_rows"]
            atomic_write(store / SNAPSHOT, snapshot)
            return snapshot
        finally:
            if Path(agy) == DEFAULT_AGY:
                cleanup_provider_logs()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--agy", type=Path, default=DEFAULT_AGY)
    parser.add_argument("--now", help="Timezone-aware ISO timestamp for deterministic tests")
    args = parser.parse_args(argv)
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    try:
        if now is None:
            raise UsageError("invalid_now")
        collect(args.state_dir, args.agy, now)
    except (UsageError, OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, UsageError) and str(exc) in ERRORS else "lock_failed"
        log_error(args.state_dir, code, datetime.now(timezone.utc))
        print("Gemini heti keret: ISMERETLEN (" + code + ")")
        return 75 if code == "busy" else 1
    print("Gemini heti keret: OK (pillanatkép frissítve)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

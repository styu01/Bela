#!/usr/bin/env python3
"""telegram_progress_watchdog.tmux_session_alive uses the EXACT tmux target (PROGI review 6212).

TMUXEXACT1771: tmux answers a bare `-t NAME` with a session whose name merely STARTS with NAME when
no session is called NAME. The watchdog asked `has-session -t agent-<name>`, so a dead agent-X with a
live agent-X2 read as alive and its silence went unreported. Driven against an isolated tmux server
(TMUX_TMPDIR = a fresh temp dir, TMUX unset), never the fleet's.

Run: python3 <thisfile>   Exit 0 = all pass.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(os.path.dirname(HERE), "hooks", "telegram_progress_watchdog.py")

failures = 0


def check(name, cond):
    global failures
    print(("  PASS: " if cond else "  FAIL: ") + name)
    if not cond:
        failures += 1


if not shutil.which("tmux"):
    print("  SKIP: no tmux on this host")
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="tgwd-exact-")
os.environ.pop("TMUX", None)
os.environ["TMUX_TMPDIR"] = tmp
os.environ.pop("TELEGRAM_WATCHDOG_FORCE_AGENT_UP", None)
started = False
try:
    subprocess.run(["tmux", "new-session", "-d", "-s", "agent-foo2", "sleep 120"], check=True)
    started = True
    sock = subprocess.run(["tmux", "display-message", "-p", "-t", "=agent-foo2:", "#{socket_path}"],
                          capture_output=True, text=True).stdout.strip()
    if not sock.startswith(tmp) and not sock.startswith("/private" + tmp):
        print("  FAIL: the isolated tmux server could not be proven (socket %r); nothing else run" % sock)
        sys.exit(1)

    spec = importlib.util.spec_from_file_location("tgwd", MOD)
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)

    bare = subprocess.run(["tmux", "has-session", "-t", "agent-foo"], capture_output=True).returncode
    if bare == 0:
        print("  NOTE: control -- the bare -t agent-foo is answered by agent-foo2 on this tmux (the hazard is real)")
    check("a missing agent-foo is NOT alive while agent-foo2 runs", wd.tmux_session_alive("agent-foo") is False)
    check("agent-foo2 itself is alive", wd.tmux_session_alive("agent-foo2") is True)
finally:
    if started:
        subprocess.run(["tmux", "kill-server"], capture_output=True)
    shutil.rmtree(tmp, ignore_errors=True)

print("PASS" if failures == 0 else "FAIL (%d)" % failures)
sys.exit(1 if failures else 0)

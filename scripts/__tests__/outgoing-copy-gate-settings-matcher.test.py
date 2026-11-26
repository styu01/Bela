#!/usr/bin/env python3
"""The main agent's .claude/settings.json must register outgoing-copy-gate.py for the Gmail tools it audits.

Measured gap: the gate dispatch (EMAIL_TOOL_RE) audits mcp__gmail__draft_email, update_draft and reply_all, but the
PreToolUse matchers only covered Bash, '.*send_email.*' and the Telegram reply, so a draft or reply_all never reached
the hook at all. A matcher is a regex on the tool name; both full-match and search semantics are checked, so the
verdict does not depend on how the harness anchors it."""
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
HOOK = os.path.join(ROOT, "scripts", "hooks", "outgoing-copy-gate.py")
SETTINGS = os.path.join(ROOT, ".claude", "settings.json")
FAILS = []


def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else " (%s)" % detail))
    if not ok:
        FAILS.append(name)


settings = json.load(open(SETTINGS, encoding="utf-8"))
matchers = [m["matcher"] for m in settings["hooks"]["PreToolUse"]
            if any("outgoing-copy-gate.py" in h.get("command", "") for h in m.get("hooks", []))]
check("outgoing-copy-gate is registered on PreToolUse", bool(matchers), repr(matchers))


def registered(tool):
    return all(any(fn(re.compile(m), tool) for m in matchers)
               for fn in (lambda r, t: r.fullmatch(t), lambda r, t: r.search(t)))


spec = importlib.util.spec_from_file_location("ocg_matcher", HOOK)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

MUST_REGISTER = [
    "Bash",
    "mcp__plugin_telegram_telegram__reply",
    "mcp__gmail__send_email",
    "mcp__gmail__draft_email",
    "mcp__gmail__update_draft",
    "mcp__gmail__reply_all",
    "mcp__claude_ai_Gmail__reply_all",
]
for tool in MUST_REGISTER:
    check("registered: %s" % tool, registered(tool))
    if tool not in ("Bash", "mcp__plugin_telegram_telegram__reply"):
        check("the gate dispatch audits it too: %s" % tool, gate.EMAIL_TOOL_RE.search(tool) is not None)

for tool in ("mcp__gmail__search_emails", "mcp__gmail__read_email", "mcp__gmail__list_labels", "Read", "Edit"):
    check("not registered (no outgoing text): %s" % tool, not registered(tool))

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), FAILS), file=sys.stderr)
    sys.exit(1)
print("All outgoing-copy-gate settings-matcher tests passed.")

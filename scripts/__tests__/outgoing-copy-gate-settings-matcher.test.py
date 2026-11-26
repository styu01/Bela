#!/usr/bin/env python3
"""The main agent's .claude/settings.json must register outgoing-copy-gate.py for the Gmail tools it audits.

Measured gap: the gate dispatch audits mcp__gmail__draft_email, update_draft, reply_all, the Gmail connector's reply,
send_message, create_draft, forward and the Telegram edit_message, but the PreToolUse matchers only covered Bash,
'.*send_email.*' and the Telegram reply, so those calls never reached the hook at all. A bare forward (no note of ours)
must pass, the same as the manage_email forward; send_draft carries no text and is not registered. A matcher is a regex on the tool name; both full-match and search semantics are checked, so the
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
    "mcp__plugin_telegram_telegram__edit_message",
    "mcp__gmail__send_email",
    "mcp__gmail__draft_email",
    "mcp__gmail__update_draft",
    "mcp__gmail__reply_all",
    "mcp__claude_ai_Gmail__reply_all",
    "mcp__claude_ai_Gmail__reply",
    "mcp__claude_ai_Gmail__send_message",
    "mcp__claude_ai_Gmail__create_draft",
    "mcp__claude_ai_Gmail__forward",
]
for tool in MUST_REGISTER:
    check("registered: %s" % tool, registered(tool))
    hits = [m for m in matchers if re.search(m, tool)]
    check("registered exactly once (the hook must not run twice): %s" % tool, len(hits) == 1, repr(hits))
    if tool not in ("Bash",) and "telegram" not in tool:
        check("the gate dispatch audits it too: %s" % tool, gate.EMAIL_TOOL_RE.search(tool) is not None)

# send_draft carries no text (it was audited when drafted); read-only tools carry none of ours.
for tool in ("mcp__gmail__send_draft", "mcp__gmail__search_emails", "mcp__gmail__read_email",
             "mcp__gmail__list_labels", "mcp__claude_ai_Gmail__forward_all_x", "Read", "Edit"):
    check("not registered (no outgoing text): %s" % tool, not any(re.search(m, tool) for m in matchers))

# Real hook runs with real payloads on every newly registered name (isolated rules file, no name rule).
import subprocess
import tempfile
RULES = os.path.join(tempfile.mkdtemp(prefix="copygate-matcher-"), "rules.json")
with open(RULES, "w", encoding="utf-8") as fh:
    json.dump({"no_name_rule": True, "no_name_rule_reason": "isolated test"}, fh)
GOOD = "Szia! Köszönöm a levelet, holnap átnézem és küldöm a választ. Üdvözlettel, Béla"
BAD = "Szia! Kerlek nezd meg a levelet, es kuldom a valaszt. Koszonom szepen."
EM = "Szia! Köszönöm a levelet \u2014 holnap átnézem és küldöm a választ."
RUNS = [
    ("mcp__plugin_telegram_telegram__edit_message", {"chat_id": "1", "message_id": "2", "text": "Kész \u2014 mehet."}, 2),
    ("mcp__plugin_telegram_telegram__edit_message", {"chat_id": "1", "message_id": "2", "text": "Kész, mehet."}, 0),
    ("mcp__claude_ai_Gmail__forward", {"messageId": "abc", "to": ["x@y.hu"]}, 0),
    ("mcp__claude_ai_Gmail__forward", {"messageId": "abc", "to": ["x@y.hu"], "cc": ["z@y.hu"]}, 0),
    ("mcp__claude_ai_Gmail__forward", {"messageId": "abc", "to": ["x@y.hu"], "body": BAD}, 2),
    ("mcp__claude_ai_Gmail__forward", {"messageId": "abc", "to": ["x@y.hu"], "body": GOOD}, 0),
    # a note in a field the collector does not know must not ride the bare-forward exemption
    ("mcp__claude_ai_Gmail__forward", {"messageId": "abc", "to": ["x@y.hu"], "note": BAD}, 2),
    # the exemption accepts only a string or a list of strings in an address/id field (PROGI re-review)
    ("mcp__claude_ai_Gmail__forward", {"messageId": "abc", "to": "x@y.hu", "cc": None}, 0),
    ("mcp__claude_ai_Gmail__forward", {"messageId": {"text": BAD}, "to": ["x@y.hu"]}, 2),
    ("mcp__claude_ai_Gmail__forward", {"messageId": "abc", "to": [{"name": "Anna \u2014 x", "email": "x@y.hu"}]}, 2),
    ("mcp__claude_ai_Gmail__reply", {"messageId": "abc", "body": BAD}, 2),
    ("mcp__claude_ai_Gmail__reply", {"messageId": "abc", "body": GOOD}, 0),
    ("mcp__claude_ai_Gmail__send_message", {"to": ["x@y.hu"], "subject": "Ajánlat", "body": EM}, 2),
    ("mcp__claude_ai_Gmail__send_message", {"to": ["x@y.hu"], "subject": "Ajánlat", "body": GOOD}, 0),
    ("mcp__claude_ai_Gmail__create_draft", {"to": ["x@y.hu"], "subject": "Ajánlat", "body": GOOD}, 0),
    ("mcp__claude_ai_Gmail__create_draft", {"to": ["x@y.hu"], "subject": "Teszt", "body": BAD}, 2),
    ("mcp__gmail__draft_email", {"to": ["x@y.hu"], "subject": "Ajánlat", "body": GOOD}, 0),
    ("mcp__gmail__draft_email", {"to": ["x@y.hu"], "subject": "Ajánlat", "body": EM}, 2),
    ("mcp__gmail__update_draft", {"draftId": "d", "subject": "Ajánlat", "body": GOOD}, 0),
    ("mcp__gmail__reply_all", {"messageId": "abc", "body": BAD}, 2),
]
for tool, ti, want in RUNS:
    p = subprocess.run([sys.executable, HOOK], input=json.dumps({"tool_name": tool, "tool_input": ti}, ensure_ascii=False),
                       capture_output=True, text=True, timeout=30,
                       env={**os.environ, "OUTGOING_COPY_GATE_RULES": RULES, "PYTHONDONTWRITEBYTECODE": "1"})
    check("hook run %s %s -> exit %d" % (tool, sorted(ti), want), p.returncode == want,
          "exit %d: %s" % (p.returncode, (p.stderr + p.stdout)[:200]))

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), FAILS), file=sys.stderr)
    sys.exit(1)
print("All outgoing-copy-gate settings-matcher tests passed.")

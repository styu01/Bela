#!/usr/bin/env python3
"""HOMOGLYPHMICRO924 (upstream babbc71a): unit and formula notation is not a mixed-script word.

Before the fix 'µs' (MICRO SIGN), 'm²' / 'cm³' (SUPERSCRIPT TWO/THREE) and 'H₂O' (SUBSCRIPT TWO) were blocked as
homoglyphs, on the Telegram/email branch and on the inter-agent (curl /api/messages) branch. The exemption is a closed
list: KELVIN SIGN and ROMAN NUMERAL ONE (lookalikes of K and I) and Cyrillic must still block, and a neutral sign must
not shield a real lookalike in the same word."""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hooks", "outgoing-copy-gate.py")
MICRO, SUP2, SUP3, SUB2 = "µ", "²", "³", "₂"
KELVIN, ROMAN_ONE, CYR_A, CYR_S = "K", "Ⅰ", "а", "с"
FAILS = []
TMP = tempfile.mkdtemp(prefix="copygate-micro-")
RULES = os.path.join(TMP, "rules.json")
with open(RULES, "w", encoding="utf-8") as fh:
    json.dump({"no_name_rule": True, "no_name_rule_reason": "isolated test"}, fh)
ENV = {**os.environ, "OUTGOING_COPY_GATE_RULES": RULES, "PYTHONDONTWRITEBYTECODE": "1"}


def run(payload):
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload, ensure_ascii=False), text=True,
                       capture_output=True, env=ENV, timeout=30)
    return p.returncode, p.stderr


def reply(text):
    return run({"tool_name": "mcp__plugin_telegram_telegram__reply", "tool_input": {"text": text}})


def interagent(content):
    body = json.dumps({"from": "kisprogi", "to": "bela", "content": content}, ensure_ascii=False)
    cmd = ("curl -s -X POST http://localhost:3420/api/messages -H 'Content-Type: application/json' "
           "--data-binary @- <<'JSON'\n" + body + "\nJSON")
    return run({"tool_name": "Bash", "tool_input": {"command": cmd}, "hook_event_name": "PreToolUse"})


def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else " (%s)" % detail[:300]))
    if not ok:
        FAILS.append(name)


HU = "Szia! Holnap reggel átnézem, köszönöm szépen, és küldöm a választ. "
UNITS = "a mért késés 40 %ss, a ház 100 m%s, 5 cm%s, a víz H%sO." % (MICRO, SUP2, SUP3, SUB2)

for label, send in (("telegram", reply), ("inter-agent", interagent)):
    code, err = send(HU + UNITS)
    check("%s: unit notation passes" % label, code == 0 and "VEGYES" not in err and "homogl" not in err.lower(), err)
    for nm, word, want in (("KELVIN SIGN", KELVIN + "ARTYA", "KELVIN SIGN"),
                           ("ROMAN NUMERAL ONE", ROMAN_ONE + "NVOICE", "ROMAN NUMERAL ONE"),
                           ("Cyrillic a", "k" + CYR_A + "rtya", "CYRILLIC SMALL LETTER A"),
                           ("neutral sign does not shield a lookalike", "m" + CYR_S + SUP2, "CYRILLIC SMALL LETTER ES")):
        code, err = send(HU + "a " + word + " kész.")
        check("%s: %s still blocks" % (label, nm), code == 2 and want in err, "%s %s" % (code, err))

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), FAILS), file=sys.stderr)
    sys.exit(1)
print("All homoglyph-micro tests passed.")

#!/usr/bin/env python3
"""SENDERNAMEMSG1126: when a Bash command is refused ONLY because a sender is named somewhere in it (the
LAUNCHGATE1126 sender-token layer), the copy gate says so in its own words, instead of the misleading "the hook
found no text to inspect" block (PROGI msg 5993). A real send without an inspectable body keeps the old message.
Runs the real hook as a subprocess, like the PreToolUse call."""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hooks", "outgoing-copy-gate.py")
RULES = os.path.join(tempfile.mkdtemp(prefix="copygate-sendername-"), "rules.json")
with open(RULES, "w", encoding="utf-8") as fh:
    json.dump({"no_name_rule": True, "no_name_rule_reason": "isolated test"}, fh)
X = "send" + "mail"
NEW = "kuldo-nevu szo all"
OLD = "nem talalt vizsgalhato szoveget"
failures = []


def run(cmd):
    p = subprocess.run([sys.executable, HOOK],
                       input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
                       capture_output=True, text=True, timeout=30,
                       env={**os.environ, "OUTGOING_COPY_GATE_RULES": RULES, "PYTHONDONTWRITEBYTECODE": "1"})
    return p.returncode, p.stderr + p.stdout


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f": {detail[:300]}"))
    if not ok:
        failures.append(name)


# Refused only by the sender-token layer: the new, specific message.
for cmd in (f"pkill -0 {X}", f"apt-get install -y {X}", f"node scripts/check.mjs {X}", f"vim {X}"):
    rc, out = run(cmd)
    check(f"token layer only, own message: {cmd}", rc == 2 and NEW in out and OLD not in out, f"rc={rc} {out}")

# A real send with no inspectable body: the old message, unchanged.
for cmd in (f"{X} a@b.hu", f"bash -lc '{X} a@b.hu'", f"perl -e 'system(\"{X} a@b.hu\")'"):
    rc, out = run(cmd)
    check(f"real send, old message: {cmd}", rc == 2 and OLD in out and NEW not in out, f"rc={rc} {out}")

# A send that the head rules see AND that also names the sender elsewhere: still the old message (not "maybe").
rc, out = run(f"{X} a@b.hu; pkill -0 {X}")
check("a real send next to a token-layer hit keeps the old message", rc == 2 and OLD in out and NEW not in out,
      f"rc={rc} {out}")

# Not a send at all: passes.
for cmd in (f"grep -rn {X} src/", "git status"):
    rc, out = run(cmd)
    check(f"no send, passes: {cmd}", rc == 0, f"rc={rc} {out}")

print()
if failures:
    print(f"FAIL: {len(failures)}")
    sys.exit(1)
print("ITELET: the listed cases are as expected")

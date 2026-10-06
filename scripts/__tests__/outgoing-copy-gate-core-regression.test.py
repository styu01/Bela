#!/usr/bin/env python3
"""Direct-Python coverage for the hook branches normally wrapped by Vitest."""
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK = os.path.join(ROOT, "hooks", "outgoing-copy-gate.py")
CASES = os.path.join(ROOT, "hooks", "send-invocation-cases.json")

spec = importlib.util.spec_from_file_location("outgoing_copy_gate", HOOK)
gate = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(gate)

failures = []


def check(name, actual, expected):
    ok = actual == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={actual!r}, want={expected!r}")
    if not ok:
        failures.append(name)


with open(CASES, encoding="utf-8") as fh:
    for case in json.load(fh)["cases"]:
        check(
            f"send invocation: {case['name']}",
            gate.is_send_invocation(case["cmd"]),
            case["expected"],
        )

# 347df3d1: technical identifiers are ignored by the name rule, while prose
# with a Hungarian suffix remains visible to it.
gate.BAD_NAME = re.compile(r"\bTeszt(?:[- ]?Elek)?\b", re.I)
for name, text, expected in (
    ("name in a URL is masked", "https://example.test/TesztElek", False),
    ("name in a code span is masked", "A " + chr(96) + "TesztElek" + chr(96) + " azonosító.", False),
    ("suffixed prose name remains visible", "Kérlek, szólj TesztElek-val.", True),
):
    check(name, bool(gate.BAD_NAME.search(gate.strip_for_name(text))), expected)

if failures:
    print(f"FAILED: {', '.join(failures)}", file=sys.stderr)
    sys.exit(1)
print("All direct outgoing-copy-gate core regressions passed.")

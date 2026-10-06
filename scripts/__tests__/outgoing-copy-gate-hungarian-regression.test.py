#!/usr/bin/env python3
"""Isolated real-message regression cases for the outgoing-copy gate."""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hooks", "outgoing-copy-gate.py")


def run_hook(text):
    payload = {
        "tool_name": "mcp__plugin_telegram_telegram__reply",
        "tool_input": {"text": text, "format": "markdownv2"},
    }
    return subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        env={**os.environ, "OUTGOING_COPY_GATE_RULES": RULES},
        timeout=20,
    )


def expect(name, text, code, reason=None):
    result = run_hook(text)
    ok = result.returncode == code and (reason is None or reason in result.stderr)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={result.returncode}, want={code}")
    if not ok:
        print(result.stderr, file=sys.stderr)
        FAILURES.append(name)


with tempfile.TemporaryDirectory(prefix="copygate-hu-") as TMP:
    RULES = os.path.join(TMP, "rules.json")
    with open(RULES, "w", encoding="utf-8") as fh:
        json.dump({"no_name_rule": True, "no_name_rule_reason": "izolált regressziós teszt"}, fh)

    FAILURES = []

    # A live Telegram-style answer: heading, structure, and a Hungarian suffix
    # on a dotted filename must be accepted as normal prose.
    expect(
        "structured Telegram reply with a dotted filename suffix passes",
        "**Ellenőrzés**\n\nA store/dashboard.error.log-ban nincs új hiba.\n\n- a napló rendben van\n- a következő ellenőrzés később fut",
        0,
    )
    expect(
        "Telegram double hyphen is still blocked",
        "**Ellenőrzés**\n\nA store/dashboard.error.log-ban nincs új hiba -- kérlek, később nézd meg.",
        2,
        "DUPLA KOTOJEL",
    )

    for label, dash in (
        ("em dash", "—"),
        ("en dash", "–"),
        ("named HTML entity", "&mdash;"),
        ("numeric HTML entity", "&#8212;"),
    ):
        expect(
            f"{label} is blocked",
            f"Szia! A határidő {dash} péntek, köszönöm.",
            2,
            "GONDOLATJEL",
        )

    # These are identifiers, ranges, URLs, or quoted command output rather
    # than prohibited dash typography, and must not turn into false positives.
    expect("ordinary hyphen passes", "A kliens-oldali ellenőrzés rendben van, köszönöm.", 0)
    expect("numeric range passes", "A futás várhatóan 10-20 percig tart, köszönöm.", 0)
    expect("URL passes", "A részletek itt vannak: https://pelda.hu/kapcsolat, köszönöm.", 0)
    expect(
        "quoted command output passes",
        'A parancs kimenete: "status=ok; retry=0". Köszönöm.',
        0,
    )

    if FAILURES:
        print(f"FAILED: {', '.join(FAILURES)}", file=sys.stderr)
        sys.exit(1)
    print("All Hungarian outgoing-copy-gate regressions passed.")

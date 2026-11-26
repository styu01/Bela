#!/usr/bin/env python3
"""Synthetic suffix regressions. Only invoke the local hook, never a sender."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

DEFAULT_HOOK = Path(__file__).resolve().parents[1] / "hooks/outgoing-copy-gate.py"
HOOK = Path(os.environ.get("TEST_GATE_HOOK", DEFAULT_HOOK)).resolve()
CONTEXT = "Szia! Kérlek, nézd meg: {}. Köszönöm."


class IdentifierSuffixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="gate-id-suffix-")
        cls.rules = Path(cls.tmp.name) / "rules.json"
        cls.rules.write_text(json.dumps({
            "bad_name_patterns": [r"\bTesztElek\b"],
            "correction": "Teszt Elek (szintetikus tesztnév)",
        }), encoding="utf-8")
        cls.env = {**os.environ, "OUTGOING_COPY_GATE_RULES": str(cls.rules),
                   "PYTHONDONTWRITEBYTECODE": "1"}
        old = os.environ.get("OUTGOING_COPY_GATE_RULES")
        os.environ["OUTGOING_COPY_GATE_RULES"] = str(cls.rules)
        try:
            spec = importlib.util.spec_from_file_location("gate_suffix_test", HOOK)
            cls.gate = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.gate)
        finally:
            if old is None:
                os.environ.pop("OUTGOING_COPY_GATE_RULES", None)
            else:
                os.environ["OUTGOING_COPY_GATE_RULES"] = old

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def invoke(self, text, code, reason=None, tool="mcp__plugin_telegram_telegram__reply"):
        payload = {"tool_name": tool, "tool_input": {
            "text": text, "format": "markdownv2", "body": text,
        }}
        p = subprocess.run([sys.executable, str(HOOK)],
                           input=json.dumps(payload, ensure_ascii=False),
                           capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(p.returncode, code, p.stderr + p.stdout)
        if reason:
            self.assertIn(reason, p.stderr)
        self.assertNotIn("belső hiba", p.stderr.lower())
        return p

    def test_shortened_hex_identifiers(self):
        for stem in ("99f4", "A9F4", "abcd", "deadbeef", "1234"):
            for ellipsis in ("...", "…"):
                for suffix in ("es", "as"):
                    token = f"{stem}{ellipsis}-{suffix}"
                    with self.subTest(token=token):
                        self.invoke(CONTEXT.format(token), 0)
                        self.assertNotIn(suffix, [w for w, _ in self.gate.accent_check_tokens(
                            self.gate.strip_technical(token))])

    def test_plain_identifiers_and_numeric_suffixes(self):
        for token in ("99f4-es", "99ff-es", "99ff-as", "429-es", "403-as", "25-ös"):
            with self.subTest(token=token):
                self.invoke(CONTEXT.format(token), 0)

    def test_real_unaccented_conjunction_is_blocked(self):
        for text in ("az es a", "Az es a", "az\nes\ta", CONTEXT.format("az es a"),
                     CONTEXT.format("99f4…-es azonosító es a többi"),
                     CONTEXT.format("99f4...-as azonosító es a többi"),
                     CONTEXT.format("99f4… es a többi")):
            with self.subTest(text=text):
                self.invoke(text, 2, "es -> és")

    def test_no_general_ellipsis_or_suffix_exemption(self):
        for token in ("valami…-es", "99f4… es", "99f4... es", "99f4…- es",
                      "99f4… -es", "99f4....-es", "99f4..-es", "99f4…-esek"):
            with self.subTest(token=token):
                words = [w for w, _ in self.gate.accent_check_tokens(
                    self.gate.strip_technical(token))]
                self.assertTrue(any(w.startswith("es") for w in words), words)
        # Whole-token boundaries: no partial exemption inside a larger word.
        for token in ("x99f4…-es", "ő99f4…-es", "99f4…-es_foo"):
            with self.subTest(token=token):
                self.assertIsNone(re.search(
                    r"(?<![\w.])[A-Fa-f0-9]{4,}(?:\.{3}|…)-(?:es|as)(?![\w-])", token))

    def test_other_real_accent_errors_are_blocked(self):
        for token in ("99f4…-es, kerlek", "99f4...-as, koszonom"):
            with self.subTest(token=token):
                self.invoke(CONTEXT.format(token), 2, "HIANYZO EKEZETEK")

    def test_other_rules_still_block(self):
        for text, reason in (("99f4…-es — teszt", "GONDOLATJEL"),
                             ("99f4…-es &mdash; teszt", "GONDOLATJEL"),
                             ("99f4…-as -- teszt", "DUPLA KOTOJEL"),
                             ("99f4…-es TesztElek", "HELYTELEN NEV"),
                             ("99f4…-as tеszt", "VEGYES IRASRENDSZERU")):
            with self.subTest(text=text):
                self.invoke(CONTEXT.format(text), 2, reason)
        # The identifier exception belongs ONLY to the accent mask.
        text = "99f4…-es"
        self.assertEqual(self.gate.strip_for_name(text), text)

    def test_edited_messages_and_email_use_the_same_audit(self):
        for tool in ("mcp__plugin_telegram_telegram__edit_message", "send_email"):
            with self.subTest(tool=tool):
                self.invoke(CONTEXT.format("99f4…-es"), 0, tool=tool)
                self.invoke(CONTEXT.format("99f4…-es es a többi"), 2,
                            "es -> és", tool=tool)

    def test_positive_control_with_correct_conjunction(self):
        self.invoke(CONTEXT.format("99f4…-es és a 99ff...-as"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""MAINGATENLCOMMENT1104 (Samu 69660/2): the main agent's approval gate reads a shell comment as bash does.

Measured on develop before the fix (is_send_invocation): a comment on one line swallowed the send on the next
("echo hi # note" newline "sendmail a@b.hu" -> False), an apostrophe in the comment did the same, and a # inside a
word started a comment ("echo a#b; sendmail a@b.hu" -> False). The shared rows live in send-invocation-cases.json;
this file holds the forms where only the python twin is exact (the JS twin keeps every word: MJSTOKCOMMENT1010)."""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ocg", os.path.join(HERE, "..", "hooks", "outgoing-copy-gate.py"))
ocg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ocg)
failed = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond else f" -- {extra}"))
    if not cond:
        failed.append(name)


for cmd, want, what in [
    ("echo hi # note; sendmail a@b.hu", False, "a comment runs to the end of its line, a ; inside it too"),
    ("echo hi # note\nsendmail a@b.hu", True, "the next line is a command again"),
    ("echo hi # a 'quote\nsendmail a@b.hu", True, "a quote inside a comment is comment text"),
    ("echo a#b; sendmail a@b.hu", True, "a # inside a word"),
    ("echo $# x; sendmail a@b.hu", True, "$# is no comment"),
    ("echo ${#x}; sendmail a@b.hu", True, "${#x} is no comment"),
    ("echo 'it''s' # c\nsendmail a@b.hu", True, "quotes before a comment"),
    ("# only a comment", False, "a command that is only a comment"),
    ("echo $(echo a)#x; sendmail a@b.hu", True, "a # right after the ) of a $(...) is part of the word (Bence 69701)"),
    ("echo $(echo a) #x; sendmail a@b.hu", False, "control: after a space it is a comment"),
    ("echo $( (echo a) )#x; sendmail a@b.hu", True, "a plain ( ) nested inside the $( ): the outer ) still closes the $("),
    ("echo a\\ #b; sendmail a@b.hu", True, "a # after an escaped space is no comment (bash runs the send; PROGI review)"),
    ("echo a\\\t#b; sendmail a@b.hu", True, "a # after an escaped tab is no comment"),
    ("echo a\\;#b; sendmail a@b.hu", True, "a # after an escaped ; is no comment"),
    ("echo a\\\\ #b; sendmail a@b.hu", False, "control: an escaped BACKSLASH, then a real space: the # is a comment again"),
    # PROGI re-review: a # on the very FIRST character is a comment (escaped_at started at -1 == i-1 at i == 0),
    # and a backslash-newline is a line continuation that bash removes before splitting words, not an escape.
    ("#x\\\nsendmail a@b.hu", True, "a comment on the first line ends at the newline, even after a trailing backslash"),
    ("\\\n#&&\\\nsendmail a@b.hu", True, "line continuations around a comment: the send on the next line runs"),
    ("#x; sendmail a@b.hu", False, "control: a # on the first character comments out the whole line"),
    ("true; \\\nsendmail a@b.hu", True, "a line continuation before the send keeps it at command position"),
    ("echo a \\\nsendmail", False, "control: after a line continuation, sendmail is still echo's argument"),
]:
    got = ocg.is_send_invocation(cmd)
    check(f"{what}: {cmd!r} -> {want}", got is want, f"got {got}")
for cmd, want in [
    ("echo a#b; x", [["echo", "a#b"], ["x"]]),
    ("echo hi # c\nx", [["echo", "hi"], ["x"]]),
    ("echo \"#q\" z", [["echo", "#q", "z"]]),
]:
    got = ocg._segments_tokens(cmd)
    check(f"tokens of {cmd!r}", got == want, f"got {got}")

# NOTE: upstream also covers email-approval-gate.py names_own_sender; that hook does not exist in this fork.

print()
if failed:
    print(f"{len(failed)} FAILED: {failed}", file=sys.stderr)
    sys.exit(1)
print("All outgoing-copy-gate-nlcomment tests passed.")

#!/usr/bin/env bash
# channels.sh _answer_accept_dialog (upstream ef83f266 TRUSTGATE901, ported; PROGI review 6212).
#
# MEASURED on Claude Code 2.1.326 (own pty, throwaway HOME and project dir): on the folder-trust and
# the bypass-permissions panel the focused first row is "No, exit"; Enter, Tab+Enter, "1"+Enter and
# "2"+Enter all EXIT the session, Down+Enter and Up+Enter accept. The guard used to answer "2" Enter
# (bypass) and "1" Enter (trust): both chose the exit. The function is sliced out of channels.sh and
# run against a fake tmux (logged calls, scripted captures); channels.sh itself is never executed.
set -u
INSTALL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
CHANNELS="$INSTALL_DIR/scripts/channels.sh"
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1 -- expected: $2, got: $3"; }
eq() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "$2" "$3"; fi; }

KEYS_WORD="send""-keys"
cat > "$TMPD/tmux" <<EOF
#!/bin/bash
echo "\$*" >> "\$FAKE_LOG"
case "\$1" in
  capture-pane) cat "\$FAKE_DIR/after" ;;
esac
exit 0
EOF
chmod +x "$TMPD/tmux"

fn="$(sed -n '/^_answer_accept_dialog() {$/,/^}$/p' "$CHANNELS")"
[ -n "$fn" ] || { echo "  FAIL: _answer_accept_dialog not found in channels.sh"; exit 1; }
eval "$fn"
TMUX="$TMPD/tmux"; SESSION="main-channels"

# The 2.1.326 panels as tmux renders them (bordered box, cursor glyph on the focused row).
TRUST_NO=$'╭──────────────────────────────────────╮\n│ Quick safety check: Is this a project│\n│ you created or one you trust?        │\n│ ❯ No, exit                           │\n│   Yes, I trust this folder           │\n│ Enter to confirm · Esc to cancel     │\n╰──────────────────────────────────────╯'
TRUST_YES=$'│ Quick safety check: Is this a project│\n│   No, exit                           │\n│ ❯ Yes, I trust this folder           │\n╰──────────────────────────────────────╯'
BYPASS_NO=$'Bypass Permissions mode\n\n❯ No, exit\n  Yes, I accept\n\nEnter to confirm · Esc to exit'
BYPASS_YES=$'Bypass Permissions mode\n\n  No, exit\n❯ Yes, I accept\n\nEnter to confirm · Esc to exit'
FAR=$'❯ No, exit\n  Maybe later\n  Yes, I accept'
NOCURSOR=$'Bypass Permissions mode\n  No, exit\n  Yes, I accept'

# answer <pane> <capture after the Down>
answer() {
  export FAKE_DIR="$TMPD/c$RANDOM$RANDOM"; mkdir -p "$FAKE_DIR"
  export FAKE_LOG="$FAKE_DIR/log"; : > "$FAKE_LOG"
  printf '%s\n' "$2" > "$FAKE_DIR/after"
  sleep() { :; }
  _answer_accept_dialog "$1" 2> "$FAKE_DIR/err"
  unset -f sleep
}
keys() { grep "^$KEYS_WORD" "$FAKE_LOG" | sed "s/^$KEYS_WORD -t =main-channels: //" | tr '\n' ' ' | sed 's/ $//'; }

echo "channels.sh startup dialog answer (2.1.326 layout)"
answer "$TRUST_NO" "$TRUST_YES"
eq "trust panel, cursor on No: Down, re-read, Enter on Yes" "Down Enter" "$(keys)"
answer "$BYPASS_NO" "$BYPASS_YES"
eq "bypass panel, cursor on No: Down, re-read, Enter on Yes" "Down Enter" "$(keys)"
answer "$TRUST_NO" "$TRUST_NO"
eq "LOCAL the Down did not move the cursor: NO Enter"     "Down" "$(keys)"
case "$(cat "$FAKE_DIR/err")" in *"did not land on Yes after Down, NO Enter"*) pass "LOCAL ...and it is logged" ;; *) fail "LOCAL ...logged" "did not land on Yes" "$(cat "$FAKE_DIR/err")" ;; esac
answer "$BYPASS_YES" ""
eq "cursor already on Yes: Enter only"                    "Enter" "$(keys)"
answer "$FAR" ""
eq "Yes two rows below the cursor: no key"                "" "$(keys)"
answer "$NOCURSOR" ""
eq "no cursor glyph: no key"                              "" "$(keys)"
case "$(cat "$FAKE_DIR/err")" in *"unknown shape -- no key sent"*) pass "...and it is logged" ;; *) fail "...logged" "unknown shape" "$(cat "$FAKE_DIR/err")" ;; esac
eq "never a number key"                                   "0" "$(grep -c "^$KEYS_WORD.* [12] Enter" "$FAKE_LOG")"

echo "channels.sh guard wiring (read, never run)"
guard="$(sed -n '/^while \[ "\$SECONDS" -lt "\$_boot_guard_deadline" \]; do$/,/^done$/p' "$CHANNELS")"
eq "the bypass branch answers from the pane" "1" "$(printf '%s\n' "$guard" | sed -n '/"Bypass Permissions mode"/,/;;/p' | grep -c '_answer_accept_dialog "\$pane"')"
eq "the trust branch answers from the pane"  "1" "$(printf '%s\n' "$guard" | sed -n '/"Do you trust the files in this folder?"/,/;;/p' | grep -c '_answer_accept_dialog "\$pane"')"
eq "the trust branch also matches the 2.1.246+ option text" "1" "$(printf '%s\n' "$guard" | grep -c '\*"Do you trust the files in this folder?"\*|\*"Yes, I trust this folder"\*)')"
eq "no number answer is left in the guard"   "0" "$(printf '%s\n' "$guard" | grep -cE "$KEYS_WORD"' -t "=\$SESSION:" "[12]" Enter')"
fn_at="$(grep -n '^_answer_accept_dialog() {$' "$CHANNELS" | cut -d: -f1)"
loop_at="$(grep -n '^while \[ "\$SECONDS" -lt "\$_boot_guard_deadline" \]; do$' "$CHANNELS" | cut -d: -f1)"
if [ -n "$fn_at" ] && [ -n "$loop_at" ] && [ "$fn_at" -lt "$loop_at" ]; then pass "the function is defined before the guard loop"; else fail "defined before the loop" "fn < loop" "fn=$fn_at loop=$loop_at"; fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]

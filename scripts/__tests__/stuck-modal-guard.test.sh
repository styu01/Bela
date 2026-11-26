#!/bin/bash
# Contract tests for scripts/stuck-modal-guard.sh.
# Run: bash scripts/__tests__/stuck-modal-guard.test.sh
#
# Pins the pure pane classifier (idle / busy / stuck / empty) against captured
# pane fixtures and the confirm-window decision, so the false-positive guarantee
# ("a legitimately working session is NEVER touched") is locked by a test.

set -u

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
assert_eq() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected '$2', got '$3')"; fi; }

INSTALL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
GUARD="$INSTALL_DIR/scripts/stuck-modal-guard.sh"

classify() { printf '%s' "$1" | bash "$GUARD" classify; }
decide()   { bash "$GUARD" decide "$1" "$2" "$3"; }

echo "stuck-modal-guard tests"
echo "======================="

# ---------------------------------------------------------------------------
# (a) Classifier: healthy panes -> idle / busy (NEVER stuck)
# ---------------------------------------------------------------------------
echo ""
echo "(a) Healthy panes are never 'stuck'"

IDLE_BYPASS='Some reply text from the agent.
                                                      ⏵⏵ bypass permissions on (shift+tab to cycle)'
assert_eq "idle: bypass-permissions footer" "idle" "$(classify "$IDLE_BYPASS")"

IDLE_SHORTCUTS='╭─────────────╮
│ ❯           │
╰─────────────╯
  ? for shortcuts'
assert_eq "idle: '? for shortcuts' footer" "idle" "$(classify "$IDLE_SHORTCUTS")"

BUSY_INTERRUPT='✻ Combobulating… (8s · ↓ 1.2k tokens · esc to interrupt)
⏵⏵ bypass permissions on (shift+tab to cycle)'
assert_eq "busy: 'esc to interrupt' (working footer) -> busy NOT stuck" "busy" "$(classify "$BUSY_INTERRUPT")"

BUSY_TOKENS='✻ Thinking… (52s · ↓ 2.6k tokens)'
assert_eq "busy: token counter '(Ns · ↓' -> busy" "busy" "$(classify "$BUSY_TOKENS")"

# W2 regression: the counter separator may render as an ASCII period (locale /
# terminal dependent) instead of the Unicode middle-dot. A working pane MUST
# still classify as busy, never stuck (else it gets respawned mid-turn).
BUSY_TOKENS_ASCII='✻ Thinking… (52s . ↓ 2.6k tokens)'
assert_eq "busy: token counter '(Ns . ↓' ASCII-dot -> busy" "busy" "$(classify "$BUSY_TOKENS_ASCII")"

# ---------------------------------------------------------------------------
# (b) Classifier: a wedged /mcp modal -> stuck
# ---------------------------------------------------------------------------
echo ""
echo "(b) Wedged /mcp modal is 'stuck'"

MCP_MODAL='Manage MCP servers

  ❯ 1. telegram   ✗ failed
       Reconnect
       View tools

  Esc to go back'
assert_eq "stuck: /mcp modal (no idle/busy markers)" "stuck" "$(classify "$MCP_MODAL")"

TRUST_DIALOG='Do you trust the files in this folder?
  1. Yes
  2. No'
assert_eq "stuck: an unexpected blocking dialog with no footer" "stuck" "$(classify "$TRUST_DIALOG")"

# ---------------------------------------------------------------------------
# (c) Classifier: empty capture -> empty (inconclusive)
# ---------------------------------------------------------------------------
echo ""
echo "(c) Empty capture"
assert_eq "empty: whitespace-only pane" "empty" "$(classify '
   ')"

# ---------------------------------------------------------------------------
# (d) Decision: confirm-window persistence (anti-flap) + healthy-clears
# ---------------------------------------------------------------------------
echo ""
echo "(d) Confirm-window decision (STUCK_SECONDS=120)"
assert_eq "decide: first stuck sighting -> start-confirm" "start-confirm" "$(decide stuck 0 1000)"
assert_eq "decide: stuck 30s (< 120) -> wait-confirm"     "wait-confirm"  "$(decide stuck 970 1000)"
assert_eq "decide: stuck 200s (>= 120) -> act"            "act"           "$(decide stuck 800 1000)"
assert_eq "decide: idle -> clear confirm window"          "clear"         "$(decide idle 800 1000)"
assert_eq "decide: busy -> clear confirm window"          "clear"         "$(decide busy 800 1000)"
assert_eq "decide: empty capture -> hold (preserve window)" "hold"        "$(decide empty 800 1000)"

# Custom STUCK_SECONDS is honoured (env override)
assert_eq "decide: honours STUCK_MODAL_SECONDS override" "act" \
  "$(STUCK_MODAL_SECONDS=10 bash "$GUARD" decide stuck 980 1000)"
# F: a non-integer STUCK_MODAL_SECONDS must fall back to the default (not error
# the -ge comparison and short-circuit recovery). 200s elapsed >= 120 -> act.
assert_eq "decide: invalid STUCK_MODAL_SECONDS falls back to default" "act" \
  "$(STUCK_MODAL_SECONDS=garbage bash "$GUARD" decide stuck 800 1000)"

# ---------------------------------------------------------------------------
# (e) Model-id sanitization (W1) — safe to interpolate into the respawn string
# ---------------------------------------------------------------------------
echo ""
echo "(e) Model-id sanitization"
assert_eq "sanitize: legit bracketed model id is preserved" "claude-opus-4-8[1m]" \
  "$(bash "$GUARD" sanitize-model 'claude-opus-4-8[1m]')"
SAN="$(bash "$GUARD" sanitize-model "claude'; rm -rf / #")"
case "$SAN" in
  *"'"*|*";"*|*" "*|*"/"*|*'$'*|*'`'*|*'#'*) fail "sanitize: shell metacharacter survived: $SAN" ;;
  *) pass "sanitize: shell metacharacters stripped ($SAN)" ;;
esac

# ---------------------------------------------------------------------------
# (f) F1 — a missing state dir is created (no flock defer-forever, cold install)
# ---------------------------------------------------------------------------
echo ""
echo "(f) State-dir auto-create"
# CHANNELS_SESSION points at a non-existent session so the guard no-ops at the
# has-session check WITHOUT touching any real pane; the mkdir runs before that.
TMP_F1="$(mktemp -d)"; NOSTORE="$TMP_F1/sub"   # NOSTORE does not exist yet
# A tmux stub that knows no session: without tmux on PATH the guard stops before
# the mkdir (a CI runner or a container has none), so the test needs one.
mkdir -p "$TMP_F1/bin" && printf '#!/bin/sh\nexit 1\n' > "$TMP_F1/bin/tmux" && chmod +x "$TMP_F1/bin/tmux"
PATH="$TMP_F1/bin:$PATH" CHANNELS_SESSION="nonexistent-modal-guard-test-channels" STUCK_MODAL_STATE_DIR="$NOSTORE" \
  bash "$GUARD" >/dev/null 2>&1
[ -d "$NOSTORE" ] && pass "F1: run_guard creates a missing state dir (lock can't defer forever)" \
  || fail "F1: missing state dir not created — exec 9> on the lock would fail and defer recovery"
rm -rf "$TMP_F1"

# ---------------------------------------------------------------------------
echo ""
echo "(g2) PANEFOOTEROUTSIDE1114: the live footer, where the UI draws it, in every mode"

# Footers as Claude Code 2.1.325 draws them (measured captures, empty input
# box). Before this, only the bypass and the shortcuts footer counted, and the
# real, empty pane of the other modes classified as stuck.
RULE='────────────────────────────────────────────────────────────────────────────────'
mode_pane() { printf '⏺ Done.\n\n%s\n❯ \n%s\n%s' "$RULE" "$RULE" "$1"; }
for FOOT in '  ⏸ plan mode on (shift+tab to cycle)' \
            '  ⏵⏵ auto mode on (shift+tab to cycle)' \
            '  ⏵⏵ accept edits on (shift+tab to cycle)' \
            "  ⏵⏵ don't ask on (shift+tab to cycle)" \
            '  ⏸ manual mode on · ? for shortcuts' \
            '  ⏵⏵ bypass permissions on (shift+tab to cycle)'; do
  assert_eq "idle: empty pane in mode '$FOOT'" "idle" "$(classify "$(mode_pane "$FOOT")")"
  assert_eq "footer-line: the live footer in mode '$FOOT'" "$FOOT" "$(mode_pane "$FOOT" | bash "$GUARD" footer-line)"
done

# A line in the output that QUOTES the footer, then an open permission dialog
# instead of the input box and footer: stuck, not idle.
QUOTE_OVER_DIALOG='⏺ Done.
  ⎿  ⏵⏵ bypass permissions on · 1 shell · ↓ to manage
╭────────────────────────────────────────
 Bash command
   echo hi
 Do you want to proceed?
 ❯ 1. Yes
   2. No, and tell Claude what to do differently (esc)'
assert_eq "stuck: a quoted footer above an open dialog" "stuck" "$(classify "$QUOTE_OVER_DIALOG")"
assert_eq "footer-line: none above an open dialog" "" "$(printf '%s' "$QUOTE_OVER_DIALOG" | bash "$GUARD" footer-line)"
QUOTE_PLAIN_OVER_DIALOG='the log said: bypass permissions on and ? for shortcuts
 Do you want to proceed?
 ❯ 1. Yes
   2. No'
assert_eq "stuck: the bare footer words in prose above a dialog" "stuck" "$(classify "$QUOTE_PLAIN_OVER_DIALOG")"
# Under the last rule, a line with the footer words that does not START like the
# live footer is skipped; the live footer under it is the one.
UNDER_RULE="$(printf '%s\n❯ \n%s\n  ⚠ it said: bypass permissions on (shift+tab to cycle)\n  ⏸ plan mode on (shift+tab to cycle)' "$RULE" "$RULE")"
assert_eq "footer-line: a quote under the rule is skipped for the live footer" \
  "  ⏸ plan mode on (shift+tab to cycle)" "$(printf '%s' "$UNDER_RULE" | bash "$GUARD" footer-line)"
NO_START="$(printf '%s\n❯ \n%s\nbypass permissions on (shift+tab to cycle)' "$RULE" "$RULE")"
assert_eq "stuck: the footer words under the rule without the live start" "stuck" "$(classify "$NO_START")"

# Local: a working pane must never classify as stuck now that step 3 reads only the live footer (the guard would
# respawn it mid-turn). Measured 2.1.326 shapes: a turn past one minute, and the clipped busy footer.
LONG_TURN="$(printf '✢ Warping… (2m 39s · ↓ 11.2k tokens)\n\n%s\n❯ \n%s\n  ⏵⏵ bypass permissions on · install gh for PR status · 1 shell · esc to inte…' "$RULE" "$RULE")"
assert_eq "busy: a turn past one minute with the clipped busy footer (local)" "busy" "$(classify "$LONG_TURN")"
CLIPPED_ONLY="$(printf '%s\n❯ \n%s\n  ⏵⏵ bypass permissions on · install gh for PR status · 1 shell · esc to inte…' "$RULE" "$RULE")"
assert_eq "busy: the clipped busy footer alone (local)" "busy" "$(classify "$CLIPPED_ONLY")"
assert_eq "footer-line: a clipped BUSY footer is not the idle footer (local)" "" "$(printf '%s' "$CLIPPED_ONLY" | bash "$GUARD" footer-line)"
HOUR_TURN="$(printf '✻ Baking… (1h 2m 5s · ↓ 1.2k tokens)\n\n%s\n❯ \n%s\n  ⏵⏵ bypass permissions on (shift+tab to cycle)' "$RULE" "$RULE")"
assert_eq "busy: a turn past one hour (local)" "busy" "$(classify "$HOUR_TURN")"

# ---------------------------------------------------------------------------
echo ""
echo "======================="
TOTAL=$((PASS + FAIL))
echo "Results: $PASS/$TOTAL passed"
if [ "$FAIL" -gt 0 ]; then echo "FAILED: $FAIL tests"; exit 1; fi
echo "All tests passed."

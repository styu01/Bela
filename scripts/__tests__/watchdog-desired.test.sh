#!/bin/bash
# Contract tests for the watchdog's sub-agent restart decision (DESIREDLIST1128,
# KISPROGI 6543 F2).
#
# Regression origin (2026-11-28): the cron watchdog treated every folder under
# agents/ as an agent that must run, so every 5 minutes it logged "autobot /
# deepseek / iris / webchat-relay missing, restarting..." and only their
# missing bot token stopped the start. webchat-relay is not an agent at all and
# must never be started.
#
# Driven only through `watchdog.sh --decide <dir> [desired-file]`, which decides
# and exits before the lock, tmux, the dashboard API or the log. The full
# watchdog cycle is NEVER run here (standing rule: no ops loop in tests).
# Run: bash scripts/__tests__/watchdog-desired.test.sh

set -u

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1 -- got: $2"; }

INSTALL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
WATCHDOG="${WATCHDOG_BIN:-$INSTALL_DIR/scripts/watchdog.sh}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# $1 = agent name, $2 = "config" | "noconfig" | "linkconfig", $3 = optional marker file
make_agent() {
  local dir="$TMP/agents/$1"
  mkdir -p "$dir"
  case "$2" in
    config) printf '{"model":"claude-opus-5-5"}' > "$dir/agent-config.json" ;;
    linkconfig) printf '{}' > "$TMP/real-config.json"; ln -s "$TMP/real-config.json" "$dir/agent-config.json" ;;
  esac
  [ -n "${3:-}" ] && printf 'x' > "$dir/$3"
  echo "$dir"
}

DESIRED="$TMP/agents-desired.json"
printf '[\n  "kisprogi",\n  "okoska",\n  "progi",\n  "vizsla",\n  "webchat-relay",\n  "marked"\n]' > "$DESIRED"

expect() {
  local got
  got=$(bash "$WATCHDOG" --decide "$2" "$3" 2>&1)
  if [ "$got" = "$4" ]; then pass "$1"; else fail "$1" "$got"; fi
}

echo "watchdog restart-decision tests"
echo "==============================="

echo "(a) the desired list decides"
for a in kisprogi okoska progi vizsla; do
  expect "$a on the list is restarted" "$(make_agent "$a" config)" "$DESIRED" "restart"
done
for a in autobot deepseek iris; do
  expect "$a (on-demand, not on the list) is skipped" "$(make_agent "$a" config)" "$DESIRED" "skip:not-desired"
done
expect "trailing slash in the folder path" "$TMP/agents/progi/" "$DESIRED" "restart"

echo "(b) never-start identities, even when listed"
expect "webchat-relay is never started" "$(make_agent webchat-relay noconfig .p597-owner-webchat-bridge)" "$DESIRED" "skip:never-start"
mkdir -p "$TMP/other/webchat-relay"; printf '{}' > "$TMP/other/webchat-relay/agent-config.json"
expect "webchat-relay by name alone (agent-config, no marker) is never started" "$TMP/other/webchat-relay" "$DESIRED" "skip:never-start"
expect "a folder with the bridge marker is never started" "$(make_agent marked config .p597-owner-webchat-bridge)" "$DESIRED" "skip:never-start"
mkdir -p "$TMP/agents/linkmarked"; printf '{}' > "$TMP/agents/linkmarked/agent-config.json"; ln -s /nonexistent "$TMP/agents/linkmarked/.p597-owner-webchat-bridge"
expect "a dangling marker link counts as the marker" "$TMP/agents/linkmarked" "$DESIRED" "skip:never-start"

echo "(c) not an agent folder"
printf '["noconf","linkconf"]' > "$TMP/d2.json"
expect "no agent-config.json" "$(make_agent noconf noconfig)" "$TMP/d2.json" "skip:no-agent-config"
expect "agent-config.json as a link" "$(make_agent linkconf linkconfig)" "$TMP/d2.json" "skip:no-agent-config"

echo "(d) an unusable desired list restarts nothing"
P="$(make_agent progi config)"
expect "missing list" "$P" "$TMP/none.json" "skip:desired-list-missing"
printf '{bad' > "$TMP/bad.json"; expect "corrupt list" "$P" "$TMP/bad.json" "skip:desired-list-unreadable"
printf '{"progi":true}' > "$TMP/obj.json"; expect "object, not a list" "$P" "$TMP/obj.json" "skip:desired-list-unreadable"
printf '"progi"' > "$TMP/str.json"; expect "string, not a list" "$P" "$TMP/str.json" "skip:desired-list-unreadable"
mkdir -p "$TMP/dirlist.json"; expect "a folder in place of the list" "$P" "$TMP/dirlist.json" "skip:desired-list-unreadable"
ln -s /nonexistent "$TMP/dangling.json"; expect "a dangling link in place of the list" "$P" "$TMP/dangling.json" "skip:desired-list-unreadable"
printf '[1, null, ["progi"], "prog"]' > "$TMP/odd.json"; expect "only exact string entries count" "$P" "$TMP/odd.json" "skip:not-desired"
printf '[]' > "$TMP/empty.json"; expect "empty list" "$P" "$TMP/empty.json" "skip:not-desired"

echo "(e) the cycle asks the decision before any restart step"
LOOP="$(sed -n '/^for AGENT_DIR in /,/^done$/p' "$WATCHDOG")"
dec_line=$(printf '%s\n' "$LOOP" | grep -n 'watchdog_restart_decision' | head -1 | cut -d: -f1)
restart_line=$(printf '%s\n' "$LOOP" | grep -n 'missing, restarting' | head -1 | cut -d: -f1)
spawn_line=$(printf '%s\n' "$LOOP" | grep -n 'tmux new-session' | head -1 | cut -d: -f1)
if [ -n "$dec_line" ] && [ -n "$restart_line" ] && [ -n "$spawn_line" ] && [ "$dec_line" -lt "$restart_line" ] && [ "$dec_line" -lt "$spawn_line" ]; then
  pass "decision precedes the restart log line and the spawn"
else
  fail "decision precedes the restart log line and the spawn" "dec=$dec_line restart=$restart_line spawn=$spawn_line"
fi
if printf '%s\n' "$LOOP" | grep -q '\[ "\$DECISION" = restart \] || continue'; then
  pass "anything but restart skips the agent"
else
  fail "anything but restart skips the agent" "guard line not found"
fi

echo ""
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]

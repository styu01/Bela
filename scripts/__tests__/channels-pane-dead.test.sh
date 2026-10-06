#!/bin/bash
set -u

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1 -- expected: $2, got: $3"; }

INSTALL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
CHANNELS="${CHANNELS_BIN:-$INSTALL_DIR/scripts/channels.sh}"
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
FAKE="$TMPD/tmux"

expect_check() {
  printf '#!/bin/sh\n%s\n' "$3" > "$FAKE"; chmod +x "$FAKE"
  local got
  got="$(CHANNELS_TMUX_BIN="$FAKE" bash "$CHANNELS" --pane-dead-check marveen-channels 2>/dev/null)"
  if [ "$got" = "$2" ]; then pass "$1"; else fail "$1" "$2" "$got"; fi
}

echo "channels.sh pane-dead detector"
expect_check "single dead pane" dead 'echo 1'
expect_check "single live pane" alive 'echo 0'
expect_check "several panes, one dead" dead 'printf "0\n1\n0\n"'
expect_check "failed tmux query is fail-safe alive" alive 'exit 1'
expect_check "garbage is alive" alive 'echo no-server'

printf '#!/bin/sh\ncase "$*" in *"-t marveen-channels "*) echo 1;; *) echo 0;; esac\n' > "$FAKE"; chmod +x "$FAKE"
got="$(CHANNELS_TMUX_BIN="$FAKE" bash "$CHANNELS" --pane-dead-check marveen-channels 2>/dev/null)"
if [ "$got" = dead ]; then pass "uses requested session"; else fail "uses requested session" dead "$got"; fi

echo "passed: $PASS failed: $FAIL"
[ "$FAIL" -eq 0 ]

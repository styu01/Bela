#!/bin/bash
# Contract tests for the watchdog's restart replay (REPLAYOFF1127, BELA msg 6167).
# Run: bash scripts/__tests__/watchdog-replay.test.sh
#
# What is pinned:
#   1. By default (MARVEEN_WATCHDOG_REPLAY unset or 0) a restart replays NOTHING: no dashboard
#      request, no keystroke into the pane, and one clear log line saying so. Without the
#      server-side seen state, 'delivered' rows include already-processed ones (measured: one
#      restart would have replayed 12 handled messages into kisprogi).
#   2. The code behind the switch stays correct for the day it is turned on:
#      - it asks /api/messages with `agent=` (never the unknown `to=`),
#      - a non-200 answer is logged and replays nothing (curl without -f exits 0 on a 400),
#      - a non-list body replays nothing (iterating an error dict walks its keys),
#      - only delivered, not completed, recent rows addressed to the agent are replayed,
#      - a chunk that ends in ';' goes out with the ';' escaped (tmux would drop it).
#
# Hermetic: the script under test is COPIED into a temp install (its INSTALL_DIR, token, .env and
# lock all live there), the dashboard is a local stub, tmux is a fake binary that records its argv
# (WATCHDOG_TMUX_BIN), and the boot wait is zeroed (WATCHDOG_REPLAY_BOOT_SEC). Nothing reaches a
# real pane or the real dashboard.

set -u

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
assert_eq() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected '$2', got '$3')"; fi; }
assert_has() { case "$3" in *"$2"*) pass "$1" ;; *) fail "$1 (missing '$2' in: $3)" ;; esac; }

INSTALL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="${WATCHDOG_BIN:-$INSTALL_DIR/scripts/watchdog.sh}"

TMP="$(mktemp -d)"
STUB_PID=""
trap '[ -n "$STUB_PID" ] && kill "$STUB_PID" 2>/dev/null; rm -rf "$TMP"' EXIT

INST="$TMP/inst"
mkdir -p "$INST/scripts" "$INST/store" "$INST/logs"
cp "$SRC" "$INST/scripts/watchdog.sh"
echo "test-token" > "$INST/store/.dashboard-token"

REQLOG="$TMP/requests.log"; PORTFILE="$TMP/port"; MODEFILE="$TMP/mode"
cat > "$TMP/stub.py" <<'PYEOF'
import json, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
reqlog, portfile, modefile = sys.argv[1:4]
AGENT = "testagent"
now = int(time.time())
# The 2nd row's text is sized so that the replayed text's first 990-char chunk ends in ';'.
prefix = "[Újraküldés - feladat elveszett restart előtt]: "
semi = "S" * (989 - len(prefix)) + ";" + "MARADEK"
ROWS = [
    {"id": 1, "from_agent": "bela", "to_agent": AGENT, "status": "delivered", "completed_at": None, "created_at": now - 600, "content": "ELSO_FELADAT"},
    {"id": 2, "from_agent": "bela", "to_agent": AGENT, "status": "delivered", "completed_at": None, "created_at": now - 300, "content": semi},
    {"id": 3, "from_agent": "bela", "to_agent": AGENT, "status": "done", "completed_at": now - 100, "created_at": now - 200, "content": "KESZ_SOR"},
    {"id": 4, "from_agent": "bela", "to_agent": AGENT, "status": "delivered", "completed_at": None, "created_at": now - 3 * 3600, "content": "REGI_SOR"},
    {"id": 5, "from_agent": AGENT, "to_agent": "bela", "status": "delivered", "completed_at": None, "created_at": now - 50, "content": "KIMENO_SOR"},
]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        with open(reqlog, "a") as f:
            f.write(json.dumps({"path": u.path, "query": q, "auth": self.headers.get("Authorization")}) + "\n")
        mode = open(modefile).read().strip()
        if "to" in q or mode == "400":
            code, body = 400, {"error": "unknown query parameter", "unknown": ["to"]}
        elif mode == "dict":
            code, body = 200, {"error": "something else", "rows": ROWS}
        else:
            code, body = 200, ROWS
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
srv = HTTPServer(("127.0.0.1", 0), H)
open(portfile, "w").write(str(srv.server_port))
srv.serve_forever()
PYEOF
echo ok > "$MODEFILE"
python3 "$TMP/stub.py" "$REQLOG" "$PORTFILE" "$MODEFILE" &
STUB_PID=$!
disown "$STUB_PID" 2>/dev/null || true
for _ in $(seq 1 50); do [ -s "$PORTFILE" ] && break; sleep 0.1; done
[ -s "$PORTFILE" ] || { echo "FATAL: the dashboard stub did not start"; exit 1; }
echo "WEB_PORT=$(cat "$PORTFILE")" > "$INST/.env"

# Fake tmux: records each invocation's argv as one JSON line.
TMUXLOG="$TMP/tmux.log"
cat > "$TMP/fake-tmux" <<PYEOF
#!/usr/bin/env python3
import json, sys
with open("$TMUXLOG", "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
PYEOF
chmod +x "$TMP/fake-tmux"

run_replay() { # replay-switch-value ("" = unset)
  : > "$REQLOG"; : > "$TMUXLOG"
  if [ -z "$1" ]; then
    env -u MARVEEN_WATCHDOG_REPLAY WATCHDOG_TMUX_BIN="$TMP/fake-tmux" WATCHDOG_REPLAY_BOOT_SEC=0 \
      bash "$INST/scripts/watchdog.sh" --replay-only testagent agent-testagent 2>&1
  else
    MARVEEN_WATCHDOG_REPLAY="$1" WATCHDOG_TMUX_BIN="$TMP/fake-tmux" WATCHDOG_REPLAY_BOOT_SEC=0 \
      bash "$INST/scripts/watchdog.sh" --replay-only testagent agent-testagent 2>&1
  fi
}
lines() { if [ -s "$1" ]; then wc -l < "$1" | tr -d ' '; else echo 0; fi; }
sendkeys_literal() { # all -l payloads, in order, one per line
  python3 - "$TMUXLOG" <<'PYEOF'
import json, sys
for line in open(sys.argv[1]):
    a = json.loads(line)
    if "send-keys" in a and "-l" in a:
        print(a[a.index("-l") + 1])
PYEOF
}
enters() { python3 -c "import json,sys; print(sum(1 for l in open(sys.argv[1]) if json.loads(l)[-1:]==['Enter']))" "$TMUXLOG"; }

echo "watchdog restart replay (REPLAYOFF1127)"
echo "======================================="

echo ""
echo "1. default: replay OFF"
OUT="$(run_replay "")"
assert_has "the log line says the replay is off and replayed nothing" "replay kikapcsolva (nincs seen-allapot), 0 uzenet visszajatszva" "$OUT"
assert_eq "no dashboard request" "0" "$(lines "$REQLOG")"
assert_eq "no keystroke into the pane" "0" "$(lines "$TMUXLOG")"
OUT="$(run_replay 0)"
assert_has "MARVEEN_WATCHDOG_REPLAY=0 is the same: off" "replay kikapcsolva" "$OUT"
assert_eq "=0: no keystroke" "0" "$(lines "$TMUXLOG")"
OUT="$(run_replay yes)"
assert_has "only the exact value 1 turns it on ('yes' stays off)" "replay kikapcsolva" "$OUT"

echo ""
echo "2. switched on: the code behind the switch"
echo 400 > "$MODEFILE"
OUT="$(run_replay 1)"
assert_has "a 400 answer is logged with its status" "/api/messages HTTP 400, replay kihagyva" "$OUT"
assert_eq "a 400 answer: no keystroke" "0" "$(lines "$TMUXLOG")"

echo dict > "$MODEFILE"
OUT="$(run_replay 1)"
assert_has "a 200 with a non-list body is refused" "body is not a list, replay kihagyva" "$OUT"
assert_eq "a non-list body: no keystroke" "0" "$(lines "$TMUXLOG")"

echo ok > "$MODEFILE"
OUT="$(run_replay 1)"
Q="$(python3 -c "import json,sys; d=json.loads(open(sys.argv[1]).readline()); print(sorted(d['query']), d['query'].get('agent'), d['auth'])" "$REQLOG")"
assert_eq "asks with agent= (and limit=), never to=" "['agent', 'limit'] ['testagent'] Bearer test-token" "$Q"
assert_has "logs the replay count" "testagent: replaying 2 unfinished message(s)" "$OUT"
PAYLOADS="$(sendkeys_literal)"
assert_has "row 1 (delivered, recent) is replayed" "ELSO_FELADAT" "$PAYLOADS"
assert_eq "the done row is not replayed" "0" "$(printf '%s' "$PAYLOADS" | grep -c KESZ_SOR)"
assert_eq "the >2h old row is not replayed" "0" "$(printf '%s' "$PAYLOADS" | grep -c REGI_SOR)"
assert_eq "a row FROM the agent is not replayed" "0" "$(printf '%s' "$PAYLOADS" | grep -c KIMENO_SOR)"
assert_eq "one Enter per replayed message" "2" "$(enters)"
SEMI_CHUNK="$(printf '%s\n' "$PAYLOADS" | grep -E '^\[Újraküldés.*SSSS' | head -1)"
assert_eq "the chunk ending in ';' is sent with the ';' escaped" '\;' "${SEMI_CHUNK: -2}"
assert_eq "the escaped chunk is 990 chars + 1 (only the last char changed)" "991" "$(printf '%s' "$SEMI_CHUNK" | python3 -c 'import sys; print(len(sys.stdin.read()))')"
assert_has "the rest of that message follows in the next chunk" "MARADEK" "$PAYLOADS"
NO_SEMI_ESC="$(printf '%s\n' "$PAYLOADS" | grep -c 'ELSO_FELADAT\\;')"
assert_eq "a chunk NOT ending in ';' is untouched" "0" "$NO_SEMI_ESC"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]

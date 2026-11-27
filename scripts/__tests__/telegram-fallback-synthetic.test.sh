#!/bin/bash
# Contract tests (upstream #2269, a21f3e5b, ported onto this install's per-entry watchdog):
#   1. a CLI-written last line never goes out as the agent's reply, on BOTH fallback senders
#      (the Stop hook's guaranteed fallback and the watchdog's backstop);
#   2. a real fallback send leaves exactly one outbound ledger row, written after the pending
#      state is gone;
#   3. (watchdog) a line the harness writes INSIDE a round (isMeta without origin: the Stop hook's
#      own "VÁLASZOLATLAN TELEGRAM" nudge, a skill load) does not end the round, while an inbound
#      Telegram message (isMeta WITH origin {kind: channel}, CC 2.1.326) still starts one;
#   4. (watchdog) the 15-min backstop does not fire on an OPEN round that wrote within WEDGED_SEC,
#      and still fires once that round is quiet, or once it has ended.
# Run: bash scripts/__tests__/telegram-fallback-synthetic.test.sh
#
#   (a) isApiErrorMessage / model "<synthetic>" with "You've hit your session limit"
#         -> placeholder edited to the outage text, no sendMessage, no delete, no ledger row
#   (b) model "<synthetic>" with "No response requested."
#         -> nothing sent, no outage edit, no ledger row
#   (c) a real model id whose genuine reply starts with "API error"
#         -> sent once (no false positive), one outbound ledger row with the path's prefix,
#            written only after the pending entry is removed
#
# Hermetic: Bot API traffic goes to a local stub (TELEGRAM_API_BASE), the ledger is a sandbox
# SQLite file (LEDGER_DB_PATH), each case has its own state dir, nothing touches tmux.

set -u

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
assert_eq() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected '$2', got '$3')"; fi; }

INSTALL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
STOP_HOOK="${STOP_HOOK_BIN:-$INSTALL_DIR/scripts/hooks/telegram_progress_clear.py}"
WATCHDOG="${WATCHDOG_BIN:-$INSTALL_DIR/scripts/hooks/telegram_progress_watchdog.py}"
export MARVEEN_ROOT="$INSTALL_DIR"
export MAIN_AGENT_ID="mainagent"

TMP="$(mktemp -d)"
STUB_PID=""
trap '[ -n "$STUB_PID" ] && kill "$STUB_PID" 2>/dev/null; rm -rf "$TMP"' EXIT

REQLOG="$TMP/requests.log"
PORTFILE="$TMP/port"
cat > "$TMP/stub.py" <<'PYEOF'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
reqlog = sys.argv[1]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8") if n else ""
        method = self.path.rsplit("/", 1)[-1]
        with open(reqlog, "a", encoding="utf-8") as f:
            f.write(f"{method} {body}\n")
        out = {"ok": True, "result": {"message_id": 9001} if method == "sendMessage" else True}
        payload = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
srv = HTTPServer(("127.0.0.1", 0), H)
open(sys.argv[2], "w").write(str(srv.server_port))
srv.serve_forever()
PYEOF
python3 "$TMP/stub.py" "$REQLOG" "$PORTFILE" &
STUB_PID=$!
disown "$STUB_PID" 2>/dev/null || true
for _ in $(seq 1 50); do [ -s "$PORTFILE" ] && break; sleep 0.1; done
[ -s "$PORTFILE" ] || { echo "FAIL: the Bot API stub did not start"; exit 1; }
export TELEGRAM_API_BASE="http://127.0.0.1:$(cat "$PORTFILE")"

# The three assistant-line fixtures (shapes measured in this install's main transcripts).
LINE_LIMIT='{"type":"assistant","isApiErrorMessage":true,"message":{"role":"assistant","model":"<synthetic>","content":[{"type":"text","text":"You'"'"'ve hit your session limit · resets 12am (UTC)"}]}}'
LINE_NORESP='{"type":"assistant","message":{"role":"assistant","model":"<synthetic>","content":[{"type":"text","text":"No response requested."}]}}'
LINE_REAL='{"type":"assistant","message":{"role":"assistant","model":"claude-opus-5-5","content":[{"type":"text","text":"API error 429 means the upstream rate limit; retry after the reset."}]}}'

# State dir: token, a transcript, one pending entry (chat 111 / message 5) whose created_at is
# AGE seconds ago, and an empty sandbox ledger. The transcript is built by a python layout
# (see below) so every line can carry its own timestamp relative to created_at.
mkstate() { # name layout age [extra-line]
  local sd="$TMP/$1"
  mkdir -p "$sd/progress"
  echo "TELEGRAM_BOT_TOKEN=stub-token" > "$sd/.env"
  python3 - "$sd" "$2" "$3" "${4:-}" <<'PYEOF'
import datetime, json, os, sqlite3, sys, time
sd, layout, age, extra = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
now = time.time()
created = now - age
def iso(t): return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
def prompt(t, text="kerdes"): return {"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": text}}
def chan_prompt(t):  # an inbound Telegram message as CC 2.1.326 writes it: isMeta WITH an origin
    return {"type": "user", "isMeta": True, "origin": {"kind": "channel", "server": "plugin:telegram:telegram"},
            "timestamp": iso(t), "message": {"role": "user", "content": "<channel source=\"plugin:telegram:telegram\">kerdes</channel>"}}
def nudge(t):        # the Stop hook's own nudge: isMeta, NO origin
    return {"type": "user", "isMeta": True, "timestamp": iso(t),
            "message": {"role": "user", "content": "Stop hook feedback:\nKÖTELEZŐ: erre a Telegram-üzenetre még NEM küldtél választ"}}
def text(t, s): return {"type": "assistant", "timestamp": iso(t), "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": s}]}}
def reply_call(t, i): return {"type": "assistant", "timestamp": iso(t), "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "tool_use", "id": i, "name": "mcp__plugin_telegram_telegram__reply"}]}}
def result(t, i): return {"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": i}]}}
if layout == "line":           # prompt at created_at, then the given assistant line
    ev = [prompt(created), json.loads(extra)]
elif layout == "nudge-then-reply":   # answer, nudge, then the real reply call WITH a result
    ev = [prompt(created), text(created + 5, "ELSO_SZOVEG a nudge elott"), nudge(created + 10),
          text(created + 15, "VALASZ_A_NUDGE_UTAN"), reply_call(created + 16, "r1"), result(created + 17, "r1")]
elif layout == "channel-anchor":     # the round is opened by an isMeta+origin channel line
    ev = [chan_prompt(created), text(created + 5, "CSATORNA_VALASZ")]
elif layout.startswith("open-last-"):  # open round, last write N seconds ago
    ago = int(layout.rsplit("-", 1)[1])
    ev = [prompt(created), text(now - ago, "MEG_DOLGOZOM")]
elif layout == "ended-recent":       # the round wrote 110 s ago and then ENDED (a later prompt 100 s ago)
    ev = [prompt(created), text(now - 110, "LEZART_KOR_VALASZA"), prompt(now - 100, "masik kor"), text(now - 30, "MASIK_KOR")]
else:
    raise SystemExit("unknown layout " + layout)
tr = os.path.join(sd, "transcript.jsonl")
with open(tr, "w") as f:
    for e in ev: f.write(json.dumps(e, ensure_ascii=False) + "\n")
p = os.path.join(sd, "progress", "sid1.json")
json.dump([{"chat_id": "111", "message_id": 5, "transcript_path": tr, "created_at": created}], open(p, "w"))
os.utime(p, (created, created))
sqlite3.connect(os.path.join(sd, "ledger.db")).close()
PYEOF
  echo "$sd"
}

# Both runners load the hook as a module and wrap its ledger_log_out, so the test can see
# whether the pending file still existed at the moment of the ledger write.
cat > "$TMP/run_stop.py" <<'PYEOF'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("clear", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
sd = sys.argv[2]
orig = getattr(m, "ledger_log_out", None)   # absent in a pre-#2269 hook (the before-run)
def wrapped(*a, **k):
    with open(os.path.join(sd, "ledger-order"), "a") as f:
        f.write("pending-present\n" if os.path.exists(os.path.join(sd, "progress", "sid1.json")) else "pending-gone\n")
    return orig(*a, **k)
if orig is not None:
    m.ledger_log_out = wrapped
m.main()
PYEOF
run_stop() { # state dir
  : > "$REQLOG"
  printf '{"session_id":"sid1","transcript_path":"%s","stop_hook_active":true,"cwd":"%s"}' \
      "$1/transcript.jsonl" "$INSTALL_DIR/agents/progi" \
    | TELEGRAM_STATE_DIR="$1" LEDGER_DB_PATH="$1/ledger.db" python3 "$TMP/run_stop.py" "$STOP_HOOK" "$1" >/dev/null 2>&1
}
run_watchdog() { # state dir
  : > "$REQLOG"
  LEDGER_DB_PATH="$1/ledger.db" python3 - "$WATCHDOG" "$1" <<'PYEOF' >/dev/null 2>&1
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("wd", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
sd = sys.argv[2]
orig = getattr(m, "ledger_log_out", None)   # absent in a pre-#2269 hook (the before-run)
def wrapped(*a, **k):
    with open(os.path.join(sd, "ledger-order"), "a") as f:
        f.write("pending-present\n" if os.path.exists(os.path.join(sd, "progress", "sid1.json")) else "pending-gone\n")
    return orig(*a, **k)
if orig is not None:
    m.ledger_log_out = wrapped
m.handle_dir(os.path.join(sd, "progress"))
PYEOF
}

count() { grep -c "^$1 " "$REQLOG" 2>/dev/null || true; }
sent_has() { grep '^sendMessage ' "$REQLOG" | grep -c "$1" || true; }
outage_edits() { # editMessageText calls whose (JSON-decoded) text is the outage text
  python3 - "$REQLOG" <<'PYEOF'
import json, sys
n = 0
for line in open(sys.argv[1], encoding="utf-8"):
    method, _, body = line.rstrip("\n").partition(" ")
    if method == "editMessageText":
        try:
            n += "átmeneti kiesés" in json.loads(body).get("text", "")
        except Exception:
            pass
print(n)
PYEOF
}
ledger_q() { # db sql -> scalar
  python3 - "$1" "$2" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    print(con.execute(sys.argv[2]).fetchone()[0])
except sqlite3.OperationalError:
    print(0)
PYEOF
}
ledger_all() { ledger_q "$1" "SELECT COUNT(*) FROM conversation_log WHERE direction='out'"; }
ledger_prefixed() { ledger_q "$1" "SELECT COUNT(*) FROM conversation_log WHERE direction='out' AND text LIKE '$2%'"; }
pending() { [ -f "$1/progress/sid1.json" ] && echo yes || echo no; }

echo "telegram fallback synthetic + ledger + round scope"
echo "=================================================="
for path in stop watchdog; do
  echo "== $path"
  if [ "$path" = stop ]; then PREFIX="[telegram-progress-clear, fallback]"; else PREFIX="[telegram-progress-watchdog]"; fi
  run() { if [ "$path" = stop ]; then run_stop "$1"; else run_watchdog "$1"; fi; }

  sd="$(mkstate "$path-a" line 1200 "$LINE_LIMIT")"; run "$sd"
  assert_eq "$path (a) limit line: no sendMessage" "0" "$(count sendMessage)"
  assert_eq "$path (a) limit line: placeholder edited to the outage text" "1" "$(outage_edits)"
  assert_eq "$path (a) limit line: placeholder stays (no delete)" "0" "$(count deleteMessage)"
  assert_eq "$path (a) limit line: no ledger row" "0" "$(ledger_all "$sd/ledger.db")"

  sd="$(mkstate "$path-b" line 1200 "$LINE_NORESP")"; run "$sd"
  assert_eq "$path (b) 'No response requested.': no sendMessage" "0" "$(count sendMessage)"
  assert_eq "$path (b) 'No response requested.': no outage edit" "0" "$(outage_edits)"
  assert_eq "$path (b) 'No response requested.': no ledger row" "0" "$(ledger_all "$sd/ledger.db")"

  sd="$(mkstate "$path-c" line 1200 "$LINE_REAL")"; run "$sd"
  assert_eq "$path (c) real reply starting 'API error': sent once" "1" "$(count sendMessage)"
  assert_eq "$path (c) real reply: the sent text is the reply" "1" "$(sent_has 'upstream rate limit')"
  assert_eq "$path (c) real reply: no outage edit" "0" "$(outage_edits)"
  assert_eq "$path (c) real reply: one outbound ledger row" "1" "$(ledger_all "$sd/ledger.db")"
  assert_eq "$path (c) real reply: the row carries the path's prefix" "1" "$(ledger_prefixed "$sd/ledger.db" "$PREFIX")"
  assert_eq "$path (c) real reply: ledger written after the pending state is gone" "pending-gone" "$( { tr '\n' ' ' < "$sd/ledger-order"; } 2>/dev/null | sed 's/ $//')"
  if [ "$path" = stop ]; then
    # LOCAL: the agent id is the one ledger-outbound.py derives from the session cwd.
    assert_eq "stop (c) the row's agent is the cwd's agent (agents/progi -> progi)" "1" "$(ledger_q "$sd/ledger.db" "SELECT COUNT(*) FROM conversation_log WHERE direction='out' AND agent_id='progi'")"
  else
    assert_eq "watchdog (c) the row's agent is the main agent (no agents/<name> in the dir)" "1" "$(ledger_q "$sd/ledger.db" "SELECT COUNT(*) FROM conversation_log WHERE direction='out' AND agent_id='mainagent'")"
  fi
done

echo "== watchdog: round scope (isMeta lines)"
sd="$(mkstate wd-nudge nudge-then-reply 1200)"; run_watchdog "$sd"
assert_eq "nudge inside the round: the reply after it counts -> nothing resent" "0" "$(count sendMessage)"
assert_eq "nudge inside the round: placeholder cleared (reply-already-delivered)" "1" "$(count deleteMessage)"
assert_eq "nudge inside the round: no ledger row (nothing was sent)" "0" "$(ledger_all "$sd/ledger.db")"
sd="$(mkstate wd-chan channel-anchor 1200)"; run_watchdog "$sd"
assert_eq "an isMeta channel line WITH origin still anchors the round: its answer is delivered" "1" "$(sent_has CSATORNA_VALASZ)"
assert_eq "channel anchor: no generic-error edit" "0" "$(count editMessageText)"

echo "== watchdog: the backstop and an open round"
sd="$(mkstate wd-open60 open-last-60 1200)"; run_watchdog "$sd"
assert_eq "open round, wrote 60 s ago, marker 20 min old: no send" "0" "$(count sendMessage)"
assert_eq "open round, wrote 60 s ago: no edit" "0" "$(count editMessageText)"
assert_eq "open round, wrote 60 s ago: entry kept" "yes" "$(pending "$sd")"
sd="$(mkstate wd-open950 open-last-950 1200)"; run_watchdog "$sd"
assert_eq "open round silent for 950 s (> WEDGED_SEC): the backstop fires" "1" "$(count sendMessage)"
assert_eq "open round silent: entry handled" "no" "$(pending "$sd")"
sd="$(mkstate wd-ended ended-recent 1200)"; run_watchdog "$sd"
assert_eq "ended round (its last write 110 s ago, then a new prompt): the backstop fires as before" "1" "$(sent_has LEZART_KOR_VALASZA)"
sd="$(mkstate wd-young open-last-60 300)"; run_watchdog "$sd"
assert_eq "control: a 5-min-old open round is untouched (below the backstop)" "0" "$(count sendMessage)"

echo
echo "telegram fallback synthetic + ledger + round scope: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]

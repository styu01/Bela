#!/bin/bash
# Watchdog: checks sessions every 5 minutes, restarts if missing.
# Cron: */5 * * * * ~/marveen/scripts/watchdog.sh

# Cron's shell has no DBUS_SESSION_BUS_ADDRESS / XDG_RUNTIME_DIR (those are
# only set automatically in an interactive login session), so every
# `systemctl --user ...` call below fails with "Failed to connect to user
# scope bus" when this script runs from cron -- silently, since the fallback
# HTTP re-check after the failed restart often still sees the service come
# back up via an unrelated path (systemd's own Restart=always, or a manual
# restart), masking that THIS script's own restart attempt never worked.
# `loginctl enable-linger` (assumed enabled for this user) keeps the user
# manager and its bus socket alive at this fixed path with no session
# required; naming it explicitly is enough. Kanban 2bf0ae50, root-caused and
# verified via `env -i XDG_RUNTIME_DIR=... DBUS_SESSION_BUS_ADDRESS=...`.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"

INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$INSTALL_DIR/logs/watchdog.log"
mkdir -p "$INSTALL_DIR/logs"

# Dashboard port: config-driven (.env WEB_PORT), default 3420.
[ -f "$INSTALL_DIR/.env" ] && WEB_PORT="$(grep -E '^WEB_PORT=' "$INSTALL_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')"
WEB_PORT="${WEB_PORT:-3420}"

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

# Resolve which channel an agent must be respawned on, from its OWN
# agent-config.json channelProvider -- the same field src/web/agent-process.ts
# launches from. Sets AGENT_PROVIDER / TOKEN_VAR / STATE_ENV_VAR.
#
# Hardcoding telegram here had two failure modes for a non-telegram agent:
# (a) no TELEGRAM_BOT_TOKEN in its .env, so the loop hit "no bot token,
# skipping" and the watchdog NEVER restarted it -- precisely the agent the
# watchdog exists for stayed dead, while the log line looked routine; or
# (b) it came back on the wrong channel and was mute on its real one.
#
# Unknown / missing / malformed provider falls back to telegram, matching
# the pre-existing default so single-channel telegram installs are unaffected.
resolve_agent_provider() {
  local agent_dir="$1"
  AGENT_PROVIDER=$(python3 -c "import json; d=json.load(open('$agent_dir/agent-config.json')); print(d.get('channelProvider','telegram'))" 2>/dev/null || echo telegram)
  [ -n "$AGENT_PROVIDER" ] || AGENT_PROVIDER=telegram
  case "$AGENT_PROVIDER" in
    slack)      TOKEN_VAR="SLACK_BOT_TOKEN";      STATE_ENV_VAR="SLACK_STATE_DIR" ;;
    discord)    TOKEN_VAR="DISCORD_BOT_TOKEN";    STATE_ENV_VAR="DISCORD_STATE_DIR" ;;
    teams)      TOKEN_VAR="TEAMS_BOT_TOKEN";      STATE_ENV_VAR="TEAMS_STATE_DIR" ;;
    googlechat) TOKEN_VAR="GOOGLECHAT_BOT_TOKEN"; STATE_ENV_VAR="GOOGLECHAT_STATE_DIR" ;;
    *)          AGENT_PROVIDER=telegram; TOKEN_VAR="TELEGRAM_BOT_TOKEN"; STATE_ENV_VAR="TELEGRAM_STATE_DIR" ;;
  esac
}

# Self-test hook: print the resolution for one agent dir and exit, without
# touching tmux, the dashboard API or the log. Lets the contract be tested
# from fixtures (scripts/__tests__/watchdog-provider.test.sh) instead of
# requiring a live agent with a real bot token.
if [ "${1:-}" = "--resolve-provider" ]; then
  [ -n "${2:-}" ] || { echo "usage: watchdog.sh --resolve-provider <agent-dir>" >&2; exit 2; }
  resolve_agent_provider "$2"
  echo "provider=$AGENT_PROVIDER token_var=$TOKEN_VAR state_env_var=$STATE_ENV_VAR"
  exit 0
fi

# MIOAGENTINSTALL1114 (upstream 9301a1dd, plan 7.4, 7.5; ported, BELA 6404): the package start guard. This loop starts
# agents WITHOUT startAgentProcess, so it carries the server guard's three
# conditions itself, with the same texts:
#   1. the name is on the hidden list the server writes (store/agent-package-hidden.json:
#      the names under a VALID install marker); a missing or unreadable list is an
#      empty list (logged): the watchdog then starts as it always did;
#   2. CLAUDE.md starts with the shell prefix, but PACKAGE.md is missing, a link,
#      not a regular file, unreadable or empty;
#   3. .package/manifest.json exists and is not empty, but CLAUDE.md is not a
#      regular file starting with the shell prefix.
# The prefix bytes live in scripts/lib/package-shell-prefix (a test binds them to the
# server's constant). They are compared through a pipe (head -c N | cmp), never read
# into a variable: bash drops NUL bytes and trailing newlines there, and two inputs
# the server refuses would look like the shell (plan 7.5, Dani B3).
PACKAGE_SHELL_PREFIX_FILE="$INSTALL_DIR/scripts/lib/package-shell-prefix"
PACKAGE_HIDDEN_LIST="$INSTALL_DIR/store/agent-package-hidden.json"
# The owner approvals (approval.ts); overridable only so the self-test can point at a stand-in folder.
PACKAGE_APPROVALS_DIR="${WATCHDOG_PACKAGE_APPROVALS_DIR:-$INSTALL_DIR/store/agent-package-approved}"
# The update traces (update.ts runApplyBlock writes one first and removes it last); overridable for the self-test only.
PACKAGE_UPDATING_DIR="${WATCHDOG_PACKAGE_UPDATING_DIR:-$INSTALL_DIR/store/agent-package-updating}"

# 0 when $1 is a regular file (not a link) whose first bytes are the prefix. Any
# failure of the compare (an unreadable file) is "not the shell".
starts_with_package_shell() {
  [ -f "$1" ] && [ ! -L "$1" ] && [ -f "$PACKAGE_SHELL_PREFIX_FILE" ] || return 1
  local n
  n=$(LC_ALL=C wc -c < "$PACKAGE_SHELL_PREFIX_FILE" | tr -d ' ')
  LC_ALL=C head -c "$n" "$1" 2>/dev/null | LC_ALL=C cmp -s - "$PACKAGE_SHELL_PREFIX_FILE"
}

# 0 when the readable regular file $1 holds no byte other than space, tab, LF, VT,
# FF and CR (plan 7.5: the six bytes, LC_ALL=C; a BOM or a no-break space is content).
file_is_blank() {
  [ "$(LC_ALL=C tr -d ' \011\012\013\014\015' < "$1" 2>/dev/null | LC_ALL=C head -c 1 | LC_ALL=C wc -c | tr -d ' ')" = 0 ]
}

# 0 when PACKAGE.md is present: a regular, readable, non-empty file, not a link.
# The link test comes first: [ -f ] and [ -s ] follow a link (Dani C3).
package_layer_present() {
  [ ! -L "$1" ] && [ -f "$1" ] && [ -r "$1" ] && ! file_is_blank "$1"
}

# 0 when the manifest file exists and is not empty. Fail closed: a link, a folder
# or an unreadable file in its place counts as a non-empty manifest.
package_manifest_present() {
  [ -L "$1" ] || [ -e "$1" ] || return 1
  [ ! -L "$1" ] && [ -f "$1" ] && [ -r "$1" ] || return 0
  ! file_is_blank "$1"
}

# 0 when the agent name $1 stands on the hidden list $2. A missing or unreadable list
# answers 1 (not hidden); the caller logs that once per tick.
package_install_hidden() {
  [ -f "$2" ] || return 1
  python3 -c 'import json,sys
try:
    names = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(names, list) and sys.argv[2] in names else 1)' "$2" "$1" 2>/dev/null
}

# This fork (KISPROGI 6464 H1): 0 while an update trace stands for the name $1, any entry (a file, a link, a folder),
# like marker.ts hasUpdatingTrace: an update is being applied, or one was interrupted and the folder may hold a mix.
package_update_in_progress() {
  [ -e "$PACKAGE_UPDATING_DIR/$1.json" ] || [ -L "$PACKAGE_UPDATING_DIR/$1.json" ]
}

# This fork (BELA 6438/6444): a package agent starts only when its owner approval names exactly the package that stands
# in its folder: store/agent-package-approved/<name>.json against agents/<name>/.package/manifest.json, the slug, the
# version and the sha256 of the signed payload (src/web/agent-package/approval.ts makes the same comparison). Read by
# python3 straight from the two files, no derived list: nothing can go stale. Fail closed: a missing, linked,
# unreadable or mismatched file, or python3 failing, is "not approved".
package_approved() {
  python3 - "$PACKAGE_APPROVALS_DIR/$2.json" "${1%/}/.package/manifest.json" "$2" <<'PY' 2>/dev/null
import json, os, sys
appr, man, name = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    if os.path.islink(appr) or os.path.islink(man) or not os.path.isfile(appr) or not os.path.isfile(man):
        sys.exit(1)
    a = json.load(open(appr, encoding="utf-8"))
    m = json.load(open(man, encoding="utf-8"))
    p = m.get("payload") or {}
    ok = (a.get("schema") == 1 and a.get("name") == name and m.get("schema") == 1
          and isinstance(m.get("payloadSha256"), str) and a.get("payloadSha256") == m.get("payloadSha256")
          and isinstance(p.get("slug"), str) and a.get("slug") == p.get("slug")
          and isinstance(p.get("version"), str) and a.get("version") == p.get("version"))
    sys.exit(0 if ok else 1)
except Exception:
    sys.exit(1)
PY
}

# Prints the refusal text, or nothing when the agent may start. $1 agent dir, $2 name,
# $3 the hidden list file. Upstream's three package conditions, then the owner approval.
package_start_refusal() {
  local d="${1%/}"
  if package_install_hidden "$2" "$3"; then echo "Package install incomplete"; return; fi
  # H1: only a package agent's folder (the shell or a manifest) is held by the trace (install.ts packageStartRefusal).
  if package_update_in_progress "$2" && { starts_with_package_shell "$d/CLAUDE.md" || package_manifest_present "$d/.package/manifest.json"; }; then
    echo "Package update in progress"; return
  fi
  local is_pkg=0
  if starts_with_package_shell "$d/CLAUDE.md"; then
    package_layer_present "$d/PACKAGE.md" || { echo "Package files missing: run a package repair"; return; }
    is_pkg=1
  elif package_manifest_present "$d/.package/manifest.json"; then
    echo "Package shell broken: CLAUDE.md must start with the PACKAGE.md and LOCAL.md import lines"
    return
  fi
  if [ "$is_pkg" = 1 ] && ! package_approved "$d" "$2"; then
    echo "Package awaiting owner approval"
  fi
}

# Self-test hook: watchdog.sh --check-package <agent-dir> [hidden-list-file]
if [ "${1:-}" = "--check-package" ]; then
  [ -n "${2:-}" ] || { echo "usage: watchdog.sh --check-package <agent-dir> [hidden-list-file]" >&2; exit 2; }
  REFUSAL="$(package_start_refusal "$2" "$(basename "${2%/}")" "${3:-$PACKAGE_HIDDEN_LIST}")"
  if [ -z "$REFUSAL" ]; then echo "package=ok"; else echo "package=refused:$REFUSAL"; fi
  exit 0
fi



# Overlap guard: a cycle's per-agent restart sleeps + message replay can exceed
# the 5-min cron interval; without this a second watchdog would start mid-restart
# and both could spawn the same session (restart-race, 2026-08-06). NON-BLOCKING:
# if a cycle is still running, this tick skips. `exec 9>` must succeed BEFORE
# flock -- otherwise `flock -n 9` acts on fd 0 (stdin) and SUCCEEDS, silently
# bypassing the lock (trap documented in stuck-modal-guard.sh). Placed AFTER the
# --resolve-provider fast path so the provider-contract tests never take the lock.
exec 9>"$INSTALL_DIR/logs/.watchdog.lock" || { echo "$(timestamp) [watchdog] cannot open lock file, skipping" >> "$LOG"; exit 0; }
if command -v flock >/dev/null 2>&1; then
  flock -n 9 || { echo "$(timestamp) [watchdog] another watchdog run in progress, skipping" >> "$LOG"; exit 0; }
fi

export PATH="/opt/homebrew/bin:$HOME/.bun/bin:/home/linuxbrew/.linuxbrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

TOKEN=""
if [ -f "$INSTALL_DIR/store/.dashboard-token" ]; then
  TOKEN=$(cat "$INSTALL_DIR/store/.dashboard-token")
fi

# Replays delivered-but-not-completed messages from the last 2 hours into
# a freshly restarted agent session. Called after a confirmed restart.
#
# REPLAYOFF1127 (local; BELA msg 6167): the replay is OFF unless
# MARVEEN_WATCHDOG_REPLAY=1. Measured 2026-11-27:
#   - it has been dead since 2026-08-18: it asked /api/messages?to=..., `to` is
#     not a known parameter there (400), curl without -f still exits 0, and the
#     error JSON travelled on as data; logs/watchdog.log holds 10 "restarted OK"
#     lines since 2026-06-29 and not one "replaying" line;
#   - fixing only the parameter (upstream cca1e519) would REVIVE it, and here
#     'delivered' does not mean "not processed": an agent marks nothing as done
#     on most rows (4221 delivered vs 1930 done in agent_messages). One restart
#     at the measuring moment would have replayed 12 already-handled messages
#     into kisprogi, 10 into progi and 5 into bela.
# Telling "delivered but never seen" from "seen" needs the server-side seen
# state of upstream d4edb634 (message-seen-sweep, resume-queue-drain), which
# this install does not have; that commit comes with that infrastructure, in a
# later wave. Until then the restart path relies on the context-guard handoff /
# TASK-FOLYTATAS reload, and this function only logs that it replayed nothing.
# The code below the switch is kept CORRECT (agent= parameter, HTTP status,
# list check, trailing ';' protection) so that turning it on later does not
# resurrect the old bugs -- but turning it on without the seen state brings the
# duplicate replays back.
replay_unfinished_messages() {
  local AGENT_ID="$1"
  local SESSION_NAME="$2"

  if [ "${MARVEEN_WATCHDOG_REPLAY:-0}" != "1" ]; then
    echo "[watchdog] $AGENT_ID: ujrainditas utani replay kikapcsolva (nincs seen-allapot), 0 uzenet visszajatszva"
    return
  fi

  [ -z "$TOKEN" ] && return

  local NOW CUTOFF RESPONSE TMPDATA HTTP_CODE
  NOW=$(date +%s)
  CUTOFF=$(( NOW - 7200 ))

  # `agent`, not `to`: /api/messages knows agent, status, limit, before and
  # answers anything else with a 400. The HTTP status is checked explicitly:
  # curl without -f exits 0 on a 400, so `|| return` alone lets an error body
  # through as data. (Comments stay ABOVE the command: a comment between
  # backslash-continued lines swallows the rest of the line.)
  RESPONSE=$(curl -s -m 5 \
    -H "Authorization: Bearer $TOKEN" \
    -w '\n%{http_code}' \
    "http://localhost:${WEB_PORT}/api/messages?agent=${AGENT_ID}&limit=200" 2>/dev/null) || return
  HTTP_CODE=$(printf '%s' "$RESPONSE" | tail -n1)
  RESPONSE=$(printf '%s' "$RESPONSE" | sed '$d')
  if [ "$HTTP_CODE" != "200" ]; then
    echo "[watchdog] $AGENT_ID: /api/messages HTTP $HTTP_CODE, replay kihagyva"
    return
  fi

  [ -z "$RESPONSE" ] || [ "$RESPONSE" = "[]" ] && return

  TMPDATA=$(mktemp)
  echo "$RESPONSE" > "$TMPDATA"

  python3 - "$SESSION_NAME" "$AGENT_ID" "$CUTOFF" "$TMPDATA" <<'PYEOF' 2>/dev/null
import json, os, sys, subprocess, time

session_name, agent_id, cutoff_str, data_file = sys.argv[1:5]
cutoff = int(cutoff_str)
# Test seams (scripts/__tests__/watchdog-replay.test.sh): the tmux binary and the boot wait.
tmux_bin = os.environ.get("WATCHDOG_TMUX_BIN") or "tmux"
boot_wait = float(os.environ.get("WATCHDOG_REPLAY_BOOT_SEC") or 15)

with open(data_file) as f:
    msgs = json.load(f)

# A non-list body (an error object) must never be iterated: iterating a dict
# walks its KEYS, which is how the 400 body used to reach this loop.
if not isinstance(msgs, list):
    print(f"[watchdog] {agent_id}: /api/messages body is not a list, replay kihagyva", flush=True)
    sys.exit(0)

pending = [
    m for m in msgs
    if isinstance(m, dict)
       and m.get('to_agent') == agent_id
       and m.get('status') == 'delivered'
       and m.get('completed_at') is None
       and m.get('created_at', 0) >= cutoff
]

if not pending:
    sys.exit(0)

print(f"[watchdog] {agent_id}: replaying {len(pending)} unfinished message(s)", flush=True)
time.sleep(boot_wait)  # let claude boot up and reach the prompt

for m in pending:
    content = m.get('content', '')
    full_msg = f"[Újraküldés - feladat elveszett restart előtt]: {content}"
    chunk_size = 990
    for i in range(0, len(full_msg), chunk_size):
        chunk = full_msg[i:i + chunk_size]
        # tmux reads a trailing ';' of a send-keys argument as its own command
        # separator and drops it, even with -l (upstream a067ed2d; PROGI review
        # msg 6026 named this caller). Same rule as protectTrailingSemicolon()
        # in src/web/tmux-keys.ts: escape only the LAST character.
        if chunk.endswith(';'):
            chunk = chunk[:-1] + '\\;'
        subprocess.run([tmux_bin, 'send-keys', '-t', session_name, '-l', chunk],
                       timeout=5, capture_output=True)
    subprocess.run([tmux_bin, 'send-keys', '-t', session_name, 'Enter'],
                   timeout=5, capture_output=True)
    time.sleep(min(2, boot_wait))

PYEOF

  rm -f "$TMPDATA"
}

# Self-test hook (like --resolve-provider above): run ONLY the restart replay for
# one agent/session and print what it would log, without the dashboard check or
# any restart. Lets scripts/__tests__/watchdog-replay.test.sh pin REPLAYOFF1127
# against a stub dashboard and a fake tmux.
if [ "${1:-}" = "--replay-only" ]; then
  replay_unfinished_messages "${2:-}" "${3:-}"
  exit 0
fi

# ── Dashboard ──────────────────────────────────────────────────────────────
# HTTP ping, not a process-name grep: node always runs via its full NVM path
# here (e.g. /home/kisss/.nvm/versions/node/v22.23.1/bin/node .../dist/index.js),
# so a "node dist/index.js" substring grep never matches and falsely triggers
# a restart (and a doomed EADDRINUSE npm start) every single cycle.
DASHBOARD_HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H "Authorization: Bearer $TOKEN" "http://localhost:${WEB_PORT}/api/agents" 2>/dev/null)
if [ "$DASHBOARD_HTTP_CODE" != "200" ]; then
  echo "$(timestamp) [watchdog] Dashboard down (HTTP ${DASHBOARD_HTTP_CODE:-no-response}), restarting..." >> "$LOG"
  if systemctl --user restart bela-dashboard.service \
      >> "$INSTALL_DIR/logs/dashboard.log" 2>&1
  then
    echo "$(timestamp) [watchdog] systemd dashboard restart requested" >> "$LOG"
  else
    echo "$(timestamp) [watchdog] systemd dashboard restart FAILED" >> "$LOG"
  fi
  sleep 5
  NEW_CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H "Authorization: Bearer $TOKEN" "http://localhost:${WEB_PORT}/api/agents" 2>/dev/null)
  echo "$(timestamp) [watchdog] Dashboard restart attempted (HTTP ${NEW_CODE:-no-response})" >> "$LOG"
fi

# ── Main agent session ─────────────────────────────────────────────────────
MAIN_AGENT_ID="$(grep -E '^MAIN_AGENT_ID=' "$INSTALL_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
MAIN_AGENT_ID="${MAIN_AGENT_ID:-marveen}"
MAIN_SESSION="${MAIN_AGENT_ID}-channels"

if ! tmux has-session -t "=$MAIN_SESSION:" 2>/dev/null; then
  # Mutual-exclusion gate, mirroring channel-watchdog.sh's gate 4. The dashboard
  # channel-monitor, channel-watchdog.sh, systemd (Restart=always) and this cron
  # loop can all recreate the main channels session; they coordinate through the
  # shared respawn stamp (store/.channel-last-respawn, written by channels.sh).
  # Without this gate this loop spawned a competing channels.sh every 5 min while
  # another actor was mid-respawn -> two claude processes contending for the same
  # bot token (409) and rapid-exit (root-caused 2026-08-06, ~4.5h outage). Defer
  # while a respawn is inside the grace window (same 15 min as channel-watchdog.sh);
  # only act as the last-resort backstop once every other actor has stopped trying.
  MAIN_RESPAWN_STAMP="$INSTALL_DIR/store/.channel-last-respawn"
  _mlast=0
  [ -f "$MAIN_RESPAWN_STAMP" ] && _mlast="$(stat -c %Y "$MAIN_RESPAWN_STAMP" 2>/dev/null || echo 0)"
  if [ "$(( $(date +%s) - _mlast ))" -lt 900 ]; then
    echo "$(timestamp) [watchdog] $MAIN_SESSION missing but a respawn is within the 900s grace -- deferring (systemd/channels.sh/channel-watchdog cover it)" >> "$LOG"
  else
    echo "$(timestamp) [watchdog] $MAIN_SESSION missing, restarting..." >> "$LOG"
    nohup "$INSTALL_DIR/scripts/channels.sh" >> "$INSTALL_DIR/logs/marveen-channels.log" 2>&1 &
    sleep 5
    if tmux has-session -t "=$MAIN_SESSION:" 2>/dev/null; then
      echo "$(timestamp) [watchdog] $MAIN_SESSION restarted OK" >> "$LOG"
    else
      echo "$(timestamp) [watchdog] $MAIN_SESSION restart FAILED" >> "$LOG"
    fi
  fi
fi

# ── Sub-agents: restart if missing ────────────────────────────────────────
if [ ! -d "$INSTALL_DIR/agents" ]; then
  exit 0
fi

CLAUDE_BIN="$(command -v claude)"

# The server writes the hidden list on every start (an empty one too), so a missing
# list is abnormal: say so once per tick, then go on (upstream 9301a1dd, plan 7.2, J1/f).
if [ ! -f "$PACKAGE_HIDDEN_LIST" ]; then
  echo "$(timestamp) [watchdog] agent package hidden list missing ($PACKAGE_HIDDEN_LIST); treated as empty" >> "$LOG"
fi

for AGENT_DIR in "$INSTALL_DIR/agents"/*/; do
  AGENT_ID=$(basename "$AGENT_DIR")
  SESSION_NAME="agent-${AGENT_ID}"

  if tmux has-session -t "=$SESSION_NAME:" 2>/dev/null; then
    continue
  fi

  # Cross-process respawn coordination (2026-08-06 audit): if the dashboard just
  # (re)started this agent -- restartAgentProcess stamps
  # store/.agent-<id>-last-respawn BEFORE it stops the old session -- the new
  # session may not have re-appeared yet. Defer rather than race a duplicate
  # `tmux new-session` for the same name. The 60s grace covers the dashboard
  # stop->start window plus our detection lag; the dashboard's own runners still
  # cover a genuine crash that happens to land inside the grace.
  RESPAWN_STAMP="$INSTALL_DIR/store/.agent-${AGENT_ID}-last-respawn"
  if [ -f "$RESPAWN_STAMP" ]; then
    _rlast="$(stat -c %Y "$RESPAWN_STAMP" 2>/dev/null || echo 0)"
    _rnow="$(date +%s)"
    if [ "$(( _rnow - _rlast ))" -lt 60 ]; then
      echo "$(timestamp) [watchdog] $AGENT_ID (re)spawned $(( _rnow - _rlast ))s ago by another actor (< 60s grace), deferring" >> "$LOG"
      continue
    fi
  fi

  echo "$(timestamp) [watchdog] $AGENT_ID missing, restarting..." >> "$LOG"

  resolve_agent_provider "$AGENT_DIR"

  CHAN_DIR="$AGENT_DIR/.claude/channels/$AGENT_PROVIDER"
  BOT_TOKEN=$(grep "$TOKEN_VAR" "$CHAN_DIR/.env" 2>/dev/null | cut -d= -f2- | head -1)
  MODEL=$(python3 -c "import json; d=json.load(open('$AGENT_DIR/agent-config.json')); print(d.get('model','claude-haiku-4-5-20251001'))" 2>/dev/null || echo "claude-haiku-4-5-20251001")

  if [ -z "$BOT_TOKEN" ]; then
    echo "$(timestamp) [watchdog] $AGENT_ID: no $AGENT_PROVIDER bot token, skipping" >> "$LOG"
    continue
  fi

  # The package start guard, after the token check (KISPROGI 6464 M3): an agent with no token is skipped above anyway,
  # so a package agent waiting for approval no longer costs a python3 run and a log line every tick for nothing.
  PKG_REFUSAL="$(package_start_refusal "$AGENT_DIR" "$AGENT_ID" "$PACKAGE_HIDDEN_LIST")"
  if [ -n "$PKG_REFUSAL" ]; then
    echo "$(timestamp) [watchdog] $AGENT_ID: not started: $PKG_REFUSAL" >> "$LOG"
    continue
  fi

  # 2026-08-06 (Istvan-approved narrow hotfix): mirror the safety env-vars the
  # dashboard's startAgentProcess (src/web/agent-process.ts) sets, so a
  # watchdog-spawned FALLBACK agent is not divergent. Without these a watchdog
  # respawn ran with the claude auto-updater ENABLED -- re-corrupting the shared
  # global claude install (root-caused 2026-08-05) -- missed the phantom-injection
  # prompt-suggestion fix, and skipped the channel-MCP registration hardening.
  # The two conditional envs (isolated config dir, fleet OAuth token) are keyed on
  # the SAME on-disk artifacts the dashboard uses, so this REFLECTS the provisioned
  # state, never re-derives it: no isolated dir + no token => neither is set,
  # exactly like the dashboard. (The larger structural fix -- have the watchdog
  # call the canonical POST /api/agents/<id>/restart path instead of an inline
  # spawn -- is tracked separately, not done here.)
  SAFETY_ENV="export DISABLE_AUTOUPDATER=1 && export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false && export MCP_SERVER_CONNECTION_BATCH_SIZE=10 && export MCP_CONNECTION_NONBLOCKING=1 && export MCP_TIMEOUT=60000 && "
  AGENT_CONFIG_DIR="${AGENT_DIR}.claude-config"
  if [ -d "$AGENT_CONFIG_DIR" ]; then
    SAFETY_ENV="${SAFETY_ENV}export CLAUDE_CONFIG_DIR=\"$AGENT_CONFIG_DIR\" && "
  elif [ -s "$INSTALL_DIR/store/.claude-oauth-token" ]; then
    SAFETY_ENV="${SAFETY_ENV}export CLAUDE_CODE_OAUTH_TOKEN=\"\$(cat '$INSTALL_DIR/store/.claude-oauth-token')\" && "
  fi

  CMD="export PATH=\"/opt/homebrew/bin:\$HOME/.bun/bin:/home/linuxbrew/.linuxbrew/bin:\$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:\$PATH\" && unset TELEGRAM_BOT_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN DISCORD_BOT_TOKEN && ${SAFETY_ENV}export ${STATE_ENV_VAR}=\"$CHAN_DIR\" && cd \"$AGENT_DIR\" && ${CLAUDE_BIN} --dangerously-skip-permissions --model '$MODEL' --channels plugin:${AGENT_PROVIDER}@claude-plugins-official"

  tmux new-session -d -s "$SESSION_NAME" "$CMD" 2>/dev/null
  # H1: the dashboard may have begun a package update between the guard above and the spawn (it writes its trace,
  # then checks that the agent is not running). Ask the guard again; a refusal now withdraws the fresh session before
  # Claude Code has read a file.
  PKG_REFUSAL="$(package_start_refusal "$AGENT_DIR" "$AGENT_ID" "$PACKAGE_HIDDEN_LIST")"
  if [ -n "$PKG_REFUSAL" ]; then
    tmux kill-session -t "=$SESSION_NAME:" 2>/dev/null
    echo "$(timestamp) [watchdog] $AGENT_ID: start withdrawn: $PKG_REFUSAL" >> "$LOG"
    continue
  fi
  sleep 2

  if tmux has-session -t "=$SESSION_NAME:" 2>/dev/null; then
    echo "$(timestamp) [watchdog] $AGENT_ID restarted OK" >> "$LOG"
    # Stamp our own spawn so the shared timestamp always means "last respawn by
    # ANY actor" (keeps a later watchdog cycle / any future dashboard-side check
    # from immediately racing this fresh session).
    touch "$RESPAWN_STAMP" 2>/dev/null || true
    REPLAY_OUT=$(replay_unfinished_messages "$AGENT_ID" "$SESSION_NAME" 2>&1)
    [ -n "$REPLAY_OUT" ] && echo "$(timestamp) $REPLAY_OUT" >> "$LOG"
  else
    echo "$(timestamp) [watchdog] $AGENT_ID restart FAILED" >> "$LOG"
  fi
done

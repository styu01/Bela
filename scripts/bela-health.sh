#!/bin/bash
# ============================================================================
# BÉLA health check — 5 percenként cron-ból futtatva
# 1. Ellenőrzi hogy BÉLA session/process él-e → ha nem, újraindít
# 2. Ellenőrzi hogy a patchek megvannak-e → ha nem, visszaírja őket
# ============================================================================

MARVEEN_DIR="$HOME/marveen"
PLUGIN_DIR="$HOME/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6"
CHANNELS_DIR="$HOME/.claude/channels/telegram"
SESSION_NAME="bela-channels"
LOG="$MARVEEN_DIR/store/bela-health.log"
START_SCRIPT="$MARVEEN_DIR/scripts/bela-start.sh"
TELEGRAM_USER_ID="6209177290"
TELEGRAM_BOT_TOKEN="8880939510:AAGbn9kyDEmD476MTNAhNMu2t_gQYZgmqgg"
NEEDS_RESTART=0

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# NVM kell hogy a claude elérhető legyen
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && source "$NVM_DIR/nvm.sh" && nvm use 22 --silent 2>/dev/null

# ---- A. Session/process ellenőrzés ----------------------------------------
if tmux has-session -t "=$SESSION_NAME:" 2>/dev/null; then
    CLAUDE_PID=$(pgrep -f "claude.*--channels.*telegram" 2>/dev/null | head -1)
    if [ -z "$CLAUDE_PID" ]; then
        log "WARN: session él de claude process nem fut"
        NEEDS_RESTART=1
    else
        # server.ts (Socket Mode) liveness ellenőrzés — runtime-agnosztikus
        SERVER_TS_PID=$(pgrep -f "telegram.*server\.ts" 2>/dev/null | head -1)
        if [ -z "$SERVER_TS_PID" ]; then
            log "WARN: claude fut de server.ts (Slack plugin) nem fut — restart"
            NEEDS_RESTART=1
        else
            # Transcript korroboráció: ha a transcript is régi, valódi fagyás
            TRANSCRIPT_DIR="$MARVEEN_DIR/.claude/projects"
            NEWEST_JSONL=$(find "$TRANSCRIPT_DIR" -name "*.jsonl" -newer /tmp/.bela-health-ref 2>/dev/null | head -1)
            NOW=$(date +%s)
            TRANSCRIPT_MTIME=$(find "$TRANSCRIPT_DIR" -name "*.jsonl" -printf "%T@\n" 2>/dev/null | sort -n | tail -1 | cut -d. -f1)
            if [ -n "$TRANSCRIPT_MTIME" ] && [ "$((NOW - TRANSCRIPT_MTIME))" -gt 300 ]; then
                # Transcript 5+ perce nem frissült — ellenőrizd a pane-t is
                PANE=$(tmux capture-pane -t "=$SESSION_NAME:" -p 2>/dev/null)
                case "$PANE" in
                    *"esc to interrupt"*|*"Working"*|*"Thinking"*)
                        log "INFO: transcript régi de BÉLA aktívan dolgozik — nem indítjuk újra"
                        ;;
                    *)
                        log "WARN: transcript $((NOW - TRANSCRIPT_MTIME))s régi és pane idle — valószínű fagyás, restart"
                        NEEDS_RESTART=1
                        ;;
                esac
            fi
        fi
    fi
else
    log "WARN: $SESSION_NAME session nem él"
    NEEDS_RESTART=1
    # Session teljesen halott -- a StopFailure hook nem tud lefutni
    # (az is a Claude Code-on belül futna), ezért itt küldünk
    # önálló auth-down értesítést, hátha OAuth lejárat volt az ok.
    python3 "$MARVEEN_DIR/scripts/hooks/auth-down-notify.py" 2>/dev/null || true
fi

# ---- A2. Pane-szintű auth hiba ellenőrzés (session él, de 401-be ragadt) --
if tmux has-session -t "=$SESSION_NAME:" 2>/dev/null; then
    PANE_AUTH_CHECK=$(tmux capture-pane -t "=$SESSION_NAME:" -p 2>/dev/null | tail -10)
    case "$PANE_AUTH_CHECK" in
        *"401"*|*"Invalid authentication"*|*"Please run /login"*)
            log "WARN: pane 401/auth hibát mutat -- auth-down értesítés (NINCS restart, OAuth-ot kézzel kell rendezni)"
            python3 "$MARVEEN_DIR/scripts/hooks/auth-down-notify.py" 2>/dev/null || true
            # NEEDS_RESTART szándékosan NEM 1 -- újraindítás nem oldja meg
            # a lejárt OAuth tokent, csak ugyanazt a 401-et kapnánk megint
            # 5 percenként, ami felesleges restart-loop lenne.
            ;;
    esac
fi

# ---- B. WebClient timeout patch ellenőrzés --------------------------------
SERVER_TS="$PLUGIN_DIR/server.ts"
if [ -f "$SERVER_TS" ]; then
    if ! grep -q 'timeout: 15000' "$SERVER_TS" 2>/dev/null; then
        log "WARN: timeout patch eltűnt — visszaírás"
        sed -i 's/const web = new WebClient(botToken)/const web = new WebClient(botToken, { timeout: 15000, retryConfig: { retries: 2, factor: 2, minTimeout: 1000, maxTimeout: 5000, randomize: true } })/' "$SERVER_TS"
        if grep -q 'timeout: 15000' "$SERVER_TS" 2>/dev/null; then
            log "  timeout patch visszaírva OK — restart szükséges"
            NEEDS_RESTART=1
        else
            log "  WARN: timeout patch visszaírás sikertelen"
        fi
    fi
fi

# ---- B2. SocketModeClient keepalive patch ellenőrzés ----------------------
if [ -f "$SERVER_TS" ]; then
    if ! grep -q "clientPingTimeout" "$SERVER_TS" 2>/dev/null; then
        log "WARN: keepalive patch eltűnt — visszaírás"
        sed -i 's/const socket = new SocketModeClient({ appToken })/const socket = new SocketModeClient({ appToken, pingPongLoggingEnabled: false, clientPingTimeout: 30000 })/' "$SERVER_TS"
        log "  keepalive patch visszaírva OK — restart szükséges"
        NEEDS_RESTART=1
    fi
fi

# ---- C. .mcp.json kulcsnév ellenőrzés ------------------------------------
MCP_JSON="$PLUGIN_DIR/.mcp.json"
if [ -f "$MCP_JSON" ]; then
    if ! grep -q '"telegram"' "$MCP_JSON" 2>/dev/null; then
        log "WARN: .mcp.json kulcsnév eltűnt — visszaírás"
        cat > "$MCP_JSON" << 'MCPEOF'
{
    "mcpServers": {
        "marveen-marketplace": {
            "command": "${CLAUDE_PLUGIN_ROOT}/node_modules/.bin/tsx",
            "args": [
                "${CLAUDE_PLUGIN_ROOT}/server.ts"
            ]
        }
    }
}
MCPEOF
        log "  .mcp.json visszaírva OK — restart szükséges"
        NEEDS_RESTART=1
    fi
fi

# ---- D. access.json ellenőrzés -------------------------------------------
ACCESS_JSON="$CHANNELS_DIR/access.json"
if [ ! -f "$ACCESS_JSON" ] || ! grep -q "$TELEGRAM_USER_ID" "$ACCESS_JSON"; then
    log "WARN: access.json hiányzik vagy hibás — visszaírás"
    mkdir -p "$CHANNELS_DIR"
    cat > "$ACCESS_JSON" << ACCESSEOF
{
  "dmPolicy": "allowlist",
  "allowFrom": ["$TELEGRAM_USER_ID"],
  "channels": {
    "telegram": {
      "requireMention": false,
      "allowFrom": []
    }
  },
  "pending": {}
}
ACCESSEOF
    chmod 600 "$ACCESS_JSON"
    log "  access.json visszaírva OK"
fi

# ---- E. Failure counter reset (ne halmozódjon) ---------------------------
FAIL_LOG="$MARVEEN_DIR/store/channels-failures.log"
if [ -f "$FAIL_LOG" ]; then
    FAIL_COUNT=$(grep -c "rapid-exit" "$FAIL_LOG" 2>/dev/null | tr -d "[:space:]")
    [ -z "$FAIL_COUNT" ] && FAIL_COUNT=0
    if [ "$FAIL_COUNT" -ge 3 ]; then
        log "WARN: failure counter magas ($FAIL_COUNT) — reset"
        > "$FAIL_LOG"
    fi
fi

# ---- F. Újraindítás ha szükséges -----------------------------------------
if [ "$NEEDS_RESTART" -eq 1 ]; then
    pkill -f "scripts/channels.sh" 2>/dev/null || true
    sleep 1
    log "Újraindítás szükséges — bela-start.sh futtatása"
    bash "$START_SCRIPT" >> "$LOG" 2>&1
fi

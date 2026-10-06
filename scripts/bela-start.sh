#!/bin/bash
# ============================================================================
# BÉLA robust startup script — WSL2 + Telegram
# ============================================================================

set -euo pipefail

MARVEEN_DIR="$HOME/marveen"
PLUGIN_DIR="$HOME/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6"
CHANNELS_DIR="$HOME/.claude/channels/telegram"
LOG="$MARVEEN_DIR/store/bela-start.log"
SESSION_NAME="bela-channels"
TELEGRAM_USER_ID="6209177290"

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

log "=== BÉLA startup begin ==="

# ---- 1. NVM + Node --------------------------------------------------------
log "1. NVM betöltés..."
export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    source "$NVM_DIR/nvm.sh"
    if ! nvm use 22 --silent 2>/dev/null; then
        log "FATAL: Node 22 nem érhető el"
        exit 1
    fi
else
    log "FATAL: nvm.sh nem található"
    exit 1
fi

# ---- 2. Claude Code telepítés-ellenőrzés ----------------------------------
log "2. Claude Code ellenőrzés..."
LOCAL_CLAUDE="$HOME/.local/bin/claude"
# Pinned claude-code version: keep the fleet on ONE known-good version so a
# self-heal reinstall never silently up/downgrades. Fleet standard (2026-08-05,
# István-approved): Node 22 + claude-code 2.1.222. Bump here to move the fleet.
# NOTE: this is the AVX-capable-host pin; fix-avx.sh/install-linux.sh keep a
# SEPARATE, older CLAUDE_PIN for AVX-less hosts that cannot run the newer build.
CLAUDE_PIN="2.1.222"

# Find a claude binary that ACTUALLY resolves, preferring the Node 22 nvm tree
# (fleet standard) but never hard-binding to one nvm version: a missing bin
# symlink in one tree must not wedge startup when another tree (or ~/.local/bin)
# has a live claude. `-e` follows symlinks, so a dangling link is treated as
# absent -- not accepted as "live". (root-caused 2026-08-05: the old logic keyed
# off a single $(nvm version 22)/bin/claude path that did not exist, so the
# self-heal branch never fired and a broken symlink stayed broken.)
find_live_claude() {
    local c
    for c in \
        "$NVM_DIR/versions/node/$(nvm version 22 2>/dev/null)/bin/claude" \
        "$NVM_DIR"/versions/node/*/bin/claude \
        "$LOCAL_CLAUDE"; do
        [ -n "$c" ] && [ -e "$c" ] && { echo "$c"; return 0; }
    done
    return 1
}

# Repair ~/.local/bin/claude ONLY if it is missing or dangling (never flip a
# working link -- deliberate version pivots are a separate operation).
if [ ! -L "$LOCAL_CLAUDE" ] || [ ! -e "$LOCAL_CLAUDE" ]; then
    if LIVE_CLAUDE="$(find_live_claude)"; then
        log "   symlink javítás: $LOCAL_CLAUDE -> $LIVE_CLAUDE"
        mkdir -p "$(dirname "$LOCAL_CLAUDE")"
        ln -sf "$LIVE_CLAUDE" "$LOCAL_CLAUDE"
    fi
fi

if ! claude --version &>/dev/null; then
    log "   claude nem található, telepítés (flock-olva, pin: @$CLAUDE_PIN)..."
    # Serialize the global install fleet-wide: parallel `npm install -g` into the
    # same nvm prefix races the atomic extract+rename and can orphan a staging
    # dir / skip the bin symlink (root-caused 2026-08-05). One installer at a time.
    (
        flock -w 180 9 || { log "FATAL: npm-install lock nem megszerezhető"; exit 1; }
        npm install -g "@anthropic-ai/claude-code@$CLAUDE_PIN" --prefer-offline 2>/dev/null \
          || { curl -fsSL "https://registry.npmjs.org/@anthropic-ai/claude-code/-/claude-code-$CLAUDE_PIN.tgz" -o /tmp/claude-code.tgz \
                 && npm install -g /tmp/claude-code.tgz 2>/dev/null; } \
          || { log "FATAL: claude telepítés sikertelen"; exit 1; }
    ) 9>/tmp/claude-npm-install.lock
    # Verify the bin symlink materialised -- the race symptom is a present package
    # with NO bin/claude link. Recreate from the package's own bin if missing
    # (matches npm's own relative link shape).
    NVM_BIN_DIR="$NVM_DIR/versions/node/$(nvm version 22)/bin"
    if [ ! -e "$NVM_BIN_DIR/claude" ] && [ -e "$NVM_BIN_DIR/../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe" ]; then
        log "   bin symlink hiányzik az install után, pótlás: $NVM_BIN_DIR/claude"
        ln -sf ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe "$NVM_BIN_DIR/claude"
    fi
    if [ ! -L "$LOCAL_CLAUDE" ] || [ ! -e "$LOCAL_CLAUDE" ]; then
        if LIVE_CLAUDE="$(find_live_claude)"; then ln -sf "$LIVE_CLAUDE" "$LOCAL_CLAUDE"; fi
    fi
fi
CLAUDE_BIN="$(command -v claude)"
log "   claude OK: $CLAUDE_BIN ($(claude --version 2>/dev/null || echo 'version unknown'))"

# ---- 3. Failure counter reset ---------------------------------------------
log "3. Failure counter reset..."
> "$MARVEEN_DIR/store/channels-failures.log" 2>/dev/null || true

# ---- 4. .mcp.json kulcsnév javítás ----------------------------------------
log "4. .mcp.json ellenőrzés..."
MCP_JSON="$PLUGIN_DIR/.mcp.json"
if [ -f "$MCP_JSON" ]; then
    if ! grep -q '"claude-plugins-official"' "$MCP_JSON"; then
        log "   .mcp.json kulcsnév javítás..."
        cat > "$MCP_JSON" << 'MCPEOF'
{
    "mcpServers": {
        "claude-plugins-official": {
            "command": "bun",
            "args": [
                "${CLAUDE_PLUGIN_ROOT}/server.ts"
            ]
        }
    }
}
MCPEOF
        log "   .mcp.json OK"
    else
        log "   .mcp.json kulcsnév OK"
    fi
else
    log "   WARN: .mcp.json nem található: $MCP_JSON"
fi

# ---- 5. managed-settings.json ellenőrzés ----------------------------------
log "5. managed-settings.json ellenőrzés..."
MANAGED="/etc/claude-code/managed-settings.json"
if [ ! -f "$MANAGED" ] || ! grep -q "telegram" "$MANAGED"; then
    log "   managed-settings.json javítás (sudo)..."
    sudo mkdir -p /etc/claude-code
    sudo tee "$MANAGED" > /dev/null << 'MANAGEDEOF'
{
  "channelsEnabled": true,
  "allowedChannelPlugins": [
    {
      "plugin": "telegram",
      "marketplace": "claude-plugins-official"
    }
  ]
}
MANAGEDEOF
    log "   managed-settings.json OK"
else
    log "   managed-settings.json OK"
fi

# ---- 6. Model, effort és plugin beállítások -------------------------------
log "6. Model/effort/plugins ellenőrzés..."
PROJECT_SETTINGS="$MARVEEN_DIR/.claude/settings.json"
if [ -f "$PROJECT_SETTINGS" ]; then
    python3 - << 'PYEOF'
import json
path = "/home/kisss/marveen/.claude/settings.json"
with open(path) as f: s = json.load(f)
changed = False
if s.get("model") != "claude-sonnet-5-5":
    s["model"] = "claude-sonnet-5-5"
    changed = True
if s.get("effort") != "medium":
    s["effort"] = "medium"
    changed = True
plugins = s.setdefault("enabledPlugins", {})
if plugins.get("telegram@claude-plugins-official") != True:
    plugins["telegram@claude-plugins-official"] = True
    changed = True
if plugins.get("slack-channel@marveen-marketplace") != False:
    plugins["slack-channel@marveen-marketplace"] = False
    changed = True
if changed:
    with open(path, "w") as f: json.dump(s, f, indent=4, ensure_ascii=False)
    print("   model/effort/plugins javítva")
else:
    print("   model/effort/plugins OK")
PYEOF
fi

# ---- 7. StopFailure hook ellenőrzés ---------------------------------------
log "7. StopFailure hook ellenőrzés..."
if [ -f "$PROJECT_SETTINGS" ]; then
    if ! grep -q "rate-limit-notify" "$PROJECT_SETTINGS"; then
        log "   StopFailure hook hiányzik — visszaírás..."
        python3 - << 'PYEOF'
import json
path = "/home/kisss/marveen/.claude/settings.json"
with open(path) as f: s = json.load(f)
hook = {"hooks": [{"type": "command","command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/hooks/rate-limit-notify.py\"","timeout": 15,"async": True}]}
hooks = s.setdefault("hooks", {})
if not any("rate-limit-notify" in str(h) for h in hooks.get("StopFailure",[])):
    hooks["StopFailure"] = [hook]
with open(path, "w") as f: json.dump(s, f, indent=4, ensure_ascii=False)
PYEOF
        log "   StopFailure hook visszaírva OK"
    else
        log "   StopFailure hook OK"
    fi
fi

# ---- 8. installed_plugins.json projectPath ellenőrzés --------------------
log "8. installed_plugins.json ellenőrzés..."
python3 - << 'PYEOF'
import json, os
path = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
try:
    with open(path) as f: d = json.load(f)
    changed = False
    for e in d.get("plugins", {}).get("telegram@claude-plugins-official", []):
        if e.get("projectPath") != "/home/kisss/marveen":
            e["projectPath"] = "/home/kisss/marveen"
            e["scope"] = "project"
            changed = True
    if changed:
        with open(path, "w") as f: json.dump(d, f, indent=4)
        print("   projectPath javítva")
    else:
        print("   projectPath OK")
except Exception as e:
    print(f"   WARN: {e}")
PYEOF

# ---- 9. Régi session cleanup -----------------------------------------------
log "9. Régi session cleanup..."
pkill -f "scripts/channels.sh" 2>/dev/null || true
sleep 1
tmux kill-session -t "$SESSION_NAME" 2>/dev/null && log "   régi session kilőve" || log "   nincs régi session"
sleep 2

# ---- 10. BÉLA indítás -------------------------------------------------------
log "10. BÉLA indítás..."
cd "$MARVEEN_DIR"
bash scripts/channels.sh telegram &
CHANNELS_PID=$!
log "   channels.sh PID: $CHANNELS_PID"

sleep 20
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    log "   BÉLA session él ✓"
else
    log "   WARN: BÉLA session nem található 20s után"
fi

# ---- 11. Kitchen szerver indítás -------------------------------------------
# LEÁLLÍTVA (2026-07-23, Istvan kérése) -- Béla Home konyhai asszisztens projekt
# leállítva, v2.0 verzió jön később. Ne indítsd automatikusan.
log "11. Kitchen szerver: leállítva (projekt szüneteltetve, v2.0-ra vár)"

log "=== BÉLA startup kész ==="

# Disk-space + stuck-modal guards (2026-06-03 hardening)

Two independent systemd `--user` timers that address the failure mode the
2026-06-03 dawn incident exposed: the root fs filled to 100% from a 2.2G orphaned
`/tmp/health_*` Apple Health export, which wedged the main session in a `/mcp`
modal and left inbound messages dropped until a human noticed. They are
**independent of the dashboard** (which dies with its process and is itself
unreliable under disk-full).

## A. Disk-space guard -- `scripts/disk-space-guard.sh`

Every minute: read `df /` usage.
- `>= 90%` (`REAP_THRESHOLD`): reap **age-guarded allowlist scratch** -- globs in
  `REAP_GLOBS` (currently `health_*`) directly under `/tmp`, only entries older
  than `REAP_MIN_AGE_MIN` (30 min), so a currently-running export (recent mtime)
  is never deleted.
- `>= 95%` (`ALERT_THRESHOLD`) after reap: alert the owner over the **direct Telegram
  Bot API** (the MCP plugin is dead under disk-full), at most once/hour.

Thresholds + the reap allowlist are constants at the top of the script. All
stamp/log writes are best-effort (ENOSPC-tolerant).

## B. Stuck-modal guard -- `scripts/stuck-modal-guard.sh`

Every minute: classify the main channels session pane (`${MAIN_AGENT_ID}-channels`,
mirrors `src/pane-state.ts`):
- **idle** (`? for shortcuts` / `bypass permissions on`) or **busy**
  (`esc to interrupt` / `(Ns · ↓` token counter) → healthy, never touched.
- **stuck** (neither marker → the modal overlay hides the idle footer and no live
  turn) → only after it **persists `STUCK_SECONDS` = 120s** (≥2 consecutive
  ticks): Escape up to 4× (like `channels.sh ensure_modal_closed`); if still not
  idle, `respawn-pane`. The respawn **shares `channel-watchdog.sh`'s
  `.channel-last-respawn` grace stamp**, so the two watchdogs never double-respawn.

A legitimately working session (`esc to interrupt` + `bypass permissions on`) is
classified busy/idle and is never disturbed (locked by the contract test).

> Companion hardening: make the stamp writes in your existing watchdog scripts
> best-effort (`… 2>/dev/null || true`) so an ENOSPC write under disk-full cannot
> crash a watchdog or emit a false signal.

### Two readers of the footer, two jobs (PROGI 6026, BELA 6054, 2026-11-27)

- `stuck-modal-guard.sh` (no argument, `stuck-modal-guard.timer`) is the PRIMARY reader for recovery: it watches
  the main channels session and is the only one that presses Escape / respawns. Its `classify_pane` step 3
  reads the live footer through `live_footer_line` (a port of `liveFooterLine`, pinned by
  `src/__tests__/pane-footer-port-parity.test.ts`). Step 3b (guard-only) also takes the live mode footer
  WITHOUT its "(shift+tab to cycle)" hint, which Claude Code 2.1.326 drops when the row is short of room
  (PR status, a footer indicator, a background count; the fleet's panes are 80 columns): without it the
  guard would respawn an idle session. For this guard "idle" is the safe answer; the TypeScript reader keeps
  answering "unknown" for those shapes (no delivery is its safe answer), so step 3b stays out of the port.
- `stuck-modal-guard.sh fleet` (`stuck-menu-scan.timer`, `stuck-modal-guard-fleet.py`) is NOT a second
  recovery guard: it scans the `agent-*` sessions for the session-limit menu and presses at most the one
  "Stop and wait" digit of an exactly recognized menu (shadow mode by default: no keys, no notifications).
  Its "healthy footer" set only ever blocks keys, so it follows the bash guard's shapes (the measured live
  footer with the "· install gh for PR status" tail, every permission mode, a turn past one minute, the
  clipped "esc to inte…") instead of keeping a parity of its own.
- On this install (2026-11-27) only `stuck-menu-scan.timer` is enabled, in shadow mode; the no-argument
  guard's timer is not installed.

## Activation

The unit files carry `/path/to/marveen` and `/home/USER` placeholders -- replace
them with your install dir and home before installing.

```bash
# 1. install the unit files (after editing the placeholders)
cp scripts/systemd/disk-space-guard.{service,timer}  ~/.config/systemd/user/
cp scripts/systemd/stuck-modal-guard.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
# 2. enable + start the timers
systemctl --user enable --now disk-space-guard.timer stuck-modal-guard.timer
# 3. verify
systemctl --user list-timers | grep -E 'disk-space|stuck-modal'
```

## Tests

```bash
bash scripts/__tests__/disk-space-guard.test.sh    # thresholds / age-guard / alert+cooldown / malformed
bash scripts/__tests__/stuck-modal-guard.test.sh   # classify fixtures (idle/busy/stuck/empty) + confirm-window
```

## Re-review hardening (2026-06-03, post-cross-model)

- **W2a -- busy-marker glyph robustness:** the `(Ns · ↓` token-counter separator can
  render as a Unicode middle-dot OR an ASCII period depending on terminal/locale.
  `classify_pane` now matches both (`(·|\.)`), so a working pane is never misread as
  STUCK and respawned mid-turn. Locked by a regression fixture in the test suite.
- **W2b -- respawn plugin id is config-overridable:** `STUCK_MODAL_PLUGIN` (default
  `plugin:telegram@claude-plugins-official`) so a renamed/local-build install isn't
  respawned with a wrong plugin id (which would exit immediately while the alert
  falsely says "respawned"). `%q`-quoted at interpolation, like the model id.

## Deferred findings

- **Telegram bot token in the `curl` URL path (`alert_owner`, both guards).** The
  token is interpolated as `…/bot${token}/sendMessage`, so it is visible in
  `/proc/<pid>/cmdline` for the curl process's lifetime. **Disposition: ACCEPTED**
  for this deployment -- the host is single-user and the guards run as
  `systemctl --user`, so no other local user can read the process table; the token
  already lives in a local `.env` the same user owns. On a shared/multi-user host
  this would be a real exposure and must move to a `--config`/netrc (mode 0600) form.
  Re-evaluate if the deployment model changes. (Source: PR #264 cross-model + sec-*
  re-review, 2026-06-03.)

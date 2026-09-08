import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// CHATID0 (2026-09-08): "0" is the installer's placeholder for an un-paired
// chat (install-macos.sh's default before pairing writes back the real id).
// A bare emptiness/falsy check does not catch it -- `[ -z "0" ]` is false,
// `not "0"` is false in Python, `[ -n "0" ]` is true. Every alert-sending
// shell/python script outside src/ that reads ALLOWED_CHAT_ID directly (the
// TS-side sites already route through owner-chat.ts's normalizeChatId/
// resolveOwnerChatId, see notify.test.ts and reauth-healer.test.ts) needs its
// own explicit "0" guard, or it silently tries to deliver to a nonexistent
// chat instead of recognising "not configured" up front.
const ROOT = join(__dirname, '..', '..')
function scriptSrc(relPath: string): string {
  return readFileSync(join(ROOT, relPath), 'utf-8')
}

describe('CHATID0: shell/python alert scripts guard against the "0" placeholder', () => {
  it('limit-monitor.sh treats CHAT_ID="0" the same as empty', () => {
    const src = scriptSrc('scripts/limit-monitor.sh')
    expect(src).toMatch(/\[ -z "\$CHAT_ID" \] \|\| \[ "\$CHAT_ID" = "0" \]/)
  })

  it('github-pr-monitor.sh treats CHAT_ID="0" the same as empty', () => {
    const src = scriptSrc('scripts/github-pr-monitor.sh')
    expect(src).toMatch(/\[ -n "\$\{CHAT_ID:-\}" \] && \[ "\$\{CHAT_ID\}" != "0" \]/)
  })

  it('disk-space-guard.sh normalizes a "0" chat id to empty before the fallback and final guard', () => {
    const src = scriptSrc('scripts/disk-space-guard.sh')
    const start = src.indexOf("chat=\"$(grep -E '^ALLOWED_CHAT_ID=' \"$INSTALL_DIR/.env\"")
    expect(start).toBeGreaterThan(0)
    const region = src.slice(start, start + 700)
    // normalized BEFORE the TELEGRAM_CHAT_ID fallback would otherwise be skipped
    expect(region).toMatch(/\[ "\$chat" = "0" \] && chat=""/)
    // and normalized again after the fallback assignment, so a "0" TELEGRAM_CHAT_ID
    // doesn't slip through either
    expect(region.match(/\[ "\$chat" = "0" \] && chat=""/g)?.length).toBe(2)
  })

  it('stuck-modal-guard.sh normalizes a "0" chat id to empty before the fallback and final guard', () => {
    const src = scriptSrc('scripts/stuck-modal-guard.sh')
    const start = src.indexOf("chat=\"$(grep -E '^ALLOWED_CHAT_ID=' \"$INSTALL_DIR/.env\"")
    expect(start).toBeGreaterThan(0)
    const region = src.slice(start, start + 700)
    expect(region).toMatch(/\[ "\$chat" = "0" \] && chat=""/)
    expect(region.match(/\[ "\$chat" = "0" \] && chat=""/g)?.length).toBe(2)
  })

  it('watchdog-inbound-prober.py exits as a safe no-op on ALLOWED_CHAT_ID="0", not just on empty', () => {
    const src = scriptSrc('scripts/watchdog-inbound-prober.py')
    expect(src).toMatch(/if not allowed_chat_id_raw or allowed_chat_id_raw == "0":/)
  })
})

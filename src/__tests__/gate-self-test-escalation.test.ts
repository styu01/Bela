import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { mkdtempSync, writeFileSync, rmSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

const esc = vi.hoisted(() => ({ escalated: [] as { key: string; belaText: string; ownerText: string }[], cleared: [] as string[], messages: [] as string[] }))
vi.mock('../web/owner-escalation.js', async (orig) => ({
  ...(await orig<typeof import('../web/owner-escalation.js')>()),
  escalateToOwner: (o: { key: string; belaText: string; ownerText: string }) => { esc.escalated.push(o) },
  clearOwnerEscalation: (k: string) => { esc.cleared.push(k) },
}))
vi.mock('../db.js', async (orig) => ({
  ...(await orig<typeof import('../db.js')>()),
  createAgentMessage: (_f: string, _t: string, content: string) => { esc.messages.push(content); return {} },
}))

import { runGateSelfTest, reportPruneKept, resetGateSelfTestState } from '../web/gate-self-test.js'
import { hookCommand } from '../web/agent-scaffold.js'

// PROGI 6248 (iv): the default alert path is escalateToOwner (BELA first, the owner if it stays), keyed per
// gate script; the recovery clears that key and tells BELA.
const tmp = mkdtempSync(join(tmpdir(), 'gate-self-test-esc-'))
afterAll(() => rmSync(tmp, { recursive: true, force: true }))

describe('default alerter', () => {
  beforeEach(() => { resetGateSelfTestState(); esc.escalated.length = 0; esc.cleared.length = 0; esc.messages.length = 0 })
  it('a failure escalates (BELA text + owner text), the recovery clears the key and messages BELA', async () => {
    mkdirSync(join(tmp, 'g'), { recursive: true })
    const g = join(tmp, 'g', 'email-send-gate.mjs'); writeFileSync(g, 'const = ;')
    const sp = join(tmp, 's.json')
    writeFileSync(sp, JSON.stringify({ hooks: { PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command: hookCommand(g), timeout: 10 }] }] } }))
    const targets = [{ agent: 'iris', settingsPath: sp, projectDir: tmp }]
    await runGateSelfTest({ targets })
    expect(esc.escalated.map((e) => e.key)).toEqual(['gate-self-test:email-send-gate.mjs'])
    expect(esc.escalated[0].belaText).toContain('[kapu-onteszt] KAPU HIBA')
    expect(esc.escalated[0].ownerText).toContain('BÉLA mar ertesitve volt errol')
    expect(esc.escalated[0].belaText).not.toMatch(/—| -- /)
    writeFileSync(g, 'process.exit(0)')
    await runGateSelfTest({ targets })
    expect(esc.cleared).toEqual(['gate-self-test:email-send-gate.mjs'])
    expect(esc.messages.length).toBe(1)
    expect(esc.messages[0]).toContain('HELYREALLT')
  }, 30_000)
  it('the prune-kept report escalates too', () => {
    reportPruneKept('okoska', '/x/settings.json', [hookCommand('/opt/scripts/self-pace-gate.mjs')])
    expect(esc.escalated.map((e) => e.key)).toEqual(['gate-prune-kept:/x/settings.json'])
  })
})

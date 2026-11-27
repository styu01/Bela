import { it, expect, vi, beforeEach, afterEach, afterAll, describe } from 'vitest'
import { mkdtempSync, writeFileSync, rmSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

// PROGI 6268: the two escalation bugs of 7e051ec7 (iv), PROGI's PoC turned around, with the REAL
// owner-escalation module (only the BELA message and the owner Telegram send captured, fake timers).
const seen = vi.hoisted(() => ({ bela: [] as string[], owner: [] as string[] }))
vi.mock('../db.js', async (orig) => ({ ...(await orig<typeof import('../db.js')>()), createAgentMessage: (_f: string, _t: string, c: string) => { seen.bela.push(c); return {} } }))
vi.mock('../notify.js', async (orig) => ({
  ...(await orig<typeof import('../notify.js')>()),
  notifyChannelOrThrow: async (t: string) => { seen.owner.push(t) },
  notifyChannel: async (t: string) => { seen.owner.push(t) },
}))
import { runGateSelfTest, resetGateSelfTestState, GATE_STAGE2_EXTRA_MS } from '../web/gate-self-test.js'
import { hookCommand } from '../web/agent-scaffold.js'
import { _resetAllOwnerEscalationsForTest } from '../web/owner-escalation.js'

const tmp = mkdtempSync(join(tmpdir(), 'gate-esc-flow-'))
afterAll(() => rmSync(tmp, { recursive: true, force: true }))
const mk = (name: string, gateBody: string) => {
  mkdirSync(join(tmp, name), { recursive: true })
  const g = join(tmp, name, 'email-send-gate.mjs'); writeFileSync(g, gateBody)
  const sp = join(tmp, name, 's.json')
  writeFileSync(sp, JSON.stringify({ hooks: { PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command: hookCommand(g), timeout: 10 }] }] } }))
  return { g, t: { agent: name, settingsPath: sp, projectDir: tmp } }
}
const flush = async (ms: number) => { await vi.advanceTimersByTimeAsync(ms) }
const count = (arr: string[], re: RegExp) => arr.filter((t) => re.test(t)).length

beforeEach(() => { resetGateSelfTestState(); _resetAllOwnerEscalationsForTest(); seen.bela.length = 0; seen.owner.length = 0; vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }) })
afterEach(() => { vi.useRealTimers() })

describe('HIBA 2: a gate BELA fixes within the cycle never pages the owner', () => {
  it('fixed at once, 6 min later (and 16 min, the next pass): owner 0; the all-clear only to BELA', async () => {
    const a = mk('p1', 'const = ;')
    await runGateSelfTest({ targets: [a.t] })
    await flush(10)
    expect(count(seen.bela, /KAPU HIBA/)).toBe(1)
    writeFileSync(a.g, 'process.exit(0)')
    await flush(6 * 60_000)
    expect(seen.owner.length).toBe(0)
    await flush(10 * 60_000) // the 15 min pass is due
    await runGateSelfTest({ targets: [a.t] })
    expect(count(seen.bela, /HELYREALLT/)).toBe(1)
    await flush(30 * 60_000)
    expect(seen.owner.length).toBe(0)
    expect(GATE_STAGE2_EXTRA_MS).toBeGreaterThanOrEqual(20 * 60_000)
  }, 60_000)

  it('(d) a break that stays pages the owner after the 20 min, and then the all-clear reaches the owner too', async () => {
    const a = mk('p2', 'const = ;')
    await runGateSelfTest({ targets: [a.t] })
    await flush(10)
    await flush(GATE_STAGE2_EXTRA_MS - 2 * 60_000)
    expect(seen.owner.length).toBe(0) // not before the 20 min
    await flush(3 * 60_000)
    expect(count(seen.owner, /KAPU HIBA/)).toBe(1)
    writeFileSync(a.g, 'process.exit(0)')
    await runGateSelfTest({ targets: [a.t] })
    expect(count(seen.bela, /HELYREALLT/)).toBe(1)
    await flush(10)
    expect(count(seen.owner, /HELYREALLT/)).toBe(1)
  }, 60_000)
})

describe('HIBA 1: the gate key never stays open for an entry that is gone', () => {
  it('the failing agent disappears -> recovered; a later failure of the same gate on another agent is told again', async () => {
    const a = mk('q-a', 'const = ;'), b = mk('q-b', 'process.exit(0)')
    await runGateSelfTest({ targets: [a.t, b.t] })
    await flush(10)
    expect(count(seen.bela, /KAPU HIBA/)).toBe(1)
    await runGateSelfTest({ targets: [b.t] }) // agent a removed: its entry is gone
    expect(count(seen.bela, /HELYREALLT/)).toBe(1)
    writeFileSync(b.g, 'const = ;')
    await runGateSelfTest({ targets: [b.t] })
    await flush(10)
    expect(count(seen.bela, /KAPU HIBA/)).toBe(2)
    expect(seen.bela.filter((t) => /KAPU HIBA/.test(t))[1]).toContain('agens: q-b')
  }, 60_000)

  it('every entry of the gate script disappears (its only agent removed): recovered; the gate failing again later is told', async () => {
    const a = mk('v-a', 'const = ;')
    await runGateSelfTest({ targets: [a.t] })
    await flush(10)
    await runGateSelfTest({ targets: [] }) // no entry of this gate at all any more
    expect(count(seen.bela, /HELYREALLT/)).toBe(1)
    await runGateSelfTest({ targets: [a.t] }) // re-added, still broken
    await flush(10)
    expect(count(seen.bela, /KAPU HIBA/)).toBe(2)
  }, 60_000)

  it('(b) A still failing and escalated, then B breaks on the same gate: told again, with both', async () => {
    const a = mk('r-a', 'const = ;'), b = mk('r-b', 'process.exit(0)')
    await runGateSelfTest({ targets: [a.t, b.t] })
    await flush(10)
    writeFileSync(b.g, 'const = ;')
    await runGateSelfTest({ targets: [a.t, b.t] })
    await flush(10)
    const hiba = seen.bela.filter((t) => /KAPU HIBA/.test(t))
    expect(hiba.length).toBe(2)
    expect(hiba[1]).toContain('agens: r-a')
    expect(hiba[1]).toContain('agens: r-b')
  }, 60_000)

  it('still failing, nothing new: no repeat message', async () => {
    const a = mk('s-a', 'const = ;')
    await runGateSelfTest({ targets: [a.t] })
    await runGateSelfTest({ targets: [a.t] })
    await runGateSelfTest({ targets: [a.t] })
    await flush(10)
    expect(count(seen.bela, /KAPU HIBA/)).toBe(1)
  }, 60_000)
})

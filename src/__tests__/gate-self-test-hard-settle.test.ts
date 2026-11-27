import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { EventEmitter } from 'node:events'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

// PROGI 6346 (H), BELA 6347: a pass that never ends made every later tick return [] in silence. Three guards:
// REAL_GIT settles on its own GIT_SETTLE_GRACE_MS after the deadline (a git SIGKILL cannot end never calls back);
// runGateCommand settles GATE_SETTLE_GRACE_MS after its kill (a grandchild holding stdout kept 'close' away);
// and a tick that finds the previous pass running for GATE_SELF_TEST_STALL_MS tells it, once, with a HELYREALLT
// when that pass finally ends. node:child_process is mocked: its children never exit.

const h = vi.hoisted(() => ({ killed: [] as string[], destroyed: 0, gitCalls: 0 }))
vi.mock('node:child_process', async (orig) => {
  const { EventEmitter: EE } = await import('node:events')
  const stream = () => Object.assign(new EE(), { destroy: () => { h.destroyed++ }, end: () => {}, on: EE.prototype.on })
  const child = () => {
    const c = new EE() as EventEmitter & Record<string, unknown>
    c.stdout = stream(); c.stderr = stream(); c.stdin = stream()
    c.kill = (sig: string) => { h.killed.push(sig); return true } // the process ignores it: never exits
    return c
  }
  return {
    ...(await orig() as object),
    execFile: vi.fn(() => { h.gitCalls++; return child() }), // the callback never fires
    spawn: vi.fn(() => child()),
  }
})

import {
  REAL_GIT, GIT_CALL_TIMEOUT_MS, GIT_SETTLE_GRACE_MS, GATE_SETTLE_GRACE_MS, GATE_SELF_TEST_STALL_MS, GATE_STALL_KEY,
  runGateCommand, runGateSelfTest, resetGateSelfTestState, type GateAlerter, type GateEntry,
} from '../web/gate-self-test.js'

const tmp = mkdtempSync(join(tmpdir(), 'gate-hard-settle-'))
const entry = (timeoutSec = 10): GateEntry => ({
  agent: 'progi', settingsPath: join(tmp, 's.json'), projectDir: tmp, matcher: 'Bash', command: 'true', timeoutSec, gate: 'egress-gate.mjs',
})

beforeEach(() => { h.killed.length = 0; h.destroyed = 0; h.gitCalls = 0; resetGateSelfTestState(); vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers(); resetGateSelfTestState() })

describe('REAL_GIT: its own hard settle', () => {
  it('a git whose callback never comes: null GIT_SETTLE_GRACE_MS after the deadline, not before', async () => {
    let answer: string | null | undefined
    void Promise.resolve(REAL_GIT.run(tmp, ['rev-parse', '--show-toplevel'])).then((v) => { answer = v })
    await vi.advanceTimersByTimeAsync(GIT_CALL_TIMEOUT_MS + GIT_SETTLE_GRACE_MS - 10)
    expect(answer).toBeUndefined()
    await vi.advanceTimersByTimeAsync(20)
    expect(answer).toBeNull()
  })
})

describe('runGateCommand: its own hard settle after the kill', () => {
  it('a child that never closes after SIGKILL: a deadline failure GATE_SETTLE_GRACE_MS later, pipes destroyed', async () => {
    let res: { ok: boolean; detail: string; timeoutLike?: boolean } | undefined
    void runGateCommand(entry(10)).then((r) => { res = r })
    await vi.advanceTimersByTimeAsync(13_000 + 10)
    expect(h.killed).toEqual(['SIGKILL'])
    expect(res).toBeUndefined()
    await vi.advanceTimersByTimeAsync(GATE_SETTLE_GRACE_MS)
    expect(res?.ok).toBe(false)
    expect(res?.timeoutLike).toBe(true)
    expect(res?.detail).toContain('SIGKILL')
    expect(h.destroyed).toBeGreaterThanOrEqual(2)
  })
})

describe('the round watchdog', () => {
  it('a pass running past GATE_SELF_TEST_STALL_MS is told once; HELYREALLT when it ends', async () => {
    const sp = join(tmp, 'settings-stall.json')
    const gate = join(tmp, 'egress-gate.mjs')
    writeFileSync(gate, 'process.exit(0)\n') // it exists, so the pass spawns it (and the mocked child never closes)
    writeFileSync(sp, JSON.stringify({ hooks: { PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command: `node ${gate}`, timeout: 10 }] }] } }))
    const targets = [{ agent: 'progi', settingsPath: sp, projectDir: tmp }]
    const ev: Array<{ kind: string; key: string; text: string }> = []
    const alerter: GateAlerter = { fail: (key, text) => { ev.push({ kind: 'fail', key, text }) }, recover: (key, text) => { ev.push({ kind: 'recover', key, text }) } }
    let t = 1_000_000
    const now = () => t
    // pass A: its gate child never closes, so it waits on our settle timers (not advanced yet)
    let aDone = false
    void runGateSelfTest({ targets, alerter, now, git: null, retryDelayMs: 0 }).then(() => { aDone = true })
    await vi.advanceTimersByTimeAsync(0)
    t += GATE_SELF_TEST_STALL_MS - 1
    expect(await runGateSelfTest({ targets, alerter, now, git: null })).toEqual([])
    expect(ev).toEqual([]) // not yet
    t += 2
    expect(await runGateSelfTest({ targets, alerter, now, git: null })).toEqual([])
    expect(ev).toHaveLength(1)
    expect(ev[0]).toMatchObject({ kind: 'fail', key: GATE_STALL_KEY })
    expect(ev[0].text).toContain('AZ ONTESZT ELAKADT')
    t += 60_000
    await runGateSelfTest({ targets, alerter, now, git: null })
    expect(ev).toHaveLength(1) // told once
    // pass A ends: its settle timers fire, then the one re-run of a timeout-like failure settles the same way
    await vi.advanceTimersByTimeAsync(2 * (13_000 + GATE_SETTLE_GRACE_MS) + 100)
    expect(aDone).toBe(true)
    const stall = ev.filter((e) => e.key === GATE_STALL_KEY)
    expect(stall.map((e) => e.kind)).toEqual(['fail', 'recover'])
    expect(stall[1].text).toContain('HELYREALLT')
    // (the hung gate itself is told on its own key, as any deadline failure)
    expect(ev.some((e) => e.kind === 'fail' && e.key === 'gate-self-test:egress-gate.mjs')).toBe(true)
  })
})

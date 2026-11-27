import { describe, it, expect, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  resolveMachineOrigin,
  noteMachineFragmentLeft,
  clearMachineFragmentLeft,
  hasMachineFragmentLeft,
  onClearResult,
  onParkedState,
  applyStuckRestartBusyGuard,
  decideStuckInputRestart,
} from '../web/channel-monitor.js'
import { decideStuckInputRecovery, type StuckInputState } from '../pane-state.js'
import { MAIN_CHANNELS_SESSION as MAIN } from '../web/main-agent.js'

// STUCKFRAGMENT915 (upstream 360e2e26, ported), with the record bound to the FRAGMENT (PROGI review 6238,
// blocking). A failed clearInputBuffer leaves a fragment parked that WE injected; the capture heuristic can
// no longer recognise it, so the restart guard defers forever. We remember that we left it, but only THAT
// fragment: a session-level flag was inherited by a person's own text typed after deleting the fragment
// between two ticks, and the main pane was respawned ~6 minutes later.

describe('resolveMachineOrigin', () => {
  it('OR of the heuristic and our record, never subtracting', () => {
    expect(resolveMachineOrigin(true, false)).toBe(true)
    expect(resolveMachineOrigin(false, true)).toBe(true)
    expect(resolveMachineOrigin(false, false)).toBe(false)
    expect(resolveMachineOrigin(true, true)).toBe(true)
  })
})

describe('the record is bound to the fragment signature', () => {
  beforeEach(() => clearMachineFragmentLeft(MAIN))

  it('counts only while the parked signature is exactly the recorded one', () => {
    noteMachineFragmentLeft(MAIN, 'FRAG')
    expect(hasMachineFragmentLeft(MAIN, 'FRAG')).toBe(true)
    expect(hasMachineFragmentLeft(MAIN, 'FRAG + typed by a person')).toBe(false)
    expect(hasMachineFragmentLeft(MAIN, null)).toBe(false)
  })

  it('onClearResult: a failed clear records the leftover signature; nothing readable or a success records nothing', () => {
    onClearResult(MAIN, true, null)
    expect(hasMachineFragmentLeft(MAIN, 'FRAG')).toBe(false)
    onClearResult(MAIN, false, null)
    expect(hasMachineFragmentLeft(MAIN, 'FRAG')).toBe(false)
    onClearResult(MAIN, false, 'FRAG')
    expect(hasMachineFragmentLeft(MAIN, 'FRAG')).toBe(true)
    // a later success does not erase it: forgetting has one owner
    onClearResult(MAIN, true, null)
    expect(hasMachineFragmentLeft(MAIN, 'FRAG')).toBe(true)
  })

  it('only the main session is recorded (no per-sub-agent leak)', () => {
    onClearResult('agent-okoska', false, 'FRAG')
    expect(hasMachineFragmentLeft('agent-okoska', 'FRAG')).toBe(false)
  })

  it('onParkedState forgets on an empty box AND on any different parked text', () => {
    noteMachineFragmentLeft(MAIN, 'FRAG')
    onParkedState(MAIN, 'FRAG')
    expect(hasMachineFragmentLeft(MAIN, 'FRAG')).toBe(true)
    onParkedState(MAIN, 'something else')
    expect(hasMachineFragmentLeft(MAIN, 'FRAG')).toBe(false)
    noteMachineFragmentLeft(MAIN, 'FRAG')
    onParkedState(MAIN, null)
    expect(hasMachineFragmentLeft(MAIN, 'FRAG')).toBe(false)
  })

  it('is per-session: another pane does not touch the main record', () => {
    noteMachineFragmentLeft(MAIN, 'FRAG')
    onParkedState('agent-other', null)
    expect(hasMachineFragmentLeft(MAIN, 'FRAG')).toBe(true)
  })
})

// Tick simulation of maybeRestartWedgedMainChannel with the real decision functions: the main pane stays
// 'typing', soft recovery has no remedy (softRemedy=false), the heuristics are silent (the cut fragment).
function simulate(sigAt: (tick: number) => string | null, ticks = 20): { restarts: number; firstRestartTick: number | null } {
  const TH = { confirmMs: 90_000, dedupMs: 45_000, maxAttempts: 4 }
  let state: StuckInputState = { parkedSig: null, firstSeenAt: null, lastRecoverAt: null, attempts: 0 }
  let restarts = 0
  let firstRestartTick: number | null = null
  let lastRestartAt = 0
  for (let t = 0; t < ticks; t++) {
    const now = 1_000_000 + t * 60_000
    state = decideStuckInputRecovery(sigAt(t), state, now, TH).next
    onParkedState(MAIN, state.parkedSig)
    const parked = state.parkedSig !== null
    const machineOrigin = resolveMachineOrigin(false, hasMachineFragmentLeft(MAIN, state.parkedSig))
    const action = applyStuckRestartBusyGuard('typing',
      decideStuckInputRestart(parked, state.attempts, TH.maxAttempts, now, lastRestartAt, restarts, 5 * 60_000, 3),
      { machineOrigin, softRemedy: false })
    if (action === 'restart') { restarts++; lastRestartAt = now; firstRestartTick ??= t }
  }
  return { restarts, firstRestartTick }
}

describe('end to end (tick simulation, PROGI 6238 scenario)', () => {
  beforeEach(() => clearMachineFragmentLeft(MAIN))

  it('the unchanged fragment we left (the 25 h wedge) still escalates to the restart', () => {
    onClearResult(MAIN, false, 'FRAG')
    const r = simulate(() => 'FRAG')
    expect(r.restarts).toBeGreaterThan(0)
  })

  it('PROGI 6238: the fragment deleted and a person\'s own text typed between two ticks (box never seen empty): NO restart', () => {
    onClearResult(MAIN, false, 'FRAG')
    const r = simulate((t) => (t < 2 ? 'FRAG' : 'the owner is typing a draft here'))
    expect(r.restarts).toBe(0)
  })

  it('a person appending to the fragment: NO restart either', () => {
    onClearResult(MAIN, false, 'FRAG')
    const r = simulate((t) => (t < 1 ? 'FRAG' : 'FRAG and more text'))
    expect(r.restarts).toBe(0)
  })

  it('control: without our record the cut fragment defers forever (the bug the port fixes)', () => {
    expect(simulate(() => 'FRAG').restarts).toBe(0)
  })
})

describe('the call sites', () => {
  const __dirname = dirname(fileURLToPath(import.meta.url))
  const src = readFileSync(join(__dirname, '..', 'web', 'channel-monitor.ts'), 'utf-8')
  const sliceFn = (name: string) => { const s = src.indexOf('function ' + name); expect(s).toBeGreaterThan(0); return src.slice(s, src.indexOf('\n}\n', s)) }
  const sliceCase = (label: string) => { const s = src.indexOf("case '" + label + "': {"); expect(s).toBeGreaterThan(0); return src.slice(s, src.indexOf('\n      }\n', s)) }

  it('both clears record the re-captured leftover signature on failure', () => {
    for (const c of ['clear-preamble', 'clear-scheduled']) {
      expect(sliceCase(c)).toContain('onClearResult(session, cleared, cleared ? null : leftoverSignature(session))')
    }
  })
  it('the restart guard reports the parked SIGNATURE before its early return, and reads the record by it', () => {
    const body = sliceFn('maybeRestartWedgedMainChannel')
    const report = body.indexOf('onParkedState(MAIN_CHANNELS_SESSION, state.parkedSig)')
    expect(report).toBeGreaterThan(0)
    expect(report).toBeLessThan(body.indexOf('if (!parked)'))
    expect(body).toMatch(/resolveMachineOrigin\(\s*machineOriginByPrefix \|\| machineOriginByRegistry,\s*hasMachineFragmentLeft\(MAIN_CHANNELS_SESSION, state\.parkedSig\),/)
  })
})

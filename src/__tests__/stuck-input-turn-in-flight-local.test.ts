import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { detectPaneState, paneTurnInFlight } from '../pane-state.js'
import { shouldAlertMainBusyCap, SUBAGENT_OVERDUE_BUSY_CAP_MS } from '../web/channel-monitor.js'

// Upstream f890dbcc, ported: local checks next to the upstream tests (pane-state.test.ts,
// channel-monitor-subagent-overdue-guard.test.ts, stuck-input-owner-alert-busy-gate.test.ts).

const FX = join(__dirname, 'fixtures', 'pane-2.1.326')
const load = (n: string) => readFileSync(join(FX, n), 'utf-8')

describe('paneTurnInFlight on real 2.1.326 captures: parity with detectPaneState', () => {
  for (const f of ['busy-long-turn-truncated-footer.txt', 'busy-turn-with-background-agent.txt']) {
    it(`${f}: busy for both`, () => {
      const p = load(f)
      expect(detectPaneState(p)).toBe('busy')
      expect(paneTurnInFlight(p)).toBe(true)
    })
  }
  it('idle-footer-debug-line-below.txt: not in flight', () => {
    const p = load('idle-footer-debug-line-below.txt')
    expect(detectPaneState(p)).not.toBe('busy')
    expect(paneTurnInFlight(p)).toBe(false)
  })
})

const CM = readFileSync(join(__dirname, '..', 'web', 'channel-monitor.ts'), 'utf-8')

function sliceFn(src: string, head: string): string {
  const i = src.indexOf(head)
  expect(i, head).toBeGreaterThan(0)
  return src.slice(i, src.indexOf('\n}\n', i))
}

describe('wiring', () => {
  it('recoverStuckInputForSession feeds paneTurnInFlight into decideStuckInputRecovery', () => {
    const fn = sliceFn(CM, 'export async function recoverStuckInputForSession(')
    expect(fn).toContain('const turnInFlight = sig != null && pane != null && paneTurnInFlight(pane)')
    expect(fn).toContain('decideStuckInputRecovery(sig, prev, Date.now(), thresholds, { turnInFlight })')
  })

  it('the sub-agent overdue escalation reads the pane at alert time and passes turnInFlight', () => {
    const fn = sliceFn(CM, 'function maybeAlertStuckSubAgent(')
    const cheap = fn.indexOf('if (state.parkedSig === null || now - last < SUBAGENT_OVERDUE_ALERT_MIN_INTERVAL_MS) return')
    const capture = fn.indexOf('capturePane(session)')
    expect(cheap).toBeGreaterThan(0)
    expect(capture).toBeGreaterThan(cheap)
    expect(fn).toContain('SUBAGENT_OVERDUE_ALERT_MIN_INTERVAL_MS, turnInFlight)')
  })

  it('LOCAL: the escalation still goes BELA-first, with the honest text and no respawn-pane -k advice', () => {
    const fn = sliceFn(CM, 'function maybeAlertStuckSubAgent(')
    expect(fn).toContain('escalateToOwner({')
    expect(fn).toContain('const base = subAgentOverdueAlertText({ label, session, agentName, parkedMs, turnInFlight })')
    expect(fn).not.toMatch(/szükség esetén/)
    expect(fn).not.toContain('automatikus próbálkozás')
  })
})

// PROGI review 6238 (2) on efdf4f8c.

describe('PROGI 6238 (2a): the main restart guard gets the box-height-proof busy verdict', () => {
  it('maybeRestartWedgedMainChannel passes paneTurnInFlight -> busy to the guard', () => {
    const fn = sliceFn(CM, 'function maybeRestartWedgedMainChannel(')
    expect(fn).toContain("const paneState: PaneState | null = paneContent == null ? null : turnInFlight ? 'busy' : detectPaneState(paneContent)")
    expect(fn.indexOf('const turnInFlight = paneContent != null && paneTurnInFlight(paneContent)')).toBeLessThan(fn.indexOf('applyStuckRestartBusyGuard('))
  })
})

describe('PROGI 6238 (2): the main session busy cap', () => {
  const st = (sig: string | null, firstSeenAt: number | null) => ({ parkedSig: sig, firstSeenAt, lastRecoverAt: null, attempts: 0 })
  const CAP = SUBAGENT_OVERDUE_BUSY_CAP_MS
  it('alerts once a parked text waited past the cap behind a live turn, once per text', () => {
    expect(shouldAlertMainBusyCap(st('A', 0), true, CAP - 1, null)).toBe(false)
    expect(shouldAlertMainBusyCap(st('A', 0), true, CAP, null)).toBe(true)
    expect(shouldAlertMainBusyCap(st('A', 0), true, CAP * 3, 'A')).toBe(false)
    expect(shouldAlertMainBusyCap(st('B', 0), true, CAP, 'A')).toBe(true)
  })
  it('no turn in flight or nothing parked: no cap alert (the restart path owns those)', () => {
    expect(shouldAlertMainBusyCap(st('A', 0), false, CAP * 2, null)).toBe(false)
    expect(shouldAlertMainBusyCap(st(null, null), true, CAP * 2, null)).toBe(false)
  })
  it('wired in maybeRestartWedgedMainChannel, the record reset when the box empties', () => {
    const fn = sliceFn(CM, 'function maybeRestartWedgedMainChannel(')
    expect(fn).toContain('if (shouldAlertMainBusyCap(state, turnInFlight, Date.now(), mainBusyCapAlertedSig)) {')
    expect(fn).toContain('if (state.parkedSig === null) mainBusyCapAlertedSig = null')
  })
})

describe('PROGI 6238 (2b): the background-agent block does not hide the spinner from paneTurnInFlight', () => {
  const SEP = '─'.repeat(80)
  const box = ['❯ <inter-agent notice> elso sor', ...Array.from({ length: 8 }, (_, i) => `  folytatas ${i + 2}. sor`)]
  const pane = (agents: number) => [
    'korabbi kimenet', '✢ Warping… (2m 39s · ↓ 11.2k tokens)', '', SEP, ...box, SEP,
    '  ⏵⏵ bypass permissions on · install gh for PR status · 1 shell', '', '  ● main',
    ...Array.from({ length: agents }, (_, i) => `  ◯ general-purpose  task ${i}            7s · ↓ 69.8k tokens`),
  ].join('\n')
  it('with 0, 1 and 5 background agents under the footer the working pane is in flight', () => {
    for (const n of [1, 5]) expect(paneTurnInFlight(pane(n)), `${n} agents`).toBe(true)
  })
})

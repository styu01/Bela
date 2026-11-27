import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const esc = vi.hoisted(() => ({ escalated: [] as { key: string; belaText: string }[], cleared: [] as string[] }))
vi.mock('../web/owner-escalation.js', async (orig) => ({
  ...(await orig<typeof import('../web/owner-escalation.js')>()),
  escalateToOwner: (o: { key: string; belaText: string }) => { esc.escalated.push(o) },
  clearOwnerEscalation: (k: string) => { esc.cleared.push(k) },
}))

import { detectsModelConsentDialog, modelConsentKeys } from '../pane-state.js'
import { dismissModelConsentDialogIfPresent } from '../web/agent-process.js'

// BELA 6227 item 5 / PROGI 6226 (3a). Shapes built from the 2.1.326 binary (not raised live: it needs a real
// account at its usage limit): no numbers, focus on the FIRST (switch) row, the second row "Continue with ..."
// only when credits are on, otherwise a buy / enable / admin row; optional "Upgrade to Max or Ultra".
const dialog = (title: string, rows: string[]) => [
  ` ${title}`, '', ' Fable 5 runs on usage credits. You have $12.40 in credits.', '', ...rows, '', ' Enter to confirm · Esc to cancel',
].join('\n')
const CREDITS_ON = dialog('Fable 5 now uses usage credits', [' ❯ Switch to Sonnet 5 and continue', '   Continue with Fable 5'])
const CREDITS_ON_DN = dialog('Fable now runs on usage credits', [' ❯ Not now', '   Continue with usage credits'])
const BUY = dialog('Fable 5 now uses usage credits', [' ❯ Switch to Sonnet 5 and continue', '   Yes, buy usage credits', '   Upgrade to Max or Ultra'])
const TURN_ON = dialog("You've reached your Fable limit", [' ❯ Switch to Sonnet 5 and continue', '   Turn on usage credits and continue'])
const REENABLE = dialog('Fable 5 now uses usage credits', [' ❯ Switch to Sonnet 5 and continue', '   Yes, re-enable and continue'])
const OLD_NUMBERED = [' Fable 5 now uses usage credits', '', '   1. Continue with Fable 5', ' ❯ 2. Switch to Sonnet 5 and continue', '', ' Enter to confirm · Esc to cancel'].join('\n')
const IDLE = '──────\n❯ \n──────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'
const KEYS = 'send' + '-keys'

describe('detection', () => {
  it('every 2.1.326 shape and the old numbered one are detected', () => {
    for (const p of [CREDITS_ON, CREDITS_ON_DN, BUY, TURN_ON, REENABLE, OLD_NUMBERED]) expect(detectsModelConsentDialog(p)).toBe(true)
  })
  it('a quote in a live idle pane, a busy pane, a title far above: not a dialog', () => {
    expect(detectsModelConsentDialog(`  ⎿  it said "Fable 5 now uses usage credits"\n   Continue with Fable 5\n${IDLE}`)).toBe(false)
    expect(detectsModelConsentDialog(`${CREDITS_ON}\n✻ Thinking… (3s · ↓ 1k tokens · esc to interrupt)`)).toBe(false)
    expect(detectsModelConsentDialog(['Fable 5 now uses usage credits', ...Array.from({ length: 50 }, (_, i) => `x ${i}`), ' ❯ Switch to Sonnet 5 and continue'].join('\n'))).toBe(false)
  })
})

describe('modelConsentKeys: only "Continue with", never money, never the switch row', () => {
  it('credits on, focus on switch: Down + Enter (never a "1", never a bare Enter)', () => {
    expect(modelConsentKeys(CREDITS_ON)).toEqual(['Down', 'Enter'])
    expect(modelConsentKeys(CREDITS_ON_DN)).toEqual(['Down', 'Enter'])
  })
  it('the old numbered layout: Up + Enter to "1. Continue with"', () => {
    expect(modelConsentKeys(OLD_NUMBERED)).toEqual(['Up', 'Enter'])
  })
  it('a buy / turn on / re-enable confirm row: null (no key)', () => {
    for (const p of [BUY, TURN_ON, REENABLE]) expect(modelConsentKeys(p)).toBeNull()
  })
  it('HYPOTHETICAL future wordings: a "Continue with" row that also buys, or an unknown "Yes, ..." row: null', () => {
    expect(modelConsentKeys(dialog('Fable 5 now uses usage credits', [' ❯ Switch to Sonnet 5 and continue', '   Continue with Fable 5 and buy usage credits']))).toBeNull()
    expect(modelConsentKeys(dialog('Fable 5 now uses usage credits', [' ❯ Switch to Sonnet 5 and continue', '   Yes, continue on Fable 5']))).toBeNull()
  })
  it('cursor on the Continue row: a bare Enter', () => {
    const p = CREDITS_ON.replace(' ❯ Switch', '   Switch').replace('   Continue with Fable 5', ' ❯ Continue with Fable 5')
    expect(modelConsentKeys(p)).toEqual(['Enter'])
  })
})

describe('dismissModelConsentDialogIfPresent (tmux layer injected)', () => {
  beforeEach(() => { esc.escalated.length = 0; esc.cleared.length = 0 })
  function io(frames: (string | null)[]) {
    const sent: string[] = []
    let n = 0
    return { sent, io: { capture: () => frames[Math.min(n++, frames.length - 1)], send: (_s: string, _h: string | null, k: string) => { sent.push(k) }, wait: async () => {} } }
  }
  const landed = CREDITS_ON.replace(' ❯ Switch', '   Switch').replace('   Continue with Fable 5', ' ❯ Continue with Fable 5')
  it('credits on: Down, re-check, Enter -> answered', async () => {
    const f = io([CREDITS_ON, landed])
    expect(await dismissModelConsentDialogIfPresent('agent-a', null, f.io)).toBe('answered')
    expect(f.sent).toEqual(['Down', 'Enter'])
    expect(esc.escalated).toEqual([])
  })
  it('a purchase row: NO key, blocked, escalated once, cleared when gone', async () => {
    const f = io([BUY])
    expect(await dismissModelConsentDialogIfPresent('agent-b', null, f.io)).toBe('blocked')
    expect(f.sent).toEqual([])
    expect(esc.escalated.map((e) => e.key)).toEqual(['model-consent-blocked:agent-b'])
    expect(esc.escalated[0].belaText).not.toMatch(/—| -- /)
    await dismissModelConsentDialogIfPresent('agent-b', null, io([BUY]).io)
    expect(esc.escalated.length).toBe(1)
    expect(await dismissModelConsentDialogIfPresent('agent-b', null, io([IDLE]).io)).toBe('absent')
    expect(esc.cleared).toEqual(['model-consent-blocked:agent-b'])
  })
  it('the move does not land: blocked without Enter', async () => {
    const f = io([CREDITS_ON, CREDITS_ON])
    expect(await dismissModelConsentDialogIfPresent('agent-c', null, f.io)).toBe('blocked')
    expect(f.sent).toEqual(['Down'])
  })
  it('no dialog: absent, nothing sent', async () => {
    const f = io([IDLE])
    expect(await dismissModelConsentDialogIfPresent('agent-d', null, f.io)).toBe('absent')
    expect(f.sent).toEqual([])
  })
})

describe('callers: no bare Enter onto a dialog that could not be answered', () => {
  const cm = readFileSync(join(__dirname, '..', 'web', 'channel-monitor.ts'), 'utf-8')
  it('both stuck-input Enter paths stop on blocked before the Enter', () => {
    const re = new RegExp(String.raw`if \(\(await dismissModelConsentDialogIfPresent\(session\)\) === 'blocked'\) \{[\s\S]{0,200}?break\s*\}\s*execFileSync\(TMUX, \['` + KEYS + String.raw`', '-t', exactTmuxTarget\(session\), 'Enter'\]`, 'g')
    expect(cm.match(re)?.length).toBe(2)
    expect(cm).not.toMatch(/await dismissModelConsentDialogIfPresent\(session\)\n\s*execFileSync/)
  })
  it('the monitor claims "kept the model" only when the dialog was answered', () => {
    expect(cm).toMatch(/const consent = await dismissModelConsentDialogIfPresent\(t\.session\)\s*if \(consent === 'answered'\) \{\s*alertOwnerOrBela\(t, 'model-consent-dialog'/)
  })
})

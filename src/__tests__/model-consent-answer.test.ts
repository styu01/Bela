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
import { dismissModelConsentDialogIfPresent, dismissResumeSummaryModalIfPresent, MODEL_CONSENT_SETTLE_MS } from '../web/agent-process.js'

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
    const waits: { at: number; ms: number }[] = []
    let n = 0
    return {
      sent, waits,
      io: {
        capture: () => frames[Math.min(n++, frames.length - 1)],
        send: (_s: string, _h: string | null, k: string) => { sent.push(k) },
        wait: async (ms: number) => { waits.push({ at: sent.length, ms }) },
      },
    }
  }
  const landed = CREDITS_ON.replace(' ❯ Switch', '   Switch').replace('   Continue with Fable 5', ' ❯ Continue with Fable 5')
  it('credits on: settle, Down, re-check, Enter, the dialog gone -> answered', async () => {
    const f = io([CREDITS_ON, CREDITS_ON, landed, IDLE])
    expect(await dismissModelConsentDialogIfPresent('agent-a', null, f.io)).toBe('answered')
    expect(f.sent).toEqual(['Down', 'Enter'])
    expect(esc.escalated).toEqual([])
  })
  it('(A1) no key before the dialog has been up for at least a second', async () => {
    const f = io([CREDITS_ON, CREDITS_ON, landed, IDLE])
    await dismissModelConsentDialogIfPresent('agent-settle', null, f.io)
    const before = f.waits.filter((w) => w.at === 0)
    expect(before.length).toBeGreaterThan(0)
    expect(before[0].ms).toBeGreaterThanOrEqual(1000)
    expect(MODEL_CONSENT_SETTLE_MS).toBeGreaterThanOrEqual(1000)
    // and after the Enter the same settle before the result is judged
    expect(f.waits.filter((w) => w.at === 2).some((w) => w.ms >= 1000)).toBe(true)
  })
  it('(A2) PoC 6287: an Enter the CLI refuses (the dialog remounts, focus back on switch) -> blocked, escalated, no further key', async () => {
    const f = io([CREDITS_ON, CREDITS_ON, landed, CREDITS_ON])
    expect(await dismissModelConsentDialogIfPresent('agent-refused', null, f.io)).toBe('blocked')
    expect(f.sent).toEqual(['Down', 'Enter'])
    expect(esc.escalated.map((e) => e.key)).toEqual(['model-consent-blocked:agent-refused'])
    expect(esc.escalated[0].belaText).toMatch(/elutasitotta/)
    // BELA 6313: no absolute "no more keys" claim (a later delivery attempt may retry, verified)
    expect(esc.escalated[0].belaText).toMatch(/Ebben a korben tobb billentyut nem kuldok/)
    expect(esc.escalated[0].belaText).not.toMatch(/tobb billentyut NEM kuldtem/)
    expect(esc.escalated[0].belaText).not.toMatch(/—| -- /)
  })
  it('(A2) a pane that cannot be read after the Enter -> blocked (the caller sends no Enter), escalated', async () => {
    const f = io([CREDITS_ON, CREDITS_ON, landed, null])
    expect(await dismissModelConsentDialogIfPresent('agent-unseen', null, f.io)).toBe('blocked')
    expect(f.sent).toEqual(['Down', 'Enter'])
    expect(esc.escalated.map((e) => e.key)).toEqual(['model-consent-blocked:agent-unseen'])
  })
  it('the dialog closes during the settle: absent, no key', async () => {
    const f = io([CREDITS_ON, IDLE])
    expect(await dismissModelConsentDialogIfPresent('agent-gone', null, f.io)).toBe('absent')
    expect(f.sent).toEqual([])
  })
  it('the pane cannot be read after the settle: blocked, no key, no alarm for a capture hiccup', async () => {
    const f = io([CREDITS_ON, null])
    expect(await dismissModelConsentDialogIfPresent('agent-hiccup', null, f.io)).toBe('blocked')
    expect(f.sent).toEqual([])
    expect(esc.escalated).toEqual([])
  })
  it('a refused round escalates once; a later answered round clears it', async () => {
    await dismissModelConsentDialogIfPresent('agent-clr', null, io([CREDITS_ON, CREDITS_ON, landed, CREDITS_ON]).io)
    await dismissModelConsentDialogIfPresent('agent-clr', null, io([CREDITS_ON, CREDITS_ON, landed, CREDITS_ON]).io)
    expect(esc.escalated.length).toBe(1)
    expect(await dismissModelConsentDialogIfPresent('agent-clr', null, io([CREDITS_ON, CREDITS_ON, landed, IDLE]).io)).toBe('answered')
    expect(esc.cleared).toEqual(['model-consent-blocked:agent-clr'])
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
  it('BELA 6313: the escalation text says what was sent (no row: no key at all; a missed move: the arrow, no Enter)', async () => {
    await dismissModelConsentDialogIfPresent('agent-txt-norow', null, io([BUY, BUY]).io)
    expect(esc.escalated[0].belaText).toMatch(/egyetlen billentyut sem nyomtam/)
    const f = io([CREDITS_ON, CREDITS_ON, CREDITS_ON])
    await dismissModelConsentDialogIfPresent('agent-txt-miss', null, f.io)
    expect(f.sent).toEqual(['Down'])
    expect(esc.escalated[1].belaText).toMatch(/egy nyilat kuldtem, de a kurzor nem ert oda/)
    expect(esc.escalated[1].belaText).toMatch(/Entert NEM nyomtam/)
    expect(esc.escalated[1].belaText).not.toMatch(/egyetlen billentyut sem/)
    for (const e of esc.escalated) expect(e.belaText).not.toMatch(/—| -- /)
  })
  it('the move does not land: blocked without Enter', async () => {
    const f = io([CREDITS_ON, CREDITS_ON, CREDITS_ON])
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

describe('BELA 6297 (A): the consent handler shares the per-session dialog lock with the resume handler', () => {
  beforeEach(() => { esc.escalated.length = 0; esc.cleared.length = 0 })
  const landed = CREDITS_ON.replace(' ❯ Switch', '   Switch').replace('   Continue with Fable 5', ' ❯ Continue with Fable 5')
  const RESUME = ['Resume this conversation?', '', 'This conversation has been inactive for 9h.', '', '❯ Resume', '  Start a new conversation', '', 'Enter to confirm · Esc to resume'].join('\n')
  type Ev = string
  function gated(frames: (string | null)[], tag: string, timeline: Ev[], gate: Promise<void>) {
    let n = 0
    return {
      capture: () => frames[Math.min(n++, frames.length - 1)],
      send: (_s: string, _h: string | null, k: string) => { timeline.push(`${tag}:${k}`) },
      wait: async () => { await gate },
    }
  }
  const tick = () => new Promise((r) => setTimeout(r, 20))

  it('a second consent caller WAITS for the running one, gets its outcome, sends no key', async () => {
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    const tl: Ev[] = []
    const first = dismissModelConsentDialogIfPresent('agent-l1', null, gated([CREDITS_ON, CREDITS_ON, landed, IDLE], 'c1', tl, gate))
    let done2 = false
    const second = dismissModelConsentDialogIfPresent('agent-l1', null, gated([CREDITS_ON], 'c2', tl, Promise.resolve())).then((r) => { done2 = true; return r })
    await tick()
    expect(done2).toBe(false)
    release()
    expect(await first).toBe('answered')
    expect(await second).toBe('answered')
    expect(tl).toEqual(['c1:Down', 'c1:Enter'])
    // released afterwards
    expect(await dismissModelConsentDialogIfPresent('agent-l1', null, gated([IDLE], 'c3', tl, Promise.resolve()))).toBe('absent')
  })

  it('a consent caller during a running RESUME run waits for it, then probes the pane itself; no key interleaves', async () => {
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    const tl: Ev[] = []
    // the resume run: settle, Enter on "Resume", judge -> the consent dialog is what shows next
    const resume = dismissResumeSummaryModalIfPresent('agent-l2', null, gated([RESUME, RESUME, CREDITS_ON], 'resume', tl, gate))
    let consentStarted = false
    const consent = dismissModelConsentDialogIfPresent('agent-l2', null, {
      capture: (() => { const fr = [CREDITS_ON, CREDITS_ON, landed, IDLE]; let n = 0; return () => { consentStarted = true; return fr[Math.min(n++, fr.length - 1)] } })(),
      send: (_s, _h, k) => { tl.push(`consent:${k}`) },
      wait: async () => {},
    })
    await tick()
    expect(consentStarted).toBe(false) // not even a capture while the resume run holds the pane
    release()
    await resume
    expect(await consent).toBe('answered')
    expect(tl).toEqual(['resume:Enter', 'consent:Down', 'consent:Enter'])
  })

  it('a resume caller during a running CONSENT run waits for it as well', async () => {
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    const tl: Ev[] = []
    const consent = dismissModelConsentDialogIfPresent('agent-l3', null, gated([CREDITS_ON, CREDITS_ON, landed, IDLE], 'consent', tl, gate))
    let resumeStarted = false
    const resume = dismissResumeSummaryModalIfPresent('agent-l3', null, { capture: () => { resumeStarted = true; return IDLE }, send: (_s, _h, k) => { tl.push(`resume:${k}`) }, wait: async () => {} })
    await tick()
    expect(resumeStarted).toBe(false)
    release()
    expect(await consent).toBe('answered')
    expect(await resume).toBe('absent')
    expect(tl).toEqual(['consent:Down', 'consent:Enter'])
  })

  it('other sessions (and the same name on another host) are not held up', async () => {
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    const tl: Ev[] = []
    const held = dismissModelConsentDialogIfPresent('agent-l4', null, gated([CREDITS_ON, CREDITS_ON, landed, IDLE], 'held', tl, gate))
    expect(await dismissModelConsentDialogIfPresent('agent-other', null, gated([IDLE], 'o', tl, Promise.resolve()))).toBe('absent')
    expect(await dismissModelConsentDialogIfPresent('agent-l4', 'laptop', gated([IDLE], 'r', tl, Promise.resolve()))).toBe('absent')
    expect(await dismissResumeSummaryModalIfPresent('agent-other', null, gated([IDLE], 'o2', tl, Promise.resolve()))).toBe('absent')
    release()
    expect(await held).toBe('answered')
  })

  it('three waiters of mixed kinds: each kind runs at most once per turn, never two at once', async () => {
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    const tl: Ev[] = []
    let active = 0
    let maxActive = 0
    const track = (frames: (string | null)[], tag: string, g: Promise<void>) => {
      const base = gated(frames, tag, tl, g)
      return { ...base, capture: (s: string, h: string | null) => { active++; maxActive = Math.max(maxActive, active); const r = base.capture(); active--; return r } }
    }
    const r1 = dismissResumeSummaryModalIfPresent('agent-l5', null, track([RESUME, RESUME, IDLE], 'r1', gate))
    const c1 = dismissModelConsentDialogIfPresent('agent-l5', null, track([IDLE], 'c1', Promise.resolve()))
    const c2 = dismissModelConsentDialogIfPresent('agent-l5', null, track([IDLE], 'c2', Promise.resolve()))
    const r2 = dismissResumeSummaryModalIfPresent('agent-l5', null, track([IDLE], 'r2', Promise.resolve()))
    await tick()
    release()
    expect(await r1).toBe('answered')
    expect(await r2).toBe('answered') // joined the running resume run
    expect(await c1).toBe('absent')
    expect(await c2).toBe('absent')   // joined c1's run
    expect(maxActive).toBe(1)
    expect(tl).toEqual(['r1:Enter'])
  })
})

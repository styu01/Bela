import { describe, it, expect, vi, beforeEach } from 'vitest'

const esc = vi.hoisted(() => ({ escalated: [] as { key: string; belaText: string }[], cleared: [] as string[] }))
vi.mock('../web/owner-escalation.js', async (orig) => ({
  ...(await orig<typeof import('../web/owner-escalation.js')>()),
  escalateToOwner: (o: { key: string; belaText: string }) => { esc.escalated.push(o) },
  clearOwnerEscalation: (k: string) => { esc.cleared.push(k) },
}))

import { detectsResumeDialog, resumeDialogKeys, resumeDialogNeedsArrow, resumeDialogNudgeKeys } from '../pane-state.js'
const MAIN_SESSION = 'bela-channels'
import { dismissResumeSummaryModalIfPresent } from '../web/agent-process.js'

// BELA 6227 item 4 / PROGI 6226 (3b). The shapes below are built from the 2.1.326 binary (the dialog
// cannot be raised with a throwaway HOME: it needs a real, long-idle subscription session; live measurement
// only with the owner's approval). Title "Resume this conversation?", options Resume / Start a new
// conversation (+ optional description row) / Resume from summary (only when offered, + description row);
// on the idle trigger no numbers and the focus can be on "Start a new conversation".

const HEAD = [
  'Resume this conversation?',
  '',
  'This conversation has been inactive for 9h and is 182k tokens long. Resuming it will use about 41% of your 5-hour usage limit.',
  '',
]
const FOOT = ['', 'Enter to confirm · Esc to resume']
const pane = (rows: string[]) => [...HEAD, ...rows, ...FOOT].join('\n')

const IDLE_FOCUS_NEW_WITH_DESC = pane([
  '  Resume',
  '❯ Start a new conversation',
  '    Claude can look this one up if you refer to it later',
  '  Resume from summary',
  '    Claude writes a summary now, and you continue from it',
])
const IDLE_FOCUS_NEW_NO_SUMMARY = pane(['  Resume', '❯ Start a new conversation'])
const NUMBERED_FOCUS_RESUME = pane(['❯ 1. Resume', '  2. Start a new conversation', '  3. Resume from summary'])
const INLINE_DESC = pane([
  '  Resume',
  '❯ Start a new conversation  Claude can look this one up if you refer to it later',
  '  Resume from summary  Claude writes a summary now, and you continue from it',
])
const OLD_NUMBERED = [
  'This session is 3d old and 120k tokens.',
  'Resuming the full session uses a lot of your usage limits. Resuming from a summary uses less.',
  '',
  '❯ 1. Resume from summary (recommended)',
  '  2. Resume full session as-is',
  '',
  'Enter to confirm',
].join('\n')

const moveTo = (p: string, from: RegExp, to: RegExp) => p.split('\n').map((l) => {
  if (from.test(l)) return l.replace('❯', ' ')
  if (to.test(l)) return l.replace(/^ /, '❯')
  return l
}).join('\n')

describe('detectsResumeDialog', () => {
  it('both shapes are detected', () => {
    for (const p of [IDLE_FOCUS_NEW_WITH_DESC, IDLE_FOCUS_NEW_NO_SUMMARY, NUMBERED_FOCUS_RESUME, INLINE_DESC]) expect(detectsResumeDialog(p)).toBe('resume-conversation')
    expect(detectsResumeDialog(OLD_NUMBERED)).toBe('resume-old')
  })
  it('a quoted "Resume from summary" in a live idle pane is NOT a dialog (the old trigger fired on it)', () => {
    const quoted = ['  ⎿  the pane showed "Resume from summary" and "Resume this conversation?" at boot', '', '─'.repeat(80), '❯ ', '─'.repeat(80), '  ⏵⏵ bypass permissions on (shift+tab to cycle)'].join('\n')
    expect(detectsResumeDialog(quoted)).toBeNull()
  })
  it('a busy pane is not a dialog', () => {
    expect(detectsResumeDialog(`${IDLE_FOCUS_NEW_WITH_DESC}\n✻ Thinking… (12s · ↓ 1.2k tokens · esc to interrupt)`)).toBeNull()
  })
  it('the title far above the live region (scrollback) does not count', () => {
    const p = ['Resume this conversation?', ...Array.from({ length: 60 }, (_, i) => `output ${i}`)].join('\n')
    expect(detectsResumeDialog(p)).toBeNull()
  })
})

describe('resumeDialogKeys: summary when offered, else Resume, never a new conversation', () => {
  it('idle trigger, focus on "Start a new conversation", description rows: ONE Down (moves count options, not rows)', () => {
    expect(resumeDialogKeys(IDLE_FOCUS_NEW_WITH_DESC)).toEqual(['Down', 'Enter'])
  })
  it('same with the descriptions drawn inline', () => {
    expect(resumeDialogKeys(INLINE_DESC)).toEqual(['Down', 'Enter'])
  })
  it('no summary offered, focus on "Start a new conversation": Up to Resume', () => {
    expect(resumeDialogKeys(IDLE_FOCUS_NEW_NO_SUMMARY)).toEqual(['Up', 'Enter'])
  })
  it('numbered, focus on Resume, summary offered: two Downs (never a "1")', () => {
    const k = resumeDialogKeys(NUMBERED_FOCUS_RESUME)
    expect(k).toEqual(['Down', 'Down', 'Enter'])
    expect(k!.some((x) => /^\d$/.test(x))).toBe(false)
  })
  it('cursor already on the target: a bare Enter', () => {
    expect(resumeDialogKeys(moveTo(IDLE_FOCUS_NEW_WITH_DESC, /❯ Start a new/, /^ {2}Resume from summary/))).toEqual(['Enter'])
    expect(resumeDialogKeys(moveTo(IDLE_FOCUS_NEW_NO_SUMMARY, /❯ Start a new/, /^ {2}Resume$/))).toEqual(['Enter'])
  })
  it('the old numbered dialog: the summary row', () => {
    expect(resumeDialogKeys(OLD_NUMBERED)).toEqual(['Enter'])
  })
  it('a bare Enter is never returned while the cursor sits on "Start a new conversation"', () => {
    for (const p of [IDLE_FOCUS_NEW_WITH_DESC, IDLE_FOCUS_NEW_NO_SUMMARY, INLINE_DESC]) expect(resumeDialogKeys(p)).not.toEqual(['Enter'])
  })
  it('uncertain shapes: null (no key at all)', () => {
    expect(resumeDialogKeys(pane(['  Resume', '  Start a new conversation']))).toBeNull() // no cursor
    expect(resumeDialogKeys(pane(['  Resume', '❯ Start a new conversation', '  Resume from summary', '  Resume from summary']))).toBeNull() // two summaries
    expect(resumeDialogKeys(pane(['❯ Start a new conversation']))).toBeNull() // only the new-conversation row
    expect(resumeDialogKeys(pane(['  Resume', '  Start a new conversation', '❯ Claude can look this one up if you refer to it later']))).toBeNull() // cursor on a description
  })
})

describe('dismissResumeSummaryModalIfPresent (tmux layer injected; PROGI 6252 A-E)', () => {
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
  const landed = moveTo(IDLE_FOCUS_NEW_WITH_DESC, /❯ Start a new/, /^ {2}Resume from summary/)
  const IDLE = '──────\n❯ \n──────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'
  const FOCUS_RESUME_NO_SUMMARY = pane(['❯ Resume', '  Start a new conversation'])
  const HINT = (p: string) => p.replace('Enter to confirm · Esc to resume', '↑/↓ pick a row first · Enter to confirm · Esc to dismiss')

  it('answered: settle >= 1 s before any key, Down, re-check, Enter, and the dialog is seen gone', async () => {
    const f = io([IDLE_FOCUS_NEW_WITH_DESC, IDLE_FOCUS_NEW_WITH_DESC, landed, IDLE])
    expect(await dismissResumeSummaryModalIfPresent('agent-a', null, f.io)).toBe('answered')
    expect(f.sent).toEqual(['Down', 'Enter'])
    expect(f.waits.find((w) => w.at === 0)!.ms).toBeGreaterThanOrEqual(1000) // (C)
    expect(esc.escalated).toEqual([])
  })

  it('(A) PROGI PoC: the Enter is swallowed (dialog still up): not "answered"; Esc closes it -> dismissed, no escalation', async () => {
    const f = io([FOCUS_RESUME_NO_SUMMARY, FOCUS_RESUME_NO_SUMMARY, FOCUS_RESUME_NO_SUMMARY, IDLE])
    expect(await dismissResumeSummaryModalIfPresent('agent-b', null, f.io)).toBe('dismissed')
    expect(f.sent).toEqual(['Enter', 'Escape'])
    expect(esc.escalated).toEqual([])
  })

  it('(A)+(D) Enter swallowed and Esc does not close it either: ONE escalation naming Esc as a way out; cleared when gone', async () => {
    const stuck = () => io([FOCUS_RESUME_NO_SUMMARY])
    expect(await dismissResumeSummaryModalIfPresent('agent-c', null, stuck().io)).toBe('blocked')
    expect(await dismissResumeSummaryModalIfPresent('agent-c', null, stuck().io)).toBe('blocked')
    expect(esc.escalated.map((e) => e.key)).toEqual(['resume-dialog-blocked:agent-c'])
    expect(esc.escalated[0].belaText).toContain('vagy Esc (folytatja, uj beszelgetest nem indit)')
    expect(esc.escalated[0].belaText).not.toMatch(/—| -- /)
    expect(await dismissResumeSummaryModalIfPresent('agent-c', null, io([IDLE]).io)).toBe('absent')
    expect(esc.cleared).toEqual(['resume-dialog-blocked:agent-c'])
  })

  it('(B) cursor already on the target but "pick a row first": the Up nudge (a no-op on the first row), re-check, Enter', async () => {
    const hinted = HINT(FOCUS_RESUME_NO_SUMMARY)
    expect(resumeDialogNeedsArrow(hinted)).toBe(true)
    expect(resumeDialogNudgeKeys(hinted)).toEqual(['Up'])
    const f = io([hinted, hinted, FOCUS_RESUME_NO_SUMMARY, IDLE])
    expect(await dismissResumeSummaryModalIfPresent('agent-d', null, f.io)).toBe('answered')
    expect(f.sent).toEqual(['Up', 'Enter'])
  })

  it('(B) nudge keys off the first row: Up then Down (one away, one back)', () => {
    const p = HINT(moveTo(IDLE_FOCUS_NEW_WITH_DESC, /❯ Start a new/, /^ {2}Resume from summary/))
    expect(resumeDialogNudgeKeys(p)).toEqual(['Up', 'Down'])
  })

  it('(B) the hint still there after the nudge: no Enter, Esc path', async () => {
    const hinted = HINT(FOCUS_RESUME_NO_SUMMARY)
    const f = io([hinted, hinted, hinted, IDLE])
    expect(await dismissResumeSummaryModalIfPresent('agent-e', null, f.io)).toBe('dismissed')
    expect(f.sent).toEqual(['Up', 'Escape'])
  })

  it('the move does not land: no Enter, Esc; still up -> escalated', async () => {
    const f = io([IDLE_FOCUS_NEW_WITH_DESC])
    expect(await dismissResumeSummaryModalIfPresent('agent-f', null, f.io)).toBe('blocked')
    expect(f.sent).toEqual(['Down', 'Escape'])
    expect(f.sent).not.toContain('Enter')
    expect(esc.escalated.length).toBe(1)
  })

  it('an unknown shape: Esc first (dismiss / resume, never a new conversation)', async () => {
    const unk = pane(['  Resume', '  Start a new conversation'])
    const f = io([unk, unk, IDLE])
    expect(await dismissResumeSummaryModalIfPresent('agent-g', null, f.io)).toBe('dismissed')
    expect(f.sent).toEqual(['Escape'])
  })

  it('the old numbered dialog gets no Esc when it cannot be answered: escalated directly', async () => {
    const oldUnk = OLD_NUMBERED.replace('❯ 1. Resume from summary (recommended)', '  1. Resume from summary (recommended)')
    const f = io([oldUnk])
    expect(await dismissResumeSummaryModalIfPresent('agent-h', null, f.io)).toBe('blocked')
    expect(f.sent).toEqual([])
    expect(esc.escalated.length).toBe(1)
  })

  it('(E) the dialog under a visible idle footer: an alert, no key, once', async () => {
    const under = [...HEAD, '❯ Resume', '  Start a new conversation', '', '─'.repeat(40), '❯ ', '─'.repeat(40), '  ⏵⏵ bypass permissions on (shift+tab to cycle)'].join('\n')
    expect(detectsResumeDialog(under)).toBeNull()
    const f = io([under])
    expect(await dismissResumeSummaryModalIfPresent('agent-i', null, f.io)).toBe('alerted')
    expect(await dismissResumeSummaryModalIfPresent('agent-i', null, io([under]).io)).toBe('alerted')
    expect(f.sent).toEqual([])
    expect(esc.escalated.map((e) => e.key)).toEqual(['resume-dialog-footer:agent-i'])
  })

  it('BELA live evidence 2026-11-27 14:49:26: a QUOTED "Resume from summary" in bela-channels drew "1"+Enter from the old code; now nothing', async () => {
    const quoted = [
      '⏺ A logban: "Dismissed Claude Code resume-from-summary modal" -- a "Resume from summary" szoveg az en paneemben volt idezve.',
      '',
      '─'.repeat(80), '❯ ', '─'.repeat(80),
      '  ⏵⏵ bypass permissions on (shift+tab to cycle)',
    ].join('\n')
    expect(/Resume from summary/.test(quoted)).toBe(true) // the old trigger fired on this
    const f = io([quoted])
    expect(await dismissResumeSummaryModalIfPresent(MAIN_SESSION, null, f.io)).toBe('absent')
    expect(f.sent).toEqual([])
    expect(esc.escalated).toEqual([])
  })

  it('no dialog: absent, nothing sent', async () => {
    const f = io([IDLE])
    expect(await dismissResumeSummaryModalIfPresent('agent-j', null, f.io)).toBe('absent')
    expect(f.sent).toEqual([])
  })
})

import { describe, it, expect, vi, beforeEach } from 'vitest'

const esc = vi.hoisted(() => ({ escalated: [] as { key: string; belaText: string }[], cleared: [] as string[] }))
vi.mock('../web/owner-escalation.js', async (orig) => ({
  ...(await orig<typeof import('../web/owner-escalation.js')>()),
  escalateToOwner: (o: { key: string; belaText: string }) => { esc.escalated.push(o) },
  clearOwnerEscalation: (k: string) => { esc.cleared.push(k) },
}))

import { detectsResumeDialog, resumeDialogKeys } from '../pane-state.js'
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

describe('dismissResumeSummaryModalIfPresent (tmux layer injected)', () => {
  beforeEach(() => { esc.escalated.length = 0; esc.cleared.length = 0 })
  function io(frames: (string | null)[]) {
    const sent: string[] = []
    let n = 0
    return { sent, io: { capture: () => frames[Math.min(n++, frames.length - 1)], send: (_s: string, _h: string | null, k: string) => { sent.push(k) }, wait: async () => {} } }
  }
  const landed = moveTo(IDLE_FOCUS_NEW_WITH_DESC, /❯ Start a new/, /^ {2}Resume from summary/)

  it('idle trigger on 2.1.326: Down, re-check on the summary row, Enter; nothing escalated', async () => {
    const f = io([IDLE_FOCUS_NEW_WITH_DESC, landed])
    await dismissResumeSummaryModalIfPresent('agent-a', null, f.io)
    expect(f.sent).toEqual(['Down', 'Enter'])
    expect(esc.escalated).toEqual([])
  })
  it('the move does not land: NO Enter, escalated once, cleared when the dialog is gone', async () => {
    const f = io([IDLE_FOCUS_NEW_WITH_DESC, IDLE_FOCUS_NEW_WITH_DESC])
    await dismissResumeSummaryModalIfPresent('agent-b', null, f.io)
    expect(f.sent).toEqual(['Down'])
    expect(esc.escalated.map((e) => e.key)).toEqual(['resume-dialog-blocked:agent-b'])
    expect(esc.escalated[0].belaText).not.toMatch(/—| -- /)
    const g = io([IDLE_FOCUS_NEW_WITH_DESC, IDLE_FOCUS_NEW_WITH_DESC])
    await dismissResumeSummaryModalIfPresent('agent-b', null, g.io)
    expect(esc.escalated.length).toBe(1) // once per spell
    const h = io(['──────\n❯ \n──────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'])
    await dismissResumeSummaryModalIfPresent('agent-b', null, h.io)
    expect(h.sent).toEqual([])
    expect(esc.cleared).toEqual(['resume-dialog-blocked:agent-b'])
  })
  it('an unknown shape: nothing sent at all, escalated', async () => {
    const f = io([pane(['  Resume', '  Start a new conversation'])])
    await dismissResumeSummaryModalIfPresent('agent-c', null, f.io)
    expect(f.sent).toEqual([])
    expect(esc.escalated.length).toBe(1)
  })
  it('no dialog: nothing sent, nothing escalated', async () => {
    const f = io(['──────\n❯ \n──────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'])
    await dismissResumeSummaryModalIfPresent('agent-d', null, f.io)
    expect(f.sent).toEqual([])
    expect(esc.escalated).toEqual([])
    expect(esc.cleared).toEqual([])
  })
})

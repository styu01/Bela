import { describe, it, expect, vi, beforeEach } from 'vitest'

// BELA 6297 (B): sendPromptToSession(..., { onDialogBlocked: 'abort' }) types NOTHING when the resume or the
// model-consent handler leaves its dialog up (it escalated), and returns 'aborted-dialog' so the scheduler
// queues a retry. Drives the real sendPromptToSession; tmux goes through node:child_process execFileSync
// (mocked with argument inspection, as in send-prompt-chunk-binding.test.ts).

const KEYS = 'send' + '-keys'
const h = vi.hoisted(() => ({ pane: '', calls: [] as string[][], escalated: [] as string[] }))

vi.mock('node:child_process', async (orig) => ({
  ...(await orig() as object),
  execFileSync: vi.fn((_file: string, args?: string[]) => {
    if (Array.isArray(args)) {
      h.calls.push(args)
      if (args.includes('capture-pane')) return h.pane
    }
    return ''
  }),
}))
vi.mock('../notify.js', () => ({ notifyChannel: vi.fn(async () => {}), notifyTelegram: vi.fn(async () => {}) }))
vi.mock('../web/owner-escalation.js', async (orig) => ({
  ...(await orig<typeof import('../web/owner-escalation.js')>()),
  escalateToOwner: (o: { key: string }) => { h.escalated.push(o.key) },
  clearOwnerEscalation: () => {},
}))

import { sendPromptToSession } from '../web/agent-process.js'
import { __resetSessionSendLocks } from '../web/session-send-lock.js'

const SEP = '─'.repeat(80)
const IDLE = ['', SEP, '❯ ', SEP, '  ⏵⏵ bypass permissions on (shift+tab to cycle)'].join('\n')
// a consent dialog without a "Continue with" row: the handler sends no key and escalates ('blocked')
const BUY = [' Fable 5 now uses usage credits', '', ' Fable 5 runs on usage credits.', '', ' ❯ Switch to Sonnet 5 and continue', '   Yes, buy usage credits', '', ' Enter to confirm · Esc to cancel'].join('\n')
// a resume dialog under a visible idle footer: the handler alerts and sends no key ('alerted')
const RESUME_UNDER_FOOTER = ['Resume this conversation?', '', 'This conversation has been inactive for 9h.', '', '❯ Resume', '  Start a new conversation', '', SEP, '❯ ', SEP, '  ⏵⏵ bypass permissions on (shift+tab to cycle)'].join('\n')

const keyCalls = () => h.calls.filter((a) => a.includes(KEYS))

beforeEach(() => {
  h.calls.length = 0
  h.escalated.length = 0
  __resetSessionSendLocks()
})

describe("sendPromptToSession onDialogBlocked: 'abort'", () => {
  it('a consent dialog its handler cannot answer: nothing typed, aborted-dialog', async () => {
    h.pane = BUY
    expect(await sendPromptToSession('dlg-abort-consent', 'hello there', null, { waitForIdle: false, onDialogBlocked: 'abort' })).toBe('aborted-dialog')
    expect(keyCalls()).toEqual([])
    expect(h.escalated).toEqual(['model-consent-blocked:dlg-abort-consent'])
  }, 15_000)

  it('a resume dialog under the idle footer (alerted, not answered): nothing typed, aborted-dialog', async () => {
    h.pane = RESUME_UNDER_FOOTER
    expect(await sendPromptToSession('dlg-abort-resume', 'hello there', null, { waitForIdle: false, lockMode: 'held', onDialogBlocked: 'abort' })).toBe('aborted-dialog')
    expect(keyCalls()).toEqual([])
  }, 15_000)

  it("BELA 6313 E1: 'abort-blocked' (the router) stops on a consent dialog its handler left 'blocked'", async () => {
    h.pane = BUY
    expect(await sendPromptToSession('dlg-hard-consent', 'hello there', null, { waitForIdle: false, onDialogBlocked: 'abort-blocked' })).toBe('aborted-dialog')
    expect(keyCalls()).toEqual([])
  }, 15_000)

  it("BELA 6313 E1: 'abort-blocked' does NOT stop on the resume 'alerted' shape (the message is typed as before)", async () => {
    h.pane = RESUME_UNDER_FOOTER
    expect(await sendPromptToSession('dlg-hard-resume-alerted', 'hello there', null, { waitForIdle: false, lockMode: 'held', onDialogBlocked: 'abort-blocked' })).toBe('sent')
    expect(keyCalls().some((a) => a.includes('-l'))).toBe(true)
  }, 30_000)

  it('no dialog: delivered as before', async () => {
    h.pane = IDLE
    expect(await sendPromptToSession('dlg-abort-none', 'hello there', null, { waitForIdle: false, onDialogBlocked: 'abort' })).toBe('sent')
    expect(keyCalls().some((a) => a.includes('-l'))).toBe(true)
  }, 15_000)
})

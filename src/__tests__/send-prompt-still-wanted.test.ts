import { describe, it, expect, vi, beforeEach } from 'vitest'

// PROGI 6026 / BELA 6054: the router's re-read (TICKVAKSAG916) ran BEFORE sendPromptToSession, which can itself
// wait: the modal dismissals, the wait-until-idle budget (up to 12 s) and the per-pane lock. A row closed during
// those waits was still typed. opts.stillWanted is asked once more after all of them, inside the lock, right
// before the first keystroke. Drives the real sendPromptToSession; tmux goes through node:child_process
// execFileSync (mocked with argument inspection, as in send-prompt-dialog-abort.test.ts).

const KEYS = 'send' + '-keys'
const h = vi.hoisted(() => ({ panes: [] as string[], fallback: '', calls: [] as string[][], captures: 0 }))

vi.mock('node:child_process', async (orig) => ({
  ...(await orig() as object),
  execFileSync: vi.fn((_file: string, args?: string[]) => {
    if (Array.isArray(args)) {
      h.calls.push(args)
      if (args.includes('capture-pane')) {
        h.captures++
        return h.panes.length ? h.panes.shift() : h.fallback
      }
    }
    return ''
  }),
}))
vi.mock('../notify.js', () => ({ notifyChannel: vi.fn(async () => {}), notifyTelegram: vi.fn(async () => {}) }))
vi.mock('../web/owner-escalation.js', async (orig) => ({
  ...(await orig<typeof import('../web/owner-escalation.js')>()),
  escalateToOwner: () => {},
  clearOwnerEscalation: () => {},
}))

import { sendPromptToSession } from '../web/agent-process.js'
import { __resetSessionSendLocks, tryAcquireSessionSendLane } from '../web/session-send-lock.js'

const SEP = '─'.repeat(80)
const IDLE = ['', SEP, '❯ ', SEP, '  ⏵⏵ bypass permissions on (shift+tab to cycle)'].join('\n')
const BUSY = ['✻ Baking… (12s · 1.2k tokens · esc to interrupt)', SEP, '❯ ', SEP, '  ⏵⏵ bypass permissions on (shift+tab to cycle)'].join('\n')

const keyCalls = () => h.calls.filter((a) => a.includes(KEYS))
const literalCalls = () => keyCalls().filter((a) => a.includes('-l'))

beforeEach(() => {
  h.calls.length = 0
  h.panes.length = 0
  h.captures = 0
  h.fallback = IDLE
  __resetSessionSendLocks()
})

describe('sendPromptToSession opts.stillWanted', () => {
  it('false: nothing typed, aborted-stale', async () => {
    let asked = 0
    expect(await sendPromptToSession('sw-false', 'hello there', null, { waitForIdle: false, stillWanted: () => { asked++; return false } })).toBe('aborted-stale')
    expect(asked).toBe(1)
    expect(keyCalls()).toEqual([])
  }, 15_000)

  it('true: delivered as before, asked once (not again before the submit-retry Enters)', async () => {
    let asked = 0
    expect(await sendPromptToSession('sw-true', 'hello there', null, { waitForIdle: false, stillWanted: () => { asked++; return true } })).toBe('sent')
    expect(asked).toBe(1)
    expect(literalCalls().length).toBeGreaterThan(0)
  }, 15_000)

  it('a throw counts as wanted (the caller\'s own read decides nothing): delivered', async () => {
    expect(await sendPromptToSession('sw-throw', 'hello there', null, { waitForIdle: false, stillWanted: () => { throw new Error('db gone') } })).toBe('sent')
    expect(literalCalls().length).toBeGreaterThan(0)
  }, 15_000)

  it('asked AFTER the wait-until-idle budget: the pane was busy for three polls, the question comes after them', async () => {
    // the modal dismissals capture first (a few times), then the idle wait polls; queue enough busy frames
    h.panes.push(BUSY, BUSY, BUSY, BUSY, BUSY, BUSY)
    let capturesWhenAsked = -1
    let busyLeftWhenAsked = -1
    const r = await sendPromptToSession('sw-after-idle', 'hello there', null, {
      stillWanted: () => { capturesWhenAsked = h.captures; busyLeftWhenAsked = h.panes.length; return false },
    })
    expect(r).toBe('aborted-stale')
    expect(busyLeftWhenAsked).toBe(0) // every busy frame was consumed before the question
    expect(capturesWhenAsked).toBeGreaterThanOrEqual(7) // ... and the idle frame that ended the wait
    expect(keyCalls()).toEqual([])
  }, 30_000)

  it('asked AFTER the per-pane lock wait: a concurrent writer closes the row while holding the lane', async () => {
    const release = tryAcquireSessionSendLane('sw-after-lock', null)!
    let closed = false
    setTimeout(() => { closed = true; release() }, 300)
    let sawClosed: boolean | null = null
    const r = await sendPromptToSession('sw-after-lock', 'hello there', null, {
      waitForIdle: false,
      stillWanted: () => { sawClosed = closed; return !closed },
    })
    expect(sawClosed).toBe(true)
    expect(r).toBe('aborted-stale')
    expect(literalCalls()).toEqual([])
  }, 15_000)

  it("lockMode 'held' (the caller owns the lane): asked too", async () => {
    expect(await sendPromptToSession('sw-held', 'hello there', null, { waitForIdle: false, lockMode: 'held', stillWanted: () => false })).toBe('aborted-stale')
    expect(keyCalls()).toEqual([])
  }, 15_000)

  it('without stillWanted nothing changes: sent', async () => {
    expect(await sendPromptToSession('sw-none', 'hello there', null, { waitForIdle: false })).toBe('sent')
    expect(literalCalls().length).toBeGreaterThan(0)
  }, 15_000)
})

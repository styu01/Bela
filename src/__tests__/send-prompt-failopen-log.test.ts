import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// PROGI 6362 / BELA 6367: when the per-pane lock wait fails open and the caller's stillWanted then says no, nothing
// is typed; the old warning "sent WITHOUT the per-pane lock" claimed a keystroke that never happened. The fail-open
// warning now says what happened. Real sendPromptToSession, mocked tmux (as in send-prompt-still-wanted.test.ts),
// a lane held for good, fake timers past the deliver wait budget.

const KEYS = 'send' + '-keys'
const h = vi.hoisted(() => ({ calls: [] as string[][], warns: [] as string[] }))
vi.mock('node:child_process', async (orig) => ({
  ...(await orig() as object),
  execFileSync: vi.fn((_file: string, args?: string[]) => {
    if (Array.isArray(args)) {
      h.calls.push(args)
      if (args.includes('capture-pane')) return ['', '─'.repeat(80), '❯ ', '─'.repeat(80), '  ⏵⏵ bypass permissions on (shift+tab to cycle)'].join('\n')
    }
    return ''
  }),
}))
vi.mock('../logger.js', () => {
  const log = { info: vi.fn(), debug: vi.fn(), error: vi.fn(), warn: vi.fn((_o: unknown, msg?: string) => { if (typeof msg === 'string') h.warns.push(msg) }), child: () => log }
  return { logger: log }
})
vi.mock('../notify.js', () => ({ notifyChannel: vi.fn(async () => {}), notifyTelegram: vi.fn(async () => {}) }))
vi.mock('../web/owner-escalation.js', async (orig) => ({
  ...(await orig<typeof import('../web/owner-escalation.js')>()),
  escalateToOwner: () => {},
  clearOwnerEscalation: () => {},
}))

import { sendPromptToSession } from '../web/agent-process.js'
import { __resetSessionSendLocks, tryAcquireSessionSendLane, DEFAULT_DELIVER_WAIT_BUDGET_MS } from '../web/session-send-lock.js'

beforeEach(() => { h.calls.length = 0; h.warns.length = 0; __resetSessionSendLocks(); vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

const failOpen = async (session: string, stillWanted: () => boolean) => {
  tryAcquireSessionSendLane(session, null) // held for good: the wait budget elapses, the send fails open
  const p = sendPromptToSession(session, 'hello there', null, { waitForIdle: false, stillWanted })
  await vi.advanceTimersByTimeAsync(DEFAULT_DELIVER_WAIT_BUDGET_MS + 30_000)
  return p
}

describe('the fail-open warning says what happened', () => {
  it("aborted-stale after a fail-open: 'nothing typed', never 'sent WITHOUT the per-pane lock'", async () => {
    expect(await failOpen('fo-stale', () => false)).toBe('aborted-stale')
    expect(h.calls.filter((a) => a.includes(KEYS))).toEqual([])
    expect(h.warns.some((m) => m.includes('nothing typed') && m.includes('fail-open'))).toBe(true)
    expect(h.warns.some((m) => m.includes('sent WITHOUT the per-pane lock'))).toBe(false)
  })
  it("sent after a fail-open: the old 'sent WITHOUT the per-pane lock' warning", async () => {
    expect(await failOpen('fo-sent', () => true)).toBe('sent')
    expect(h.calls.some((a) => a.includes(KEYS) && a.includes('-l'))).toBe(true)
    expect(h.warns.some((m) => m.includes('sent WITHOUT the per-pane lock'))).toBe(true)
    expect(h.warns.some((m) => m.includes('nothing typed'))).toBe(false)
  })
})

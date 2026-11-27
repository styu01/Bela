// Upstream 7a47b24a removed the router's blind main-agent wakeup; from then on the inbox-nudge-watcher
// is the ONLY thing that wakes the main agent for a pending inter-agent row (BELA msg 6175 asked for
// proof). These tests drive the watcher's real tick (fake timers, mocked tmux/DB IO) through the three
// situations the main agent is in: idle, busy, and absent then freshly started (a context-guard
// restart or a gate /clear), and pin the worst-case latency the watcher constants allow.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({
  pending: [] as Array<{ id: number; created_at: number }>,
  ready: true,
  exists: true,
  sends: [] as Array<{ at: number; session: string; text: string }>,
  t0: 0,
  alert: vi.fn(),
}))

vi.mock('../logger.js', () => ({ logger: { info: vi.fn(), warn: vi.fn(), error: vi.fn(), debug: vi.fn() } }))
vi.mock('../config.js', () => ({ MAIN_AGENT_ID: 'bela' }))
vi.mock('../db.js', () => ({ getPendingMessages: () => h.pending }))
vi.mock('../settings-store.js', () => ({ getEffectiveSettingValue: () => 'hu' }))
vi.mock('../web/main-agent.js', () => ({ MAIN_CHANNELS_SESSION: 'bela-channels' }))
vi.mock('../web/channel-monitor.js', () => ({ sendAlert: h.alert }))
vi.mock('../web/agent-process.js', () => ({
  sessionExistsOnHost: () => h.exists,
  isSessionReadyForPrompt: async () => h.exists && h.ready,
  sendPromptToSession: async (session: string, text: string) => {
    h.sends.push({ at: Date.now() - h.t0, session, text })
    return 'sent'
  },
}))

import { startInboxNudgeWatcher, INBOX_NUDGE_INITIAL_DELAY_MS, INBOX_NUDGE_INTERVAL_MS, MIN_PENDING_AGE_MS, NUDGE_DEBOUNCE_MS, _resetNudgeStateForTest } from '../web/inbox-nudge-watcher.js'

let timer: NodeJS.Timeout
const sec = (ms: number) => Math.floor(ms / 1000)
function arrive(id: number, agoMs = 0) { h.pending.push({ id, created_at: sec(Date.now() - agoMs) }) }
function drain() { h.pending.length = 0 }              // what the UserPromptSubmit drain hook does on the nudged turn

beforeEach(() => {
  vi.useFakeTimers()
  _resetNudgeStateForTest()
  h.pending.length = 0; h.sends.length = 0; h.ready = true; h.exists = true; h.alert.mockClear()
  h.t0 = Date.now()
  timer = startInboxNudgeWatcher()
})
afterEach(() => { clearInterval(timer); vi.useRealTimers() })

// run the watcher until its first tick (55 s) so every test starts on the steady 20 s cadence
async function warm() { await vi.advanceTimersByTimeAsync(INBOX_NUDGE_INITIAL_DELAY_MS + 1) }

describe('without the router wakeup, a main-agent row still wakes the main agent', () => {
  it('(1) idle main agent: nudged on the next tick once the row is >= 10 s old, into the main channels session', async () => {
    await warm()
    const arrivedAt = Date.now() - h.t0
    arrive(1)
    await vi.advanceTimersByTimeAsync(INBOX_NUDGE_INTERVAL_MS * 2)
    expect(h.sends).toHaveLength(1)
    expect(h.sends[0]!.session).toBe('bela-channels')
    expect(h.sends[0]!.text).toContain('[Inbox]')
    expect(h.sends[0]!.at - arrivedAt).toBeLessThanOrEqual(MIN_PENDING_AGE_MS + INBOX_NUDGE_INTERVAL_MS)
    drain()
    await vi.advanceTimersByTimeAsync(5 * 60_000)
    expect(h.sends).toHaveLength(1)                     // drained: no further nudge
  })

  it('(2) busy main agent: nothing is typed while busy; nudged within one interval after it goes idle', async () => {
    await warm()
    h.ready = false
    arrive(2)
    await vi.advanceTimersByTimeAsync(10 * 60_000)     // a 10-minute turn
    expect(h.sends).toHaveLength(0)
    const idleAt = Date.now() - h.t0
    h.ready = true
    await vi.advanceTimersByTimeAsync(INBOX_NUDGE_INTERVAL_MS)
    expect(h.sends).toHaveLength(1)
    expect(h.sends[0]!.at - idleAt).toBeLessThanOrEqual(INBOX_NUDGE_INTERVAL_MS)
  })

  it('(3) restart / clear: no session -> waits; the fresh session is nudged once it is ready', async () => {
    await warm()
    h.exists = false                                    // killed by the context-guard / gate restart
    arrive(3)
    await vi.advanceTimersByTimeAsync(2 * 60_000)
    expect(h.sends).toHaveLength(0)
    h.exists = true; h.ready = false                    // fresh session booting
    await vi.advanceTimersByTimeAsync(60_000)
    expect(h.sends).toHaveLength(0)
    const readyAt = Date.now() - h.t0
    h.ready = true                                      // fresh session at its prompt
    await vi.advanceTimersByTimeAsync(INBOX_NUDGE_INTERVAL_MS)
    expect(h.sends).toHaveLength(1)
    expect(h.sends[0]!.session).toBe('bela-channels')
    expect(h.sends[0]!.at - readyAt).toBeLessThanOrEqual(INBOX_NUDGE_INTERVAL_MS)
  })

  it('worst case on an idle agent: a row arriving right after a nudge waits for the 60 s debounce, never longer than debounce + one interval', async () => {
    await warm()
    arrive(4)
    await vi.advanceTimersByTimeAsync(INBOX_NUDGE_INTERVAL_MS * 2)
    expect(h.sends).toHaveLength(1)
    drain()
    const firstAt = h.sends[0]!.at
    arrive(5)                                           // right after the first nudge
    await vi.advanceTimersByTimeAsync(NUDGE_DEBOUNCE_MS + INBOX_NUDGE_INTERVAL_MS)
    expect(h.sends).toHaveLength(2)
    expect(h.sends[1]!.at - firstAt).toBeGreaterThanOrEqual(NUDGE_DEBOUNCE_MS)
    expect(h.sends[1]!.at - firstAt).toBeLessThanOrEqual(NUDGE_DEBOUNCE_MS + INBOX_NUDGE_INTERVAL_MS)
  })

  it('a drain that never claims: 3 nudges, then the owner is alerted once (no endless paid turns)', async () => {
    await warm()
    arrive(6, 60_000)
    await vi.advanceTimersByTimeAsync(30 * 60_000)
    expect(h.sends).toHaveLength(3)
    expect(h.alert).toHaveBeenCalledTimes(1)
  })
})

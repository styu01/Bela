// Integration test for the usage-fleet-pause enforcement wired into
// runMessageRouterTick() (kanban ff2ed32d, Codex review point #4). The pure
// decision logic (readFleetPauseState / shouldHoldForFleetPause) has its own
// unit tests in usage-fleet-pause.test.ts; this file verifies the router
// ACTUALLY calls that logic at the right point and behaves correctly --
// holding a FELADAT: delegation instead of delivering it while paused, but
// never blocking non-delegation traffic, unprotected agents, or delivery
// once unpaused.

import { describe, it, expect, vi, beforeEach } from 'vitest'

const mockGetPendingMessages = vi.fn()
const mockMarkDelivered = vi.fn((..._a: unknown[]) => true)
const mockMarkFailed = vi.fn((..._a: unknown[]) => true)
const mockCreateAgentMessage = vi.fn((..._a: unknown[]) => ({ id: 999 }))
const mockSendPromptToSession = vi.fn()
const mockReadFleetPauseState = vi.fn()

vi.mock('../logger.js', () => ({
  logger: { info: vi.fn(), warn: vi.fn(), debug: vi.fn(), error: vi.fn() },
}))

vi.mock('../config.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../config.js')>()),
  MAIN_AGENT_ID: 'bela',
  SUBAGENT_TELEGRAM_WAKE_ENABLED: false,
}))

vi.mock('../db.js', () => ({
  getPendingMessages: (toAgent?: string) => {
    if (toAgent) return []
    return mockGetPendingMessages()
  },
  // TICKVAKSAG916: the router re-reads a row's status before the send; 'pending' keeps this test on the delivery path.
  getMessageStatus: (_id: number) => 'pending',
  markMessageDelivered: (...a: unknown[]) => mockMarkDelivered(...a),
  markMessageFailed: (...a: unknown[]) => mockMarkFailed(...a),
  markMessageDone: (..._a: unknown[]) => true,
  createAgentMessage: (...a: unknown[]) => mockCreateAgentMessage(...a),
  stampMessageTrace: (..._a: unknown[]) => false,
  upsertOtelSpan: (..._a: unknown[]) => undefined,
  closeOtelSpan: (..._a: unknown[]) => false,
}))

vi.mock('../web/voice-directive.js', () => ({
  resolveAgentChannelStateDir: () => '/tmp/none',
}))

vi.mock('../web/agent-config.js', () => ({
  readAgentRemoteHost: () => null,
  readAgentVoiceConfig: () => ({ responseMode: 'text' }),
}))

vi.mock('../web/agent-process.js', () => ({
  agentSessionName: (name: string) => `agent-${name}`,
  isSessionReadyForPrompt: vi.fn(() => true), // session is idle/ready -- reach the pause gate
  clearStaleParkedInput: vi.fn(() => false),
  sendPromptToSession: (...a: unknown[]) => mockSendPromptToSession(...a),
  sessionExistsOnHost: () => true,
  capturePane: () => null,
}))

vi.mock('../web/voice-modality.js', () => ({
  setLastInboundModality: vi.fn(),
}))

vi.mock('../web/main-agent.js', () => ({
  MAIN_CHANNELS_SESSION: 'bela-channels',
}))

vi.mock('../web/agent-message-wrap.js', () => ({
  classifyAgentMessage: () => ({ category: 'trusted-peer', safeFrom: 'bela' }),
  wrapAgentMessageForDelivery: () => ({ prefix: '', wrapped: '' }),
}))

vi.mock('../web/usage-fleet-pause.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../web/usage-fleet-pause.js')>()),
  readFleetPauseState: (...a: unknown[]) => mockReadFleetPauseState(...a),
}))

import { runMessageRouterTick } from '../web/message-router.js'

function makeMsg(id: number, toAgent: string, content: string) {
  const nowSec = Math.floor(Date.now() / 1000)
  return { id, from_agent: 'bela', to_agent: toAgent, content, created_at: nowSec }
}

describe('message router: usage-fleet-pause enforcement', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockMarkDelivered.mockReturnValue(true)
    mockMarkFailed.mockReturnValue(true)
  })

  // Distinct message ids across every test in this file, not just within one
  // test: routerUsagePauseNotified (and routerLoggedMisses/routerInjectFailures)
  // are module-level singletons in message-router.ts that persist across
  // tests sharing the same import, not reset by vi.clearAllMocks(). Reusing
  // an id across a "held" test and a "notified" test would let the second
  // test silently observe the first test's already-notified state.

  it('holds a FELADAT: delegation to a protected agent while paused -- not delivered, not failed', async () => {
    mockReadFleetPauseState.mockReturnValue({ paused: true, metric: 'five_hour', percent: 91.2 })
    mockGetPendingMessages.mockReturnValue([makeMsg(1, 'progi', 'FELADAT:\n\nBuild the thing')])

    await runMessageRouterTick()

    expect(mockSendPromptToSession).not.toHaveBeenCalled()
    expect(mockMarkDelivered).not.toHaveBeenCalled()
    expect(mockMarkFailed).not.toHaveBeenCalled()
  })

  it('notifies the orchestrator (BÉLA) once when a delegation is held', async () => {
    mockReadFleetPauseState.mockReturnValue({ paused: true, metric: 'five_hour', percent: 91.2, source: 'authoritative_statusline' })
    mockGetPendingMessages.mockReturnValue([makeMsg(2, 'progi', 'FELADAT:\n\nBuild the thing')])

    await runMessageRouterTick()

    const notice = mockCreateAgentMessage.mock.calls.find(
      (c) => c[0] === 'system' && c[1] === 'bela' && String(c[2]).includes('usage-pause-held'),
    )
    expect(notice).toBeDefined()
    expect(String(notice?.[2])).toContain('five_hour')
    expect(String(notice?.[2])).toContain('91.2%')
  })

  it('delivers a FELADAT: delegation normally when NOT paused', async () => {
    mockReadFleetPauseState.mockReturnValue({ paused: false })
    mockGetPendingMessages.mockReturnValue([makeMsg(3, 'progi', 'FELADAT:\n\nBuild the thing')])

    await runMessageRouterTick()

    expect(mockSendPromptToSession).toHaveBeenCalledTimes(1)
    expect(mockMarkDelivered).toHaveBeenCalledWith(3)
  })

  it('delivers a non-FELADAT: message normally even while paused (replies/coordination stay flowing)', async () => {
    mockReadFleetPauseState.mockReturnValue({ paused: true, metric: 'five_hour', percent: 91.2 })
    mockGetPendingMessages.mockReturnValue([makeMsg(4, 'progi', 'Quick question about the last task')])

    await runMessageRouterTick()

    expect(mockSendPromptToSession).toHaveBeenCalledTimes(1)
    expect(mockMarkDelivered).toHaveBeenCalledWith(4)
  })

  it('delivers the pause announcement itself even though it targets a protected agent while paused (no chicken-and-egg deadlock)', async () => {
    mockReadFleetPauseState.mockReturnValue({ paused: true, metric: 'five_hour', percent: 91.2 })
    mockGetPendingMessages.mockReturnValue([
      makeMsg(5, 'progi', 'MEGOSZTOTT USAGE >=90% (five_hour 91.2%). NE indits UJ munkat amig nem jelzem hogy feloldva.'),
    ])

    await runMessageRouterTick()

    expect(mockSendPromptToSession).toHaveBeenCalledTimes(1)
    expect(mockMarkDelivered).toHaveBeenCalledWith(5)
  })

  it('delivers a FELADAT: delegation to an unprotected agent even while paused', async () => {
    mockReadFleetPauseState.mockReturnValue({ paused: true, metric: 'five_hour', percent: 91.2 })
    mockGetPendingMessages.mockReturnValue([makeMsg(6, 'vizsla', 'FELADAT:\n\nRead this page')])

    await runMessageRouterTick()

    expect(mockSendPromptToSession).toHaveBeenCalledTimes(1)
    expect(mockMarkDelivered).toHaveBeenCalledWith(6)
  })

  it('holds each protected agent independently: progi held, vizsla not, in the same tick', async () => {
    mockReadFleetPauseState.mockReturnValue({ paused: true })
    mockGetPendingMessages.mockReturnValue([
      makeMsg(7, 'progi', 'FELADAT:\n\nA'),
      makeMsg(8, 'vizsla', 'FELADAT:\n\nB'),
      makeMsg(9, 'okoska', 'FELADAT:\n\nC'),
    ])

    await runMessageRouterTick()

    expect(mockSendPromptToSession).toHaveBeenCalledTimes(1) // only vizsla's message
    expect(mockMarkDelivered).toHaveBeenCalledWith(8)
    expect(mockMarkDelivered).not.toHaveBeenCalledWith(7)
    expect(mockMarkDelivered).not.toHaveBeenCalledWith(9)
  })
})

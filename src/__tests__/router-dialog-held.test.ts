// BELA 6313 E1 / PROGI 6312: the router asks sendPromptToSession for onDialogBlocked 'abort-blocked'. When a
// resume / model-consent dialog its handler could not answer is up ('blocked'), nothing is typed and
// 'aborted-dialog' comes back: the row stays PENDING (no markMessageDelivered, no markMessageFailed, the
// inject-failure counter untouched, so it can never turn 'failed' this way), one log line per message,
// delivered on the first tick after the dialog is gone. Harness from router-status-reread.test.ts.
import { describe, it, expect, vi, beforeEach } from 'vitest'

const mockGetPendingMessages = vi.fn()
const mockMarkDelivered = vi.fn((..._a: unknown[]) => true)
const mockMarkFailed = vi.fn((..._a: unknown[]) => true)
const mockSendPrompt = vi.fn(async (..._a: unknown[]) => 'sent' as const)
// The live status of each row, as the DB would answer it RIGHT NOW -- the test
// mutates this mid-tick to model a change landing while the tick runs.
const liveStatus = new Map<number, string | null>()
const mockGetMessageStatus = vi.fn((id: number) => liveStatus.get(id) ?? null)

const mockWarn = vi.fn()
vi.mock('../logger.js', () => ({
  logger: { info: vi.fn(), warn: (...a: unknown[]) => mockWarn(...a), debug: vi.fn(), error: vi.fn() },
}))

vi.mock('../config.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../config.js')>()),
  MAIN_AGENT_ID: 'orin',
  SUBAGENT_TELEGRAM_WAKE_ENABLED: false,
}))

vi.mock('../db.js', () => ({
  getPendingMessages: (toAgent?: string) => {
    if (toAgent) return [] // per-agent query of the reconnect pre-pass
    return mockGetPendingMessages()
  },
  getMessageStatus: (id: number) => mockGetMessageStatus(id),
  markMessageDelivered: (...a: unknown[]) => mockMarkDelivered(...a),
  markMessageFailed: (...a: unknown[]) => mockMarkFailed(...a),
  markMessageDone: (..._a: unknown[]) => true,
  markPendingFederatedFailed: (..._a: unknown[]) => true,
  setMessageResult: (..._a: unknown[]) => true,
  createAgentMessage: (..._a: unknown[]) => ({ id: 999 }),
  countNewerMessagesFromSameSender: (..._a: unknown[]) => 0,
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
  readAgentWorksourceChannel: () => false,
}))

vi.mock('../web/agent-process.js', () => ({
  clearFeedbackModalAndRecheck: () => false,
  agentSessionName: (name: string) => `agent-${name}`,
  isSessionReadyForPrompt: vi.fn(async () => true),
  clearStaleParkedInput: vi.fn(async () => false),
  sendPromptToSession: (...a: unknown[]) => mockSendPrompt(...a),
  sessionExistsOnHost: (..._a: unknown[]) => true,
  capturePane: (..._a: unknown[]) => '',
}))

vi.mock('../web/voice-modality.js', () => ({
  setLastInboundModality: vi.fn(),
}))

vi.mock('../web/main-agent.js', () => ({
  MAIN_CHANNELS_SESSION: 'orin-channels',
}))

vi.mock('../web/agent-message-wrap.js', () => ({
  classifyAgentMessage: () => ({ category: 'trusted-peer', safeFrom: 'orin' }),
  wrapAgentMessageForDelivery: () => ({ prefix: '', wrapped: 'payload' }),
}))

vi.mock('../web/telegram-inbox-wake.js', () => ({
  maybeWakeSubAgentsForTelegram: vi.fn(),
}))

import { runMessageRouterTick } from '../web/message-router.js'

function pendingRows(ids: number[]) {
  const nowSec = Math.floor(Date.now() / 1000)
  return ids.map((id) => ({
    id,
    from_agent: 'orin',
    to_agent: 'dex', // sub-agent -> tmux-inject path (the main agent PULLs)
    content: `payload ${id}`,
    created_at: nowSec,
    origin_note: null,
    trace_id: null,
    span_id: null,
  }))
}

function snapshot(ids: number[]) {
  liveStatus.clear()
  for (const id of ids) liveStatus.set(id, 'pending')
  mockGetPendingMessages.mockReturnValue(pendingRows(ids))
}


const heldLogs = () => mockWarn.mock.calls.filter((c) => String(c[1] ?? '').startsWith('Agent message held')).length

describe('message router: a message held behind a dialog that could not be answered', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    liveStatus.clear()
    mockMarkDelivered.mockReturnValue(true)
    mockMarkFailed.mockReturnValue(true)
  })

  it("asks for 'abort-blocked'; 'aborted-dialog' keeps the row pending across many ticks, logged once, then delivers", async () => {
    snapshot([501])
    mockSendPrompt.mockImplementation(async () => 'aborted-dialog' as never)
    for (let i = 0; i < 6; i++) await runMessageRouterTick()
    expect(mockSendPrompt).toHaveBeenCalledTimes(6)
    // PROGI 6026: plus the router's after-the-wait re-read (stillWanted)
    expect(mockSendPrompt.mock.calls[0][3]).toEqual({ onDialogBlocked: 'abort-blocked', stillWanted: expect.any(Function) })
    expect(mockMarkDelivered).not.toHaveBeenCalled()
    expect(mockMarkFailed).not.toHaveBeenCalled()
    expect(heldLogs()).toBe(1)
    mockSendPrompt.mockImplementation(async () => 'sent' as const)
    await runMessageRouterTick()
    expect(mockMarkDelivered).toHaveBeenCalledWith(501)
    expect(mockMarkFailed).not.toHaveBeenCalled()
  })

  it('held ticks do not count as inject failures: two later throws still retry (MAX 3), the third fails', async () => {
    snapshot([502])
    mockSendPrompt.mockImplementation(async () => 'aborted-dialog' as never)
    for (let i = 0; i < 5; i++) await runMessageRouterTick()
    mockSendPrompt.mockImplementation(async () => { throw new Error('tmux') })
    await runMessageRouterTick()
    await runMessageRouterTick()
    expect(mockMarkFailed).not.toHaveBeenCalled()
    await runMessageRouterTick()
    expect(mockMarkFailed).toHaveBeenCalledTimes(1)
  })

  it('a second held message gets its own log line; one delivered meanwhile is not affected', async () => {
    snapshot([503, 504])
    mockSendPrompt.mockImplementation(async (...a: unknown[]) => (mockSendPrompt.mock.calls.length % 2 === 1 ? 'aborted-dialog' : 'sent') as never)
    await runMessageRouterTick()
    expect(heldLogs()).toBe(1)
    expect(mockMarkDelivered).toHaveBeenCalledTimes(1)
  })
})

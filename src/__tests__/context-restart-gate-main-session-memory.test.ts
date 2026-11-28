import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

// PROGI review 6206 item 2 (5ed34743 residual risk): after the context-restart gate's OWN /clear of the main agent,
// the remembered last-proven session (the respawn fallback when the claude already died) was the PRE-clear sid until
// the next proof (a guard tick, up to 5 min). A death in that window would --resume the large pre-clear context.
// Now the gate forgets it at once and proves the session again MAIN_SESSION_REPROVE_DELAY_MS later. Sub-agents are
// not touched (the memory is the main agent's only). The mock set is the wiring test's, so checkAgent runs its real
// allow branch.
const SANDBOX = mkdtempSync(join(tmpdir(), 'gate-mainmem-'))
mkdirSync(join(SANDBOX, 'store'), { recursive: true })

vi.mock('../config.js', async (orig) => {
  const actual = await orig<typeof import('../config.js')>()
  return { ...actual, MAIN_AGENT_ID: 'testmain', PROJECT_ROOT: SANDBOX }
})
vi.mock('../logger.js', () => ({
  logger: { info: vi.fn(), warn: vi.fn(), debug: vi.fn(), error: vi.fn() },
}))
vi.mock('../db.js', () => ({
  getDispatchedPendingStats: vi.fn(() => ({ count: 0, hasStale: false })),
  // LEDGERACK905 (ported from upstream Szotasz/marveen 4fb9fbcbf, 2026-09-10):
  // the runner now calls openInboundQuestionMessageId, not
  // hasOpenInboundQuestion (kept in db.ts for other callers, but unused here).
  // Mocking only the old name would silently fall through the runner's own
  // try/catch to `false` on every call -- passing, but no longer exercising
  // the real wiring. null = "nothing open", matching the old mock's false.
  openInboundQuestionMessageId: vi.fn(() => null),
  hasBlockingKanbanCardForAssignee: vi.fn(() => false),
  createAgentMessage: vi.fn(),
  GATE_ALERT_ORIGIN_NOTE: 'context-restart-gate persistent-block alert',
  GATE_NOTICE_ORIGIN_NOTE: 'context-restart-gate pre-clear notice',
}))
vi.mock('../web/context-restart-transcript-archive.js', () => ({
  archiveTranscriptBeforeContextRestart: vi.fn(() => ({
    path: '/cold/archive.txt', sourcePath: '/session.jsonl',
  })),
}))
vi.mock('../web/usage-fleet-pause.js', () => ({
  readFleetPauseState: vi.fn(() => ({ paused: false })),
}))
vi.mock('../web/agent-process.js', () => ({
  // KISPROGI 6543 F3: checkAgent skips a positively 'stopped' sub-agent.
  agentRunState: vi.fn(() => 'running'),
  agentSessionName: (n: string) => `agent-${n}`,
  capturePane: vi.fn(() => 'fake pane content\n$ '),
  sendPromptToSession: vi.fn(async () => 'sent'),
}))
vi.mock('../pane-state.js', () => ({
  detectPaneState: vi.fn(() => 'idle'),
}))
vi.mock('../model-fallback.js', () => ({
  detectsUsageLimit: vi.fn(() => false),
}))
vi.mock('../web/active-model.js', () => ({
  readContextTokensFromProjectDir: vi.fn(() => 0),
}))
vi.mock('../web/main-agent.js', () => ({
  MAIN_CHANNELS_SESSION: 'testmain-channels',
}))
vi.mock('../web/context-guard-runner.js', () => ({
  getHardGuardPhase: vi.fn(() => 'idle'),
}))
vi.mock('../web/agent-config.js', () => ({
  listAgentNames: vi.fn(() => []),
  readAgentClaudeConfigDir: vi.fn(() => undefined),
}))

// Single dispatcher mock for node:child_process's execFileSync, branching on
// the tmux/ps arg shape (see context-restart-gate-runner.ts's own comments on
// "direct shape" pane-process detection for why each branch exists).
const execFileSyncMock = vi.fn((_cmd: string, args?: string[]) => {
  if (args?.[0] === 'list-panes') return '12345\n'          // getPanePid
  if (args?.[0] === '--ppid') return ''                      // getChildPids -> no children
  if (args?.includes?.('etimes=')) return '120\n'             // getPidAgeSeconds
  if (args?.includes?.('comm=')) return 'claude\n'            // getCommForPid (direct shape: pane IS claude)
  if (args?.[0] === 'send-keys') return ''                    // the /clear + Enter sends
  return ''
})
const mst = vi.hoisted(() => ({ forget: vi.fn(), prove: vi.fn(() => ({ resume: { sessionId: 'new-sid', transcriptPath: '/t/new.jsonl' }, reason: null })) }))
vi.mock('../web/main-session-transcript.js', () => ({
  forgetRememberedMainSession: mst.forget,
  provenMainSession: mst.prove,
}))
vi.mock('node:child_process', () => ({ execFileSync: execFileSyncMock }))

const { checkAgent, settleMainSessionMemoryAfterClear, MAIN_SESSION_REPROVE_DELAY_MS } = await import('../web/context-restart-gate-runner.js')
const { writeGateConfig } = await import('../web/context-restart-gate-store.js')
const { readContextTokensFromProjectDir } = await import('../web/active-model.js')
const { PRE_CLEAR_NOTICE_MS } = await import('../context-restart-gate.js')

beforeEach(() => {
  execFileSyncMock.mockClear()
  mst.forget.mockClear()
  mst.prove.mockClear()
})
afterAll(() => rmSync(SANDBOX, { recursive: true, force: true }))

async function clearVia(name: string): Promise<void> {
  writeGateConfig(name, { enabled: true, thresholdTokens: 100, wakeAfterClear: false })
  vi.mocked(readContextTokensFromProjectDir).mockReturnValue(500)
  const nowMs = Date.now()
  await checkAgent(name, nowMs)
  await checkAgent(name, nowMs + PRE_CLEAR_NOTICE_MS)
  expect(execFileSyncMock.mock.calls.some(([, args]) => (args as string[] | undefined)?.includes('/clear'))).toBe(true)
}

describe('the gate\'s own /clear of the main agent settles the remembered main session', () => {
  it('main agent: the memory is forgotten right after the /clear, not before it', async () => {
    await clearVia('testmain')
    expect(mst.forget).toHaveBeenCalledTimes(1)
    expect(mst.forget).toHaveBeenCalledWith(SANDBOX)
    const clearCall = execFileSyncMock.mock.calls.findIndex(([, args]) => (args as string[] | undefined)?.includes('/clear'))
    expect(mst.forget.mock.invocationCallOrder[0]).toBeGreaterThan(execFileSyncMock.mock.invocationCallOrder[clearCall])
  })

  it('a sub-agent\'s /clear leaves the main session memory alone', async () => {
    await clearVia('worker-x')
    expect(mst.forget).not.toHaveBeenCalled()
    expect(mst.prove).not.toHaveBeenCalled()
  })

  it('a failed /clear send forgets nothing (the old session is still the live one)', async () => {
    writeGateConfig('testmain', { enabled: true, thresholdTokens: 100, wakeAfterClear: false })
    vi.mocked(readContextTokensFromProjectDir).mockReturnValue(500)
    const nowMs = Date.now() + 10 * 60 * 60_000
    await checkAgent('testmain', nowMs)
    const real = execFileSyncMock.getMockImplementation()!
    execFileSyncMock.mockImplementation((cmd: string, args?: string[]) => {
      if (args?.includes('/clear')) throw new Error('tmux gone')
      return real(cmd, args)
    })
    try {
      await checkAgent('testmain', nowMs + PRE_CLEAR_NOTICE_MS)
    } finally {
      execFileSyncMock.mockImplementation(real)
    }
    // The send WAS attempted (so this is the failure path, not an earlier exit)...
    expect(execFileSyncMock.mock.calls.some(([, args]) => (args as string[] | undefined)?.includes('/clear'))).toBe(true)
    // ...and nothing was forgotten.
    expect(mst.forget).not.toHaveBeenCalled()
  })
})

describe('settleMainSessionMemoryAfterClear', () => {
  it('forgets at once, proves again after MAIN_SESSION_REPROVE_DELAY_MS, on the main channels session', () => {
    vi.useFakeTimers()
    try {
      settleMainSessionMemoryAfterClear()
      expect(mst.forget).toHaveBeenCalledWith(SANDBOX)
      expect(mst.prove).not.toHaveBeenCalled()
      vi.advanceTimersByTime(MAIN_SESSION_REPROVE_DELAY_MS - 1)
      expect(mst.prove).not.toHaveBeenCalled()
      vi.advanceTimersByTime(1)
      expect(mst.prove).toHaveBeenCalledTimes(1)
      expect(mst.prove).toHaveBeenCalledWith('testmain-channels', SANDBOX)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a throwing re-proof is contained (the memory just stays empty)', () => {
    vi.useFakeTimers()
    try {
      mst.prove.mockImplementationOnce(() => { throw new Error('boom') })
      settleMainSessionMemoryAfterClear()
      expect(() => vi.advanceTimersByTime(MAIN_SESSION_REPROVE_DELAY_MS)).not.toThrow()
    } finally {
      vi.useRealTimers()
    }
  })

  it('the delay is short next to the 5-minute guard tick it replaces', () => {
    expect(MAIN_SESSION_REPROVE_DELAY_MS).toBeGreaterThanOrEqual(5_000)
    expect(MAIN_SESSION_REPROVE_DELAY_MS).toBeLessThanOrEqual(30_000)
  })
})

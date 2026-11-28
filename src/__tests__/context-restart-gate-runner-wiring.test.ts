import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

// Codex review point 1 (kanban 0d8bf173, round 2): the new PUT
// /api/agents/:name/context-restart-gate route is worthless if nothing reads
// the config it writes. `checkAgent` (context-restart-gate-runner.ts) is the
// exact function the live sweep calls on every tick, exported specifically so
// this can be proven end-to-end: write a config the same way the route does
// (via writeGateConfig), then call checkAgent directly and assert on the
// process boundary (execFileSync) it actually crosses -- not on the store,
// which would only prove the write, not the wiring.
const SANDBOX = mkdtempSync(join(tmpdir(), 'gate-wiring-'))
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
  // KISPROGI 6543 F3, 6584 K2: checkAgent skips a positively stopped sub-agent.
  agentPositivelyStopped: vi.fn(() => false),
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
vi.mock('node:child_process', () => ({ execFileSync: execFileSyncMock }))

const { checkAgent, deliverPendingWake, gateWakePrompt } = await import('../web/context-restart-gate-runner.js')
const { sendPromptToSession, agentPositivelyStopped } = await import('../web/agent-process.js')
const { writeGateConfig, readGateRunState, writeGateRunState } = await import('../web/context-restart-gate-store.js')
const { readContextTokensFromProjectDir } = await import('../web/active-model.js')
const { PRE_CLEAR_NOTICE_MS, WAKE_DELAY_MS, WAKE_MAX_AGE_MS } = await import('../context-restart-gate.js')
const { hasBlockingKanbanCardForAssignee, createAgentMessage, openInboundQuestionMessageId } = await import('../db.js')
const { archiveTranscriptBeforeContextRestart } = await import('../web/context-restart-transcript-archive.js')
const { readFleetPauseState } = await import('../web/usage-fleet-pause.js')

beforeEach(() => {
  execFileSyncMock.mockClear()
  vi.mocked(agentPositivelyStopped).mockReset()
  vi.mocked(agentPositivelyStopped).mockReturnValue(false)
  vi.mocked(hasBlockingKanbanCardForAssignee).mockReset()
  vi.mocked(hasBlockingKanbanCardForAssignee).mockReturnValue(false)
  vi.mocked(openInboundQuestionMessageId).mockReset()
  vi.mocked(openInboundQuestionMessageId).mockReturnValue(null)
  vi.mocked(createAgentMessage).mockClear()
  vi.mocked(archiveTranscriptBeforeContextRestart).mockReset()
  vi.mocked(archiveTranscriptBeforeContextRestart).mockReturnValue({
    path: '/cold/archive.txt', sourcePath: '/session.jsonl',
  })
  vi.mocked(readFleetPauseState).mockReset()
  vi.mocked(readFleetPauseState).mockReturnValue({ paused: false })
})
afterAll(() => rmSync(SANDBOX, { recursive: true, force: true }))

describe('context-restart-gate wiring: config store -> live sweep', () => {
  it('disabled config: checkAgent never touches tmux', async () => {
    // No writeGateConfig call for this agent -- readGateConfig falls back to
    // DEFAULT_GATE_CONFIG, whose enabled is false. This is the state every
    // agent is in right now (kanban 0d8bf173: build the switch, don't flip it).
    await checkAgent('worker-disabled', Date.now())

    expect(execFileSyncMock).not.toHaveBeenCalled()
  })

  it('enabled config + all-clear inputs: sends a ten-minute notice, then /clear via tmux send-keys', async () => {
    const name = 'worker-enabled'
    // Mirrors exactly what PUT /api/agents/:name/context-restart-gate writes.
    writeGateConfig(name, { enabled: true, thresholdTokens: 100 })
    vi.mocked(readContextTokensFromProjectDir).mockReturnValue(500) // >= thresholdTokens

    const nowMs = Date.now()
    await checkAgent(name, nowMs)

    // The first all-clear sweep must never clear immediately: it gives the
    // agent a bounded opportunity to write a concise warm state note.
    expect(execFileSyncMock.mock.calls.some(([, args]) => args?.includes('/clear'))).toBe(false)
    expect(readGateRunState(name).preClearNoticeAt).toBe(nowMs)

    // The fixed notice must be the approved full ten minutes. At the halfway
    // point the countdown is still a hard
    // stop; at the exact boundary the re-check may proceed to /clear.
    expect(PRE_CLEAR_NOTICE_MS).toBe(10 * 60_000)
    await checkAgent(name, nowMs + PRE_CLEAR_NOTICE_MS / 2)
    expect(execFileSyncMock.mock.calls.some(([, args]) => args?.includes('/clear'))).toBe(false)

    await checkAgent(name, nowMs + PRE_CLEAR_NOTICE_MS)

    // Crossed the process boundary: the actual send-keys call the runner's
    // 'allow' branch makes, not just an in-process assertion on the store.
    const sendKeysCalls = execFileSyncMock.mock.calls.filter(([, args]) => args?.[0] === 'send-keys')
    expect(sendKeysCalls.length).toBeGreaterThan(0)
    expect(sendKeysCalls.some(([, args]) => args?.includes('/clear'))).toBe(true)
    expect(archiveTranscriptBeforeContextRestart).toHaveBeenCalledWith(expect.objectContaining({
      agent: name,
      workingDir: join(SANDBOX, 'agents', name),
      nowMs: nowMs + PRE_CLEAR_NOTICE_MS,
    }))
    const firstClearSend = execFileSyncMock.mock.calls.findIndex(([, args]) => args?.includes('/clear'))
    expect(firstClearSend).toBeGreaterThanOrEqual(0)
    expect(vi.mocked(archiveTranscriptBeforeContextRestart).mock.invocationCallOrder[0])
      .toBeLessThan(execFileSyncMock.mock.invocationCallOrder[firstClearSend])

    // And the run-state round-trips through the real (sandboxed) store, same
    // as the live sweep would leave it for the next tick.
    const runState = readGateRunState(name)
    expect(runState.lastClearAt).toBe(nowMs + PRE_CLEAR_NOTICE_MS)
    expect(runState.firstBlockedAt).toBeNull()
    expect(runState.preClearNoticeAt).toBeNull()
    // upstream 8953cacf: the /clear leaves a persisted wake debt for the fresh session.
    expect(runState.pendingWakeAt).toBe(nowMs + PRE_CLEAR_NOTICE_MS)
  })

  it('disabled config for a second agent still never touches tmux, even after the enabled one fired', async () => {
    // Guards against a shared-mutable-state false pass: prove the fast-exit
    // still holds per-agent, not just "the mock was clean at suite start".
    await checkAgent('worker-still-disabled', Date.now())

    expect(execFileSyncMock).not.toHaveBeenCalled()
  })

  it('assigned unfinished Kanban work blocks /clear even with every process-level signal clear', async () => {
    const name = 'worker-kanban-open'
    writeGateConfig(name, { enabled: true, thresholdTokens: 100 })
    vi.mocked(readContextTokensFromProjectDir).mockReturnValueOnce(500)
    vi.mocked(hasBlockingKanbanCardForAssignee).mockReturnValueOnce(true)

    await checkAgent(name, Date.now())

    expect(archiveTranscriptBeforeContextRestart).not.toHaveBeenCalled()
    expect(execFileSyncMock.mock.calls.some(([, args]) => args?.[0] === 'send-keys')).toBe(false)
  })

  it('fails closed when the Kanban query itself throws', async () => {
    const name = 'worker-kanban-unmeasurable'
    writeGateConfig(name, { enabled: true, thresholdTokens: 100 })
    vi.mocked(readContextTokensFromProjectDir).mockReturnValueOnce(500)
    vi.mocked(hasBlockingKanbanCardForAssignee).mockImplementationOnce(() => { throw new Error('db unavailable') })

    await checkAgent(name, Date.now())

    expect(archiveTranscriptBeforeContextRestart).not.toHaveBeenCalled()
    expect(execFileSyncMock.mock.calls.some(([, args]) => args?.[0] === 'send-keys')).toBe(false)
  })

  it('never sends /clear when the mandatory pre-clear transcript archive fails', async () => {
    const name = 'worker-archive-failed'
    writeGateConfig(name, { enabled: true, thresholdTokens: 100 })
    vi.mocked(readContextTokensFromProjectDir).mockReturnValue(500)
    const nowMs = Date.now()
    await checkAgent(name, nowMs) // pre-clear notice
    vi.mocked(archiveTranscriptBeforeContextRestart).mockImplementationOnce(() => {
      throw new Error('no transcript')
    })

    await checkAgent(name, nowMs + PRE_CLEAR_NOTICE_MS)

    expect(execFileSyncMock.mock.calls.some(([, args]) => args?.[0] === 'send-keys')).toBe(false)
    expect(readGateRunState(name).lastClearAt).toBeNull()
    expect(readGateRunState(name).preClearNoticeAt).toBeNull()

    await checkAgent(name, nowMs + 6 * 60_000)
    expect(readGateRunState(name).preClearNoticeAt).toBe(nowMs + 6 * 60_000)
  })

  it('sends a pre-clear notice and clears while the shared usage fleet pause is active when all local signals are clear', async () => {
    const name = 'worker-usage-paused'
    writeGateConfig(name, { enabled: true, thresholdTokens: 100 })
    vi.mocked(readContextTokensFromProjectDir).mockReturnValue(500)
    vi.mocked(readFleetPauseState).mockReturnValue({ paused: true, metric: 'five_hour', percent: 91 })
    const nowMs = Date.now()

    await checkAgent(name, nowMs)

    expect(createAgentMessage).toHaveBeenCalledTimes(1)
    expect(readGateRunState(name).preClearNoticeAt).toBe(nowMs)
    expect(archiveTranscriptBeforeContextRestart).not.toHaveBeenCalled()

    await checkAgent(name, nowMs + PRE_CLEAR_NOTICE_MS)

    expect(archiveTranscriptBeforeContextRestart).toHaveBeenCalledTimes(1)
    expect(execFileSyncMock.mock.calls.some(([, args]) => args?.includes('/clear'))).toBe(true)
    expect(readGateRunState(name).lastClearAt).toBe(nowMs + PRE_CLEAR_NOTICE_MS)
  })

  it('invalidates an old notice when new Kanban work appears, requiring a fresh notice after it clears', async () => {
    const name = 'worker-notice-invalidated'
    writeGateConfig(name, { enabled: true, thresholdTokens: 100 })
    vi.mocked(readContextTokensFromProjectDir).mockReturnValue(500)
    const nowMs = Date.now()
    await checkAgent(name, nowMs)
    expect(readGateRunState(name).preClearNoticeAt).toBe(nowMs)

    vi.mocked(hasBlockingKanbanCardForAssignee).mockReturnValueOnce(true)
    await checkAgent(name, nowMs + PRE_CLEAR_NOTICE_MS)
    expect(readGateRunState(name).preClearNoticeAt).toBeNull()
    expect(execFileSyncMock.mock.calls.some(([, args]) => args?.includes('/clear'))).toBe(false)

    await checkAgent(name, nowMs + 6 * 60_000)
    expect(readGateRunState(name).preClearNoticeAt).toBe(nowMs + 6 * 60_000)
  })
})

// upstream 8953cacf (#976), runner side: the wake nudge owed after a gate /clear. The pure due/drop rule is pinned in
// context-restart-gate.test.ts (decideWake); these cases drive the delivery through the real (sandboxed) store.
describe('context-restart-gate wake nudge delivery', () => {
  const owe = (name: string, at: number) => writeGateRunState(name, { ...readGateRunState(name), pendingWakeAt: at })
  beforeEach(() => {
    vi.mocked(sendPromptToSession).mockReset()
    vi.mocked(sendPromptToSession).mockResolvedValue('sent')
  })

  it('waits while the fresh session boots, then delivers once and clears the debt', async () => {
    const t0 = Date.now()
    owe('wake-a', t0)
    await deliverPendingWake('wake-a', 'agent-wake-a', t0 + WAKE_DELAY_MS - 1)
    expect(sendPromptToSession).not.toHaveBeenCalled()
    await deliverPendingWake('wake-a', 'agent-wake-a', t0 + WAKE_DELAY_MS + 1)
    expect(sendPromptToSession).toHaveBeenCalledTimes(1)
    expect(vi.mocked(sendPromptToSession).mock.calls[0][0]).toBe('agent-wake-a')
    expect(vi.mocked(sendPromptToSession).mock.calls[0][1]).toBe(gateWakePrompt())
    expect(vi.mocked(sendPromptToSession).mock.calls[0][3]).toMatchObject({ waitForIdle: true, onBusyTimeout: 'abort' })
    expect(readGateRunState('wake-a').pendingWakeAt).toBeNull()
  })

  it('a busy pane keeps the debt for the next sweep', async () => {
    const t0 = Date.now()
    owe('wake-b', t0)
    vi.mocked(sendPromptToSession).mockResolvedValue('aborted-busy')
    await deliverPendingWake('wake-b', 'agent-wake-b', t0 + WAKE_DELAY_MS + 1)
    expect(readGateRunState('wake-b').pendingWakeAt).toBe(t0)
  })

  it('a debt older than the max age is dropped, never typed into moved-on work', async () => {
    const t0 = Date.now()
    owe('wake-c', t0)
    await deliverPendingWake('wake-c', 'agent-wake-c', t0 + WAKE_MAX_AGE_MS + 1)
    expect(sendPromptToSession).not.toHaveBeenCalled()
    expect(readGateRunState('wake-c').pendingWakeAt).toBeNull()
  })

  it('the fast-path timer and a sweep never both deliver the same debt', async () => {
    const t0 = Date.now()
    owe('wake-d', t0)
    let release: (v: 'sent') => void = () => {}
    vi.mocked(sendPromptToSession).mockImplementation(() => new Promise((r) => { release = r }))
    const first = deliverPendingWake('wake-d', 'agent-wake-d', t0 + WAKE_DELAY_MS + 1)
    await deliverPendingWake('wake-d', 'agent-wake-d', t0 + WAKE_DELAY_MS + 2)
    release('sent')
    await first
    expect(sendPromptToSession).toHaveBeenCalledTimes(1)
  })

  it('checkAgent settles an owed wake on its next sweep (a dashboard restart lost the timer)', async () => {
    const name = 'wake-e'
    writeGateConfig(name, { enabled: true, thresholdTokens: 1_000_000 })
    const t0 = Date.now()
    owe(name, t0)
    await checkAgent(name, t0 + WAKE_DELAY_MS + 1)
    expect(sendPromptToSession).toHaveBeenCalledTimes(1)
    expect(readGateRunState(name).pendingWakeAt).toBeNull()
  })
})

describe('context-restart-gate wakeAfterClear switch (LOCAL)', () => {
  it('an agent with wakeAfterClear false owes no wake after its /clear', async () => {
    const name = 'wake-off'
    writeGateConfig(name, { enabled: true, thresholdTokens: 100, wakeAfterClear: false })
    vi.mocked(readContextTokensFromProjectDir).mockReturnValue(500)
    const nowMs = Date.now()
    await checkAgent(name, nowMs)
    await checkAgent(name, nowMs + PRE_CLEAR_NOTICE_MS)
    expect(readGateRunState(name).lastClearAt).toBe(nowMs + PRE_CLEAR_NOTICE_MS)
    expect(readGateRunState(name).pendingWakeAt).toBeNull()
  })
})

// upstream cbef8ef4 (GATESENDER922 / GATEDEADLOCK922): the persistent-block alert's envelope and wording escalation,
// observable only from the live sweep.
describe('context-restart-gate persistent-block alert envelope', () => {
  it("is sent by 'system' with its own prefix and origin note, and escalates past 120 minutes", async () => {
    const name = 'alert-env'
    writeGateConfig(name, { enabled: true, thresholdTokens: 100, persistentBlockAlertMs: 60_000 })
    vi.mocked(readContextTokensFromProjectDir).mockReturnValue(500)
    vi.mocked(hasBlockingKanbanCardForAssignee).mockReturnValue(true)
    const t0 = Date.now()
    await checkAgent(name, t0)
    await checkAgent(name, t0 + 2 * 60_000)
    const calls = () => vi.mocked(createAgentMessage).mock.calls.filter((c) => String(c[2]).startsWith('[CONTEXT-RESTART-GATE-RIASZTAS]'))
    expect(calls().length).toBe(1)
    const [from, to, text, note] = calls()[0]
    expect(from).toBe('system')
    expect(to).toBe('testmain')
    expect(text).toContain(`"${name}"`)
    expect(text).not.toContain('FIGYELEM: ugyanez az ok')
    expect(note).toBe('context-restart-gate persistent-block alert')
    await checkAgent(name, t0 + 130 * 60_000)
    expect(calls().length).toBe(2)
    expect(String(calls()[1][2])).toContain('FIGYELEM: ugyanez az ok 130 perce valtozatlan')
  })
})

// KISPROGI 6543 F3: a stopped sub-agent (on-demand autobot/iris, or one stopped on purpose) used to be probed on every
// sweep: tmux list-panes wrote "can't find session: agent-<name>" to the dashboard's stderr, and its last transcript
// could start a block clock ending in a false persistent-block alert.
describe('context-restart-gate: a stopped sub-agent is not probed', () => {
  it('skips every tmux probe, message and transcript read for a stopped sub-agent', async () => {
    const name = 'stopped-ondemand'
    writeGateConfig(name, { enabled: true, thresholdTokens: 100, persistentBlockAlertMs: 60_000 })
    vi.mocked(agentPositivelyStopped).mockReturnValue(true)
    vi.mocked(readContextTokensFromProjectDir).mockClear()
    vi.mocked(readContextTokensFromProjectDir).mockReturnValue(500)
    const t0 = Date.now()
    await checkAgent(name, t0)
    await checkAgent(name, t0 + 3 * 60 * 60_000)
    expect(execFileSyncMock).not.toHaveBeenCalled()
    expect(readContextTokensFromProjectDir).not.toHaveBeenCalled()
    expect(vi.mocked(createAgentMessage)).not.toHaveBeenCalled()
    expect(vi.mocked(sendPromptToSession)).not.toHaveBeenCalledWith(`agent-${name}`, expect.anything(), expect.anything(), expect.anything())
    expect(readGateRunState(name).firstBlockedAt).toBeNull()
  })

  it('clears a stale block streak and pre-clear notice once the agent is stopped, keeps the rest', async () => {
    const name = 'stopped-with-streak'
    writeGateConfig(name, { enabled: true, thresholdTokens: 100, persistentBlockAlertMs: 60_000 })
    writeGateRunState(name, { firstBlockedAt: 1000, lastAlertAt: 2000, lastClearAt: 3000, preClearNoticeAt: 4000, pendingWakeAt: 5000 })
    vi.mocked(agentPositivelyStopped).mockReturnValue(true)
    await checkAgent(name, Date.now())
    expect(readGateRunState(name)).toEqual({ firstBlockedAt: null, lastAlertAt: 2000, lastClearAt: 3000, preClearNoticeAt: null, pendingWakeAt: 5000 })
    expect(execFileSyncMock).not.toHaveBeenCalled()
  })

  it("a restarted agent does not inherit the old streak: no immediate persistent-block alert", async () => {
    const name = 'stopped-then-running'
    writeGateConfig(name, { enabled: true, thresholdTokens: 100, persistentBlockAlertMs: 60_000 })
    writeGateRunState(name, { firstBlockedAt: 1000, lastAlertAt: null, lastClearAt: null, preClearNoticeAt: null, pendingWakeAt: null })
    vi.mocked(agentPositivelyStopped).mockReturnValue(true)
    const t0 = Date.now()
    await checkAgent(name, t0)
    vi.mocked(agentPositivelyStopped).mockReturnValue(false)
    vi.mocked(readContextTokensFromProjectDir).mockReturnValue(500)
    vi.mocked(hasBlockingKanbanCardForAssignee).mockReturnValue(true)
    await checkAgent(name, t0 + 1000)
    const alerts = vi.mocked(createAgentMessage).mock.calls.filter((c) => String(c[2]).startsWith('[CONTEXT-RESTART-GATE-RIASZTAS]'))
    expect(alerts.length).toBe(0)
    expect(readGateRunState(name).firstBlockedAt).toBe(t0 + 1000)
  })

  it('an agent not positively stopped (running, remote, or a timed-out probe) keeps the fail-closed probes; the main agent is never skipped', async () => {
    for (const state of ['unreachable', 'running'] as const) {
      execFileSyncMock.mockClear()
      writeGateConfig(`probe-${state}`, { enabled: true, thresholdTokens: 100 })
      vi.mocked(agentPositivelyStopped).mockReturnValue(false)
      vi.mocked(readContextTokensFromProjectDir).mockReturnValue(0)
      await checkAgent(`probe-${state}`, Date.now())
      expect(execFileSyncMock.mock.calls.some((c) => c[1]?.[0] === 'list-panes')).toBe(true)
    }
    execFileSyncMock.mockClear()
    vi.mocked(agentPositivelyStopped).mockClear()
    vi.mocked(agentPositivelyStopped).mockReturnValue(true)
    writeGateConfig('testmain', { enabled: true, thresholdTokens: 100 })
    await checkAgent('testmain', Date.now())
    expect(vi.mocked(agentPositivelyStopped)).not.toHaveBeenCalled()
    expect(execFileSyncMock.mock.calls.some((c) => c[1]?.[0] === 'list-panes')).toBe(true)
  })

  it('KISPROGI 6584 K2: a running agent whose run-state probe timed out keeps its block streak (no reset)', async () => {
    const name = 'probe-timeout'
    writeGateConfig(name, { enabled: true, thresholdTokens: 100, persistentBlockAlertMs: 60 * 60_000 })
    writeGateRunState(name, { firstBlockedAt: 1000, lastAlertAt: null, lastClearAt: null, preClearNoticeAt: 4000, pendingWakeAt: null })
    vi.mocked(agentPositivelyStopped).mockReturnValue(false)  // what a timed-out list-sessions now gives
    vi.mocked(readContextTokensFromProjectDir).mockReturnValue(500)
    vi.mocked(hasBlockingKanbanCardForAssignee).mockReturnValue(true)
    await checkAgent(name, Date.now())
    expect(readGateRunState(name).firstBlockedAt).toBe(1000)
    expect(execFileSyncMock.mock.calls.some((c) => c[1]?.[0] === 'list-panes')).toBe(true)
  })

  it('the pane-pid probe pipes tmux stderr instead of inheriting it', async () => {
    writeGateConfig('stderr-check', { enabled: true, thresholdTokens: 100 })
    vi.mocked(readContextTokensFromProjectDir).mockReturnValue(0)
    await checkAgent('stderr-check', Date.now())
    const call = execFileSyncMock.mock.calls.find((c) => c[1]?.[0] === 'list-panes') as unknown as [string, string[], { stdio?: unknown }]
    expect(call?.[2]?.stdio).toEqual(['ignore', 'pipe', 'pipe'])
  })
})

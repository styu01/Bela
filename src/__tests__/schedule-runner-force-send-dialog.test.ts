import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { ScheduledTask } from '../web/scheduled-tasks-io.js'

// BELA 6297 (B) / PROGI 6294: a forceSend task skips the busy check and the idle wait, so nothing stood between
// its prompt + Enter and a resume ("Resume this conversation?") or model-consent dialog on the pane: the Enter
// could start a new conversation or switch the model. Now the forceSend gate answers such a dialog through
// the shared, session-locked handlers and defers ('dialog', retry queue) when it is still up; a dialog that
// comes up after the gate and that the handlers cannot answer makes sendPromptToSession type nothing
// ('aborted-dialog'), which the runner also defers instead of recording 'fired'.

const m = vi.hoisted(() => ({
  frames: [] as (string | null)[],
  captures: 0,
  sendPrompt: [] as unknown[][],
  sendPromptResults: [] as string[],
  resume: 0,
  consent: 0,
  retries: [] as unknown[][],
  runs: [] as unknown[][],
  clearParked: false,
  enters: 0,
  tasks: [] as ScheduledTask[],
}))

vi.mock('../logger.js', () => ({ logger: { info: vi.fn(), warn: vi.fn(), debug: vi.fn(), error: vi.fn() } }))
vi.mock('../web/atomic-write.js', () => ({ atomicWriteFileSync: vi.fn() }))
vi.mock('../db.js', () => ({
  appendTaskRun: (...a: unknown[]) => { m.runs.push(a) },
  listPendingTaskRetries: () => [],
  deletePendingTaskRetry: vi.fn(),
  updatePendingTaskRetry: vi.fn(() => true),
  insertPendingTaskRetryIfNew: (...a: unknown[]) => { m.retries.push(a) },
  markPendingTaskRetryAlert: vi.fn(() => false),
  clearPendingTaskRetryAlert: vi.fn(),
  markScheduledTaskKanbanWaiting: vi.fn(),
}))
vi.mock('../web/telegram.js', () => ({ sendTelegramMessage: vi.fn(async () => {}), sendTelegramPhoto: vi.fn(async () => {}) }))
vi.mock('../web/scheduled-tasks-io.js', () => ({ listScheduledTasks: () => m.tasks, SCHEDULED_TASKS_DIR: '/tmp/marveen-force-send-dialog-no-tasks-dir' }))
vi.mock('../web/agent-process.js', () => ({
  agentSessionName: (name: string) => `agent-${name}`,
  isAgentRunning: () => true,
  isSessionReadyForPrompt: async () => true,
  sendPromptToSession: async (...a: unknown[]) => { m.sendPrompt.push(a); return m.sendPromptResults.shift() ?? 'sent' },
  startAgentProcess: async () => ({ ok: true }),
  sessionExistsOnHost: () => true,
  capturePane: () => { const f = m.frames[Math.min(m.captures, m.frames.length - 1)]; m.captures++; return f ?? null },
  sendEnterToSession: () => { m.enters++ },
  clearStaleParkedInput: async () => m.clearParked,
  dismissResumeSummaryModalIfPresent: async () => { m.resume++; return 'answered' },
  dismissModelConsentDialogIfPresent: async () => { m.consent++; return 'answered' },
}))

const IDLE = '──────\n❯ \n──────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'
const RESUME = ['Resume this conversation?', '', 'This conversation has been inactive for 9h and is 182k tokens long.', '', '  Resume', '❯ Start a new conversation', '  Resume from summary', '', 'Enter to confirm · Esc to resume'].join('\n')
const CONSENT = [' Fable 5 now uses usage credits', '', ' Fable 5 runs on usage credits. You have $12.40 in credits.', '', ' ❯ Switch to Sonnet 5 and continue', '   Continue with Fable 5', '', ' Enter to confirm · Esc to cancel'].join('\n')

function task(over: Partial<ScheduledTask> = {}): ScheduledTask {
  return { name: 'fsd-task', description: 'fixture', prompt: 'Do the thing.', agent: 'fsdagent', enabled: true, createdAt: 0, type: 'task', schedule: '0 8 * * *', targetSession: 'fsd-session', forceSend: true, ...over }
}

async function runNow(t: ScheduledTask): Promise<string | undefined> {
  m.tasks = [t]
  vi.resetModules()
  const { runScheduledTaskNow } = await import('../web/schedule-runner.js')
  const r = await runScheduledTaskNow(t.name)
  return r.result
}

beforeEach(() => {
  vi.useFakeTimers()
  m.frames = []; m.captures = 0; m.sendPrompt = []; m.sendPromptResults = []; m.resume = 0; m.consent = 0
  m.retries = []; m.runs = []; m.clearParked = false; m.enters = 0
})
afterEach(() => { vi.useRealTimers() })

describe('forceSend gate: resume / model-consent dialog', () => {
  it('a resume dialog the handler answers: the task is typed afterwards (handlers ran BEFORE the prompt)', async () => {
    m.frames = [RESUME, IDLE]
    expect(await runNow(task())).toBe('fsdagent: fired')
    expect(m.resume).toBe(1)
    expect(m.consent).toBe(1)
    expect(m.sendPrompt.length).toBe(1)
  })

  it('a resume dialog still up after the handlers: deferred as dialog, nothing typed, not recorded as fired', async () => {
    m.frames = [RESUME, RESUME]
    expect(await runNow(task())).toBe('fsdagent: dialog')
    expect(m.sendPrompt).toEqual([])
    expect(m.retries.map((r) => r[3])).toEqual(['dialog'])
    expect(m.runs.filter((r) => r[2] === 'fired')).toEqual([])
  })

  it('a model-consent dialog still up: deferred as dialog, nothing typed', async () => {
    m.frames = [CONSENT, CONSENT]
    expect(await runNow(task())).toBe('fsdagent: dialog')
    expect(m.sendPrompt).toEqual([])
    expect(m.consent).toBe(1)
  })

  it('the pane cannot be read after the handlers: deferred (no typing on an unseen pane)', async () => {
    m.frames = [CONSENT, null]
    expect(await runNow(task())).toBe('fsdagent: dialog')
    expect(m.sendPrompt).toEqual([])
  })

  it('no dialog: the gate sends no key and calls no handler (sendPromptToSession keeps its own)', async () => {
    m.frames = [IDLE]
    expect(await runNow(task())).toBe('fsdagent: fired')
    expect(m.resume + m.consent).toBe(0)
    expect(m.sendPrompt.length).toBe(1)
  })

  it('a dialog that appears after the gate and cannot be answered: sendPromptToSession aborts, the runner defers', async () => {
    m.frames = [IDLE]
    m.sendPromptResults = ['aborted-dialog']
    expect(await runNow(task())).toBe('fsdagent: dialog')
    expect(m.retries.map((r) => r[3])).toEqual(['dialog'])
    expect(m.runs.filter((r) => r[2] === 'fired')).toEqual([])
  })

  it('every scheduler send asks for the abort on a dialog left up (forceSend and normal tasks)', async () => {
    m.frames = [IDLE]
    await runNow(task())
    await runNow(task({ forceSend: false }))
    expect(m.sendPrompt.map((a) => a[3])).toEqual([
      { waitForIdle: false, onDialogBlocked: 'abort' },
      { waitForIdle: true, onDialogBlocked: 'abort' },
    ])
  })

  it('the resubmit re-inject that meets a dialog queues a retry instead of losing the cleared prompt', async () => {
    const stuck = '──────\n❯ [Utemezett feladat: fsd-task] Do the thing.\n──────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'
    m.frames = [stuck]
    m.clearParked = true
    m.sendPromptResults = ['sent', 'aborted-dialog']
    expect(await runNow(task({ forceSend: false }))).toBe('fsdagent: fired')
    await vi.advanceTimersByTimeAsync(2000 + 3000 + 3000 + 100)
    expect(m.enters).toBe(2)                       // attempts 0 and 1: bare Enter
    expect(m.sendPrompt.length).toBe(2)            // attempt 2: the re-inject
    expect(m.sendPrompt[1][3]).toEqual({ waitForIdle: false, lockMode: 'held', onDialogBlocked: 'abort' })
    expect(m.retries.map((r) => r[3])).toEqual(['dialog'])
    await vi.advanceTimersByTimeAsync(20_000)
    expect(m.sendPrompt.length).toBe(2)            // the chain stopped
  })
})

describe('contracts', () => {
  const SR = readFileSync(join(__dirname, '..', 'web', 'schedule-runner.ts'), 'utf-8')
  it("'dialog' is queued like 'first-run' (exempt from skipIfBusy) and has its own alert text", () => {
    expect(SR).toMatch(/\} else if \(result === 'dialog'\) \{[\s\S]{0,400}?insertPendingTaskRetryIfNew\(task\.name, agentName, now, 'dialog'\)/)
    expect(SR).toMatch(/result === 'first-run' \|\| result === 'dialog'\) \{/)
    expect(SR).toMatch(/const dialogStuck = view\.lastReason === 'dialog'/)
  })
  it('the forceSend dialog gate sits before the bypass log line', () => {
    const gate = SR.indexOf('if (pane != null && forceSendDialogKind(pane) != null) {')
    expect(gate).toBeGreaterThan(SR.indexOf('const forceGate = pane != null ? detectsFirstRunGate(pane) : null'))
    expect(SR.indexOf("'forceSend=true, bypassing busy-state check'")).toBeGreaterThan(gate)
  })
})

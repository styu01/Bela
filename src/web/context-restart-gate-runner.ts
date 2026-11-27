import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'
import { logger } from '../logger.js'
import { MAIN_AGENT_ID, PROJECT_ROOT } from '../config.js'
import { listAllAgentNames, readAgentClaudeConfigDir } from './agent-config.js'
import { agentSessionName, capturePane, sendPromptToSession } from './agent-process.js'
import { detectPaneState } from '../pane-state.js'
import { detectsUsageLimit } from '../model-fallback.js'
import { readContextTokensFromProjectDir } from './active-model.js'
import { MAIN_CHANNELS_SESSION } from './main-agent.js'
import { withSessionSendLock } from './session-send-lock.js'
import { getHardGuardPhase } from './context-guard-runner.js'
import { readGateConfig, readGateRunState, writeGateRunState } from './context-restart-gate-store.js'
import { readFleetPauseState } from './usage-fleet-pause.js'
import {
  getDispatchedPendingStats,
  openInboundQuestionMessageId,
  hasBlockingKanbanCardForAssignee,
  createAgentMessage,
} from '../db.js'
import { archiveTranscriptBeforeContextRestart } from './context-restart-transcript-archive.js'
import {
  decideGate,
  decideWake,
  nextBlockClock,
  PRE_CLEAR_NOTICE_MS,
  WAKE_DELAY_MS,
  type GateInputs,
} from '../context-restart-gate.js'

// Fleet context-restart gate: proactively send /clear to an agent session
// before the context grows unwieldy, while holding the send lane and only
// when ALL gate conditions confirm no work is in flight.
//
// This complements the hard context-guard (context-guard-runner.ts), which
// acts at 90%/97% of the context window via hard process restarts. The soft
// gate acts much earlier (default 400k tokens) via /clear -- the SessionStart
// hooks (ledger-replay and taskstate-replay) then inject their normal context
// snapshot into the fresh session automatically. Separately, every actual
// gate clear exports a full cold-tier research archive immediately beforehand;
// that archive is never a continuity/replay input.
//
// The runner starts 3 minutes after dashboard boot (offset from context-guard's
// 4.5 min so the two sweeps do not fire simultaneously) and then sweeps on each
// agent's configured retryIntervalMs.

const INITIAL_DELAY_MS = 3 * 60_000   // 3 min

// The wake-nudge timing lives in context-restart-gate.ts (WAKE_DELAY_MS, WAKE_MAX_AGE_MS, decideWake) so the "is it
// due yet" rule stays pure and unit-tested; this module only performs the delivery (upstream 8953cacf, #976).

/**
 * The nudge text. Deliberately short: the substance is already in the session as SessionStart context (taskstate
 * replay, warm memory), and re-stating it here would only compete with it.
 *
 * LOCAL ADAPTATION: upstream's last sentence ("Rovid jelzest kuldj a sajat csatornadon...") is left out on purpose.
 * On the main agent it would put a Telegram message to the owner after every gate /clear (several a day), against
 * the owner's "no notice for routine things" rule; our context-guard resume prompt drops the same line (795798d9).
 */
export function gateWakePrompt(): string {
  return (
    '[CONTEXT-RESTART-GATE] Friss kontextussal indultal, mert a kapu ujrainditott (/clear). ' +
    'A visszatoltott blokkokban ott a beszelgetes vege, a napi naplo kivonata es -- ha volt futo munkad -- ' +
    'a TASK-FOLYTATAS a mar kesz lepesekkel es a kovetkezo akcioval. ' +
    'Olvasd be oket, ellenorizd a kanban tabladat es a hot memoriaidat, es FOLYTASD onnan ahol abbamaradt. ' +
    'Ne kezdd elolrol ami mar kesz, es ne delegald ujra amit mar atadtal. ' +
    'Ha nem volt futo munkad, az is teljes erteku allapot -- olyankor ne talalj ki magadnak feladatot, es ne kuldj errol uzenetet.'
  )
}

// One delivery attempt per agent at a time: the fast-path timer and a sweep must not both read the same debt and
// both type the nudge.
const wakeInFlight = new Set<string>()

/**
 * Deliver the wake nudge owed for an earlier /clear, if one is due. Retries across sweeps while the pane stays busy
 * (the debt survives in the state file), and gives up once the nudge is older than WAKE_MAX_AGE_MS -- by then the
 * session has either been woken by someone else or moved on. Exported for the regression test.
 */
export async function deliverPendingWake(name: string, session: string, nowMs: number): Promise<void> {
  if (wakeInFlight.has(name)) return
  wakeInFlight.add(name)
  try { await deliverPendingWakeOnce(name, session, nowMs) } finally { wakeInFlight.delete(name) }
}
async function deliverPendingWakeOnce(name: string, session: string, nowMs: number): Promise<void> {
  const due = readGateRunState(name).pendingWakeAt
  const action = decideWake(due, nowMs)
  if (action === 'none' || action === 'wait') return
  const clearDebt = (): void => {
    writeGateRunState(name, { ...readGateRunState(name), pendingWakeAt: null })
  }
  if (action === 'drop') {
    logger.info({ agent: name, ageMs: due === null ? null : nowMs - due }, 'context-restart-gate: wake nudge dropped (stale)')
    clearDebt()
    return
  }
  try {
    const outcome = await sendPromptToSession(session, gateWakePrompt(), null, { waitForIdle: true, onBusyTimeout: 'abort' })
    if (outcome === 'sent') {
      logger.info({ agent: name }, 'context-restart-gate: wake nudge delivered')
      clearDebt()
    } else {
      // Busy or lane-locked: keep the debt, retry on the next sweep.
      logger.info({ agent: name, outcome }, 'context-restart-gate: wake nudge deferred')
    }
  } catch (err) {
    logger.warn({ err, agent: name }, 'context-restart-gate: wake nudge failed (will retry)')
  }
}

// tmux path (matches other runners).
const TMUX = process.env.TMUX_BIN ?? '/usr/bin/tmux'

// Child-process measurement constants.
//
// Two-tier filter to separate "infrastructure" (MCP servers, telegram plugin,
// gmail runner) from "work" (Task-tool subagents, background Bash):
//
//   CHILD_MIN_AGE_S     -- lower bound: skip children younger than this to
//                          ignore transient exec() calls (<1s typical).
//
//   INFRA_AGE_DELTA_S   -- absolute delta upper bound: if a child's age is
//                          within this many seconds of the claude process age,
//                          it started near session boot and is infrastructure.
//                          Measured on this host: MCP servers start 1-3 seconds
//                          after claude (ratio 0.9996-0.9999). 60s is a generous
//                          but ABSOLUTE cap -- unlike a ratio, it does NOT loosen
//                          as session length grows. A Task-tool subagent running
//                          for 90 min in a 2h session would have a delta >> 60s.
//
// A child is treated as "possibly work" only when:
//   age >= CHILD_MIN_AGE_S  AND  age < claudeAgeS - INFRA_AGE_DELTA_S
//
// On ps failure (null age): fail-closed → treat as work.
const CHILD_MIN_AGE_S    = 3
const INFRA_AGE_DELTA_S  = 60   // seconds; 60s >> measured 3s max MCP startup delta

/**
 * Pure: true if a child process with the given age (seconds) should be treated
 * as infrastructure (MCP server, plugin runner) rather than in-flight work.
 * Exported for tests.
 *
 * Infrastructure is detected by absolute age delta from the claude process:
 *   - age < CHILD_MIN_AGE_S                          → transient exec() → infra
 *   - age >= claudeAgeS - INFRA_AGE_DELTA_S          → started within 60s of claude → infra
 *   - otherwise                                      → spawned during session → possibly work
 *
 * Using absolute delta (not ratio) is intentional: a ratio loosens with session
 * length, so a 90-min subagent in a 2h session would be misclassified. An
 * absolute 60s cap is generous yet immune to session age.
 */
export function isInfrastructureChild(childAgeS: number, claudeAgeS: number): boolean {
  if (childAgeS < CHILD_MIN_AGE_S) return true
  if (childAgeS >= claudeAgeS - INFRA_AGE_DELTA_S) return true
  return false
}

/**
 * The last inbound message the ledger drain surfaced for this agent, or null.
 * The drain (scripts/hooks/ledger-live-drain.py) writes the id into
 * store/.ledger-drain-<agent> when it puts a lost inbound in front of the
 * agent; the sanitisation here mirrors its _statefile().
 */
function drainSurfacedMessageId(ledgerAgentId: string): string | null {
  const safe = String(ledgerAgentId).replace(/[^A-Za-z0-9_-]/g, '_')
  try {
    const raw = readFileSync(join(PROJECT_ROOT, 'store', `.ledger-drain-${safe}`), 'utf-8').trim()
    return raw || null
  } catch { return null }
}

/**
 * Does an unanswered inbound still justify holding the gate shut?
 *
 * LEDGERACK905 (ported from upstream Szotasz/marveen 4fb9fbcbf, 2026-09-10).
 * Only until the agent has actually been SHOWN it. Before that, a /clear could
 * lose a question nobody has read; after it, the agent knows and the decision
 * to answer is its own -- and some messages rightly get no answer. Upstream's
 * measured case: a bare "ok" reply held the gate for eight hours at 630% of
 * the threshold, and the only way out would have been to wake the owner at
 * midnight with a reply nobody needed. Block until surfaced, no arbitrary
 * timer.
 *
 * Pure so the rule is testable without a database or a statefile.
 */
export function openQuestionBlocks(
  openMessageId: string | null,
  surfacedMessageId: string | null,
): boolean {
  if (openMessageId === null) return false      // nothing open
  if (openMessageId === '') return true         // open, but unidentifiable: hold
  return openMessageId !== surfacedMessageId    // held until the drain showed it
}

function sessionFor(name: string): string {
  return name === MAIN_AGENT_ID ? MAIN_CHANNELS_SESSION : agentSessionName(name)
}

function workingDirFor(name: string): string {
  if (name === MAIN_AGENT_ID) return PROJECT_ROOT
  return join(PROJECT_ROOT, 'agents', name)
}

/**
 * Claude Code config root for an agent, or undefined for the host default.
 *
 * Transcripts live under <config-root>/projects/<encoded-working-dir>/, and an
 * agent launched with CLAUDE_CONFIG_DIR keeps them somewhere other than
 * ~/.claude. Reading without this looks in the default root, finds nothing, and
 * the gate's contextTokens comes back null -- which is a fail-closed BLOCK, so
 * the symptom is a gate that never opens and never says why.
 */
function configDirFor(name: string): string | undefined {
  return name === MAIN_AGENT_ID ? undefined : (readAgentClaudeConfigDir(name) ?? undefined)
}

function agentIdForLedger(name: string): string {
  // The main agent's ledger key is the MAIN_AGENT_ID (e.g. "bigme"), same as
  // returned by ledger_lib.agent_id_from_cwd for the project root.
  return name
}

// ---- Pane helpers -----------------------------------------------------------

function capturePaneOrNull(session: string): string | null {
  try { return capturePane(session) } catch { return null }
}

// ---- Child-process detection ------------------------------------------------
//
// Session shapes measured on this host (see review #938 rounds 1+2):
//
//   Direct shape (most sessions):
//     pane_pid comm=claude → the pane IS the claude process.
//     bigme-channels, agent-eddie/ford/slarti/trillian/zaphod all have this.
//
//   Wrapper shape (worker sessions):
//     pane_pid comm=BASH → the pane is a shell; claude is a child.
//     bigme-worker (pane=2797), bigme-worker-fast (pane=2888) both have this.
//     If we skip the comm check and assume pane_pid=claude, we look at BASH's
//     children instead of claude's -- in the wrapper shape, claude itself is a
//     child of BASH with age ≈ BASH age (ratio ≈ 1.0 → classified as infra) and
//     all real work children of claude are invisible. This is a false-allow.
//
// Solution: read comm of pane_pid first; if not 'claude', find the child whose
// comm IS 'claude'. That is the process whose children we inspect.
//
// Two-tier age filter separates infrastructure from work (see constants above):
//   - age < CHILD_MIN_AGE_S                  → transient exec(), skip
//   - age >= claude_age - INFRA_AGE_DELTA_S  → started near boot = infra, skip
//   - otherwise                              → spawned during session = possibly work
//
// On ps failure for any PID: fail-closed (return null → decideGate blocks).

function getPanePid(session: string): number | null {
  try {
    const raw = execFileSync(TMUX, ['list-panes', '-t', session, '-F', '#{pane_pid}'],
      { timeout: 3000, encoding: 'utf-8' })
    const pid = parseInt(raw.split('\n')[0]?.trim() ?? '', 10)
    return Number.isFinite(pid) && pid > 0 ? pid : null
  } catch { return null }
}

function getChildPids(parentPid: number): number[] {
  try {
    const out = execFileSync('/bin/ps', ['--ppid', String(parentPid), '-o', 'pid='],
      { timeout: 3000, encoding: 'utf-8' })
    return out.split('\n')
      .map(l => parseInt(l.trim(), 10))
      .filter(n => Number.isFinite(n) && n > 0)
  } catch { return [] }
}

function getPidAgeSeconds(pid: number): number | null {
  try {
    const out = execFileSync('/bin/ps', ['-p', String(pid), '-o', 'etimes='],
      { timeout: 2000, encoding: 'utf-8' })
    const secs = parseInt(out.trim(), 10)
    return Number.isFinite(secs) ? secs : null
  } catch { return null }
}

function getChildArgsStr(pid: number): string | null {
  try {
    const out = execFileSync('/bin/ps', ['-p', String(pid), '-o', 'args='],
      { timeout: 2000, encoding: 'utf-8' })
    return out.trim() || null
  } catch { return null }
}

function getCommForPid(pid: number): string | null {
  try {
    const out = execFileSync('/bin/ps', ['-p', String(pid), '-o', 'comm='],
      { timeout: 2000, encoding: 'utf-8' })
    return out.trim() || null
  } catch { return null }
}

/**
 * Pure: given the pane process and its immediate children (comm already resolved),
 * returns the PID of the actual claude process in the tree.
 *
 * Direct shape (most sessions): pane comm=claude → pane IS claude.
 * Wrapper shape (worker sessions): pane comm=bash/BASH → claude is a child.
 * Returns null (fail-closed) if claude cannot be located in either position.
 *
 * Exported for tests.
 */
export function findClaudePidInTree(
  panePid: number,
  paneComm: string | null,
  children: ReadonlyArray<{ pid: number; comm: string | null }>,
): number | null {
  if (paneComm === null) return null
  if (paneComm === 'claude') return panePid
  for (const child of children) {
    if (child.comm === 'claude') return child.pid
  }
  return null
}

// ---- MCP process pattern helpers --------------------------------------------
//
// After a channel-mcp-reconnect.ts-triggered reconnect, the MCP server process
// restarts with a fresh (young) age. The age-based infra filter would classify
// it as possibly-work and block the gate for up to 2h. To avoid this, we also
// check whether a child process's args identify it as an MCP server by:
//
//   1. Matching known plugin cache paths (/plugins/cache/) -- all Claude Code
//      channel plugins run from the global plugin cache dir.
//
//   2. Matching package names extracted from the session's .mcp.json -- covers
//      npm/npx-started MCP servers (e.g. gmail-mcp-server@1.0.30).
//
// A process matching either criterion is infra even if young.
//
// Tokens like 'npx', 'npm', 'exec', 'bun', 'node' are skipped; only the
// package/script name that uniquely identifies the server is extracted.

const MCP_SKIP_ARGS = new Set([
  'npx', 'npm', 'exec', '-y', '--yes', 'bun', 'node', 'deno',
  'python3', 'python', 'ruby', 'uvx', 'run', 'start',
])

/**
 * Pure: extract identifying package names from an mcpServers config object.
 * Strips runtime launchers (npx, npm, bun, node...) and version suffixes.
 * Exported for tests.
 */
export function extractMcpPackageNames(mcpServers: Record<string, unknown>): string[] {
  const names: string[] = []
  for (const v of Object.values(mcpServers) as Record<string, unknown>[]) {
    const allArgs = [
      typeof v['command'] === 'string' ? v['command'] : '',
      ...((Array.isArray(v['args']) ? v['args'] : []) as string[]),
    ]
    for (const raw of allArgs) {
      if (!raw || typeof raw !== 'string') continue
      if (raw.startsWith('-')) continue
      // Take basename (strip absolute path prefix) then version suffix
      const base = raw.split('/').at(-1)?.replace(/@.*$/, '') ?? ''
      if (base.length < 5 || MCP_SKIP_ARGS.has(base.toLowerCase())) continue
      names.push(base)
    }
  }
  return names
}

function getMcpJsonPatterns(workingDir: string): string[] {
  try {
    const raw = JSON.parse(readFileSync(join(workingDir, '.mcp.json'), 'utf-8')) as Record<string, unknown>
    const servers = (raw['mcpServers'] ?? {}) as Record<string, unknown>
    return extractMcpPackageNames(servers)
  } catch { return [] }
}

/**
 * Pure: true if a child process (identified by its full args string) is an
 * MCP server and should be treated as infrastructure regardless of age.
 *
 * Two criteria (either is sufficient):
 *   - args contains '/plugins/cache/' → channel plugin (telegram, slack, etc.)
 *   - args contains a package name from mcpPatterns → .mcp.json MCP server
 *
 * Exported for tests.
 */
export function isMcpProcess(childArgs: string, mcpPatterns: string[]): boolean {
  if (childArgs.includes('/plugins/cache/')) return true
  return mcpPatterns.some(p => childArgs.includes(p))
}

/**
 * Returns true if the session's claude process has live children that look
 * like in-flight work (Task-tool subagents, background Bash), false if only
 * infrastructure children are found, null if the check cannot be completed
 * (fail-closed → decideGate blocks).
 */
function hasLiveChildProcesses(session: string, mcpPatterns: string[]): boolean | null {
  const panePid = getPanePid(session)
  if (panePid === null) return null

  // Locate the actual claude process -- may be the pane itself (direct shape)
  // or a child of the pane shell (wrapper shape, e.g. bigme-worker).
  const paneComm = getCommForPid(panePid)
  const panePidChildren = getChildPids(panePid)
  const claudePid = findClaudePidInTree(
    panePid,
    paneComm,
    panePidChildren.map(pid => ({ pid, comm: getCommForPid(pid) })),
  )
  if (claudePid === null) return null   // can't locate claude -- fail-closed

  const claudeAge = getPidAgeSeconds(claudePid)
  if (claudeAge === null) return null

  // Inspect claude's own children (MCP servers + possible Task-tool subagents).
  const claudeChildren = claudePid === panePid ? panePidChildren : getChildPids(claudePid)
  if (claudeChildren.length === 0) return false

  for (const pid of claudeChildren) {
    const age = getPidAgeSeconds(pid)
    if (age === null) return null   // fail-closed
    if (isInfrastructureChild(age, claudeAge)) continue   // age-based infra
    // Age alone is not enough: a reconnected MCP server starts fresh (young).
    // Check process args to identify MCP servers regardless of age.
    const args = getChildArgsStr(pid) ?? ''
    if (isMcpProcess(args, mcpPatterns)) continue   // pattern-based infra
    return true   // live work child
  }
  return false
}

// ---- Task-state helper ------------------------------------------------------

// A taskstate record survives restarts by design (taskstate-replay re-injects
// it). Its mere existence does not mean work is running NOW -- an open thread
// can live for days. Only a RECENTLY-WRITTEN record (written during the current
// work session, not hours/days ago by a prior one) is a reliable signal of
// actively in-flight work. 10 minutes covers a PreCompact or a proactive write
// at the start of a task; anything older than that is a stale thread.
export const TASKSTATE_FRESH_WINDOW_MS = 10 * 60 * 1000  // 10 min

function hasLiveTaskStateFile(name: string, nowMs: number): boolean {
  const path = join(PROJECT_ROOT, 'store', 'agent-taskstate', `${name}.json`)
  if (!existsSync(path)) return false
  try {
    const raw = JSON.parse(readFileSync(path, 'utf-8')) as Record<string, unknown>
    if (raw.consumed === true) return false
    const nextAction = String(raw.nextAction ?? '').trim()
    if (!nextAction) return false
    // Only block if the record was written recently (active work session).
    const ts = typeof raw.ts === 'number' ? raw.ts : 0
    return ts > 0 && nowMs - ts <= TASKSTATE_FRESH_WINDOW_MS
  } catch { return false }
}

/**
 * Collect args strings of live work children for diagnostic logging.
 * Called only on the alert path (infrequent) so the extra ps calls are fine.
 */
function getLiveWorkChildArgs(session: string, mcpPatterns: string[]): string[] {
  try {
    const panePid = getPanePid(session)
    if (panePid === null) return []
    const paneComm = getCommForPid(panePid)
    const panePidChildren = getChildPids(panePid)
    const claudePid = findClaudePidInTree(
      panePid, paneComm,
      panePidChildren.map(pid => ({ pid, comm: getCommForPid(pid) })),
    )
    if (claudePid === null) return []
    const claudeAge = getPidAgeSeconds(claudePid)
    if (claudeAge === null) return []
    const children = claudePid === panePid ? panePidChildren : getChildPids(claudePid)
    const result: string[] = []
    for (const pid of children) {
      const age = getPidAgeSeconds(pid)
      if (age === null || isInfrastructureChild(age, claudeAge)) continue
      const args = getChildArgsStr(pid) ?? ''
      if (isMcpProcess(args, mcpPatterns)) continue
      result.push(args || `PID ${pid}`)
    }
    return result
  } catch { return [] }
}

// ---- Gate check for one agent -----------------------------------------------

// Exported (not just called from scheduleSweep) so the wiring between the
// config store and the live sweep can be proven directly in tests: PUT
// .../context-restart-gate {enabled:true} is worthless unless something
// actually reads it and acts, and the only honest way to show that is to call
// the same function the running sweep calls, not just assert on the store.
export async function checkAgent(name: string, nowMs: number): Promise<void> {
  const cfg = readGateConfig(name)
  if (!cfg.enabled) return   // fast-exit without touching state

  const session = sessionFor(name)
  const workingDir = workingDirFor(name)

  // Settle any wake owed from an earlier /clear before measuring anything (upstream 8953cacf): the inline nudge
  // after the /clear can be lost to a dashboard restart, and this is what makes the debt durable. Runs before the
  // run-state read below, so the writes further down never resurrect a debt that was just paid.
  await deliverPendingWake(name, session, nowMs)

  // Gather inputs (all deterministic, no AI inference).
  const paneRaw = capturePaneOrNull(session)
  const paneState = paneRaw !== null ? detectPaneState(paneRaw) : null
  const paneUsageLimited = paneRaw !== null ? detectsUsageLimit(paneRaw) : false

  const hardGuardPhase = getHardGuardPhase(name)

  const contextTokens = readContextTokensFromProjectDir(workingDir, configDirFor(name))

  const dispatchedStats = (() => {
    try { return getDispatchedPendingStats(name, nowMs, cfg.staleCutoffMs) }
    catch { return null }
  })()

  const openQuestion = (() => {
    try {
      const ledgerId = agentIdForLedger(name)
      return openQuestionBlocks(openInboundQuestionMessageId(ledgerId),
                                drainSurfacedMessageId(ledgerId))
    }
    catch { return false }
  })()

  const liveTaskState = hasLiveTaskStateFile(name, nowMs)

  // Keep the shared 90%+ usage state in decision telemetry. It is deliberately
  // not a gate condition: the soft 400k context threshold must still clear an
  // otherwise-idle session during a long-lived fleet usage pause.
  const fleetPause = readFleetPauseState()

  // An assigned card is work even when the pane happens to look idle. Query
  // failures are deliberately converted to "open": a missing safety signal
  // must never become permission to /clear.
  const blockingKanbanCard = (() => {
    try { return hasBlockingKanbanCardForAssignee(name) }
    catch { return null }
  })()

  const mcpPatterns = getMcpJsonPatterns(workingDir)
  const childProcesses = (() => {
    try { return hasLiveChildProcesses(session, mcpPatterns) }
    catch { return null }
  })()

  // If the DB query for dispatched stats failed, fail-closed by treating it as
  // if there are pending messages (count=1). Log the failure.
  if (dispatchedStats === null) {
    logger.warn({ agent: name }, 'context-restart-gate: dispatched-stats query failed (fail-closed)')
  }
  if (blockingKanbanCard === null) {
    logger.warn({ agent: name }, 'context-restart-gate: blocking-kanban-card query failed (fail-closed)')
  }

  const inputs: GateInputs = {
    nowMs,
    contextTokens,
    paneState,
    paneUsageLimited,
    hardGuardPhase,
    pendingOutboundCount:   dispatchedStats === null ? 1 : dispatchedStats.count,
    hasStaleOutbound:       dispatchedStats?.hasStale ?? false,
    hasChildProcesses:      childProcesses,
    hasOpenQuestion:        openQuestion,
    hasLiveTaskState:       liveTaskState,
    hasBlockingKanbanCard:  blockingKanbanCard === null ? true : blockingKanbanCard,
    fleetUsagePaused:       fleetPause.paused,
  }

  const runState = readGateRunState(name)
  const decision = decideGate(inputs, cfg, runState.firstBlockedAt)

  logger.debug({ agent: name, action: decision.action, reason: decision.reason,
    contextTokens, paneState, hardGuardPhase, fleetUsagePaused: fleetPause.paused }, 'context-restart-gate: decision')

  // A notice is a bounded courtesy, not a new proof of safety. It is sent only
  // while every current fail-closed signal allows a future clear. If a signal
  // turns blocking during the ten-minute window, the notice is discarded so a
  // later clear gets a fresh, truthful ten-minute warning instead of relying
  // on an old one.
  let effectiveState = runState
  if (decision.action !== 'allow' && effectiveState.preClearNoticeAt !== null) {
    effectiveState = { ...effectiveState, preClearNoticeAt: null }
    writeGateRunState(name, effectiveState)
  }

  switch (decision.action) {
    case 'allow': {
      if (effectiveState.preClearNoticeAt === null) {
        try {
          createAgentMessage(
            MAIN_AGENT_ID,
            name,
            `[CONTEXT-RESTART-GATE] ${Math.round(PRE_CLEAR_NOTICE_MS / 60_000)} percen belül automatikus context-clear várható. Ha van folyamatban levő munkád, írj magadnak rövid állapotjegyzetet a warm memóriába MOST. A clear csak akkor történik meg, ha addig is minden biztonsági kapufeltétel tiszta marad.`,
            'context-restart-gate pre-clear notice',
          )
          writeGateRunState(name, { ...effectiveState, preClearNoticeAt: nowMs })
          logger.info({ agent: name, waitMs: PRE_CLEAR_NOTICE_MS },
            'context-restart-gate: pre-clear notice sent; waiting before re-check')
        } catch (err) {
          // No notice => no clear. The next sweep can retry the bounded notice
          // instead of silently treating a failed message write as delivered.
          logger.warn({ err, agent: name }, 'context-restart-gate: pre-clear notice failed; clear deferred')
        }
        break
      }

      const noticeAgeMs = nowMs - effectiveState.preClearNoticeAt
      if (noticeAgeMs < PRE_CLEAR_NOTICE_MS) {
        logger.debug({ agent: name, noticeAgeMs, waitMs: PRE_CLEAR_NOTICE_MS },
          'context-restart-gate: pre-clear notice countdown active')
        break
      }

      if (decision.noteStaleOutbound) {
        logger.info({ agent: name },
          'context-restart-gate: opening despite stale dispatched messages (beyond staleCutoffMs)')
      }
      // Hold the send lane across BOTH the mandatory archive and /clear. If
      // archive creation fails, the callback throws before either send-keys
      // call, so no actual clear can occur without its cold-tier transcript.
      // This archive is an emergency/research record only; normal daily
      // continuity remains the independent ledger/task-state SessionStart
      // replay and warm handoff mechanisms.
      try {
        await withSessionSendLock(session, null, 'deliver', async () => {
          const archive = archiveTranscriptBeforeContextRestart({
            agent: name,
            workingDir,
            configDir: configDirFor(name),
            nowMs,
          })
          logger.info({ agent: name, archive: archive.path, source: archive.sourcePath },
            'context-restart-gate: cold-tier transcript archive created before /clear')
          execFileSync(TMUX, ['send-keys', '-t', session, '-l', '/clear'], { timeout: 5000 })
          execFileSync(TMUX, ['send-keys', '-t', session, 'Enter'], { timeout: 5000 })
        })
        logger.info({ agent: name, contextTokens }, 'context-restart-gate: /clear sent')
        writeGateRunState(name, {
          ...effectiveState,
          firstBlockedAt: null,
          lastClearAt: nowMs,
          preClearNoticeAt: null,
          // Owe the fresh session a wake nudge (see WAKE_DELAY_MS), unless switched off for this agent.
          pendingWakeAt: cfg.wakeAfterClear ? nowMs : null,
        })
        // Fast path: nudge in ~25 s rather than at the next sweep. The persisted debt above is the fallback.
        // LOCAL ADAPTATION: upstream awaited a 25 s sleep inside the sweep; a timer (unref'd, so it never holds the
        // process) keeps the sweep moving.
        if (cfg.wakeAfterClear) {
          const wakeTimer = setTimeout(() => {
            deliverPendingWake(name, session, Date.now()).catch((err) => logger.warn({ err, agent: name }, 'context-restart-gate: wake fast path failed'))
          }, WAKE_DELAY_MS)
          wakeTimer.unref?.()
        }
      } catch (err) {
        // The old warning may now be arbitrarily stale. Require a new bounded
        // notice before any retry, whether the archive or send-keys failed.
        writeGateRunState(name, { ...effectiveState, preClearNoticeAt: null })
        logger.warn({ err, agent: name }, 'context-restart-gate: archive or /clear send failed; clear not confirmed')
      }
      break
    }

    case 'block-alert': {
      // Continuous blocking for >= persistentBlockAlertMs. Alert bigme, but
      // only once per persistentBlockAlertMs to avoid message spam.
      const alertDue = effectiveState.lastAlertAt === null
        || nowMs - effectiveState.lastAlertAt >= cfg.persistentBlockAlertMs
      if (alertDue) {
        const blockedSinceMin = effectiveState.firstBlockedAt !== null
          ? Math.round((nowMs - effectiveState.firstBlockedAt) / 60_000)
          : '?'
        try {
          // When the block reason is child processes, include their args so
          // bigme can identify the culprit at a glance (no post-hoc investigation).
          let childInfo = ''
          if (decision.reason.startsWith('live-child-processes')) {
            const workArgs = getLiveWorkChildArgs(session, mcpPatterns)
            if (workArgs.length > 0) {
              childInfo = ` Blokkolo gyerekfolyamatok: ${workArgs.slice(0, 5).join('; ')}`
            }
          }
          createAgentMessage(
            name,
            MAIN_AGENT_ID,
            `[CONTEXT-RESTART-GATE] A(z) "${name}" agens kapuja ${blockedSinceMin} perce folyamatosan blokkolt. Ok: ${decision.reason}.${childInfo} A(z) ${Math.round(cfg.thresholdTokens / 1000)}k tokenes kuszob ele ert, de a kapu nem enged -- ellenorizd hogy nincs-e elakadt munka.`,
            'context-restart-gate persistent-block alert',
          )
          logger.warn({ agent: name, reason: decision.reason, blockedSinceMin },
            'context-restart-gate: persistent-block alert sent')
          writeGateRunState(name, {
            ...effectiveState,
            firstBlockedAt: effectiveState.firstBlockedAt ?? nowMs,
            lastAlertAt: nowMs,
          })
        } catch (alertErr) {
          logger.warn({ alertErr, agent: name }, 'context-restart-gate: alert message failed')
        }
      }
      break
    }

    case 'block': {
      // Advance (or clear) the blocking-streak clock; see nextBlockClock.
      const firstBlockedAt = nextBlockClock(
        effectiveState.firstBlockedAt, inputs.contextTokens, cfg.thresholdTokens, nowMs,
      )
      if (firstBlockedAt !== effectiveState.firstBlockedAt) {
        writeGateRunState(name, { ...effectiveState, firstBlockedAt, preClearNoticeAt: null })
      }
      break
    }
  }
}

// ---- Runner -----------------------------------------------------------------

const sweepTimers = new Map<string, NodeJS.Timeout>()

function scheduleSweep(name: string, delayMs: number): void {
  sweepTimers.set(name, setTimeout(async () => {
    const cfg = readGateConfig(name)
    if (!cfg.enabled) {
      sweepTimers.delete(name)
      return
    }
    try { await checkAgent(name, Date.now()) }
    catch (err) { logger.debug({ err, agent: name }, 'context-restart-gate: sweep error') }
    // Re-schedule using the agent's current retryIntervalMs (may have changed).
    scheduleSweep(name, readGateConfig(name).retryIntervalMs)
  }, delayMs))
}

export function startContextRestartGateRunner(): void {
  // Stagger each agent slightly so they don't all hit the DB simultaneously.
  //
  // listAllAgentNames(), NOT listAgentNames() (USAGETRACK904, 2026-09-04): a
  // dashboard-hidden technical worker (HIDDEN_AGENT_SENTINEL) is hidden from
  // the OPERATOR, not from the fleet's life support -- the identical mistake
  // context-guard-runner.ts's guardSweepAgentNames() was already fixed for
  // after the 2026-08-04 agents/heartbeat incident (see that function's own
  // comment). No agent carries the sentinel today, so this was not yet live-
  // broken, but the two sibling runners protecting the SAME class of session
  // must agree on who they sweep, or the next agent hidden from the dashboard
  // silently loses this gate exactly the way heartbeat lost the hard guard.
  const agents = [MAIN_AGENT_ID, ...listAllAgentNames()]
  const seen = new Set<string>()
  let offset = 0
  for (const name of agents) {
    if (seen.has(name)) continue
    seen.add(name)
    const cfg = readGateConfig(name)
    if (!cfg.enabled) {
      // Schedule a one-time check after the initial delay in case the config
      // changes at runtime; the per-agent sweep self-terminates when disabled.
      scheduleSweep(name, INITIAL_DELAY_MS + offset)
    } else {
      scheduleSweep(name, INITIAL_DELAY_MS + offset)
    }
    offset += 2_000  // 2s stagger per agent
  }
}

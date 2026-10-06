// Reap orphaned channel-plugin pollers (bun/node processes that survived a
// tmux kill-session or are left over from a previous agent crash).
//
// The bug we close (2026-06-01 incident, channel-disconnect roundtrip):
//   - stopAgentProcess used `pkill -f TELEGRAM_STATE_DIR=<dir>`, but the
//     plugin process argv is just `bun run --cwd .../telegram/0.0.6 start`
//     - the env var lives in /proc-equivalent environment storage, not argv,
//     so `pkill -f` never matches and the orphan keeps polling getUpdates
//     with the same bot token until SIGTERM by hand.
//   - startAgentProcess only killed the tmux session pre-launch and did NOT
//     reap orphans at all. After a restart the old poller raced the new one
//     and Telegram returned 409 Conflict in a loop.
//   - The plugin writes bot.pid in <chanDir>/bot.pid. That works on the
//     happy path but if a new poller crashed and a later one overwrote the
//     file, the older orphan is no longer in bot.pid - we miss it.
//
// Strategy: combine two identifiers.
//   1. bot.pid (cheap, works for the supervised process).
//   2. `ps eww -e` scan for the *_STATE_DIR=<chanDir> env-var match. This
//      catches orphans whose pid is no longer in bot.pid - any process that
//      was started against this channel state dir is in scope, regardless
//      of how its argv was rendered. macOS BSD ps emits each process's full
//      environment when invoked with `e`; we grep that.

import { execFileSync, execSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { ChannelProviderType } from '../channel-provider.js'
import { channelStateDir } from '../channel-provider.js'
import { logger } from '../logger.js'

const STATE_ENV_VAR: Record<ChannelProviderType, string> = {
  telegram: 'TELEGRAM_STATE_DIR',
  slack: 'SLACK_STATE_DIR',
  discord: 'DISCORD_STATE_DIR',
  googlechat: 'GOOGLECHAT_STATE_DIR',
  teams: 'TEAMS_STATE_DIR',
}

// Parse `ps eww -e` output and return every PID whose process environment
// contains `<envVar>=<value>`. Exported for testability.
//
// `ps eww -e` rows on macOS look like:
//   90798 s000  S+   0:00.01 bun run --cwd ... HOME=/Users/... TELEGRAM_STATE_DIR=/path... ...
// The match must be precise: substring `TELEGRAM_STATE_DIR=/path` against
// `TELEGRAM_STATE_DIR=/path-elsewhere` is acceptable because the value is an
// absolute path, but we still anchor on the env-var literal to avoid
// matching a row that just *mentions* the path string in its argv.
export function parsePollerPidsFromPs(
  psOutput: string,
  envVar: string,
  value: string,
): number[] {
  const needle = `${envVar}=${value}`
  const out: number[] = []
  for (const line of psOutput.split('\n')) {
    if (!line.includes(needle)) continue
    const m = line.match(/^\s*(\d+)\s/)
    if (!m) continue
    const pid = parseInt(m[1]!, 10)
    if (pid > 1) out.push(pid)
  }
  return out
}

function listPollerPidsByStateDir(envVar: string, chanDir: string): number[] {
  try {
    const out = execSync('/bin/ps eww -e', { timeout: 5000, encoding: 'utf-8', maxBuffer: 8 * 1024 * 1024 })
    return parsePollerPidsFromPs(out, envVar, chanDir)
  } catch (err) {
    logger.warn({ err, chanDir }, 'channel-poller-reap: ps scan failed')
    return []
  }
}

function readBotPid(chanDir: string): number | null {
  const path = join(chanDir, 'bot.pid')
  if (!existsSync(path)) return null
  try {
    const pid = parseInt(readFileSync(path, 'utf-8').trim(), 10)
    return Number.isFinite(pid) && pid > 1 ? pid : null
  } catch {
    return null
  }
}

export interface ReapResult {
  reaped: number[]
  source: { fromBotPid: number | null; fromEnvScan: number[] }
  // Candidates that belong to a live tmux pane must be left to the caller's
  // imminent respawn-pane. Killing the pane leader here can collapse the pane
  // before that safe replacement happens.
  skippedLivePane: number[]
  // A tmux server can inherit the channel state-dir environment. It is never
  // a poller candidate we may kill: doing so takes every shared-tmux session
  // down with it.
  skippedTmuxServer?: boolean
}

// ---------------------------------------------------------------------------
// Down-verdict forensics (2026-07-14).
//
// The watchdog restarted agents ~10x/day on a "channel plugin down" verdict,
// and the restart DESTROYS the evidence: the poller is reaped, the session is
// respawned, and the post-mortem log says only "down -- auto-restarting". So
// the interesting question -- did the poller really die, or did the tree-walk
// lose a live one? -- could not be answered from the logs at all.
//
// This captures the state at the MOMENT the verdict is formed, before anything
// is torn down: is a poller process for this chanDir alive at all, is it in the
// claude process tree, and does bot.pid still point at it. One WARN per
// down-spell, so it costs a ps per spell, not per sweep.

export interface PollerEvidenceRow {
  pid: number
  ppid: number
  // Whether claudePid is an ancestor of this pid. FALSE with a live pid is the
  // interesting case: the poller exists but hangs outside the tree the liveness
  // probe walks (reparented / attached to a previous claude).
  inClaudeTree: boolean
}

export interface PollerEvidence {
  botPid: number | null
  botPidAlive: boolean
  // Pollers found by env-var scan, i.e. every process started against this
  // channel state dir regardless of parentage.
  envScanPids: number[]
  rows: PollerEvidenceRow[]
  // The verdict this evidence supports, spelled out so the log line is readable
  // without re-deriving it:
  //   'no-poller'      -> nothing alive: the plugin really did die.
  //   'orphaned'       -> a live poller exists but is NOT under claude.
  //   'in-tree'        -> a live poller IS under claude: the probe was WRONG.
  interpretation: 'no-poller' | 'orphaned' | 'in-tree'
}

// Pure core: exported for tests (no ps, no fs).
export function buildPollerEvidence(
  procs: ProcRow[],
  botPid: number | null,
  envScanPids: number[],
  claudePid: number,
): PollerEvidence {
  const byPid = new Map<number, ProcRow>()
  for (const p of procs) byPid.set(p.pid, p)

  const isUnderClaude = (pid: number): boolean => {
    let cur = pid
    const seen = new Set<number>()
    for (let hops = 0; hops < 8; hops++) {
      if (cur === claudePid) return true
      if (seen.has(cur)) break
      seen.add(cur)
      const next = byPid.get(cur)?.ppid
      if (next === undefined || next === cur || next <= 1) break
      cur = next
    }
    return false
  }

  const candidates = new Set<number>(envScanPids)
  if (botPid != null) candidates.add(botPid)

  const rows: PollerEvidenceRow[] = []
  for (const pid of candidates) {
    const row = byPid.get(pid)
    if (!row) continue // not in the ps snapshot -> dead
    rows.push({ pid, ppid: row.ppid, inClaudeTree: isUnderClaude(pid) })
  }

  const interpretation: PollerEvidence['interpretation'] = rows.length === 0
    ? 'no-poller'
    : rows.some((r) => r.inClaudeTree) ? 'in-tree' : 'orphaned'

  return {
    botPid,
    botPidAlive: botPid != null && byPid.has(botPid),
    envScanPids,
    rows,
    interpretation,
  }
}

// Collect the evidence for one agent. Call this ONCE per down-spell, at the
// first down observation, BEFORE any teardown.
export function collectPollerEvidence(
  provider: ChannelProviderType,
  agentDirPath: string,
  claudePid: number,
): PollerEvidence {
  const chanDir = channelStateDir(provider, agentDirPath)
  return buildPollerEvidence(
    snapshotProcs(),
    readBotPid(chanDir),
    listPollerPidsByStateDir(STATE_ENV_VAR[provider], chanDir),
    claudePid,
  )
}

/**
 * Reap every channel-plugin poller process associated with this agent.
 * Combines bot.pid (cheap, supervised pid) with a `ps eww -e` env-var scan
 * (catches orphans whose pid is no longer in bot.pid). SIGTERM first; after
 * a short grace period, SIGKILL any survivor. Safe to call multiple times
 * (process.kill on a missing pid is caught).
 */
export function reapChannelOrphans(
  provider: ChannelProviderType,
  agentDirPath: string,
  opts: { tmuxPath?: string } = {},
): ReapResult {
  const chanDir = channelStateDir(provider, agentDirPath)
  const envVar = STATE_ENV_VAR[provider]

  const fromBotPid = readBotPid(chanDir)
  const fromEnvScan = listPollerPidsByStateDir(envVar, chanDir)

  // Deduplicate while preserving order so the bot.pid path is logged first.
  const candidates: number[] = []
  const seen = new Set<number>()
  for (const pid of [fromBotPid, ...fromEnvScan]) {
    if (pid && !seen.has(pid)) {
      seen.add(pid)
      candidates.push(pid)
    }
  }

  // Fail closed when tmux cannot identify live pane leaders. An empty result
  // means the query failed, not that a running tmux server has no panes.
  const tmuxPath = opts.tmuxPath ?? 'tmux'
  const liveResult = livePanePids(tmuxPath)
  const live = liveResult.pids
  // No server is a known safe no-work result. A different empty lookup still
  // means our observation failed and must retain the fail-safe refusal.
  const noTmuxServer = liveResult.serverAbsent
  const liveQueryFailed = live.size === 0 && !noTmuxServer
  const skippedLivePane = liveQueryFailed ? [] : candidates.filter((pid) => live.has(pid))
  if (liveQueryFailed && candidates.length > 0) {
    logger.warn({ provider, chanDir, candidates },
      'channel-poller-reap: could not resolve live tmux panes, refusing to reap (fail-safe)')
  }

  // The shared tmux server itself can carry the main session's state-dir var
  // when it was created by the channels launcher. Resolve it separately and
  // fail safe if this cannot be measured.
  const serverPid = liveQueryFailed || noTmuxServer ? null : tmuxServerPid(tmuxPath)
  const serverQueryFailed = !liveQueryFailed && !noTmuxServer && serverPid === null
  if (serverQueryFailed && candidates.length > 0) {
    logger.warn({ provider, chanDir, candidates },
      'channel-poller-reap: could not resolve the tmux server pid, refusing to reap (fail-safe)')
  }
  const skippedTmuxServer = serverPid !== null && candidates.includes(serverPid) && !live.has(serverPid)
  const all = noTmuxServer || liveQueryFailed || serverQueryFailed
    ? []
    : candidates.filter((pid) => !live.has(pid) && pid !== serverPid)
  if (skippedTmuxServer) {
    logger.warn({ provider, chanDir, serverPid, fromBotPid, fromEnvScan },
      'channel-poller-reap: candidate IS the tmux server, sparing it')
  }

  // SIGTERM, give bun/node ~300ms to flush, then SIGKILL stragglers.
  for (const pid of all) {
    try { process.kill(pid, 'SIGTERM') } catch { /* already gone */ }
  }
  if (all.length > 0) {
    try { execFileSync('/bin/sleep', ['0.3'], { timeout: 2000 }) } catch { /* ignore */ }
    for (const pid of all) {
      try { process.kill(pid, 0) /* probe */; process.kill(pid, 'SIGKILL') } catch { /* gone */ }
    }
  }

  if (all.length > 0) {
    logger.info({ provider, chanDir, reaped: all, fromBotPid, fromEnvScan }, 'channel-poller-reap: orphans killed')
  }
  if (skippedLivePane.length > 0) {
    logger.warn({ provider, chanDir, skippedLivePane, fromBotPid, fromEnvScan },
      'channel-poller-reap: candidate IS a live pane leader, sparing it')
  }
  return { reaped: all, source: { fromBotPid, fromEnvScan }, skippedLivePane, skippedTmuxServer }
}

// ---------------------------------------------------------------------------
// Detached channel CLAUDE reaper (the parent-process leak, 2026-06-03).
//
// reapChannelOrphans (above) kills bun/node POLLERS by env-var scan + bot.pid.
// That works for sub-agents (their claude+poller carry TELEGRAM_STATE_DIR=<dir>)
// but MISSES the main channels session entirely: channels.sh launches the main
// `claude --channels` with NO *_STATE_DIR export (the plugin uses its default
// dir), so neither the main claude nor its poller match the env needle, and the
// plugin never writes bot.pid. When a --continue respawn (channel-monitor
// respawn-pane / agent-process start) fails to tear down the prior claude, the
// detached claude survives -- reparented to the tmux server -- and keeps a bun
// poller hitting getUpdates on the SHARED bot token. 5 such orphans accumulated
// over 13 days, each 409-racing the live poller (token churn + a self-feeding
// agent thrash-restart loop). See project_channels_continue_respawn_leak.
//
// Identification is by tmux-pane attribution, NOT env/argv heuristics (cmdline
// alone cannot tell a live agent claude from a detached one -- see
// feedback_verify_session_before_kill): a `claude --channels` process is an
// orphan iff neither its pid nor any ancestor pid is a LIVE tmux pane pid.
//   - main session: tmux runs claude as the pane leader, so claudePid == panePid.
//   - sub-agents:   tmux runs `sh -c "...claude..."`, so the pane pid is the sh
//                   and claude is its child -> ancestor walk catches it.
// The tmux SERVER process is excluded up front: its argv embeds the full
// `new-session ... claude --channels ...` string, a false positive, but argv[0]
// is tmux, not claude.

export interface ProcRow { pid: number; ppid: number; command: string }

// argv[0] basename === 'claude' (the binary), so the tmux server row whose argv
// merely *contains* the claude command string is excluded.
function isClaudeBinary(command: string): boolean {
  const argv0 = command.trim().split(/\s+/, 1)[0] ?? ''
  const base = argv0.split('/').pop() ?? ''
  return base === 'claude'
}

/**
 * Pure: return the pids of `claude --channels` processes that are NOT attached
 * to any live tmux pane (orphans). `livePanePids` is the set of pane pids from
 * `tmux list-panes -a`. `channelNeedle` optionally restricts to one plugin
 * (e.g. 'plugin:telegram@...'); when omitted, every channel plugin is in scope.
 * Exported for testability.
 */
export function findOrphanChannelClaudes(
  procs: ProcRow[],
  livePanePids: Set<number>,
  channelNeedle?: string,
): number[] {
  const byPid = new Map<number, ProcRow>()
  for (const p of procs) byPid.set(p.pid, p)

  const attachedToLivePane = (pid: number): boolean => {
    let cur = pid
    const seen = new Set<number>()
    for (let hops = 0; hops < 8; hops++) {
      if (livePanePids.has(cur)) return true
      if (seen.has(cur)) break
      seen.add(cur)
      const next = byPid.get(cur)?.ppid
      if (next === undefined || next === cur || next <= 1) break
      cur = next
    }
    return false
  }

  const orphans: number[] = []
  for (const p of procs) {
    if (!p.command.includes('--channels')) continue
    if (!isClaudeBinary(p.command)) continue
    if (channelNeedle && !p.command.includes(channelNeedle)) continue
    if (attachedToLivePane(p.pid)) continue
    orphans.push(p.pid)
  }
  return orphans
}

function snapshotProcs(): ProcRow[] {
  try {
    const out = execSync('/bin/ps -axww -o pid=,ppid=,command=', { timeout: 5000, encoding: 'utf-8', maxBuffer: 8 * 1024 * 1024 })
    const rows: ProcRow[] = []
    for (const line of out.split('\n')) {
      const m = line.match(/^\s*(\d+)\s+(\d+)\s+(.*)$/)
      if (!m) continue
      rows.push({ pid: parseInt(m[1]!, 10), ppid: parseInt(m[2]!, 10), command: m[3]! })
    }
    return rows
  } catch (err) {
    logger.warn({ err }, 'channel-poller-reap: ps -axww snapshot failed')
    return []
  }
}

function tmuxServerPid(tmuxPath: string): number | null {
  try {
    const out = execSync(`${tmuxPath} display-message -p '#{pid}'`, { timeout: 5000, encoding: 'utf-8' })
    const n = parseInt(out.trim().split('\n')[0] ?? '', 10)
    return Number.isFinite(n) && n > 1 ? n : null
  } catch (err) {
    logger.warn({ err }, 'channel-poller-reap: tmux display-message failed')
    return null
  }
}

interface LivePaneLookup {
  pids: Set<number>
  serverAbsent: boolean
}

/**
 * tmux exits non-zero with one of these diagnostics when the final session has
 * gone away (including a missing or refused socket). That is a normal
 * "nothing to reap" condition, not a degraded observation: there cannot be a
 * live pane to protect while no server exists.
 */
export function isTmuxServerAbsentError(err: unknown): boolean {
  return /no server running|error connecting to .*\((No such file or directory|Connection refused)\)/i.test(String(err))
}

function livePanePids(tmuxPath: string): LivePaneLookup {
  try {
    const out = execSync(`${tmuxPath} list-panes -a -F '#{pane_pid}'`, { timeout: 5000, encoding: 'utf-8' })
    const s = new Set<number>()
    for (const line of out.split('\n')) {
      const n = parseInt(line.trim(), 10)
      if (Number.isFinite(n) && n > 1) s.add(n)
    }
    return { pids: s, serverAbsent: false }
  } catch (err) {
    if (isTmuxServerAbsentError(err)) {
      return { pids: new Set(), serverAbsent: true }
    }
    logger.warn({ err }, 'channel-poller-reap: tmux list-panes failed')
    return { pids: new Set(), serverAbsent: false }
  }
}

function killBunChildren(claudePid: number): void {
  try {
    const out = execSync(`/usr/bin/pgrep -P ${claudePid} bun`, { timeout: 3000, encoding: 'utf-8' })
    for (const line of out.split('\n')) {
      const pid = parseInt(line.trim(), 10)
      if (Number.isFinite(pid) && pid > 1) {
        try { process.kill(pid, 'SIGTERM') } catch { /* gone */ }
      }
    }
  } catch { /* no bun children (pgrep exits 1) */ }
}

/**
 * Reap detached `claude --channels` orphans (parent-process leak). SAFE to call
 * before any (re)spawn: it spares every claude attached to a live tmux pane, so
 * it never kills the active session or a live sibling agent -- only truly
 * detached leftovers. Kills each orphan's bun poller children first, then the
 * claude (SIGTERM, ~300ms grace, SIGKILL stragglers). Returns reaped pids.
 *
 * tmuxPath defaults to a bare `tmux` (resolved on PATH); callers that already
 * hold an absolute path should pass it.
 */
export function reapDetachedChannelClaudes(opts: { channelNeedle?: string; tmuxPath?: string } = {}): number[] {
  const tmuxPath = opts.tmuxPath ?? 'tmux'
  const procs = snapshotProcs()
  const liveResult = livePanePids(tmuxPath)
  const live = liveResult.pids
  // No live panes resolved (tmux query failed) -> refuse to reap: without the
  // live set we cannot tell orphans from the active session. Fail safe.
  if (live.size === 0) {
    if (liveResult.serverAbsent) {
      logger.debug('channel-poller-reap: tmux server absent, no detached-claude reap needed')
    } else {
      logger.warn('channel-poller-reap: no live panes resolved, skipping detached-claude reap (fail-safe)')
    }
    return []
  }
  const orphans = findOrphanChannelClaudes(procs, live, opts.channelNeedle)
  for (const pid of orphans) {
    killBunChildren(pid)
    try { process.kill(pid, 'SIGTERM') } catch { /* gone */ }
  }
  if (orphans.length > 0) {
    try { execFileSync('/bin/sleep', ['0.3'], { timeout: 2000 }) } catch { /* ignore */ }
    for (const pid of orphans) {
      try { process.kill(pid, 0); process.kill(pid, 'SIGKILL') } catch { /* gone */ }
    }
    logger.info({ reaped: orphans, channelNeedle: opts.channelNeedle ?? '(all)' }, 'channel-poller-reap: detached channel claudes killed')
  }
  return orphans
}

// ---------------------------------------------------------------------------
// Foreign main-token poller reaper.
//
// A plain local Claude process launched in the project directory may load the
// Telegram plugin from project settings. Unlike a channel-configured sub-agent,
// it has no provider state-dir override and can therefore poll the MAIN bot
// token. It must be distinguished from the legitimate main-session poller by
// the nearest owning Claude process, not merely by ancestry: an Agent/Task
// child can itself descend from the main pane while still being the thief.

const PLUGIN_ROOT_NEEDLE: Record<ChannelProviderType, string> = {
  telegram: '/telegram',
  slack: '/slack-channel',
  discord: '/discord',
  googlechat: '/googlechat',
  teams: '/teams',
}

export function parseMainDirPollerPids(
  psEwwOutput: string,
  pluginRootNeedle: string,
  stateEnvVar: string,
): number[] {
  const escaped = pluginRootNeedle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const rootRe = new RegExp(`CLAUDE_PLUGIN_ROOT=\\S*${escaped}(?:[/@ ]|$)`)
  const out: number[] = []
  for (const line of psEwwOutput.split('\n')) {
    if (!rootRe.test(line) || line.includes(`${stateEnvVar}=`)) continue
    const m = line.match(/^\s*(\d+)\s/)
    if (!m) continue
    const pid = parseInt(m[1]!, 10)
    if (pid > 1) out.push(pid)
  }
  return out
}

export function nearestClaudeAncestor(pid: number, byPid: Map<number, ProcRow>): number | null {
  const seen = new Set<number>()
  let cur = byPid.get(pid)?.ppid
  for (let hops = 0; hops < 8; hops++) {
    if (cur === undefined || cur <= 1 || seen.has(cur)) break
    seen.add(cur)
    const row = byPid.get(cur)
    if (row && isClaudeBinary(row.command)) return cur
    cur = row?.ppid
  }
  return null
}

/**
 * A poller's nearest Claude ancestor is part of the main channels session only
 * when it is the pane leader itself, or it is below a shell/wrapper pane leader
 * with no other Claude between it and that pane.  A second Claude below the
 * live main Claude is a locally spawned SDK/`claude -p` process and may own a
 * foreign main-token poller, even though it is transitively in the pane tree.
 */
export function claudeBelongsToMainPane(
  claudePid: number,
  byPid: Map<number, ProcRow>,
  panePids: Set<number>,
): boolean {
  if (panePids.has(claudePid)) return true

  const seen = new Set<number>([claudePid])
  let cur = byPid.get(claudePid)?.ppid
  for (let hops = 0; hops < 8; hops++) {
    // This is a destructive reaper: a truncated/looping ps snapshot proves
    // neither ownership nor foreignness. Keep the candidate alive unless an
    // actual second Claude is observed. Only a real init-reparented tree is a
    // positive orphan verdict.
    if (cur === undefined || seen.has(cur)) return true
    if (cur <= 1) return false
    const row = byPid.get(cur)
    // A direct or indirect child Claude of a Claude-led pane is a separate
    // local invocation (SDK fallback / `claude -p`), not the channels Claude.
    // A shell-led pane is the one legitimate wrapper shape.
    if (panePids.has(cur)) return !isClaudeBinary(row?.command ?? '')
    seen.add(cur)
    // The first Claude is the candidate itself.  Any additional Claude before
    // the pane leader marks a nested local invocation, not the channels pane.
    if (row && isClaudeBinary(row.command)) return false
    cur = row?.ppid
  }
  // More than eight wrapper levels is unusual but not evidence of a thief.
  return true
}

export function findForeignMainPollers(
  candidatePollerPids: number[],
  procs: ProcRow[],
  legitClaudePids: Set<number>,
): number[] {
  if (legitClaudePids.size === 0) return []
  const byPid = new Map<number, ProcRow>()
  for (const p of procs) byPid.set(p.pid, p)
  const out: number[] = []
  for (const pid of candidatePollerPids) {
    const owner = nearestClaudeAncestor(pid, byPid)
    // No identifiable Claude owner is deliberately fail-safe: a Node wrapper
    // or truncated process snapshot must never cause us to kill a poller.
    if (owner !== null && !claudeBelongsToMainPane(owner, byPid, legitClaudePids)) out.push(pid)
  }
  return out
}

function mainSessionPanePids(session: string, tmuxPath: string): Set<number> {
  try {
    const out = execSync(`${tmuxPath} list-panes -t ${session} -F '#{pane_pid}'`, { timeout: 5000, encoding: 'utf-8' })
    const outPids = new Set<number>()
    for (const line of out.split('\n')) {
      const n = parseInt(line.trim(), 10)
      if (Number.isFinite(n) && n > 1) outPids.add(n)
    }
    return outPids
  } catch {
    return new Set()
  }
}

export function reapForeignMainPollers(opts: {
  provider: ChannelProviderType
  mainSession: string
  tmuxPath?: string
}): number[] {
  const tmuxPath = opts.tmuxPath ?? 'tmux'
  const legit = mainSessionPanePids(opts.mainSession, tmuxPath)
  if (legit.size === 0) return []

  let psEww: string
  try {
    psEww = execSync('/bin/ps eww -e', { timeout: 5000, encoding: 'utf-8', maxBuffer: 8 * 1024 * 1024 })
  } catch (err) {
    logger.warn({ err }, 'channel-poller-reap: ps eww scan failed (foreign-main reap skipped)')
    return []
  }
  const candidates = parseMainDirPollerPids(psEww, PLUGIN_ROOT_NEEDLE[opts.provider], STATE_ENV_VAR[opts.provider])
  if (candidates.length === 0) return []

  const foreign = findForeignMainPollers(candidates, snapshotProcs(), legit)
  for (const pid of foreign) {
    try { process.kill(pid, 'SIGTERM') } catch { /* already gone */ }
  }
  if (foreign.length > 0) {
    try { execFileSync('/bin/sleep', ['0.3'], { timeout: 2000 }) } catch { /* ignore */ }
    for (const pid of foreign) {
      try { process.kill(pid, 0); process.kill(pid, 'SIGKILL') } catch { /* gone */ }
    }
    logger.info(
      { provider: opts.provider, mainSession: opts.mainSession, reaped: foreign, legit: [...legit] },
      'channel-poller-reap: foreign main-token poller(s) killed',
    )
  }
  return foreign
}

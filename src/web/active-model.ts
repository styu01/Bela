import { existsSync, readdirSync, readFileSync, statSync, openSync, readSync, closeSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'

// Claude Code writes one .jsonl session log per session under
// ~/.claude/projects/<encoded-working-dir>/. Every assistant turn carries the
// model id that answered it. We use that to surface the *live* running model
// (vs. the configured value in agent-config.json), so the dashboard can show
// what the running process is actually using, including across restarts.
//
// When an agent is launched with --continue, Claude Code appends to the same
// session jsonl across restarts, so the latest "model" field may reflect a
// pre-restart turn rather than the freshly-spawned process. Callers that know
// when the current session started should pass sinceUnixSec; we then ignore
// any line whose own timestamp predates that, leaving the caller to fall back
// to the configured model until the new session writes its first turn.
const cache = new Map<string, { value: string | null; expiresAt: number }>()
const TTL_MS = 3000

// Resolve the session-log directory Claude Code writes for a working dir.
// Logs live under <config-root>/projects/<encoded-working-dir>/, where the
// config root is ~/.claude by default but an alternate one when the agent was
// launched with CLAUDE_CONFIG_DIR. Pass that absolute config root as configDir
// so we read the right project dir for agents on a non-default config.
export function projectsDirFor(workingDir: string, configDir?: string, homeDirOverride?: string): string {
  const base = configDir ?? join(homeDirOverride ?? homedir(), '.claude')
  const encoded = workingDir.replace(/[/.]/g, '-')
  return join(base, 'projects', encoded)
}

/**
 * Newest Claude Code JSONL transcript for a session, or null when no readable
 * transcript exists. Shared by usage readers and the context-restart archive
 * path so both refer to the same on-disk session source.
 */
export function latestTranscriptPathForProjectDir(workingDir: string, configDir?: string): string | null {
  try {
    const dir = projectsDirFor(workingDir, configDir)
    if (!existsSync(dir)) return null
    const jsonls = readdirSync(dir)
      .filter(f => f.endsWith('.jsonl'))
      .map(f => ({ path: join(dir, f), mtime: statSync(join(dir, f)).mtimeMs }))
      .sort((a, b) => b.mtime - a.mtime)
    return jsonls[0]?.path ?? null
  } catch { return null }
}

export function readActiveModelFromProjectDir(workingDir: string, sinceUnixSec?: number, configDir?: string): string | null {
  const now = Date.now()
  const cacheKey = `${workingDir}:${sinceUnixSec ?? ''}:${configDir ?? ''}`
  const cached = cache.get(cacheKey)
  if (cached && cached.expiresAt > now) return cached.value
  let value: string | null = null
  try {
    const dir = projectsDirFor(workingDir, configDir)
    if (!existsSync(dir)) {
      cache.set(cacheKey, { value: null, expiresAt: now + TTL_MS })
      return null
    }
    const jsonls = readdirSync(dir)
      .filter(f => f.endsWith('.jsonl'))
      .map(f => ({ f, mtime: statSync(join(dir, f)).mtimeMs }))
      .sort((a, b) => b.mtime - a.mtime)
    if (jsonls.length === 0) {
      cache.set(cacheKey, { value: null, expiresAt: now + TTL_MS })
      return null
    }
    value = activeModelFromContent(readFileSync(join(dir, jsonls[0].f), 'utf-8'), sinceUnixSec)
  } catch { /* fall through */ }
  cache.set(cacheKey, { value, expiresAt: now + TTL_MS })
  return value
}

// Pure: the model of the last assistant line of a transcript's content (optionally not older than
// sinceUnixSec). Shared by the directory read above and the own-file read below, so both apply the
// exact same rule.
export function activeModelFromContent(content: string, sinceUnixSec?: number): string | null {
  const lines = content.split('\n')
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim()
    if (!line) continue
    try {
      const entry = JSON.parse(line)
      const msg = entry?.message
      const model = msg?.model
      if (typeof model !== 'string' || model.startsWith('<')) continue
      if (sinceUnixSec !== undefined) {
        const ts = entry?.timestamp
        if (typeof ts !== 'string') continue
        const lineUnix = Math.floor(new Date(ts).getTime() / 1000)
        if (!Number.isFinite(lineUnix) || lineUnix < sinceUnixSec) continue
      }
      return model
    } catch { /* skip malformed JSON line */ }
  }
  return null
}

// Pure: input + cache_read + cache_creation tokens of the last line carrying a usage.
export function contextTokensFromContent(content: string): number | null {
  const lines = content.split('\n')
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim()
    if (!line) continue
    try {
      const u = JSON.parse(line)?.message?.usage
      if (u && typeof u === 'object') {
        const inp = Number(u.input_tokens) || 0
        const cr = Number(u.cache_read_input_tokens) || 0
        const cc = Number(u.cache_creation_input_tokens) || 0
        const total = inp + cr + cc
        if (total > 0) return total
      }
    } catch { /* skip malformed JSON line */ }
  }
  return null
}

// STUCKOWNSID1127 follow-up (BELA msg 6173): read ONE known transcript (the main session's proven own
// file) instead of the newest file of a shared project dir. Memoised on (path, mtime): the guard asks
// every tick, the main transcript runs to tens of MB, and an unchanged file has an unchanged answer.
const ownFileMemo = new Map<string, { mtimeMs: number; tokens: number | null; model: string | null }>()
function ownFileFacts(path: string): { tokens: number | null; model: string | null } | null {
  try {
    const mtimeMs = statSync(path).mtimeMs
    const hit = ownFileMemo.get(path)
    if (hit && hit.mtimeMs === mtimeMs) return hit
    const content = readFileSync(path, 'utf-8')
    const facts = { mtimeMs, tokens: contextTokensFromContent(content), model: activeModelFromContent(content) }
    if (ownFileMemo.size > 8) ownFileMemo.clear()
    ownFileMemo.set(path, facts)
    return facts
  } catch {
    return null
  }
}
export function readContextTokensFromFile(path: string): number | null {
  return ownFileFacts(path)?.tokens ?? null
}
export function readActiveModelFromFile(path: string): string | null {
  return ownFileFacts(path)?.model ?? null
}

const ctxCache = new Map<string, { value: number | null; expiresAt: number }>()

// Ported from upstream Szotasz/marveen (26ac8c83, used by 9d6b614b's restart turn-quiet check).
const TURN_TAIL_BYTES = 512 * 1024

// Epoch ms of the last TURN line (type user / assistant) in the newest
// transcript; null = no transcript or no such line in the tail. Unlike the
// file mtime, bookkeeping lines do not count: Claude Code writes a
// queue-operation, a "UserPromptSubmit operation blocked by hook" system line
// and last-prompt / title metadata for every prompt a hook blocks, so an owner
// command made the transcript look "active" by its own blocked prompt
// (ELSOKOR922 Phase 7 A-smoke: /model refused "transcript-active (0s)").
// A running turn writes user (tool_result) and assistant lines, so the gap
// between two tool calls -- what the quiet window guards -- still counts.
// A local slash command (/model, /effort, /clear sent into the pane) writes
// `user` lines -- "<command-name>/model</command-name>..." and
// "<local-command-stdout>Set model to ...</local-command-stdout>" -- but no model
// turn runs. Counted as activity they made every write within 20 s of our own
// previous /model read "turn-active" (measured on the test bot, 2026-09-23).
function isLocalCommandLine(e: { type?: unknown; message?: { content?: unknown } }): boolean {
  if (e.type !== 'user') return false
  const c = e.message?.content
  return typeof c === 'string' && /^\s*<(local-command-(stdout|stderr|caveat)|command-name)>/.test(c)
}

export function readLastTurnActivityMs(workingDir: string, configDir?: string): number | null {
  try {
    const dir = projectsDirFor(workingDir, configDir)
    if (!existsSync(dir)) return null
    const newest = readdirSync(dir)
      .filter(f => f.endsWith('.jsonl'))
      .map(f => ({ f, mtime: statSync(join(dir, f)).mtimeMs, size: statSync(join(dir, f)).size }))
      .sort((a, b) => b.mtime - a.mtime)[0]
    if (!newest) return null
    const fd = openSync(join(dir, newest.f), 'r')
    let text: string
    try {
      const len = Math.min(newest.size, TURN_TAIL_BYTES)
      const buf = Buffer.alloc(len)
      readSync(fd, buf, 0, len, newest.size - len)
      text = buf.toString('utf-8')
    } finally { closeSync(fd) }
    const lines = text.split('\n')
    for (let i = lines.length - 1; i >= 0; i--) {
      const line = lines[i].trim()
      if (!line.startsWith('{')) continue
      try {
        const e = JSON.parse(line)
        if (e?.type !== 'user' && e?.type !== 'assistant') continue
        if (isLocalCommandLine(e)) continue
        const at = typeof e.timestamp === 'string' ? new Date(e.timestamp).getTime() : NaN
        if (Number.isFinite(at)) return at
      } catch { /* a line cut by the tail window, or malformed */ }
    }
    // The WHOLE file was read and holds no turn: a fresh session (after a
    // deploy or /clear) that has only bookkeeping lines. That is "never had a
    // turn" -- quiet -- not "unknown". Returning null here made the caller
    // fall back to the file's mtime, which every hook-blocked command bumps,
    // so commands typed in a row kept each other "turn-active" (measured on
    // the test bot, 2026-09-23). A file larger than the window stays null.
    if (newest.size <= TURN_TAIL_BYTES) return 0
  } catch { /* fall through */ }
  return null
}

// Current context size of the live session, in tokens. Claude Code records a
// `usage` object on each assistant turn; the context that gets re-read every
// turn is input_tokens + cache_read_input_tokens + cache_creation_input_tokens
// (output_tokens is the new reply, not context). We scan the newest transcript
// from the end for the last turn carrying a usage and sum those three. Returns
// null when there is no transcript / no usage yet (fresh session). This is what
// the dashboard surfaces so the operator can see a session growing heavy and
// decide to restart it.
export function readContextTokensFromProjectDir(workingDir: string, configDir?: string): number | null {
  const now = Date.now()
  const cacheKey = `${workingDir}:${configDir ?? ''}`
  const cached = ctxCache.get(cacheKey)
  if (cached && cached.expiresAt > now) return cached.value
  let value: number | null = null
  try {
    const dir = projectsDirFor(workingDir, configDir)
    if (existsSync(dir)) {
      const jsonls = readdirSync(dir)
        .filter(f => f.endsWith('.jsonl'))
        .map(f => ({ f, mtime: statSync(join(dir, f)).mtimeMs }))
        .sort((a, b) => b.mtime - a.mtime)
      if (jsonls.length > 0) {
        value = contextTokensFromContent(readFileSync(join(dir, jsonls[0].f), 'utf-8'))
      }
    }
  } catch { /* fall through */ }
  ctxCache.set(cacheKey, { value, expiresAt: now + TTL_MS })
  return value
}

/**
 * Wall-clock mtime (ms) of the newest transcript for a working dir, or null
 * when there is none (fresh session, unreadable dir, agent on a remote host).
 *
 * This is the cheapest "when did this session last do anything" signal, and
 * the honest one: Claude Code appends to the jsonl on every turn, so the
 * file's mtime is written BY the session, outside the dashboard process. A
 * clock kept in dashboard memory dies with the dashboard, and a
 * count-the-sweeps streak measures the sweep interval rather than the agent.
 * Neither survives a restart; this does.
 *
 * What it does NOT measure: whether the agent is working right now. A single
 * long tool call (a 30-minute Bash, a subagent) appends nothing while it runs,
 * so the transcript goes quiet while real work is in flight. Callers must pair
 * this with a live-work signal -- the guard uses paneIdle -- and never treat a
 * stale mtime on its own as "finished".
 *
 * The mtime is already computed inside readContextTokensFromProjectDir to pick
 * the newest file; this exposes it rather than recomputing the selection
 * differently, so the two always describe the SAME transcript.
 */
export function readTranscriptMtimeFromProjectDir(workingDir: string, configDir?: string): number | null {
  try {
    const dir = projectsDirFor(workingDir, configDir)
    if (!existsSync(dir)) return null
    let newest: number | null = null
    for (const f of readdirSync(dir)) {
      if (!f.endsWith('.jsonl')) continue
      const m = statSync(join(dir, f)).mtimeMs
      if (newest === null || m > newest) newest = m
    }
    return newest
  } catch { return null }
}

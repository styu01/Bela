// STUCKOWNSID1127 (local, not upstream): the stuck-tool-call watcher's kill gate must read the
// MAIN session's OWN transcript, not the newest .jsonl anywhere in its project dir.
//
// Why: the main session's project dir (~/.claude/projects/-home-kisss-marveen) is shared. Other
// sessions whose cwd is the marveen root write there too: measured 2026-11-27 over the last 14
// days, two 'sdk-cli' sessions (one on 11-26 08:45, running alongside BELA's own session) plus the
// owner's interactive 'cli' sessions in the repo root. The kill gate's STUCKATTEMPTS1018 branch
// ("written at or after the freeze -> alive") then holds a REAL wedge for the whole spell once a
// foreign session writes once during it (PROGI review msg 6145 (c)).
//
// Source of truth for "which file is the main session writing": Claude Code keeps a per-process
// record at <CLAUDE_CONFIG_DIR>/sessions/<pid>.json ({pid, sessionId, cwd, procStart, ...}).
// Measured on this host with CC 2.1.326, for every running claude (9 processes): the record's
// sessionId names the file the process is appending to, and it stays right across all three ways
// a session gets its file:
//   - fresh start: a new <sid>.jsonl (bela-channels, 2026-11-27);
//   - --continue: the old file keeps growing under its old sid (agent-iris: launched 11-26 with
//     --continue, record sid 54106e91 = the file started on 11-16);
//   - /clear: the record switches to the new sid at the clear (agent-progi 07:51:10 UTC: the
//     record names the new 260b8707 file; the old file got one last metadata write at that moment).
// procStart is field 22 of /proc/<pid>/stat (start time in clock ticks); it matched for all 9.
//
// Identity checks before a record is trusted (a stale record of a dead process can sit in
// sessions/ for months -- files from June are there today, and pids are reused):
//   pid field == the pane pid, sessionId is a UUID (it becomes a file name: no path tricks),
//   cwd == the project root, and procStart == the live process's start time where /proc exists
//   (Linux). A record WITHOUT procStart, or a pid whose /proc stat cannot be read, is rejected on
//   Linux (identity not provable); without
//   /proc (macOS) pid + cwd is accepted -- stated limit: a stale record of an earlier main claude
//   with the same reused pid would be read there.
//
// Any failure returns { ok: false, reason } and the caller falls back to the old directory-level
// read: never more kills than before this change, only fewer foreign-session masks.

import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { projectsDirFor } from './active-model.js'
import { resolveFromPath } from '../platform.js'

const SESSION_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export interface OwnTranscriptDeps {
  /** File content, or null when it cannot be read. */
  readFile(path: string): string | null
  /** mtime in ms, or null when the file does not exist / cannot be stat'ed. */
  mtimeMs(path: string): number | null
  /** True where a Linux-style /proc exists (then procStart MUST be readable for a live pid). */
  procAvailable: boolean
  /** Start time of the live process (field 22 of /proc/<pid>/stat), or null when it cannot be read. */
  procStart(pid: number): string | null
  /** CLAUDE_CONFIG_DIR from the live process's environment, or null when unreadable or unset. */
  procConfigDir(pid: number): string | null
  home: string
}

export type OwnTranscript =
  | { ok: true; sessionId: string; path: string; mtimeMs: number }
  | { ok: false; reason: string }

// Pure (all I/O through deps). extraConfigDirs: config roots to try after the process's own
// CLAUDE_CONFIG_DIR and before ~/.claude (the main agent's .channels-config, for hosts where
// /proc/<pid>/environ is not readable).
export function resolveOwnSessionTranscript(
  pid: number,
  projectRoot: string,
  deps: OwnTranscriptDeps,
  extraConfigDirs: string[] = [],
): OwnTranscript {
  const candidates = [deps.procConfigDir(pid), ...extraConfigDirs, join(deps.home, '.claude')]
    .filter((d): d is string => typeof d === 'string' && d.length > 0)
  const roots = [...new Set(candidates)]
  const liveStart = deps.procAvailable ? deps.procStart(pid) : null
  // /proc exists but the pid's stat is unreadable: the process is gone (or not ours) -- no record can be proven.
  if (deps.procAvailable && liveStart === null) return { ok: false, reason: 'proc-unreadable' }
  let reason = 'no-session-record'
  for (const root of roots) {
    const raw = deps.readFile(join(root, 'sessions', `${pid}.json`))
    if (raw === null) continue
    let rec: Record<string, unknown>
    try {
      const parsed: unknown = JSON.parse(raw)
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) { reason = 'record-not-object'; continue }
      rec = parsed as Record<string, unknown>
    } catch { reason = 'record-unparsable'; continue }
    if (rec.pid !== pid) { reason = 'record-pid-mismatch'; continue }
    const sid = rec.sessionId
    if (typeof sid !== 'string' || !SESSION_ID_RE.test(sid)) { reason = 'record-bad-session-id'; continue }
    if (rec.cwd !== projectRoot) { reason = 'record-cwd-mismatch'; continue }
    if (deps.procAvailable) {
      if (rec.procStart === undefined || rec.procStart === null) { reason = 'record-no-procstart'; continue }
      if (String(rec.procStart) !== liveStart) { reason = 'record-procstart-mismatch'; continue }
    }
    const path = join(projectsDirFor(projectRoot, root), `${sid}.jsonl`)
    const mtime = deps.mtimeMs(path)
    if (mtime === null) return { ok: false, reason: 'transcript-missing' }
    return { ok: true, sessionId: sid, path, mtimeMs: mtime }
  }
  return { ok: false, reason }
}

// Field 22 of /proc/<pid>/stat. comm (field 2) may hold spaces and parens, so split after the
// LAST ')': the remainder starts at field 3, which puts field 22 at index 19.
export function parseProcStatStartTime(stat: string): string | null {
  const close = stat.lastIndexOf(')')
  if (close < 0) return null
  const fields = stat.slice(close + 1).trim().split(/\s+/)
  const v = fields[19]
  return v !== undefined && /^\d+$/.test(v) ? v : null
}

// CLAUDE_CONFIG_DIR from a NUL-separated /proc/<pid>/environ blob.
export function parseEnvironConfigDir(environ: string): string | null {
  for (const entry of environ.split('\0')) {
    if (entry.startsWith('CLAUDE_CONFIG_DIR=')) {
      const v = entry.slice('CLAUDE_CONFIG_DIR='.length)
      return v.length > 0 ? v : null
    }
  }
  return null
}

function readOrNull(path: string): string | null {
  try { return readFileSync(path, 'utf-8') } catch { return null }
}

export const liveOwnTranscriptDeps: OwnTranscriptDeps = {
  readFile: readOrNull,
  procAvailable: existsSync('/proc/self/stat'),
  mtimeMs: (path) => { try { return statSync(path).mtimeMs } catch { return null } },
  procStart: (pid) => {
    const stat = readOrNull(`/proc/${pid}/stat`)
    return stat === null ? null : parseProcStatStartTime(stat)
  },
  procConfigDir: (pid) => {
    const env = readOrNull(`/proc/${pid}/environ`)
    return env === null ? null : parseEnvironConfigDir(env)
  },
  home: homedir(),
}

// The pid of a local tmux session's first pane, or null on any failure. For the main channels session
// this IS its claude process (measured: pane_pid comm=claude, launched without a wrapper shell).
export function tmuxPanePid(session: string): number | null {
  try {
    const out = execFileSync(resolveFromPath('tmux'), ['list-panes', '-t', session, '-F', '#{pane_pid}'],
      { timeout: 3000, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'] })
    const first = out.split('\n')[0]?.trim()
    return first && /^\d+$/.test(first) ? Number(first) : null
  } catch {
    return null
  }
}

export interface MainTranscriptMtimeDeps {
  panePid(session: string): number | null
  own(pid: number, projectRoot: string): OwnTranscript
  /** The old directory-level read: the fallback whenever the own file cannot be proven. */
  dir(): number | null
}

// The MAIN session's transcript mtime for the readers outside the stuck-watcher (STUCKOWNSID1127
// follow-up, PROGI msg 6169 item 3): its own file when provable, else the old directory-level newest
// jsonl -- so a failed proof behaves exactly as before this change.
export function mainSessionTranscriptMtime(
  session: string,
  projectRoot: string,
  deps: MainTranscriptMtimeDeps,
): { mtime: number | null; source: 'own' | 'dir'; fallbackReason: string | null } {
  const pid = deps.panePid(session)
  if (pid === null) return { mtime: deps.dir(), source: 'dir', fallbackReason: 'no-pane-pid' }
  const own = deps.own(pid, projectRoot)
  if (own.ok) return { mtime: own.mtimeMs, source: 'own', fallbackReason: null }
  return { mtime: deps.dir(), source: 'dir', fallbackReason: own.reason }
}

// The main session's PROVEN own session, for a respawn that should `--resume <sid>` it (BELA msg
// 6173: the down-cascade stage 3 too, not only the stuck-watcher). null + the reason when it cannot
// be proven; the caller then respawns with --continue and logs that reason.
export function provenMainSession(
  session: string,
  projectRoot: string,
  deps: { panePid(session: string): number | null; own(pid: number, projectRoot: string): OwnTranscript } =
    { panePid: tmuxPanePid, own: readOwnSessionTranscript },
): { resume: { sessionId: string; transcriptPath: string } | null; reason: string | null } {
  const pid = deps.panePid(session)
  if (pid === null) return { resume: null, reason: 'no-pane-pid' }
  const own = deps.own(pid, projectRoot)
  if (!own.ok) return { resume: null, reason: own.reason }
  rememberMainSession(projectRoot, { sessionId: own.sessionId, transcriptPath: own.path })
  return { resume: { sessionId: own.sessionId, transcriptPath: own.path }, reason: null }
}

// The LAST proven own session per project root, in memory (BELA msg 6197 item 6, PROGI 6196 on
// 8a72094a). When the respawn has to happen after the claude process already died, its
// sessions/<pid>.json record can no longer be proven, and a plain --continue would take the dir's
// newest INTERACTIVE session -- a live foreign one included. Every successful proof (the guard's
// tick, the stuck-watcher's kill gate, the stage-3 resume) refreshes this, so it holds BELA's own
// session as of moments before the death. Used only within REMEMBER_MAX_AGE_MS and while the
// transcript exists. Stated limit: a dashboard restart empties it (then --continue, as before).
export const REMEMBER_MAX_AGE_MS = 6 * 60 * 60_000
const rememberedMain = new Map<string, { sessionId: string; transcriptPath: string; at: number }>()
export function rememberMainSession(projectRoot: string, s: { sessionId: string; transcriptPath: string }, now = Date.now()): void {
  rememberedMain.set(projectRoot, { ...s, at: now })
}
export function rememberedMainSession(
  projectRoot: string,
  now = Date.now(),
  exists: (path: string) => boolean = existsSync,
): { sessionId: string; transcriptPath: string } | null {
  const r = rememberedMain.get(projectRoot)
  if (!r || now - r.at > REMEMBER_MAX_AGE_MS || !exists(r.transcriptPath)) return null
  return { sessionId: r.sessionId, transcriptPath: r.transcriptPath }
}
/**
 * Drop the remembered session of one project root. The context-restart gate calls this right after its own /clear
 * of the main agent (PROGI review 6206 item 2): the remembered sid is then the PRE-clear session, and a respawn
 * that had to fall back to it would --resume the large context the clear just shed. With nothing remembered the
 * fallback is the old --continue, which after a clear takes the newest file -- the post-clear one.
 */
export function forgetRememberedMainSession(projectRoot: string): void { rememberedMain.delete(projectRoot) }
/** Test seam. */
export function _forgetRememberedMainSessions(): void { rememberedMain.clear() }

export function readOwnSessionTranscript(pid: number, projectRoot: string): OwnTranscript {
  try {
    const r = resolveOwnSessionTranscript(pid, projectRoot, liveOwnTranscriptDeps, [join(projectRoot, '.channels-config')])
    if (r.ok) rememberMainSession(projectRoot, { sessionId: r.sessionId, transcriptPath: r.path })
    return r
  } catch {
    return { ok: false, reason: 'error' }
  }
}

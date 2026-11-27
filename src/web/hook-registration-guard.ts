import { existsSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, sep } from 'node:path'
import { atomicWriteFileSync } from './atomic-write.js'
import { logger } from '../logger.js'

// Guard + self-heal for hook registration into user-global settings.json.
//
// Incident (2026-07-11): an app instance started from a git worktree (a
// WEB_ONLY smoke instance under .claude/worktrees/agent-XXXX) ran the startup
// hook backfill and registered UserPromptSubmit / SessionStart hooks into the
// USER-GLOBAL ~/.claude/settings.json with absolute paths rooted in its own
// (temporary) PROJECT_ROOT. When the worktree was deleted, python3 exited 2
// ("can't open file") -- and a non-zero UserPromptSubmit hook BLOCKS the
// prompt, so the main agent went deaf to all inbound messages until the stale
// entries were removed by hand.
//
// Two layers:
//   1. shouldRegisterHooks(): skip registration entirely when the running
//      instance is a worktree checkout (its PROJECT_ROOT is temporary) or a
//      WEB_ONLY staging instance (not the install that owns the settings).
//   2. pruneStaleHookEntries(): during normal startup, remove entries THIS
//      app previously wrote whose script file no longer exists. Foreign hook
//      entries (not our script names, not worktree-pathed) are never touched.

// Hook script filenames this app registers into settings.json files
// (templates/settings.json.template, ensureAgentStalenessHook, the
// PreToolUse gates, and the telegram-progress installer). Used to decide
// whether a missing-file hook entry is OURS (prunable) or foreign (kept).
export const KNOWN_HOOK_SCRIPTS: readonly string[] = [
  'taskstate-replay.py',
  'voice-reply-directive.py',
  'staleness-guard.py',
  'email-send-gate.mjs',
  'self-pace-gate.mjs',
  'telegram_progress.py',
  'telegram_progress_clear.py',
  'telegram_progress_watchdog.py',
  'inbox-drain.py',
  'channel-inbox-drain.py',
  'ledger-capture.py',
  'tool-log-capture.py',
  // The /clear continuity pair: SessionEnd capture + SessionStart replay (upstream 7a47b24a).
  'clear-capture.py',
  'clear-replay.py',
]

// Path fragment that marks a checkout as an agent worktree. Kept
// separator-agnostic by normalizing before matching.
const WORKTREES_FRAGMENT = '/.claude/worktrees/'

function normalizeSeparators(p: string): string {
  return p.split(sep).join('/')
}

// True when the .git entry at the given root is a FILE (a linked worktree's
// gitdir pointer) rather than a directory (a normal checkout's object store).
// This is the generic worktree signal: in every `git worktree add` checkout,
// .git is a plain file, so PROJECT_ROOT differs from the common dir's toplevel.
function gitEntryIsFile(root: string): boolean {
  try {
    return statSync(join(root, '.git')).isFile()
  } catch {
    return false
  }
}

// Is the resolved project root a git-worktree checkout (and therefore a
// temporary location that must never be baked into user-global settings)?
// The isGitFile dependency is injectable so the decision logic is unit-testable
// without a real filesystem.
export function isWorktreeRoot(
  projectRoot: string,
  deps: { isGitFile?: (root: string) => boolean } = {},
): boolean {
  const normalized = normalizeSeparators(projectRoot)
  if (normalized.includes(WORKTREES_FRAGMENT)) return true
  return (deps.isGitFile ?? gitEntryIsFile)(projectRoot)
}

// Temp-dir prefixes that mark a checkout as transient. A plain `git clone` under
// a temp dir (NOT a git worktree, so isWorktreeRoot misses it) is exactly the
// canary / second-instance case: 2026-07-13 a develop canary started from
// /private/tmp/marveen-work registered hooks into the USER-GLOBAL settings.json
// with /tmp-rooted paths -- the same deaf-agent trap isWorktreeRoot was added to
// prevent, one class wider. A real install never runs from a temp dir, so
// skipping here can never suppress a legitimate owner's registration.
const TEMP_ROOT_PREFIXES = ['/tmp/', '/private/tmp/', '/var/folders/', '/private/var/folders/']

// Is the project root under a temporary directory (a transient second instance
// -- canary, throwaway clone -- that does not own the user's settings)? The
// tmpDir dependency is injectable so the OS tmpdir is included and the logic is
// unit-testable without touching the environment.
export function isTemporaryRoot(
  projectRoot: string,
  deps: { tmpDir?: string } = {},
): boolean {
  const normalized = normalizeSeparators(projectRoot)
  const prefixes = [...TEMP_ROOT_PREFIXES]
  if (deps.tmpDir) {
    const t = normalizeSeparators(deps.tmpDir).replace(/\/+$/, '') + '/'
    prefixes.push(t)
  }
  return prefixes.some((p) => normalized.startsWith(p))
}

export interface HookRegistrationDecision {
  register: boolean
  reason?: string
}

// Central decision: may this instance register hooks into settings.json?
// Skips worktree checkouts, temp-dir clones (both temporary PROJECT_ROOTs), and
// WEB_ONLY staging instances (not the install that owns the user's settings).
export function shouldRegisterHooks(opts: {
  projectRoot: string
  webOnly: boolean
  isGitFile?: (root: string) => boolean
  tmpDir?: string
}): HookRegistrationDecision {
  if (isWorktreeRoot(opts.projectRoot, { isGitFile: opts.isGitFile })) {
    return { register: false, reason: 'project root is a git worktree checkout (temporary path)' }
  }
  if (isTemporaryRoot(opts.projectRoot, { tmpDir: opts.tmpDir })) {
    return { register: false, reason: 'project root is under a temp dir (transient second instance)' }
  }
  if (opts.webOnly) {
    return { register: false, reason: 'WEB_ONLY staging mode' }
  }
  return { register: true }
}

type CommandHook = { type?: string; command?: string; [k: string]: unknown }
type HookGroup = { hooks?: CommandHook[]; [k: string]: unknown }

// Extract the candidate script paths from a hook command string: tokens that
// end with one of our known hook script names, or that point into a
// .claude/worktrees/ checkout. Anything else in the command is ignored, so a
// foreign hook that merely mentions python3 is never considered ours.
function ourScriptPaths(command: string, knownScripts: readonly string[]): string[] {
  const paths: string[] = []
  for (const raw of command.split(/\s+/)) {
    // The fail-closed gate wrapper (agent-scaffold failClosedGateRun) writes `"<node>" "<path>"; else ...`:
    // a token can end in `";`. The trailing ; is stripped before the quotes (upstream 42775042, ported).
    const token = raw.replace(/^['"]+/, '').replace(/;+$/, '').replace(/['"]+$/, '')
    if (!token) continue
    const normalized = normalizeSeparators(token)
    const base = normalized.slice(normalized.lastIndexOf('/') + 1)
    if (knownScripts.includes(base) || normalized.includes(WORKTREES_FRAGMENT)) {
      paths.push(token)
    }
  }
  return paths
}

export interface PruneResult {
  changed: boolean
  removed: string[]
  /** Fail-closed gate entries kept although their file is missing: it is missing where this install expects it. */
  keptFailClosed: string[]
}

// = GATE_FAIL_CLOSED_TAG in agent-scaffold.ts (not imported: that module pulls in the agent runtime; a test pins the two).
export const FAIL_CLOSED_MARK = 'governance-kapu (fail-closed)'

// Self-heal: remove hook entries this app previously wrote whose script file
// no longer exists on disk. An entry is prunable only when BOTH hold:
//   - its command references a path matching our known hook script names, or
//     a path inside a .claude/worktrees/ checkout, AND
//   - that referenced file is missing.
// Everything else (foreign commands, agent-type hooks, our entries whose file
// still exists) is preserved byte-identically. Mutates `settings` in place;
// the caller persists it (read-modify-write with an atomic write).
export function pruneStaleHookEntries(
  settings: Record<string, unknown>,
  opts: {
    fileExists: (path: string) => boolean
    knownScripts?: readonly string[]
    /** The live root's scripts/ and scripts/hooks/: a fail-closed gate missing there stays and keeps denying. */
    expectedScriptDirs?: readonly string[]
  },
): PruneResult {
  const knownScripts = opts.knownScripts ?? KNOWN_HOOK_SCRIPTS
  const expectedDirs = (opts.expectedScriptDirs ?? []).map(normalizeSeparators)
  const removed: string[] = []
  const keptFailClosed: string[] = []
  const hooks = settings.hooks
  if (!hooks || typeof hooks !== 'object' || Array.isArray(hooks)) return { changed: false, removed, keptFailClosed }
  const hooksRecord = hooks as Record<string, unknown>

  for (const [event, groups] of Object.entries(hooksRecord)) {
    if (!Array.isArray(groups)) continue
    const keptGroups: HookGroup[] = []
    let eventChanged = false
    for (const group of groups as HookGroup[]) {
      if (!group || typeof group !== 'object' || !Array.isArray(group.hooks)) {
        keptGroups.push(group)
        continue
      }
      const keptHooks = group.hooks.filter((h) => {
        if (!h || typeof h !== 'object' || h.type !== 'command' || typeof h.command !== 'string') return true
        const scriptPaths = ourScriptPaths(h.command, knownScripts)
        if (scriptPaths.length === 0) return true // foreign entry: never touch
        const missing = scriptPaths.filter((p) => !opts.fileExists(p))
        if (missing.length === 0) return true
        // Upstream 42775042 (#2874 B1), ported: a shipped fail-closed gate missing where this install runs it
        // from is a broken gate, not a stale entry. Pruned, nothing would put it back (the gate injectors
        // refuse a missing script, isUnsafeHookCommand): a gate that denied every call would become no gate.
        // It stays and denies. The same gate on another root (a gone worktree) is pruned as before.
        // LOCAL: no extra-hooks (local-hooks/) mechanism here, so there is no moved-name exception.
        const expected = (p: string) => expectedDirs.includes(dirname(normalizeSeparators(p)))
        if (h.command.includes(FAIL_CLOSED_MARK) && missing.some(expected)) {
          keptFailClosed.push(h.command)
          return true
        }
        removed.push(h.command)
        return false
      })
      if (keptHooks.length !== group.hooks.length) {
        eventChanged = true
        // Drop the group entirely when pruning emptied it; a matcher with no
        // hooks is dead weight. Groups that keep at least one hook survive.
        if (keptHooks.length > 0) keptGroups.push({ ...group, hooks: keptHooks })
      } else {
        keptGroups.push(group)
      }
    }
    if (eventChanged) {
      if (keptGroups.length > 0) hooksRecord[event] = keptGroups
      else delete hooksRecord[event]
    }
  }
  return { changed: removed.length > 0, removed, keptFailClosed }
}

// File-level wrapper: parse a settings.json, prune stale entries, and write it
// back atomically when anything changed. Unparseable or missing files are left
// untouched (never destroy a user's settings on a parse error). Returns the
// pruned command strings for logging. `liveRoot` is the root this install runs its shipped hooks from:
// a fail-closed gate missing under its scripts/ or scripts/hooks/ is kept, with an error log.
export function pruneStaleHooksFromSettingsFile(
  settingsPath: string,
  liveRoot?: string,
  // BELA 6223 a: the "kept" case is also reported to the main agent (web.ts passes gate-self-test's
  // reportPruneKept); this module stays free of that import.
  onKeptFailClosed?: (commands: string[]) => void,
): string[] {
  if (!existsSync(settingsPath)) return []
  let settings: Record<string, unknown>
  // BELA 6300: a leading UTF-8 BOM (Claude Code reads such a file) is dropped for the parse and kept on a
  // rewrite, so the file's bytes change only where entries were pruned.
  let bom = ''
  try {
    const raw = readFileSync(settingsPath, 'utf-8')
    if (raw.startsWith('\uFEFF')) bom = '\uFEFF'
    settings = JSON.parse(bom ? raw.slice(1) : raw) as Record<string, unknown>
  } catch {
    return []
  }
  if (!settings || typeof settings !== 'object') return []
  const expectedScriptDirs = liveRoot ? [join(liveRoot, 'scripts'), join(liveRoot, 'scripts', 'hooks')] : []
  const { changed, removed, keptFailClosed } = pruneStaleHookEntries(settings, { fileExists: existsSync, expectedScriptDirs })
  for (const command of keptFailClosed) {
    logger.error({ settingsPath, command }, 'boot-hook-prune: a fail-closed gate is missing where this install expects it; kept, it denies until the file is back')
  }
  if (keptFailClosed.length && onKeptFailClosed) {
    try { onKeptFailClosed(keptFailClosed) } catch { /* reporting never breaks the prune */ }
  }
  if (changed) atomicWriteFileSync(settingsPath, bom + JSON.stringify(settings, null, 2))
  return removed
}

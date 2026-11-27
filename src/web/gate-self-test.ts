import { spawn, execFileSync } from 'node:child_process'
import { existsSync, readFileSync, realpathSync } from 'node:fs'
import { join, dirname, relative, isAbsolute, resolve } from 'node:path'
import { loadavg } from 'node:os'
import { PROJECT_ROOT, MAIN_AGENT_ID, WEB_PORT } from '../config.js'
import { logger } from '../logger.js'
import { createAgentMessage } from '../db.js'
import { agentDir, listAgentNames } from './agent-config.js'
import { agentSettingsPath, GATE_FAIL_CLOSED_TAG } from './agent-scaffold.js'
import { escalateToOwner, clearOwnerEscalation, ownerEscalationStage2Sent } from './owner-escalation.js'
import { notifyChannel } from '../notify.js'

// Gate self-test (BELA 6223 a). Since the gates fail closed (upstream 0e681c83), a gate whose file is broken
// blocks every call it matches: a broken self-pace-gate makes the sub-agents mute (they cannot even run the
// curl that would report it), a broken outgoing-copy-gate blocks every Bash call of the main agent. Nobody
// would see it until somebody tries. This runs every wired gate command EXACTLY as the settings file holds it
// (main agent's user, project and isolated settings, every sub-agent), with a benign tool call, at boot and
// every 15 minutes, and tells the main agent once per state change: a gate that stopped passing a benign call,
// and a gate that recovered. The boot prune's "kept" case (a fail-closed gate whose file is missing at the
// live root, src/web/hook-registration-guard.ts) is reported through the same channel.

// The gate scripts this install wires (agent-scaffold injectors + the main agent's project settings).
export const GATE_SCRIPT_NAMES: readonly string[] = [
  'email-send-gate.mjs',
  'self-pace-gate.mjs',
  'kanban-write-gate.mjs',
  'egress-gate.mjs',
  'outgoing-copy-gate.py',
]

export const GATE_SELF_TEST_INITIAL_DELAY_MS = 90_000
export const GATE_SELF_TEST_INTERVAL_MS = 15 * 60_000

export interface GateTarget { agent: string; settingsPath: string; projectDir: string }
export interface GateEntry extends GateTarget { matcher: string; command: string; timeoutSec: number; gate: string }
export interface GateRunResult {
  entry: GateEntry
  ok: boolean
  detail: string
  /** A deadline / kill / signal failure: worth one re-run before alerting (PROGI 6248 ii). */
  timeoutLike?: boolean
  /** The gate file (or a local module it loads) differs from git HEAD (BELA 6270): also re-checked once. */
  integrity?: boolean
}

/** Every settings file a gate can be wired in, with the project dir its session runs in. */
export function gateTargets(): GateTarget[] {
  const targets: GateTarget[] = [
    { agent: MAIN_AGENT_ID, settingsPath: agentSettingsPath(MAIN_AGENT_ID), projectDir: PROJECT_ROOT },
    { agent: MAIN_AGENT_ID, settingsPath: join(PROJECT_ROOT, '.claude', 'settings.json'), projectDir: PROJECT_ROOT },
  ]
  // The main session runs with CLAUDE_CONFIG_DIR=.channels-config when MAIN_AGENT_ISOLATED_CONFIG is on: its
  // user settings are that copy (refreshed from ~/.claude at each main-agent start), not ~/.claude.
  const isolated = join(PROJECT_ROOT, '.channels-config', 'settings.json')
  if (existsSync(isolated)) targets.push({ agent: MAIN_AGENT_ID, settingsPath: isolated, projectDir: PROJECT_ROOT })
  for (const name of listAgentNames()) {
    if (name === MAIN_AGENT_ID) continue
    targets.push({ agent: name, settingsPath: agentSettingsPath(name), projectDir: agentDir(name) })
  }
  return targets
}

/** The gate hooks wired in the PreToolUse section of each target's settings. Pure (reader injected). */
export function collectGateEntries(targets: readonly GateTarget[], read: (path: string) => string | null): GateEntry[] {
  return collectGateEntriesWithStatus(targets, read).entries
}

/** Same, plus the settings files that EXIST but could not be read or parsed this pass (PROGI 6278 b): their
 *  entries are unknown, not gone. `exists` is injectable for tests. */
export function collectGateEntriesWithStatus(
  targets: readonly GateTarget[],
  read: (path: string) => string | null,
  exists: (path: string) => boolean = existsSync,
): { entries: GateEntry[]; unreadable: Set<string> } {
  const out: GateEntry[] = []
  const unreadable = new Set<string>()
  for (const t of targets) {
    const raw = read(t.settingsPath)
    if (raw == null) {
      if (exists(t.settingsPath)) unreadable.add(t.settingsPath)
      continue
    }
    let settings: unknown
    // BELA 6300 / PROGI 6299: a leading UTF-8 BOM is dropped before the parse. Claude Code 2.1.326 loads the
    // hooks of a BOM-prefixed settings.json (measured, user and project level), so such a file is readable
    // to it and must not be reported 'unreadable' (JSON.parse alone throws on the BOM).
    try { settings = JSON.parse(raw.replace(/^\uFEFF/, '')) } catch { unreadable.add(t.settingsPath); continue }
    const ptu = (settings as { hooks?: { PreToolUse?: unknown } })?.hooks?.PreToolUse
    if (!Array.isArray(ptu)) continue
    for (const group of ptu as { matcher?: unknown; hooks?: unknown }[]) {
      if (!group || !Array.isArray(group.hooks)) continue
      const matcher = typeof group.matcher === 'string' ? group.matcher : ''
      for (const h of group.hooks as { type?: unknown; command?: unknown; timeout?: unknown }[]) {
        if (!h || h.type !== 'command' || typeof h.command !== 'string') continue
        const gate = GATE_SCRIPT_NAMES.find((g) => (h.command as string).includes(g))
        if (!gate) continue
        const timeoutSec = typeof h.timeout === 'number' && h.timeout > 0 ? h.timeout : 60
        out.push({ ...t, matcher, command: h.command, timeoutSec, gate })
      }
    }
  }
  return { entries: out, unreadable }
}

/** A benign tool call the matcher selects: the first candidate it matches, else a Bash `true` (which still
 *  proves the command line itself loads and runs). Pure. */
export function benignToolCall(matcher: string, cwd: string, port: number | string = WEB_PORT): { tool_name: string; tool_input: Record<string, unknown> } {
  const candidates: { tool_name: string; tool_input: Record<string, unknown> }[] = [
    { tool_name: 'Bash', tool_input: { command: 'true', description: 'gate self-test' } },
    // An address on the egress gate's built-in list (the local dashboard): the gate decides, nothing is fetched.
    { tool_name: 'WebFetch', tool_input: { url: `http://localhost:${port}/api/health`, prompt: 'gate self-test' } },
    { tool_name: 'mcp__plugin_telegram_telegram__reply', tool_input: { chat_id: '0', text: 'kapu onteszt' } },
    { tool_name: 'Write', tool_input: { file_path: join(cwd, '.gate-self-test.txt'), content: 'kapu onteszt' } },
  ]
  let re: RegExp | null = null
  try { re = new RegExp(`^(?:${matcher})$`) } catch { re = null }
  if (re) for (const c of candidates) if (re.test(c.tool_name)) return c
  return candidates[0]
}

/** ok = exit 0 and no deny / block decision on stdout. Pure. */
export function classifyGateRun(rc: number | null, stdout: string, stderr: string, signal: string | null = null): { ok: boolean; detail: string; timeoutLike: boolean } {
  const deny = /"permissionDecision"\s*:\s*"deny"/.test(stdout) || /"decision"\s*:\s*"block"/.test(stdout)
  const ok = rc === 0 && !deny
  if (ok) return { ok, detail: '', timeoutLike: false }
  // The fail-closed tail reports the gate's own status in its message: rc=124 is the wrapper deadline, 137
  // the kill after it; a null rc is our own kill or a signal.
  const timeoutLike = rc === null || signal != null || /\brc=(?:124|137)\b/.test(stderr)
  const last = stderr.split('\n').map((l) => l.trim()).filter(Boolean).pop() ?? ''
  const head = rc === null ? `jel=${signal ?? '?'}` : `rc=${rc}`
  return { ok, detail: `${head}${deny ? ', deny a stdout-on' : ''}${last ? `: ${last.slice(0, 300)}` : ''}`, timeoutLike }
}

/** The gate script path a hook command runs, with $CLAUDE_PROJECT_DIR expanded, or null. Pure. */
export function gateScriptPath(command: string, gate: string, projectDir: string): string | null {
  for (const raw of command.split(/\s+/)) {
    const token = raw.replace(/^['"]+/, '').replace(/;+$/, '').replace(/['"]+$/, '')
    if (!token.endsWith('/' + gate) && token !== gate) continue
    return token.replace(/\$\{?CLAUDE_PROJECT_DIR\}?/g, projectDir)
  }
  return null
}

// BELA 6270 / PROGI 6268: the benign call cannot tell a working gate from one whose content was swapped for
// a script that lets everything through (it passes the benign call too). So the gate file, and every local
// module it loads, is compared with its blob in git HEAD of the checkout it lives in: `git hash-object
// --no-filters` (the raw bytes, no attribute filter) against `git rev-parse HEAD:<path>`.
// A file outside any git checkout (an install that is not a clone) is not verifiable and is skipped; a file
// in a checkout but not in HEAD, or different from it, is a failing entry with its own effect text.
export interface GitIo { run: (cwd: string, args: readonly string[]) => string | null }
const GIT_ENV_DROP = ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_CEILING_DIRECTORIES']
export const REAL_GIT: GitIo = {
  run: (cwd, args) => {
    try {
      const env = { ...process.env }
      for (const k of GIT_ENV_DROP) delete env[k]
      return execFileSync('git', [...args], { cwd, env, encoding: 'utf-8', timeout: 5000, stdio: ['ignore', 'pipe', 'ignore'] }).trim()
    } catch {
      return null
    }
  },
}

const MJS_IMPORT_RX = /(?:^|[\s;])(?:import|export)\s[^'"`]*?from\s*['"](\.{1,2}\/[^'"]+)['"]|(?:^|[\s;])import\s*\(?\s*['"](\.{1,2}\/[^'"]+)['"]/gm
const PY_IMPORT_RX = /^\s*(?:from\s+([A-Za-z_][\w]*)\s+import\b|import\s+([A-Za-z_][\w]*(?:\s*,\s*[A-Za-z_][\w]*)*))/gm

/** The gate script and the local files it loads, recursively: relative static / dynamic imports of a JS
 *  module, same-directory modules of a Python script. Pure over `read` / `exists`. */
export function gateScriptClosure(
  script: string,
  read: (path: string) => string | null,
  exists: (path: string) => boolean = existsSync,
): string[] {
  const seen: string[] = []
  const todo = [script]
  while (todo.length) {
    const f = todo.shift() as string
    if (seen.includes(f)) continue
    seen.push(f)
    const src = read(f)
    if (src == null) continue
    const deps: string[] = []
    if (/\.(?:mjs|js|cjs)$/.test(f)) {
      for (const m of src.matchAll(MJS_IMPORT_RX)) deps.push(resolve(dirname(f), m[1] ?? m[2]))
    } else if (f.endsWith('.py')) {
      for (const m of src.matchAll(PY_IMPORT_RX)) {
        const names = m[1] ? [m[1]] : m[2].split(',').map((x) => x.trim())
        for (const n of names) deps.push(join(dirname(f), `${n}.py`))
      }
    }
    for (const d of deps) if (exists(d) && !seen.includes(d)) todo.push(d)
  }
  return seen
}

/** null: not verifiable (not in a git checkout). Otherwise ok, or the files that are not HEAD's content. */
export function checkGateIntegrity(
  script: string,
  git: GitIo = REAL_GIT,
  read: (path: string) => string | null = (p) => { try { return readFileSync(p, 'utf-8') } catch { return null } },
  exists: (path: string) => boolean = existsSync,
): { ok: boolean; detail: string } | null {
  let real = script
  try { real = realpathSync(script) } catch { /* checked by the caller: it exists */ }
  const top = git.run(dirname(real), ['rev-parse', '--show-toplevel'])
  if (!top) return null
  const bad: string[] = []
  for (const f of gateScriptClosure(real, read, exists)) {
    let fr = f
    try { fr = realpathSync(f) } catch { /* keep */ }
    const rel = relative(top, fr)
    if (rel.startsWith('..') || isAbsolute(rel)) { bad.push(`${fr} (a repon kivul)`); continue }
    const head = git.run(top, ['rev-parse', '--verify', '--quiet', `HEAD:${rel}`])
    if (!head) { bad.push(`${rel} (nincs a git HEAD-ben)`); continue }
    // BELA 6319 (A) / PROGI 6318: --no-filters, the hash of the RAW bytes. With --path the path's attributes
    // applied, so a clean filter set through .git/info/attributes + .git/config could hide a swapped gate (git
    // status / diff use the same filter, so they would not show it either) AND this test would run that
    // filter's command from .git/config every pass. No gate here has an eol / encoding attribute (.mjs, .py).
    const work = git.run(top, ['hash-object', '--no-filters', '--', fr])
    if (work !== head) bad.push(`${rel} (elter a git HEAD-tol)`)
  }
  if (!bad.length) return { ok: true, detail: '' }
  const files = bad.map((b) => b.replace(/ \(.*\)$/, ''))
  return {
    ok: false,
    detail: `a kapu tartalma nem a verziokezelt: ${bad.join(', ')} (repo: ${top}). Nezd meg: git -C ${top} diff HEAD ${files.join(' ')}; ha nem szandekos: git -C ${top} restore --source=HEAD ${files.join(' ')}`,
  }
}

/** Run one wired gate command exactly as written, with a benign call on stdin. Never throws. */
export function runGateCommand(
  entry: GateEntry,
  port: number | string = WEB_PORT,
  integrity?: (script: string) => { ok: boolean; detail: string } | null,
): Promise<GateRunResult> {
  const cwd = existsSync(entry.projectDir) ? entry.projectDir : PROJECT_ROOT
  const call = benignToolCall(entry.matcher, cwd, port)
  const input = JSON.stringify({
    session_id: 'gate-self-test', transcript_path: '/dev/null', cwd,
    hook_event_name: 'PreToolUse', ...call,
  })
  // PROGI 6248 (i): the main agent's Telegram arm exits 0 when its gate file is missing (a missing file must
  // not block the owner's only channel), so running it would pass silently; the file itself is checked.
  const script = gateScriptPath(entry.command, entry.gate, cwd)
  if (script != null && script.startsWith('/') && !existsSync(script)) {
    return Promise.resolve({ entry, ok: false, detail: `a kapu fajlja hianyzik: ${script}`, timeoutLike: false })
  }
  if (integrity && script != null && script.startsWith('/')) {
    let v: { ok: boolean; detail: string } | null = null
    try { v = integrity(script) } catch { v = null }
    if (v != null && !v.ok) return Promise.resolve({ entry, ok: false, detail: v.detail, timeoutLike: false, integrity: true })
  }
  return new Promise((resolve) => {
    let stdout = ''
    let stderr = ''
    let settled = false
    const finish = (rc: number | null, signal: string | null) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      const c = classifyGateRun(rc, stdout, stderr, signal)
      resolve({ entry, ok: c.ok, detail: c.detail, timeoutLike: c.timeoutLike })
    }
    const child = spawn('/bin/sh', ['-c', entry.command], {
      cwd,
      env: { ...process.env, CLAUDE_PROJECT_DIR: cwd },
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    // The hook's own timeout plus a margin: the fail-closed wrapper ends a hung gate 2 s inside it.
    const timer = setTimeout(() => { try { child.kill('SIGKILL') } catch { /* gone */ } }, (entry.timeoutSec + 3) * 1000)
    child.stdout.on('data', (d) => { if (stdout.length < 65_536) stdout += String(d) })
    child.stderr.on('data', (d) => { if (stderr.length < 65_536) stderr += String(d) })
    child.on('error', (err) => { stderr += String(err); finish(null, 'spawn-error') })
    child.on('close', (code, signal) => finish(code, signal))
    child.stdin.on('error', () => { /* a gate that exits before reading stdin */ })
    child.stdin.end(input)
  })
}

const resultKey = (e: GateEntry) => `${e.settingsPath}\u0000${e.matcher}\u0000${e.gate}`

/** State transitions since the last run. Pure: `prev` is the per-entry ok flag of the previous run. */
export function diffGateStates(prev: ReadonlyMap<string, boolean>, results: readonly GateRunResult[]): {
  newlyFailed: GateRunResult[]; recovered: GateRunResult[]; next: Map<string, boolean>
} {
  const next = new Map<string, boolean>()
  const newlyFailed: GateRunResult[] = []
  const recovered: GateRunResult[] = []
  for (const r of results) {
    const k = resultKey(r.entry)
    next.set(k, r.ok)
    const was = prev.get(k)
    if (!r.ok && was !== false) newlyFailed.push(r)
    else if (r.ok && was === false) recovered.push(r)
  }
  return { newlyFailed, recovered, next }
}

const isWrapped = (e: GateEntry) => e.command.includes(GATE_FAIL_CLOSED_TAG)

/** One alert text for the entries of one gate script that stopped passing. A fail-closed (wrapped) entry
 *  blocks every call it matches; an unwrapped one (the main agent's Telegram arm, fail-open by design) lets
 *  them through UNCHECKED (PROGI 6248 iii). The load average is given for a timeout-like failure (ii). */
export function gateFailureText(failed: readonly GateRunResult[], load: readonly number[] = loadavg()): string {
  const lines = failed.map((r) => {
    const effect = r.integrity
      ? 'ismeretlen: a kapu nem a verziokezelt kodot futtatja, akar mindent atengedhet'
      : isWrapped(r.entry)
        ? 'az illeszkedo hivasok TILTVA'
        : 'a kapu nem fut, a hivasok ellenorzes NELKUL mennek ki'
    return `- agens: ${r.entry.agent}, kapu: ${r.entry.gate} (matcher: ${r.entry.matcher || '(mind)'}), beallitas: ${r.entry.settingsPath}, ${r.detail}. Hatas: ${effect}.`
  })
  const timed = failed.some((r) => r.timeoutLike)
  const tampered = failed.some((r) => r.integrity)
  return [
    `[kapu-onteszt] KAPU HIBA: ${failed.length} bekotott kapu-bejegyzes egy artalmatlan hivason elbukott, vagy nem fut.${timed ? ` Idotullepes jellegu hiba (60 mp mulva ujrafuttatva is), load: ${load.map((x) => x.toFixed(2)).join(' ')}.` : ''}`,
    ...lines,
    tampered
      ? 'Teendo: a verziokezelttol eltero kapu-fajlt ellenorizni (ki es miert modositotta), es ha nem szandekos, visszaallitani a git HEAD tartalmara; ha szandekos, commitolni. 60 mp mulva ujraellenorizve is elter. A kovetkezo onteszt (15 percen belul) jelzi a helyreallast.'
      : 'Teendo: a kapu fajljat (vagy fuggoseget) javitani. Bash nelkul is megy: a fo agens Read/Edit/Write eszkoze nincs kapuzva. A kovetkezo onteszt (15 percen belul) jelzi a helyreallast.',
  ].join('\n')
}

export function gateRecoveryText(recovered: readonly GateRunResult[], vanished: readonly GateRunResult[] = []): string {
  const fmt = (rs: readonly GateRunResult[]) => rs.map((r) => `${r.entry.agent} / ${r.entry.gate} (${r.entry.matcher || '(mind)'})`).join('; ')
  const parts: string[] = []
  if (recovered.length) parts.push(`${fmt(recovered)}: az artalmatlan hivast ujra atengedik.`)
  // PROGI 6278 (c): an entry that simply disappeared did not "pass again".
  if (vanished.length) parts.push(`${fmt(vanished)}: a hibas bejegyzes mar nincs bekotve.`)
  return `[kapu-onteszt] HELYREALLT: ${parts.join(' ')}`
}

// PROGI 6248 (iv): BELA first, the owner if it stays (escalateToOwner), keyed per gate SCRIPT, so one broken
// gate file seen on every agent is one escalation.
// PROGI 6268 (the review of that): the stage-2 owner alert fires on escalateToOwner's own timer, 5 min by
// default, while this test looks only every 15 min, so practically every gate failure reached the owner even
// when BELA fixed it in a minute. The gate keys get GATE_STAGE2_EXTRA_MS (20 min = the 15 min cycle + the
// 60 s re-run + margin): a fix within one cycle is seen and cleared before the owner is paged; a persistent
// break still reaches the owner in ~20 min. (A 2-3 min re-check loop while open was the alternative; it adds
// a second timer path and more process spawns exactly during an incident, for a faster all-clear only.)
// When the owner WAS paged, the all-clear goes to the owner too.
export const GATE_STAGE2_EXTRA_MS = 20 * 60_000

export interface GateAlerter {
  /** stage2ExtraMs: how long until the owner is paged (a re-raise keeps the original deadline, 6278 a). */
  fail: (key: string, text: string, stage2ExtraMs?: number) => void
  recover: (key: string, text: string) => void
}
const escalatingAlerter: GateAlerter = {
  fail: (key, text, stage2ExtraMs = GATE_STAGE2_EXTRA_MS) => escalateToOwner({
    key,
    belaText: text,
    ownerText: `⚠️ ${text}\nBÉLA mar ertesitve volt errol, de nem oldodott meg.`,
    stage2ExtraMs,
  }),
  recover: (key, text) => {
    const ownerWasTold = ownerEscalationStage2Sent(key)
    clearOwnerEscalation(key)
    createAgentMessage('system', MAIN_AGENT_ID, text, 'gate self-test')
    if (ownerWasTold) void notifyChannel(text).catch(() => { /* best effort: BELA has it */ })
  },
}
const sendAlerter = (send: (text: string) => void): GateAlerter => ({ fail: (_k, t) => send(t), recover: (_k, t) => send(t) })

export const GATE_SELF_TEST_RETRY_DELAY_MS = 60_000

let lastStates = new Map<string, boolean>()
// Open gate escalations: gate script -> the failing entries (by resultKey) it was raised for, and their results.
let openGates = new Map<string, { keys: Set<string>; results: GateRunResult[]; raisedAt: number }>()
// PROGI 6288: settings files unreadable in consecutive passes (path -> streak), and those escalated.
let unreadableStreak = new Map<string, number>()
const unreadableEscalated = new Set<string>()
export const UNREADABLE_SETTINGS_PASSES = 2

/** A settings file that exists but cannot be read / parsed for UNREADABLE_SETTINGS_PASSES passes in a row is
 *  itself escalated (BELA first, the owner if it stays): Claude Code then either does not start or runs that
 *  agent WITHOUT its hooks, every gate fail-open. One pass is tolerated (a write caught mid-way). Pure over
 *  its inputs: returns the paths to raise and to clear, and the next streak map. */
export function decideUnreadableSettings(
  prevStreak: ReadonlyMap<string, number>, escalated: ReadonlySet<string>, unreadableNow: ReadonlySet<string>,
): { raise: string[]; clear: string[]; next: Map<string, number> } {
  const next = new Map<string, number>()
  for (const p of unreadableNow) next.set(p, (prevStreak.get(p) ?? 0) + 1)
  const raise = [...next].filter(([p, n]) => n >= UNREADABLE_SETTINGS_PASSES && !escalated.has(p)).map(([p]) => p)
  const clear = [...escalated].filter((p) => !unreadableNow.has(p))
  return { raise, clear, next }
}

/** PROGI 6268 (a)(b): reconcile the open gate escalations with THIS pass's results. Pure over its inputs:
 *  - a gate with no failing entry now -> recover (also when its failing entry simply disappeared: an agent
 *    removed, a matcher repaired; before, only a per-entry "recovered" transition cleared the key, and a
 *    later failure of the same gate on another agent was swallowed by the still-open key);
 *  - a gate failing now that has no open escalation -> fail;
 *  - an open gate where an entry fails that was not part of the escalation -> fail again (the caller clears
 *    first), with the full current list. */
export function reconcileGateEscalations(
  open: ReadonlyMap<string, { keys: ReadonlySet<string>; results: readonly GateRunResult[] }>,
  results: readonly GateRunResult[],
  unreadable: ReadonlySet<string> = new Set(),
): {
  fail: { gate: string; results: GateRunResult[]; reraise: boolean }[]
  recover: { gate: string; results: GateRunResult[]; recovered: GateRunResult[]; vanished: GateRunResult[] }[]
} {
  const failingByGate = new Map<string, GateRunResult[]>()
  for (const r of results) if (!r.ok) failingByGate.set(r.entry.gate, [...(failingByGate.get(r.entry.gate) ?? []), r])
  const nowByKey = new Map(results.map((r) => [resultKey(r.entry), r]))
  const fail: { gate: string; results: GateRunResult[]; reraise: boolean }[] = []
  const recover: { gate: string; results: GateRunResult[]; recovered: GateRunResult[]; vanished: GateRunResult[] }[] = []
  for (const [gate, o] of open) {
    if (failingByGate.has(gate)) continue
    // PROGI 6278 (b): an entry whose settings file could not be read this pass is unknown, not gone.
    if (o.results.some((r) => unreadable.has(r.entry.settingsPath) && !nowByKey.has(resultKey(r.entry)))) continue
    const recovered = o.results.filter((r) => nowByKey.has(resultKey(r.entry)))
    const vanished = o.results.filter((r) => !nowByKey.has(resultKey(r.entry)))
    recover.push({ gate, results: [...o.results], recovered, vanished })
  }
  for (const [gate, failing] of failingByGate) {
    const o = open.get(gate)
    if (!o) fail.push({ gate, results: failing, reraise: false })
    else if (failing.some((r) => !o.keys.has(resultKey(r.entry)))) fail.push({ gate, results: failing, reraise: true })
  }
  return { fail, recover }
}
let running = false

/** One self-test pass. Exported for tests (targets, reader, alerter / sender and the re-run wait injectable). */
export async function runGateSelfTest(opts: {
  targets?: GateTarget[]
  read?: (path: string) => string | null
  send?: (text: string) => void
  alerter?: GateAlerter
  port?: number | string
  retryDelayMs?: number
  load?: () => number[]
  exists?: (path: string) => boolean
  now?: () => number
  /** BELA 6270: the git layer of the integrity check (injectable); null switches the check off. */
  git?: GitIo | null
} = {}): Promise<GateRunResult[]> {
  if (running) return []
  running = true
  try {
    const read = opts.read ?? ((p: string) => { try { return readFileSync(p, 'utf-8') } catch { return null } })
    const { entries, unreadable } = collectGateEntriesWithStatus(opts.targets ?? gateTargets(), read, opts.exists)
    const results: GateRunResult[] = []
    // BELA 6270: one integrity verdict per gate script per pass (the same file is wired in many settings).
    const git = opts.git === undefined ? REAL_GIT : opts.git
    const verdicts = new Map<string, { ok: boolean; detail: string } | null>()
    const integrity = git == null ? undefined : (script: string) => {
      if (!verdicts.has(script)) verdicts.set(script, checkGateIntegrity(script, git, undefined, opts.exists))
      return verdicts.get(script) ?? null
    }
    // Sequential on purpose: ~30 short processes every 15 minutes, never a burst on a loaded host.
    for (const e of entries) results.push(await runGateCommand(e, opts.port, integrity))
    // PROGI 6248 (ii): a timeout-like failure (deadline, kill, signal) on a loaded host is given one more
    // chance 60 s later; only a second failure counts. BELA 6270: an integrity mismatch too (a pass that
    // caught a checkout / cherry-pick between its file writes and its HEAD move), with a fresh verdict.
    const timed = results.map((r, i) => (!r.ok && (r.timeoutLike || r.integrity) ? i : -1)).filter((i) => i >= 0)
    if (timed.length) {
      logger.warn({ count: timed.length, load: loadavg() }, 'gate self-test: timeout-like or integrity gate failures, one re-run in 60 s before alerting')
      await new Promise((r) => setTimeout(r, opts.retryDelayMs ?? GATE_SELF_TEST_RETRY_DELAY_MS))
      verdicts.clear()
      for (const i of timed) results[i] = await runGateCommand(results[i].entry, opts.port, integrity)
    }
    const d = diffGateStates(lastStates, results)
    lastStates = d.next
    if (d.newlyFailed.length) {
      logger.error({ failed: d.newlyFailed.map((r) => ({ agent: r.entry.agent, gate: r.entry.gate, matcher: r.entry.matcher, detail: r.detail })) }, 'gate self-test: a wired gate no longer passes a benign call')
    }
    const alerter = opts.alerter ?? (opts.send ? sendAlerter(opts.send) : escalatingAlerter)
    if (unreadable.size) logger.warn({ unreadable: [...unreadable] }, 'gate self-test: settings file(s) unreadable this pass; their open escalations are kept')
    {
      const alerterU = opts.alerter ?? (opts.send ? sendAlerter(opts.send) : escalatingAlerter)
      const agentOf = new Map((opts.targets ?? gateTargets()).map((t) => [t.settingsPath, t.agent]))
      const u = decideUnreadableSettings(unreadableStreak, unreadableEscalated, unreadable)
      unreadableStreak = u.next
      for (const path of u.raise) {
        unreadableEscalated.add(path)
        const text = `[kapu-onteszt] OLVASHATATLAN SETTINGS: agens ${agentOf.get(path) ?? '?'}, fajl ${path}: ${UNREADABLE_SETTINGS_PASSES} egymast koveto korben nem olvashato vagy nem ervenyes JSON. A Claude Code ilyenkor vagy nem indul, vagy a hookjai NELKUL fut: annal az agensnel minden kapu fail-open. Teendo: a fajl javitasa (a sub-agens settings-et a kovetkezo spawn ujrageneralja; a fo agense kezzel / git-bol).`
        logger.error({ path }, 'gate self-test: settings file unreadable in consecutive passes -- escalating')
        try { alerterU.fail(`gate-settings-unreadable:${path}`, text) } catch (err) { logger.warn({ err }, 'gate self-test: alert send failed') }
      }
      for (const path of u.clear) {
        unreadableEscalated.delete(path)
        try { alerterU.recover(`gate-settings-unreadable:${path}`, `[kapu-onteszt] HELYREALLT: ${path} ujra olvashato (vagy mar nincs meg).`) } catch (err) { logger.warn({ err }, 'gate self-test: recovery send failed') }
      }
    }
    const rec = reconcileGateEscalations(openGates, results, unreadable)
    for (const r of rec.recover) {
      openGates.delete(r.gate)
      logger.info({ gate: r.gate, vanished: r.vanished.length }, 'gate self-test: gate recovered (no failing entry left)')
      try { alerter.recover(`gate-self-test:${r.gate}`, gateRecoveryText(r.recovered, r.vanished)) } catch (err) { logger.warn({ err }, 'gate self-test: recovery send failed') }
    }
    if (rec.fail.length) {
      const load = opts.load ? opts.load() : loadavg()
      const nowMs = opts.now ? opts.now() : Date.now()
      for (const f of rec.fail) {
        const key = `gate-self-test:${f.gate}`
        // An open escalation only refreshes its text in escalateToOwner: clear it first so the new entry is told.
        // PROGI 6278 (a): the re-raise keeps the ORIGINAL owner deadline (20 min from the first raise), or a
        // new failing entry every pass would push the owner page out forever.
        const prevRaisedAt = f.reraise ? openGates.get(f.gate)?.raisedAt : undefined
        const raisedAt = prevRaisedAt ?? nowMs
        const stage2 = Math.max(0, GATE_STAGE2_EXTRA_MS - (nowMs - raisedAt))
        if (f.reraise) { try { clearOwnerEscalation(key) } catch { /* not open */ } }
        openGates.set(f.gate, { keys: new Set(f.results.map((r) => resultKey(r.entry))), results: f.results, raisedAt })
        try { alerter.fail(key, gateFailureText(f.results, load), stage2) } catch (err) { logger.warn({ err }, 'gate self-test: alert send failed') }
      }
    }
    return results
  } finally {
    running = false
  }
}

const prunedKeptAlerted = new Set<string>()

/** The boot prune kept a fail-closed gate whose file is missing at the live root: it denies every call it
 *  matches. Told to the main agent once per entry per process lifetime (the prune runs at boot). */
export function reportPruneKept(agent: string, settingsPath: string, commands: readonly string[], send?: (text: string) => void): void {
  const fresh = commands.filter((c) => !prunedKeptAlerted.has(`${settingsPath}\u0000${c}`))
  if (!fresh.length) return
  for (const c of fresh) prunedKeptAlerted.add(`${settingsPath}\u0000${c}`)
  const gates = fresh.map((c) => GATE_SCRIPT_NAMES.find((g) => c.includes(g)) ?? c.slice(0, 80))
  const text = `[kapu-onteszt] HIANYZO KAPU-FAJL: agens ${agent}, beallitas ${settingsPath}, kapu: ${gates.join(', ')}. A bejegyzes megmaradt (fail-closed), igy az illeszkedo hivasok TILTVA, amig a fajl vissza nem kerul. Teendo: a fajl visszaallitasa (git), majd dashboard ujrainditas.`
  try {
    const alerter = send ? sendAlerter(send) : escalatingAlerter
    alerter.fail(`gate-prune-kept:${settingsPath}`, text)
  } catch (err) {
    logger.warn({ err }, 'gate self-test: prune-kept alert send failed')
  }
}

/** Test hook: forget the per-process state. */
export function resetGateSelfTestState(): void {
  lastStates = new Map()
  openGates = new Map()
  unreadableStreak = new Map()
  unreadableEscalated.clear()
  running = false
  prunedKeptAlerted.clear()
}

export function startGateSelfTest(): NodeJS.Timeout {
  setTimeout(() => { void runGateSelfTest().catch((err) => logger.warn({ err }, 'gate self-test failed')) }, GATE_SELF_TEST_INITIAL_DELAY_MS).unref()
  return setInterval(() => { void runGateSelfTest().catch((err) => logger.warn({ err }, 'gate self-test failed')) }, GATE_SELF_TEST_INTERVAL_MS)
}

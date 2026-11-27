import { spawn } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { PROJECT_ROOT, MAIN_AGENT_ID, WEB_PORT } from '../config.js'
import { logger } from '../logger.js'
import { createAgentMessage } from '../db.js'
import { agentDir, listAgentNames } from './agent-config.js'
import { agentSettingsPath } from './agent-scaffold.js'

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
export interface GateRunResult { entry: GateEntry; ok: boolean; detail: string }

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
  const out: GateEntry[] = []
  for (const t of targets) {
    const raw = read(t.settingsPath)
    if (raw == null) continue
    let settings: unknown
    try { settings = JSON.parse(raw) } catch { continue }
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
  return out
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
export function classifyGateRun(rc: number | null, stdout: string, stderr: string, signal: string | null = null): { ok: boolean; detail: string } {
  const deny = /"permissionDecision"\s*:\s*"deny"/.test(stdout) || /"decision"\s*:\s*"block"/.test(stdout)
  const ok = rc === 0 && !deny
  if (ok) return { ok, detail: '' }
  const last = stderr.split('\n').map((l) => l.trim()).filter(Boolean).pop() ?? ''
  const head = rc === null ? `jel=${signal ?? '?'}` : `rc=${rc}`
  return { ok, detail: `${head}${deny ? ', deny a stdout-on' : ''}${last ? `: ${last.slice(0, 300)}` : ''}` }
}

/** Run one wired gate command exactly as written, with a benign call on stdin. Never throws. */
export function runGateCommand(entry: GateEntry, port: number | string = WEB_PORT): Promise<GateRunResult> {
  const cwd = existsSync(entry.projectDir) ? entry.projectDir : PROJECT_ROOT
  const call = benignToolCall(entry.matcher, cwd, port)
  const input = JSON.stringify({
    session_id: 'gate-self-test', transcript_path: '/dev/null', cwd,
    hook_event_name: 'PreToolUse', ...call,
  })
  return new Promise((resolve) => {
    let stdout = ''
    let stderr = ''
    let settled = false
    const finish = (rc: number | null, signal: string | null) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      const c = classifyGateRun(rc, stdout, stderr, signal)
      resolve({ entry, ok: c.ok, detail: c.detail })
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

export function gateFailureText(failed: readonly GateRunResult[]): string {
  const lines = failed.map((r) => `- agens: ${r.entry.agent}, kapu: ${r.entry.gate} (matcher: ${r.entry.matcher || '(mind)'}), beallitas: ${r.entry.settingsPath}, ${r.detail}`)
  return [
    `[kapu-onteszt] KAPU HIBA: ${failed.length} bekotott kapu egy artalmatlan hivast sem enged at, vagy nem fut. Az erintett agens minden ilyen hivasa TILTVA, amig ez fennall.`,
    ...lines,
    'Teendo: a kapu fajljat (vagy fuggoseget) javitani. Bash nelkul is megy: a fo agens Read/Edit/Write eszkoze nincs kapuzva. A kovetkezo onteszt (15 percen belul) jelzi a helyreallast.',
  ].join('\n')
}

export function gateRecoveryText(recovered: readonly GateRunResult[]): string {
  const list = recovered.map((r) => `${r.entry.agent} / ${r.entry.gate} (${r.entry.matcher || '(mind)'})`).join('; ')
  return `[kapu-onteszt] HELYREALLT: ${list}. Az artalmatlan hivast ujra atengedik.`
}

type Send = (text: string) => void
const sendToMain: Send = (text) => { createAgentMessage('system', MAIN_AGENT_ID, text, 'gate self-test') }

let lastStates = new Map<string, boolean>()
let running = false

/** One self-test pass. Exported for tests (targets, reader and sender injectable). */
export async function runGateSelfTest(opts: {
  targets?: GateTarget[]
  read?: (path: string) => string | null
  send?: Send
  port?: number | string
} = {}): Promise<GateRunResult[]> {
  if (running) return []
  running = true
  try {
    const read = opts.read ?? ((p: string) => { try { return readFileSync(p, 'utf-8') } catch { return null } })
    const entries = collectGateEntries(opts.targets ?? gateTargets(), read)
    const results: GateRunResult[] = []
    // Sequential on purpose: ~30 short processes every 15 minutes, never a burst on a loaded host.
    for (const e of entries) results.push(await runGateCommand(e, opts.port))
    const d = diffGateStates(lastStates, results)
    lastStates = d.next
    const send = opts.send ?? sendToMain
    if (d.newlyFailed.length) {
      logger.error({ failed: d.newlyFailed.map((r) => ({ agent: r.entry.agent, gate: r.entry.gate, matcher: r.entry.matcher, detail: r.detail })) }, 'gate self-test: a wired gate no longer passes a benign call')
      try { send(gateFailureText(d.newlyFailed)) } catch (err) { logger.warn({ err }, 'gate self-test: alert send failed') }
    }
    if (d.recovered.length) {
      logger.info({ recovered: d.recovered.map((r) => ({ agent: r.entry.agent, gate: r.entry.gate })) }, 'gate self-test: gate recovered')
      try { send(gateRecoveryText(d.recovered)) } catch (err) { logger.warn({ err }, 'gate self-test: recovery send failed') }
    }
    return results
  } finally {
    running = false
  }
}

const prunedKeptAlerted = new Set<string>()

/** The boot prune kept a fail-closed gate whose file is missing at the live root: it denies every call it
 *  matches. Told to the main agent once per entry per process lifetime (the prune runs at boot). */
export function reportPruneKept(agent: string, settingsPath: string, commands: readonly string[], send: Send = sendToMain): void {
  const fresh = commands.filter((c) => !prunedKeptAlerted.has(`${settingsPath}\u0000${c}`))
  if (!fresh.length) return
  for (const c of fresh) prunedKeptAlerted.add(`${settingsPath}\u0000${c}`)
  const gates = fresh.map((c) => GATE_SCRIPT_NAMES.find((g) => c.includes(g)) ?? c.slice(0, 80))
  try {
    send(`[kapu-onteszt] HIANYZO KAPU-FAJL: agens ${agent}, beallitas ${settingsPath}, kapu: ${gates.join(', ')}. A bejegyzes megmaradt (fail-closed), igy az illeszkedo hivasok TILTVA, amig a fajl vissza nem kerul. Teendo: a fajl visszaallitasa (git), majd dashboard ujrainditas.`)
  } catch (err) {
    logger.warn({ err }, 'gate self-test: prune-kept alert send failed')
  }
}

/** Test hook: forget the per-process state. */
export function resetGateSelfTestState(): void {
  lastStates = new Map()
  running = false
  prunedKeptAlerted.clear()
}

export function startGateSelfTest(): NodeJS.Timeout {
  setTimeout(() => { void runGateSelfTest().catch((err) => logger.warn({ err }, 'gate self-test failed')) }, GATE_SELF_TEST_INITIAL_DELAY_MS).unref()
  return setInterval(() => { void runGateSelfTest().catch((err) => logger.warn({ err }, 'gate self-test failed')) }, GATE_SELF_TEST_INTERVAL_MS)
}

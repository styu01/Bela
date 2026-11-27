import { describe, it, expect, beforeEach, afterAll } from 'vitest'
import { mkdtempSync, writeFileSync, rmSync, readFileSync, cpSync, appendFileSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  collectGateEntries, benignToolCall, classifyGateRun, diffGateStates, runGateCommand, runGateSelfTest,
  reportPruneKept, resetGateSelfTestState, gateFailureText, type GateEntry, type GateTarget,
} from '../web/gate-self-test.js'
import { hookCommand, GATE_FAIL_CLOSED_TAG, injectEmailSendGate, injectSelfPaceGate, injectKanbanWriteGate, injectEgressGate } from '../web/agent-scaffold.js'
import { PROJECT_ROOT } from '../config.js'

// BELA 6223 a: every wired gate command is run exactly as the settings file holds it, with a benign call;
// a gate that stops passing it is told to the main agent once per state change.

const tmp = mkdtempSync(join(tmpdir(), 'gate-self-test-'))
afterAll(() => rmSync(tmp, { recursive: true, force: true }))
const read = (p: string) => { try { return readFileSync(p, 'utf-8') } catch { return null } }
const settingsWith = (entries: { matcher: string; command: string; timeout?: number }[]) =>
  JSON.stringify({ hooks: { PreToolUse: entries.map((e) => ({ matcher: e.matcher, hooks: [{ type: 'command', command: e.command, timeout: e.timeout ?? 10 }] })) } })
const gate = (name: string, body: string) => { const p = join(tmp, name); writeFileSync(p, body); return p }

describe('collectGateEntries', () => {
  it('picks the gate hooks only, from every target, with their matcher and timeout; bad files are skipped', () => {
    const a = join(tmp, 'a.json'); const b = join(tmp, 'b.json'); const bad = join(tmp, 'bad.json')
    writeFileSync(a, JSON.stringify({ hooks: {
      PreToolUse: [
        { matcher: 'Bash', hooks: [{ type: 'command', command: 'node "/x/scripts/self-pace-gate.mjs"', timeout: 10 }, { type: 'command', command: 'python3 /x/other.py' }] },
        { matcher: 'Read', hooks: [{ type: 'command', command: '/x/channel-image-resize.sh' }] },
      ],
      UserPromptSubmit: [{ hooks: [{ type: 'command', command: 'node /x/scripts/email-send-gate.mjs' }] }],
    } }))
    writeFileSync(b, settingsWith([{ matcher: 'WebFetch', command: 'node "/x/scripts/hooks/egress-gate.mjs"', timeout: 15 }]))
    writeFileSync(bad, '{ not json')
    const targets: GateTarget[] = [
      { agent: 'progi', settingsPath: a, projectDir: tmp }, { agent: 'bela', settingsPath: b, projectDir: tmp },
      { agent: 'x', settingsPath: bad, projectDir: tmp }, { agent: 'y', settingsPath: join(tmp, 'missing.json'), projectDir: tmp },
    ]
    const es = collectGateEntries(targets, read)
    expect(es.map((e) => [e.agent, e.gate, e.matcher, e.timeoutSec])).toEqual([
      ['progi', 'self-pace-gate.mjs', 'Bash', 10],
      ['bela', 'egress-gate.mjs', 'WebFetch', 15],
    ])
  })
})

describe('benignToolCall', () => {
  it('the first candidate the matcher selects; Bash otherwise (and for a bad regex)', () => {
    expect(benignToolCall('Bash|.*send_email.*|.*manage_email.*', tmp).tool_name).toBe('Bash')
    const w = benignToolCall('WebFetch', tmp, 3420)
    expect(w.tool_name).toBe('WebFetch')
    expect(w.tool_input.url).toBe('http://localhost:3420/api/health')
    expect(benignToolCall('mcp__plugin_telegram_telegram__(reply|edit_message)$', tmp).tool_name).toBe('mcp__plugin_telegram_telegram__reply')
    expect(benignToolCall('mcp__.*[Gg]mail__(draft_email|reply)$', tmp).tool_name).toBe('Bash')
    expect(benignToolCall('(', tmp).tool_name).toBe('Bash')
    expect(benignToolCall('Write|Edit', tmp).tool_name).toBe('Write')
  })
})

describe('classifyGateRun', () => {
  it('ok only on rc 0 without a deny / block decision; the detail names rc and the last stderr line', () => {
    expect(classifyGateRun(0, '', '').ok).toBe(true)
    expect(classifyGateRun(0, '{"hookSpecificOutput":{"permissionDecision":"deny"}}', '').ok).toBe(false)
    expect(classifyGateRun(0, '{"decision":"block"}', '').ok).toBe(false)
    const f = classifyGateRun(2, '', 'SyntaxError: x\n\ngovernance-kapu (fail-closed): g.mjs: ...\n')
    expect(f.ok).toBe(false)
    expect(f.detail).toBe('rc=2: governance-kapu (fail-closed): g.mjs: ...')
    expect(classifyGateRun(null, '', '', 'SIGKILL').detail).toBe('jel=SIGKILL')
  })
})

describe('diffGateStates', () => {
  const e = (m: string): GateEntry => ({ agent: 'a', settingsPath: 's', projectDir: tmp, matcher: m, command: 'c', timeoutSec: 10, gate: 'self-pace-gate.mjs' })
  it('first sight of a failure and ok->fail alert; fail->fail is quiet; fail->ok recovers; ok->ok is quiet', () => {
    let d = diffGateStates(new Map(), [{ entry: e('A'), ok: false, detail: '' }, { entry: e('B'), ok: true, detail: '' }])
    expect(d.newlyFailed.map((r) => r.entry.matcher)).toEqual(['A'])
    d = diffGateStates(d.next, [{ entry: e('A'), ok: false, detail: '' }, { entry: e('B'), ok: false, detail: '' }])
    expect(d.newlyFailed.map((r) => r.entry.matcher)).toEqual(['B'])
    d = diffGateStates(d.next, [{ entry: e('A'), ok: true, detail: '' }, { entry: e('B'), ok: false, detail: '' }])
    expect(d.recovered.map((r) => r.entry.matcher)).toEqual(['A'])
    expect(d.newlyFailed).toEqual([])
  })
})

describe('runGateCommand on stand-in gates (built with the real fail-closed builder)', () => {
  const entry = (command: string, timeoutSec = 10): GateEntry => ({ agent: 'progi', settingsPath: 's', projectDir: tmp, matcher: 'Bash', command, timeoutSec, gate: 'self-pace-gate.mjs' })
  it('a healthy gate passes', async () => {
    expect((await runGateCommand(entry(hookCommand(gate('ok.mjs', 'process.exit(0)'))))).ok).toBe(true)
  })
  it('a syntax error in the gate file fails, and the detail carries the fail-closed line', async () => {
    const r = await runGateCommand(entry(hookCommand(gate('broken.mjs', 'const = ;'))))
    expect(r.ok).toBe(false)
    expect(r.detail).toContain('rc=2')
    expect(r.detail).toContain(GATE_FAIL_CLOSED_TAG)
  })
  it('a gate that denies the benign call fails', async () => {
    const r = await runGateCommand(entry(hookCommand(gate('deny.mjs', 'process.stdout.write(JSON.stringify({hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny"}}))'))))
    expect(r.ok).toBe(false)
  })
  it('the benign call reaches the gate on stdin', async () => {
    const p = gate('echo.mjs', 'let s="";process.stdin.on("data",d=>s+=d);process.stdin.on("end",()=>{const j=JSON.parse(s);process.exit(j.tool_name==="Bash"&&j.tool_input.command==="true"&&j.hook_event_name==="PreToolUse"?0:3)})')
    expect((await runGateCommand(entry(hookCommand(p)))).ok).toBe(true)
  })
  it('a hung gate is ended (by the wrapper deadline) and fails', async () => {
    const r = await runGateCommand(entry(hookCommand(gate('hang.mjs', 'setTimeout(()=>{},60000)'), [], 3), 3))
    expect(r.ok).toBe(false)
  }, 15_000)
})

describe('runGateSelfTest end to end (temp settings, injected sender)', () => {
  beforeEach(() => resetGateSelfTestState())
  it('alerts once when a gate breaks, stays quiet while broken, and reports the recovery', async () => {
    mkdirSync(join(tmp, 'e2e'), { recursive: true })
    const g = gate('e2e/self-pace-gate.mjs', 'process.exit(0)')
    const sp = join(tmp, 'e2e.json')
    writeFileSync(sp, settingsWith([{ matcher: 'Bash|Write', command: hookCommand(g) }]))
    const targets = [{ agent: 'okoska', settingsPath: sp, projectDir: tmp }]
    const sent: string[] = []
    const send = (t: string) => { sent.push(t) }
    await runGateSelfTest({ targets, send })
    expect(sent).toEqual([])
    writeFileSync(g, 'const = ;')
    await runGateSelfTest({ targets, send })
    expect(sent.length).toBe(1)
    expect(sent[0]).toContain('[kapu-onteszt] KAPU HIBA')
    expect(sent[0]).toContain('agens: okoska')
    await runGateSelfTest({ targets, send })
    expect(sent.length).toBe(1)
    writeFileSync(g, 'process.exit(0)')
    await runGateSelfTest({ targets, send })
    expect(sent.length).toBe(2)
    expect(sent[1]).toContain('[kapu-onteszt] HELYREALLT: okoska')
  })
})

describe('the real gates, as the injectors wire them, on a disposable copy', () => {
  beforeEach(() => resetGateSelfTestState())
  it('every injected gate passes its benign call; a broken self-pace-gate copy is reported', async () => {
    const root = join(tmp, 'install'); mkdirSync(join(root, 'store'), { recursive: true })
    cpSync(join(PROJECT_ROOT, 'scripts'), join(root, 'scripts'), { recursive: true })
    const s: Record<string, unknown> = {}
    injectEmailSendGate(s); injectSelfPaceGate(s); injectKanbanWriteGate(s); injectEgressGate(s)
    // point every command at the copy
    const json = JSON.stringify(s).split(JSON.stringify(PROJECT_ROOT).slice(1, -1)).join(JSON.stringify(root).slice(1, -1))
    const sp = join(tmp, 'real.json'); writeFileSync(sp, json)
    const targets = [{ agent: 'progi', settingsPath: sp, projectDir: root }]
    const sent: string[] = []
    const res = await runGateSelfTest({ targets, send: (t) => { sent.push(t) }, port: 3420 })
    expect(res.map((r) => r.entry.gate).sort()).toEqual(['egress-gate.mjs', 'email-send-gate.mjs', 'kanban-write-gate.mjs', 'self-pace-gate.mjs'])
    expect(res.filter((r) => !r.ok).map((r) => `${r.entry.gate}: ${r.detail}`)).toEqual([])
    expect(sent).toEqual([])
    appendFileSync(join(root, 'scripts', 'self-pace-gate.mjs'), '\nconst = ;\n')
    await runGateSelfTest({ targets, send: (t) => { sent.push(t) }, port: 3420 })
    expect(sent.length).toBe(1)
    expect(sent[0]).toContain('kapu: self-pace-gate.mjs')
    expect(sent[0]).toContain('agens: progi')
  }, 60_000)

  it('the main agent project settings (outgoing-copy-gate arms) pass their benign calls', async () => {
    const targets = [{ agent: 'bela', settingsPath: join(PROJECT_ROOT, '.claude', 'settings.json'), projectDir: PROJECT_ROOT }]
    const sent: string[] = []
    const res = await runGateSelfTest({ targets, send: (t) => { sent.push(t) } })
    expect(res.length).toBe(4)
    expect(res.filter((r) => !r.ok).map((r) => r.detail)).toEqual([])
  }, 60_000)
})

describe('reportPruneKept', () => {
  beforeEach(() => resetGateSelfTestState())
  it('tells the main agent once per kept entry', () => {
    const sent: string[] = []
    const cmd = hookCommand('/opt/app/scripts/email-send-gate.mjs')
    reportPruneKept('iris', '/s.json', [cmd], (t) => { sent.push(t) })
    reportPruneKept('iris', '/s.json', [cmd], (t) => { sent.push(t) })
    expect(sent.length).toBe(1)
    expect(sent[0]).toContain('HIANYZO KAPU-FAJL: agens iris')
    expect(sent[0]).toContain('kapu: email-send-gate.mjs')
  })
})

describe('texts', () => {
  it('no em dash and no " -- " in what goes to the main agent', () => {
    const e: GateEntry = { agent: 'a', settingsPath: 's', projectDir: tmp, matcher: 'Bash', command: 'c', timeoutSec: 10, gate: 'g' }
    const t = gateFailureText([{ entry: e, ok: false, detail: 'rc=2: x' }])
    expect(t).not.toMatch(/—| -- /)
    expect(t).toContain('Read/Edit/Write eszkoze nincs kapuzva')
  })
})

describe('wiring', () => {
  const web = readFileSync(join(__dirname, '..', 'web.ts'), 'utf-8')
  it('started only inside the hookDecision.register branch, after the gate backfill, and cleared on close', () => {
    const reg = web.indexOf('if (!hookDecision.register) {')
    const backfill = web.indexOf("logger.warn({ err }, 'Agent hook backfill skipped')")
    const start = web.indexOf('gateSelfTestInterval = startGateSelfTest()')
    const seed = web.indexOf('ensureDefaultScheduledTasks()\n')
    expect(reg).toBeGreaterThan(0)
    expect(start).toBeGreaterThan(backfill)
    expect(start).toBeLessThan(seed)
    expect(web).toContain('if (gateSelfTestInterval) clearInterval(gateSelfTestInterval)')
  })
  it('the boot prune hands its kept entries to reportPruneKept', () => {
    expect(web).toMatch(/pruneStaleHooksFromSettingsFile\(agentSettingsPath\(agentName\), PROJECT_ROOT,\s*\(kept\) => reportPruneKept\(agentName, agentSettingsPath\(agentName\), kept\)\)/)
  })
})

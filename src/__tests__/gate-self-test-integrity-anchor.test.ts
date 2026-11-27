import { describe, it, expect, beforeEach, afterAll, afterEach, vi } from 'vitest'
import { mkdtempSync, writeFileSync, rmSync, mkdirSync, chmodSync, realpathSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { tmpdir } from 'node:os'
import { execFileSync } from 'node:child_process'
import {
  checkGateIntegrity, detectGitAnchor, runGateSelfTest, resetGateSelfTestState, startGateSelfTest, REAL_GIT,
  unverifiableKey, gateUnverifiableText, gateUnverifiableRecoveryText, reconcileUnverifiable, GATE_STAGE2_EXTRA_MS,
  type GateRunResult, type GitIo, type GateAlerter,
} from '../web/gate-self-test.js'
import { hookCommand } from '../web/agent-scaffold.js'
import { PROJECT_ROOT } from '../config.js'

// BELA 6319 (B) / PROGI 6318 (3b): in an install that was a git clone at dashboard start, a gate whose
// integrity cannot be checked (broken .git/HEAD, unreadable .git, safe.directory refusal, git not answering,
// or a nested .git that takes the gate out of the install's checkout) is a failing entry, not a silent skip.

const tmp = realpathSync(mkdtempSync(join(tmpdir(), 'gate-anchor-')))
afterAll(() => rmSync(tmp, { recursive: true, force: true }))
const git = (cwd: string, ...args: string[]) => execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false', ...args], { cwd, stdio: 'pipe' }).toString()

function repo(name: string, files: Record<string, string>): string {
  const dir = join(tmp, name)
  mkdirSync(join(dir, 'scripts'), { recursive: true })
  for (const [f, body] of Object.entries(files)) { mkdirSync(dirname(join(dir, f)), { recursive: true }); writeFileSync(join(dir, f), body) }
  git(dir, 'init', '-q')
  git(dir, 'add', '-A')
  git(dir, 'commit', '-q', '-m', 'init')
  return dir
}
const NOGIT: GitIo = { run: () => null }

describe('detectGitAnchor', () => {
  it('the checkout top of the root (also from a subdirectory); a plain directory: null', () => {
    const r = repo('anchor', { 'scripts/egress-gate.mjs': 'process.exit(0)\n' })
    expect(detectGitAnchor(r)).toBe(r)
    expect(detectGitAnchor(join(r, 'scripts'))).toBe(r)
    const plain = join(tmp, 'plain-root'); mkdirSync(plain, { recursive: true })
    expect(detectGitAnchor(plain)).toBeNull()
  })
  it('git not answering at boot: a .git entry at the root still makes it a clone; without one, not a clone', () => {
    expect(detectGitAnchor('/srv/app', NOGIT, (p) => p === '/srv/app/.git')).toBe('/srv/app')
    expect(detectGitAnchor('/srv/app', NOGIT, () => false)).toBeNull()
  })
})

describe('checkGateIntegrity under the install anchor (real git)', () => {
  it('a broken .git/HEAD: unverifiable failing verdict under the anchor; the old null without one', () => {
    const r = repo('broken-head', { 'scripts/egress-gate.mjs': 'process.exit(2)\n' })
    const gate = join(r, 'scripts', 'egress-gate.mjs')
    expect(checkGateIntegrity(gate, REAL_GIT, undefined, undefined, r)).toEqual({ ok: true, detail: '' })
    writeFileSync(gate, 'process.exit(0)\n') // swapped AND the checkout broken: the swap must not get through silently
    writeFileSync(join(r, '.git', 'HEAD'), 'garbage\n')
    const v = checkGateIntegrity(gate, REAL_GIT, undefined, undefined, r)!
    expect(v.ok).toBe(false)
    expect(v.unverifiable).toBe(true)
    expect(v.detail).toContain('nem ellenorizheto')
    expect(v.detail).toContain(`git -C ${r} status`)
    expect(v.detail).not.toMatch(/—| -- /)
    expect(checkGateIntegrity(gate, REAL_GIT)).toBeNull()
    writeFileSync(join(r, '.git', 'HEAD'), 'ref: refs/heads/master\n')
  })
  it('an unreadable .git (chmod 000): unverifiable under the anchor', () => {
    const r = repo('unreadable', { 'scripts/egress-gate.mjs': 'process.exit(2)\n' })
    const gate = join(r, 'scripts', 'egress-gate.mjs')
    chmodSync(join(r, '.git'), 0o000)
    try {
      const v = checkGateIntegrity(gate, REAL_GIT, undefined, undefined, r)
      expect(v?.ok).toBe(false)
      expect(v?.unverifiable).toBe(true)
    } finally { chmodSync(join(r, '.git'), 0o755) }
    expect(checkGateIntegrity(gate, REAL_GIT, undefined, undefined, r)).toEqual({ ok: true, detail: '' })
  })
  it('git not answering at all (safe.directory refusal, hung / missing git): unverifiable under the anchor only', () => {
    const r = repo('nogit', { 'scripts/egress-gate.mjs': 'process.exit(2)\n' })
    expect(checkGateIntegrity(join(r, 'scripts', 'egress-gate.mjs'), NOGIT, undefined, undefined, r)?.unverifiable).toBe(true)
    // a gate OUTSIDE the anchor (e.g. a user-level hook in no checkout) keeps the skip
    const outside = join(tmp, 'outside'); mkdirSync(outside, { recursive: true }); writeFileSync(join(outside, 'egress-gate.mjs'), '')
    expect(checkGateIntegrity(join(outside, 'egress-gate.mjs'), NOGIT, undefined, undefined, r)).toBeNull()
    // a sibling directory sharing the anchor's name prefix is not "under" it
    const sib = `${r}-x`; mkdirSync(sib, { recursive: true }); writeFileSync(join(sib, 'egress-gate.mjs'), '')
    expect(checkGateIntegrity(join(sib, 'egress-gate.mjs'), NOGIT, undefined, undefined, r)).toBeNull()
  })
  it('a nested .git inside the checkout whose HEAD matches the swapped gate: unverifiable under the anchor (was ok)', () => {
    const r = repo('nested', { 'scripts/egress-gate.mjs': 'process.exit(2)\n' })
    const gate = join(r, 'scripts', 'egress-gate.mjs')
    writeFileSync(gate, 'process.exit(0) // allow all\n')
    git(join(r, 'scripts'), 'init', '-q')
    git(join(r, 'scripts'), 'add', '-A')
    git(join(r, 'scripts'), 'commit', '-q', '-m', 'forged')
    expect(checkGateIntegrity(gate, REAL_GIT)).toEqual({ ok: true, detail: '' }) // the bypass without the anchor
    const v = checkGateIntegrity(gate, REAL_GIT, undefined, undefined, r)!
    expect(v.ok).toBe(false)
    expect(v.unverifiable).toBe(true)
    expect(v.detail).toContain('beagyazott')
    expect(v.detail).toContain(join(r, 'scripts'))
  })
  it('the anchor checkout itself working: the usual verdicts (ok / differs) are unchanged', () => {
    const r = repo('normal', { 'scripts/egress-gate.mjs': 'process.exit(2)\n' })
    const gate = join(r, 'scripts', 'egress-gate.mjs')
    writeFileSync(gate, 'process.exit(0)\n')
    const v = checkGateIntegrity(gate, REAL_GIT, undefined, undefined, r)!
    expect(v.ok).toBe(false)
    expect(v.unverifiable).toBeUndefined()
    expect(v.detail).toContain('scripts/egress-gate.mjs (elter a git HEAD-tol)')
  })
})

describe('runGateSelfTest with the install anchor', () => {
  beforeEach(() => resetGateSelfTestState())
  const wire = (r: string, gate: string) => {
    const sp = join(r, `settings-${gate}.json`)
    writeFileSync(sp, JSON.stringify({ hooks: { PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command: hookCommand(join(r, 'scripts', gate)), timeout: 10 }] }] } }))
    return [{ agent: 'progi', settingsPath: sp, projectDir: r }]
  }

  it('a broken checkout: alerted after the 60 s re-check with its own effect and fix, then recovered', async () => {
    const r = repo('e2e-broken', { 'scripts/egress-gate.mjs': 'process.exit(0)\n' })
    const targets = wire(r, 'egress-gate.mjs')
    const sent: string[] = []
    const send = (t: string) => { sent.push(t) }
    await runGateSelfTest({ targets, send, retryDelayMs: 0, gitAnchor: r })
    expect(sent).toEqual([])
    writeFileSync(join(r, '.git', 'HEAD'), 'garbage\n')
    let tops = 0
    const counting: GitIo = { run: (cwd, args) => { if (args[1] === '--show-toplevel') tops++; return REAL_GIT.run(cwd, args) } }
    const res = await runGateSelfTest({ targets, send, retryDelayMs: 0, gitAnchor: r, git: counting })
    expect(tops).toBe(2) // first pass + the fresh verdict of the re-check
    expect(res[0].ok).toBe(false)
    expect(res[0].integrity).toBe(true)
    expect(res[0].unverifiable).toBe(true)
    expect(sent.length).toBe(1)
    expect(sent[0]).toContain(`[kapu-onteszt] KAPU-INTEGRITAS NEM ELLENORIZHETO: a telepites git checkoutjaban (${r})`)
    expect(sent[0]).toContain('Hatas: ismeretlen')
    expect(sent[0]).toContain(`Teendo: a checkout rendbetetele (git -C ${r} status`)
    expect(sent[0]).not.toContain('visszaallitani a git HEAD tartalmara')
    expect(sent[0]).not.toMatch(/—| -- /)
    writeFileSync(join(r, '.git', 'HEAD'), 'ref: refs/heads/master\n')
    await runGateSelfTest({ targets, send, retryDelayMs: 0, gitAnchor: r })
    expect(sent.length).toBe(2)
    expect(sent[1]).toContain(`[kapu-onteszt] HELYREALLT: a telepites git checkoutja (${r}) ujra ellenorizheto`)
  })

  it('a git hiccup gone by the re-check 60 s later is not alerted', async () => {
    const r = repo('e2e-hiccup', { 'scripts/egress-gate.mjs': 'process.exit(0)\n' })
    const targets = wire(r, 'egress-gate.mjs')
    let first = true
    const flaky: GitIo = { run: (cwd, args) => { if (first && args[1] === '--show-toplevel') { first = false; return null } return REAL_GIT.run(cwd, args) } }
    const sent: string[] = []
    const res = await runGateSelfTest({ targets, send: (t) => { sent.push(t) }, retryDelayMs: 0, gitAnchor: r, git: flaky })
    expect(res[0].ok).toBe(true)
    expect(sent).toEqual([])
  })

  it('not a clone (gitAnchor null): the broken / missing git is skipped as before, the benign call decides', async () => {
    const r = repo('e2e-noanchor', { 'scripts/egress-gate.mjs': 'process.exit(0)\n' })
    const targets = wire(r, 'egress-gate.mjs')
    const sent: string[] = []
    const res = await runGateSelfTest({ targets, send: (t) => { sent.push(t) }, retryDelayMs: 0, gitAnchor: null, git: NOGIT })
    expect(res[0].ok).toBe(true)
    expect(sent).toEqual([])
  })
})

describe('startGateSelfTest takes the anchor at start', () => {
  afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); resetGateSelfTestState() })
  it('after start, a gate under PROJECT_ROOT whose git does not answer is a failing entry (no gitAnchor passed)', async () => {
    const gate = join(PROJECT_ROOT, 'scripts', 'hooks', 'egress-gate.mjs')
    const sp = join(tmp, 'settings-start.json')
    writeFileSync(sp, JSON.stringify({ hooks: { PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command: hookCommand(gate), timeout: 10 }] }] } }))
    const targets = [{ agent: 'progi', settingsPath: sp, projectDir: tmp }]
    // before start: no anchor known, the old skip (the result is then the benign call's; only the flag matters)
    const before = await runGateSelfTest({ targets, send: () => {}, retryDelayMs: 0, git: NOGIT })
    expect(before[0].unverifiable).toBeUndefined()
    resetGateSelfTestState()
    vi.useFakeTimers()
    clearInterval(startGateSelfTest()) // the boot run is a timer that never fires here
    vi.useRealTimers()
    const after = await runGateSelfTest({ targets, send: () => {}, retryDelayMs: 0, git: NOGIT })
    expect(after[0].ok).toBe(false)
    expect(after[0].unverifiable).toBe(true)
  })
})


// BELA 6327 (2): one escalation per broken checkout, not one per gate script.
describe('one escalation for a checkout whose gates cannot be verified', () => {
  beforeEach(() => resetGateSelfTestState())
  type Ev = { kind: 'fail' | 'recover'; key: string; text: string; stage2?: number }
  const recorder = () => {
    const ev: Ev[] = []
    const alerter: GateAlerter = {
      fail: (key, text, stage2) => { ev.push({ kind: 'fail', key, text, stage2 }) },
      recover: (key, text) => { ev.push({ kind: 'recover', key, text }) },
    }
    return { ev, alerter }
  }
  const wireMany = (r: string, gates: string[], agents: string[]) => agents.map((agent) => {
    const sp = join(r, `settings-${agent}.json`)
    writeFileSync(sp, JSON.stringify({ hooks: { PreToolUse: gates.map((g) => ({ matcher: 'Bash', hooks: [{ type: 'command', command: hookCommand(join(r, 'scripts', g)), timeout: 10 }] })) } }))
    return { agent, settingsPath: sp, projectDir: r }
  })

  it('two gate scripts on three agents, checkout broken: ONE alert on the checkout key listing both gates; ONE all-clear', async () => {
    const r = repo('agg', { 'scripts/egress-gate.mjs': 'process.exit(0)\n', 'scripts/self-pace-gate.mjs': 'process.exit(0)\n' })
    const targets = wireMany(r, ['egress-gate.mjs', 'self-pace-gate.mjs'], ['progi', 'okoska', 'iris'])
    const { ev, alerter } = recorder()
    await runGateSelfTest({ targets, alerter, retryDelayMs: 0, gitAnchor: r })
    expect(ev).toEqual([])
    writeFileSync(join(r, '.git', 'HEAD'), 'garbage\n')
    const res = await runGateSelfTest({ targets, alerter, retryDelayMs: 0, gitAnchor: r })
    expect(res.filter((x) => x.unverifiable).length).toBe(6)
    expect(ev.length).toBe(1)
    expect(ev[0].kind).toBe('fail')
    expect(ev[0].key).toBe(unverifiableKey(r))
    expect(ev[0].key).toBe(`gate-integrity-unverifiable:${r}`)
    expect(ev[0].text).toContain('2 kapu-szkript')
    expect(ev[0].text).toContain('- kapu: egress-gate.mjs (3 bekotott bejegyzes, agensek: progi, okoska, iris)')
    expect(ev[0].text).toContain('- kapu: self-pace-gate.mjs (3 bekotott bejegyzes, agensek: progi, okoska, iris)')
    expect(ev[0].text).not.toMatch(/—| -- /)
    // still broken next pass: nothing new
    await runGateSelfTest({ targets, alerter, retryDelayMs: 0, gitAnchor: r })
    expect(ev.length).toBe(1)
    writeFileSync(join(r, '.git', 'HEAD'), 'ref: refs/heads/master\n')
    await runGateSelfTest({ targets, alerter, retryDelayMs: 0, gitAnchor: r })
    expect(ev.length).toBe(2)
    expect(ev[1]).toEqual({ kind: 'recover', key: unverifiableKey(r), text: gateUnverifiableRecoveryText(r) })
  })

  it('a further gate script becoming unverifiable re-raises with the full list and the ORIGINAL owner deadline', async () => {
    const r = repo('agg-reraise', { 'scripts/a/egress-gate.mjs': 'process.exit(0)\n', 'scripts/b/self-pace-gate.mjs': 'process.exit(0)\n' })
    const sp = join(r, 'settings.json')
    writeFileSync(sp, JSON.stringify({ hooks: { PreToolUse: [
      { matcher: 'Bash', hooks: [{ type: 'command', command: hookCommand(join(r, 'scripts', 'a', 'egress-gate.mjs')), timeout: 10 }] },
      { matcher: 'Bash', hooks: [{ type: 'command', command: hookCommand(join(r, 'scripts', 'b', 'self-pace-gate.mjs')), timeout: 10 }] },
    ] } }))
    const targets = [{ agent: 'progi', settingsPath: sp, projectDir: r }]
    const { ev, alerter } = recorder()
    let now = 1_000_000
    git(join(r, 'scripts', 'a'), 'init', '-q') // nested repo: only gate a unverifiable
    await runGateSelfTest({ targets, alerter, retryDelayMs: 0, gitAnchor: r, now: () => now })
    expect(ev.map((e) => e.kind)).toEqual(['fail'])
    expect(ev[0].stage2).toBe(GATE_STAGE2_EXTRA_MS)
    expect(ev[0].text).toContain('1 kapu-szkript')
    now += 5 * 60_000
    git(join(r, 'scripts', 'b'), 'init', '-q')
    await runGateSelfTest({ targets, alerter, retryDelayMs: 0, gitAnchor: r, now: () => now })
    expect(ev.length).toBe(2)
    expect(ev[1].kind).toBe('fail')
    expect(ev[1].key).toBe(unverifiableKey(r))
    expect(ev[1].stage2).toBe(GATE_STAGE2_EXTRA_MS - 5 * 60_000)
    expect(ev[1].text).toContain('2 kapu-szkript')
  })

  it('an open per-gate "differs" escalation is neither recovered nor re-alerted while the checkout is unverifiable', async () => {
    const r = repo('agg-mixed', { 'scripts/egress-gate.mjs': 'process.exit(0)\n' })
    const targets = wireMany(r, ['egress-gate.mjs'], ['progi'])
    const { ev, alerter } = recorder()
    writeFileSync(join(r, 'scripts', 'egress-gate.mjs'), 'process.exit(0) // swapped\n')
    await runGateSelfTest({ targets, alerter, retryDelayMs: 0, gitAnchor: r })
    expect(ev.map((e) => [e.kind, e.key])).toEqual([['fail', 'gate-self-test:egress-gate.mjs']])
    writeFileSync(join(r, '.git', 'HEAD'), 'garbage\n')
    await runGateSelfTest({ targets, alerter, retryDelayMs: 0, gitAnchor: r })
    // no false "HELYREALLT" for the swapped gate, one checkout alert
    expect(ev.map((e) => [e.kind, e.key])).toEqual([['fail', 'gate-self-test:egress-gate.mjs'], ['fail', unverifiableKey(r)]])
    writeFileSync(join(r, '.git', 'HEAD'), 'ref: refs/heads/master\n')
    await runGateSelfTest({ targets, alerter, retryDelayMs: 0, gitAnchor: r })
    // checkout fixed, gate still swapped: the checkout all-clear only; the gate escalation stays open, not re-sent
    expect(ev.slice(2).map((e) => [e.kind, e.key])).toEqual([['recover', unverifiableKey(r)]])
    git(r, 'checkout', '-q', 'HEAD', '--', 'scripts/egress-gate.mjs')
    await runGateSelfTest({ targets, alerter, retryDelayMs: 0, gitAnchor: r })
    expect(ev.slice(3).map((e) => [e.kind, e.key])).toEqual([['recover', 'gate-self-test:egress-gate.mjs']])
  })
})

describe('reconcileUnverifiable (pure)', () => {
  const entry = (agent: string, gate: string, sp = `/s/${agent}`) => ({ agent, settingsPath: sp, projectDir: '/p', matcher: 'Bash', command: 'x', timeoutSec: 10, gate })
  const u = (agent: string, gate: string, sp?: string): GateRunResult => ({ entry: entry(agent, gate, sp), ok: false, detail: 'd', integrity: true, unverifiable: true })
  const ok = (agent: string, gate: string): GateRunResult => ({ entry: entry(agent, gate), ok: true, detail: '' })
  it('fail / re-raise only on a new gate script / quiet / recover / an unreadable settings file keeps it open', () => {
    expect(reconcileUnverifiable(null, [u('a', 'g1')], [u('a', 'g1')])).toEqual({ fail: { reraise: false }, recover: false })
    const open = { repo: '/r', gates: new Set(['g1']), results: [u('a', 'g1', '/s/a')], raisedAt: 0 }
    expect(reconcileUnverifiable(open, [u('a', 'g1'), u('b', 'g1')], [])).toEqual({ fail: null, recover: false })
    expect(reconcileUnverifiable(open, [u('a', 'g1'), u('a', 'g2')], [])).toEqual({ fail: { reraise: true }, recover: false })
    expect(reconcileUnverifiable(open, [], [ok('a', 'g1')])).toEqual({ fail: null, recover: true })
    expect(reconcileUnverifiable(open, [], [], new Set(['/s/a']))).toEqual({ fail: null, recover: false })
    expect(reconcileUnverifiable(open, [], [], new Set())).toEqual({ fail: null, recover: true })
    expect(reconcileUnverifiable(null, [], [ok('a', 'g1')])).toEqual({ fail: null, recover: false })
  })
  it('the alert text groups by gate script and names the checkout', () => {
    const t = gateUnverifiableText('/r', [u('a', 'g1'), u('b', 'g1'), u('a', 'g2')])
    expect(t).toContain('a telepites git checkoutjaban (/r) 2 kapu-szkript')
    expect(t).toContain('- kapu: g1 (2 bekotott bejegyzes, agensek: a, b): d.')
    expect(t).toContain('- kapu: g2 (1 bekotott bejegyzes, agensek: a): d.')
    // the main agent has several settings files wiring the same gate: named once
    expect(gateUnverifiableText('/r', [u('bela', 'g1', '/s1'), u('bela', 'g1', '/s2'), u('bela', 'g1', '/s3')])).toContain('- kapu: g1 (3 bekotott bejegyzes, agensek: bela): d.')
    expect(t).not.toMatch(/—| -- /)
  })
})

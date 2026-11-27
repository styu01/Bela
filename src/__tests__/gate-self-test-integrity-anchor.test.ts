import { describe, it, expect, beforeEach, afterAll, afterEach, vi } from 'vitest'
import { mkdtempSync, writeFileSync, rmSync, mkdirSync, chmodSync, realpathSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { execFileSync } from 'node:child_process'
import {
  checkGateIntegrity, detectGitAnchor, runGateSelfTest, resetGateSelfTestState, startGateSelfTest, REAL_GIT,
  gateFailureText, type GateRunResult, type GitIo,
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
  for (const [f, body] of Object.entries(files)) writeFileSync(join(dir, f), body)
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
    expect(sent[0]).toContain('[kapu-onteszt] KAPU HIBA')
    expect(sent[0]).toContain('Hatas: ismeretlen: a kapu tartalma nem ellenorizheto')
    expect(sent[0]).toContain('a telepites git checkoutjat rendbe tenni')
    expect(sent[0]).not.toContain('visszaallitani a git HEAD tartalmara')
    expect(sent[0]).not.toMatch(/—| -- /)
    writeFileSync(join(r, '.git', 'HEAD'), 'ref: refs/heads/master\n')
    await runGateSelfTest({ targets, send, retryDelayMs: 0, gitAnchor: r })
    expect(sent.length).toBe(2)
    expect(sent[1]).toContain('[kapu-onteszt] HELYREALLT')
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

describe('gateFailureText for an unverifiable gate', () => {
  it('its own effect and fix; a mixed list keeps the swapped-gate fix', () => {
    const entry = { agent: 'a', settingsPath: '/s', projectDir: '/p', matcher: 'Bash', command: 'x', timeoutSec: 10, gate: 'egress-gate.mjs' }
    const u: GateRunResult = { entry, ok: false, detail: 'a kapu integritasa nem ellenorizheto: ...', integrity: true, unverifiable: true }
    const t = gateFailureText([u], [0, 0, 0])
    expect(t).toContain('Hatas: ismeretlen: a kapu tartalma nem ellenorizheto')
    expect(t).toContain('a telepites git checkoutjat rendbe tenni')
    expect(t).not.toMatch(/—| -- /)
    const d: GateRunResult = { entry: { ...entry, gate: 'self-pace-gate.mjs' }, ok: false, detail: 'elter', integrity: true }
    const m = gateFailureText([d, u], [0, 0, 0])
    expect(m).toContain('visszaallitani a git HEAD tartalmara')
    expect(m).toContain('Hatas: ismeretlen: a kapu nem a verziokezelt kodot futtatja')
  })
})

import { describe, it, expect, beforeEach, afterAll } from 'vitest'
import { mkdtempSync, writeFileSync, rmSync, readFileSync, mkdirSync, symlinkSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { execFileSync } from 'node:child_process'
import {
  gateScriptClosure, checkGateIntegrity, runGateSelfTest, resetGateSelfTestState, REAL_GIT, gateFailureText,
  type GateRunResult,
} from '../web/gate-self-test.js'
import { hookCommand } from '../web/agent-scaffold.js'

// BELA 6270 / PROGI 6268: the benign-call self-test cannot see a gate whose content was swapped for one that
// lets everything through. The gate file and the local modules it loads are compared with git HEAD.

const tmp = mkdtempSync(join(tmpdir(), 'gate-integrity-'))
afterAll(() => rmSync(tmp, { recursive: true, force: true }))
const read = (p: string) => { try { return readFileSync(p, 'utf-8') } catch { return null } }
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

describe('gateScriptClosure', () => {
  it('follows relative static, re-export, side-effect and dynamic imports; ignores packages and missing files; survives a cycle', () => {
    const d = join(tmp, 'closure'); mkdirSync(join(d, 'lib'), { recursive: true })
    writeFileSync(join(d, 'gate.mjs'), "import { a } from './a.mjs'\nimport fs from 'node:fs'\nexport { b } from \"./lib/b.mjs\"\nimport './c.mjs'\nconst x = await import('./d.mjs')\nimport y from './missing.mjs'\n")
    writeFileSync(join(d, 'a.mjs'), "import { g } from './gate.mjs'\n")
    writeFileSync(join(d, 'lib', 'b.mjs'), "import { a } from '../a.mjs'\n")
    writeFileSync(join(d, 'c.mjs'), '')
    writeFileSync(join(d, 'd.mjs'), '')
    const got = gateScriptClosure(join(d, 'gate.mjs'), read).map((p) => p.slice(d.length + 1)).sort()
    expect(got).toEqual(['a.mjs', 'c.mjs', 'd.mjs', 'gate.mjs', 'lib/b.mjs'])
  })
  it('a Python gate: same-directory modules only (from x import, import a, b), the standard library is not a file here', () => {
    const d = join(tmp, 'closure-py'); mkdirSync(d, { recursive: true })
    writeFileSync(join(d, 'gate.py'), 'import json, os\nimport helper, other\nfrom util import f\n  import nested\n')
    for (const f of ['helper.py', 'other.py', 'util.py', 'nested.py']) writeFileSync(join(d, f), '')
    const got = gateScriptClosure(join(d, 'gate.py'), read).map((p) => p.slice(d.length + 1)).sort()
    expect(got).toEqual(['gate.py', 'helper.py', 'nested.py', 'other.py', 'util.py'])
  })
})

describe('checkGateIntegrity (real git, temp repo)', () => {
  it('HEAD content: ok; a changed gate, a changed imported module, an untracked gate: named; outside a checkout: null', () => {
    const r = repo('integ', {
      'scripts/kanban-write-gate.mjs': "import { s } from './self-pace-gate.mjs'\nprocess.exit(0)\n",
      'scripts/self-pace-gate.mjs': 'export const s = 1\n',
    })
    const gate = join(r, 'scripts', 'kanban-write-gate.mjs')
    expect(checkGateIntegrity(gate)).toEqual({ ok: true, detail: '' })
    writeFileSync(join(r, 'scripts', 'self-pace-gate.mjs'), 'export const s = 1 // allow all\n')
    const v = checkGateIntegrity(gate)!
    expect(v.ok).toBe(false)
    expect(v.detail).toContain('scripts/self-pace-gate.mjs (elter a git HEAD-tol)')
    expect(v.detail).not.toContain('kanban-write-gate.mjs (')
    expect(v.detail).toContain(`git -C ${r} restore --source=HEAD scripts/self-pace-gate.mjs`)
    expect(v.detail).not.toMatch(/—| -- /)
    git(r, 'checkout', '-q', 'HEAD', '--', 'scripts/self-pace-gate.mjs')
    expect(checkGateIntegrity(gate)!.ok).toBe(true)
    writeFileSync(join(r, 'scripts', 'egress-gate.mjs'), 'process.exit(0)\n')
    expect(checkGateIntegrity(join(r, 'scripts', 'egress-gate.mjs'))!.detail).toContain('scripts/egress-gate.mjs (nincs a git HEAD-ben)')
    const outside = join(tmp, 'plain'); mkdirSync(outside, { recursive: true }); writeFileSync(join(outside, 'egress-gate.mjs'), '')
    expect(checkGateIntegrity(join(outside, 'egress-gate.mjs'))).toBeNull()
  })
  it('a symlinked gate path is judged by the repo file it points to', () => {
    const r = repo('integ-link', { 'scripts/egress-gate.mjs': 'process.exit(0)\n' })
    const link = join(tmp, 'egress-link.mjs')
    symlinkSync(join(r, 'scripts', 'egress-gate.mjs'), link)
    expect(checkGateIntegrity(link)).toEqual({ ok: true, detail: '' })
    writeFileSync(join(r, 'scripts', 'egress-gate.mjs'), 'process.exit(0) // x\n')
    expect(checkGateIntegrity(link)!.detail).toContain('scripts/egress-gate.mjs (elter a git HEAD-tol)')
  })
  it('BELA 6319 (A) / PROGI 6318: a clean filter from .git/info/attributes + .git/config neither hides a swapped gate nor gets run', () => {
    const r = repo('integ-filter', { 'scripts/egress-gate.mjs': 'process.exit(2)\n' })
    const gate = join(r, 'scripts', 'egress-gate.mjs')
    const headCopy = join(r, '.git', 'headcopy')
    writeFileSync(headCopy, readFileSync(gate))
    const marker = join(r, '.git', 'filter-ran')
    writeFileSync(join(r, '.git', 'info', 'attributes'), 'scripts/egress-gate.mjs filter=fake\n')
    git(r, 'config', 'filter.fake.clean', `touch ${marker}; cat ${headCopy}`)
    git(r, 'config', 'filter.fake.smudge', `touch ${marker}; cat`)
    // the filter hides the swap from git itself (status sees no change)
    writeFileSync(gate, 'process.exit(0)\n')
    expect(git(r, 'status', '--porcelain')).toBe('')
    rmSync(marker, { force: true })
    const v = checkGateIntegrity(gate)!
    expect(v.ok).toBe(false)
    expect(v.detail).toContain('scripts/egress-gate.mjs (elter a git HEAD-tol)')
    expect(existsSync(marker)).toBe(false) // the command from .git/config did NOT run
    // the HEAD content itself still verifies with the filter configured (no false alarm)
    writeFileSync(gate, readFileSync(headCopy))
    expect(checkGateIntegrity(gate)).toEqual({ ok: true, detail: '' })
    expect(existsSync(marker)).toBe(false)
  })

  it('PROGI 6332 (D): a `git replace` of the HEAD commit with a forged one (gate swapped in it) does not hide the swap', () => {
    const r = repo('integ-replace', { 'scripts/egress-gate.mjs': 'process.exit(2)\n' })
    const gate = join(r, 'scripts', 'egress-gate.mjs')
    writeFileSync(gate, 'process.exit(0)\n')
    git(r, 'add', 'scripts/egress-gate.mjs')
    const forged = git(r, 'commit-tree', git(r, 'write-tree').trim(), '-p', 'HEAD', '-m', 'init').trim()
    git(r, 'replace', git(r, 'rev-parse', 'HEAD').trim(), forged)
    git(r, 'reset', '-q')
    // git itself no longer sees the swap
    expect(git(r, 'status', '--porcelain')).toBe('')
    expect(git(r, 'rev-parse', 'HEAD:scripts/egress-gate.mjs').trim()).toBe(git(r, 'hash-object', 'scripts/egress-gate.mjs').trim())
    const v = checkGateIntegrity(gate)!
    expect(v.ok).toBe(false)
    expect(v.detail).toContain('scripts/egress-gate.mjs (elter a git HEAD-tol)')
    // a GIT_REPLACE_REF_BASE in the dashboard environment does not bring it back
    process.env.GIT_REPLACE_REF_BASE = 'refs/replace/'
    try { expect(checkGateIntegrity(gate)!.ok).toBe(false) } finally { delete process.env.GIT_REPLACE_REF_BASE }
    git(r, 'replace', '-d', git(r, 'rev-parse', 'HEAD').trim())
    git(r, 'checkout', '-q', 'HEAD', '--', 'scripts/egress-gate.mjs')
    expect(checkGateIntegrity(gate)).toEqual({ ok: true, detail: '' })
  })

  it('a git that fails (no repo answer) is not verifiable, never a false alarm', () => {
    expect(checkGateIntegrity('/x/scripts/egress-gate.mjs', { run: () => null }, () => '', () => true)).toBeNull()
  })
})

describe('runGateSelfTest with the integrity check', () => {
  beforeEach(() => resetGateSelfTestState())
  const wire = (r: string, gate: string) => {
    const sp = join(r, `settings-${gate}.json`)
    writeFileSync(sp, JSON.stringify({ hooks: { PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command: hookCommand(join(r, 'scripts', gate)), timeout: 10 }] }] } }))
    return [{ agent: 'progi', settingsPath: sp, projectDir: r }]
  }

  it('a gate swapped for one that passes everything (the benign call passes too): alerted with its own effect, then recovered', async () => {
    const r = repo('e2e', { 'scripts/self-pace-gate.mjs': 'process.exit(0)\n' })
    const targets = wire(r, 'self-pace-gate.mjs')
    const sent: string[] = []
    const send = (t: string) => { sent.push(t) }
    await runGateSelfTest({ targets, send, retryDelayMs: 0 })
    expect(sent).toEqual([])
    writeFileSync(join(r, 'scripts', 'self-pace-gate.mjs'), '// allow everything\nprocess.exit(0)\n')
    const res = await runGateSelfTest({ targets, send, retryDelayMs: 0 })
    expect(res[0].ok).toBe(false)
    expect(res[0].integrity).toBe(true)
    expect(sent.length).toBe(1)
    expect(sent[0]).toContain('[kapu-onteszt] KAPU HIBA')
    expect(sent[0]).toContain('scripts/self-pace-gate.mjs (elter a git HEAD-tol)')
    expect(sent[0]).toContain('Hatas: ismeretlen')
    expect(sent[0]).not.toContain('TILTVA')
    expect(sent[0]).not.toContain('Idotullepes')
    expect(sent[0]).not.toMatch(/—| -- /)
    git(r, 'checkout', '-q', 'HEAD', '--', 'scripts/self-pace-gate.mjs')
    await runGateSelfTest({ targets, send, retryDelayMs: 0 })
    expect(sent.length).toBe(2)
    expect(sent[1]).toContain('[kapu-onteszt] HELYREALLT')
  })

  it('a mismatch seen mid-deploy that is gone at the re-check 60 s later is not alerted', async () => {
    const r = repo('e2e-race', { 'scripts/egress-gate.mjs': 'process.exit(0)\n' })
    const targets = wire(r, 'egress-gate.mjs')
    const sent: string[] = []
    writeFileSync(join(r, 'scripts', 'egress-gate.mjs'), 'process.exit(0) // half-deployed\n')
    setTimeout(() => { git(r, 'checkout', '-q', 'HEAD', '--', 'scripts/egress-gate.mjs') }, 50)
    const res = await runGateSelfTest({ targets, send: (t) => { sent.push(t) }, retryDelayMs: 400 })
    expect(res[0].ok).toBe(true)
    expect(sent).toEqual([])
  })

  it('a still-differing file after the re-check is alerted (the verdict is taken afresh, not cached)', async () => {
    const r = repo('e2e-stays', { 'scripts/egress-gate.mjs': 'process.exit(0)\n' })
    const targets = wire(r, 'egress-gate.mjs')
    writeFileSync(join(r, 'scripts', 'egress-gate.mjs'), 'process.exit(0) // a\n')
    let calls = 0
    const counting = { run: (cwd: string, args: readonly string[]) => { if (args[0] === 'hash-object') calls++; return REAL_GIT.run(cwd, args) } }
    const sent: string[] = []
    await runGateSelfTest({ targets, send: (t) => { sent.push(t) }, retryDelayMs: 0, git: counting })
    expect(calls).toBe(2)
    expect(sent.length).toBe(1)
  })

  it('one verdict per script per pass, however many settings wire it', async () => {
    const r = repo('e2e-memo', { 'scripts/egress-gate.mjs': 'process.exit(0)\n' })
    const a = wire(r, 'egress-gate.mjs')
    const sp2 = join(r, 'settings-2.json'); writeFileSync(sp2, readFileSync(a[0].settingsPath, 'utf-8'))
    let calls = 0
    const counting = { run: (cwd: string, args: readonly string[]) => { if (args[0] === 'hash-object') calls++; return REAL_GIT.run(cwd, args) } }
    await runGateSelfTest({ targets: [...a, { agent: 'okoska', settingsPath: sp2, projectDir: r }], send: () => {}, retryDelayMs: 0, git: counting })
    expect(calls).toBe(1)
  })

  it('git: null switches the check off (the swapped gate passes the benign call, as before)', async () => {
    const r = repo('e2e-off', { 'scripts/egress-gate.mjs': 'process.exit(0)\n' })
    const targets = wire(r, 'egress-gate.mjs')
    writeFileSync(join(r, 'scripts', 'egress-gate.mjs'), 'process.exit(0) // b\n')
    const sent: string[] = []
    const res = await runGateSelfTest({ targets, send: (t) => { sent.push(t) }, retryDelayMs: 0, git: null })
    expect(res[0].ok).toBe(true)
    expect(sent).toEqual([])
  })
})

describe('gateFailureText for an integrity failure', () => {
  it('names the effect as unknown and the git restore as the fix', () => {
    const r: GateRunResult = { entry: { agent: 'a', settingsPath: '/s', projectDir: '/p', matcher: 'Bash', command: 'x', timeoutSec: 10, gate: 'egress-gate.mjs' }, ok: false, detail: 'a kapu tartalma nem a verziokezelt: scripts/egress-gate.mjs (elter a git HEAD-tol)', integrity: true }
    const t = gateFailureText([r], [0, 0, 0])
    expect(t).toContain('Hatas: ismeretlen')
    expect(t).toContain('visszaallitani a git HEAD tartalmara')
  })
})

import { describe, it, expect, beforeEach, afterAll } from 'vitest'
import { mkdtempSync, writeFileSync, rmSync, mkdirSync, symlinkSync, unlinkSync, realpathSync, renameSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { tmpdir } from 'node:os'
import { execFileSync } from 'node:child_process'
import { checkGateIntegrity, symlinkBelow, runGateSelfTest, resetGateSelfTestState, REAL_GIT } from '../web/gate-self-test.js'
import { hookCommand } from '../web/agent-scaffold.js'

// PROGI 6332 (C): a symlink on the wired gate path (the file, a parent directory, or a local module) under the
// install's checkout is a failing integrity entry; the verdict is no longer taken for the symlink's target.

const tmp = realpathSync(mkdtempSync(join(tmpdir(), 'gate-symlink-')))
afterAll(() => rmSync(tmp, { recursive: true, force: true }))
const git = (cwd: string, ...args: string[]) => execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false', ...args], { cwd, stdio: 'pipe' }).toString()

const GATE = "import { deny } from './mod.mjs'\nprocess.exit(deny() ? 2 : 0)\n"
function repo(name: string, extra: Record<string, string> = {}): { d: string; gate: string } {
  const d = join(tmp, name)
  const files: Record<string, string> = {
    'scripts/gate.mjs': GATE,
    'scripts/mod.mjs': 'export function deny() { return true }\n',
    'scripts/noop.mjs': 'process.exit(0)\n',
    'scripts/mod-open.mjs': 'export function deny() { return false }\n',
    ...extra,
  }
  for (const [f, body] of Object.entries(files)) { mkdirSync(dirname(join(d, f)), { recursive: true }); writeFileSync(join(d, f), body) }
  git(d, 'init', '-q'); git(d, 'add', '-A'); git(d, 'commit', '-q', '-m', 'init')
  return { d, gate: join(d, 'scripts', 'gate.mjs') }
}
const chk = (gate: string, anchor: string | null) => checkGateIntegrity(gate, REAL_GIT, undefined, undefined, anchor)
const OK = { ok: true, detail: '' }

describe('symlink on the wired gate path, under the anchor', () => {
  it('(C1) the gate swapped for a symlink to an already COMMITTED pass-all file of the same repo: failing (was ok)', () => {
    const r = repo('c1')
    expect(chk(r.gate, r.d)).toEqual(OK)
    unlinkSync(r.gate); symlinkSync('noop.mjs', r.gate)
    expect(chk(r.gate, null)).toEqual(OK) // without an anchor: the old judgement of the target (documented)
    const v = chk(r.gate, r.d)!
    expect(v.ok).toBe(false)
    expect(v.unverifiable).toBeUndefined() // a detectable swap, not "cannot check"
    expect(v.detail).toContain('scripts/gate.mjs (szimlink a bekotott utvonalon: scripts/gate.mjs -> noop.mjs)')
    expect(v.detail).toContain(`git -C ${r.d} restore --source=HEAD scripts/gate.mjs`)
    expect(v.detail).not.toMatch(/—| -- /)
    // the suggested fix works
    git(r.d, 'restore', '--source=HEAD', 'scripts/gate.mjs')
    expect(chk(r.gate, r.d)).toEqual(OK)
  })

  it('(C2) a symlink into a forged repo outside the anchor (was ok), and to a loose file outside any repo (was a silent skip): failing', () => {
    const r = repo('c2')
    const fake = repo('c2-fake')
    writeFileSync(fake.gate, 'process.exit(0)\n'); git(fake.d, 'commit', '-qam', 'swap')
    unlinkSync(r.gate); symlinkSync(fake.gate, r.gate)
    expect(chk(r.gate, r.d)?.ok).toBe(false)
    expect(chk(r.gate, r.d)?.detail).toContain(`scripts/gate.mjs -> ${fake.gate}`)
    const loose = join(tmp, 'loose'); mkdirSync(loose, { recursive: true }); writeFileSync(join(loose, 'g.mjs'), 'process.exit(0)\n')
    unlinkSync(r.gate); symlinkSync(join(loose, 'g.mjs'), r.gate)
    expect(chk(r.gate, null)).toBeNull() // the old skip without an anchor
    expect(chk(r.gate, r.d)?.ok).toBe(false)
  })

  it('a PARENT directory of the gate swapped for a symlink (to a forged copy outside): failing, the directory named', () => {
    const r = repo('parent')
    const fake = repo('parent-fake')
    writeFileSync(fake.gate, 'process.exit(0)\n'); git(fake.d, 'commit', '-qam', 'swap')
    renameSync(join(r.d, 'scripts'), join(tmp, 'parent-scripts-aside'))
    symlinkSync(join(fake.d, 'scripts'), join(r.d, 'scripts'))
    const v = chk(r.gate, r.d)!
    expect(v.ok).toBe(false)
    expect(v.detail).toContain(`(szimlink a bekotott utvonalon: scripts -> ${join(fake.d, 'scripts')})`)
    expect(v.detail).toMatch(/restore --source=HEAD scripts$/) // the symlinked directory itself, not the file under it
    git(r.d, 'restore', '--source=HEAD', 'scripts')
    expect(chk(r.gate, r.d)).toEqual(OK)
  })

  it('a local MODULE swapped for a symlink to a committed permissive module: failing, the module named', () => {
    const r = repo('module')
    unlinkSync(join(r.d, 'scripts', 'mod.mjs')); symlinkSync('mod-open.mjs', join(r.d, 'scripts', 'mod.mjs'))
    expect(chk(r.gate, null)).toEqual(OK) // the old bypass
    const v = chk(r.gate, r.d)!
    expect(v.ok).toBe(false)
    expect(v.detail).toContain('scripts/mod.mjs (szimlink: -> mod-open.mjs)')
    expect(v.detail).not.toContain('scripts/gate.mjs (')
    expect(v.detail).toContain(`git -C ${r.d} restore --source=HEAD scripts/mod.mjs`)
  })
})

describe('(F) a symlink inside the checkout pointing back to its root (PROGI 6338, BELA 6339)', () => {
  it('a wired path through a link back to the checkout root: failing (was ok, the verified file ran as <root>/scripts/gate.mjs)', () => {
    const r = repo('loop', { 'agents/x/keep.txt': '' })
    symlinkSync(r.d, join(r.d, 'agents', 'x', 'root'))
    const wired = join(r.d, 'agents', 'x', 'root', 'scripts', 'gate.mjs')
    const v = chk(wired, r.d)!
    expect(v.ok).toBe(false)
    expect(v.detail).toContain(`szimlink a bekotott utvonalon: agents/x/root -> ${r.d}`)
    expect(v.unverifiable).toBeUndefined()
  })
  it('PROGI PoC: scripts/hooks swapped for a link to the root, a pass-all gate.mjs COMMITTED at the root: failing (was ok)', () => {
    const r = repo('loop-pass', { 'scripts/hooks/gate.mjs': GATE, 'scripts/hooks/mod.mjs': 'export function deny() { return true }\n', 'gate.mjs': 'process.exit(0)\n' })
    const wired = join(r.d, 'scripts', 'hooks', 'gate.mjs')
    expect(chk(wired, r.d)).toEqual(OK) // the real directory: ok (GATE + its module)
    rmSync(join(r.d, 'scripts', 'hooks'), { recursive: true }); symlinkSync(r.d, join(r.d, 'scripts', 'hooks'))
    const v = chk(wired, r.d)!
    expect(v.ok).toBe(false)
    expect(v.detail).toContain(`szimlink a bekotott utvonalon: scripts/hooks -> ${r.d}`)
    expect(v.detail).toContain(`git -C ${r.d} restore --source=HEAD scripts/hooks`)
  })
  it('through an alias ABOVE the root the inner loop link is still found; the alias alone stays ok', () => {
    const r = repo('loop-alias', { 'agents/x/keep.txt': '' })
    const alias = join(tmp, 'alias-of-loop'); symlinkSync(r.d, alias)
    expect(chk(join(alias, 'scripts', 'gate.mjs'), r.d)).toEqual(OK)
    symlinkSync(r.d, join(r.d, 'agents', 'x', 'root'))
    expect(chk(join(alias, 'agents', 'x', 'root', 'scripts', 'gate.mjs'), r.d)?.detail).toContain(`szimlink a bekotott utvonalon: agents/x/root -> ${r.d}`)
  })
})

describe('no false alarm', () => {
  it('a symlink ABOVE the checkout root (an alias of the install path) is not the checkout\'s business: ok', () => {
    const r = repo('above')
    const alias = join(tmp, 'alias-of-above'); symlinkSync(r.d, alias)
    expect(chk(join(alias, 'scripts', 'gate.mjs'), r.d)).toEqual(OK)
    // ... and a symlink swap below the root is still seen through the alias
    unlinkSync(r.gate); symlinkSync('noop.mjs', r.gate)
    expect(chk(join(alias, 'scripts', 'gate.mjs'), r.d)?.detail).toContain('szimlink a bekotott utvonalon: scripts/gate.mjs -> noop.mjs')
  })
  it('plain files (today\'s install): ok; a gate outside the anchor keeps the old rules', () => {
    const r = repo('plain')
    expect(chk(r.gate, r.d)).toEqual(OK)
    const other = repo('plain-other')
    expect(chk(other.gate, r.d)).toEqual(OK)
  })
})

describe('symlinkBelow', () => {
  it('first symlink component below the base with its target; none; a missing component: null', () => {
    const b = join(tmp, 'sb'); mkdirSync(join(b, 'a', 'b'), { recursive: true }); writeFileSync(join(b, 'a', 'b', 'f'), '')
    expect(symlinkBelow(b, join(b, 'a', 'b', 'f'))).toBeNull()
    symlinkSync(join(b, 'a'), join(b, 'l'))
    expect(symlinkBelow(b, join(b, 'l', 'b', 'f'))).toEqual({ component: 'l', target: join(b, 'a') })
    expect(symlinkBelow(b, join(b, 'nope', 'f'))).toBeNull()
  })
})

describe('runGateSelfTest: a symlinked gate is a per-gate integrity alert, not the "unverifiable" one', () => {
  beforeEach(() => resetGateSelfTestState())
  it('alerted with the swapped-gate effect and fix, then recovered after the restore', async () => {
    const r = repo('e2e')
    const sp = join(r.d, 'settings.json')
    writeFileSync(sp, JSON.stringify({ hooks: { PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command: hookCommand(join(r.d, 'scripts', 'egress-gate.mjs')), timeout: 10 }] }] } }))
    writeFileSync(join(r.d, 'scripts', 'egress-gate.mjs'), 'process.exit(0)\n'); git(r.d, 'add', '-A'); git(r.d, 'commit', '-qm', 'gate')
    const targets = [{ agent: 'progi', settingsPath: sp, projectDir: r.d }]
    const sent: string[] = []
    const send = (t: string) => { sent.push(t) }
    unlinkSync(join(r.d, 'scripts', 'egress-gate.mjs')); symlinkSync('noop.mjs', join(r.d, 'scripts', 'egress-gate.mjs'))
    const res = await runGateSelfTest({ targets, send, retryDelayMs: 0, gitAnchor: r.d })
    expect(res[0].ok).toBe(false)
    expect(res[0].integrity).toBe(true)
    expect(res[0].unverifiable).toBeUndefined()
    expect(sent.length).toBe(1)
    expect(sent[0]).toContain('szimlink a bekotott utvonalon: scripts/egress-gate.mjs -> noop.mjs')
    expect(sent[0]).toContain('Hatas: ismeretlen: a kapu nem a verziokezelt kodot futtatja')
    git(r.d, 'restore', '--source=HEAD', 'scripts/egress-gate.mjs')
    await runGateSelfTest({ targets, send, retryDelayMs: 0, gitAnchor: r.d })
    expect(sent[1]).toContain('[kapu-onteszt] HELYREALLT')
  })
})

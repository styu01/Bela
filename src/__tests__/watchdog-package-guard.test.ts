// scripts/watchdog.sh starts sub-agents without startAgentProcess, so it carries the package start guard itself:
// upstream 9301a1dd's three conditions (hidden by an install marker, package files missing, shell broken) and this
// fork's owner approval (BELA 6438/6444), read by python3 straight from the approval and the manifest. Run for real
// through the script's --check-package self-test hook on stand-in agent folders.
import { describe, it, expect, afterAll } from 'vitest'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, symlinkSync, renameSync, unlinkSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const root = mkdtempSync(join(tmpdir(), 'wd-pkg-guard-'))
afterAll(() => rmSync(root, { recursive: true, force: true }))
const APPR = join(root, 'appr')
const UPD = join(root, 'updating')
const HIDDEN = join(root, 'hidden.json')
const SHA = 'a'.repeat(64)
const PREFIX = readFileSync(join(process.cwd(), 'scripts', 'lib', 'package-shell-prefix'), 'utf-8')
mkdirSync(APPR)
mkdirSync(UPD)
writeFileSync(HIDDEN, '[]')

function check(dir: string): string {
  const r = spawnSync('bash', ['scripts/watchdog.sh', '--check-package', dir, HIDDEN], {
    cwd: process.cwd(), encoding: 'utf-8', env: { ...process.env, WATCHDOG_PACKAGE_APPROVALS_DIR: APPR, WATCHDOG_PACKAGE_UPDATING_DIR: UPD },
  })
  return r.stdout.trim()
}
function pkgAgent(name: string, version = '1.0.0'): string {
  const d = join(root, 'agents', name)
  mkdirSync(join(d, '.package'), { recursive: true })
  writeFileSync(join(d, 'CLAUDE.md'), PREFIX + '\nrest\n')
  writeFileSync(join(d, 'PACKAGE.md'), 'persona\n')
  writeFileSync(join(d, '.package', 'manifest.json'), JSON.stringify({ schema: 1, payloadSha256: SHA, kid: 'k', installedAt: 'x', updatedAt: 'x', payload: { slug: name, version } }))
  return d
}
function approve(name: string, version = '1.0.0', sha = SHA): void {
  writeFileSync(join(APPR, `${name}.json`), JSON.stringify({ schema: 1, name, slug: name, version, payloadSha256: sha, approvedAt: 't', approvedBy: 'o' }))
}

describe('watchdog.sh package start guard', () => {
  it('a hand-made agent starts as before', () => {
    const d = join(root, 'agents', 'hand')
    mkdirSync(d, { recursive: true })
    writeFileSync(join(d, 'CLAUDE.md'), '# hand\n')
    expect(check(d)).toBe('package=ok')
  })

  it('a package agent with no approval is not started', () => {
    expect(check(pkgAgent('p1'))).toBe('package=refused:Package awaiting owner approval')
  })

  it('an approval that names exactly the installed package lets it start', () => {
    const d = pkgAgent('p2'); approve('p2')
    expect(check(d)).toBe('package=ok')
  })

  it('an update (new version or new payload) needs a new approval', () => {
    const d = pkgAgent('p3', '1.1.0'); approve('p3', '1.0.0')
    expect(check(d)).toBe('package=refused:Package awaiting owner approval')
    const e = pkgAgent('p4'); approve('p4', '1.0.0', 'b'.repeat(64))
    expect(check(e)).toBe('package=refused:Package awaiting owner approval')
  })

  it('an approval file reached through a link does not count', () => {
    const d = pkgAgent('p5'); approve('p5')
    renameSync(join(APPR, 'p5.json'), join(APPR, 'real-p5.json'))
    symlinkSync(join(APPR, 'real-p5.json'), join(APPR, 'p5.json'))
    expect(check(d)).toBe('package=refused:Package awaiting owner approval')
    unlinkSync(join(APPR, 'p5.json'))
  })

  it('an approval for another name does not count', () => {
    const d = pkgAgent('p6')
    writeFileSync(join(APPR, 'p6.json'), JSON.stringify({ schema: 1, name: 'other', slug: 'p6', version: '1.0.0', payloadSha256: SHA, approvedAt: 't', approvedBy: 'o' }))
    expect(check(d)).toBe('package=refused:Package awaiting owner approval')
  })

  it("upstream's three conditions still come first", () => {
    const d = pkgAgent('p7'); approve('p7')
    writeFileSync(HIDDEN, '["p7"]')
    expect(check(d)).toBe('package=refused:Package install incomplete')
    writeFileSync(HIDDEN, '[]')
    unlinkSync(join(d, 'PACKAGE.md'))
    expect(check(d)).toBe('package=refused:Package files missing: run a package repair')
    writeFileSync(join(d, 'PACKAGE.md'), 'persona\n')
    writeFileSync(join(d, 'CLAUDE.md'), '# no shell\n')
    expect(check(d)).toBe('package=refused:Package shell broken: CLAUDE.md must start with the PACKAGE.md and LOCAL.md import lines')
  })
})

// This fork (KISPROGI 6464 H1): while an update trace stands (store/agent-package-updating/<name>.json) the package
// agent is not started, with the server's text (install.ts REFUSAL_UPDATE_IN_PROGRESS); the same scope as marker.ts
// hasUpdatingTrace + isPackageAgentDir.
describe('watchdog.sh: a package update in progress', () => {
  it('an approved package agent with an update trace is not started; without it, it is', () => {
    const d = pkgAgent('u1'); approve('u1')
    writeFileSync(join(UPD, 'u1.json'), '{"from":"1.0.0","to":"1.1.0","startedAt":"x"}')
    expect(check(d)).toBe('package=refused:Package update in progress')
    unlinkSync(join(UPD, 'u1.json'))
    expect(check(d)).toBe('package=ok')
  })
  it('a trace that is a dangling link, a folder or unparsable counts (fail closed)', () => {
    const d = pkgAgent('u2'); approve('u2')
    symlinkSync(join(root, 'nowhere'), join(UPD, 'u2.json'))
    expect(check(d)).toBe('package=refused:Package update in progress')
    unlinkSync(join(UPD, 'u2.json'))
    mkdirSync(join(UPD, 'u2.json'))
    expect(check(d)).toBe('package=refused:Package update in progress')
    rmSync(join(UPD, 'u2.json'), { recursive: true })
    writeFileSync(join(UPD, 'u2.json'), 'not json')
    expect(check(d)).toBe('package=refused:Package update in progress')
    unlinkSync(join(UPD, 'u2.json'))
  })
  it('a folder with a manifest but a broken shell is held by the trace too', () => {
    const d = pkgAgent('u3'); approve('u3')
    writeFileSync(join(d, 'CLAUDE.md'), '# no shell\n')
    writeFileSync(join(UPD, 'u3.json'), '{}')
    expect(check(d)).toBe('package=refused:Package update in progress')
    unlinkSync(join(UPD, 'u3.json'))
  })
  it("a stale trace under a hand-made agent's name does not stop that agent", () => {
    const d = join(root, 'agents', 'hand2')
    mkdirSync(d, { recursive: true })
    writeFileSync(join(d, 'CLAUDE.md'), '# hand\n')
    writeFileSync(join(UPD, 'hand2.json'), '{}')
    expect(check(d)).toBe('package=ok')
    unlinkSync(join(UPD, 'hand2.json'))
  })
})

// The sub-agent loop's order (source pins: the loop needs a live tmux to run, and must never be run by a test: its
// own PATH export would reach the real systemctl and tmux). KISPROGI 6464 M3: the package guard after the bot-token
// check. H1: after the spawn the guard is asked again and a refusal withdraws the session.
describe('watchdog.sh: the sub-agent loop order (KISPROGI 6464 M3, H1)', () => {
  const sh = readFileSync(join(process.cwd(), 'scripts', 'watchdog.sh'), 'utf-8')
  const loop = sh.slice(sh.indexOf('for AGENT_DIR in "$INSTALL_DIR/agents"/*/; do'))
  it('the package guard runs after the bot-token skip, and before the spawn', () => {
    const token = loop.indexOf('if [ -z "$BOT_TOKEN" ]; then')
    const guard = loop.indexOf('PKG_REFUSAL="$(package_start_refusal "$AGENT_DIR" "$AGENT_ID" "$PACKAGE_HIDDEN_LIST")"')
    const spawn = loop.indexOf('tmux new-session -d -s "$SESSION_NAME" "$CMD"')
    expect(token).toBeGreaterThan(0)
    expect(guard).toBeGreaterThan(token)
    expect(spawn).toBeGreaterThan(guard)
  })
  it('right after the spawn: the guard again, and a refusal kills the fresh session and skips the agent', () => {
    const spawn = loop.indexOf('tmux new-session -d -s "$SESSION_NAME" "$CMD"')
    const after = loop.slice(spawn).split('\n').slice(1).filter((l) => l.trim() !== '' && !l.trim().startsWith('#'))
    expect(after[0].trim()).toBe('PKG_REFUSAL="$(package_start_refusal "$AGENT_DIR" "$AGENT_ID" "$PACKAGE_HIDDEN_LIST")"')
    expect(after[1].trim()).toBe('if [ -n "$PKG_REFUSAL" ]; then')
    expect(after[2].trim()).toBe('tmux kill-session -t "=$SESSION_NAME:" 2>/dev/null')
    expect(after.slice(3, 6).map((l) => l.trim())).toContain('continue')
  })
})

// Upstream's watchdog-scaffold-guard.test.sh runs the shared byte cases (scripts/__tests__/fixtures/package-shell-cases.json,
// the same list the TS predicates run) through --check-package; that script also tests a --check-scaffolded hook this
// fork's watchdog does not have, so its package part is carried here instead (LOCAL ADAPTATION, BELA 6404). The shell
// cases run with a valid record and a matching approval, so the answer turns on the prefix bytes alone.
describe('watchdog.sh: the shared package shell byte cases', () => {
  const cases = JSON.parse(readFileSync(join(process.cwd(), 'scripts', '__tests__', 'fixtures', 'package-shell-cases.json'), 'utf-8')) as {
    prefix: Array<{ name: string; b64: string; shell: boolean }>
    blank: Array<{ name: string; b64: string; blank: boolean }>
  }
  let n = 0
  for (const c of cases.prefix) {
    it(`prefix: ${c.name} -> ${c.shell ? 'the shell' : 'not the shell'}`, () => {
      const name = `pc${n++}`
      const d = pkgAgent(name)
      writeFileSync(join(d, 'CLAUDE.md'), Buffer.from(c.b64, 'base64'))
      approve(name)
      expect(check(d)).toBe(c.shell ? 'package=ok' : 'package=refused:Package shell broken: CLAUDE.md must start with the PACKAGE.md and LOCAL.md import lines')
    })
  }
  let m = 0
  for (const c of cases.blank) {
    it(`manifest bytes: ${c.name} -> ${c.blank ? 'no manifest' : 'a manifest'}`, () => {
      const d = join(root, 'agents', `bc${m++}`)
      mkdirSync(join(d, '.package'), { recursive: true })
      writeFileSync(join(d, 'CLAUDE.md'), '# not the shell\n')
      writeFileSync(join(d, '.package', 'manifest.json'), Buffer.from(c.b64, 'base64'))
      expect(check(d)).toBe(c.blank ? 'package=ok' : 'package=refused:Package shell broken: CLAUDE.md must start with the PACKAGE.md and LOCAL.md import lines')
    })
  }
})

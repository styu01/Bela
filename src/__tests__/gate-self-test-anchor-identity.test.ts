import { describe, it, expect, beforeEach, afterAll, afterEach, vi } from 'vitest'
import { mkdtempSync, writeFileSync, rmSync, mkdirSync, realpathSync, statSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { tmpdir } from 'node:os'
import { execFileSync } from 'node:child_process'
import {
  anchorChangedReason, anchorIdentity, runGateSelfTest, resetGateSelfTestState, startGateSelfTest, REAL_GIT,
  type GateAlerter,
} from '../web/gate-self-test.js'
import { hookCommand } from '../web/agent-scaffold.js'
import { PROJECT_ROOT } from '../config.js'

// PROGI 6346 (G), BELA 6345: the install anchor is a PATH taken at boot. A checkout replaced under it (moved away and
// another clone put there, a symlinked root re-pointed) left every verdict judged against the new tree's HEAD. The
// anchor directory's device + inode are taken at boot and compared each pass, with PROJECT_ROOT still resolving into
// the anchor; a change makes every gate 'unverifiable' on the one per-checkout key.

const tmp = realpathSync(mkdtempSync(join(tmpdir(), 'gate-anchor-id-')))
afterAll(() => rmSync(tmp, { recursive: true, force: true }))
const git = (cwd: string, ...args: string[]) => execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false', ...args], { cwd, stdio: 'pipe' }).toString()
function repo(name: string, files: Record<string, string>): string {
  const dir = join(tmp, name)
  for (const [f, body] of Object.entries(files)) { mkdirSync(dirname(join(dir, f)), { recursive: true }); writeFileSync(join(dir, f), body) }
  git(dir, 'init', '-q'); git(dir, 'add', '-A'); git(dir, 'commit', '-q', '-m', 'init')
  return dir
}

describe('anchorChangedReason', () => {
  const boot = { dev: 1, ino: 100 }
  it('the same directory, the root at or below the anchor: null', () => {
    expect(anchorChangedReason('/srv/app', boot, { dev: 1, ino: 100 }, '/srv/app')).toBeNull()
    expect(anchorChangedReason('/srv/app', boot, { dev: 1, ino: 100 }, '/srv/app/sub')).toBeNull()
  })
  it('another inode, another device, a stat failure, or the root resolving elsewhere: a reason naming the anchor and the way out', () => {
    for (const r of [
      anchorChangedReason('/srv/app', boot, { dev: 1, ino: 101 }, '/srv/app'),
      anchorChangedReason('/srv/app', boot, { dev: 2, ino: 100 }, '/srv/app'),
      anchorChangedReason('/srv/app', boot, null, '/srv/app'),
      anchorChangedReason('/srv/app', boot, { dev: 1, ino: 100 }, '/srv/app-new'),
      anchorChangedReason('/srv/app', boot, { dev: 1, ino: 100 }, '/srv'),
    ]) {
      expect(r).toContain('/srv/app')
      expect(r).not.toMatch(/—| -- /)
    }
    expect(anchorChangedReason('/srv/app', boot, { dev: 1, ino: 101 }, '/srv/app')).toContain('kicserelodott')
    expect(anchorChangedReason('/srv/app', boot, { dev: 1, ino: 100 }, '/srv/app-new')).toContain('/srv/app-new')
  })
  it('anchorIdentity: the real device and inode; a missing path: null', () => {
    const st = statSync(tmp)
    expect(anchorIdentity(tmp)).toEqual({ dev: st.dev, ino: st.ino })
    expect(anchorIdentity(join(tmp, 'nope'))).toBeNull()
  })
})

describe('runGateSelfTest: a replaced checkout makes every gate unverifiable on the one key', () => {
  beforeEach(() => resetGateSelfTestState())
  type Ev = { kind: 'fail' | 'recover'; key: string; text: string }
  const setup = (name: string) => {
    const r = repo(name, { 'scripts/egress-gate.mjs': 'process.exit(0)\n', 'scripts/self-pace-gate.mjs': 'process.exit(0)\n' })
    const sp = join(r, 'settings.json')
    writeFileSync(sp, JSON.stringify({ hooks: { PreToolUse: ['egress-gate.mjs', 'self-pace-gate.mjs'].map((g) => ({ matcher: 'Bash', hooks: [{ type: 'command', command: hookCommand(join(r, 'scripts', g)), timeout: 10 }] })) } }))
    const ev: Ev[] = []
    const alerter: GateAlerter = { fail: (key, text) => { ev.push({ kind: 'fail', key, text }) }, recover: (key, text) => { ev.push({ kind: 'recover', key, text }) } }
    const id = anchorIdentity(r)!
    return { r, targets: [{ agent: 'progi', settingsPath: sp, projectDir: r }], ev, alerter, id }
  }

  it('same identity: ok, nothing told', async () => {
    const s = setup('same')
    const res = await runGateSelfTest({ targets: s.targets, alerter: s.alerter, retryDelayMs: 0, git: REAL_GIT, gitAnchor: s.r, anchorIdentity: s.id, projectRoot: s.r })
    expect(res.every((x) => x.ok)).toBe(true)
    expect(s.ev).toEqual([])
  })

  it('another inode: one escalation listing both gates with the reason; back to the boot identity: HELYREALLT', async () => {
    const s = setup('swapped')
    const other = { dev: s.id.dev, ino: s.id.ino + 1 }
    const res = await runGateSelfTest({ targets: s.targets, alerter: s.alerter, retryDelayMs: 0, git: REAL_GIT, gitAnchor: s.r, anchorIdentity: s.id, projectRoot: s.r, stat: () => other })
    expect(res.every((x) => !x.ok && x.unverifiable)).toBe(true)
    expect(s.ev).toHaveLength(1)
    expect(s.ev[0].key).toBe(`gate-integrity-unverifiable:${s.r}`)
    expect(s.ev[0].text).toContain('kicserelodott')
    expect(s.ev[0].text).toContain('egress-gate.mjs')
    expect(s.ev[0].text).toContain('self-pace-gate.mjs')
    await runGateSelfTest({ targets: s.targets, alerter: s.alerter, retryDelayMs: 0, git: REAL_GIT, gitAnchor: s.r, anchorIdentity: s.id, projectRoot: s.r })
    expect(s.ev.map((e) => e.kind)).toEqual(['fail', 'recover'])
  })

  it('PROJECT_ROOT resolving outside the anchor: unverifiable with that reason', async () => {
    const s = setup('rootmoved')
    const elsewhere = join(tmp, 'elsewhere'); mkdirSync(elsewhere, { recursive: true })
    const res = await runGateSelfTest({ targets: s.targets, alerter: s.alerter, retryDelayMs: 0, git: REAL_GIT, gitAnchor: s.r, anchorIdentity: s.id, projectRoot: elsewhere })
    expect(res.every((x) => x.unverifiable)).toBe(true)
    expect(s.ev[0].text).toContain(elsewhere)
  })

  it('a change that is gone by the 60 s re-run is not told (the re-run compares again)', async () => {
    const s = setup('flap')
    let calls = 0
    const stat = (p: string) => { calls++; return calls === 1 ? { dev: s.id.dev, ino: s.id.ino + 9 } : statSync(p) }
    const res = await runGateSelfTest({ targets: s.targets, alerter: s.alerter, retryDelayMs: 0, git: REAL_GIT, gitAnchor: s.r, anchorIdentity: s.id, projectRoot: s.r, stat })
    expect(res.every((x) => x.ok)).toBe(true)
    expect(s.ev).toEqual([])
  })

  it('an anchor injected without an identity: no comparison (the old behaviour)', async () => {
    const s = setup('noid')
    const res = await runGateSelfTest({ targets: s.targets, alerter: s.alerter, retryDelayMs: 0, git: REAL_GIT, gitAnchor: s.r, projectRoot: join(tmp, 'x'), stat: () => ({ dev: 0, ino: 0 }) })
    expect(res.every((x) => x.ok)).toBe(true)
  })
})

describe('startGateSelfTest takes the anchor identity at start', () => {
  afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); resetGateSelfTestState() })
  it('after start, a pass whose stat reports another inode for the install checkout makes its gate unverifiable', async () => {
    const gate = join(PROJECT_ROOT, 'scripts', 'hooks', 'egress-gate.mjs')
    const sp = join(tmp, 'settings-start.json')
    writeFileSync(sp, JSON.stringify({ hooks: { PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command: hookCommand(gate), timeout: 10 }] }] } }))
    const targets = [{ agent: 'progi', settingsPath: sp, projectDir: tmp }]
    vi.useFakeTimers()
    clearInterval(startGateSelfTest())
    vi.useRealTimers()
    const res = await runGateSelfTest({ targets, send: () => {}, retryDelayMs: 0, git: REAL_GIT, stat: () => ({ dev: -1, ino: -1 }) })
    expect(res[0].unverifiable).toBe(true)
    expect(res[0].detail).toContain('kicserelodott')
  })
})

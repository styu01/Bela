import { describe, it, expect, afterAll, afterEach } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, chmodSync, readFileSync, existsSync, realpathSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { REAL_GIT, GIT_CALL_TIMEOUT_MS, detectGitAnchor, checkGateIntegrity } from '../web/gate-self-test.js'

// PROGI 6318 (4): the integrity check's git calls are asynchronous. With execFileSync a hung git froze the
// dashboard's event loop for up to 5 s a call (12 calls a pass, twice with the re-check). A fake `git` first in
// PATH stands in for a hung one.

const tmp = realpathSync(mkdtempSync(join(tmpdir(), 'gate-async-git-')))
const pids: number[] = []
afterAll(() => {
  for (const p of pids) { try { process.kill(p, 'SIGKILL') } catch { /* gone */ } }
  rmSync(tmp, { recursive: true, force: true })
})
const savedPath = process.env.PATH
afterEach(() => { process.env.PATH = savedPath })

const alive = (pid: number): boolean => { try { process.kill(pid, 0); return true } catch { return false } }

/** A `git` that ignores SIGTERM and never answers; writes its own pid (and its background child's, if any). */
function fakeGit(name: string, body: string): string {
  const dir = join(tmp, name)
  mkdirSync(dir, { recursive: true })
  const f = join(dir, 'git')
  writeFileSync(f, `#!/bin/sh\ntrap '' TERM\n${body}\n`)
  chmodSync(f, 0o755)
  return dir
}

/** Run `fn` while a 20 ms ticker runs; returns the result, the ticks seen and the elapsed time. */
async function withTicker<T>(fn: () => Promise<T>): Promise<{ value: T; ticks: number; ms: number }> {
  let ticks = 0
  const t = setInterval(() => { ticks++ }, 20)
  const t0 = Date.now()
  try {
    const value = await fn()
    return { value, ticks, ms: Date.now() - t0 }
  } finally {
    clearInterval(t)
  }
}

describe('REAL_GIT does not block the event loop', () => {
  it('a hung git: null after the deadline, the loop kept running meanwhile, the process is killed (SIGKILL, it ignores TERM)', async () => {
    const dir = fakeGit('hung', `echo $$ > "${join(tmp, 'hung.pid')}"\nexec sleep 30`)
    process.env.PATH = `${dir}:${savedPath}`
    const r = await withTicker(async () => await REAL_GIT.run(tmp, ['rev-parse', '--show-toplevel']))
    const pid = Number(readFileSync(join(tmp, 'hung.pid'), 'utf-8').trim())
    pids.push(pid)
    expect(r.value).toBeNull()
    expect(r.ms).toBeGreaterThanOrEqual(GIT_CALL_TIMEOUT_MS - 100)
    expect(r.ms).toBeLessThan(GIT_CALL_TIMEOUT_MS + 800)
    // ~250 ticks in 5 s when the loop is free; a blocking call allows none until it returns.
    expect(r.ticks).toBeGreaterThan(100)
    await new Promise((res) => setTimeout(res, 100))
    expect(alive(pid)).toBe(false)
  }, 15_000)

  it('a git whose child keeps stdout open past the kill: the call still ends at the deadline', async () => {
    const dir = fakeGit('grandchild', `sleep 30 &\necho $! > "${join(tmp, 'gc.pid')}"\nexec sleep 30`)
    process.env.PATH = `${dir}:${savedPath}`
    const r = await withTicker(async () => await REAL_GIT.run(tmp, ['rev-parse', '--show-toplevel']))
    if (existsSync(join(tmp, 'gc.pid'))) pids.push(Number(readFileSync(join(tmp, 'gc.pid'), 'utf-8').trim()))
    expect(r.value).toBeNull()
    expect(r.ms).toBeLessThan(GIT_CALL_TIMEOUT_MS + 800)
    expect(r.ticks).toBeGreaterThan(100)
  }, 15_000)

  it('a git that reads stdin gets EOF at once (stdin closed), not the deadline', async () => {
    const dir = fakeGit('stdin', `cat > /dev/null\necho "${tmp}"`)
    process.env.PATH = `${dir}:${savedPath}`
    const r = await withTicker(async () => await REAL_GIT.run(tmp, ['rev-parse', '--show-toplevel']))
    expect(r.value).toBe(tmp)
    expect(r.ms).toBeLessThan(2000)
  }, 15_000)

  it('the boot anchor and an integrity verdict under a hung git: the loop keeps running, the old answers come back', async () => {
    const dir = fakeGit('hung2', 'exec sleep 30')
    process.env.PATH = `${dir}:${savedPath}`
    writeFileSync(join(tmp, 'gate.mjs'), 'process.exit(0)\n')
    const r = await withTicker(async () => [
      await detectGitAnchor(tmp, REAL_GIT, () => false),
      await checkGateIntegrity(join(tmp, 'gate.mjs'), REAL_GIT, undefined, undefined, tmp),
    ] as const)
    expect(r.value[0]).toBeNull() // no answer and no .git: not a clone
    expect(r.value[1]?.unverifiable).toBe(true) // under an anchor: unverifiable, not skipped
    expect(r.ticks).toBeGreaterThan(200)
  }, 25_000)
})

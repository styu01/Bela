import { describe, it, expect, afterAll } from 'vitest'
import { spawn, execFileSync, type ChildProcess } from 'node:child_process'
import { readFileSync, mkdtempSync, writeFileSync, chmodSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const CHANNELS = join(REPO_ROOT, 'scripts', 'channels.sh')
const channelsSh = readFileSync(CHANNELS, 'utf-8')

function ownerCheck(botPid: string | number, panePid: string | number, env: Record<string, string> = {}): string {
  return execFileSync('bash', [CHANNELS, '--bot-owner-check', String(botPid), String(panePid)], {
    encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'], env: { ...process.env, ...env },
  }).trim()
}

const stubDirs: string[] = []
function stubPs(body: string, rc = 0): string {
  const dir = mkdtempSync(join(tmpdir(), 'ps-stub-'))
  const path = join(dir, 'ps')
  writeFileSync(path, `#!/bin/sh\nprintf '%s' '${body}'\nexit ${rc}\n`)
  chmodSync(path, 0o755)
  stubDirs.push(dir)
  return path
}
const spawned: ChildProcess[] = []
afterAll(() => {
  for (const dir of stubDirs) rmSync(dir, { recursive: true, force: true })
  for (const child of spawned) { try { process.kill(-child.pid!, 'SIGKILL') } catch { /* gone */ } }
})

async function tree(): Promise<{ root: number; leaf: number }> {
  const child = spawn('bash', ['-c', 'bash -c "sleep 60; true" & wait'], { detached: true, stdio: 'ignore' })
  spawned.push(child)
  const root = child.pid!
  for (let i = 0; i < 50; i++) {
    const out = execFileSync('bash', ['-c', `pgrep -P $(pgrep -P ${root} | head -1) 2>/dev/null || true`], { encoding: 'utf-8' }).trim()
    if (out) return { root, leaf: Number(out.split('\n')[0]) }
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  throw new Error('process tree did not come up')
}

describe('bot.pid owner check', () => {
  it('recognises a pane descendant as own and another tree as foreign', async () => {
    const own = await tree()
    const foreign = await tree()
    expect(ownerCheck(own.leaf, own.root)).toBe('own')
    expect(ownerCheck(foreign.leaf, own.root)).toBe('foreign')
  })

  it('returns unknown for invalid and unmeasurable parent chains', () => {
    expect(ownerCheck('', 123)).toBe('unknown')
    expect(ownerCheck(5000, 4000, { CHANNELS_PS_BIN: stubPs('', 1) })).toBe('unknown')
    expect(ownerCheck(5000, 4000, { CHANNELS_PS_BIN: stubPs('1') })).toBe('foreign')
  })
})

function watchdogLoop(): string {
  const start = channelsSh.indexOf('while $TMUX has-session -t "$SESSION"')
  expect(start).toBeGreaterThan(0)
  const end = channelsSh.indexOf('\ndone\n', start)
  expect(end).toBeGreaterThan(start)
  return channelsSh.slice(start, end)
}

describe('watchdog hijack wiring', () => {
  it('re-reads the pane pid and checks bot.pid ownership before marking it alive', () => {
    const loop = watchdogLoop()
    const reread = loop.indexOf('_pane_pid_now="$($TMUX list-panes')
    const check = loop.indexOf('bot_pid_descends_from "$_bot_pid" "$_watchdog_claude_pid"')
    const alive = loop.indexOf('_plugin_alive=true')
    expect(reread).toBeGreaterThan(0)
    expect(check).toBeGreaterThan(reread)
    expect(alive).toBeGreaterThan(check)
  })

  it('does not let the bun-child fallback mask a confirmed hijack', () => {
    expect(watchdogLoop()).toContain('[ "$_plugin_alive" != "true" ] && [ "$_bot_hijacked" != "true" ]')
  })
})

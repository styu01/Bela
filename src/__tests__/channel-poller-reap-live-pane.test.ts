import { describe, it, expect, afterAll } from 'vitest'
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync, chmodSync, mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { reapChannelOrphans } from '../web/channel-poller-reap.js'
import { channelStateDir } from '../channel-provider.js'

const tmp = mkdtempSync(join(tmpdir(), 'reap-live-pane-'))
const agentDir = join(tmp, 'agent')
const chanDir = channelStateDir('telegram', agentDir)
mkdirSync(chanDir, { recursive: true })
const fakeTmux = join(tmp, 'fake-tmux')
const kids: number[] = []

function victim(): number {
  const p = spawn('/bin/sleep', ['300'], {
    detached: true,
    stdio: 'ignore',
    env: { ...process.env, TELEGRAM_STATE_DIR: chanDir },
  })
  p.unref()
  kids.push(p.pid!)
  return p.pid!
}
function fakeTmuxPrints(body: string): void {
  writeFileSync(fakeTmux, `#!/bin/sh\n${body}\n`)
  chmodSync(fakeTmux, 0o755)
}
const alive = (pid: number): boolean => { try { process.kill(pid, 0); return true } catch { return false } }
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

afterAll(() => {
  for (const kid of kids) { try { process.kill(kid, 'SIGKILL') } catch { /* gone */ } }
  rmSync(tmp, { recursive: true, force: true })
})

describe('reapChannelOrphans fail-safe tmux guards', () => {
  it('spares a candidate that is a live pane leader', async () => {
    const pid = victim(); await sleep(150)
    writeFileSync(join(chanDir, 'bot.pid'), String(pid))
    fakeTmuxPrints(`case "$1" in display-message) echo 5000 ;; *) echo ${pid}; echo 4242 ;; esac`)
    const result = reapChannelOrphans('telegram', agentDir, { tmuxPath: fakeTmux })
    await sleep(500)
    expect(result.skippedLivePane).toContain(pid)
    expect(result.reaped).not.toContain(pid)
    expect(alive(pid)).toBe(true)
  })

  it('refuses to reap when the pane query fails', async () => {
    const pid = victim(); await sleep(150)
    writeFileSync(join(chanDir, 'bot.pid'), String(pid))
    fakeTmuxPrints('exit 1')
    const result = reapChannelOrphans('telegram', agentDir, { tmuxPath: fakeTmux })
    await sleep(500)
    expect(result.reaped).toEqual([])
    expect(alive(pid)).toBe(true)
  })

  it('reaps a candidate which is neither a pane leader nor tmux server', async () => {
    const pid = victim(); await sleep(150)
    writeFileSync(join(chanDir, 'bot.pid'), String(pid))
    fakeTmuxPrints('case "$1" in display-message) echo 5000 ;; *) echo 4242 ;; esac')
    const result = reapChannelOrphans('telegram', agentDir, { tmuxPath: fakeTmux })
    await sleep(800)
    expect(result.reaped).toContain(pid)
    expect(alive(pid)).toBe(false)
  })

  it('spares a candidate that is the tmux server itself', async () => {
    const pid = victim(); await sleep(150)
    writeFileSync(join(chanDir, 'bot.pid'), String(pid))
    fakeTmuxPrints(`case "$1" in display-message) echo ${pid} ;; *) echo 4242 ;; esac`)
    const result = reapChannelOrphans('telegram', agentDir, { tmuxPath: fakeTmux })
    await sleep(500)
    expect(result.reaped).not.toContain(pid)
    expect(result.skippedTmuxServer).toBe(true)
    expect(alive(pid)).toBe(true)
  })

  it('refuses to reap when the tmux server pid cannot be resolved', async () => {
    const pid = victim(); await sleep(150)
    writeFileSync(join(chanDir, 'bot.pid'), String(pid))
    fakeTmuxPrints('case "$1" in display-message) exit 1 ;; *) echo 4242 ;; esac')
    const result = reapChannelOrphans('telegram', agentDir, { tmuxPath: fakeTmux })
    await sleep(500)
    expect(result.reaped).toEqual([])
    expect(alive(pid)).toBe(true)
  })
})

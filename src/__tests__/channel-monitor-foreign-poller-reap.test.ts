import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const SOURCE = readFileSync(join(process.cwd(), 'src/web/channel-monitor.ts'), 'utf8')

describe('channel monitor foreign main-poller reap wiring', () => {
  it('reaps a foreign Telegram poller both on the first down spell and periodically', () => {
    const calls = SOURCE.match(/reapForeignMainPollers\(\{[\s\S]*?provider: 'telegram'[\s\S]*?mainSession: MAIN_CHANNELS_SESSION[\s\S]*?tmuxPath: TMUX[\s\S]*?\}\)/g) ?? []
    expect(calls).toHaveLength(2)
  })

  it('keeps foreign-poller cleanup independent of the context restart gate', () => {
    const foreignReapAt = SOURCE.indexOf('reapForeignMainPollers')
    expect(foreignReapAt).toBeGreaterThanOrEqual(0)
    expect(SOURCE.slice(foreignReapAt - 500, foreignReapAt + 1000)).not.toContain('context-restart-gate')
  })
})

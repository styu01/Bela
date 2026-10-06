import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'
import { matchesProviderPollerCmd } from '../channel-coordinator/provider-poller-match.js'

const PROBE = join(process.cwd(), 'scripts', 'channel-keepalive-probe.sh')

function probeRuntimeRegex(): string {
  const source = readFileSync(PROBE, 'utf-8')
  const match = source.match(/^RUNTIME_TOKEN_RX='([^']+)'$/m)
  expect(match, 'RUNTIME_TOKEN_RX not found').not.toBeNull()
  return match![1]
}

function shellAccepts(command: string): boolean {
  try {
    const output = execFileSync('/bin/sh', [
      '-c', `printf '%s\\n' "$1" | grep -E "$2" | grep -E '/telegram/' | grep -v grep`, 'sh', command, probeRuntimeRegex(),
    ], { encoding: 'utf-8' })
    return output.trim().length > 0
  } catch {
    return false
  }
}

const POLLERS = [
  '331 /home/u/.bun/bin/bun run --cwd /home/u/.claude/plugins/cache/telegram/0.0.6 start',
  '332 bun run --cwd /home/u/.claude/plugins/cache/telegram/0.0.6 start',
  '333 /usr/local/bin/node /home/u/.claude/plugins/cache/telegram/server.js',
]
const NON_POLLERS = [
  '999 /usr/lib/node_modules/tool/cli.js --plugin /telegram/x',
  '111 python3 /home/u/.claude/plugins/cache/telegram/bot.py',
  '222 /home/u/.bun/bin/bun run --cwd /home/u/.claude/plugins/cache/discord/0.1 start',
]

describe('channel keepalive probe runtime regex', () => {
  it('accepts bare and full-path bun/node telegram pollers', () => {
    for (const command of POLLERS) expect(shellAccepts(command), command).toBe(true)
  })
  it('rejects non-pollers', () => {
    for (const command of NON_POLLERS) expect(shellAccepts(command), command).toBe(false)
  })
  it('uses no non-portable word-boundary escape', () => {
    expect(probeRuntimeRegex()).not.toContain('\\b')
  })
  it('agrees with the TypeScript detector', () => {
    for (const command of POLLERS) {
      expect(matchesProviderPollerCmd(command, 'telegram')).toBe(true)
      expect(shellAccepts(command)).toBe(true)
    }
    for (const command of NON_POLLERS) {
      expect(matchesProviderPollerCmd(command, 'telegram')).toBe(false)
      expect(shellAccepts(command)).toBe(false)
    }
  })
})

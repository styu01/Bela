import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const SRC = readFileSync(join(__dirname, '..', 'web', 'agent-process.ts'), 'utf-8')

function epermWatcherBody(): string {
  const start = SRC.indexOf('const checkEperm = () => {')
  expect(start, 'checkEperm not found').toBeGreaterThan(-1)
  const end = SRC.indexOf('setTimeout(checkEperm, 1500)', start)
  expect(end, 'checkEperm scheduling tail not found').toBeGreaterThan(start)
  return SRC.slice(start, end)
}

describe('EPERM fallback dialog scope', () => {
  it('answers startup dialogs only after the /tmp relaunch', () => {
    const body = epermWatcherBody()
    const guard = body.indexOf('if (epermRestarted) {')
    const bypass = body.indexOf('Bypass Permissions mode')
    expect(guard).toBeGreaterThan(-1)
    expect(bypass).toBeGreaterThan(guard)
    expect(body.slice(0, guard)).not.toContain("'send-keys'")
  })

  it('uses a one-shot /tmp launch with the real agent CLAUDE.md linked in', () => {
    const body = epermWatcherBody()
    expect(body).toContain('mkdtempSync')
    expect(body).toContain('epermRestarted = true')
    expect(body).toContain('buildLaunchCmd(fallbackCwd)')
    expect(body).toContain("join(dir, 'CLAUDE.md')")
  })
})

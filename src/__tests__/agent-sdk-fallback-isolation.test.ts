import { describe, expect, it } from 'vitest'
import { mkdtempSync, readFileSync, rmSync, symlinkSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { tmpdir } from 'node:os'
import { PROJECT_ROOT } from '../config.js'
import { ensureSdkFallbackDirectory, sdkFallbackSpawnIsolation } from '../agent.js'

describe('legacy SDK fallback isolation', () => {
  it('moves default PROJECT_ROOT calls to a private neutral cwd and excludes settings sources', () => {
    const options = sdkFallbackSpawnIsolation(PROJECT_ROOT)
    expect(options.cwd).not.toBe(PROJECT_ROOT)
    expect(options.cwd).toContain('/store/.sdk-fallback')
    expect(options.settingSources).toEqual([])
    expect(options.env.CLAUDE_CONFIG_DIR).toContain('/store/.sdk-fallback/.claude-config')
  })

  it('preserves a caller-owned isolated cwd/config rather than breaking heartbeat or digest setup', () => {
    const options = sdkFallbackSpawnIsolation('/tmp/existing-isolated-worker', { CLAUDE_CONFIG_DIR: '/tmp/existing-config' })
    expect(options.cwd).toBe('/tmp/existing-isolated-worker')
    expect(options.env.CLAUDE_CONFIG_DIR).toBe('/tmp/existing-config')
    expect(options).not.toHaveProperty('settingSources')
  })

  it('rejects a symlink at the private fallback root instead of following it', () => {
    const dir = mkdtempSync(join(tmpdir(), 'sdk-fallback-test-'))
    const target = join(dir, 'target')
    const link = join(dir, 'fallback')
    try {
      symlinkSync(target, link)
      expect(() => ensureSdkFallbackDirectory(link)).toThrow(/unsafe SDK fallback directory/)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('uses the isolation options only after the normal worker path has declined the request', () => {
    const here = dirname(fileURLToPath(import.meta.url))
    const source = readFileSync(join(here, '../agent.ts'), 'utf-8')
    expect(source.indexOf('if (!authFailed) return { text, error }')).toBeLessThan(source.indexOf('sdkFallbackSpawnIsolation(cwd, env)'))
    expect(source).toContain('cwd: sdkIsolation.cwd')
    expect(source).toContain('...(sdkIsolation.settingSources ? { settingSources: sdkIsolation.settingSources } : {})')
    expect(source).not.toContain('plugins: sdkIsolation.plugins')
  })
})

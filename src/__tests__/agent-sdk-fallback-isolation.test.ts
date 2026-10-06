import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { PROJECT_ROOT } from '../config.js'
import { sdkFallbackSpawnIsolation } from '../agent.js'

describe('legacy SDK fallback isolation', () => {
  it('moves default PROJECT_ROOT calls to a neutral cwd and explicitly disables filesystem plugins', () => {
    const options = sdkFallbackSpawnIsolation(PROJECT_ROOT)
    expect(options.cwd).not.toBe(PROJECT_ROOT)
    expect(options.cwd).not.toContain('/marveen/')
    expect(options.settingSources).toEqual([])
    expect(options.plugins).toEqual([])
    expect(options.env.CLAUDE_CONFIG_DIR).toContain('marveen-sdk-fallback')
  })

  it('preserves a caller-owned isolated cwd/config rather than breaking heartbeat or digest setup', () => {
    const options = sdkFallbackSpawnIsolation('/tmp/existing-isolated-worker', { CLAUDE_CONFIG_DIR: '/tmp/existing-config' })
    expect(options.cwd).toBe('/tmp/existing-isolated-worker')
    expect(options.env.CLAUDE_CONFIG_DIR).toBe('/tmp/existing-config')
    expect(options.settingSources).toEqual([])
    expect(options.plugins).toEqual([])
  })

  it('uses the isolation options only after the normal worker path has declined the request', () => {
    const here = dirname(fileURLToPath(import.meta.url))
    const source = readFileSync(join(here, '../agent.ts'), 'utf-8')
    expect(source.indexOf('if (!authFailed) return { text, error }')).toBeLessThan(source.indexOf('sdkFallbackSpawnIsolation(cwd, env)'))
    expect(source).toContain('cwd: sdkIsolation.cwd')
    expect(source).toContain('settingSources: sdkIsolation.settingSources')
    expect(source).toContain('plugins: sdkIsolation.plugins')
  })
})

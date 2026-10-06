import { afterEach, describe, expect, it, vi } from 'vitest'
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const ROOT = mkdtempSync(join(tmpdir(), 'model-fallback-main-env-'))
vi.mock('../config.js', async () => {
  const actual = await vi.importActual<typeof import('../config.js')>('../config.js')
  return { ...actual, PROJECT_ROOT: ROOT }
})
vi.mock('../logger.js', () => ({ logger: { info: vi.fn(), warn: vi.fn(), debug: vi.fn(), error: vi.fn() } }))

const { _mainModelIoForTest: io } = await import('../web/model-fallback-runner.js')
const ENV = join(ROOT, '.env')
const SETTINGS = join(ROOT, '.claude', 'settings.json')

function seed(env: string, settings: string) {
  mkdirSync(join(ROOT, '.claude'), { recursive: true })
  writeFileSync(ENV, env)
  chmodSync(ENV, 0o600)
  writeFileSync(SETTINGS, settings)
}

afterEach(() => { seed('', JSON.stringify({ model: 'claude-sonnet-5' })) })

describe('main model fallback follows the launch source', () => {
  it('uses MAIN_AGENT_MODEL in .env before settings.json and preserves the tracked file', () => {
    seed('SECRET=x\nMAIN_AGENT_MODEL=claude-opus-5\n', JSON.stringify({ model: 'claude-sonnet-5' }))
    expect(io.readMainModel()).toBe('claude-opus-5')
    io.writeMainModel('claude-sonnet-5')
    expect(readFileSync(ENV, 'utf8')).toContain('MAIN_AGENT_MODEL=claude-sonnet-5')
    expect(JSON.parse(readFileSync(SETTINGS, 'utf8')).model).toBe('claude-sonnet-5')
  })

  it('refuses to clobber a corrupt settings file when no env override exists', () => {
    const corrupt = '{"model":'
    seed('SECRET=x\n', corrupt)
    expect(() => io.writeMainModel('claude-opus-5')).toThrow()
    expect(readFileSync(SETTINGS, 'utf8')).toBe(corrupt)
  })
})

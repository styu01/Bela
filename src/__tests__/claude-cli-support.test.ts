import { describe, expect, it } from 'vitest'
import {
  CLAUDE_MODEL_MIN_CLI,
  baseModelId,
  claudeSupportForCli,
  compareVersions,
  isModelUnsupportedByCli,
  parseClaudeVersion,
} from '../claude-cli-support.js'
import { resolveModelId } from '../web/agent-config.js'

describe('Claude CLI model support table', () => {
  it('parses and numerically compares Claude CLI versions', () => {
    expect(parseClaudeVersion('2.1.283 (Claude Code)')).toBe('2.1.283')
    expect(parseClaudeVersion('Not logged in')).toBeNull()
    expect(compareVersions('2.1.220', '2.1.283')).toBeLessThan(0)
    expect(compareVersions('2.1.283', '2.1.283')).toBe(0)
  })

  it('gates Sonnet 5.5 at the locally measured 2.1.220 minimum without changing the sonnet alias target', () => {
    expect(CLAUDE_MODEL_MIN_CLI['claude-sonnet-5-5']?.minCli).toBe('2.1.220')
    expect(isModelUnsupportedByCli('claude-sonnet-5-5', '2.1.219')).toBe(true)
    expect(isModelUnsupportedByCli('claude-sonnet-5-5', '2.1.220')).toBe(false)
    expect(isModelUnsupportedByCli('claude-sonnet-5', '2.1.220')).toBe(false)
    expect(resolveModelId('sonnet')).toBe('claude-sonnet-5')
    expect(resolveModelId('sonnet55')).toBe('claude-sonnet-5-5')
  })

  it('offers Opus 5.5 only through its 1M variant while sharing the base compatibility gate', () => {
    expect(baseModelId('claude-opus-5-5[1m]')).toBe('claude-opus-5-5')
    expect(isModelUnsupportedByCli('claude-opus-5-5[1m]', '2.1.279')).toBe(true)
    expect(isModelUnsupportedByCli('claude-opus-5-5[1m]', '2.1.280')).toBe(false)
  })

  it('fails open only when the CLI cannot be measured', () => {
    expect(claudeSupportForCli(null)).toMatchObject({ measured: false, unsupported: [] })
    expect(claudeSupportForCli('2.1.219').unsupported.map((entry) => entry.id))
      .toContain('claude-sonnet-5-5')
    expect(claudeSupportForCli('2.1.220').unsupported.map((entry) => entry.id))
      .not.toContain('claude-sonnet-5-5')
  })
})

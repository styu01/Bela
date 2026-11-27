// PROGI review 6196 / BELA 6197 (urgent before the Saturday switch back to sonnet 5.5): a DATED model
// id (claude-sonnet-5-5-YYYYMMDD) must get the 1M window like the bare id, not the 200k fallback --
// otherwise the guard hands off a 1M session at ~180k (prepPct 0.85 of 200k).
import { describe, expect, it } from 'vitest'
import { contextLimitForModel } from '../context-guard.js'

const ONE_M = contextLimitForModel('claude-sonnet-5-5')

describe('contextLimitForModel with dated ids', () => {
  it('control: the bare sonnet 5 / 5.5 ids are the 1M class', () => {
    expect(ONE_M).toBeGreaterThan(200_000)
    expect(contextLimitForModel('claude-sonnet-5')).toBe(ONE_M)
  })
  it.each(['claude-sonnet-5-5-20261015', 'claude-sonnet-5-20260801', 'CLAUDE-SONNET-5-5-20261015'])('%s -> the same 1M window', (id) => {
    expect(contextLimitForModel(id)).toBe(ONE_M)
  })
  it('opus dated ids were already right and stay so', () => {
    expect(contextLimitForModel('claude-opus-5-5-20261015')).toBe(ONE_M)
  })
  it.each(['claude-haiku-4-5-20251001', 'claude-haiku-5-5', 'claude-sonnet-4-5-20250929', 'claude-sonnet-5-5-2026101', 'claude-sonnet-5-5-x', 'my-claude-sonnet-5-5'])('%s stays 200k (no over-estimate)', (id) => {
    expect(contextLimitForModel(id)).toBe(200_000)
  })
})

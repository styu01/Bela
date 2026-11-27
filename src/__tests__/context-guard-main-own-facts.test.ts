// STUCKOWNSID1127 follow-up (BELA msg 6173 item 2): the context-guard measures the MAIN agent's
// context size and model from its OWN transcript, not from the newest file of the shared marveen-root
// project dir. The danger the directory read carried: a foreign session near its own 200k window,
// measured as BELA, crosses prepPct/actPct/hardPct and triggers a FALSE handoff/restart.
import { describe, it, expect, vi } from 'vitest'
import { mkdtempSync, writeFileSync, utimesSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { contextTokensFromContent, activeModelFromContent, readContextTokensFromFile, readActiveModelFromFile } from '../web/active-model.js'
import { readMainGuardFacts, type GuardReadDeps } from '../web/context-guard-runner.js'
import { PROJECT_ROOT } from '../config.js'
import { contextLimitForModel } from '../context-guard.js'

const line = (o: unknown) => JSON.stringify(o)
const usage = (inp: number, cr: number, cc: number, model = 'claude-opus-5-5', ts = '2026-11-27T08:00:00.000Z') =>
  line({ type: 'assistant', timestamp: ts, message: { model, usage: { input_tokens: inp, cache_read_input_tokens: cr, cache_creation_input_tokens: cc } } })

describe('the pure content readers (shared by the directory and the own-file read)', () => {
  it('tokens = input + cache_read + cache_creation of the LAST line with a usage', () => {
    const c = [usage(1, 2, 3), usage(10, 100, 1000), line({ type: 'user', message: { content: 'x' } })].join('\n')
    expect(contextTokensFromContent(c)).toBe(1110)
    expect(contextTokensFromContent(line({ type: 'user' }))).toBeNull()
  })
  it('model = the last real model; a "<synthetic>" CLI line is skipped', () => {
    const c = [usage(1, 1, 1, 'claude-sonnet-5-5'), line({ type: 'assistant', message: { model: '<synthetic>' } })].join('\n')
    expect(activeModelFromContent(c)).toBe('claude-sonnet-5-5')
    expect(activeModelFromContent(c, Date.parse('2026-11-28T00:00:00Z') / 1000)).toBeNull()
  })
})

describe('the own-file read is memoised on (path, mtime) and re-reads a changed file', () => {
  it('same mtime -> the memo; a new write -> the new value', () => {
    const dir = mkdtempSync(join(tmpdir(), 'guard-own-'))
    try {
      const p = join(dir, 'own.jsonl')
      writeFileSync(p, usage(5, 5, 5, 'claude-opus-5-5') + '\n')
      utimesSync(p, 1000, 1000)
      expect(readContextTokensFromFile(p)).toBe(15)
      expect(readActiveModelFromFile(p)).toBe('claude-opus-5-5')
      writeFileSync(p, readFileSync(p, 'utf-8') + usage(50, 50, 50, 'claude-sonnet-5-5') + '\n')
      utimesSync(p, 2000, 2000)
      expect(readContextTokensFromFile(p)).toBe(150)
      expect(readActiveModelFromFile(p)).toBe('claude-sonnet-5-5')
      expect(readContextTokensFromFile(join(dir, 'missing.jsonl'))).toBeNull()
    } finally { rmSync(dir, { recursive: true, force: true }) }
  })
})

function deps(over: Partial<GuardReadDeps> = {}) {
  return {
    ownPath: vi.fn(() => '/x/own.jsonl' as string | null),
    tokensFromFile: vi.fn((_p: string) => 100_000 as number | null),          // BELA: 100k on a 1M model
    modelFromFile: vi.fn((_p: string) => 'claude-sonnet-5-5' as string | null),
    tokensFromDir: vi.fn((_w: string, _c?: string) => 190_000 as number | null), // foreign: 190k on a 200k model
    modelFromDir: vi.fn((_w: string) => 'claude-haiku-4-5-20251001' as string | null),
    ...over,
  }
}

describe('readMainGuardFacts', () => {
  it('proven own file: tokens AND model from it; the foreign newest file is not read', () => {
    const d = deps()
    const f = readMainGuardFacts(d)
    expect(f).toEqual({ tokens: 100_000, model: 'claude-sonnet-5-5', source: 'own' })
    expect(d.tokensFromDir).not.toHaveBeenCalled()
    expect(d.modelFromDir).not.toHaveBeenCalled()
    // what the guard computes from it, against what the directory read would have produced
    expect(f.tokens! / contextLimitForModel(f.model)).toBeLessThan(0.15)
    expect(190_000 / contextLimitForModel('claude-haiku-4-5-20251001')).toBeGreaterThan(0.9)   // the false-restart reading avoided
  })
  it('proven own file without a usage yet -> tokens null (unmeasurable), NO fallback to a foreign file', () => {
    const d = deps({ tokensFromFile: vi.fn(() => null) })
    expect(readMainGuardFacts(d).tokens).toBeNull()
    expect(d.tokensFromDir).not.toHaveBeenCalled()
  })
  it('not provable -> both values from the old directory reads, with the same arguments as before', () => {
    const d = deps({ ownPath: vi.fn(() => null) })
    expect(readMainGuardFacts(d)).toEqual({ tokens: 190_000, model: 'claude-haiku-4-5-20251001', source: 'dir' })
    expect((d.tokensFromDir as unknown as { mock: { calls: unknown[][] } }).mock.calls[0]![0]).toBe(PROJECT_ROOT)
    expect(d.modelFromDir).toHaveBeenCalledWith(PROJECT_ROOT)
    expect(d.tokensFromFile).not.toHaveBeenCalled()
  })
})

describe('wiring: the main agent goes through readMainGuardFacts in both measurements', () => {
  const src = readFileSync(join(__dirname, '..', 'web', 'context-guard-runner.ts'), 'utf-8')
  const body = (fn: string) => {
    const at = src.indexOf(`function ${fn}(`)
    expect(at, fn).toBeGreaterThan(0)
    return src.slice(at, src.indexOf('\n}\n', at)).split('\n').filter((l) => !l.trim().startsWith('//')).join('\n')
  }
  it('measurePct: one facts read for the main agent feeds both tokens and model', () => {
    const b = body('measurePct')
    expect(b).toContain('const mainFacts = isMain ? readMainGuardFacts() : null')
    expect(b).toContain('const tokens = mainFacts ? mainFacts.tokens : readContextTokensFromProjectDir(workingDir, configDir)')
    expect(b).toMatch(/const model = \(mainFacts\s*\n\s*\? mainFacts\.model/)
  })
  it('measureContextTokens: the main agent reads readMainGuardFacts().tokens', () => {
    expect(body('measureContextTokens')).toContain('? readMainGuardFacts().tokens')
  })
})

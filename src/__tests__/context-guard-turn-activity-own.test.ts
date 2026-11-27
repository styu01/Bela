// BELA msg 6197 item 4 (PROGI 6196): the restart's "is a turn still running" check reads the MAIN
// agent's own transcript, not the newest file of the shared project dir (a foreign session's fresh
// line used to hold BELA's restart back).
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { lastTurnActivityFromFile } from '../web/active-model.js'
import { guardLastTurnActivityMs } from '../web/context-guard-runner.js'
import { MAIN_AGENT_ID, PROJECT_ROOT } from '../config.js'

const dirs: string[] = []
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }) })
const file = (lines: unknown[]) => {
  const d = mkdtempSync(join(tmpdir(), 'turn-own-')); dirs.push(d)
  const p = join(d, 't.jsonl'); writeFileSync(p, lines.map((l) => JSON.stringify(l)).join('\n') + '\n'); return p
}

describe('lastTurnActivityFromFile', () => {
  it('the last user/assistant line, skipping a local slash command echo', () => {
    const p = file([
      { type: 'assistant', timestamp: '2026-11-27T10:00:00.000Z' },
      { type: 'user', timestamp: '2026-11-27T10:05:00.000Z', message: { content: '<command-name>/model</command-name>' } },
      { type: 'system', timestamp: '2026-11-27T10:06:00.000Z' },
    ])
    expect(lastTurnActivityFromFile(p)).toBe(Date.parse('2026-11-27T10:00:00.000Z'))
  })
  it('a fresh session with bookkeeping only -> 0 (quiet), an unreadable file -> null', () => {
    expect(lastTurnActivityFromFile(file([{ type: 'custom-title' }]))).toBe(0)
    expect(lastTurnActivityFromFile('/nonexistent/x.jsonl')).toBeNull()
  })
})

describe('guardLastTurnActivityMs', () => {
  const deps = (own: string | null) => ({
    ownPath: vi.fn(() => own),
    fromFile: vi.fn((_p: string) => 111 as number | null),
    fromDir: vi.fn((_w: string, _c?: string) => 999 as number | null),   // a foreign session's fresh line
  })
  it('main agent with a proven own file: read from it, the shared dir is not consulted', () => {
    const d = deps('/x/own.jsonl')
    expect(guardLastTurnActivityMs(MAIN_AGENT_ID, d)).toBe(111)
    expect(d.fromFile).toHaveBeenCalledWith('/x/own.jsonl')
    expect(d.fromDir).not.toHaveBeenCalled()
  })
  it('main agent not provable -> the old directory read', () => {
    const d = deps(null)
    expect(guardLastTurnActivityMs(MAIN_AGENT_ID, d)).toBe(999)
    expect(d.fromDir.mock.calls[0]![0]).toBe(PROJECT_ROOT)
  })
  it('a sub-agent keeps the directory read and never asks for the main session', () => {
    const d = deps('/x/own.jsonl')
    expect(guardLastTurnActivityMs('progi-turn-test', d)).toBe(999)
    expect(d.ownPath).not.toHaveBeenCalled()
  })
})

describe('wiring', () => {
  it('the restart turn-quiet check calls guardLastTurnActivityMs(name), not the raw directory read', async () => {
    const { readFileSync } = await import('node:fs')
    const src = readFileSync(join(__dirname, '..', 'web', 'context-guard-runner.ts'), 'utf-8')
    const at = src.indexOf("if (decision.action === 'restart' && !inputs.paneBusy) {")
    const block = src.slice(at, at + 200)
    expect(block).toContain('const turnAt = guardLastTurnActivityMs(name)')
  })
})

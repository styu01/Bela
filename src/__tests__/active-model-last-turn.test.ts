// readLastTurnActivityMs (ported from upstream 26ac8c83 for 9d6b614b's restart turn-quiet check): the epoch ms of
// the last real TURN line (user / assistant) in the newest transcript. Bookkeeping lines and local slash-command
// echoes do not count; a small transcript with no turn at all reads 0 ("never had a turn"), not null.

import { describe, it, expect, afterAll } from 'vitest'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync, utimesSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { readLastTurnActivityMs, projectsDirFor } from '../web/active-model.js'

const ROOT = mkdtempSync(join(tmpdir(), 'lastturn-'))
afterAll(() => rmSync(ROOT, { recursive: true, force: true }))

let n = 0
function transcript(lines: unknown[], extraFiles: Array<{ lines: unknown[]; ageMs: number }> = []): { wd: string; cfg: string } {
  const wd = `/work/agent${n++}`
  const cfg = join(ROOT, `cfg${n}`)
  const dir = projectsDirFor(wd, cfg)
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'newest.jsonl'), lines.map((l) => JSON.stringify(l)).join('\n') + '\n')
  for (const [i, f] of extraFiles.entries()) {
    const p = join(dir, `old${i}.jsonl`)
    writeFileSync(p, f.lines.map((l) => JSON.stringify(l)).join('\n') + '\n')
    const t = (Date.now() - f.ageMs) / 1000
    utimesSync(p, t, t)
  }
  return { wd, cfg }
}

const TS = (iso: string) => new Date(iso).getTime()

describe('readLastTurnActivityMs', () => {
  it('returns the last user/assistant line, skipping bookkeeping after it', () => {
    const { wd, cfg } = transcript([
      { type: 'user', timestamp: '2026-11-27T08:00:00.000Z', message: { content: 'do it' } },
      { type: 'assistant', timestamp: '2026-11-27T08:00:05.000Z', message: { content: [] } },
      { type: 'queue-operation', timestamp: '2026-11-27T08:01:00.000Z' },
      { type: 'system', timestamp: '2026-11-27T08:01:01.000Z', content: 'UserPromptSubmit operation blocked by hook' },
    ])
    expect(readLastTurnActivityMs(wd, cfg)).toBe(TS('2026-11-27T08:00:05.000Z'))
  })

  it('a local slash-command echo is not a turn', () => {
    const { wd, cfg } = transcript([
      { type: 'assistant', timestamp: '2026-11-27T08:00:05.000Z', message: { content: [] } },
      { type: 'user', timestamp: '2026-11-27T08:02:00.000Z', message: { content: '<command-name>/model</command-name>' } },
      { type: 'user', timestamp: '2026-11-27T08:02:01.000Z', message: { content: '<local-command-stdout>Set model</local-command-stdout>' } },
    ])
    expect(readLastTurnActivityMs(wd, cfg)).toBe(TS('2026-11-27T08:00:05.000Z'))
  })

  it('a small transcript with no turn reads 0 (quiet), not null', () => {
    const { wd, cfg } = transcript([{ type: 'queue-operation', timestamp: '2026-11-27T08:01:00.000Z' }])
    expect(readLastTurnActivityMs(wd, cfg)).toBe(0)
  })

  it('reads the NEWEST transcript only', () => {
    const { wd, cfg } = transcript(
      [{ type: 'assistant', timestamp: '2026-11-27T09:00:00.000Z', message: { content: [] } }],
      [{ lines: [{ type: 'assistant', timestamp: '2026-11-27T10:00:00.000Z', message: { content: [] } }], ageMs: 3_600_000 }],
    )
    expect(readLastTurnActivityMs(wd, cfg)).toBe(TS('2026-11-27T09:00:00.000Z'))
  })

  it('no transcript directory is null (unknown), so the caller keeps its decision', () => {
    expect(readLastTurnActivityMs('/work/none', join(ROOT, 'nothing'))).toBeNull()
  })
})

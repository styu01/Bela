// STUCKOWNSID1127 follow-up (PROGI msg 6169 item 3b): the context-guard's idle reading for the MAIN
// agent comes from the main session's OWN transcript, like the stuck-watcher's kill gate and the
// schedule-runner's sawTurn. A foreign write in the shared marveen-root project dir used to make BELA
// look active (the safe direction: the idle flush only came later), now the three readers agree.
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { guardTranscriptMtime, type GuardTranscriptDeps } from '../web/context-guard-runner.js'
import { MAIN_AGENT_ID, PROJECT_ROOT } from '../config.js'
import { MAIN_CHANNELS_SESSION } from '../web/main-agent.js'

const SID = '10c0fe6a-2ea2-4dec-85ef-a4462cc76b85'
function deps(over: Partial<GuardTranscriptDeps> = {}) {
  return {
    panePid: vi.fn((_s: string) => 4242 as number | null),
    own: vi.fn((_p: number, _r: string) => ({ ok: true as const, sessionId: SID, path: `/x/${SID}.jsonl`, mtimeMs: 1_000 }) as any),
    dir: vi.fn((_w: string, _c?: string) => 9_000 as number | null),
    ...over,
  }
}

describe('guardTranscriptMtime', () => {
  it('main agent: its own file (a newer foreign write in the dir does not count as activity)', () => {
    const d = deps()
    expect(guardTranscriptMtime(MAIN_AGENT_ID, d)).toBe(1_000)
    expect(d.panePid).toHaveBeenCalledWith(MAIN_CHANNELS_SESSION)
    expect(d.own).toHaveBeenCalledWith(4242, PROJECT_ROOT)
    expect(d.dir).not.toHaveBeenCalled()
  })
  it('main agent, not provable -> the old directory read', () => {
    const d = deps({ own: vi.fn(() => ({ ok: false, reason: 'record-cwd-mismatch' }) as any) })
    expect(guardTranscriptMtime(MAIN_AGENT_ID, d)).toBe(9_000)
    expect((d.dir as unknown as { mock: { calls: unknown[][] } }).mock.calls[0]![0]).toBe(PROJECT_ROOT)
  })
  it('main agent, no pane pid -> the old directory read, no record lookup', () => {
    const d = deps({ panePid: vi.fn(() => null) })
    expect(guardTranscriptMtime(MAIN_AGENT_ID, d)).toBe(9_000)
    expect(d.own).not.toHaveBeenCalled()
  })
  it('a sub-agent keeps the directory read, tmux untouched', () => {
    const d = deps()
    expect(guardTranscriptMtime('progi-own-transcript-test', d)).toBe(9_000)
    expect(d.panePid).not.toHaveBeenCalled()
    expect(d.own).not.toHaveBeenCalled()
  })
})

describe('wiring: measureIdleMs reads through guardTranscriptMtime', () => {
  it('the live body calls guardTranscriptMtime(name), not the raw directory read', () => {
    const src = readFileSync(join(__dirname, '..', 'web', 'context-guard-runner.ts'), 'utf-8')
    const at = src.indexOf('function measureIdleMs(')
    expect(at).toBeGreaterThan(0)
    const body = src.slice(at, src.indexOf('\n}\n', at))
    expect(body).toContain('const mtime = guardTranscriptMtime(name)')
    expect(body).not.toContain('readTranscriptMtimeFromProjectDir(')
  })
})

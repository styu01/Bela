// STUCKOWNSID1127 follow-up (PROGI msg 6169 item 3a): a scheduled prompt injected into the MAIN
// session counts as "started a turn" only on the main session's OWN transcript. The project dir of
// the marveen root is shared, so a foreign write after injectedAt (the owner's CLI session, an sdk-cli
// run) used to set sawTurn on a prompt that never ran -- a lost task counted as delivered.
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { inflightTranscriptMtime, type InflightTranscriptDeps } from '../web/schedule-runner.js'
import { mainSessionTranscriptMtime } from '../web/main-session-transcript.js'
import { MAIN_CHANNELS_SESSION } from '../web/main-agent.js'
import { PROJECT_ROOT } from '../config.js'

const OWN_MTIME = 1_000   // the main session's own file: written BEFORE the injection
const DIR_MTIME = 9_000   // the dir's newest file: a foreign session, written AFTER the injection
const SID = '10c0fe6a-2ea2-4dec-85ef-a4462cc76b85'

function deps(over: Partial<InflightTranscriptDeps> = {}) {
  const d = {
    panePid: vi.fn((_s: string) => 4242 as number | null),
    own: vi.fn((_pid: number, _root: string) => ({ ok: true as const, sessionId: SID, path: `/x/${SID}.jsonl`, mtimeMs: OWN_MTIME }) as any),
    dir: vi.fn((_w: string, _c?: string) => DIR_MTIME as number | null),
    ...over,
  }
  return d
}
const MAIN = { session: MAIN_CHANNELS_SESSION, host: null, workingDir: PROJECT_ROOT, configDir: undefined }

describe('inflightTranscriptMtime', () => {
  it('main agent: its OWN file decides, a newer foreign write in the dir is ignored (no false sawTurn)', () => {
    const d = deps()
    expect(inflightTranscriptMtime(MAIN, d)).toBe(OWN_MTIME)
    expect(d.panePid).toHaveBeenCalledWith(MAIN_CHANNELS_SESSION)
    expect(d.own).toHaveBeenCalledWith(4242, PROJECT_ROOT)
    expect(d.dir).not.toHaveBeenCalled()
  })

  it('main agent, own file not provable -> the old directory read, as before', () => {
    const d = deps({ own: vi.fn(() => ({ ok: false, reason: 'record-procstart-mismatch' }) as any) })
    expect(inflightTranscriptMtime(MAIN, d)).toBe(DIR_MTIME)
    expect(d.dir).toHaveBeenCalledWith(PROJECT_ROOT, undefined)
  })

  it('main agent, no pane pid -> the old directory read, the own resolver is not consulted', () => {
    const d = deps({ panePid: vi.fn(() => null) })
    expect(inflightTranscriptMtime(MAIN, d)).toBe(DIR_MTIME)
    expect(d.own).not.toHaveBeenCalled()
  })

  it('a sub-agent (own project dir) keeps the directory read; tmux and the record are not touched', () => {
    const d = deps()
    const sub = { session: 'agent-progi', host: null, workingDir: '/home/u/marveen/agents/progi', configDir: '/home/u/marveen/agents/progi/.claude-config' }
    expect(inflightTranscriptMtime(sub, d)).toBe(DIR_MTIME)
    expect(d.dir).toHaveBeenCalledWith(sub.workingDir, sub.configDir)
    expect(d.panePid).not.toHaveBeenCalled()
  })

  it('a remote main session (host set) keeps the directory read', () => {
    const d = deps()
    expect(inflightTranscriptMtime({ ...MAIN, host: 'remote-box' }, d)).toBe(DIR_MTIME)
    expect(d.panePid).not.toHaveBeenCalled()
  })

  it('the main session name with another working dir keeps the directory read', () => {
    const d = deps()
    expect(inflightTranscriptMtime({ ...MAIN, workingDir: '/elsewhere' }, d)).toBe(DIR_MTIME)
    expect(d.own).not.toHaveBeenCalled()
  })
})

describe('mainSessionTranscriptMtime', () => {
  it('own / not provable / no pane pid', () => {
    const okOwn = () => ({ ok: true as const, sessionId: SID, path: 'p', mtimeMs: 5 })
    expect(mainSessionTranscriptMtime('s', '/r', { panePid: () => 1, own: okOwn, dir: () => 7 })).toEqual({ mtime: 5, source: 'own', fallbackReason: null })
    expect(mainSessionTranscriptMtime('s', '/r', { panePid: () => 1, own: () => ({ ok: false, reason: 'no-session-record' }), dir: () => 7 }))
      .toEqual({ mtime: 7, source: 'dir', fallbackReason: 'no-session-record' })
    expect(mainSessionTranscriptMtime('s', '/r', { panePid: () => null, own: okOwn, dir: () => 7 }))
      .toEqual({ mtime: 7, source: 'dir', fallbackReason: 'no-pane-pid' })
  })
})

describe('wiring: the in-flight sweep reads through inflightTranscriptMtime', () => {
  it('the sawTurn transcript branch calls inflightTranscriptMtime(entry), not the raw directory read', () => {
    const src = readFileSync(join(__dirname, '..', 'web', 'schedule-runner.ts'), 'utf-8')
    const at = src.indexOf('if (!entry.sawTurn) {')
    expect(at).toBeGreaterThan(0)
    const block = src.slice(at, src.indexOf('isTaskCompletionEvidence(state, entry.sawTurn)', at))
    expect(block).toContain('const mtime = inflightTranscriptMtime(entry)')
    expect(block).not.toContain('readTranscriptMtimeFromProjectDir(')
  })
})

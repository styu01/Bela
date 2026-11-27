// BELA msg 6197 item 6 (PROGI 6196 on 8a72094a): after the claude process died its record cannot be
// proven any more; the respawn must not fall back to a bare --continue (which takes the newest
// interactive session of the dir, a live foreign one included) while the last PROVEN own session is
// known and its transcript exists.
import { beforeEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { provenMainSession, rememberMainSession, rememberedMainSession, REMEMBER_MAX_AGE_MS, _forgetRememberedMainSessions } from '../web/main-session-transcript.js'

const SID = '10c0fe6a-2ea2-4dec-85ef-a4462cc76b85'
const PATH = '/x/10c0fe6a-2ea2-4dec-85ef-a4462cc76b85.jsonl'
beforeEach(() => _forgetRememberedMainSessions())

describe('remembered main session', () => {
  it('a successful proof remembers; a later unprovable read finds it', () => {
    const r = provenMainSession('s', '/root', { panePid: () => 7, own: () => ({ ok: true, sessionId: SID, path: PATH, mtimeMs: 1 }) })
    expect(r.resume?.sessionId).toBe(SID)
    expect(provenMainSession('s', '/root', { panePid: () => null, own: () => ({ ok: false, reason: 'x' }) }).resume).toBeNull()  // proof itself stays honest
    expect(rememberedMainSession('/root', Date.now(), () => true)).toEqual({ sessionId: SID, transcriptPath: PATH })
  })
  it('only within REMEMBER_MAX_AGE_MS, only while the transcript exists, only for its own root', () => {
    rememberMainSession('/root', { sessionId: SID, transcriptPath: PATH }, 1_000)
    expect(rememberedMainSession('/root', 1_000 + REMEMBER_MAX_AGE_MS, () => true)).not.toBeNull()
    expect(rememberedMainSession('/root', 1_000 + REMEMBER_MAX_AGE_MS + 1, () => true)).toBeNull()
    expect(rememberedMainSession('/root', 2_000, () => false)).toBeNull()
    expect(rememberedMainSession('/other', 2_000, () => true)).toBeNull()
  })
  it('nothing remembered -> null (then --continue, as before)', () => {
    expect(rememberedMainSession('/root', Date.now(), () => true)).toBeNull()
  })
})

describe('wiring: resumeMarveenSession falls back to the remembered session, never over a proven one', () => {
  it('the chooser gets opts.resume ?? remembered, and remembered is only looked up when nothing was proven', () => {
    const src = readFileSync(join(__dirname, '..', 'web', 'channel-monitor.ts'), 'utf-8')
    expect(src).toContain('const remembered = opts.resume ? null : rememberedMainSession(PROJECT_ROOT)')
    expect(src).toContain('chooseMainRespawnResume(opts.resume ?? remembered, existsSync, opts.unprovenReason)')
  })
  it('readOwnSessionTranscript (the kill gate path) also remembers on success', () => {
    const src = readFileSync(join(__dirname, '..', 'web', 'main-session-transcript.ts'), 'utf-8')
    expect(src).toContain('if (r.ok) rememberMainSession(projectRoot, { sessionId: r.sessionId, transcriptPath: r.path })')
  })
})

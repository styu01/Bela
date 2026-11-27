// STUCKOWNSID1127 follow-up (PROGI review msg 6169): the stuck-watcher's recovery respawn resumes the
// main session's PROVEN own session id with `--resume <sid>` instead of `--continue`.
//
// Why: `--continue` opens the project dir's NEWEST interactive session. Measured on CC 2.1.326
// (isolated scratch CLAUDE_CONFIG_DIR, fake channel token, 2026-11-27): with an older session A and a
// newer, still RUNNING interactive session B, `--continue --channels ...` came up on B -- two
// processes on B's transcript. `--resume A --channels ...` came up on A with the plugin loaded.
// A sid with no transcript makes claude exit 1 ("No conversation found with session ID"), so the
// respawn re-checks the file right before it and falls back to --continue.
import { describe, it, expect } from 'vitest'
import { execFileSync } from 'node:child_process'
import { buildMainSessionRespawnCmd, chooseMainRespawnResume } from '../web/channel-monitor.js'

const SID = '01619efd-d396-4b74-9ba1-e4f387274385'
const PATH_OK = '/x/projects/-home-u-marveen/01619efd-d396-4b74-9ba1-e4f387274385.jsonl'
const BASE = { claudePath: 'claude', pluginId: 'telegram@claude-plugins-official', model: 'claude-opus-5-5' }

describe('chooseMainRespawnResume', () => {
  it('a proven session whose transcript exists -> --resume <sid>', () => {
    expect(chooseMainRespawnResume({ sessionId: SID, transcriptPath: PATH_OK }, (p) => p === PATH_OK))
      .toEqual({ resumeSessionId: SID, respawnMode: 'resume', continueReason: null })
  })
  it('nothing proven -> --continue, reason logged', () => {
    expect(chooseMainRespawnResume(null, () => true))
      .toEqual({ resumeSessionId: undefined, respawnMode: 'continue', continueReason: 'no-proven-session' })
  })
  it('the transcript is gone at respawn time -> --continue (a --resume would exit 1: a dead pane)', () => {
    expect(chooseMainRespawnResume({ sessionId: SID, transcriptPath: PATH_OK }, () => false))
      .toEqual({ resumeSessionId: undefined, respawnMode: 'continue', continueReason: 'transcript-missing-at-respawn' })
  })
  it.each(['', 'not-a-uuid', `${SID} --x`, `${SID}; touch PWNED`, '../../etc/passwd'])('a non-UUID id %j -> --continue', (bad) => {
    expect(chooseMainRespawnResume({ sessionId: bad, transcriptPath: PATH_OK }, () => true))
      .toMatchObject({ resumeSessionId: undefined, respawnMode: 'continue', continueReason: 'bad-session-id' })
  })
})

describe('buildMainSessionRespawnCmd with resumeSessionId', () => {
  it('continueSession + a UUID -> --resume <sid>, and no --continue', () => {
    const cmd = buildMainSessionRespawnCmd({ ...BASE, continueSession: true, resumeSessionId: SID })
    expect(cmd).toContain(`claude --resume ${SID} --dangerously-skip-permissions`)
    expect(cmd).not.toContain('--continue')
    expect(cmd).toContain('--channels plugin:telegram@claude-plugins-official')
  })
  it('control: continueSession without an id -> --continue, as before', () => {
    const cmd = buildMainSessionRespawnCmd({ ...BASE, continueSession: true })
    expect(cmd).toContain('claude --continue --dangerously-skip-permissions')
    expect(cmd).not.toContain('--resume')
  })
  it('a fresh start ignores the id (no --resume, no --continue)', () => {
    const cmd = buildMainSessionRespawnCmd({ ...BASE, continueSession: false, resumeSessionId: SID })
    expect(cmd).not.toContain('--resume')
    expect(cmd).not.toContain('--continue')
  })
  it('a hostile id never reaches the command: it falls back to --continue', () => {
    const hostile = `${SID}; touch PWNED`
    const cmd = buildMainSessionRespawnCmd({ ...BASE, continueSession: true, resumeSessionId: hostile })
    expect(cmd).not.toContain('PWNED')
    expect(cmd).toContain('claude --continue ')
  })
  it('a POSIX shell parses the built --resume token as exactly two inert words', () => {
    const cmd = buildMainSessionRespawnCmd({ ...BASE, continueSession: true, resumeSessionId: SID })
    // Replace the claude invocation with printf so the shell shows how it split the arguments.
    const probe = cmd.replace(/&& claude /, '&& printf "[%s]" ')
    const out = execFileSync('/bin/sh', ['-c', probe], { encoding: 'utf-8' })
    expect(out.startsWith(`[--resume][${SID}][--dangerously-skip-permissions]`)).toBe(true)
  })
})

describe('wiring: resumeMarveenSession decides the mode with the chooser and hands the id to the builder', () => {
  it('the live (non-comment) body calls the chooser with existsSync and passes resumeSessionId on', async () => {
    const { readFileSync } = await import('node:fs')
    const { join } = await import('node:path')
    const src = readFileSync(join(__dirname, '..', 'web', 'channel-monitor.ts'), 'utf-8')
    const start = src.indexOf('export async function resumeMarveenSession(')
    expect(start).toBeGreaterThan(0)
    const body = src.slice(start, src.indexOf('\n}\n', start))
    const code = body.split('\n').filter((l) => !l.trim().startsWith('//')).join('\n')
    const chooseIdx = code.indexOf('chooseMainRespawnResume(opts.resume ?? null, existsSync)')
    const buildIdx = code.indexOf('buildMainSessionRespawnCmd({')
    const passIdx = code.indexOf('resumeSessionId,', buildIdx)
    const respawnIdx = code.indexOf("'respawn-pane'")
    expect(chooseIdx).toBeGreaterThan(0)
    expect(buildIdx).toBeGreaterThan(chooseIdx)
    expect(passIdx).toBeGreaterThan(buildIdx)
    expect(respawnIdx).toBeGreaterThan(passIdx)
  })
})

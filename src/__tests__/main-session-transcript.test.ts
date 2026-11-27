// STUCKOWNSID1127: which transcript is the main session's own. The record shapes below are the
// measured CC 2.1.326 sessions/<pid>.json fields (2026-11-27, bela-channels).
import { describe, expect, it } from 'vitest'
import { join } from 'node:path'
import {
  resolveOwnSessionTranscript,
  parseProcStatStartTime,
  parseEnvironConfigDir,
  type OwnTranscriptDeps,
} from '../web/main-session-transcript.js'

const ROOT = '/home/u/marveen'
const SID = '10c0fe6a-2ea2-4dec-85ef-a4462cc76b85'
const PID = 1728274
const CFG = '/home/u/marveen/.channels-config'
const HOME = '/home/u'
const record = (over: Record<string, unknown> = {}) => JSON.stringify({
  pid: PID, sessionId: SID, cwd: ROOT, startedAt: 1795765679416, procStart: '54961429',
  version: '2.1.326', kind: 'interactive', entrypoint: 'cli', status: 'busy', ...over,
})
const ownPath = (cfg: string) => join(cfg, 'projects', '-home-u-marveen', `${SID}.jsonl`)

function deps(over: Partial<OwnTranscriptDeps> & { files?: Record<string, string>; mtimes?: Record<string, number> } = {}): OwnTranscriptDeps {
  const files = over.files ?? { [join(CFG, 'sessions', `${PID}.json`)]: record() }
  const mtimes = over.mtimes ?? { [ownPath(CFG)]: 5_000 }
  return {
    readFile: p => files[p] ?? null,
    mtimeMs: p => mtimes[p] ?? null,
    procAvailable: true,
    procStart: () => '54961429',
    procConfigDir: () => CFG,
    home: HOME,
    ...over,
  }
}

describe('resolveOwnSessionTranscript', () => {
  it('a valid record under the process CLAUDE_CONFIG_DIR -> that sid file and its mtime', () => {
    expect(resolveOwnSessionTranscript(PID, ROOT, deps())).toEqual({ ok: true, sessionId: SID, path: ownPath(CFG), mtimeMs: 5_000 })
  })

  it('environ unreadable -> the extra root (.channels-config) is tried, then ~/.claude', () => {
    const home = join(HOME, '.claude')
    expect(resolveOwnSessionTranscript(PID, ROOT, deps({ procConfigDir: () => null }), [CFG])).toMatchObject({ ok: true, path: ownPath(CFG) })
    const d = deps({ procConfigDir: () => null, files: { [join(home, 'sessions', `${PID}.json`)]: record() }, mtimes: { [ownPath(home)]: 7 } })
    expect(resolveOwnSessionTranscript(PID, ROOT, d, [CFG])).toEqual({ ok: true, sessionId: SID, path: ownPath(home), mtimeMs: 7 })
  })

  it('no record anywhere -> not provable', () => {
    expect(resolveOwnSessionTranscript(PID, ROOT, deps({ files: {} }))).toEqual({ ok: false, reason: 'no-session-record' })
  })

  it.each([
    ['record-unparsable', '{not json'],
    ['record-not-object', '[1,2]'],
    ['record-not-object', 'null'],
    ['record-pid-mismatch', record({ pid: PID + 1 })],
    ['record-pid-mismatch', record({ pid: String(PID) })],
    ['record-bad-session-id', record({ sessionId: '../../../etc/passwd' })],
    ['record-bad-session-id', record({ sessionId: `${SID}/x` })],
    ['record-bad-session-id', record({ sessionId: 42 })],
    ['record-cwd-mismatch', record({ cwd: '/home/u/marveen/agents/progi' })],
    ['record-cwd-mismatch', record({ cwd: '/home/u/marveen/' })],
    ['record-no-procstart', record({ procStart: undefined })],
    ['record-procstart-mismatch', record({ procStart: '111' })],
  ])('rejects (%s)', (reason, raw) => {
    const d = deps({ files: { [join(CFG, 'sessions', `${PID}.json`)]: raw } })
    expect(resolveOwnSessionTranscript(PID, ROOT, d)).toEqual({ ok: false, reason })
  })

  it('a stale record in the first root does not stop a valid one in a later root', () => {
    const home = join(HOME, '.claude')
    const d = deps({
      files: { [join(CFG, 'sessions', `${PID}.json`)]: record({ procStart: '1' }), [join(home, 'sessions', `${PID}.json`)]: record() },
      mtimes: { [ownPath(home)]: 9 },
    })
    expect(resolveOwnSessionTranscript(PID, ROOT, d)).toMatchObject({ ok: true, mtimeMs: 9 })
  })

  it('/proc present but the pid stat unreadable (process gone) -> not provable, no record trusted', () => {
    expect(resolveOwnSessionTranscript(PID, ROOT, deps({ procStart: () => null }))).toEqual({ ok: false, reason: 'proc-unreadable' })
  })

  it('no /proc (macOS): pid + cwd suffice, procStart is not compared', () => {
    const d = deps({ procAvailable: false, procStart: () => { throw new Error('must not be called') }, files: { [join(CFG, 'sessions', `${PID}.json`)]: record({ procStart: undefined }) } })
    expect(resolveOwnSessionTranscript(PID, ROOT, d)).toMatchObject({ ok: true })
  })

  it('valid record but the sid file does not exist (yet) -> not provable', () => {
    expect(resolveOwnSessionTranscript(PID, ROOT, deps({ mtimes: {} }))).toEqual({ ok: false, reason: 'transcript-missing' })
  })

  it('a numeric procStart in the record compares as text', () => {
    const d = deps({ files: { [join(CFG, 'sessions', `${PID}.json`)]: record({ procStart: 54961429 }) } })
    expect(resolveOwnSessionTranscript(PID, ROOT, d)).toMatchObject({ ok: true })
  })
})

describe('parseProcStatStartTime', () => {
  it('field 22 of a real /proc/<pid>/stat line', () => {
    const line = '1728274 (claude) S 801 1728274 1728274 34816 1728274 4194560 160000 0 0 0 900 120 0 0 20 0 18 0 54961429 75000000000 90000 18446744073709551615'
    expect(parseProcStatStartTime(line)).toBe('54961429')
  })
  it('comm with spaces and parens', () => {
    const line = '42 (we ird) (x)) S 1 42 42 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 777 1000 10'
    expect(parseProcStatStartTime(line)).toBe('777')
  })
  it('garbage -> null', () => {
    expect(parseProcStatStartTime('no parens here')).toBeNull()
    expect(parseProcStatStartTime('1 (a) S 2 3')).toBeNull()
  })
})

describe('parseEnvironConfigDir', () => {
  it('finds CLAUDE_CONFIG_DIR in a NUL-separated environ', () => {
    expect(parseEnvironConfigDir(`HOME=/home/u\0CLAUDE_CONFIG_DIR=${CFG}\0PATH=/bin\0`)).toBe(CFG)
  })
  it('unset or empty -> null; a lookalike key does not match', () => {
    expect(parseEnvironConfigDir('HOME=/home/u\0')).toBeNull()
    expect(parseEnvironConfigDir('CLAUDE_CONFIG_DIR=\0')).toBeNull()
    expect(parseEnvironConfigDir('XCLAUDE_CONFIG_DIR=/x\0')).toBeNull()
  })
})

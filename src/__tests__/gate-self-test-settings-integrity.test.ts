import { describe, it, expect, beforeEach, afterAll } from 'vitest'
import { mkdtempSync, writeFileSync, rmSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  findSettingsHazards, settingsIntegrityFiles, runGateSelfTest, resetGateSelfTestState, settingsHazardText,
} from '../web/gate-self-test.js'
import { PROJECT_ROOT, MAIN_AGENT_ID } from '../config.js'

// BELA 6355 / PROGI 6354: a settings file's "env" block reaches every hook process (measured with Claude Code
// 2.1.326: NODE_OPTIONS, PYTHONPATH and PATH arrive in the hook shell), and disableAllHooks /
// allowManagedHooksOnly switch the hooks off. The gate self-test tells any of them, per file, through the
// escalation path, and says HELYREALLT once the file is clean or gone.

const tmp = mkdtempSync(join(tmpdir(), 'gate-settings-integrity-'))
afterAll(() => rmSync(tmp, { recursive: true, force: true }))
const j = (o: unknown) => JSON.stringify(o)

describe('findSettingsHazards', () => {
  it('env keys that redirect the hook\'s programs or load code into them', () => {
    expect(findSettingsHazards(j({ env: {
      PATH: '/x', BASH_ENV: '/x', ENV: '/x', LD_PRELOAD: '/x.so', LD_LIBRARY_PATH: '/x', DYLD_INSERT_LIBRARIES: '/x',
      NODE_OPTIONS: '--import=/x', NODE_PATH: '/x', PYTHONPATH: '/x', PYTHONHOME: '/x', PYTHONSTARTUP: '/x',
    } }))).toEqual(['env.BASH_ENV', 'env.DYLD_INSERT_LIBRARIES', 'env.ENV', 'env.LD_LIBRARY_PATH', 'env.LD_PRELOAD',
      'env.NODE_OPTIONS', 'env.NODE_PATH', 'env.PATH', 'env.PYTHONHOME', 'env.PYTHONPATH', 'env.PYTHONSTARTUP'])
  })
  it('PROGI 6376 (C1): the hook runner keys (measured: CLAUDE_CODE_SHELL_PREFIX wraps every hook command)', () => {
    expect(findSettingsHazards(j({ env: {
      CLAUDE_CODE_SHELL_PREFIX: '/x/wrap.sh', CLAUDE_CODE_SHELL: '/x/sh', SHELL: '/x/sh', SHELLOPTS: 'xtrace', BASHOPTS: 'extdebug', PS4: '$(x)',
      'BASH_FUNC_timeout%%': '() { :; }',
    } }))).toEqual(['env.BASHOPTS', 'env.BASH_FUNC_timeout%%', 'env.CLAUDE_CODE_SHELL', 'env.CLAUDE_CODE_SHELL_PREFIX', 'env.PS4', 'env.SHELL', 'env.SHELLOPTS'])
    expect(findSettingsHazards(j({ env: { CLAUDE_CODE_MAX_OUTPUT_TOKENS: '1', CLAUDE_CODE_SHELL_X: '1', BASH_FUNCX: '1' } }))).toEqual([])
  })
  it('ordinary env keys are not hazards (case-sensitive like the environment itself)', () => {
    expect(findSettingsHazards(j({ env: { TZ: 'Europe/Budapest', ANTHROPIC_MODEL: 'x', DISABLE_AUTOUPDATER: '1', path: '/x', NODEJS: '1', MYPATH: '/x', ENVIRONMENT: 'prod' } }))).toEqual([])
  })
  it('the hook switches: set (any value but false) is a hazard, false is not', () => {
    expect(findSettingsHazards(j({ disableAllHooks: true }))).toEqual(['disableAllHooks'])
    expect(findSettingsHazards(j({ allowManagedHooksOnly: true }))).toEqual(['allowManagedHooksOnly'])
    expect(findSettingsHazards(j({ disableAllHooks: 'yes', allowManagedHooksOnly: 1 }))).toEqual(['disableAllHooks', 'allowManagedHooksOnly'])
    expect(findSettingsHazards(j({ disableAllHooks: false, allowManagedHooksOnly: false }))).toEqual([])
  })
  it('a BOM is dropped; unparseable text, an array root, a non-object env: no hazard', () => {
    expect(findSettingsHazards('﻿' + j({ env: { PATH: '/x' } }))).toEqual(['env.PATH'])
    expect(findSettingsHazards('{ not json')).toEqual([])
    expect(findSettingsHazards(j([{ env: { PATH: '/x' } }]))).toEqual([])
    expect(findSettingsHazards(j({ env: ['PATH'] }))).toEqual([])
    expect(findSettingsHazards(j({ hooks: { PreToolUse: [] }, permissions: { allow: [] } }))).toEqual([])
  })
})

describe('settingsIntegrityFiles', () => {
  it('the main agent\'s user, isolated-copy, project and project-local settings; three per sub-agent', () => {
    const files = settingsIntegrityFiles()
    const main = files.filter((f) => f.agent === MAIN_AGENT_ID).map((f) => f.path)
    expect(main).toEqual(expect.arrayContaining([
      join(PROJECT_ROOT, '.channels-config', 'settings.json'),
      join(PROJECT_ROOT, '.claude', 'settings.json'),
      join(PROJECT_ROOT, '.claude', 'settings.local.json'),
    ]))
    expect(main.some((p) => p.endsWith(join('.claude', 'settings.json')) && !p.startsWith(PROJECT_ROOT))).toBe(true)
    const subs = new Set(files.filter((f) => f.agent !== MAIN_AGENT_ID).map((f) => f.agent))
    for (const a of subs) {
      const ps = files.filter((f) => f.agent === a).map((f) => f.path)
      expect(ps.length).toBe(3)
      expect(ps.some((p) => p.endsWith(join('.claude', 'settings.local.json')))).toBe(true)
      expect(ps.some((p) => p.endsWith(join('.claude-config', 'settings.json')))).toBe(true)
    }
  })
})

describe('runGateSelfTest: settings integrity escalation', () => {
  let dir: string
  let sent: string[]
  const run = (files: { agent: string; path: string }[], read?: (p: string) => string | null, exists?: (p: string) => boolean) =>
    runGateSelfTest({ targets: [], settingsFiles: files, send: (t) => { sent.push(t) }, retryDelayMs: 0, git: null, read, exists })
  beforeEach(() => {
    resetGateSelfTestState()
    sent = []
    dir = mkdtempSync(join(tmp, 'case-'))
  })

  it('a hazard is told once with the agent, the file and the effect; again only when it changes; HELYREALLT when clean', async () => {
    const p = join(dir, 'settings.local.json')
    const f = [{ agent: 'progi', path: p }]
    writeFileSync(p, j({ env: { NODE_OPTIONS: '--import=/tmp/x.mjs' } }))
    await run(f)
    expect(sent).toHaveLength(1)
    expect(sent[0]).toBe(settingsHazardText(f[0], ['env.NODE_OPTIONS']))
    expect(sent[0]).toContain('agens progi')
    expect(sent[0]).toContain(p)
    await run(f)
    expect(sent).toHaveLength(1) // unchanged: nothing new
    writeFileSync(p, j({ env: { NODE_OPTIONS: '--import=/tmp/x.mjs', PATH: '/evil/bin' }, disableAllHooks: true }))
    await run(f)
    expect(sent).toHaveLength(2)
    expect(sent[1]).toContain('env.NODE_OPTIONS, env.PATH')
    expect(sent[1]).toContain('disableAllHooks kapcsolo a hookokat kikapcsolja')
    writeFileSync(p, j({ env: { TZ: 'UTC' } }))
    await run(f)
    expect(sent).toHaveLength(3)
    expect(sent[2]).toContain('HELYREALLT')
    expect(sent[2]).toContain(p)
    await run(f)
    expect(sent).toHaveLength(3)
  })

  it('a file that exists but cannot be read this pass keeps its escalation (no false all-clear); a missing file is clean', async () => {
    const p = join(dir, 'settings.json')
    const f = [{ agent: 'iris', path: p }]
    writeFileSync(p, j({ allowManagedHooksOnly: true }))
    await run(f)
    expect(sent).toHaveLength(1)
    await run(f, () => null, () => true) // unreadable, still there
    expect(sent).toHaveLength(1)
    rmSync(p)
    await run(f)
    expect(sent).toHaveLength(2)
    expect(sent[1]).toContain('HELYREALLT')
  })

  it('a half-written (unparseable) file is not a false all-clear either way: it is read as no hazard only when parseable', async () => {
    const p = join(dir, 'settings.json')
    const f = [{ agent: 'okoska', path: p }]
    writeFileSync(p, '{ "env": { "PATH"') // caught mid-write: no hazard seen, nothing raised
    await run(f)
    expect(sent).toEqual([])
  })

  it('a file that drops out of the list (its agent removed) closes its escalation', async () => {
    const p = join(dir, 'settings.json')
    writeFileSync(p, j({ env: { LD_PRELOAD: '/x.so' } }))
    await run([{ agent: 'vizsla', path: p }])
    expect(sent).toHaveLength(1)
    await run([])
    expect(sent).toHaveLength(2)
    expect(sent[1]).toContain('HELYREALLT')
  })

  it('one file listed twice is one escalation; two files are two', async () => {
    const a = join(dir, 'a.json'); const b = join(dir, 'b.json')
    writeFileSync(a, j({ env: { PATH: '/x' } })); writeFileSync(b, j({ env: { BASH_ENV: '/x' } }))
    await run([{ agent: 'bela', path: a }, { agent: 'bela', path: a }, { agent: 'x', path: b }])
    expect(sent).toHaveLength(2)
  })

  it('a test that injects only targets reads no host settings file', async () => {
    mkdirSync(join(dir, 'x'))
    const reads: string[] = []
    await runGateSelfTest({ targets: [], send: (t) => { sent.push(t) }, retryDelayMs: 0, git: null, read: (p) => { reads.push(p); return null } })
    expect(reads).toEqual([])
    expect(sent).toEqual([])
  })
})

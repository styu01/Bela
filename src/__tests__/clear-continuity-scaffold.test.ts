// PROGI review msg 6196 on b5873dd8 (BELA 6197): the /clear continuity hooks reach the SUB-agents only.
//  - HIBA 1: the main agent's settings file is the GLOBAL ~/.claude/settings.json (it also fires for
//    the owner's own sessions and the sub-agents' .claude-config copies are made from it) -- it must
//    get neither clear-capture / clear-replay nor the compact|resume|clear matcher;
//  - a matcher a human narrowed is not widened back; an unparsable settings.json is not replaced by a
//    hooks-only file; a symlinked settings.json is written through, not replaced.
// Sandbox: homedir() and agentDir() point into a temp tree; the real ~/.claude is never touched.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync, symlinkSync, lstatSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

let SANDBOX = ''
vi.mock('node:os', async (orig) => {
  const actual = await orig<typeof import('node:os')>()
  return { ...actual, homedir: () => join(SANDBOX, 'home') }
})
vi.mock('../web/agent-config.js', async (orig) => {
  const actual = await orig<typeof import('../web/agent-config.js')>()
  return { ...actual, agentDir: (name: string) => join(SANDBOX, 'agents', name) }
})

const { ensureAgentHooks } = await import('../web/agent-scaffold.js')
const { MAIN_AGENT_ID } = await import('../config.js')

const SUB = 'clearsub'
const mainPath = () => join(SANDBOX, 'home', '.claude', 'settings.json')
const subPath = () => join(SANDBOX, 'agents', SUB, '.claude', 'settings.json')
const read = (p: string) => JSON.parse(readFileSync(p, 'utf-8'))
const allCommands = (s: any): string[] => Object.values(s.hooks ?? {}).flatMap((es: any) => es.flatMap((e: any) => (e.hooks ?? []).map((h: any) => h.command))).filter((c: unknown): c is string => typeof c === 'string')
const sessionStartMatchers = (s: any): string[] => (s.hooks?.SessionStart ?? []).map((e: any) => e.matcher)
const oldShape = (root: string) => ({
  someKey: 'kept',
  hooks: { SessionStart: [{ matcher: 'compact|resume', hooks: [{ type: 'command', command: `python3 ${root}/scripts/hooks/taskstate-replay.py`, timeout: 15 }] }] },
})

beforeEach(() => {
  SANDBOX = mkdtempSync(join(tmpdir(), 'clearscaf-'))
  mkdirSync(join(SANDBOX, 'home', '.claude'), { recursive: true })
  mkdirSync(join(SANDBOX, 'agents', SUB, '.claude'), { recursive: true })
})
afterEach(() => { rmSync(SANDBOX, { recursive: true, force: true }) })

const { PROJECT_ROOT } = await import('../config.js')

describe('the main agent (global settings) gets nothing of the /clear part', () => {
  it('an existing global settings.json: no clear-capture / clear-replay, the matcher stays compact|resume', () => {
    writeFileSync(mainPath(), JSON.stringify(oldShape(PROJECT_ROOT)))
    ensureAgentHooks(MAIN_AGENT_ID)
    const s = read(mainPath())
    expect(allCommands(s).some((c) => /clear-(capture|replay)\.py/.test(c))).toBe(false)
    expect(sessionStartMatchers(s)).not.toContain('compact|resume|clear')
    expect(s.hooks.SessionEnd).toBeUndefined()
  })
  it('a hook-less global settings.json seeded from the template: no clear hooks either', () => {
    writeFileSync(mainPath(), JSON.stringify({ someKey: 1 }))
    ensureAgentHooks(MAIN_AGENT_ID)
    const s = read(mainPath())
    expect(allCommands(s).some((c) => /clear-(capture|replay)\.py/.test(c))).toBe(false)
    expect(sessionStartMatchers(s)).toContain('compact|resume')
  })
  it('control: a sub-agent DOES get them', () => {
    writeFileSync(subPath(), JSON.stringify(oldShape(PROJECT_ROOT)))
    ensureAgentHooks(SUB)
    const s = read(subPath())
    expect(allCommands(s).some((c) => c.includes('clear-capture.py'))).toBe(true)
    expect(allCommands(s).some((c) => c.includes('clear-replay.py'))).toBe(true)
    expect(sessionStartMatchers(s)).toContain('compact|resume|clear')
  })
})

describe('the small items', () => {
  it('a matcher a human narrowed (compact) is left as it is', () => {
    const shape = oldShape(PROJECT_ROOT)
    shape.hooks.SessionStart[0].matcher = 'compact'
    writeFileSync(subPath(), JSON.stringify(shape))
    ensureAgentHooks(SUB)
    const taskstate = read(subPath()).hooks.SessionStart.find((e: any) => e.hooks.some((h: any) => h.command.includes('taskstate-replay.py')))
    expect(taskstate.matcher).toBe('compact')
  })
  it('an unparsable settings.json is not overwritten', () => {
    writeFileSync(subPath(), '{ "broken": ')
    expect(ensureAgentHooks(SUB)).toBe(false)
    expect(readFileSync(subPath(), 'utf-8')).toBe('{ "broken": ')
  })
  it('a symlinked settings.json is written through to its target and stays a symlink', () => {
    const target = join(SANDBOX, 'real-settings.json')
    writeFileSync(target, JSON.stringify(oldShape(PROJECT_ROOT)))
    rmSync(subPath(), { force: true })
    symlinkSync(target, subPath())
    ensureAgentHooks(SUB)
    expect(lstatSync(subPath()).isSymbolicLink()).toBe(true)
    expect(allCommands(read(target)).some((c) => c.includes('clear-capture.py'))).toBe(true)
  })
})

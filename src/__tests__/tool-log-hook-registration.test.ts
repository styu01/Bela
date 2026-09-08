import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

// TOOLLOG906 (2026-09-08): the sub-agent fleet never logged a single tool
// call -- templates/settings.json.template carried no PostToolUse block at
// all, so ensureAgentHooks (which every agent's scaffold/reconcile path runs
// through) had nothing to seed for anyone. BÉLA appeared to log purely by
// accident: its Claude Code session's PROJECT root is the marveen repo
// itself, so a hand-maintained repo-root .claude/settings.json entry fired,
// completely independent of this scaffold mechanism.
//
// Same sandbox shape as isolated-config-mcp-reconcile.test.ts: homedir() and
// agentDir() are redirected into a throwaway temp tree, so nothing here can
// read or write the real ~/.claude of the machine running the suite.
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

const SUB_AGENT = 'testsubagent'

function subAgentSettingsPath(): string {
  return join(SANDBOX, 'agents', SUB_AGENT, '.claude', 'settings.json')
}
function mainAgentSettingsPath(): string {
  return join(SANDBOX, 'home', '.claude', 'settings.json')
}
function readSettings(path: string): Record<string, unknown> {
  return JSON.parse(readFileSync(path, 'utf-8')) as Record<string, unknown>
}
function postToolUseCommands(settings: Record<string, unknown>): string[] {
  const hooks = settings.hooks as Record<string, unknown> | undefined
  const entries = (hooks?.PostToolUse ?? []) as Array<{ hooks?: Array<{ command?: string }> }>
  return entries.flatMap((e) => (e.hooks ?? []).map((h) => h.command).filter((c): c is string => !!c))
}

beforeEach(() => {
  SANDBOX = mkdtempSync(join(tmpdir(), 'toollog-'))
  mkdirSync(join(SANDBOX, 'home', '.claude'), { recursive: true })
  mkdirSync(join(SANDBOX, 'agents', SUB_AGENT, '.claude'), { recursive: true })
})
afterEach(() => { rmSync(SANDBOX, { recursive: true, force: true }) })

describe('ensureAgentHooks seeds PostToolUse -> tool-log-capture.py for sub-agents', () => {
  it('a fresh sub-agent settings.json (no hooks yet) gets the tool-log-capture.py hook', () => {
    writeFileSync(subAgentSettingsPath(), JSON.stringify({ enabledPlugins: {} }))
    ensureAgentHooks(SUB_AGENT)
    const cmds = postToolUseCommands(readSettings(subAgentSettingsPath()))
    expect(cmds.some((c) => c.includes('tool-log-capture.py'))).toBe(true)
  })

  it('an existing sub-agent settings.json (other hooks present, no PostToolUse) gets the event added', () => {
    writeFileSync(subAgentSettingsPath(), JSON.stringify({
      enabledPlugins: {},
      hooks: { UserPromptSubmit: [{ hooks: [{ type: 'command', command: 'echo hi', timeout: 5 }] }] },
    }))
    ensureAgentHooks(SUB_AGENT)
    const settings = readSettings(subAgentSettingsPath())
    const cmds = postToolUseCommands(settings)
    expect(cmds.some((c) => c.includes('tool-log-capture.py'))).toBe(true)
    // the pre-existing, unrelated event survives untouched
    const hooks = settings.hooks as Record<string, unknown>
    expect(hooks.UserPromptSubmit).toBeDefined()
  })

  it('re-running is idempotent -- does not duplicate the hook entry', () => {
    writeFileSync(subAgentSettingsPath(), JSON.stringify({ enabledPlugins: {} }))
    ensureAgentHooks(SUB_AGENT)
    ensureAgentHooks(SUB_AGENT)
    ensureAgentHooks(SUB_AGENT)
    const cmds = postToolUseCommands(readSettings(subAgentSettingsPath()))
    expect(cmds.filter((c) => c.includes('tool-log-capture.py')).length).toBe(1)
  })
})

// TOOLLOG906: MAIN_AGENT_ID's Claude Code session loads its repo-root
// /.claude/settings.json (project scope) AND ~/.claude/settings.json (user
// scope, what ensureAgentHooks writes for MAIN_AGENT_ID) at the same time.
// The repo-root file already hand-carries its OWN tool-log-capture.py entry
// with a differently-worded command ($CLAUDE_PROJECT_DIR, not
// {{PROJECT_ROOT}}) -- exact-string dedup would never recognize the two as
// the same hook, so seeding the template's copy here too would fire the
// script TWICE per tool call for the main agent specifically.
describe('ensureAgentHooks does NOT seed tool-log-capture.py for MAIN_AGENT_ID (avoids double-logging)', () => {
  it('a fresh main-agent settings.json gets every other PostToolUse-adjacent hook event but not this one', () => {
    writeFileSync(mainAgentSettingsPath(), JSON.stringify({ enabledPlugins: {} }))
    ensureAgentHooks(MAIN_AGENT_ID)
    const settings = readSettings(mainAgentSettingsPath())
    const cmds = postToolUseCommands(settings)
    expect(cmds.some((c) => c.includes('tool-log-capture.py'))).toBe(false)
    // UserPromptSubmit (a sibling event the main agent SHOULD still get) proves
    // the exception is scoped to just this one hook, not the whole template.
    const hooks = settings.hooks as Record<string, unknown> | undefined
    expect(hooks?.UserPromptSubmit).toBeDefined()
  })

  it('an existing main-agent settings.json with its own hand-maintained PostToolUse entry is left alone', () => {
    writeFileSync(mainAgentSettingsPath(), JSON.stringify({
      enabledPlugins: {},
      hooks: {
        PostToolUse: [{ hooks: [{ type: 'command', command: 'python3 "$CLAUDE_PROJECT_DIR/scripts/hooks/tool-log-capture.py"', timeout: 5 }] }],
      },
    }))
    ensureAgentHooks(MAIN_AGENT_ID)
    const cmds = postToolUseCommands(readSettings(mainAgentSettingsPath()))
    // still exactly the ONE hand-maintained entry -- no second, template-sourced copy
    expect(cmds.filter((c) => c.includes('tool-log-capture.py')).length).toBe(1)
  })
})

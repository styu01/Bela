// agent-create.ts (BELA 6404, the idea of upstream 22477879 on this fork's own POST /api/agents): the refusals run
// before anything is written, the two new ones included (an invalid model id, the main agent's name), and the base
// writes are what POST /api/agents always wrote.
import { describe, it, expect, afterAll, vi } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync, existsSync, readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

const SANDBOX = mkdtempSync(join(tmpdir(), 'agent-create-'))
mkdirSync(join(SANDBOX, 'store'), { recursive: true })
mkdirSync(join(SANDBOX, 'agents'), { recursive: true })
vi.mock('../config.js', async (orig) => {
  const actual = await orig<typeof import('../config.js')>()
  return { ...actual, PROJECT_ROOT: SANDBOX, STORE_DIR: join(SANDBOX, 'store') }
})
vi.mock('../web/claude-cli-version.js', async (orig) => {
  const actual = await orig<typeof import('../web/claude-cli-version.js')>()
  return { ...actual, measureClaudeCliVersion: async () => ({ version: null }) }
})

const { MAIN_AGENT_ID } = await import('../config.js')
const { checkAgentCreatable, writeAgentBase } = await import('../web/agent-create.js')
const OPTS = { requireDescription: true, description: 'd' }
afterAll(() => rmSync(SANDBOX, { recursive: true, force: true }))

describe('checkAgentCreatable', () => {
  it('refuses an invalid model id before anything exists (400)', async () => {
    const r = await checkAgentCreatable('neu', "claude'; rm -rf /", null, OPTS)
    expect(r?.status).toBe(400)
    expect(existsSync(join(SANDBOX, 'agents', 'neu'))).toBe(false)
  })

  it("refuses the main agent's name (409), which has no agents/<name> directory", async () => {
    const r = await checkAgentCreatable(MAIN_AGENT_ID, 'claude-opus-5-5', null, OPTS)
    expect(r).toEqual({ status: 409, body: { error: 'Name is reserved for the main agent' } })
  })

  it('refuses a non-string customProvider and an unknown one (400)', async () => {
    expect((await checkAgentCreatable('neu', 'claude-opus-5-5', null, { ...OPTS, rawCustomProvider: { id: 'x' } }))?.status).toBe(400)
    expect((await checkAgentCreatable('neu', 'claude-opus-5-5', 'nope', OPTS))?.body).toEqual({ error: 'Unknown custom provider: nope' })
  })

  it('keeps the old order: name, then description, then exists', async () => {
    expect((await checkAgentCreatable('', 'bad model', null, OPTS))?.body).toEqual({ error: 'Name is required' })
    expect((await checkAgentCreatable('neu', 'claude-opus-5-5', null, { requireDescription: true }))?.body).toEqual({ error: 'Description is required' })
    mkdirSync(join(SANDBOX, 'agents', 'old'))
    expect((await checkAgentCreatable('old', 'claude-opus-5-5', null, OPTS))?.status).toBe(409)
    expect(await checkAgentCreatable('neu', 'claude-opus-5-5', null, OPTS)).toBeNull()
    // a package brings its own texts: no description needed
    expect(await checkAgentCreatable('neu', 'claude-opus-5-5', null, { requireDescription: false })).toBeNull()
  })
})

describe('writeAgentBase', () => {
  it('writes the scaffold, the model, the profile and the display name when it differs', () => {
    writeAgentBase('base1', { model: 'claude-opus-5-5', profileId: 'default', customProvider: null, displayName: 'Base One' })
    const dir = join(SANDBOX, 'agents', 'base1')
    const cfg = JSON.parse(readFileSync(join(dir, 'agent-config.json'), 'utf-8'))
    expect(cfg.model).toBe('claude-opus-5-5')
    expect(cfg.customProvider).toBeUndefined()
    expect(JSON.stringify(cfg)).toContain('Base One')
    expect(readdirSync(dir).length).toBeGreaterThan(1)
  })
})

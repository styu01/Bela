import { describe, it, expect, beforeEach, afterAll, vi } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { EventEmitter } from 'node:events'
import type { RouteContext } from '../web/routes/types.js'

// Fork-side hardening on top of the upstream 51b18820 port (BELA 6404):
//   1. A stored provider definition is re-validated on READ, so a hand-edited or
//      otherwise tampered store/custom-providers.json can never reach a launch
//      command (it reads back as missing, and every caller refuses a missing one).
//   2. The launch env single-quotes the baseUrl (upstream used double quotes, in
//      which $(...) and backticks still expand).
//   3. PUT /api/agents/:name validates customProvider BEFORE the first write.
//   4. The background worker on a custom endpoint does not carry the fleet OAuth
//      token (the CLI prefers it and would send it to the third-party base URL).
const SANDBOX = mkdtempSync(join(tmpdir(), 'custprov-harden-'))
mkdirSync(join(SANDBOX, 'store'), { recursive: true })
mkdirSync(join(SANDBOX, 'agents', 'sub1'), { recursive: true })

vi.mock('../config.js', async (orig) => {
  const actual = await orig<typeof import('../config.js')>()
  return { ...actual, PROJECT_ROOT: SANDBOX, STORE_DIR: join(SANDBOX, 'store') }
})

const PROVIDERS = join(SANDBOX, 'store', 'custom-providers.json')
const SUB_DIR = join(SANDBOX, 'agents', 'sub1')
const SUB_CFG = join(SUB_DIR, 'agent-config.json')

const { loadCustomProvider } = await import('../web/custom-providers.js')
const { resolveProviderEnv, buildCustomProviderLaunchEnv } = await import('../web/agent-process.js')
const { tryHandleAgents } = await import('../web/routes/agents.js')

const VALID = { id: 'p1', label: 'P1', baseUrl: 'https://llm.example/anthropic', authHeader: 'none', vaultKey: null }

function seedProviders(providers: unknown[]): void {
  writeFileSync(PROVIDERS, JSON.stringify({ providers }, null, 2))
}

async function putAgent(name: string, body: unknown): Promise<{ status: number; body: any }> {
  const req = new EventEmitter() as unknown as RouteContext['req']
  ;(req as unknown as { headers: Record<string, string> }).headers = {}
  const out: { status: number; body: any } = { status: 0, body: null }
  const res = {
    writeHead(status: number) { out.status = status; return res },
    end(chunk?: string) { if (chunk) out.body = JSON.parse(chunk) },
    setHeader() {},
  }
  const url = new URL(`http://localhost:3420/api/agents/${encodeURIComponent(name)}`)
  process.nextTick(() => {
    ;(req as unknown as EventEmitter).emit('data', Buffer.from(JSON.stringify(body)))
    ;(req as unknown as EventEmitter).emit('end')
  })
  const handled = await tryHandleAgents(
    // The owner's browser login: customProvider is an owner-only field since the APIAGENTWRITE1104 port.
    { req, res, path: url.pathname, method: 'PUT', url, auth: { kind: 'session', user: 'owner' } } as unknown as RouteContext,
    join(SANDBOX, 'web'),
  )
  expect(handled).toBe(true)
  return { status: out.status || 200, body: out.body }
}

beforeEach(() => {
  rmSync(PROVIDERS, { force: true })
  rmSync(SUB_CFG, { force: true })
  rmSync(join(SUB_DIR, 'CLAUDE.md'), { force: true })
})
afterAll(() => rmSync(SANDBOX, { recursive: true, force: true }))

describe('loadCustomProvider re-validates the stored definition', () => {
  it('returns a valid stored definition unchanged', () => {
    seedProviders([VALID])
    expect(loadCustomProvider('p1')).toEqual(VALID)
  })

  for (const [what, baseUrl] of [
    ['command substitution', 'https://llm.example/$(touch /tmp/pwned)'],
    ['backtick', 'https://llm.example/`id`'],
    ['double quote breakout', 'https://llm.example/"; id; "'],
    ['plain http to a remote host', 'http://llm.example/anthropic'],
  ] as const) {
    it(`a hand-edited baseUrl with ${what} reads back as missing`, () => {
      seedProviders([{ ...VALID, baseUrl }])
      expect(loadCustomProvider('p1')).toBeNull()
    })
  }

  it('a stored definition with an invalid authHeader reads back as missing', () => {
    seedProviders([{ ...VALID, authHeader: 'Basic' }])
    expect(loadCustomProvider('p1')).toBeNull()
  })

  it('a non-object entry in the store does not throw and does not hide the valid one', () => {
    seedProviders([null, 'junk', VALID])
    expect(loadCustomProvider('p1')).toEqual(VALID)
  })
})

describe('the custom-provider launch env single-quotes the baseUrl', () => {
  it('resolveProviderEnv (sub-agent launch path)', () => {
    const { exportsStr } = resolveProviderEnv('some-model', () => null, VALID as never)
    expect(exportsStr).toContain(`export ANTHROPIC_BASE_URL='https://llm.example/anthropic' && `)
    expect(exportsStr).not.toContain('ANTHROPIC_BASE_URL="')
  })

  it('buildCustomProviderLaunchEnv (worker path)', () => {
    seedProviders([VALID])
    writeFileSync(SUB_CFG, JSON.stringify({ model: 'some-model', customProvider: 'p1' }))
    const env = buildCustomProviderLaunchEnv('sub1')
    expect(env).not.toBeNull()
    expect(env!.envPrefix.startsWith(`export ANTHROPIC_BASE_URL='https://llm.example/anthropic' && `)).toBe(true)
    expect(env!.model).toBe('some-model')
  })

  it('buildCustomProviderLaunchEnv refuses a tampered stored definition (throws, no env built)', () => {
    seedProviders([{ ...VALID, baseUrl: 'https://llm.example/$(id)' }])
    writeFileSync(SUB_CFG, JSON.stringify({ model: 'some-model', customProvider: 'p1' }))
    expect(() => buildCustomProviderLaunchEnv('sub1')).toThrow(/stored definition is invalid/)
  })
})

describe('PUT /api/agents/:name validates customProvider before the first write', () => {
  it('a non-string customProvider is refused with 400 and nothing is written', async () => {
    seedProviders([VALID])
    const r = await putAgent('sub1', { claudeMd: 'SHOULD NOT LAND', customProvider: { id: 'p1' } })
    expect(r.status).toBe(400)
    expect(existsSync(join(SUB_DIR, 'CLAUDE.md'))).toBe(false)
    expect(existsSync(SUB_CFG)).toBe(false)
  })

  it('an unknown provider id is refused with 400 and nothing is written', async () => {
    seedProviders([VALID])
    const r = await putAgent('sub1', { claudeMd: 'SHOULD NOT LAND', customProvider: 'nope' })
    expect(r.status).toBe(400)
    expect(existsSync(join(SUB_DIR, 'CLAUDE.md'))).toBe(false)
  })

  it('an id whose stored definition no longer validates is refused too', async () => {
    seedProviders([{ ...VALID, baseUrl: 'https://llm.example/$(id)' }])
    const r = await putAgent('sub1', { customProvider: 'p1' })
    expect(r.status).toBe(400)
    expect(existsSync(SUB_CFG)).toBe(false)
  })

  it('an existing id is written; null clears it', async () => {
    seedProviders([VALID])
    expect((await putAgent('sub1', { customProvider: 'p1' })).status).toBe(200)
    expect(JSON.parse(readFileSync(SUB_CFG, 'utf-8')).customProvider).toBe('p1')
    expect((await putAgent('sub1', { customProvider: null })).status).toBe(200)
    expect(JSON.parse(readFileSync(SUB_CFG, 'utf-8')).customProvider).toBeUndefined()
  })
})

describe('POST /api/agents refuses a non-string customProvider before anything is created', () => {
  async function postAgent(body: unknown): Promise<{ status: number; body: any }> {
    const req = new EventEmitter() as unknown as RouteContext['req']
    ;(req as unknown as { headers: Record<string, string> }).headers = {}
    const out: { status: number; body: any } = { status: 0, body: null }
    const res = {
      writeHead(status: number) { out.status = status; return res },
      end(chunk?: string) { if (chunk) out.body = JSON.parse(chunk) },
      setHeader() {},
    }
    const url = new URL('http://localhost:3420/api/agents')
    process.nextTick(() => {
      ;(req as unknown as EventEmitter).emit('data', Buffer.from(JSON.stringify(body)))
      ;(req as unknown as EventEmitter).emit('end')
    })
    await tryHandleAgents({ req, res, path: url.pathname, method: 'POST', url, auth: { kind: 'token' } } as unknown as RouteContext, join(SANDBOX, 'web'))
    return { status: out.status || 200, body: out.body }
  }

  it('an object customProvider is a 400 and no agent dir appears', async () => {
    seedProviders([VALID])
    const r = await postAgent({ name: 'newone', description: 'x', model: 'some-model', customProvider: { id: 'p1' } })
    expect(r.status).toBe(400)
    expect(existsSync(join(SANDBOX, 'agents', 'newone'))).toBe(false)
  })
})

describe('agent-worker: a custom-endpoint worker carries no fleet OAuth token', () => {
  const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '../web/agent-worker.ts'), 'utf-8')

  it('the custom path unsets the inherited token instead of exporting it', () => {
    expect(src).toMatch(/const authEnv = customEnvPrefix\s*\?\s*'unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY; '\s*:\s*\(hasFleetOauthToken\(\) \?/)
  })

  it('the launch string uses authEnv, not an unconditional token export', () => {
    const launchBlock = src.slice(src.indexOf('const launch ='), src.indexOf('execFileSync(TMUX', src.indexOf('const launch =')))
    expect(launchBlock).toContain('authEnv +')
    expect(launchBlock).not.toContain('hasFleetOauthToken()')
    expect(launchBlock.indexOf('authEnv')).toBeLessThan(launchBlock.indexOf('customEnvPrefix'))
  })

  it('an empty MARVEEN_WORKER_MODEL is not an override (|| not ??), so it can never launch --model \'\'', () => {
    expect(src).toMatch(/const WORKER_MODEL_OVERRIDE = process\.env\.MARVEEN_WORKER_MODEL \|\| null/)
  })
})

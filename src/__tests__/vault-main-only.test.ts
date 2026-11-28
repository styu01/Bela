import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { CALLERS, RELAY_PORT, fleetDeps, call, type Auth, type CallOptions } from './helpers/vault-route.js'

// VAULTMAINONLY (KISPROGI 6431 point 5, route level; BELA 6432 decisions): a protected vault
// entry's value only to the main agent's process or the owner's login session; changing one needs the same; a
// binding of one is refused for everyone; with VAULT_MAIN_ONLY=audit a refusal is only logged. Unprotected entries
// stay as they were for every caller. The caller is told by the (in-memory) /proc, never by the token.

const vaultStore = vi.hoisted(() => new Map<string, { label: string; value: string }>())
const envState = vi.hoisted(() => ({} as Record<string, string>))

vi.mock('../web/vault.js', () => ({
  listSecrets: () => [...vaultStore.entries()].map(([id, e]) => ({ id, label: e.label, createdAt: '', updatedAt: '' })),
  setSecret: vi.fn((id: string, label: string, value: string) => { vaultStore.set(id, { label, value }) }),
  getSecret: (id: string) => vaultStore.get(id)?.value ?? null,
  deleteSecret: (id: string) => vaultStore.delete(id),
}))
vi.mock('../web/vault-bindings.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../web/vault-bindings.js')>()),
  addBinding: vi.fn(),
  syncSecret: vi.fn(() => ({ updated: 0, errors: [] })),
  getBindings: () => [],
  removeBindingsForSecret: vi.fn(),
}))
vi.mock('../env.js', async (importOriginal) => {
  const real = await importOriginal<typeof import('../env.js')>()
  return {
    ...real,
    readEnvFile: (keys?: string[]) => (keys && keys.length && keys.every((k) => k.startsWith('VAULT_'))
      ? Object.fromEntries(keys.filter((k) => k in envState).map((k) => [k, envState[k]]))
      : real.readEnvFile(keys)),
  }
})

const { tryHandleConnectors } = await import('../web/routes/connectors.js')
const { setCallerIdentityDepsForTest, VAULT_PROTECTED_ERROR } = await import('../web/vault-guard.js')
const vault = await import('../web/vault.js')
const bindings = await import('../web/vault-bindings.js')
const { logger } = await import('../logger.js')

const PROTECTED = 'vault-vedett-kanari'
const SECRET = 'KANARI-ERTEK-123'
const PLAIN = 'Fal.ai key'

setCallerIdentityDepsForTest(fleetDeps())
afterAll(() => setCallerIdentityDepsForTest(null))

const get = (id: string, opts: CallOptions = {}) => call(tryHandleConnectors, 'GET', `/api/vault/${encodeURIComponent(id)}`, opts)

beforeEach(() => {
  vaultStore.clear()
  vaultStore.set(PROTECTED, { label: 'canary', value: SECRET })
  vaultStore.set('Moho claude token', { label: 'moho', value: 'MOHO-VALUE' })
  vaultStore.set(PLAIN, { label: 'fal', value: 'FAL-VALUE' })
  vaultStore.set('buffer api key', { label: 'buffer', value: 'BUFFER-VALUE' })
  for (const k of Object.keys(envState)) delete envState[k]
  vi.mocked(vault.setSecret).mockClear()
  vi.mocked(bindings.addBinding).mockClear()
})

describe('vault main-only: reading a protected value', () => {
  it('the main agent (and its detached children) reads it; every other caller gets 403 without the value', async () => {
    expect(await get(PROTECTED, { port: CALLERS.main.port })).toEqual({ status: 200, body: { id: PROTECTED, value: SECRET } })
    expect((await get(PROTECTED, { port: CALLERS.mainDetached.port })).status).toBe(200)
    for (const who of ['kisprogi', 'kisprogiSetsid', 'worker', 'webchat'] as const) {
      const r = await get(PROTECTED, { port: CALLERS[who].port })
      expect(r, who).toEqual({ status: 403, body: VAULT_PROTECTED_ERROR })
      expect(JSON.stringify(r.body)).not.toContain(SECRET)
    }
    expect((await get(PROTECTED, { port: RELAY_PORT })).status).toBe(403) // uid 0 relay, no session
    expect((await get(PROTECTED, { port: 59999 })).status).toBe(403) // no socket line: unidentifiable
    expect((await get(PROTECTED, {})).status).toBe(403) // no port
  })

  it('the owner session reads it from anywhere (the browser through the relay); a federation peer, a device key, an agent token never', async () => {
    expect((await get(PROTECTED, { port: RELAY_PORT, auth: { kind: 'session', user: 'istvan' } })).status).toBe(200)
    expect((await get(PROTECTED, { port: CALLERS.main.port, auth: { kind: 'federation', peer: 'codex' } })).status).toBe(403)
    expect((await get(PROTECTED, { port: CALLERS.main.port, auth: { kind: 'device', device: 'phone' } })).status).toBe(403)
    expect((await get(PROTECTED, { port: CALLERS.main.port, auth: { kind: 'token', tokenAgent: 'kisprogi' } as Auth })).status).toBe(403)
  })

  it('an unprotected entry is unchanged for every caller', async () => {
    for (const port of [CALLERS.main.port, CALLERS.kisprogi.port, CALLERS.worker.port, RELAY_PORT, 59999]) {
      expect(await get(PLAIN, { port })).toEqual({ status: 200, body: { id: PLAIN, value: 'FAL-VALUE' } })
    }
  })

  it('the built-in list (the decided ids) is protected; .env VAULT_PROTECTED_IDS only adds; an id with spaces around it counts', async () => {
    expect((await get('Moho claude token', { port: CALLERS.kisprogi.port })).status).toBe(403)
    expect((await get('buffer api key', { port: CALLERS.kisprogi.port })).status).toBe(200)
    envState.VAULT_PROTECTED_IDS = 'buffer api key, x'
    expect((await get('buffer api key', { port: CALLERS.kisprogi.port })).status).toBe(403)
    expect((await get('Moho claude token', { port: CALLERS.kisprogi.port })).status).toBe(403)
    expect((await get(' Moho claude token ', { port: CALLERS.kisprogi.port })).status).toBe(403)
  })

  it('every refusal writes one log line with the ids, the caller class, pid, comm and scope, and never the value', async () => {
    const warn = vi.spyOn(logger, 'warn')
    await get(PROTECTED, { port: CALLERS.kisprogi.port })
    const line = warn.mock.calls.find((c) => String(c[1]).includes('vault: protected entry'))
    expect(line).toBeDefined()
    expect(line![0]).toMatchObject({ vaultIds: [PROTECTED], action: 'read', caller: 'agent', session: 'agent-kisprogi', pid: 9103, comm: 'curl', mode: 'enforce' })
    expect(String((line![0] as { scope: string }).scope)).toMatch(/tmux-spawn-.*\.scope/)
    expect(JSON.stringify(warn.mock.calls)).not.toContain(SECRET)
    warn.mockRestore()
  })

  it('VAULT_MAIN_ONLY=audit: the refusal is only logged and the value goes out (the emergency switch back)', async () => {
    envState.VAULT_MAIN_ONLY = 'audit'
    const warn = vi.spyOn(logger, 'warn')
    expect(await get(PROTECTED, { port: CALLERS.kisprogi.port })).toEqual({ status: 200, body: { id: PROTECTED, value: SECRET } })
    expect(warn.mock.calls.some((c) => String(c[1]).includes('audit mode'))).toBe(true)
    warn.mockRestore()
    envState.VAULT_MAIN_ONLY = 'enforce-typo'
    expect((await get(PROTECTED, { port: CALLERS.kisprogi.port })).status).toBe(403) // anything but audit enforces
  })
})

describe('vault main-only: changing a protected entry, bindings, import', () => {
  it('POST / DELETE of a protected entry: main and the owner session only; an unprotected one as before', async () => {
    let r = await call(tryHandleConnectors, 'POST', '/api/vault', { port: CALLERS.kisprogi.port, body: { id: PROTECTED, label: 'x', value: 'evil' } })
    expect(r.status).toBe(403)
    expect(vaultStore.get(PROTECTED)!.value).toBe(SECRET)
    r = await call(tryHandleConnectors, 'POST', '/api/vault', { port: CALLERS.main.port, body: { id: PROTECTED, label: 'x', value: 'new' } })
    expect(r.status).toBe(200)
    expect(vaultStore.get(PROTECTED)!.value).toBe('new')
    r = await call(tryHandleConnectors, 'DELETE', `/api/vault/${encodeURIComponent(PROTECTED)}`, { port: CALLERS.worker.port })
    expect(r.status).toBe(403)
    expect(vaultStore.has(PROTECTED)).toBe(true)
    r = await call(tryHandleConnectors, 'DELETE', `/api/vault/${encodeURIComponent(PROTECTED)}`, { port: RELAY_PORT, auth: { kind: 'session', user: 'istvan' } })
    expect(r.status).toBe(200)
    expect(vaultStore.has(PROTECTED)).toBe(false)
    r = await call(tryHandleConnectors, 'POST', '/api/vault', { port: CALLERS.kisprogi.port, body: { id: 'new-plain', label: 'x', value: 'v' } })
    expect(r.status).toBe(200)
    r = await call(tryHandleConnectors, 'DELETE', `/api/vault/${encodeURIComponent(PLAIN)}`, { port: CALLERS.kisprogi.port })
    expect(r.status).toBe(200)
  })

  it('a binding of a protected entry is refused for every caller, the main agent and the owner too', async () => {
    const target = { targets: [{ mcpFilePath: '/nonexistent/.mcp.json', serverName: 's' }], envVar: 'K' }
    for (const opts of [{ port: CALLERS.main.port }, { port: RELAY_PORT, auth: { kind: 'session', user: 'istvan' } as Auth }]) {
      const r = await call(tryHandleConnectors, 'POST', '/api/vault/bindings', { ...opts, body: { vaultSecretId: PROTECTED, ...target } })
      expect(r.status).toBe(403)
    }
    expect(bindings.addBinding).not.toHaveBeenCalled()
    const ok = await call(tryHandleConnectors, 'POST', '/api/vault/bindings', { port: CALLERS.kisprogi.port, body: { vaultSecretId: PLAIN, ...target } })
    expect(ok.status).toBe(200)
    expect(bindings.addBinding).toHaveBeenCalledTimes(1)
  })

  it('an import naming a protected id: main / owner only, and never with a binding', async () => {
    const imp = (vaultId: string, createBinding = false) => ({ imports: [{ serverName: 's', envVar: 'K', vaultId, label: 'l', createBinding, targets: [] }] })
    expect((await call(tryHandleConnectors, 'POST', '/api/vault/import', { port: CALLERS.kisprogi.port, body: imp(PROTECTED) })).status).toBe(403)
    expect((await call(tryHandleConnectors, 'POST', '/api/vault/import', { port: CALLERS.main.port, body: imp(PROTECTED) })).status).toBe(200)
    expect((await call(tryHandleConnectors, 'POST', '/api/vault/import', { port: CALLERS.main.port, body: imp(PROTECTED, true) })).status).toBe(403)
    expect((await call(tryHandleConnectors, 'POST', '/api/vault/import', { port: CALLERS.kisprogi.port, body: imp(PLAIN, true) })).status).toBe(200)
  })

  it('an MCP install whose generated vault id would be a protected one is refused (no overwrite, no binding)', async () => {
    const { vaultAndBindEnvSecretsMultiTarget } = await import('../web/routes/connectors.js')
    expect(() => vaultAndBindEnvSecretsMultiTarget('vault', [{ mcpFilePath: '/nonexistent/.mcp.json', serverName: 'vault' }], { 'VEDETT-KANARI': 'x' }))
      .toThrow(/protected/)
    expect(vault.setSecret).not.toHaveBeenCalled()
  })
})

import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { CALLERS, RELAY_PORT, fleetDeps, call } from './helpers/vault-route.js'

// VAULTMAINONLY (BELA 6533): a fleet export with a vault password packs every vault value (encrypted with a password the
// caller chose) and a fleet import with a vault section replaces the whole vault: the main agent or the owner only.

const vaultStore = vi.hoisted(() => new Map<string, { label: string; value: string }>())
const envState = vi.hoisted(() => ({} as Record<string, string>))

vi.mock('../web/vault.js', () => ({
  listSecrets: () => [...vaultStore.entries()].map(([id, e]) => ({ id, label: e.label, createdAt: '', updatedAt: '' })),
  setSecret: vi.fn((id: string, label: string, value: string) => { vaultStore.set(id, { label, value }) }),
  getSecret: (id: string) => vaultStore.get(id)?.value ?? null,
  deleteSecret: (id: string) => vaultStore.delete(id),
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
vi.mock('../web/fleet-transfer.js', async (importOriginal) => {
  const real = await importOriginal<typeof import('../web/fleet-transfer.js')>()
  return {
    ...real,
    exportFleet: vi.fn(() => ({ data: '{"ok":1}', exportedAt: '2026-11-28T00:00:00Z', skippedPackageAgents: [] })),
    importFleet: vi.fn((_raw: string, opts: { vaultImportAllowed?: (ids: string[]) => boolean }) => {
      if (opts.vaultImportAllowed && !opts.vaultImportAllowed(['vault-vedett-kanari'])) throw new real.VaultImportRefused()
      return { ok: true, warnings: [] }
    }),
  }
})

const { tryHandleFleet } = await import('../web/routes/fleet.js')
const fleet = await import('../web/fleet-transfer.js')
const { setCallerIdentityDepsForTest } = await import('../web/vault-guard.js')

const PROTECTED = 'vault-vedett-kanari'
const PLAIN = 'Fal.ai key'

setCallerIdentityDepsForTest(fleetDeps())
afterAll(() => setCallerIdentityDepsForTest(null))

beforeEach(() => {
  vaultStore.clear()
  vaultStore.set(PROTECTED, { label: 'canary', value: 'KANARI' })
  vaultStore.set(PLAIN, { label: 'fal', value: 'FAL-VALUE' })
  for (const k of Object.keys(envState)) delete envState[k]
})

describe('vault main-only: the fleet export with secrets and a fleet import with a vault', () => {
  it('an export with a vault password needs main / owner (it packs every value); without a password it is unchanged', async () => {
    const pw = { 'x-vault-password': 'a-long-enough-password' }
    expect((await call(tryHandleFleet, 'GET', '/api/fleet/export', { port: CALLERS.kisprogi.port, headers: pw })).status).toBe(403)
    expect((await call(tryHandleFleet, 'GET', '/api/fleet/export', { port: CALLERS.main.port, headers: pw })).status).toBe(200)
    expect((await call(tryHandleFleet, 'GET', '/api/fleet/export', { port: RELAY_PORT, headers: pw, auth: { kind: 'session', user: 'istvan' } })).status).toBe(200)
    expect((await call(tryHandleFleet, 'GET', '/api/fleet/export', { port: CALLERS.kisprogi.port })).status).toBe(200)
    // a vault without a protected entry: no restriction
    vaultStore.clear()
    vaultStore.set(PLAIN, { label: 'fal', value: 'FAL-VALUE' })
    expect((await call(tryHandleFleet, 'GET', '/api/fleet/export', { port: CALLERS.kisprogi.port, headers: pw })).status).toBe(200)
  })

  it('an import carrying a vault: refused for a sub-agent (403, nothing written), allowed for main', async () => {
    expect((await call(tryHandleFleet, 'POST', '/api/fleet/import?apply=true', { port: CALLERS.kisprogi.port, body: {} })).status).toBe(403)
    expect((await call(tryHandleFleet, 'POST', '/api/fleet/import?apply=true', { port: CALLERS.main.port, body: {} })).status).toBe(200)
    expect(vi.mocked(fleet.importFleet)).toHaveBeenCalled()
  })
})

import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

// VAULTMAINONLY: a binding writes the value into an MCP config file every agent can read. A protected entry is never
// written there, whichever route made the binding (an old one, a fleet import, the catalog): syncSecret refuses it.

const tmp = vi.hoisted(() => {
  const { mkdtempSync } = require('node:fs') as typeof import('node:fs')
  const { tmpdir } = require('node:os') as typeof import('node:os')
  const { join } = require('node:path') as typeof import('node:path')
  return mkdtempSync(join(tmpdir(), 'vault-bind-protected-'))
})
const envState = vi.hoisted(() => ({} as Record<string, string>))

vi.mock('../config.js', async (importOriginal) => ({ ...(await importOriginal<typeof import('../config.js')>()), PROJECT_ROOT: tmp }))
vi.mock('../web/vault.js', () => ({
  getSecret: (id: string) => ({ 'vault-vedett-kanari': 'KANARI-ERTEK', plain: 'PLAIN-ERTEK' } as Record<string, string>)[id] ?? null,
  listSecrets: () => [],
}))
vi.mock('../env.js', async (importOriginal) => {
  const real = await importOriginal<typeof import('../env.js')>()
  return { ...real, readEnvFile: (keys?: string[]) => (keys?.every((k) => k.startsWith('VAULT_')) ? Object.fromEntries(keys.filter((k) => k in envState).map((k) => [k, envState[k]])) : real.readEnvFile(keys)) }
})

const { syncSecret } = await import('../web/vault-bindings.js')
afterAll(() => rmSync(tmp, { recursive: true, force: true }))

const mcp = join(tmp, 'agent.mcp.json')
beforeEach(() => {
  mkdirSync(join(tmp, 'store'), { recursive: true })
  writeFileSync(join(tmp, 'store', 'vault-bindings.json'), JSON.stringify({ bindings: [
    { vaultSecretId: 'vault-vedett-kanari', envVar: 'KANARI', targets: [{ mcpFilePath: mcp, serverName: 's' }] },
    { vaultSecretId: 'plain', envVar: 'PLAIN', targets: [{ mcpFilePath: mcp, serverName: 's' }] },
  ] }))
  writeFileSync(mcp, JSON.stringify({ mcpServers: { s: { command: 'x', env: {} } } }))
  for (const k of Object.keys(envState)) delete envState[k]
})

describe('vault-bindings: a protected entry is never synced into an MCP config', () => {
  it('syncSecret refuses it and leaves the file without its value; an unprotected one syncs as before', () => {
    const r = syncSecret('vault-vedett-kanari')
    expect(r.updated).toBe(0)
    expect(r.errors.join(' ')).toMatch(/protected/)
    expect(readFileSync(mcp, 'utf-8')).not.toContain('KANARI')
    expect(syncSecret('plain').updated).toBe(1)
    expect(readFileSync(mcp, 'utf-8')).toContain('PLAIN')
  })

  it('VAULT_MAIN_ONLY=audit syncs it (logged), as before the change', () => {
    envState.VAULT_MAIN_ONLY = 'audit'
    expect(syncSecret('vault-vedett-kanari').updated).toBe(1)
  })
})

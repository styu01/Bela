// KISPROGI 6416 H1 (BELA 6417): with the public HTTPS listener configured (WEB_PUBLIC_PORT + WEB_PUBLIC_URL), the
// address the AGENTS reach the API on stays local: the curl examples written into an agent's CLAUDE.md and the
// heartbeat's dashboard origin. DASHBOARD_PUBLIC_URL (that address) is a separate key and is not set here.
import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest'
import { mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'

const NAME = 'zz-pubhttps-origin-probe'
const saved: Record<string, string | undefined> = {}

beforeAll(() => {
  for (const k of ['WEB_PUBLIC_PORT', 'WEB_PUBLIC_URL', 'DASHBOARD_PUBLIC_URL']) saved[k] = process.env[k]
  process.env.WEB_PUBLIC_PORT = '3999'
  process.env.WEB_PUBLIC_URL = 'https://dash.example.org'
  delete process.env.DASHBOARD_PUBLIC_URL
  vi.resetModules()
})
afterAll(async () => {
  for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v }
  const { agentDir } = await import('../web/agent-config.js')
  try { rmSync(agentDir(NAME), { recursive: true, force: true }) } catch { /* */ }
})

describe('the agents keep the local API address while the public listener is configured', () => {
  it('config: the public keys are read, DASHBOARD_PUBLIC_URL stays empty', async () => {
    const c = await import('../config.js')
    expect(c.WEB_PUBLIC_URL).toBe('https://dash.example.org')
    expect(c.WEB_PUBLIC_PORT_RAW).toBe('3999')
    expect(c.DASHBOARD_PUBLIC_URL).toBe('')
  })
  it("an agent's CLAUDE.md curl examples go to http://localhost:<WEB_PORT>, never to the public host", async () => {
    const { agentDir } = await import('../web/agent-config.js')
    const { ensureAutonomySection } = await import('../web/agent-scaffold.js')
    const { WEB_PORT } = await import('../config.js')
    mkdirSync(agentDir(NAME), { recursive: true })
    writeFileSync(join(agentDir(NAME), 'CLAUDE.md'), '# probe\n')
    ensureAutonomySection(NAME)
    const md = readFileSync(join(agentDir(NAME), 'CLAUDE.md'), 'utf-8')
    expect(md).toContain(`curl -s -X POST http://localhost:${WEB_PORT}/api/messages`)
    expect(md).toContain(`http://localhost:${WEB_PORT}/api/approvals`)
    expect(md).not.toContain('dash.example.org')
  })
  it("the heartbeat's dashboard origin is http://localhost:<WEB_PORT>", async () => {
    const { currentHeartbeatIdentity } = await import('../web/heartbeat-agent-scaffold.js')
    const { WEB_PORT } = await import('../config.js')
    expect(currentHeartbeatIdentity().dashboardOrigin).toBe(`http://localhost:${WEB_PORT}`)
  })
})

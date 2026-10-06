import { describe, expect, it, beforeEach, afterAll } from 'vitest'
import { EventEmitter } from 'node:events'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { CLI_VERSION_OVERRIDE_ENV, resetClaudeCliVersionCache } from '../web/claude-cli-version.js'
import { refuseIfCliCannotLaunch, tryHandleAgents } from '../web/routes/agents.js'
import type { RouteContext } from '../web/routes/types.js'

const REPO = process.cwd()
const oldOverride = process.env[CLI_VERSION_OVERRIDE_ENV]

beforeEach(() => resetClaudeCliVersionCache())
afterAll(() => {
  if (oldOverride === undefined) delete process.env[CLI_VERSION_OVERRIDE_ENV]
  else process.env[CLI_VERSION_OVERRIDE_ENV] = oldOverride
})

describe('model picker CLI gate', () => {
  it('allows Sonnet 5.5 on the locally measured CLI minimum, but rejects the immediately older CLI', async () => {
    process.env[CLI_VERSION_OVERRIDE_ENV] = '2.1.220 (Claude Code)'
    expect(await refuseIfCliCannotLaunch('claude-sonnet-5-5')).toBeNull()
    expect(await refuseIfCliCannotLaunch('claude-sonnet-5')).toBeNull()
    process.env[CLI_VERSION_OVERRIDE_ENV] = '2.1.219 (Claude Code)'
    resetClaudeCliVersionCache()
    expect(await refuseIfCliCannotLaunch('claude-sonnet-5-5'))
      .toMatchObject({ installedCli: '2.1.219', minCli: '2.1.220' })
  })

  it('returns the measured support state and only the 1M Opus 5.5 variant from the API', async () => {
    process.env[CLI_VERSION_OVERRIDE_ENV] = '2.1.220'
    const req = new EventEmitter() as unknown as RouteContext['req']
    let status = 200
    let raw = ''
    const res = {
      statusCode: 200,
      setHeader() {},
      writeHead(code: number) { status = code },
      end(chunk?: string) { raw = String(chunk ?? '') },
    } as unknown as RouteContext['res']
    const handled = await tryHandleAgents({
      req, res, path: '/api/models/available', method: 'GET', url: new URL('http://127.0.0.1/api/models/available'),
    } as RouteContext, join(REPO, 'web'))
    expect(handled).toBe(true)
    expect(status).toBe(200)
    const payload = JSON.parse(raw) as { claude: Array<{ id: string }>; claudeSupport: { unsupported: Array<{ id: string }> } }
    expect(payload.claude.map((model) => model.id)).toContain('claude-opus-5-5[1m]')
    expect(payload.claude.map((model) => model.id)).not.toContain('claude-opus-5-5')
    expect(payload.claudeSupport.unsupported.map((model) => model.id)).not.toContain('claude-sonnet-5-5')
  })

  it('keeps the compatibility gate on both writers and exposes both select hints', () => {
    const route = readFileSync(join(REPO, 'src/web/routes/agents.ts'), 'utf8')
    const html = readFileSync(join(REPO, 'web/index.html'), 'utf8')
    const app = readFileSync(join(REPO, 'web/app.js'), 'utf8')
    expect(route).toContain('const cliGate = await refuseIfCliCannotLaunch(model)')
    expect(route).toContain('refuseIfCliCannotLaunch(String(data.model))')
    expect(html.split('<option value="claude-opus-5-5[1m]"').length - 1).toBe(2)
    expect(html.split('<option value="claude-opus-5-5"').length - 1).toBe(0)
    expect(html).toContain('id="agentModelCliHint"')
    expect(html).toContain('id="editAgentModelCliHint"')
    expect(app).toContain('function applyClaudeCliGate(data)')
    expect(app).toContain('option.disabled = option.value !== current')
  })
})

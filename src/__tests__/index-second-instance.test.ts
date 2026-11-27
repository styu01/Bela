// PROGI 6352 / BELA 6357: the services src/index.ts starts itself follow the dashboard's second-instance decision
// (shouldRunBackgroundServices, upstream 2c093a10): a worktree checkout, a temp-dir clone or a WEB_ONLY staging
// instance does not start the heartbeat agent on the shared tmux server, the Telegram invite auto-approve monitor
// (shared ~/.claude/channels/telegram/access.json), the owner's daily digest, nor the channel-request / store
// watchers. index.ts runs main() on import, so these are source pins (the house idiom for web.ts wiring).
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const src = readFileSync(join(__dirname, '..', 'index.ts'), 'utf-8').replace(/^\s*\/\/.*$/gm, '')
const decision = src.indexOf('const services = shouldRunBackgroundServices(')
const servicesOff = src.indexOf('const servicesOff = !services.run')

describe('index.ts: the second-instance decision', () => {
  it('is taken from the process environment (WEB_ONLY and the opt-in), before every gated service', () => {
    expect(decision).toBeGreaterThan(-1)
    expect(servicesOff).toBeGreaterThan(decision)
    const d = src.slice(decision, servicesOff)
    expect(d).toContain("webOnly: process.env['WEB_ONLY'] === 'true'")
    expect(d).toContain("allowSecondInstance: process.env[ALLOW_SECOND_INSTANCE_SERVICES_KEY] === '1'")
    expect(d).toContain('projectRoot: PROJECT_ROOT')
    for (const call of ['scheduleDailyDigest()', 'ensureHeartbeatAgent()', 'startInviteMonitor(', 'startChannelRequestWatcher()', 'startStoreWatcher()']) {
      expect(src.lastIndexOf(call), call).toBeGreaterThan(servicesOff)
    }
  })

  it('every gated start is behind servicesOff, at its one call site', () => {
    const digest = src.lastIndexOf('scheduleDailyDigest()')
    expect(src.slice(digest - 'if (!servicesOff) '.length, digest)).toBe('if (!servicesOff) ')
    for (const call of ['startInviteMonitor(', 'startChannelRequestWatcher()', 'startStoreWatcher()']) {
      const at = src.indexOf(call)
      expect(src.indexOf(call, at + 1), call).toBe(-1)
      const gate = src.lastIndexOf('  if (!servicesOff) {\n', at)
      expect(gate, call).toBeGreaterThan(servicesOff)
      expect(src.slice(gate, at).split('\n  }\n').length - 1, call).toBe(0) // still inside that block
    }
    const hb = src.indexOf('ensureHeartbeatAgent()')
    const hbGate = src.lastIndexOf('  if (servicesOff) {', hb)
    expect(hbGate).toBeGreaterThan(servicesOff)
    expect(src.slice(hbGate, hb)).toContain('} else if (shouldBootHeartbeatAgent(')
    expect(src.match(/startAgentProcess\(HEARTBEAT_AGENT_NAME\)/g)).toHaveLength(1)
    expect(src.indexOf('startAgentProcess(HEARTBEAT_AGENT_NAME)')).toBeGreaterThan(hbGate)
  })

  it('the memory decay sweep (this instance\'s own DB) is not gated', () => {
    const decay = src.indexOf('  runDecaySweep()\n')
    expect(decay).toBeGreaterThan(servicesOff)
    expect(src.slice(servicesOff, decay)).not.toMatch(/if \(!servicesOff\) \{[^}]*$/)
  })
})

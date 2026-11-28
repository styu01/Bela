import { describe, it, expect, afterAll } from 'vitest'
import { readFileSync, rmSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { writeAgentSettingsFromProfile, SUBAGENT_TOTP_STORE_DENY, SUBAGENT_PACKAGE_CREDENTIAL_DENY, agentGetsGovernanceGates } from '../web/agent-scaffold.js'
import { loadProfileTemplate } from '../web/profiles.js'
import { agentDir } from '../web/agent-config.js'
import { MAIN_AGENT_ID } from '../config.js'

// DOCKERHTTPS1119 (upstream 8a96be49, ported, BELA 6404): every sub-agent's settings deny the Read tool on the
// dashboard login's second-factor store; pushed on every spawn (writeAgentSettingsFromProfile rebuilds the deny list).
const NAME = 'zz-totp-deny-probe'
afterAll(() => { try { rmSync(agentDir(NAME), { recursive: true, force: true }) } catch { /* */ } })

describe('the second-factor store is fenced from the sub-agents', () => {
  it('the measured rule shape (//**/), not the relative one Claude Code let through', () => {
    expect(SUBAGENT_TOTP_STORE_DENY).toBe('Read(//**/store/.dashboard-totp.json)')
  })
  it('a sub-agent spawn writes it into permissions.deny, once, and again on the next spawn', () => {
    expect(agentGetsGovernanceGates(NAME)).toBe(true)
    const profile = loadProfileTemplate('developer-junior')
    writeAgentSettingsFromProfile(NAME, profile)
    writeAgentSettingsFromProfile(NAME, profile)
    const s = JSON.parse(readFileSync(join(agentDir(NAME), '.claude', 'settings.json'), 'utf-8'))
    expect(s.permissions.deny.filter((r: string) => r === SUBAGENT_TOTP_STORE_DENY)).toHaveLength(1)
  })
  it('a profile that already declares the rule does not get it twice', () => {
    const base = loadProfileTemplate('developer-junior')
    const profile = { ...base, filesystem: { ...base.filesystem, deny: [...base.filesystem.deny, SUBAGENT_TOTP_STORE_DENY] } }
    writeAgentSettingsFromProfile(NAME, profile)
    const s = JSON.parse(readFileSync(join(agentDir(NAME), '.claude', 'settings.json'), 'utf-8'))
    expect(s.permissions.deny.filter((r: string) => r === SUBAGENT_TOTP_STORE_DENY)).toHaveLength(1)
  })
  it('the main agent is not a sub-agent (the governance condition it rides on)', () => {
    expect(agentGetsGovernanceGates(MAIN_AGENT_ID)).toBe(false)
    expect(existsSync(join(agentDir(NAME), '.claude', 'settings.json'))).toBe(true)
  })
})

// KISPROGI 6454 K (this fork, BELA 6468): the agent package device credential is fenced the same way.
describe('the agent package device credential is fenced from the sub-agents', () => {
  it('the same measured rule shape, naming the credential file the network module writes', () => {
    expect(SUBAGENT_PACKAGE_CREDENTIAL_DENY).toBe('Read(//**/store/agent-package-credential.json)')
  })
  it('a sub-agent spawn writes it into permissions.deny, once, next to the second-factor rule', () => {
    const profile = loadProfileTemplate('developer-junior')
    writeAgentSettingsFromProfile(NAME, profile)
    writeAgentSettingsFromProfile(NAME, profile)
    const s = JSON.parse(readFileSync(join(agentDir(NAME), '.claude', 'settings.json'), 'utf-8'))
    expect(s.permissions.deny.filter((r: string) => r === SUBAGENT_PACKAGE_CREDENTIAL_DENY)).toHaveLength(1)
    expect(s.permissions.deny).toContain(SUBAGENT_TOTP_STORE_DENY)
  })
  it('a profile that already declares it does not get it twice', () => {
    const base = loadProfileTemplate('developer-junior')
    const profile = { ...base, filesystem: { ...base.filesystem, deny: [...base.filesystem.deny, SUBAGENT_PACKAGE_CREDENTIAL_DENY] } }
    writeAgentSettingsFromProfile(NAME, profile)
    const s = JSON.parse(readFileSync(join(agentDir(NAME), '.claude', 'settings.json'), 'utf-8'))
    expect(s.permissions.deny.filter((r: string) => r === SUBAGENT_PACKAGE_CREDENTIAL_DENY)).toHaveLength(1)
  })
  it('the file name is the one the network module writes', async () => {
    const src = readFileSync(join(process.cwd(), 'src', 'web', 'agent-package', 'remote.ts'), 'utf-8')
    expect(src).toMatch(/['"]agent-package-credential\.json['"]/)
  })
})

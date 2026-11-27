// The fleet base rules (BELA 6404; upstream 9301a1dd's lift, built on this fork's own creation prompt). One source,
// fleetBaseRulesTemplate, for the sub-agent creation prompt and for a package agent's rendered block. Upstream's two
// test files pin upstream's prompt (languages, helper recipes, eight rules), which this fork does not have; this file
// pins the same properties on this fork's text. The byte identity of the creation prompt across the lift was measured
// once, on the port: the prompt captured before and after the change compared equal (10277 bytes).
import { describe, it, expect, vi, afterAll } from 'vitest'
import { mkdtempSync, writeFileSync, readFileSync, mkdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

const tmpRoot = mkdtempSync(join(tmpdir(), 'marveen-fleet-base-rules-'))
vi.mock('../config.js', async (orig) => {
  const real = await orig<typeof import('../config.js')>()
  return { ...real, STORE_DIR: join(tmpRoot, 'store'), PROJECT_ROOT: tmpRoot, OWNER_DRIVE_FOLDER: '' }
})
vi.mock('../web/agent-config.js', async (orig) => {
  const real = await orig<typeof import('../web/agent-config.js')>()
  return { ...real, agentDir: (name: string) => join(tmpRoot, 'agents', name) }
})
const captured = { prompt: '' }
vi.mock('../agent.js', async (orig) => ({ ...(await orig<typeof import('../agent.js')>()), runAgent: async (p: string) => { captured.prompt = p; return { text: 'BODY', error: null } } }))

const sc = await import('../web/agent-scaffold.js')
const { PACKAGE_SHELL_PREFIX_TEXT } = await import('../web/agent-package/marker.js')
afterAll(() => rmSync(tmpRoot, { recursive: true, force: true }))
const TOKEN = join(tmpRoot, 'store', '.dashboard-token')

function agent(name: string, claudeMd: string, manifest = false): string {
  const dir = join(tmpRoot, 'agents', name)
  mkdirSync(join(dir, '.package'), { recursive: true })
  writeFileSync(join(dir, 'CLAUDE.md'), claudeMd)
  if (manifest) writeFileSync(join(dir, '.package', 'manifest.json'), '{"schema":1}')
  return join(dir, 'CLAUDE.md')
}

describe('fleetBaseRulesTemplate is the creation prompt\'s text (one source)', () => {
  it('the creation prompt embeds the template verbatim, right before the output instruction', async () => {
    await sc.generateClaudeMd('neu', 'a test agent', 'claude-opus-5-5')
    const t = sc.fleetBaseRulesTemplate({ agentName: 'neu', tokenPath: TOKEN, driveFolder: '' })
    expect(captured.prompt).toContain(t + '\n\nOutput ONLY the markdown content, no code fences.')
    expect(t.startsWith('## Új ismeretlen sender első üzenete (ARANYSZABÁLY)')).toBe(true)
    expect(t).toContain('7. **Login-automatizálás')
    expect(t).toContain(`$(cat ${TOKEN})`)
  })

  it('the Drive line follows the input: a folder, or "ask the owner"', () => {
    expect(sc.fleetBaseRulesTemplate({ agentName: 'a', tokenPath: TOKEN, driveFolder: 'F123' })).toContain('drive/folders/F123')
    expect(sc.fleetBaseRulesTemplate({ agentName: 'a', tokenPath: TOKEN, driveFolder: '' })).toContain('MIELŐTT bárhova írsz, kérd el')
  })

  it('renderFleetBaseRules leaves no AGENT_NAME placeholder and names the agent', () => {
    const r = sc.renderFleetBaseRules({ agentName: 'pkg-a', tokenPath: TOKEN, driveFolder: '' })
    expect(r).not.toContain('AGENT_NAME')
    expect(r).toContain('\\"from\\":\\"pkg-a\\"')
  })
})

describe('ensureFleetBaseRulesSection', () => {
  it('a hand-made agent (no markers, no manifest) is not touched, byte for byte', () => {
    const p = agent('hand', '# Hand made\nbody\n')
    sc.ensureFleetBaseRulesSection('hand', '')
    expect(readFileSync(p, 'utf-8')).toBe('# Hand made\nbody\n')
  })

  it('a package agent that lost the block gets it back right after the shell prefix; the prefix stays first', () => {
    const p = agent('pkg', PACKAGE_SHELL_PREFIX_TEXT + '\n<!-- roster -->\n', true)
    sc.ensureFleetBaseRulesSection('pkg', '')
    const out = readFileSync(p, 'utf-8')
    expect(out.startsWith(PACKAGE_SHELL_PREFIX_TEXT + '\n' + sc.FLEET_BASE_RULES_BEGIN)).toBe(true)
    expect(out).toContain(sc.FLEET_BASE_RULES_END)
    expect(out).not.toContain('AGENT_NAME')
  })

  it('a standing block is re-rendered in place, and a second run changes nothing', () => {
    const p = agent('pkg2', PACKAGE_SHELL_PREFIX_TEXT + '\n' + sc.FLEET_BASE_RULES_BEGIN + '\nOLD\n' + sc.FLEET_BASE_RULES_END + '\ntail\n', true)
    sc.ensureFleetBaseRulesSection('pkg2', 'F9')
    const once = readFileSync(p, 'utf-8')
    expect(once).not.toContain('\nOLD\n')
    expect(once).toContain('drive/folders/F9')
    expect(once.split(sc.FLEET_BASE_RULES_BEGIN).length).toBe(2)
    expect(once.endsWith('tail\n')).toBe(true)
    sc.ensureFleetBaseRulesSection('pkg2', 'F9')
    expect(readFileSync(p, 'utf-8')).toBe(once)
  })
})

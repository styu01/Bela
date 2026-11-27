// HOSTINGERRETEST1125 (upstream 4a777cc9, ported and adapted; BELA 6404): the in-process main respawns carry the main
// agent's custom provider, as channels.sh's cold launch does. Before: a respawn-pane restart started the main agent
// without ANTHROPIC_BASE_URL / the key. This fork's builder has no pane-auth post-file and takes isolatedConfigDir +
// fleetToken directly, so the env is pinned after the fleet-token export instead (LOCAL ADAPTATION).
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { buildMainSessionRespawnCmd } from '../web/channel-monitor.js'
import { mainCustomProviderLaunchEnv, MainCustomProviderBroken, type CustomProviderEnvDeps } from '../web/main-custom-provider-env.js'

const ENV = "unset CLAUDE_CODE_OAUTH_TOKEN && export ANTHROPIC_BASE_URL='https://llm.example' && export ANTHROPIC_API_KEY=\"$(cat '/k')\" && unset ANTHROPIC_AUTH_TOKEN && export ANTHROPIC_MODEL='some-model'"
const base = {
  claudePath: '/x/claude', pluginId: 'telegram@claude-plugins-official',
  model: 'claude-opus-5-5', effort: 'high', continueSession: false, isolatedConfigDir: '/cfg', fleetToken: true,
}

describe('buildMainSessionRespawnCmd: the custom provider env', () => {
  it('goes after the config dir and fleet-token exports and before claude, and the --model flag is dropped', () => {
    const cmd = buildMainSessionRespawnCmd({ ...base, customProviderEnv: ENV })
    const env = cmd.indexOf(ENV)
    expect(env).toBeGreaterThan(cmd.indexOf('export CLAUDE_CODE_OAUTH_TOKEN='))
    expect(env).toBeGreaterThan(cmd.indexOf("export CLAUDE_CONFIG_DIR='/cfg'"))
    expect(env).toBeLessThan(cmd.indexOf('/x/claude'))
    expect(cmd).not.toContain('--model')
    // effort is not a model choice: it stays
    expect(cmd).toContain("--effort 'high'")
  })

  it('without a provider the command is byte-for-byte the one without the option: --model, no provider env', () => {
    const without = buildMainSessionRespawnCmd({ ...base })
    for (const customProviderEnv of [undefined, '']) {
      const cmd = buildMainSessionRespawnCmd({ ...base, customProviderEnv })
      expect(cmd).toBe(without)
      expect(cmd).toContain("--model 'claude-opus-5-5'")
      expect(cmd).not.toContain('ANTHROPIC_BASE_URL')
    }
  })
})

describe('mainCustomProviderLaunchEnv', () => {
  const deps = (over: Partial<CustomProviderEnvDeps> & { calls?: string[][] } = {}): CustomProviderEnvDeps => ({
    provider: () => 'my-provider',
    runHelper: (args) => { over.calls?.push(args); return ENV + ' && ' },
    dirExists: () => true,
    ...over,
  })

  it('no provider: empty, the helper is not run, and a lazy config dir is not resolved', () => {
    const calls: string[][] = []
    let resolved = 0
    expect(mainCustomProviderLaunchEnv('/cfg', deps({ provider: () => null, calls }))).toBe('')
    expect(mainCustomProviderLaunchEnv(() => { resolved++; return '/cfg' }, deps({ provider: () => null, calls }))).toBe('')
    expect(calls).toEqual([])
    expect(resolved).toBe(0)
  })

  it("a provider: the helper's prefix without its trailing &&", () => {
    expect(mainCustomProviderLaunchEnv(null, deps())).toBe(ENV)
  })

  it('the config dir (plain or lazy) is passed only when it exists, as channels.sh', () => {
    const calls: string[][] = []
    mainCustomProviderLaunchEnv('/cfg', deps({ calls }))
    mainCustomProviderLaunchEnv(() => '/cfg2', deps({ calls }))
    mainCustomProviderLaunchEnv('/gone', deps({ calls, dirExists: () => false }))
    mainCustomProviderLaunchEnv(null, deps({ calls }))
    mainCustomProviderLaunchEnv(() => null, deps({ calls }))
    expect(calls).toEqual([['/cfg'], ['/cfg2'], [], [], []])
  })

  it('a configured provider the helper cannot serve stops the respawn (no silent wrong-backend launch)', () => {
    const failing = deps({ runHelper: () => { throw Object.assign(new Error('exit 1'), { stderr: 'main-agent-custom-provider: vault key "x" missing\n' }) } })
    expect(() => mainCustomProviderLaunchEnv(null, failing)).toThrow(MainCustomProviderBroken)
    expect(() => mainCustomProviderLaunchEnv(null, failing)).toThrow(/vault key "x" missing/)
    expect(() => mainCustomProviderLaunchEnv(null, deps({ runHelper: () => '' }))).toThrow(MainCustomProviderBroken)
  })
})

it('all three in-process main respawns decide the provider env first and pass it to the builder', () => {
  const src = readFileSync(join(process.cwd(), 'src', 'web', 'channel-monitor.ts'), 'utf-8')
  expect(src.match(/const customProviderEnv = mainCustomProviderLaunchEnv\(\(\) => ensureMainAgentIsolatedConfigDir\(\)\)/g)?.length).toBe(3)
  expect(src.match(/^\s*customProviderEnv,$/gm)?.length).toBe(3)
  expect(src.match(/buildMainSessionRespawnCmd\(\{/g)?.length).toBe(3)
  for (const fn of ['export function respawnMainSessionFresh(', 'export async function resumeMarveenSession(', 'function respawnMarveenSessionFresh(']) {
    const body = src.slice(src.indexOf(fn), src.indexOf('buildMainSessionRespawnCmd({', src.indexOf(fn)))
    const env = body.indexOf('mainCustomProviderLaunchEnv(')
    expect(env, fn).toBeGreaterThan(-1)
    for (const step of ['reapChannelOrphans(', 'reapDetachedChannelClaudes(', 'ensureSharedClaudeOnboarded(']) {
      const at = body.indexOf(step)
      if (at >= 0) expect(env, `${fn} ${step}`).toBeLessThan(at)
    }
  }
})

it('channels.sh drops the --model flag when the custom provider env is injected, after MODEL_FLAG was set', () => {
  const sh = readFileSync(join(process.cwd(), 'scripts', 'channels.sh'), 'utf-8')
  const block = sh.slice(sh.indexOf('if [ -n "$_cp_env" ]; then'), sh.indexOf('unset _cp_env _cp_rc _cp_cfg_dir'))
  expect(block).toContain('CUSTOM_PROVIDER_ENV="$_cp_env"')
  expect(block).toMatch(/^\s*MODEL_FLAG=""$/m)
  expect(sh.indexOf('[ -n "$MAIN_MODEL" ] && MODEL_FLAG=')).toBeGreaterThan(-1)
  expect(sh.indexOf('[ -n "$MAIN_MODEL" ] && MODEL_FLAG=')).toBeLessThan(sh.indexOf('if [ -n "$_cp_env" ]; then'))
})

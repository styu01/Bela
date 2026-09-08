import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync, readdirSync, chmodSync, statSync, unlinkSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

// Same sandbox shape as isolated-channel-config.test.ts: homedir() and
// agentDir() are redirected into a throwaway temp tree, so nothing here can
// read or write the real ~/.claude of the machine running the suite.
let SANDBOX = ''
vi.mock('node:os', async (orig) => {
  const actual = await orig<typeof import('node:os')>()
  return { ...actual, homedir: () => join(SANDBOX, 'home') }
})
vi.mock('../web/agent-config.js', async (orig) => {
  const actual = await orig<typeof import('../web/agent-config.js')>()
  return { ...actual, agentDir: (name: string) => join(SANDBOX, 'agents', name) }
})

const { ensureIsolatedChannelConfigDir } = await import('../web/agent-process.js')

const AGENT = 'testagent'

function sharedDotClaude(): string { return join(SANDBOX, 'home', '.claude.json') }
function isolatedDotClaude(): string {
  return join(SANDBOX, 'agents', AGENT, '.claude-config', '.claude.json')
}
function writeShared(servers: Record<string, unknown>): void {
  writeFileSync(sharedDotClaude(), JSON.stringify({ hasCompletedOnboarding: true, mcpServers: servers }, null, 2))
}
function readIsolated(): Record<string, unknown> {
  return JSON.parse(readFileSync(isolatedDotClaude(), 'utf-8')) as Record<string, unknown>
}
function servers(): Record<string, unknown> {
  return (readIsolated().mcpServers ?? {}) as Record<string, unknown>
}
function writeProjectMcp(servers: Record<string, unknown>): void {
  writeFileSync(join(SANDBOX, 'agents', AGENT, '.mcp.json'), JSON.stringify({ mcpServers: servers }, null, 2))
}

beforeEach(() => {
  SANDBOX = mkdtempSync(join(tmpdir(), 'mcpseed-'))
  const claude = join(SANDBOX, 'home', '.claude')
  mkdirSync(claude, { recursive: true })
  writeFileSync(join(claude, 'settings.json'), JSON.stringify({ enabledPlugins: {} }))
  mkdirSync(join(SANDBOX, 'agents', AGENT), { recursive: true })
  writeShared({ gmail: { command: 'npx', args: ['gmail-mcp'] } })
})
afterEach(() => { rmSync(SANDBOX, { recursive: true, force: true }) })

describe('isolated config dir: mcpServers reconcile', () => {
  it('propagates a server added to the shared config AFTER the dir was provisioned', () => {
    // First provision: the isolated dir is seeded from the shared config.
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect(Object.keys(servers())).toEqual(['gmail'])

    // The operator adds a second server to the shared config, as when rolling
    // one out to a running fleet.
    writeShared({
      gmail: { command: 'npx', args: ['gmail-mcp'] },
      'google-drive': { command: 'npx', args: ['gdrive-mcp'] },
    })

    // Re-provision, i.e. the agent restarts. THIS is the regression from #834:
    // before the fix the new server never arrived here.
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect(Object.keys(servers()).sort()).toEqual(['gmail', 'google-drive'])
    expect(servers()['google-drive']).toEqual({ command: 'npx', args: ['gdrive-mcp'] })
  })

  it('never overwrites an entry that already exists in the isolated config', () => {
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')

    // Claude Code (or a deliberate per-agent scoping decision) evolves the
    // entry: same key, different definition.
    const cur = readIsolated()
    cur.mcpServers = { gmail: { command: 'npx', args: ['gmail-mcp'], env: { SCOPE: 'agent-local' } } }
    writeFileSync(isolatedDotClaude(), JSON.stringify(cur, null, 2))

    // The shared config still carries its own, different definition of `gmail`.
    writeShared({
      gmail: { command: 'npx', args: ['gmail-mcp'] },
      'google-drive': { command: 'npx', args: ['gdrive-mcp'] },
    })
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')

    // The evolved entry survives untouched, and the genuinely missing one is added.
    expect(servers().gmail).toEqual({ command: 'npx', args: ['gmail-mcp'], env: { SCOPE: 'agent-local' } })
    expect(servers()['google-drive']).toEqual({ command: 'npx', args: ['gdrive-mcp'] })
  })

  it('leaves a server removed from the shared config in place (additive only)', () => {
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    writeShared({})
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect(Object.keys(servers())).toEqual(['gmail'])
  })

  it('does not touch a non-object mcpServers instead of repairing it', () => {
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    const cur = readIsolated()
    cur.mcpServers = 'corrupted-by-something-else'
    writeFileSync(isolatedDotClaude(), JSON.stringify(cur, null, 2))

    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect(readIsolated().mcpServers).toBe('corrupted-by-something-else')
  })

  it('preserves unrelated keys and keeps hasCompletedOnboarding set', () => {
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    const cur = readIsolated()
    cur.projects = { '/some/path': { hasTrustDialogAccepted: true } }
    cur.hasCompletedOnboarding = false
    writeFileSync(isolatedDotClaude(), JSON.stringify(cur, null, 2))

    writeShared({ gmail: { command: 'npx', args: ['gmail-mcp'] }, extra: { command: 'x' } })
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')

    const after = readIsolated()
    expect(after.projects).toEqual({ '/some/path': { hasTrustDialogAccepted: true } })
    expect(after.hasCompletedOnboarding).toBe(true)
    expect(Object.keys(after.mcpServers as object).sort()).toEqual(['extra', 'gmail'])
  })

  it('keeps an 0600 config at 0600 across a reconcile', () => {
    // tmp + rename replaces the inode, so without an explicit mode the new file
    // takes the umask default and a 0600 config silently relaxes to 0644. The
    // mcpServers entries carry env blocks with credentials, and this reconcile
    // is the path that rewrites the file regularly.
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    chmodSync(isolatedDotClaude(), 0o600)
    expect(statSync(isolatedDotClaude()).mode & 0o777).toBe(0o600)

    writeShared({ gmail: { command: 'npx', args: ['gmail-mcp'] }, 'google-drive': { command: 'npx' } })
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')

    // The reconcile really did run (otherwise the mode assertion proves nothing).
    expect(Object.keys(servers()).sort()).toEqual(['gmail', 'google-drive'])
    expect(statSync(isolatedDotClaude()).mode & 0o777).toBe(0o600)
  })

  it('preserves a deliberately wider mode too, rather than forcing 0600', () => {
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    chmodSync(isolatedDotClaude(), 0o644)
    writeShared({ gmail: { command: 'npx', args: ['gmail-mcp'] }, extra: { command: 'x' } })
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect(Object.keys(servers()).sort()).toEqual(['extra', 'gmail'])
    expect(statSync(isolatedDotClaude()).mode & 0o777).toBe(0o644)
  })

  it('creates a brand new isolated config owner-only, not at the umask default', () => {
    // First provision: no target existed, so the fallback decides. It must be
    // 0600 rather than whatever the umask happens to be on the host.
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect(statSync(isolatedDotClaude()).mode & 0o777).toBe(0o600)
  })

  it('leaves no staging file behind (atomic write)', () => {
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    writeShared({ gmail: { command: 'npx', args: ['gmail-mcp'] }, extra: { command: 'x' } })
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')

    const stray = readdirSync(join(SANDBOX, 'agents', AGENT, '.claude-config'))
      .filter((f) => f.includes('.tmp-'))
    expect(stray).toEqual([])
  })
})

// CORTEXMCP904 (2026-09-08): the reconcile above is additive from the SHARED
// side only -- it never checked whether the agent's own project-scoped
// .mcp.json already defines a server under the same name. Claude Code
// resolves `local` scope (.claude.json, what this test suite writes) BEFORE
// `project` scope (.mcp.json), so copying a same-named shared entry into the
// isolated .claude.json would silently SHADOW the agent's own project-scoped
// definition -- credentials and all. Upstream measured a 12h outage from
// exactly this on two agents whose shared config happened to define a server
// name that collided with their own .mcp.json.
describe('isolated config dir: mcpServers reconcile does not shadow project-scoped (.mcp.json) servers', () => {
  it('does not copy a shared server whose name collides with the agent\'s own .mcp.json (reconcile path)', () => {
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect(Object.keys(servers())).toEqual(['gmail'])

    // The agent's own project defines `google-drive` itself (project scope).
    writeProjectMcp({ 'google-drive': { command: 'npx', args: ['agent-owns-this'] } })
    // The shared config ALSO gets a `google-drive` entry with a different
    // definition, plus a genuinely new, non-colliding server.
    writeShared({
      gmail: { command: 'npx', args: ['gmail-mcp'] },
      'google-drive': { command: 'npx', args: ['SHARED-would-shadow'] },
      slack: { command: 'npx', args: ['slack-mcp'] },
    })
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')

    // The colliding name is NOT copied into the isolated .claude.json at all
    // -- it stays absent there, so Claude Code's project-scope resolution
    // (which reads .mcp.json directly) is what actually answers for it.
    expect('google-drive' in servers()).toBe(false)
    // The unrelated gap-fill still works: a non-colliding new server arrives.
    expect(servers().slack).toEqual({ command: 'npx', args: ['slack-mcp'] })
    expect(Object.keys(servers()).sort()).toEqual(['gmail', 'slack'])
  })

  it('does not seed a shared server whose name collides with the agent\'s own .mcp.json (first-provision path)', () => {
    // .mcp.json already exists BEFORE the isolated dir is ever provisioned.
    writeProjectMcp({ gmail: { command: 'npx', args: ['agent-owns-this'] } })
    writeShared({
      gmail: { command: 'npx', args: ['SHARED-would-shadow'] },
      'google-drive': { command: 'npx', args: ['gdrive-mcp'] },
    })
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')

    expect('gmail' in servers()).toBe(false)
    expect(servers()['google-drive']).toEqual({ command: 'npx', args: ['gdrive-mcp'] })
    expect(Object.keys(servers())).toEqual(['google-drive'])
  })

  it('a missing/unparseable .mcp.json does not block the normal gap-fill', () => {
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    // No .mcp.json written at all (the common case -- most agents have none).
    writeShared({ gmail: { command: 'npx', args: ['gmail-mcp'] }, extra: { command: 'x' } })
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect(Object.keys(servers()).sort()).toEqual(['extra', 'gmail'])

    // An unparseable .mcp.json must not throw or block the reconcile either.
    writeFileSync(join(SANDBOX, 'agents', AGENT, '.mcp.json'), '{not valid json')
    writeShared({ gmail: { command: 'npx', args: ['gmail-mcp'] }, extra: { command: 'x' }, more: { command: 'y' } })
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect(Object.keys(servers()).sort()).toEqual(['extra', 'gmail', 'more'])
  })

  // MIGRATION (2026-09-08 Codex review): the two tests above only prove a
  // NEW collision is never copied in. They do NOT prove an agent that was
  // ALREADY shadowed (provisioned before this fix existed, or whose .mcp.json
  // was created/edited AFTER the collision was already copied in) gets fixed.
  // The whole point of the guard is that project scope should win for a name
  // it defines -- so an existing local-scope copy has to be actively removed
  // on the next reconcile, not just left in place because "it's not a NEW
  // add". This is the exact scenario Codex's review asked to be covered.
  it('MIGRATES an agent that was already shadowed before .mcp.json existed -- the stale local copy is removed on the next reconcile', () => {
    // Provision BEFORE the agent's own .mcp.json exists: `cortex` copies in
    // from the shared config normally (nothing to collide with yet).
    writeShared({ gmail: { command: 'npx', args: ['gmail-mcp'] }, cortex: { command: 'npx', args: ['SHARED-cortex'] } })
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect(servers().cortex).toEqual({ command: 'npx', args: ['SHARED-cortex'] })

    // NOW the agent gains its own project-scoped .mcp.json defining `cortex`
    // itself -- exactly as if the project was set up, or the file was added,
    // some time after the agent was already provisioned.
    writeProjectMcp({ cortex: { command: 'npx', args: ['agent-owns-this'] } })

    // Next reconcile (e.g. the agent restarts) must REMOVE the stale
    // shared-sourced local copy so Claude Code's project-scope resolution
    // (which reads .mcp.json directly) becomes the one that actually answers
    // for `cortex`, instead of the shadow persisting forever.
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect('cortex' in servers()).toBe(false)
    // Unrelated, non-colliding entries are untouched.
    expect(servers().gmail).toEqual({ command: 'npx', args: ['gmail-mcp'] })
    expect(Object.keys(servers())).toEqual(['gmail'])
  })

  // MIGRATION, round 2 (2026-09-08 Codex review): the removal-pass above
  // must NOT depend on the shared ~/.claude.json being readable -- removing
  // an EXISTING shadow only needs the isolated config + the agent's own
  // .mcp.json. An earlier version of this fix bailed out of the whole
  // function (via an early `if (!existsSync(sharedDot)) return false` /
  // parse-failure return) before the removal-pass ever ran, so an agent
  // stayed shadowed forever if reconcile happened to run while the shared
  // config was transiently missing or corrupt.
  it('MIGRATES an already-shadowed agent even when the shared ~/.claude.json is MISSING', () => {
    writeShared({ gmail: { command: 'npx', args: ['gmail-mcp'] }, cortex: { command: 'npx', args: ['SHARED-cortex'] } })
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect(servers().cortex).toBeDefined()

    writeProjectMcp({ cortex: { command: 'npx', args: ['agent-owns-this'] } })
    unlinkSync(sharedDotClaude()) // shared config gone entirely

    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect('cortex' in servers()).toBe(false)
    expect(servers().gmail).toEqual({ command: 'npx', args: ['gmail-mcp'] })
  })

  it('MIGRATES an already-shadowed agent even when the shared ~/.claude.json is CORRUPT/unparseable', () => {
    writeShared({ gmail: { command: 'npx', args: ['gmail-mcp'] }, cortex: { command: 'npx', args: ['SHARED-cortex'] } })
    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect(servers().cortex).toBeDefined()

    writeProjectMcp({ cortex: { command: 'npx', args: ['agent-owns-this'] } })
    writeFileSync(sharedDotClaude(), '{not valid json at all')

    ensureIsolatedChannelConfigDir(AGENT, 'telegram')
    expect('cortex' in servers()).toBe(false)
    expect(servers().gmail).toEqual({ command: 'npx', args: ['gmail-mcp'] })
  })
})

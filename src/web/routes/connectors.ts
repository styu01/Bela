import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { join, basename } from 'node:path'
import { homedir } from 'node:os'
import { execSync } from 'node:child_process'
import { PROJECT_ROOT, OLLAMA_URL, MAIN_AGENT_ID } from '../../config.js'
import { logger } from '../../logger.js'
import {
  slugify as slugifyMcp,
  catalogMatchesConfigured,
  type McpListEntry,
} from '../../mcp-list-parser.js'
import { atomicWriteFileSync } from '../atomic-write.js'
import { readFileOr, AGENTS_BASE_DIR, listAgentNames, agentConfigRoot, readAgentRemoteHost } from '../agent-config.js'
import { getMcpListCache, refreshMcpListCache, purgeFromMcpListCache } from '../mcp-list.js'
import { readBody, json } from '../http-helpers.js'
import { shellEscape } from '../sanitize.js'
import { getExternalProjectPaths, addExternalProjectPath, removeExternalProjectPath, getGitHubRepos, installGitHubRepo, removeGitHubRepo, updateGitHubRepo, detectRequiredEnvVars } from '../dashboard-settings.js'
import { listSecrets, setSecret, getSecret, deleteSecret } from '../vault.js'
import { guardProtectedVault } from '../vault-guard.js'
import { isProtectedVaultId, protectedVaultIdUsable } from '../vault-protected.js'
import {
  getBindings, addBinding, removeBinding, removeBindingsForSecret,
  syncSecret, syncAllBindings, scanMcpConfigs, unsyncBinding,
} from '../vault-bindings.js'
import type { RouteContext } from './types.js'

// The catalog is the union of the committed mcp-catalog.json (the MCPs the
// central devs ship) and an optional, gitignored mcp-catalog.local.json where
// a user keeps their own dev-only MCPs. This way a user's private MCP list
// never lands in git and other users don't inherit it. Entries from the local
// file override committed ones with the same id. A broken local file is
// non-fatal (logged + ignored) so it can't take down the whole catalog.
function localCatalogPath(): string {
  return join(PROJECT_ROOT, 'mcp-catalog.local.json')
}

function readLocalCatalog(): any[] {
  const localPath = localCatalogPath()
  if (!existsSync(localPath)) return []
  try {
    const parsed = JSON.parse(readFileSync(localPath, 'utf-8'))
    if (Array.isArray(parsed)) return parsed
    logger.warn({ localPath }, 'mcp-catalog.local.json is not a JSON array, ignoring')
  } catch (err) {
    logger.error({ err }, 'Failed to parse mcp-catalog.local.json, ignoring')
  }
  return []
}

function loadMcpCatalog(): any[] {
  const central = JSON.parse(readFileSync(join(PROJECT_ROOT, 'mcp-catalog.json'), 'utf-8')) as any[]
  const byId = new Map<string, any>()
  for (const item of central) byId.set(String(item.id), item)
  for (const item of readLocalCatalog()) byId.set(String(item.id), item)
  return [...byId.values()]
}

// Slugs of every MCP server declared in a .mcp.json / .claude.json the fleet
// can see. The mcp-list cache (`claude mcp list`) only reflects servers Claude
// Code has actually spawned this run, and a catalog id ("gmail") rarely equals
// the server name a user chose ("gmail-egov", "gmail-personal"). Collecting the
// configured names lets the catalog mark an entry installed by exact id or the
// "<id>-<variant>" naming convention, so a working, configured connector stops
// showing as "telepítésre vár".
function collectConfiguredServerSlugs(): Set<string> {
  const slugs = new Set<string>()
  const files = [
    join(PROJECT_ROOT, '.mcp.json'),
    join(homedir(), '.claude.json'),
  ]
  for (const agentName of listAgentNames()) {
    files.push(join(AGENTS_BASE_DIR, agentName, '.mcp.json'))
  }
  for (const extPath of getExternalProjectPaths()) {
    files.push(join(extPath, '.mcp.json'))
  }
  for (const f of files) {
    try {
      const parsed = JSON.parse(readFileOr(f, '{}'))
      for (const name of Object.keys(parsed.mcpServers || {})) {
        const s = slugifyMcp(name)
        if (s) slugs.add(s)
      }
    } catch { /* ignore unreadable / malformed config */ }
  }
  return slugs
}

// Persist a user-installed MCP into the gitignored local catalog so it shows up
// in the dashboard catalog as a user-local entry (and can be re-installed). Env
// values are stored blank -- only the variable names are kept, mirroring the
// committed catalog -- so secrets never land in this file. Upserts by id.
function upsertLocalCatalogEntry(entry: any): void {
  const local = readLocalCatalog()
  const idx = local.findIndex(e => String(e.id) === String(entry.id))
  if (idx >= 0) local[idx] = { ...local[idx], ...entry }
  else local.push(entry)
  atomicWriteFileSync(localCatalogPath(), JSON.stringify(local, null, 2) + '\n')
}

// Move install-time env secrets out of plaintext: store each in the Vault and
// bind it so the target config (~/.claude.json or .mcp.json) holds only a
// `vault:` ref plus the resolver wrapper -- never the raw value. This is what
// the install modal already promises ("titkosítva a Vault-ba kerülnek").
//
// MULTI-TARGET FIX (2026-09-11, Codex review, kanban 24152d84): addBinding()
// REPLACES the whole binding (targets array included) on a
// (vaultSecretId, envVar) key match -- calling this per-target in a loop, as
// the first cut of the catalog install did, meant installing to A then B
// left the binding pointing ONLY at B; A's `.mcp.json` still carried the
// vault-ref wrapper from its own sync, but the NEXT secret rotation/sync
// would only ever touch B, silently orphaning A. Fixed by reading the
// EXISTING binding first and writing back the UNION of its targets with the
// newly-requested ones (deduped by mcpFilePath+serverName), so installing to
// N targets (in one call or across several calls over time) always leaves
// all N in the binding.
//
// Fail-closed: on any vault/bind/sync error we throw. The server was already
// written to .mcp.json WITHOUT the secret, so it stands non-functional but
// leaks nothing; the caller surfaces the error and the user re-adds the
// secret from the Vault page.
export function vaultAndBindEnvSecretsMultiTarget(
  serverName: string,
  targets: { mcpFilePath: string; serverName: string }[],
  envSecrets: Record<string, string>,
): void {
  for (const [key, value] of Object.entries(envSecrets)) {
    if (!value) continue
    const vaultId = `${slugifyMcp(serverName)}-${key.toLowerCase()}`
    if (isProtectedVaultId(vaultId) && !protectedVaultIdUsable(vaultId, `mcp install ${serverName}`)) {
      throw new Error(`MCP env ${key}: its vault id "${vaultId}" is a protected entry; rename the server`)
    }
    setSecret(vaultId, `${key} (${serverName})`, value)
    const existing = getBindings().find(b => b.vaultSecretId === vaultId && b.envVar === key)
    const merged = [...(existing?.targets ?? [])]
    for (const t of targets) {
      if (!merged.some(m => m.mcpFilePath === t.mcpFilePath && m.serverName === t.serverName)) {
        merged.push(t)
      }
    }
    addBinding({ vaultSecretId: vaultId, envVar: key, targets: merged })
    const result = syncSecret(vaultId)
    if (result.errors.length) {
      throw new Error(
        `A(z) "${key}" titkot nem sikerult a configba kotni: ${result.errors.join('; ')}. ` +
        `A szerver telepitve van, de a kulcs NELKUL (nem mukodik, de nem is szivarog plaintextben). ` +
        `Add meg ujra a kulcsot a Vault oldalon.`,
      )
    }
  }
}

// Thin single-target wrapper -- kept so the existing POST /api/connectors
// (custom connector add) call site does not need to change its call shape.
export function vaultAndBindEnvSecrets(
  serverName: string,
  mcpFilePath: string,
  envSecrets: Record<string, string>,
): void {
  vaultAndBindEnvSecretsMultiTarget(serverName, [{ mcpFilePath, serverName }], envSecrets)
}

// Symmetric with the above: on uninstall, remove ONLY this (mcpFilePath,
// serverName) target from every binding that references it -- never the
// whole secret/binding, which may still be legitimately bound to OTHER
// targets or even other servers. A binding left with zero targets after
// this is deleted outright (nothing left to sync); Codex review (2026-09-11):
// this does not delete the underlying Vault secret value itself, matching
// "ne igenyelje mas agent secretjenek torleset" -- only a fully target-less
// binding record goes away, the secret stays in the Vault for manual reuse.
function removeVaultBindingTarget(mcpFilePath: string, serverName: string): void {
  for (const b of getBindings()) {
    const filtered = b.targets.filter(t => !(t.mcpFilePath === mcpFilePath && t.serverName === serverName))
    if (filtered.length === b.targets.length) continue
    if (filtered.length === 0) {
      removeBinding(b.vaultSecretId, b.envVar)
    } else {
      addBinding({ ...b, targets: filtered })
    }
  }
}

// GENSHIN911-CFGROOT (2026-09-11, kanban 24152d84). Resolve which .mcp.json a
// per-agent catalog install/uninstall should read/write -- main agent ->
// PROJECT_ROOT/.mcp.json, sub-agent -> agents/<name>/.mcp.json. Reuses the
// ALREADY-EXISTING agentConfigRoot() (src/web/agent-config.ts), which is the
// SAME resolver startAgentProcess uses to launch a sub-agent's own session --
// deliberately not a bespoke path, so "where does this agent's own config
// live" has exactly one answer across the codebase, not two that could drift.
//
// This is the fix for the actual bug (kanban 24152d84): the catalog install
// route used to run `claude mcp add` from the DASHBOARD SERVER's own process
// environment, which has no CLAUDE_CONFIG_DIR set (measured directly,
// 2026-09-11: /proc/<dashboard-pid>/environ) -- so every catalog install
// silently landed in the operator's shared ~/.claude.json, never in any
// agent's actual isolated config. Live-reproduced and immediately reverted
// with `env -i HOME=... PATH=... claude mcp add ...` mimicking the exact
// dashboard process environment: "File modified: /home/kisss/.claude.json".
export function targetMcpPath(agentName: string): string {
  return join(agentConfigRoot(agentName), '.mcp.json')
}

export type McpServerConfig =
  | { command: string; args?: string[]; env?: Record<string, string> }
  | { url: string; transport?: 'sse' | 'http' }

// Validated, side-effect-free: turns a catalog item + user-supplied secret
// env values into (a) the PLAIN, non-secret server config object that gets
// written into a target's .mcp.json, and (b) the secret values that must be
// vault-bound separately (never written in plaintext). Codex review
// (2026-09-11): replaces the removed `claude mcp add` CLI call's implicit
// validation with an explicit, narrow schema -- only the two catalog item
// shapes this function already supported (stdio/local, http|sse/remote) are
// accepted; anything else throws rather than writing a malformed entry.
export function buildCatalogServerConfig(
  item: { type?: string; command?: string; args?: unknown; env?: Record<string, unknown>; url?: string; transport?: string },
  envData: Record<string, string>,
): { config: McpServerConfig; secrets: Record<string, string> } {
  if (item.type === 'local') {
    if (typeof item.command !== 'string' || !item.command.trim()) {
      throw new Error('Catalog item is missing a valid command for a local (stdio) MCP server')
    }
    // Codex review (2026-09-11): a non-string arg used to be silently
    // DROPPED -- fail-closed instead, a malformed catalog/local-catalog
    // entry should be visibly rejected, not quietly install with missing
    // arguments the server may depend on.
    let args: string[] = []
    if (item.args !== undefined) {
      if (!Array.isArray(item.args) || item.args.some(a => typeof a !== 'string')) {
        throw new Error('Catalog item args must be an array of strings')
      }
      args = item.args as string[]
    }
    // Only env keys the catalog itself DECLARES (item.env) are accepted from
    // the caller -- Codex review: an install-time envData is not a general
    // "write anything into this config" channel, it exists to fill in
    // secrets the catalog entry already names. A caller-supplied key not in
    // item.env is silently dropped (not an error -- the modal only ever
    // sends keys it rendered inputs for, which come from item.env itself,
    // so a mismatch here means a stale/hand-crafted request, not a normal
    // user flow worth hard-failing on).
    const declaredKeys = new Set(Object.keys(item.env || {}))
    const userSecrets = Object.fromEntries(
      Object.entries(envData || {}).filter(
        ([k, v]) => declaredKeys.has(k) && typeof v === 'string' && v !== '',
      ),
    ) as Record<string, string>
    // Non-empty catalog defaults not overridden by the user stay as plain
    // config -- same split the removed CLI-based install used.
    const defaultEnv = Object.fromEntries(
      Object.entries(item.env || {}).filter(
        ([k, v]) => typeof v === 'string' && v !== '' && !(k in userSecrets),
      ),
    ) as Record<string, string>
    const config: McpServerConfig = { command: item.command, args }
    if (Object.keys(defaultEnv).length) (config as { env?: Record<string, string> }).env = defaultEnv
    return { config, secrets: userSecrets }
  }
  if (item.type === 'remote') {
    if (typeof item.url !== 'string') {
      throw new Error('Catalog item is missing a URL for a remote MCP server')
    }
    let parsedUrl: URL
    try {
      parsedUrl = new URL(item.url)
    } catch {
      throw new Error(`Catalog item has an invalid URL for a remote MCP server: ${item.url}`)
    }
    if (parsedUrl.protocol !== 'http:' && parsedUrl.protocol !== 'https:') {
      throw new Error(`Catalog item URL must be http(s) for a remote MCP server, got: ${parsedUrl.protocol}`)
    }
    // Codex review (2026-09-11): an unrecognized transport used to be
    // silently coerced to 'sse' -- fail-closed instead, so a typo'd or
    // future-transport catalog entry is visibly rejected rather than
    // silently installed with a transport the item never actually declared.
    if (item.transport !== undefined && item.transport !== 'sse' && item.transport !== 'http') {
      throw new Error(`Catalog item has an unsupported transport for a remote MCP server: ${item.transport}`)
    }
    const transport = item.transport === 'http' ? 'http' : 'sse'
    return { config: { url: item.url, transport }, secrets: {} }
  }
  throw new Error(`Unsupported catalog item type: ${String(item.type)}`)
}

// Fail-closed read: only a plain-object file whose mcpServers (if present) is
// also a plain object is accepted as a base to merge a new server entry
// into. Codex review (2026-09-11): a malformed/unexpected existing file must
// refuse rather than silently being overwritten -- this is a config file a
// human or another tool may also hand-edit.
function readMcpFileForMerge(mcpFilePath: string): { mcpServers: Record<string, unknown>; [k: string]: unknown } {
  if (!existsSync(mcpFilePath)) return { mcpServers: {} }
  let parsed: unknown
  try {
    parsed = JSON.parse(readFileSync(mcpFilePath, 'utf-8'))
  } catch (err) {
    throw new Error(`${mcpFilePath} is not valid JSON -- refusing to overwrite (${(err as Error).message})`)
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${mcpFilePath} does not contain a plain JSON object -- refusing to overwrite`)
  }
  const obj = parsed as { mcpServers?: unknown; [k: string]: unknown }
  if (obj.mcpServers !== undefined
      && (obj.mcpServers === null || typeof obj.mcpServers !== 'object' || Array.isArray(obj.mcpServers))) {
    throw new Error(`${mcpFilePath}'s mcpServers is not a plain object -- refusing to overwrite`)
  }
  return { ...obj, mcpServers: (obj.mcpServers as Record<string, unknown>) ?? {} }
}

// Codex review (2026-09-11, 2nd round): atomicWriteFileSync with no explicit
// mode writes the tmp file (then renamed in place) at the PROCESS UMASK
// default (typically 0644). A first fix here merely PRESERVED whatever mode
// the file already had -- but a `.mcp.json` that was already 0644 (it may
// carry other servers' vault-ref wrappers or hand-managed credentials) would
// then stay 0644 forever across every install/uninstall. syncSecret's own
// writes (vault-bindings.ts) always force 0600 for exactly this reason --
// this file matches that: EVERY catalog install/uninstall write hardens the
// mode to 0600, tightening a looser existing mode rather than preserving it.
const CATALOG_MCP_FILE_MODE = 0o600

export interface CatalogTargetResult {
  agent: string
  ok: boolean
  error?: string
}

// Direct, per-target .mcp.json write -- NOT `claude mcp add` CLI. Sidesteps
// the CLAUDE_CONFIG_DIR/subprocess-env-inheritance bug entirely: nothing
// shells out, so the dashboard server process's own environment is
// irrelevant. Same underlying write mechanism the ALREADY-WORKING
// POST /api/connectors/:name/assign route uses for per-agent .mcp.json,
// generalized with a real, validated config builder instead of copying an
// already-registered connector's config verbatim. One target's failure does
// not abort the others -- each gets its own result so a partial install is
// reported accurately, not silently swallowed as a full success or a full
// failure.
export function installCatalogItem(
  item: { id: string; type?: string; command?: string; args?: unknown; env?: Record<string, unknown>; url?: string; transport?: string },
  targetAgents: string[],
  envData: Record<string, string>,
): CatalogTargetResult[] {
  const { config, secrets } = buildCatalogServerConfig(item, envData)
  const serverName = item.id
  const results: CatalogTargetResult[] = []
  // Collected AFTER each target's own .mcp.json write succeeds -- the secret
  // binding below is done ONCE, as a union across every target that made it
  // this far (see vaultAndBindEnvSecretsMultiTarget's own comment for why a
  // per-target loop there was the actual bug).
  const writtenTargets: { agent: string; mcpFilePath: string }[] = []
  for (const agent of targetAgents) {
    try {
      const mcpFilePath = targetMcpPath(agent)
      const mcpConfig = readMcpFileForMerge(mcpFilePath)
      // Codex review (2026-09-11): no silent overwrite of an existing entry
      // under this exact server name for this target -- a caller that wants
      // to reconfigure an already-installed server needs an explicit
      // update/replace path (not built yet), not a plain re-POST that could
      // silently drop a hand-tuned config or a different secret binding.
      if (mcpConfig.mcpServers[serverName] !== undefined) {
        throw new Error(`'${serverName}' is already configured for '${agent}' -- remove it first, or this call would silently overwrite it`)
      }
      mcpConfig.mcpServers[serverName] = config
      atomicWriteFileSync(mcpFilePath, JSON.stringify(mcpConfig, null, 2), { mode: CATALOG_MCP_FILE_MODE })
      results.push({ agent, ok: true })
      writtenTargets.push({ agent, mcpFilePath })
    } catch (err: any) {
      results.push({ agent, ok: false, error: err.message || String(err) })
    }
  }
  if (Object.keys(secrets).length && writtenTargets.length) {
    // Codex review (2026-09-11, 3rd round): a rollback that only undoes THIS
    // call's own new targets is incomplete when this call's serverName+key
    // already had a WORKING binding on an existing target (the "+ install
    // for another agent" case -- same server, a second target, a re-typed
    // secret value). vaultAndBindEnvSecretsMultiTarget's setSecret() call
    // OVERWRITES the Vault entry for a given vaultId in place; if a LATER
    // key in the same call then fails sync, the earlier key's new value has
    // already silently replaced whatever an already-installed target was
    // relying on, and the union-write also already re-persisted the merged
    // binding (existing target's entry included) BEFORE the throw. Snapshot
    // every (vaultId, envVar) this call is about to touch -- value AND
    // binding shape -- so a failure can restore the exact pre-call state,
    // not just this call's own additions.
    const preCallSecrets = Object.keys(secrets).map(key => {
      const vaultId = `${slugifyMcp(serverName)}-${key.toLowerCase()}`
      return {
        vaultId,
        key,
        hadValue: getSecret(vaultId),
        hadBinding: getBindings().find(b => b.vaultSecretId === vaultId && b.envVar === key) ?? null,
      }
    })
    try {
      vaultAndBindEnvSecretsMultiTarget(
        serverName,
        writtenTargets.map(t => ({ mcpFilePath: t.mcpFilePath, serverName })),
        secrets,
      )
    } catch (err: any) {
      // Codex review (2026-09-11, 2nd round): the previous version left the
      // .mcp.json entry in place after a binding failure -- "non-functional
      // but present". That made the failure UNRECOVERABLE from the UI: the
      // item now reads as installed (installedAgents includes this target,
      // its checkbox is disabled), and a retry POST hits the "already
      // configured" guard above. Roll the write back instead: remove the
      // server entry this call just added from every affected target's
      // .mcp.json, and undo any binding-target additions this call made
      // (removeVaultBindingTarget is exact-match on (mcpFilePath,serverName)
      // and safe to call even for a target that never made it into a
      // binding). A subsequent identical install call then behaves exactly
      // as if this attempt never happened, and is retryable from the UI.
      const msg = `Secret binding failed, rolled back: ${err.message || err}`
      for (const t of writtenTargets) {
        try {
          const mcpConfig = readMcpFileForMerge(t.mcpFilePath)
          if (mcpConfig.mcpServers[serverName] !== undefined) {
            delete mcpConfig.mcpServers[serverName]
            atomicWriteFileSync(t.mcpFilePath, JSON.stringify(mcpConfig, null, 2), { mode: CATALOG_MCP_FILE_MODE })
          }
        } catch { /* best-effort rollback -- the failure is still surfaced via result.error either way */ }
        removeVaultBindingTarget(t.mcpFilePath, serverName)
      }
      // Restore each touched vaultId to its EXACT pre-call state (value and
      // binding), so an already-installed, previously-working target that
      // shares this serverName+key is not left silently pointed at a
      // different (or now-missing) secret. A re-sync afterward reconciles
      // any surviving target's file back to that restored shape (the
      // `vault:<id>` reference string itself never changes -- only the
      // decrypted value and/or the binding's target list might have -- but
      // re-syncing is cheap and closes the loop defensively).
      for (const snap of preCallSecrets) {
        try {
          if (snap.hadValue !== null) {
            setSecret(snap.vaultId, `${snap.key} (${serverName})`, snap.hadValue)
          } else {
            deleteSecret(snap.vaultId)
          }
          if (snap.hadBinding) {
            addBinding(snap.hadBinding)
          } else {
            removeBinding(snap.vaultId, snap.key)
          }
          if (snap.hadValue !== null && snap.hadBinding) {
            syncSecret(snap.vaultId)
          }
        } catch { /* best-effort restore -- the failure is still surfaced via result.error either way */ }
      }
      for (const r of results) {
        if (r.ok && writtenTargets.some(t => t.agent === r.agent)) {
          r.ok = false
          r.error = msg
        }
      }
    }
  }
  return results
}

// Symmetric with installCatalogItem -- targets the SAME per-agent .mcp.json
// files, not the global `claude mcp remove` CLI. Codex review (2026-09-11):
// a targeted install paired with a still-global uninstall would modify the
// WRONG config on removal, reproducing the exact bug class this whole fix
// exists to close. Missing file / missing entry is a no-op success (nothing
// to remove), not an error.
export function uninstallCatalogItem(itemId: string, targetAgents: string[]): CatalogTargetResult[] {
  const results: CatalogTargetResult[] = []
  for (const agent of targetAgents) {
    try {
      const mcpFilePath = targetMcpPath(agent)
      if (existsSync(mcpFilePath)) {
        const mcpConfig = readMcpFileForMerge(mcpFilePath)
        if (mcpConfig.mcpServers[itemId] !== undefined) {
          delete mcpConfig.mcpServers[itemId]
          atomicWriteFileSync(mcpFilePath, JSON.stringify(mcpConfig, null, 2), { mode: CATALOG_MCP_FILE_MODE })
        }
      }
      // Codex review (2026-09-11): the OLD global-CLI uninstall never
      // cleaned up Vault bindings either (pre-existing debt), but the new
      // per-target mechanism actively CREATES multi-target bindings, so
      // leaving this out here would actively manufacture new orphans on
      // every uninstall rather than just inheriting old debt. Removes ONLY
      // this (mcpFilePath, serverName) target from any binding that
      // references it -- never touches other targets/servers/the secret
      // value itself; see removeVaultBindingTarget's own comment.
      removeVaultBindingTarget(mcpFilePath, itemId)
      results.push({ agent, ok: true })
    } catch (err: any) {
      results.push({ agent, ok: false, error: err.message || String(err) })
    }
  }
  return results
}

// Validate a caller-supplied target agent list: REQUIRED (Codex review,
// 2026-09-11 -- "kötelező explicit célválasztás, nincs implicit all vagy
// main"), every entry must be a known local agent (main or a real
// agents/<name> directory), and remote-configured agents (readAgentRemoteHost
// truthy) are rejected outright -- a direct local .mcp.json write cannot
// configure an SSH-remote agent's session, so silently accepting one there
// would be a confident-looking no-op.
export function resolveCatalogTargets(requestedAgents: unknown): { targets: string[]; error?: string } {
  if (!Array.isArray(requestedAgents) || requestedAgents.length === 0) {
    return { targets: [], error: 'agents (non-empty array of local agent ids) is required -- no implicit default target' }
  }
  const known = new Set<string>([MAIN_AGENT_ID, ...listAgentNames()])
  const targets: string[] = []
  const seen = new Set<string>()
  for (const raw of requestedAgents) {
    if (typeof raw !== 'string' || !known.has(raw)) {
      return { targets: [], error: `Unknown agent: ${String(raw)}` }
    }
    if (raw !== MAIN_AGENT_ID && readAgentRemoteHost(raw)) {
      return { targets: [], error: `Agent '${raw}' runs on a remote host -- catalog install/uninstall only supports local agents (direct .mcp.json write)` }
    }
    if (seen.has(raw)) continue
    seen.add(raw)
    targets.push(raw)
  }
  return { targets }
}

export async function tryHandleConnectors(ctx: RouteContext): Promise<boolean> {
  const { req, res, path, method, url } = ctx

  // GET /api/connectors -- list every MCP server visible to Claude Code,
  // pulled from the local config files plus the cached `claude mcp list`
  // output. The CLI is not invoked here -- spawning every stdio / plugin
  // MCP for a health check would race the live Telegram bot.
  if (path === '/api/connectors' && method === 'GET') {
    type ConnectorEntry = {
      name: string
      status: string
      endpoint: string
      type: string
      source: 'plugin' | 'local-user' | 'local-project' | 'local' | 'claude.ai' | 'agent' | 'agent-project' | 'external-project'
      scope: string
    }
    const connectors: ConnectorEntry[] = []
    const globalSeen = new Set<string>()

    try {
      const settings = JSON.parse(readFileOr(join(homedir(), '.claude', 'settings.json'), '{}'))
      for (const pluginKey of Object.keys(settings.enabledPlugins || {})) {
        if (!settings.enabledPlugins[pluginKey]) continue
        const name = `plugin:${pluginKey.split('@')[0].toLowerCase()}`
        if (globalSeen.has(name)) continue
        globalSeen.add(name)
        connectors.push({ name, status: 'configured', endpoint: pluginKey, type: 'plugin', source: 'plugin', scope: 'plugin' })
      }
    } catch { /* ignore */ }

    const fileSources: Array<[string, 'local-project' | 'local-user', string]> = [
      [join(PROJECT_ROOT, '.mcp.json'), 'local-project', 'global'],
      [join(homedir(), '.claude.json'), 'local-user', 'global'],
    ]
    for (const [src, source, scope] of fileSources) {
      try {
        const parsed = JSON.parse(readFileOr(src, '{}'))
        const servers = parsed.mcpServers || {}
        for (const [name, cfg] of Object.entries(servers) as Array<[string, any]>) {
          if (globalSeen.has(name)) continue
          globalSeen.add(name)
          const endpoint = cfg?.url || cfg?.command || ''
          const type = cfg?.url ? 'remote' : 'local'
          connectors.push({ name, status: 'configured', endpoint: String(endpoint), type, source, scope })
        }
      } catch { /* ignore */ }
    }

    for (const entry of getMcpListCache().entries) {
      const key = entry.source === 'plugin' ? `plugin:${entry.normalizedId}` : entry.name
      if (globalSeen.has(key)) continue
      globalSeen.add(key)
      connectors.push({
        name: entry.name,
        status: entry.status === 'unknown' ? 'configured' : entry.status,
        endpoint: entry.endpoint,
        type: entry.source === 'claude.ai' ? 'remote' : 'local',
        source: entry.source === 'plugin' ? 'plugin'
               : entry.source === 'claude.ai' ? 'claude.ai'
               : 'local',
        scope: 'global',
      })
    }

    for (const agentName of listAgentNames()) {
      const agentMcpPath = join(AGENTS_BASE_DIR, agentName, '.mcp.json')
      try {
        const parsed = JSON.parse(readFileOr(agentMcpPath, '{}'))
        const servers = parsed.mcpServers || {}
        for (const [name, cfg] of Object.entries(servers) as Array<[string, any]>) {
          const endpoint = cfg?.url || cfg?.command || ''
          const type = cfg?.url ? 'remote' : 'local'
          connectors.push({ name, status: 'configured', endpoint: String(endpoint), type, source: 'agent', scope: `agent:${agentName}` })
        }
      } catch { /* ignore */ }

      const projectsDir = join(AGENTS_BASE_DIR, agentName, 'projects')
      if (existsSync(projectsDir)) {
        try {
          for (const proj of readdirSync(projectsDir)) {
            if (!statSync(join(projectsDir, proj)).isDirectory()) continue
            const projMcpPath = join(projectsDir, proj, '.mcp.json')
            try {
              const parsed = JSON.parse(readFileOr(projMcpPath, '{}'))
              const servers = parsed.mcpServers || {}
              for (const [name, cfg] of Object.entries(servers) as Array<[string, any]>) {
                const endpoint = cfg?.url || cfg?.command || ''
                const type = cfg?.url ? 'remote' : 'local'
                connectors.push({ name, status: 'configured', endpoint: String(endpoint), type, source: 'agent-project', scope: `project:${agentName}/${proj}` })
              }
            } catch { /* ignore */ }
          }
        } catch { /* ignore */ }
      }
    }

    for (const extPath of getExternalProjectPaths()) {
      try {
        const parsed = JSON.parse(readFileOr(join(extPath, '.mcp.json'), '{}'))
        const servers = parsed.mcpServers || {}
        const projName = basename(extPath)
        for (const [name, cfg] of Object.entries(servers) as Array<[string, any]>) {
          const endpoint = cfg?.url || cfg?.command || ''
          const type = cfg?.url ? 'remote' : 'local'
          connectors.push({ name, status: 'configured', endpoint: String(endpoint), type, source: 'external-project', scope: `project:external/${projName}` })
        }
      } catch { /* ignore */ }
    }

    json(res, connectors)
    return true
  }

  if (path === '/api/connectors/status' && method === 'GET') {
    const cache = getMcpListCache()
    json(res, {
      cacheLastRefreshed: cache.lastRefreshed,
      cacheError: cache.error,
      refreshing: cache.refreshing,
    })
    return true
  }

  if (path === '/api/connectors/refresh' && method === 'POST') {
    const cache = await refreshMcpListCache()
    const httpStatus = cache.error ? 502 : 200
    json(res, {
      ok: !cache.error,
      count: cache.entries.length,
      lastRefreshed: cache.lastRefreshed,
      error: cache.error,
    }, httpStatus)
    return true
  }

  if (path === '/api/connectors/external-paths' && method === 'GET') {
    json(res, { paths: getExternalProjectPaths() })
    return true
  }

  if (path === '/api/connectors/external-paths' && method === 'POST') {
    const body = await readBody(req)
    const { path: p } = JSON.parse(body.toString()) as { path: string }
    const result = addExternalProjectPath(p)
    if (result.error) { json(res, { error: result.error }, 400); return true }
    json(res, { ok: true, paths: result.paths })
    return true
  }

  if (path === '/api/connectors/external-paths' && method === 'DELETE') {
    const body = await readBody(req)
    const { path: p } = JSON.parse(body.toString()) as { path: string }
    const paths = removeExternalProjectPath(p)
    json(res, { ok: true, paths })
    return true
  }

  if (path === '/api/connectors/github-repos' && method === 'GET') {
    json(res, { repos: getGitHubRepos() })
    return true
  }

  if (path === '/api/connectors/github-repos' && method === 'POST') {
    const body = await readBody(req)
    const { url, env } = JSON.parse(body.toString()) as { url: string, env?: Record<string, string> }
    if (!url?.trim()) { json(res, { error: 'URL is required' }, 400); return true }

    const envVarMapping: Record<string, string> = {}
    if (env) {
      for (const [key, value] of Object.entries(env)) {
        const vaultId = `github-env-${key.toLowerCase()}-${Date.now()}`
        setSecret(vaultId, `${key} (GitHub repo)`, value)
        envVarMapping[key] = vaultId
      }
    }

    const result = await installGitHubRepo(url.trim(), Object.keys(envVarMapping).length > 0 ? envVarMapping : undefined)
    if (result.error) { json(res, { error: result.error }, 400); return true }
    json(res, { ok: true, repo: result.repo, requiredEnvVars: result.requiredEnvVars })
    return true
  }

  const githubRepoMatch = path.match(/^\/api\/connectors\/github-repos\/([^/]+)$/)
  if (githubRepoMatch && method === 'DELETE') {
    const name = decodeURIComponent(githubRepoMatch[1])
    const result = removeGitHubRepo(name)
    if (result.error) { json(res, { error: result.error }, 404); return true }
    json(res, { ok: true })
    return true
  }

  if (githubRepoMatch && method === 'PATCH') {
    const name = decodeURIComponent(githubRepoMatch[1])
    const result = updateGitHubRepo(name)
    if (result.error) { json(res, { error: result.error }, 400); return true }
    json(res, { ok: true })
    return true
  }

  const connectorDetailMatch = path.match(/^\/api\/connectors\/(.+)$/)
  if (connectorDetailMatch && method === 'GET' && !path.includes('/assign')) {
    const name = decodeURIComponent(connectorDetailMatch[1])
    if (name.startsWith('plugin:')) {
      try {
        const settings = JSON.parse(readFileOr(join(homedir(), '.claude', 'settings.json'), '{}'))
        const rawSuffix = name.slice('plugin:'.length)
        const segments = rawSuffix.split(':')
        const plain = (segments[segments.length - 1] || rawSuffix).toLowerCase()
        const enabled = settings.enabledPlugins || {}
        const match = Object.keys(enabled).find(
          k => enabled[k] && k.split('@')[0].toLowerCase() === plain,
        )
        if (!match) { json(res, { error: 'Connector not found' }, 404); return true }
        json(res, { name, scope: 'user', status: 'configured', type: 'plugin', command: match, args: '', env: {} })
        return true
      } catch {
        json(res, { error: 'Connector not found' }, 404)
        return true
      }
    }
    const searchPaths: Array<[string, string]> = [
      [join(PROJECT_ROOT, '.mcp.json'), 'project'],
      [join(homedir(), '.claude.json'), 'user'],
    ]
    for (const agentName of listAgentNames()) {
      searchPaths.push([join(AGENTS_BASE_DIR, agentName, '.mcp.json'), `agent:${agentName}`])
      const projectsDir = join(AGENTS_BASE_DIR, agentName, 'projects')
      if (existsSync(projectsDir)) {
        try {
          for (const proj of readdirSync(projectsDir)) {
            if (!statSync(join(projectsDir, proj)).isDirectory()) continue
            searchPaths.push([join(projectsDir, proj, '.mcp.json'), `project:${agentName}/${proj}`])
          }
        } catch { /* ignore */ }
      }
    }
    for (const extPath of getExternalProjectPaths()) {
      searchPaths.push([join(extPath, '.mcp.json'), `project:external/${basename(extPath)}`])
    }
    for (const [src, scope] of searchPaths) {
      try {
        const parsed = JSON.parse(readFileOr(src, '{}'))
        const cfg = (parsed.mcpServers || {})[name]
        if (!cfg) continue
        const type = cfg.url ? 'remote' : 'local'
        const env: Record<string, string> = {}
        for (const k of Object.keys(cfg.env || {})) env[k] = '***'
        json(res, {
          name,
          scope,
          status: 'configured',
          type,
          command: cfg.command || cfg.url || '',
          args: Array.isArray(cfg.args) ? cfg.args.join(' ') : '',
          env,
        })
        return true
      } catch { /* fall through */ }
    }
    json(res, { error: 'Connector not found' }, 404)
    return true
  }

  if (path === '/api/connectors' && method === 'POST') {
    const body = await readBody(req)
    const data = JSON.parse(body.toString()) as {
      name: string
      type: 'stdio' | 'http' | 'sse'
      url?: string
      command?: string
      args?: string
      scope?: string
      env?: Record<string, string>
    }

    if (!data.name?.trim()) { json(res, { error: 'Name is required' }, 400); return true }

    const rawName = data.name.trim()
    const sanitizedName = rawName.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '')
    if (!sanitizedName) {
      json(res, { error: 'Name must contain at least one letter, number, hyphen, or underscore' }, 400)
      return true
    }
    const nameChanged = sanitizedName !== rawName

    try {
      const scopeFlag = data.scope === 'project' ? '-s project' : '-s user'

      const catalogEntry: any = {
        id: sanitizedName,
        name: rawName,
        description: 'Felhasználó által telepített MCP',
        category: 'custom',
        icon: '🔌',
        authType: data.env && Object.keys(data.env).length ? 'apikey' : 'none',
      }

      if ((data.type === 'http' || data.type === 'sse') && data.url) {
        const transport = data.type === 'sse' ? 'sse' : 'http'
        execSync(`claude mcp add --transport ${transport} ${scopeFlag} ${shellEscape(sanitizedName)} ${shellEscape(data.url)} 2>&1`, { timeout: 15000, encoding: 'utf-8' })
        catalogEntry.type = 'remote'
        catalogEntry.url = data.url
        catalogEntry.transport = transport
      } else if (data.type === 'stdio' && data.command) {
        // User-typed env values are secrets: register the server WITHOUT -e, then
        // vault + bind below so the config holds only vault refs, never plaintext.
        const userSecrets = Object.fromEntries(
          Object.entries(data.env || {}).filter(([, v]) => v !== ''),
        ) as Record<string, string>
        const argsStr = data.args ? data.args.split(/\s+/).filter(Boolean).map(a => shellEscape(a)).join(' ') : ''
        execSync(`claude mcp add ${scopeFlag} ${shellEscape(sanitizedName)} -- ${shellEscape(data.command)} ${argsStr} 2>&1`, { timeout: 15000, encoding: 'utf-8' })
        catalogEntry.type = 'local'
        catalogEntry.command = data.command
        catalogEntry.args = data.args ? data.args.split(/\s+/).filter(Boolean) : []
        // Store only env var names with blank values -- never the secrets.
        catalogEntry.env = Object.fromEntries(Object.keys(data.env || {}).map(k => [k, '']))
        if (Object.keys(userSecrets).length) {
          const mcpFile = data.scope === 'project' ? join(PROJECT_ROOT, '.mcp.json') : join(homedir(), '.claude.json')
          vaultAndBindEnvSecrets(sanitizedName, mcpFile, userSecrets)
        }
      } else {
        json(res, { error: 'URL (http/sse) or command (stdio) required' }, 400)
        return true
      }

      try {
        upsertLocalCatalogEntry(catalogEntry)
      } catch (err) {
        // The MCP is already installed via `claude mcp add`; a catalog-write
        // failure shouldn't fail the request -- just log and move on.
        logger.error({ err }, 'Failed to persist MCP into mcp-catalog.local.json')
      }

      json(res, { ok: true, name: sanitizedName, nameChanged })
    } catch (err: any) {
      json(res, { error: err.message || 'Failed to add connector' }, 500)
    }
    return true
  }

  if (connectorDetailMatch && method === 'DELETE' && !path.includes('/assign')) {
    const name = decodeURIComponent(connectorDetailMatch[1])
    let removed = 0
    const mcpFiles = [
      join(PROJECT_ROOT, '.mcp.json'),
      join(homedir(), '.claude.json'),
    ]
    for (const agentName of listAgentNames()) {
      mcpFiles.push(join(AGENTS_BASE_DIR, agentName, '.mcp.json'))
      const projectsDir = join(AGENTS_BASE_DIR, agentName, 'projects')
      if (existsSync(projectsDir)) {
        try {
          for (const proj of readdirSync(projectsDir)) {
            if (statSync(join(projectsDir, proj)).isDirectory()) {
              mcpFiles.push(join(projectsDir, proj, '.mcp.json'))
            }
          }
        } catch { /* ignore */ }
      }
    }
    for (const extPath of getExternalProjectPaths()) {
      mcpFiles.push(join(extPath, '.mcp.json'))
    }
    for (const mcpPath of mcpFiles) {
      try {
        const parsed = JSON.parse(readFileOr(mcpPath, '{}'))
        if (parsed.mcpServers && parsed.mcpServers[name]) {
          delete parsed.mcpServers[name]
          atomicWriteFileSync(mcpPath, JSON.stringify(parsed, null, 2))
          removed++
        }
      } catch { /* skip unreadable files */ }
    }
    if (removed > 0) {
      purgeFromMcpListCache(name)
      json(res, { ok: true, removed })
    } else if (purgeFromMcpListCache(name)) {
      json(res, { ok: true, removed: 0, purgedFromCache: true })
    } else {
      json(res, { error: 'Connector not found in any config' }, 404)
    }
    return true
  }

  const connectorAssignMatch = path.match(/^\/api\/connectors\/(.+)\/assign$/)
  if (connectorAssignMatch && method === 'POST') {
    const connectorName = decodeURIComponent(connectorAssignMatch[1])
    const body = await readBody(req)
    const { agents: rawTargetAgents, allAgents: rawVisibleAgents } = JSON.parse(body.toString()) as { agents: string[], allAgents?: string[] }

    // Only ever touch .mcp.json of real, known agents -- a caller-supplied name
    // like "../../../../tmp/evil" must never reach join()+atomicWriteFileSync.
    const knownAgents = new Set(listAgentNames())
    const targetAgents = (Array.isArray(rawTargetAgents) ? rawTargetAgents : []).filter(a => knownAgents.has(a))
    const visibleAgents = Array.isArray(rawVisibleAgents) ? rawVisibleAgents.filter(a => knownAgents.has(a)) : undefined

    if (connectorName.startsWith('plugin:')) {
      json(res, { ok: true, note: 'plugin:* connectors are global to every agent -- nothing to assign.' })
      return true
    }

    let connectorConfig: any = null
    const configSources = [
      join(PROJECT_ROOT, '.mcp.json'),
      join(homedir(), '.claude.json'),
    ]
    for (const agentName of listAgentNames()) {
      configSources.push(join(AGENTS_BASE_DIR, agentName, '.mcp.json'))
      const projectsDir = join(AGENTS_BASE_DIR, agentName, 'projects')
      if (existsSync(projectsDir)) {
        try {
          for (const proj of readdirSync(projectsDir)) {
            if (statSync(join(projectsDir, proj)).isDirectory()) {
              configSources.push(join(projectsDir, proj, '.mcp.json'))
            }
          }
        } catch { /* ignore */ }
      }
    }
    for (const extPath of getExternalProjectPaths()) {
      configSources.push(join(extPath, '.mcp.json'))
    }
    for (const src of configSources) {
      try {
        const parsed = JSON.parse(readFileOr(src, '{}'))
        if (parsed.mcpServers && parsed.mcpServers[connectorName]) {
          connectorConfig = parsed.mcpServers[connectorName]
          break
        }
      } catch { /* fall through */ }
    }
    if (!connectorConfig) { json(res, { error: 'Connector not found' }, 404); return true }

    const targetSet = new Set(targetAgents)
    for (const agentName of targetAgents) {
      const mcpPath = join(AGENTS_BASE_DIR, agentName, '.mcp.json')
      let mcpConfig: any = {}
      try { mcpConfig = JSON.parse(readFileOr(mcpPath, '{}')) } catch {}
      if (!mcpConfig.mcpServers) mcpConfig.mcpServers = {}
      mcpConfig.mcpServers[connectorName] = connectorConfig
      atomicWriteFileSync(mcpPath, JSON.stringify(mcpConfig, null, 2))
    }

    if (visibleAgents) {
      for (const agentName of visibleAgents) {
        if (targetSet.has(agentName)) continue
        const mcpPath = join(AGENTS_BASE_DIR, agentName, '.mcp.json')
        try {
          const mcpConfig = JSON.parse(readFileOr(mcpPath, '{}'))
          if (mcpConfig.mcpServers && mcpConfig.mcpServers[connectorName]) {
            delete mcpConfig.mcpServers[connectorName]
            atomicWriteFileSync(mcpPath, JSON.stringify(mcpConfig, null, 2))
          }
        } catch { /* skip */ }
      }
    }

    json(res, { ok: true })
    return true
  }

  // === MCP Catalog ===
  if (path === '/api/mcp-catalog' && method === 'GET') {
    try {
      const catalog = loadMcpCatalog()

      const installedSource = new Map<string, McpListEntry['source']>()
      for (const entry of getMcpListCache().entries) {
        if (!installedSource.has(entry.normalizedId)) {
          installedSource.set(entry.normalizedId, entry.source)
        }
      }
      // Servers configured in .mcp.json files count as installed too, even when
      // the mcp-list cache misses them or names them differently from the
      // catalog id (e.g. "gmail-egov" / "gmail-personal" for catalog id "gmail").
      const configuredSlugs = collectConfiguredServerSlugs()

      // installedAgents (2026-09-11, kanban 24152d84): per-agent state for the
      // NEW direct-.mcp.json install path -- distinct from `installed` above,
      // which reflects the OLD global CLI-scoped mechanism (mcp-list cache +
      // PROJECT_ROOT/homedir .mcp.json). An item can be installedAgents=[] and
      // still installed=true (e.g. only ever added globally via the old path,
      // or by hand) -- the frontend uses this to pre-check/label which SPECIFIC
      // agents already have it via the targeted mechanism, not to replace the
      // broader `installed` flag.
      const localAgentIds = [MAIN_AGENT_ID, ...listAgentNames()]

      const result = catalog.map(item => {
        const itemId = slugifyMcp(String(item.id ?? ''))
        const itemNameSlug = slugifyMcp(String(item.name ?? ''))
        let source = installedSource.get(itemId) || installedSource.get(itemNameSlug)
        // configMatch flags entries detected only via .mcp.json. They are
        // installed under a custom server name, so the catalog's generic-id
        // uninstall (`claude mcp remove <id>`) would not target them -- the
        // frontend hides the uninstall link and points at the Connectors list.
        let configMatch = false
        if (source === undefined && catalogMatchesConfigured(itemId, itemNameSlug, configuredSlugs)) {
          source = 'local'
          configMatch = true
        }
        const installedAgents = localAgentIds.filter(agent => {
          try {
            const parsed = JSON.parse(readFileOr(targetMcpPath(agent), '{}'))
            return Boolean(parsed?.mcpServers?.[String(item.id ?? '')])
          } catch { return false }
        })
        return {
          ...item,
          installed: source !== undefined,
          installedSource: source,
          configMatch,
          installedAgents,
        }
      })

      json(res, result)
    } catch (err) {
      logger.error({ err }, 'Failed to load MCP catalog')
      json(res, { error: 'Failed to load catalog' }, 500)
    }
    return true
  }

  const catalogInstallMatch = path.match(/^\/api\/mcp-catalog\/([^/]+)\/install$/)
  if (catalogInstallMatch && method === 'POST') {
    const id = decodeURIComponent(catalogInstallMatch[1])
    try {
      const catalog = loadMcpCatalog()
      const item = catalog.find(c => c.id === id)
      if (!item) { json(res, { error: 'Item not found in catalog' }, 404); return true }

      const body = await readBody(req)
      let envData: Record<string, string> = {}
      let requestedAgents: unknown
      try {
        const parsed = JSON.parse(body.toString())
        if (parsed.env) envData = parsed.env
        requestedAgents = parsed.agents
      } catch { /* no body or invalid json -- requestedAgents stays undefined, caught below */ }

      // 24152d84 fix (2026-09-11): no more `claude mcp add` CLI (dashboard's
      // own process env, no CLAUDE_CONFIG_DIR -- see targetMcpPath's comment)
      // and no more implicit target (Codex review: explicit selection only).
      const { targets, error: targetError } = resolveCatalogTargets(requestedAgents)
      if (targetError) { json(res, { error: targetError }, 400); return true }

      const results = installCatalogItem(item, targets, envData)
      const failed = results.filter(r => !r.ok)

      let message = 'Telepítve'
      if (item.authType === 'oauth' && item.authNote) {
        message = `Telepítve. ${item.authNote}`
      }
      if (failed.length) {
        message += ` (sikertelen: ${failed.map(f => `${f.agent} -- ${f.error}`).join('; ')})`
      }

      if (failed.length === results.length) {
        json(res, { error: message, results }, 500)
        return true
      }
      json(res, { ok: true, message, results })
    } catch (err: any) {
      logger.error({ err }, 'Failed to install MCP from catalog')
      json(res, { error: err.message || 'Failed to install' }, 500)
    }
    return true
  }

  const catalogUninstallMatch = path.match(/^\/api\/mcp-catalog\/([^/]+)\/uninstall$/)
  if (catalogUninstallMatch && method === 'DELETE') {
    const id = decodeURIComponent(catalogUninstallMatch[1])
    try {
      const catalog = loadMcpCatalog()
      const item = catalog.find(c => c.id === id)
      if (!item) { json(res, { error: 'Item not found in catalog' }, 404); return true }

      // 24152d84 fix (2026-09-11): symmetric with installCatalogItem -- targets
      // the SAME per-agent .mcp.json files the (now-removed) global
      // `claude mcp remove -s user/project` CLI never touched. A comma-separated
      // query param (not a DELETE body -- proxy/client body support for DELETE
      // is inconsistent) of agent ids to remove from; explicit, same "no
      // implicit default" rule as install.
      const agentsParam = url.searchParams.get('agents') || ''
      const requestedAgents = agentsParam.split(',').map(s => s.trim()).filter(Boolean)
      const { targets, error: targetError } = resolveCatalogTargets(requestedAgents)
      if (targetError) { json(res, { error: targetError }, 400); return true }

      const results = uninstallCatalogItem(id, targets)
      const failed = results.filter(r => !r.ok)
      let message = 'Eltávolítva'
      if (failed.length) {
        message += ` (sikertelen: ${failed.map(f => `${f.agent} -- ${f.error}`).join('; ')})`
      }
      if (failed.length === results.length && results.length > 0) {
        json(res, { error: message, results }, 500)
        return true
      }
      json(res, { ok: true, message, results })
    } catch (err: any) {
      logger.error({ err }, 'Failed to uninstall MCP from catalog')
      json(res, { error: err.message || 'Failed to uninstall' }, 500)
    }
    return true
  }

  // === Vault ===
  if (path === '/api/vault' && method === 'GET') {
    // ssh-key-* entries are managed exclusively via the SSH key pool
    // (/api/vault/ssh-keys) and must not appear as generic secret cards too.
    json(res, { secrets: listSecrets().filter(s => !s.id.startsWith('ssh-key-')) })
    return true
  }

  if (path === '/api/vault' && method === 'POST') {
    const body = await readBody(req)
    const { id, label, value } = JSON.parse(body.toString()) as { id: string, label: string, value: string }
    if (!id?.trim() || !value) { json(res, { error: 'id and value required' }, 400); return true }
    // VAULTMAINONLY: overwriting a protected entry would break its mailer silently (integrity): main / owner only
    if (!guardProtectedVault(ctx, [id], 'write')) return true
    setSecret(id.trim(), label || id.trim(), value)
    const syncResult = syncSecret(id.trim())
    json(res, { ok: true, synced: syncResult.updated })
    return true
  }

  const vaultMatch = path.match(/^\/api\/vault\/([^/]+)$/)
  // 'ssh-servers' is the dedicated SSH-server sub-resource (routes/vault-ssh.ts),
  // handled later in the web.ts chain -- without this exclusion, a bare GET
  // /api/vault/ssh-servers matches THIS generic secret-store route first
  // (id="ssh-servers"), finds no such secret, and returns 404 before
  // tryHandleVaultSsh ever runs (2026-07-01, frontend/backend Vault epic).
  const isVaultSubroute = vaultMatch && ['bindings', 'sync', 'scan', 'import', 'ssh-servers', 'ssh-keys'].includes(vaultMatch[1])
  if (vaultMatch && !isVaultSubroute && method === 'GET') {
    const id = decodeURIComponent(vaultMatch[1])
    // VAULTMAINONLY (KISPROGI 6431, BELA 6432): a protected entry's value only to the main agent or the owner's session
    if (!guardProtectedVault(ctx, [id], 'read')) return true
    const val = getSecret(id)
    if (val === null) { json(res, { error: 'Not found' }, 404); return true }
    json(res, { id, value: val })
    return true
  }

  if (vaultMatch && !isVaultSubroute && method === 'DELETE') {
    const id = decodeURIComponent(vaultMatch[1])
    if (!guardProtectedVault(ctx, [id], 'delete')) return true
    if (!deleteSecret(id)) { json(res, { error: 'Not found' }, 404); return true }
    removeBindingsForSecret(id)
    json(res, { ok: true })
    return true
  }

  // === Vault Bindings ===
  if (path === '/api/vault/bindings' && method === 'GET') {
    json(res, { bindings: getBindings() })
    return true
  }

  if (path === '/api/vault/bindings' && method === 'POST') {
    const body = await readBody(req)
    const data = JSON.parse(body.toString()) as {
      vaultSecretId: string
      envVar?: string
      serverName?: string
      targets?: Array<{ mcpFilePath: string, serverName: string }>
      // Remote-server header binding: the secret is injected into the request
      // header (e.g. Authorization) via headersHelper instead of an env var.
      headerName?: string
      headerScheme?: string
    }
    // A binding is either env-var based (local/stdio servers) or header based
    // (remote http/sse servers). Header bindings key on the header name.
    const bindingKey = data.headerName?.trim() || data.envVar?.trim()
    if (!data.vaultSecretId || !bindingKey) {
      json(res, { error: 'vaultSecretId and (envVar or headerName) required' }, 400)
      return true
    }
    // a binding writes the value into an MCP config file anyone can read: never for a protected entry
    if (!guardProtectedVault(ctx, [data.vaultSecretId], 'binding', { never: true })) return true

    let targets = data.targets || []
    if (data.serverName && targets.length === 0) {
      const searchPaths: Array<[string, string]> = [
        [join(PROJECT_ROOT, '.mcp.json'), 'project'],
        [join(homedir(), '.claude.json'), 'user'],
      ]
      for (const agentName of listAgentNames()) {
        searchPaths.push([join(AGENTS_BASE_DIR, agentName, '.mcp.json'), `agent:${agentName}`])
        const projectsDir = join(AGENTS_BASE_DIR, agentName, 'projects')
        if (existsSync(projectsDir)) {
          try {
            for (const proj of readdirSync(projectsDir)) {
              if (!statSync(join(projectsDir, proj)).isDirectory()) continue
              searchPaths.push([join(projectsDir, proj, '.mcp.json'), `project:${agentName}/${proj}`])
            }
          } catch { /* ignore */ }
        }
      }
      for (const extPath of getExternalProjectPaths()) {
        searchPaths.push([join(extPath, '.mcp.json'), `project:external/${basename(extPath)}`])
      }
      for (const [src] of searchPaths) {
        try {
          const parsed = JSON.parse(readFileOr(src, '{}'))
          if (parsed.mcpServers?.[data.serverName]) {
            targets.push({ mcpFilePath: src, serverName: data.serverName })
          }
        } catch { /* skip */ }
      }
    }

    if (targets.length === 0) {
      json(res, { error: 'No targets found for this server' }, 400)
      return true
    }
    const binding: any = { vaultSecretId: data.vaultSecretId, envVar: bindingKey, targets }
    if (data.headerName?.trim()) {
      binding.headerName = data.headerName.trim()
      binding.headerScheme = data.headerScheme?.trim() ?? 'Bearer'
    }
    addBinding(binding)
    const syncResult = syncSecret(data.vaultSecretId)
    json(res, { ok: true, synced: syncResult.updated, errors: syncResult.errors })
    return true
  }

  const bindingDeleteMatch = path.match(/^\/api\/vault\/bindings\/([^/]+)\/([^/]+)$/)
  if (bindingDeleteMatch && method === 'DELETE') {
    const secretId = decodeURIComponent(bindingDeleteMatch[1])
    const envVar = decodeURIComponent(bindingDeleteMatch[2])
    unsyncBinding(secretId, envVar)
    if (!removeBinding(secretId, envVar)) { json(res, { error: 'Binding not found' }, 404); return true }
    json(res, { ok: true })
    return true
  }

  if (path === '/api/vault/sync' && method === 'POST') {
    const result = syncAllBindings()
    json(res, { ok: true, ...result })
    return true
  }

  // === Vault Scan & Import ===
  if (path === '/api/vault/scan' && method === 'GET') {
    json(res, { findings: scanMcpConfigs() })
    return true
  }

  if (path === '/api/vault/import' && method === 'POST') {
    const body = await readBody(req)
    const { imports: importRequests } = JSON.parse(body.toString()) as {
      imports: Array<{
        serverName: string
        envVar: string
        vaultId: string
        label: string
        createBinding: boolean
        targets: Array<{ mcpFilePath: string, serverName: string }>
      }>
    }
    if (!Array.isArray(importRequests)) { json(res, { error: 'imports required' }, 400); return true }
    // VAULTMAINONLY: an import writes the entries (main / owner only for a protected one) and may bind them (never)
    if (!guardProtectedVault(ctx, importRequests.map(i => i?.vaultId), 'import')) return true
    if (!guardProtectedVault(ctx, importRequests.filter(i => i?.createBinding).map(i => i?.vaultId), 'import binding', { never: true })) return true
    let imported = 0
    let bound = 0
    const errors: string[] = []
    for (const imp of importRequests) {
      let value: string | null = null
      for (const target of imp.targets) {
        try {
          const content = JSON.parse(readFileOr(target.mcpFilePath, '{}'))
          const envVal = content?.mcpServers?.[target.serverName]?.env?.[imp.envVar]
          if (envVal && typeof envVal === 'string') { value = envVal; break }
        } catch { /* skip */ }
      }
      if (!value) {
        errors.push(`Could not read value for ${imp.envVar} from ${imp.serverName}`)
        continue
      }
      setSecret(imp.vaultId, imp.label, value)
      imported++
      if (imp.createBinding && imp.targets.length > 0) {
        addBinding({ vaultSecretId: imp.vaultId, envVar: imp.envVar, targets: imp.targets })
        const sync = syncSecret(imp.vaultId)
        bound++
        errors.push(...sync.errors)
      }
    }
    json(res, { ok: true, imported, bound, errors })
    return true
  }

  // === Ollama ===
  if (path === '/api/ollama/models' && method === 'GET') {
    try {
      const resp = await fetch(`${OLLAMA_URL}/api/tags`, { signal: AbortSignal.timeout(5000) })
      const data = await resp.json() as { models?: { name: string; size: number; details?: { parameter_size?: string } }[] }
      const models = (data.models || []).filter(m => !m.name.includes('embed')).map(m => ({
        name: m.name,
        size: Math.round(m.size / 1024 / 1024 / 1024 * 10) / 10 + ' GB',
        params: m.details?.parameter_size || '',
      }))
      json(res, models)
    } catch {
      json(res, [])
    }
    return true
  }

  return false
}

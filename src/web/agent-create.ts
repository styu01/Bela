// The base of creating an agent, apart from the HTTP handler: the refusals that must all pass before anything is
// written (checkAgentCreatable), and the synchronous writes that make a directory an agent (writeAgentBase).
// POST /api/agents calls both, and so will the agent package installer, so a package agent gets the SAME base
// instead of a copy of it.
//
// The idea is upstream's 22477879 (MIOAGENTINSTALL1114 PR-I0); the code is this fork's own POST /api/agents,
// lifted out (BELA 6404). Two refusals are new here and run before anything is written, as upstream's do: a model
// id that writeAgentModel would refuse (it threw after scaffoldAgentDir and left a half-made directory behind), and
// the main agent's name (the main agent has no agents/<name> directory, so the exists check never saw it).
import { existsSync } from 'node:fs'
import { MAIN_AGENT_ID } from '../config.js'
import { isValidModelId, InvalidModelIdError } from '../model-id.js'
import { CLAUDE_MODEL_MIN_CLI, isModelUnsupportedByCli } from '../claude-cli-support.js'
import { measureClaudeCliVersion } from './claude-cli-version.js'
import { loadCustomProvider } from './custom-providers.js'
import { agentDir, writeAgentModel, writeAgentCustomProvider, writeAgentSecurityProfile, writeAgentDisplayName } from './agent-config.js'
import { scaffoldAgentDir, writeAgentSettingsFromProfile } from './agent-scaffold.js'
import { loadProfileTemplate } from './profiles.js'

/**
 * Server-side guard for picker writes (moved here from routes/agents.ts unchanged; the route re-exports it). An
 * unavailable model must be refused here too: a stale/browserless client must not create a session that 400s on its
 * first prompt. A failed measurement deliberately stays fail-open. The probe is fresh (cache bypassed).
 */
export async function refuseIfCliCannotLaunch(model: string): Promise<Record<string, unknown> | null> {
  const cli = await measureClaudeCliVersion({ fresh: true })
  if (!isModelUnsupportedByCli(model, cli.version)) return null
  const req = CLAUDE_MODEL_MIN_CLI[model.replace(/\[[^\]]*\]$/, '')]
  return {
    error: 'model not launchable by the installed Claude Code CLI',
    model,
    installedCli: cli.version,
    minCli: req?.minCli ?? null,
    message: `A telepített Claude Code ${cli.version} nem futtatja a(z) ${model} modellt (legalább ${req?.minCli ?? '?'} kell; mérve: ${req?.measured ?? 'n/a'}). Frissítsd a CLI-t, vagy válassz támogatott modellt.`,
  }
}

export interface AgentCreateRefusal { status: number; body: Record<string, unknown> }

export interface AgentCreatableOpts {
  /** POST /api/agents needs a description (the personality is generated from it); a package brings its own texts. */
  requireDescription: boolean
  description?: unknown
  /** The request's raw customProvider field, so a non-string is refused instead of silently dropped. */
  rawCustomProvider?: unknown
}

/**
 * Every refusal of creating an agent, in the order POST /api/agents answers them. Returns the first one that
 * applies, or null when the agent may be created. Writes nothing. `name` is the sanitized id, `model` the resolved
 * model id and `customProvider` the trimmed provider id or null.
 */
export async function checkAgentCreatable(
  name: string, model: string, customProvider: string | null, opts: AgentCreatableOpts,
): Promise<AgentCreateRefusal | null> {
  if (!name) return { status: 400, body: { error: 'Name is required' } }
  // Before anything is created: writeAgentModel refuses such an id too, but only after scaffoldAgentDir.
  if (!isValidModelId(model)) return { status: 400, body: { error: new InvalidModelIdError(model).message } }
  // A non-string customProvider is refused (as in the PUT), not silently dropped into a provider-less agent.
  const raw = opts.rawCustomProvider
  if (raw !== undefined && raw !== null && raw !== '' && !customProvider) {
    return { status: 400, body: { error: 'customProvider must be null or the id of an existing custom provider (Settings > Providers)' } }
  }
  // An agent on a missing provider would not start.
  if (customProvider && !loadCustomProvider(customProvider)) return { status: 400, body: { error: `Unknown custom provider: ${customProvider}` } }
  const cliGate = await refuseIfCliCannotLaunch(model)
  if (cliGate) return { status: 422, body: cliGate }
  if (opts.requireDescription && !opts.description) return { status: 400, body: { error: 'Description is required' } }
  // The main agent lives in PROJECT_ROOT, not under agents/, so the exists check below never sees it: without this,
  // a sub-agent named like the main one would be created and share its id in messages, kanban and memories.
  if (name === MAIN_AGENT_ID) return { status: 409, body: { error: 'Name is reserved for the main agent' } }
  if (existsSync(agentDir(name))) return { status: 409, body: { error: 'Agent already exists' } }
  return null
}

export interface AgentBase {
  model: string
  profileId: string
  customProvider: string | null
  /** The name as the operator typed it; stored only when it differs from the sanitized id. */
  displayName?: string
}

/**
 * The synchronous writes that make `agents/<name>` an agent: the scaffold, the model, the custom provider (when one
 * is given), the security profile and the settings written from it, and the display name (when it differs from the
 * id). Checks nothing: the caller has already decided that the agent may be created.
 */
export function writeAgentBase(name: string, base: AgentBase): void {
  scaffoldAgentDir(name)
  writeAgentModel(name, base.model)
  if (base.customProvider) writeAgentCustomProvider(name, base.customProvider)
  writeAgentSecurityProfile(name, base.profileId)
  writeAgentSettingsFromProfile(name, loadProfileTemplate(base.profileId))
  if (base.displayName && base.displayName !== name) writeAgentDisplayName(name, base.displayName)
}

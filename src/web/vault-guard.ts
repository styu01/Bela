// VAULTMAINONLY (KISPROGI 6431 plan, BELA 6432 decisions, BELA 6533): the value of a protected vault entry
// (vault-protected.ts) leaves the dashboard over HTTP only in two cases:
//   (a) the caller's process is the main agent's (caller-identity.ts: the kernel names the tmux pane, not a token);
//   (b) the owner's dashboard login session (ownerSessionUser: a session, or the token with a valid session cookie).
// Everyone else gets 403: a sub-agent's pane and every child of it, bela-worker(-fast), the webchat units, a uid 0
// socket without a session (the WSL relay, interop tools), a federation peer, a device key, an unidentifiable caller.
// The same rule guards changing one (POST/DELETE/import, integrity: an overwrite would break the mailer silently),
// a fleet export that packs the vault and a fleet import that brings one. A binding of a protected entry (which
// writes the value into an MCP config file anyone can read) is refused for every caller.
//
// Every refusal writes one log line: the entry ids, the action, the caller's class, pane session, pid, comm and
// scope. Never a value. With VAULT_MAIN_ONLY=audit the refusal is only logged and the call goes through.
//
// A KNOWN LIMIT (KISPROGI 6431 point 3): this narrows the HTTP routes only. Every agent runs as the same OS user, so
// reading the vault from disk goes around it: importing dist/web/vault.js (getSecret), scripts/vault-resolve.mjs,
// vault-headers-helper.mjs, or decrypting store/vault.json with store/.vault-key. Against the accidental route the
// MAILCRED name rule and the store read fence stay; the real boundary is a separate OS user per agent (upstream's C
// option), a separate decision.
import type http from 'node:http'
import { MAIN_AGENT_ID, PROJECT_ROOT } from '../config.js'
import { logger } from '../logger.js'
import { json } from './http-helpers.js'
import { ownerSessionUser } from './routes/auth.js'
import type { RouteContext } from './routes/types.js'
import { identifyLocalCaller, liveCallerIdentityDeps, type CallerIdentity, type CallerIdentityDeps } from './caller-identity.js'
import { isProtectedVaultId, vaultMainOnlyMode } from './vault-protected.js'
export { protectedVaultIdUsable } from './vault-protected.js'

export const VAULT_PROTECTED_ERROR = {
  error: 'This vault entry is protected: only the main agent or the signed-in owner (dashboard login) may read or change it.',
  code: 'vault_protected',
}

export interface ProtectedVaultAccess {
  ok: boolean
  /** owner-session, main, or the refused caller's class (agent, worker, service, external, unknown, federation, device) */
  who: string
  identity?: CallerIdentity
}

let depsForTest: CallerIdentityDeps | null = null
/** Tests only: replace the live /proc + tmux reads. */
export function setCallerIdentityDepsForTest(deps: CallerIdentityDeps | null): void {
  depsForTest = deps
}

function identify(req: http.IncomingMessage): CallerIdentity {
  try {
    const deps = depsForTest ?? liveCallerIdentityDeps(`${MAIN_AGENT_ID}-channels`, PROJECT_ROOT)
    return identifyLocalCaller(req.socket?.remoteAddress, req.socket?.remotePort, req.socket?.localPort, deps)
  } catch (err) {
    return { cls: 'unknown', reason: `identify failed: ${(err as Error).message}` }
  }
}

/** May this request get or change a protected entry? (a) the main agent's process, (b) the owner's login session. */
export function protectedVaultAccess(ctx: Pick<RouteContext, 'req' | 'auth'>): ProtectedVaultAccess {
  const { auth, req } = ctx
  if (auth?.kind === 'federation' || auth?.kind === 'device') return { ok: false, who: auth.kind }
  if ((auth as { tokenAgent?: unknown } | undefined)?.tokenAgent) return { ok: false, who: 'agent-token' }
  if (ownerSessionUser(auth, req) !== null) return { ok: true, who: 'owner-session' }
  const identity = identify(req)
  return { ok: identity.cls === 'main', who: identity.cls, identity }
}

export interface GuardOptions {
  /** refuse every caller (a binding of a protected entry) */
  never?: boolean
}

/** Guard a route action on the given vault ids. True: go on. False: a 403 was written (or, in audit mode, nothing is
 *  refused and it is true). Only the protected ids among `ids` count; with none of them it is always true. */
export function guardProtectedVault(ctx: Pick<RouteContext, 'req' | 'res' | 'auth'>, ids: unknown[], action: string,
                                    opts: GuardOptions = {}): boolean {
  const hit = ids.filter((id) => isProtectedVaultId(id)).map(String)
  if (!hit.length) return true
  const access = protectedVaultAccess(ctx)
  const allowed = access.ok && !opts.never
  const who = opts.never ? `${access.who} (binding: never)` : access.who
  const fields = {
    vaultIds: hit, action, caller: who, session: access.identity?.session, pid: access.identity?.pid,
    comm: access.identity?.comm, scope: access.identity?.scope, reason: access.identity?.reason,
  }
  if (allowed) {
    logger.info(fields, 'vault: protected entry, allowed')
    return true
  }
  const mode = vaultMainOnlyMode()
  logger.warn({ ...fields, mode }, mode === 'audit' ? 'vault: protected entry, WOULD be refused (audit mode)' : 'vault: protected entry refused')
  if (mode === 'audit') return true
  json(ctx.res, VAULT_PROTECTED_ERROR, 403)
  return false
}

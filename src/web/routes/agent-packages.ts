// Agent package credential and catalog (MIOAGENTINSTALL1114, PR-I2; plan 6, 6.4, 6.5).
//
//   GET    /api/agent-packages/credential  -> {connected:false} | {connected:true, label, deviceTokenId, connectedAt}
//   POST   /api/agent-packages/credential  {code:"PPPP-SSSSSSSS"} -> the GET shape + replaced, replacedDeviceTokenId
//   DELETE /api/agent-packages/credential  -> {ok:true, connected:false, remoteRevoked:false}
//   GET    /api/agent-packages/catalog     -> {catalog: <the publishing side's body>}
//   POST   /api/agents/install-package     {slug} -> {ok, name, version, modelHintApplied, skillReport} (PR-I3, plan 6.1)
//
// Every route takes the owner (mayActAsOwner) BEFORE it reads a body: the
// credential lets this machine download packages on the owner's account. The
// network and the credential file are remote.ts's; this file never sees the
// token. The Remote comes from `deps` (5.2): the released build is what web.ts
// wires, the handler called with ONE argument, so RELEASED_DEPS and its fixed
// origin apply; a test passes its own deps with a test origin.

import { lstatSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { STORE_DIR, MAIN_AGENT_ID, OWNER_DRIVE_FOLDER } from '../../config.js'
import { createAgentMessage } from '../../db.js'
import { logger } from '../../logger.js'
import { escalatePackageNotice } from '../agent-package/notices.js'
import { approvePackage, packageApprovalState, pendingPackageAgents } from '../agent-package/approval.js'
import { scanSkillDir } from '../skill-quarantine.js'
import { json, methodNotAllowed, readBodyOrRefuse } from '../http-helpers.js'
import { RELEASED_KEYS } from '../agent-package/keys.js'
import { PACKAGE_ORIGIN, deleteCredential, hasCredential, makeRemote, readCredentialView, type Remote } from '../agent-package/remote.js'
import { SLUG_RE, meetsMinMarveenVersion, parseVersion, verifyDelivery, type Jwk } from '../agent-package/verify.js'
import { agentsBaseDir, isReservedName, markerPathOf } from '../agent-package/marker.js'
import {
  OWNER_DISPLAY_NAME_MAX_INPUT, flushPendingPackageNotices, ownerDisplayName, packageSkillReport, readPackageManifest, reapInterruptedPackageInstalls,
  runInstallBlock, sameDeviceLayout,
} from '../agent-package/install.js'
import { sanitizeAgentName } from '../sanitize.js'
import { refuseIfCliCannotLaunch } from '../agent-create.js'
import { isValidModelId } from '../../model-id.js'
import { getSettingDefinition } from '../../config-registry.js'
import { currentVersion } from '../update-checker.js'
import { serializeSkillsDir } from '../skills-dir-queue.js'
import { isAgentRunning } from '../agent-process.js'
import { DEFAULT_MODEL, listAgentNames, resolveModelId } from '../agent-config.js'
import { ownerSessionUser } from './auth.js'
import { mayActAsOwner, refuseOwner } from './auth.js'
import type { RouteContext } from './types.js'

export type PackageDeps = {
  keys: readonly Jwk[]; fetchImpl: typeof fetch; now: () => Date; remote: Remote
  /** The owner alert path for the installer's stored notices (plan 7.3, N16); none: the notices wait for the next start. */
  notify?: (text: string) => Promise<boolean>
}

export const RELEASED_DEPS: PackageDeps = {
  keys: RELEASED_KEYS,
  fetchImpl: fetch,
  now: () => new Date(),
  remote: makeRemote(PACKAGE_ORIGIN),
  notify: escalatePackageNotice,
}

/** The closed list a package's model hint must be on (the picker's DEFAULT_AGENT_MODEL valueSet). */
const PACKAGE_MODEL_HINTS: ReadonlySet<string> = new Set(getSettingDefinition('DEFAULT_AGENT_MODEL')?.valueSet ?? [])

const CREDENTIAL_PATH = '/api/agent-packages/credential'
const CATALOG_PATH = '/api/agent-packages/catalog'
const INSTALL_PATH = '/api/agents/install-package'
/** Plan 6: a request body is at most 4 KiB. */
const BODY_MAX_BYTES = 4 * 1024
const BAD_REQUEST = { error: 'bad_request' }

/**
 * The device-link code (A-side contract 4.1: `PPPP-SSSSSSSS`, Crockford base32)
 * in its canonical form, or null. As Crockford decoding reads it: case does not
 * matter, blanks and hyphens are ignored, O reads as 0 and I, L as 1.
 */
export function normalizeLinkCode(raw: unknown): string | null {
  if (typeof raw !== 'string' || raw.length > 64) return null
  const s = raw.toUpperCase().replace(/[\s-]/g, '').replace(/O/g, '0').replace(/[IL]/g, '1')
  if (!/^[0-9A-HJKMNP-TV-Z]{12}$/.test(s)) return null
  return `${s.slice(0, 4)}-${s.slice(4)}`
}

/** The redeem body: exactly {code}; null for anything else (the parse error is dropped, T1). */
function codeFromBody(raw: Buffer): string | null {
  let v: unknown
  try {
    v = JSON.parse(raw.toString('utf8'))
  } catch {
    return null
  }
  if (v === null || typeof v !== 'object' || Array.isArray(v)) return null
  const keys = Object.keys(v)
  if (keys.length !== 1 || keys[0] !== 'code') return null
  return normalizeLinkCode((v as { code: unknown }).code)
}

/** The install body: exactly {slug} with a string value; null for anything else. */
/**
 * The install body: `{slug}`, or `{slug, display_name}` (MIOAGENTB4-1122, Samu 83718: the name the owner chose for
 * the dashboard). Any other key is a 400 bad_request, as before; a display_name that is not a string, longer than
 * OWNER_DISPLAY_NAME_MAX_INPUT, or against a manifest text rule after the cleaning is its own 400, so the dialog can
 * say which field is wrong (Samu 83741).
 */
function installBodyOf(raw: Buffer): { slug: string; displayName?: string } | 'bad_request' | 'invalid_display_name' {
  let v: unknown
  try {
    v = JSON.parse(raw.toString('utf8'))
  } catch {
    return 'bad_request'
  }
  if (v === null || typeof v !== 'object' || Array.isArray(v)) return 'bad_request'
  const keys = Object.keys(v)
  if (!keys.includes('slug') || keys.some((k) => k !== 'slug' && k !== 'display_name')) return 'bad_request'
  const { slug, display_name: rawName } = v as { slug: unknown; display_name?: unknown }
  if (typeof slug !== 'string') return 'bad_request'
  if (rawName === undefined) return { slug }
  if (typeof rawName !== 'string' || Array.from(rawName).length > OWNER_DISPLAY_NAME_MAX_INPUT) return 'invalid_display_name'
  const name = ownerDisplayName(rawName)
  if (name === null) return 'invalid_display_name'
  return name === undefined ? { slug } : { slug, displayName: name }
}

type Answer = { status: number; body: Record<string, unknown> }

/** An entry at `p`, a dangling link included (existsSync follows links). */
function entryExists(p: string): boolean {
  try {
    lstatSync(p)
    return true
  } catch {
    return false
  }
}

/** Plan 7.3: after a cleanup or a set-aside beside a live server, the stored owner notices go out (best effort). */
function flushNotices(deps: PackageDeps): void {
  if (!deps.notify) return
  void flushPendingPackageNotices(deps.notify).catch((err) => logger.warn({ err }, 'agent package owner notices were not sent'))
}

/**
 * Plan 6.1 step 4: what stands under the name now. null when the name is free. A marker that stands is first given
 * to the reaper (it cannot belong to a running install: the block is synchronous); one that stays is stale_install.
 */
function nameStateRefusal(slug: string): Answer | null {
  if (entryExists(markerPathOf(slug))) {
    reapInterruptedPackageInstalls({ only: slug })
    if (entryExists(markerPathOf(slug))) return { status: 409, body: { error: 'stale_install' } }
  }
  const dir = join(agentsBaseDir(), slug)
  if (!entryExists(dir)) return null
  const m = readPackageManifest(dir)
  if (m.state === 'ok' && m.manifest.payload.slug === slug) {
    return { status: 409, body: { error: 'already_installed', version: m.manifest.payload.version } }
  }
  return { status: 409, body: { error: 'name_taken' } }
}

const UNSUPPORTED_LAYOUT_MESSAGE = 'Package install is not available on this layout: the agents folder and the store folder are on different volumes. Put both on one volume, then install again.'

/**
 * POST /api/agents/install-package (plan 6.1). Steps 1-5 are local and download nothing: on a trial account the
 * first successful download fixes the one pick, so every local refusal comes first.
 */
async function installPackage(ctx: RouteContext, deps: PackageDeps): Promise<void> {
  const { req, res } = ctx
  // 1. the owner, before the body.
  if (!mayActAsOwner(ctx.auth, req)) { refuseOwner(res); return }
  // 2. the body and the slug.
  const raw = await readBodyOrRefuse(req, res, { maxBytes: BODY_MAX_BYTES })
  if (raw === null) return
  const body = installBodyOf(raw)
  if (body === 'bad_request') { json(res, BAD_REQUEST, 400); return }
  if (body === 'invalid_display_name') { json(res, { error: 'invalid_display_name' }, 400); return }
  const slug = body.slug
  if (!SLUG_RE.test(slug) || sanitizeAgentName(slug) !== slug) { json(res, { error: 'invalid_slug' }, 400); return }
  // 3. a reserved name.
  if (isReservedName(slug)) { json(res, { error: 'name_reserved' }, 409); return }
  // 4. the layout (a set-aside must be one rename), then the name's state, in the skills-dir queue.
  if (!sameDeviceLayout(STORE_DIR)) { json(res, { error: 'unsupported_layout', message: UNSUPPORTED_LAYOUT_MESSAGE }, 409); return }
  const taken = await serializeSkillsDir(() => nameStateRefusal(slug))
  flushNotices(deps)
  if (taken !== null) { json(res, taken.body, taken.status); return }
  // 5. the own version, the CLI gate on the default model, the credential, the keys and the origin.
  const ownVersion = currentVersion()
  if (parseVersion(ownVersion) === null) { json(res, { error: 'own_version_unknown' }, 500); return }
  // This fork: the install default is the same one POST /api/agents uses (no upstream default-model guard here).
  let model = resolveModelId(DEFAULT_MODEL)
  const cliGate = await refuseIfCliCannotLaunch(model)
  if (cliGate) { json(res, cliGate, 422); return }
  if (!hasCredential()) { json(res, { error: 'not_connected' }, 409); return }
  if (deps.keys.length === 0 || !deps.remote.configured()) {
    json(res, { error: 'download_failed', reason: 'not_configured' }, 502)
    return
  }
  // 6. the download.
  const dl = await deps.remote.download(slug, ownVersion, { fetchImpl: deps.fetchImpl, now: deps.now })
  if (!dl.ok) { json(res, dl.refusal.body, dl.refusal.status); return }
  // 7. the verification: nothing is written for a package that fails it.
  const v = verifyDelivery(dl.bytes, slug, deps.keys)
  if (!v.ok) {
    json(res, { error: 'package_rejected', step: v.step, reason: v.reason, ...(v.detail ? { detail: v.detail } : {}) }, 422)
    return
  }
  const pkg = v.pkg
  // 8. the minimum version (the publishing side decided it already; this is the safety net).
  if (!meetsMinMarveenVersion(ownVersion, pkg.minMarveenVersion)) {
    json(res, { error: 'incompatible', minMarveenVersion: pkg.minMarveenVersion }, 409)
    return
  }
  // 9. the model hint: only a Claude model of the picker's closed list that the installed CLI launches (M26: the
  // payload must not send the owner's conversations to another provider).
  let modelHintApplied: boolean | null = null
  if (pkg.modelHint !== null) {
    modelHintApplied = false
    // This fork's closed list is the model picker's own (the DEFAULT_AGENT_MODEL setting's valueSet; upstream reads
    // AGENT_MODEL_CHOICES, which this fork does not have): a claude- id on it that the installed CLI launches.
    if (pkg.modelHint.startsWith('claude-') && isValidModelId(pkg.modelHint) && PACKAGE_MODEL_HINTS.has(pkg.modelHint)
      && (await refuseIfCliCannotLaunch(pkg.modelHint)) === null) {
      model = pkg.modelHint
      modelHintApplied = true
    }
  }
  // 10. the disk phase: one synchronous block in the skills-dir queue.
  // This fork: OWNER_DRIVE_FOLDER is a config value, not a settings-store key (upstream reads the store).
  const driveFolder = OWNER_DRIVE_FOLDER
  const lang = 'hu' as const // this fork's agent texts are Hungarian (upstream: agentTextLang)
  const now = deps.now()
  const out = await serializeSkillsDir(() => {
    const r = runInstallBlock({ name: slug, pkg, model, driveFolder, lang, now, ...(body.displayName ? { displayName: body.displayName } : {}) })
    if (r.ok) return { ok: true as const, skillReport: packageSkillReport(slug, pkg, now) }
    if (r.kind === 'marker_exists') return { ok: false as const, answer: { status: 409, body: { error: 'stale_install' } } }
    if (r.kind === 'dir_exists') {
      return { ok: false as const, answer: nameStateRefusal(slug) ?? { status: 409, body: { error: 'name_taken' } } }
    }
    return { ok: false as const, answer: { status: 500, body: { error: 'install_failed' } } }
  })
  if (!out.ok) {
    flushNotices(deps)
    json(res, out.answer.body, out.answer.status)
    return
  }
  // 11. This fork (BELA 6438/6444): the package is SET ASIDE, not taken into use. It does not start before the
  // owner approval (approval.ts), so the fleet is not told of a new colleague yet (that happens on approval), and
  // BELA is asked to review it: the name, the version, the files and the skill scan counts, never the texts.
  json(res, { ok: true, name: slug, version: pkg.version, modelHintApplied, skillReport: out.skillReport, approval: 'pending' })
  try {
    const files = pkg.files.map((f) => f.path).join(', ')
    const findings = Object.entries(out.skillReport.findings).map(([k, v]) => `${k}: ${v}`).join(', ') || 'nincs'
    await (deps.notify ?? escalatePackageNotice)(
      `Ugynok-csomag telepitve, JOVAHAGYASRA VAR (nem indul el addig): ${slug} ${pkg.version}. ` +
      `Fajlok: ${files}. Skill-vizsgalat talalatai: ${findings}. ` +
      `Atnezes: agents/${slug}/ (PACKAGE.md, LOCAL.md, .claude/skills). Jovahagyas: POST /api/agents/${slug}/approve-package (owner-munkamenet).`,
    )
  } catch (err) {
    logger.warn({ err, name: slug }, 'Agent package installed, but the review request failed')
  }
}

/** The fleet notice of a newly approved package agent: the NAME only (upstream plan 4.2), never its description. */
function announceApprovedPackageAgent(slug: string): void {
  try {
    const running = listAgentNames().filter((a) => a !== slug && isAgentRunning(a))
    for (const target of [MAIN_AGENT_ID, ...running]) {
      createAgentMessage('system', target, `Uj csapattag erkezett: ${slug}. Udv neki ha legkozelebb beszeltek!`)
    }
  } catch (err) {
    logger.warn({ err, name: slug }, 'Agent package approved, but the team notification failed')
  }
}

const APPROVE_RE = /^\/api\/agents\/([^/]+)\/approve-package$/
const PENDING_PATH = '/api/agent-packages/pending'

/**
 * POST /api/agents/<name>/approve-package (BELA 6438/6444): the owner approves exactly the package that stands in the
 * folder now. Optional body {version, payloadSha256}: what the approver reviewed; a package updated in between is
 * refused (409 package_changed), never approved unseen. Owner session only for now; the main agent's own caller
 * identity comes with the caller-identity module KISPROGI planned (6431), one module for both.
 */
async function approvePackageRoute(ctx: RouteContext, deps: PackageDeps, name: string): Promise<void> {
  const { req, res } = ctx
  if (!mayActAsOwner(ctx.auth, req)) { refuseOwner(res); return }
  const raw = await readBodyOrRefuse(req, res, { maxBytes: BODY_MAX_BYTES })
  if (raw === null) return
  let expected: { version?: string; payloadSha256?: string } = {}
  const text = raw.toString('utf8').trim()
  if (text) {
    let v: unknown
    try { v = JSON.parse(text) } catch { json(res, BAD_REQUEST, 400); return }
    if (v === null || typeof v !== 'object' || Array.isArray(v)) { json(res, BAD_REQUEST, 400); return }
    const o = v as Record<string, unknown>
    if (Object.keys(o).some((k) => k !== 'version' && k !== 'payloadSha256')) { json(res, BAD_REQUEST, 400); return }
    if (o.version !== undefined && typeof o.version !== 'string') { json(res, BAD_REQUEST, 400); return }
    if (o.payloadSha256 !== undefined && typeof o.payloadSha256 !== 'string') { json(res, BAD_REQUEST, 400); return }
    expected = { version: o.version as string | undefined, payloadSha256: o.payloadSha256 as string | undefined }
  }
  const by = ownerSessionUser(ctx.auth, req) ?? 'owner'
  const r = approvePackage(name, by, deps.now(), expected)
  if (!r.ok) { json(res, { error: r.error }, r.status); return }
  json(res, { ok: true, name, version: r.approval.version, payloadSha256: r.approval.payloadSha256, approvedAt: r.approval.approvedAt })
  announceApprovedPackageAgent(name)
}

/** GET /api/agent-packages/pending: the package agents that wait for an approval, with what stands in each and the
 *  skill scan counts of its skills (the approver's triage list; never a file's text). Owner session only. */
function pendingRoute(ctx: RouteContext): void {
  const { req, res } = ctx
  if (!mayActAsOwner(ctx.auth, req)) { refuseOwner(res); return }
  const pending = pendingPackageAgents().map((name) => {
    const st = packageApprovalState(name)
    const installed = st.state === 'pending' ? st.installed : null
    const findings: Record<string, number> = {}
    const skillsRoot = join(agentsBaseDir(), name, '.claude', 'skills')
    let skills: string[] = []
    try { skills = readdirSync(skillsRoot).sort() } catch { /* no skills */ }
    for (const sk of skills) {
      try {
        for (const f of scanSkillDir(join(skillsRoot, sk)).findings) findings[f.pattern] = (findings[f.pattern] ?? 0) + 1
      } catch { /* best effort */ }
    }
    return { name, state: st.state, installed, skills, findings }
  })
  json(res, { pending })
}

export async function tryHandleAgentPackages(ctx: RouteContext, deps: PackageDeps = RELEASED_DEPS): Promise<boolean> {
  const { req, res, path, method } = ctx

  const approveMatch = APPROVE_RE.exec(path)
  if (approveMatch) {
    if (method !== 'POST') { methodNotAllowed(res, method, ['POST']); return true }
    await approvePackageRoute(ctx, deps, decodeURIComponent(approveMatch[1]))
    return true
  }
  if (path === PENDING_PATH) {
    if (method !== 'GET') { methodNotAllowed(res, method, ['GET']); return true }
    pendingRoute(ctx)
    return true
  }

  if (path === CREDENTIAL_PATH && method === 'GET') {
    if (!mayActAsOwner(ctx.auth, req)) { refuseOwner(res); return true }
    json(res, readCredentialView())
    return true
  }

  if (path === CREDENTIAL_PATH && method === 'POST') {
    if (!mayActAsOwner(ctx.auth, req)) { refuseOwner(res); return true }
    const raw = await readBodyOrRefuse(req, res, { maxBytes: BODY_MAX_BYTES })
    if (raw === null) return true
    const code = codeFromBody(raw)
    if (code === null) { json(res, BAD_REQUEST, 400); return true }
    const r = await deps.remote.redeem(code, { fetchImpl: deps.fetchImpl, now: deps.now })
    if (!r.ok) { json(res, r.refusal.body, r.refusal.status); return true }
    json(res, { ...r.view, replaced: r.replaced, replacedDeviceTokenId: r.replacedDeviceTokenId })
    return true
  }

  if (path === CREDENTIAL_PATH && method === 'DELETE') {
    if (!mayActAsOwner(ctx.auth, req)) { refuseOwner(res); return true }
    try {
      deleteCredential()
    } catch {
      json(res, { error: 'credential_delete_failed' }, 500)
      return true
    }
    json(res, { ok: true, connected: false, remoteRevoked: false })
    return true
  }

  if (path === CATALOG_PATH && method === 'GET') {
    if (!mayActAsOwner(ctx.auth, req)) { refuseOwner(res); return true }
    const r = await deps.remote.catalog({ fetchImpl: deps.fetchImpl, now: deps.now })
    if (!r.ok) { json(res, r.refusal.body, r.refusal.status); return true }
    json(res, { catalog: r.catalog })
    return true
  }

  if (path === INSTALL_PATH && method === 'POST') {
    await installPackage(ctx, deps)
    return true
  }

  if (path === CREDENTIAL_PATH) { methodNotAllowed(res, method, ['GET', 'POST', 'DELETE']); return true }
  if (path === INSTALL_PATH) { methodNotAllowed(res, method, ['POST']); return true }
  if (path === CATALOG_PATH) { methodNotAllowed(res, method, ['GET']); return true }
  return false
}

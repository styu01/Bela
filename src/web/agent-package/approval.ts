// The owner approval of an installed agent package (this fork; BELA 6438/6444, Istvan's rule: a downloaded agent or
// skill is reviewed by BELA before it is used, and nothing installs itself into use).
//
// Upstream's installer (MIOAGENTPRI3-1118) leaves a finished install startable at once. Here an installed or updated
// package agent is SET ASIDE: it does not start (startAgentProcess and the watchdog both refuse it) until an approval
// record names exactly the package that stands in its folder. The record lives in the store, which only the dashboard
// writes:
//
//   store/agent-package-approved/<name>.json  {schema:1, name, slug, version, payloadSha256, approvedAt, approvedBy}
//
// It binds to the installed package record (.package/manifest.json): the slug, the version and the sha256 of the
// signed payload. An update writes a new record, so the old approval no longer matches and the agent waits again; a
// restore or a copy that brings a package agent without its approval does not start either. Fail closed throughout:
// a missing, unreadable, linked or mismatched approval, or an unreadable manifest, is "not approved".
//
// The watchdog (scripts/watchdog.sh) makes the same comparison itself, with python3 on the same two files: no derived
// list stands between them, so nothing can go stale and let an updated, unapproved package agent start there.
import { lstatSync, mkdirSync, readFileSync, readdirSync, unlinkSync } from 'node:fs'
import { join } from 'node:path'
import { STORE_DIR } from '../../config.js'
import { logger } from '../../logger.js'
import { atomicWriteFileSync } from '../atomic-write.js'
import { agentsBaseDir, isInstallableName, isPackageAgentDir, installMarkerState, updatingTracePathOf } from './marker.js'
import { clearPackageReviewNotice } from './notices.js'
import { readPackageManifest } from './install.js'

export const APPROVALS_DIRNAME = 'agent-package-approved'
/** The start refusal for a package agent its owner has not approved (the same text in the watchdog). */
export const REFUSAL_PACKAGE_NOT_APPROVED = 'Package awaiting owner approval'

export interface PackageApproval {
  schema: 1
  name: string
  slug: string
  version: string
  payloadSha256: string
  approvedAt: string
  approvedBy: string
}

export type PackageIdentity = { slug: string; version: string; payloadSha256: string }

export function approvalsDir(): string {
  return join(STORE_DIR, APPROVALS_DIRNAME)
}
function approvalPathOf(name: string): string {
  return join(approvalsDir(), `${name}.json`)
}
function agentDirOf(name: string): string {
  return join(agentsBaseDir(), name)
}

/** The package that stands in the folder, from its record; null when the record cannot be read as one. */
export function installedPackageIdentity(dir: string): PackageIdentity | null {
  const r = readPackageManifest(dir)
  if (r.state !== 'ok') return null
  return { slug: r.manifest.payload.slug, version: r.manifest.payload.version, payloadSha256: r.manifest.payloadSha256 }
}

function readApproval(name: string): PackageApproval | null {
  const p = approvalPathOf(name)
  try {
    // never through a link: the record is the dashboard's own file
    if (!lstatSync(p).isFile()) return null
    const v = JSON.parse(readFileSync(p, 'utf8')) as Record<string, unknown>
    if (v === null || typeof v !== 'object' || Array.isArray(v) || v.schema !== 1 || v.name !== name
      || typeof v.slug !== 'string' || typeof v.version !== 'string' || typeof v.payloadSha256 !== 'string'
      || typeof v.approvedAt !== 'string' || typeof v.approvedBy !== 'string') return null
    return v as unknown as PackageApproval
  } catch {
    return null
  }
}

export type ApprovalState =
  | { state: 'not_package' }
  | { state: 'approved'; approval: PackageApproval }
  | { state: 'pending'; installed: PackageIdentity; approval: PackageApproval | null }
  | { state: 'unreadable_record' }

/** Where the agent `name` stands. Only `not_package` and `approved` may start. */
export function packageApprovalState(name: string): ApprovalState {
  const dir = agentDirOf(name)
  if (!isPackageAgentDir(dir)) return { state: 'not_package' }
  const installed = installedPackageIdentity(dir)
  if (installed === null) return { state: 'unreadable_record' }
  const approval = readApproval(name)
  if (approval !== null && approval.slug === installed.slug && approval.version === installed.version
    && approval.payloadSha256 === installed.payloadSha256) return { state: 'approved', approval }
  return { state: 'pending', installed, approval }
}

/** The start refusal text, or null when the agent may start as far as the approval goes. */
export function packageApprovalRefusal(name: string): string | null {
  const s = packageApprovalState(name)
  return s.state === 'not_package' || s.state === 'approved' ? null : REFUSAL_PACKAGE_NOT_APPROVED
}

export type ApproveResult =
  | { ok: true; approval: PackageApproval }
  | { ok: false; status: number; error: string }

/**
 * Records the owner's approval of exactly the package that stands in `name`'s folder now. `expected`, when given,
 * must match it (the approver saw that version): a package updated in between is refused, not approved unseen.
 */
export function approvePackage(name: string, approvedBy: string, now: Date, expected?: Partial<PackageIdentity>): ApproveResult {
  if (!isInstallableName(name)) return { ok: false, status: 400, error: 'invalid_name' }
  const dir = agentDirOf(name)
  try {
    if (!lstatSync(dir).isDirectory()) return { ok: false, status: 404, error: 'agent_not_found' }
  } catch {
    return { ok: false, status: 404, error: 'agent_not_found' }
  }
  if (installMarkerState(name).kind === 'valid') return { ok: false, status: 409, error: 'install_in_progress' }
  if (!isPackageAgentDir(dir)) return { ok: false, status: 409, error: 'not_a_package_agent' }
  const installed = installedPackageIdentity(dir)
  if (installed === null) return { ok: false, status: 409, error: 'package_record_unreadable' }
  for (const k of ['slug', 'version', 'payloadSha256'] as const) {
    if (expected?.[k] !== undefined && expected[k] !== installed[k]) return { ok: false, status: 409, error: 'package_changed' }
  }
  const approval: PackageApproval = { schema: 1, name, ...installed, approvedAt: now.toISOString(), approvedBy }
  mkdirSync(approvalsDir(), { recursive: true, mode: 0o700 })
  atomicWriteFileSync(approvalPathOf(name), JSON.stringify(approval, null, 2) + '\n', { mode: 0o600 })
  logger.info({ name, slug: installed.slug, version: installed.version, approvedBy }, 'agent package approved')
  return { ok: true, approval }
}

/** The package agents whose approval matches what stands in their folder. */
export function approvedPackageAgents(): string[] {
  let names: string[]
  try {
    names = readdirSync(agentsBaseDir())
  } catch {
    return []
  }
  return names.filter((n) => isInstallableName(n) && packageApprovalState(n).state === 'approved').sort()
}

/** The package agents that wait for an approval (installed, or updated since the last one). */
export function pendingPackageAgents(): string[] {
  let names: string[]
  try {
    names = readdirSync(agentsBaseDir())
  } catch {
    return []
  }
  return names.filter((n) => {
    if (!isInstallableName(n) || installMarkerState(n).kind === 'valid') return false
    const s = packageApprovalState(n).state
    return s === 'pending' || s === 'unreadable_record'
  }).sort()
}

/**
 * KISPROGI 6476 (a) and what goes with it (BELA 6478): an agent folder was deleted (DELETE /api/agents/<name>, after
 * the folder is gone). Its package state outside the folder goes too:
 *  - the open review request: its stage-2 owner alert about an agent that no longer exists must not go out;
 *  - the owner approval record: a later install under the same name, even of the very same package, waits for a NEW
 *    approval instead of starting on the old one;
 *  - an update trace: it would hold a later install under the same name as "Package update in progress" (the boot
 *    sweep removeOrphanUpdatingTraces only runs at a server start).
 * A link or any other entry under those names is removed as well (the name is checked first, nothing outside the two
 * store folders is touched). Never throws; returns what it removed.
 */
export function forgetDeletedPackageAgent(name: string): { review: boolean; approval: boolean; trace: boolean } {
  const out = { review: false, approval: false, trace: false }
  if (!isInstallableName(name)) return out
  try { out.review = clearPackageReviewNotice(name) } catch (err) { logger.warn({ err, name }, 'agent package: the review request was not cleared') }
  const drop = (p: string): boolean => {
    try {
      lstatSync(p)
    } catch {
      return false
    }
    try {
      unlinkSync(p)
      return true
    } catch (err) {
      logger.warn({ err, name, path: p }, 'agent package: a record of a deleted agent was not removed')
      return false
    }
  }
  out.approval = drop(approvalPathOf(name))
  out.trace = drop(updatingTracePathOf(name))
  return out
}

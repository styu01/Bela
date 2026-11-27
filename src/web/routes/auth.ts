// Dashboard browser-login endpoints.
//
// All of these are OPTIONAL: with zero dashboard_users rows the gate behaves
// exactly like the token-only mode and only /api/auth/status is meaningful
// (setup_required:true). Login enforcement becomes available -- never required,
// the bearer token always works -- once the operator creates a user.
//
// The bearer token stays the break-glass credential: creating the FIRST user
// and resetting a password are reachable with a valid bearer, so there is never
// an unauthenticated first-run setup page (which would be a claim-the-admin race
// on loopback).
//
// Principal discipline: every handler that grants or removes access checks the
// caller's auth kind against an EXPLICIT allowlist. Only 'token' and 'session'
// can reach these paths today (federation tokens are endpoint-scoped inside
// resolveAuth and never resolve here), but the allowlists exist so any future
// AuthResult kind -- e.g. per-device keys -- is denied by default. What a new
// credential type may do here must be a review decision, not the accident of
// an else-branch.

import type http from 'node:http'
import { readBody, json } from '../http-helpers.js'
import { logger } from '../../logger.js'
import { parseCookies, SESSION_COOKIE_NAME, sessionUserFromCookie } from '../auth-gate.js'
import { isPublicRequest } from '../public-access.js'
import {
  hashPassword,
  verifyPassword,
  assertPasswordPolicy,
  PasswordPolicyError,
} from '../password-hash.js'
import {
  createSession,
  revokeSession,
  revokeAllForUser,
  listUserSessions,
} from '../auth-sessions.js'
import {
  checkThrottle,
  recordFailure,
  recordSuccess,
  runDummyVerify,
} from '../login-throttle.js'
import {
  createDeviceKey,
  listDeviceKeys,
  revokeDeviceKey,
  getDeviceKey,
} from '../auth-device-keys.js'
import { removeBridgeSshAccess, sshDirOverride } from '../bridge-enroll.js'
import {
  createDashboardUser,
  getDashboardUser,
  listDashboardUsers,
  countDashboardUsers,
  updateDashboardUserPassword,
  deleteDashboardUser,
  setDashboardUserTotpEnabled,
  logConfigChange,
} from '../../db.js'
import { notifySecurityEvent } from '../../notify.js'
import {
  getTotpForUser,
  saveTotpRecord,
  removeTotpRecord,
  createPendingLogin,
  getPendingLogin,
  consumePendingLogin,
  setPendingSetup,
  getPendingSetup,
  clearPendingSetup,
  PENDING_LOGIN_TTL_S,
  PENDING_SETUP_TTL_S,
  TotpStoreError,
  type TotpRecord,
} from '../dashboard-totp.js'
import {
  matchTotp,
  findBackupCode,
  generateTotpSecret,
  generateBackupCodes,
  hashBackupCode,
  otpauthUri,
} from '../totp.js'
import type { RouteContext } from './types.js'

const LOGIN_BODY_MAX_BYTES = 8 * 1024
const USERNAME_RE = /^[a-zA-Z0-9._-]{1,64}$/
const INVALID_CREDENTIALS = { error: 'Invalid credentials' }

// Set Secure only when the request arrived over https (Tailscale Serve sets
// x-forwarded-proto). The primary transport is plain http://127.0.0.1 where an
// unconditional Secure attribute would make Safari drop the cookie (Chrome
// exempts 127.0.0.1 as trustworthy; Safari's behavior there is inconsistent).
function isHttps(req: http.IncomingMessage): boolean {
  const xf = req.headers['x-forwarded-proto']
  const v = Array.isArray(xf) ? xf[0] : xf
  return typeof v === 'string' && v.split(',')[0]!.trim().toLowerCase() === 'https'
}

// DOCKERHTTPS1119 port: on the public listener (behind the local HTTPS proxy) the cookie is always Secure, whatever
// X-Forwarded-Proto says.
function secureCookie(req: http.IncomingMessage): boolean {
  return isHttps(req) || isPublicRequest(req)
}

function sessionCookie(token: string, req: http.IncomingMessage): string {
  const base = `${SESSION_COOKIE_NAME}=${token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000`
  return secureCookie(req) ? `${base}; Secure` : base
}

function clearCookie(req: http.IncomingMessage): string {
  const base = `${SESSION_COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0`
  return secureCookie(req) ? `${base}; Secure` : base
}

async function parseJsonBody(req: http.IncomingMessage): Promise<Record<string, unknown>> {
  const raw = (await readBody(req, { maxBytes: LOGIN_BODY_MAX_BYTES })).toString().trim()
  if (!raw) return {}
  const parsed = JSON.parse(raw)
  return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {}
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

// Explicit principal allowlist. Compares on the kind string so tests can
// exercise kinds the AuthResult union does not carry yet; anything not listed
// (including undefined auth, which the gate should have 401'd already) fails.
function kindAllowed(auth: RouteContext['auth'], kinds: readonly string[]): boolean {
  return auth !== undefined && kinds.includes(auth.kind)
}

const FORBIDDEN_KIND = { error: 'Forbidden for this credential type' }

// Who may manage dashboard users (list/create/delete). 'session' is included
// deliberately: a logged-in operator managing users is the existing behavior.
const USER_ADMIN_KINDS = ['token', 'session'] as const

// Who may mint/list/revoke device keys. Deliberately the SAME operator set --
// and deliberately NOT 'device': a device key that could mint further keys
// would turn one leaked device into unlimited, unexpiring access.
const DEVICE_KEY_ADMIN_KINDS = ['token', 'session'] as const

const DEVICE_KEY_NAME_RE = /^[\p{L}\p{N} ._-]{1,64}$/u
const DEVICE_KEY_MAX_EXPIRY_DAYS = 3650

// { authenticated, method, user, device, login_available, setup_required }.
// authenticated keeps its exact old meaning for token callers (existing probes
// parse only that field): a valid bearer -> authenticated:true. A device key is
// an authenticated principal too (method:'device', device:<key name>).
function statusPayload(auth: RouteContext['auth'], req?: http.IncomingMessage) {
  const authenticated = auth?.kind === 'token' || auth?.kind === 'session' || auth?.kind === 'device'
  const method = authenticated ? auth!.kind : null
  const user = auth?.kind === 'session' ? auth.user ?? null : null
  const device = auth?.kind === 'device' ? auth.device ?? null : null
  return {
    authenticated,
    method,
    user,
    device,
    login_available: countDashboardUsers(false) >= 1,
    setup_required: countDashboardUsers(true) === 0,
    // DOCKERHTTPS1119 port: whether the owner's own login session rides on this call (a session, or the token with the
    // login cookie beside it). The two-step sign-in settings show only then.
    owner_session: req ? ownerSessionUser(auth, req) !== null : auth?.kind === 'session',
  }
}

// ---- DOCKERHTTPS1119: the optional second factor (TOTP) of the owner's password login ----------------------------
//
// Off by default (no record in store/.dashboard-totp.json, see ../dashboard-totp.ts). With it on, POST
// /api/auth/login answers a right password with 401 totp_required and a pending token, and POST
// /api/auth/login/totp takes that token and a code (6 digits, or a backup code) and issues the session exactly as the
// password login would. Setup, enable and disable take the owner's session (ownerSessionUser: a login session, or the
// bearer token with the login cookie beside it); never the bare token, a sub-agent token or a device key.

export const TOTP_REQUIRED = { error: 'Two-step sign-in: enter the code from your authenticator app.', code: 'totp_required' }
export const TOTP_INVALID = { error: 'Invalid code', code: 'totp_invalid' }
export const TOTP_PENDING_EXPIRED = { error: 'The sign-in step expired. Sign in again with your password.', code: 'totp_pending_expired' }
export const TOTP_STORE_UNAVAILABLE = { error: 'Two-step sign-in is unavailable: its store on the server cannot be read. Repair it on the machine (docs/dashboard-auth-recovery.md).', code: 'totp_store_unavailable' }
export const TOTP_ALREADY_ENABLED = { error: 'Two-step sign-in is already on.', code: 'totp_already_enabled' }
export const TOTP_NOT_ENABLED = { error: 'Two-step sign-in is off.', code: 'totp_not_enabled' }
export const TOTP_SETUP_MISSING = { error: 'Start the setup first (or again: it expired).', code: 'totp_setup_missing' }
export const TOTP_CODE_REQUIRED = { error: 'A code is required.', code: 'totp_code_required' }
// DOCKERHTTPS1119 port: the public HTTPS address takes only a login with the two-step sign-in on.
export const PUBLIC_TOTP_REQUIRED = { error: 'The public address takes only a login with two-step sign-in on. Turn it on at the internal address first.', code: 'public_totp_required' }

/** DOCKERHTTPS1119: the owner ping when the second factor is turned off on the web (password + code). */
export function totpDisabledNotice(username: string): string {
  return `🔑 Kétlépcsős belépés kikapcsolva a dashboardon (jelszóval és kóddal): "${username}". ` +
    'Ha nem te voltál, a jelszavad és egy kódod mással is megvan: változtasd meg a jelszót, és kapcsold vissza a kétlépcsős belépést.'
}

function noStoreJson(res: http.ServerResponse, body: unknown, status: number): void {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'private, no-store' })
  res.end(JSON.stringify(body))
}

function tooManyAttempts(res: http.ServerResponse, retryAfterS: number): void {
  res.writeHead(429, { 'Content-Type': 'application/json; charset=utf-8', 'Retry-After': String(retryAfterS), 'Cache-Control': 'private, no-store' })
  res.end(JSON.stringify({ error: 'Too many attempts', retry_after_s: retryAfterS }))
}

/** The end of every successful sign-in, with or without the second factor: the support-password locks end, any
 *  presented session is revoked (fixation defence) and a fresh session cookie is issued. */
function finishLogin(req: http.IncomingMessage, res: http.ServerResponse, user: { id: number; username: string }, body: Record<string, unknown>): void {
  // Session fixation defence: if a (valid) session cookie accompanied the
  // login, revoke it -- login always mints a fresh id.
  const presented = parseCookies(req.headers.cookie)[SESSION_COOKIE_NAME]
  if (presented) revokeSession(presented)
  const token = createSession(
    { userId: user.id, username: user.username },
    { userAgent: str(req.headers['user-agent']) || null, remoteNote: isHttps(req) ? 'https' : 'loopback' },
  )
  res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Set-Cookie': sessionCookie(token, req), 'Cache-Control': 'private, no-store' })
  res.end(JSON.stringify(body))
}

/** A TOTP code (strictly after the last accepted step, +-1 step) or an unused backup code. Returns the record to save
 *  (last_step moved, or the backup code's hash removed), or null. Synchronous: no await between this and the save. */
export function checkSecondFactor(rec: TotpRecord, rawCode: string, now: number = Date.now()): { rec: TotpRecord; backupUsed: boolean } | null {
  const code = rawCode.replace(/\s/g, '')
  if (/^\d{6}$/.test(code)) {
    const step = matchTotp(rec.secret, code, now, rec.last_step)
    return step === null ? null : { rec: { ...rec, last_step: step }, backupUsed: false }
  }
  const i = findBackupCode(rec.backup, code)
  if (i < 0) return null
  return { rec: { ...rec, backup: rec.backup.filter((_, j) => j !== i) }, backupUsed: true }
}

/** The owner's own dashboard user for the setup/enable/disable/status routes, or null after answering 403.
 *  This fork (BELA 6404 port of DOCKERHTTPS1119, upstream mayActAsOwner): a dashboard login session, or the bearer token
 *  WITH a valid login session cookie beside it (the browser keeps the token from ?token= and sends it on every call, so
 *  the auth result is 'token' although the owner is signed in); the user is the cookie's. Never the bare token, a device
 *  key, a sub-agent token or a federation peer: whoever holds only those is on the machine or the fleet, and the
 *  break-glass there is `npm run dashboard-user -- totp:off`. */
export function ownerSessionUser(auth: RouteContext['auth'], req: http.IncomingMessage): string | null {
  if (auth?.kind === 'session') return auth.user ?? null
  // A per-agent token (none in this fork today; upstream's tokenAgent) is never the owner, cookie or not.
  if (auth?.kind === 'token' && !(auth as { tokenAgent?: unknown }).tokenAgent) return sessionUserFromCookie(req)
  return null
}

function totpOwner(auth: RouteContext['auth'], req: http.IncomingMessage, res: http.ServerResponse): ReturnType<typeof getDashboardUser> | null {
  const name = ownerSessionUser(auth, req)
  const user = name ? getDashboardUser(name) : undefined
  if (!user || user.disabled) {
    noStoreJson(res, { error: 'Two-step sign-in can be changed only with the dashboard login (an owner session).', code: 'owner_session_required' }, 403)
    return null
  }
  return user
}

export async function tryHandleAuth(ctx: RouteContext): Promise<boolean> {
  const { req, res, path, method, auth } = ctx

  if (path === '/api/auth/status' && method === 'GET') {
    json(res, statusPayload(auth, req))
    return true
  }

  if (path === '/api/auth/login' && method === 'POST') {
    let body: Record<string, unknown>
    try {
      body = await parseJsonBody(req)
    } catch {
      json(res, { error: 'Invalid JSON' }, 400)
      return true
    }
    const username = str(body.username).trim()
    const password = str(body.password)
    const usernameLc = username.toLowerCase()

    const throttle = checkThrottle(usernameLc)
    if (throttle.locked) {
      if (throttle.global) logger.warn('login: global failure cap reached -- all logins throttled')
      res.writeHead(429, { 'Content-Type': 'application/json; charset=utf-8', 'Retry-After': String(throttle.retryAfterS), 'Cache-Control': 'private, no-store' })
      res.end(JSON.stringify({ error: 'Too many attempts', retry_after_s: throttle.retryAfterS }))
      return true
    }

    const user = username ? getDashboardUser(username) : undefined
    let ok = false
    if (user && !user.disabled && password) {
      ok = await verifyPassword(password, user.password_hash)
    } else {
      // Unknown user / disabled / empty input: run the dummy verify so timing
      // and lockout are identical to a real wrong-password attempt.
      await runDummyVerify(password)
    }

    if (!ok || !user) {
      recordFailure(usernameLc)
      res.writeHead(401, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'private, no-store' })
      res.end(JSON.stringify(INVALID_CREDENTIALS))
      return true
    }

    recordSuccess(usernameLc)
    // DOCKERHTTPS1119: with the second factor on, the right password gives no session, only a pending sign-in that
    // POST /api/auth/login/totp completes with a code. A damaged store refuses (fail closed), never logs in on the
    // password alone, for a user whose database flag is on. With the flag off the password is enough (the flag is the
    // source of truth, see ../dashboard-totp.ts), so a damaged file does not lock out every user.
    let totp: TotpRecord | undefined
    try {
      totp = getTotpForUser(user)
    } catch (err) {
      if (user.totp_enabled || !(err instanceof TotpStoreError)) {
        logger.error({ err: (err as Error).message }, 'login: the two-step sign-in store is unreadable, login refused')
        noStoreJson(res, TOTP_STORE_UNAVAILABLE, 503)
        return true
      }
      logger.warn({ err: err.message, username: user.username }, 'login: the two-step sign-in store is unreadable; the flag is off, password login allowed')
    }
    // DOCKERHTTPS1119 port (public-access.ts, this fork's rule): on the public address only a login with the second
    // factor on gets a session. The right password of a login without it gets none; turn it on at the internal address.
    if (!totp && isPublicRequest(req)) {
      logger.warn({ username: user.username }, 'login: refused on the public address, the two-step sign-in is off for this login')
      noStoreJson(res, PUBLIC_TOTP_REQUIRED, 403)
      return true
    }
    if (totp) {
      const pending = createPendingLogin(user.id, user.username)
      noStoreJson(res, { ...TOTP_REQUIRED, pending, expires_in_s: PENDING_LOGIN_TTL_S }, 401)
      return true
    }
    finishLogin(req, res, user, { ok: true, user: user.username })
    return true
  }

  if (path === '/api/auth/login/totp' && method === 'POST') {
    let body: Record<string, unknown>
    try {
      body = await parseJsonBody(req)
    } catch {
      json(res, { error: 'Invalid JSON' }, 400)
      return true
    }
    const pendingToken = str(body.pending)
    const pending = getPendingLogin(pendingToken)
    if (!pending) {
      noStoreJson(res, TOTP_PENDING_EXPIRED, 401)
      return true
    }
    // The code guesses have their own bucket of the shared login throttle (the username plus ':totp', which no
    // username can be: USERNAME_RE has no colon), and they count toward its global cap too.
    const totpKey = pending.username.toLowerCase() + ':totp'
    const throttle = checkThrottle(totpKey)
    if (throttle.locked) {
      if (throttle.global) logger.warn('login code: global failure cap reached -- all logins throttled')
      tooManyAttempts(res, throttle.retryAfterS)
      return true
    }
    const user = getDashboardUser(pending.username)
    let rec: TotpRecord | undefined
    try {
      rec = user && !user.disabled && user.id === pending.userId ? getTotpForUser(user) : undefined
    } catch (err) {
      logger.error({ err: (err as Error).message }, 'login code: the two-step sign-in store is unreadable, login refused')
      noStoreJson(res, TOTP_STORE_UNAVAILABLE, 503)
      return true
    }
    if (!user || !rec) {
      // The user was removed or disabled, or turned the second factor off, after the password step: sign in again.
      consumePendingLogin(pendingToken)
      noStoreJson(res, TOTP_PENDING_EXPIRED, 401)
      return true
    }
    const checked = checkSecondFactor(rec, str(body.code))
    if (!checked) {
      recordFailure(totpKey)
      noStoreJson(res, TOTP_INVALID, 401)
      return true
    }
    saveTotpRecord(user.id, checked.rec)
    recordSuccess(totpKey)
    consumePendingLogin(pendingToken)
    finishLogin(req, res, user, {
      ok: true,
      user: user.username,
      ...(checked.backupUsed ? { backup_code_used: true, backup_codes_left: checked.rec.backup.length } : {}),
    })
    return true
  }

  if (path === '/api/auth/totp' && method === 'GET') {
    const user = totpOwner(auth, req, res)
    if (!user) return true
    let rec: TotpRecord | undefined
    try {
      rec = getTotpForUser(user)
    } catch {
      noStoreJson(res, TOTP_STORE_UNAVAILABLE, 503)
      return true
    }
    noStoreJson(res, { enabled: rec !== undefined, backup_codes_left: rec ? rec.backup.length : 0 }, 200)
    return true
  }

  if (path === '/api/auth/totp/setup' && method === 'POST') {
    const user = totpOwner(auth, req, res)
    if (!user) return true
    try {
      if (getTotpForUser(user)) {
        noStoreJson(res, TOTP_ALREADY_ENABLED, 409)
        return true
      }
    } catch {
      noStoreJson(res, TOTP_STORE_UNAVAILABLE, 503)
      return true
    }
    const secret = generateTotpSecret()
    setPendingSetup(user.id, secret)
    logger.info({ username: user.username }, 'two-step sign-in: setup started')
    // The one response the secret is ever in (for the QR and for typing it in by hand).
    noStoreJson(res, { secret, otpauth_uri: otpauthUri(user.username, secret), expires_in_s: PENDING_SETUP_TTL_S }, 200)
    return true
  }

  if (path === '/api/auth/totp/enable' && method === 'POST') {
    const user = totpOwner(auth, req, res)
    if (!user) return true
    let body: Record<string, unknown>
    try {
      body = await parseJsonBody(req)
    } catch {
      json(res, { error: 'Invalid JSON' }, 400)
      return true
    }
    try {
      if (getTotpForUser(user)) {
        noStoreJson(res, TOTP_ALREADY_ENABLED, 409)
        return true
      }
    } catch {
      noStoreJson(res, TOTP_STORE_UNAVAILABLE, 503)
      return true
    }
    if (!getPendingSetup(user.id)) {
      noStoreJson(res, TOTP_SETUP_MISSING, 409)
      return true
    }
    // Dani 81107 B1: the current password too, under the login's own bucket (as disable and the password change do).
    // A stolen session cookie alone must not turn the second factor on with the thief's secret: enabling signs out the
    // owner's other sessions, and the owner could not sign in again without the thief's code.
    const usernameLc = user.username.toLowerCase()
    const pwThrottle = checkThrottle(usernameLc)
    if (pwThrottle.locked) {
      tooManyAttempts(res, pwThrottle.retryAfterS)
      return true
    }
    if (!(await verifyPassword(str(body.password), user.password_hash))) {
      recordFailure(usernameLc)
      noStoreJson(res, INVALID_CREDENTIALS, 401)
      return true
    }
    recordSuccess(usernameLc)
    // Read again after the KDF await: a parallel enable may have finished, or the pending setup expired meanwhile.
    try {
      if (getTotpForUser(getDashboardUser(user.username) ?? user)) {
        noStoreJson(res, TOTP_ALREADY_ENABLED, 409)
        return true
      }
    } catch {
      noStoreJson(res, TOTP_STORE_UNAVAILABLE, 503)
      return true
    }
    const secret = getPendingSetup(user.id)
    if (!secret) {
      noStoreJson(res, TOTP_SETUP_MISSING, 409)
      return true
    }
    const step = matchTotp(secret, str(body.code).replace(/\s/g, ''), Date.now(), -1)
    if (step === null) {
      noStoreJson(res, TOTP_INVALID, 400)
      return true
    }
    const backupCodes = generateBackupCodes()
    // The file first, then the database flag: a failure between them leaves a record without the flag, which still
    // reads as ON (getTotpForUser), never as off.
    saveTotpRecord(user.id, { secret, last_step: step, backup: backupCodes.map(hashBackupCode), enabled_at: Math.floor(Date.now() / 1000) })
    setDashboardUserTotpEnabled(user.id, true)
    clearPendingSetup(user.id)
    // The sessions signed in before the second factor existed did not pass it: end them, keep the caller's own.
    revokeAllForUser(user.id, parseCookies(req.headers.cookie)[SESSION_COOKIE_NAME] ?? undefined)
    logConfigChange('security.totp_enable', null, user.username, auth!.kind)
    logger.info({ username: user.username }, 'two-step sign-in: enabled')
    // The one response the backup codes are ever in; only their hashes are kept.
    noStoreJson(res, { ok: true, enabled: true, backup_codes: backupCodes }, 200)
    return true
  }

  if (path === '/api/auth/totp/disable' && method === 'POST') {
    const user = totpOwner(auth, req, res)
    if (!user) return true
    let body: Record<string, unknown>
    try {
      body = await parseJsonBody(req)
    } catch {
      json(res, { error: 'Invalid JSON' }, 400)
      return true
    }
    let rec: TotpRecord | undefined
    try {
      rec = getTotpForUser(user)
    } catch {
      noStoreJson(res, TOTP_STORE_UNAVAILABLE, 503)
      return true
    }
    if (!rec) {
      noStoreJson(res, TOTP_NOT_ENABLED, 409)
      return true
    }
    const code = str(body.code)
    if (!code.trim()) {
      noStoreJson(res, TOTP_CODE_REQUIRED, 400)
      return true
    }
    // The password, under the login's own bucket (as the password change does), then the code under ':totp'.
    const usernameLc = user.username.toLowerCase()
    const pwThrottle = checkThrottle(usernameLc)
    if (pwThrottle.locked) {
      tooManyAttempts(res, pwThrottle.retryAfterS)
      return true
    }
    if (!(await verifyPassword(str(body.password), user.password_hash))) {
      recordFailure(usernameLc)
      noStoreJson(res, INVALID_CREDENTIALS, 401)
      return true
    }
    recordSuccess(usernameLc)
    const totpKey = usernameLc + ':totp'
    const codeThrottle = checkThrottle(totpKey)
    if (codeThrottle.locked) {
      tooManyAttempts(res, codeThrottle.retryAfterS)
      return true
    }
    // Re-read after the KDF await: a concurrent sign-in may have moved last_step or used a backup code.
    try {
      rec = getTotpForUser(user)
    } catch {
      noStoreJson(res, TOTP_STORE_UNAVAILABLE, 503)
      return true
    }
    if (!rec) {
      noStoreJson(res, TOTP_NOT_ENABLED, 409)
      return true
    }
    if (!checkSecondFactor(rec, code)) {
      recordFailure(totpKey)
      noStoreJson(res, TOTP_INVALID, 401)
      return true
    }
    recordSuccess(totpKey)
    // The flag first, then the record: a failure between them leaves a record without the flag, which still reads as
    // ON, never a flag without a record (that would be a 503 lockout).
    setDashboardUserTotpEnabled(user.id, false)
    removeTotpRecord(user.id)
    logConfigChange('security.totp_disable', null, user.username, auth!.kind)
    logger.info({ username: user.username }, 'two-step sign-in: disabled')
    // The same owner ping as the machine break-glass (dashboard-user totp:off).
    void notifySecurityEvent(totpDisabledNotice(user.username))
    noStoreJson(res, { ok: true, enabled: false }, 200)
    return true
  }

  if (path === '/api/auth/logout' && method === 'POST') {
    const presented = parseCookies(req.headers.cookie)[SESSION_COOKIE_NAME]
    if (presented) revokeSession(presented)
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Set-Cookie': clearCookie(req), 'Cache-Control': 'private, no-store' })
    res.end(JSON.stringify({ ok: true }))
    return true
  }

  if (path === '/api/auth/logout-all' && method === 'POST') {
    if (auth?.kind !== 'session' || !auth.user) {
      json(res, { error: 'Session required' }, 400)
      return true
    }
    const user = getDashboardUser(auth.user)
    if (user) revokeAllForUser(user.id)
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Set-Cookie': clearCookie(req), 'Cache-Control': 'private, no-store' })
    res.end(JSON.stringify({ ok: true }))
    return true
  }

  if (path === '/api/auth/sessions' && method === 'GET') {
    if (auth?.kind !== 'session' || !auth.user) {
      json(res, { error: 'Session required' }, 400)
      return true
    }
    const user = getDashboardUser(auth.user)
    if (!user) {
      json(res, { error: 'User not found' }, 404)
      return true
    }
    json(res, { sessions: listUserSessions(user.id) })
    return true
  }

  if (path === '/api/auth/password' && method === 'POST') {
    let body: Record<string, unknown>
    try {
      body = await parseJsonBody(req)
    } catch {
      json(res, { error: 'Invalid JSON' }, 400)
      return true
    }
    const newPassword = str(body.new_password)

    let user = undefined as ReturnType<typeof getDashboardUser>
    if (auth?.kind === 'session' && auth.user) {
      user = getDashboardUser(auth.user)
      if (!user) {
        json(res, { error: 'User not found' }, 404)
        return true
      }
      // Session callers must prove knowledge of the current password.
      const current = str(body.current_password)
      if (!(await verifyPassword(current, user.password_hash))) {
        json(res, INVALID_CREDENTIALS, 401)
        return true
      }
    } else if (auth?.kind === 'token') {
      // Bearer break-glass: may omit current_password but must name the target.
      // EXPLICITLY token-only -- a reset without the current password is the
      // strongest action here and must never leak to weaker credential kinds.
      const username = str(body.username)
      user = username ? getDashboardUser(username) : undefined
      if (!user) {
        json(res, { error: 'User not found' }, 404)
        return true
      }
    } else {
      json(res, FORBIDDEN_KIND, 403)
      return true
    }

    try {
      assertPasswordPolicy(newPassword)
    } catch (err) {
      json(res, { error: err instanceof PasswordPolicyError ? err.message : 'Invalid password' }, 400)
      return true
    }
    const hash = await hashPassword(newPassword)
    updateDashboardUserPassword(user.id, hash)
    // Revoke every other session of this user; keep the caller's own if present.
    const presented = parseCookies(req.headers.cookie)[SESSION_COOKIE_NAME]
    revokeAllForUser(user.id, presented ?? undefined)
    if (auth?.kind === 'token') {
      // Break-glass reset (no current_password proven): leave a durable audit
      // row and ping the operator's channel. Only the username goes into the
      // trail, never credential material. Fire-and-forget: the reset must
      // succeed with or without a wired channel (notifySecurityEvent is
      // silent when none is configured).
      logConfigChange('security.break_glass_password_reset', null, user.username, 'token')
      logger.warn({ username: user.username }, 'break-glass password reset via bearer token')
      void notifySecurityEvent(`🔑 Break-glass jelszó-reset a dashboardon: "${user.username}" jelszavát a hozzáférési tokennel állították át. Ha nem te voltál, futtasd: npm run dashboard-user -- security:reset`)
    }
    json(res, { ok: true })
    return true
  }

  if (path === '/api/auth/users' && method === 'GET') {
    if (!kindAllowed(auth, USER_ADMIN_KINDS)) {
      json(res, FORBIDDEN_KIND, 403)
      return true
    }
    json(res, { users: listDashboardUsers() })
    return true
  }

  if (path === '/api/auth/users' && method === 'POST') {
    if (!kindAllowed(auth, USER_ADMIN_KINDS)) {
      json(res, FORBIDDEN_KIND, 403)
      return true
    }
    let body: Record<string, unknown>
    try {
      body = await parseJsonBody(req)
    } catch {
      json(res, { error: 'Invalid JSON' }, 400)
      return true
    }
    const username = str(body.username).trim()
    const password = str(body.password)
    if (!USERNAME_RE.test(username)) {
      json(res, { error: 'Invalid username (1-64 chars: letters, digits, . _ -)' }, 400)
      return true
    }
    if (getDashboardUser(username)) {
      json(res, { error: 'User already exists' }, 409)
      return true
    }
    try {
      assertPasswordPolicy(password)
    } catch (err) {
      json(res, { error: err instanceof PasswordPolicyError ? err.message : 'Invalid password' }, 400)
      return true
    }
    const hash = await hashPassword(password)
    const created = createDashboardUser(username, hash)
    logger.info({ username: created.username }, 'dashboard user created')
    json(res, { ok: true, user: { id: created.id, username: created.username } }, 201)
    return true
  }

  if (path === '/api/auth/device-keys' && method === 'GET') {
    if (!kindAllowed(auth, DEVICE_KEY_ADMIN_KINDS)) {
      json(res, FORBIDDEN_KIND, 403)
      return true
    }
    json(res, { keys: listDeviceKeys() })
    return true
  }

  if (path === '/api/auth/device-keys' && method === 'POST') {
    if (!kindAllowed(auth, DEVICE_KEY_ADMIN_KINDS)) {
      json(res, FORBIDDEN_KIND, 403)
      return true
    }
    let body: Record<string, unknown>
    try {
      body = await parseJsonBody(req)
    } catch {
      json(res, { error: 'Invalid JSON' }, 400)
      return true
    }
    const name = str(body.name).trim()
    if (!DEVICE_KEY_NAME_RE.test(name)) {
      json(res, { error: 'Invalid device name (1-64 chars: letters, digits, space, . _ -)' }, 400)
      return true
    }
    // Expiry is opt-in: absent/0 means the key lives until revoked.
    let expiresInDays: number | undefined
    if (body.expires_in_days !== undefined && body.expires_in_days !== null && body.expires_in_days !== 0) {
      const n = Number(body.expires_in_days)
      if (!Number.isFinite(n) || n <= 0 || n > DEVICE_KEY_MAX_EXPIRY_DAYS) {
        json(res, { error: `Invalid expires_in_days (1-${DEVICE_KEY_MAX_EXPIRY_DAYS})` }, 400)
        return true
      }
      expiresInDays = n
    }
    const minted = createDeviceKey(name, { expiresInDays })
    logger.info({ id: minted.id, name: minted.name, expiresAt: minted.expiresAt }, 'device key minted')
    // `key` is the one and only disclosure of the raw credential.
    json(res, { ok: true, id: minted.id, name: minted.name, key: minted.key, created_at: minted.createdAt, expires_at: minted.expiresAt }, 201)
    return true
  }

  const deviceKeyMatch = /^\/api\/auth\/device-keys\/(\d+)$/.exec(path)
  if (deviceKeyMatch && method === 'DELETE') {
    if (!kindAllowed(auth, DEVICE_KEY_ADMIN_KINDS)) {
      json(res, FORBIDDEN_KIND, 403)
      return true
    }
    const id = Number(deviceKeyMatch[1])
    const key = getDeviceKey(id)
    if (!key || !revokeDeviceKey(id)) {
      json(res, { error: 'Device key not found' }, 404)
      return true
    }
    // Bridge-paired key: revoke means BOTH halves at once -- the dashboard
    // key (above) and the SSH authorized_keys line. Idempotent on the SSH
    // side; a failure there must not resurrect the already-revoked key, so it
    // is reported, not rolled back.
    let sshRemoved: boolean | undefined
    if (key.installId) {
      try {
        sshRemoved = await removeBridgeSshAccess(key.installId)
      } catch (err) {
        logger.error({ err, id, installId: key.installId }, 'device key revoked but authorized_keys removal failed')
        sshRemoved = false
      }
      logConfigChange('security.bridge_revoke', null, `${key.name} (${key.installId}) ssh_removed=${sshRemoved}${sshDirOverride() ? ' sshdir_override=1' : ''}`, auth!.kind)
    }
    logger.info({ id, name: key.name, installId: key.installId ?? undefined, sshRemoved }, 'device key revoked')
    json(res, { ok: true, ...(key.installId ? { ssh_removed: sshRemoved } : {}) })
    return true
  }

  const delMatch = /^\/api\/auth\/users\/([^/]+)$/.exec(path)
  if (delMatch && method === 'DELETE') {
    if (!kindAllowed(auth, USER_ADMIN_KINDS)) {
      json(res, FORBIDDEN_KIND, 403)
      return true
    }
    const username = decodeURIComponent(delMatch[1]!)
    const user = getDashboardUser(username)
    if (!user) {
      json(res, { error: 'User not found' }, 404)
      return true
    }
    deleteDashboardUser(username)
    revokeAllForUser(user.id)
    // DOCKERHTTPS1119: the removed login's second-factor secret goes with it. A damaged store is not a reason to
    // keep the login: logged, the deletion stands.
    try { removeTotpRecord(user.id) } catch (err) { logger.warn({ err: (err as Error).message }, 'dashboard user deleted, its two-step record could not be removed') }
    // Deleting the last user is the documented way to return to token-only mode.
    logger.info({ username, remaining: countDashboardUsers(true) }, 'dashboard user deleted')
    json(res, { ok: true })
    return true
  }

  return false
}

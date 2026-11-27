// DOCKERHTTPS1119 port (BELA 6404): the optional public listener behind a local HTTPS proxy, OFF by default, usable
// only together with the TOTP second factor. Real sockets for the listener, a real temp database for the gate.
import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest'
import http from 'node:http'
import { Readable } from 'node:stream'
import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

vi.mock('../notify.js', () => ({
  notifyChannel: vi.fn(async () => {}),
  notifyTelegram: vi.fn(async () => {}),
  notifySecurityEvent: vi.fn(async () => {}),
  notifyOwner: vi.fn(async () => {}),
}))

import { initDatabase, getDb, createDashboardUser, getDashboardUser, setDashboardUserTotpEnabled, listDashboardUserTotpState } from '../db.js'
import { hashPassword } from '../web/password-hash.js'
import { createSession, _clearSessionCacheForTest } from '../web/auth-sessions.js'
import { resolveAuth, SESSION_COOKIE_NAME } from '../web/auth-gate.js'
import { tryHandleAuth } from '../web/routes/auth.js'
import { _resetThrottleForTest } from '../web/login-throttle.js'
import { _setTotpStorePathForTest, _resetTotpStateForTest, saveTotpRecord } from '../web/dashboard-totp.js'
import { generateTotpSecret, hotp, base32Decode, totpStepAt } from '../web/totp.js'
import {
  publicListenerPort, publicListenerDecision, publicPathOpenWithoutSession, markPublicRequest, isPublicRequest,
  openPublicListener, PUBLIC_LISTENER_HOST,
} from '../web/public-access.js'
import type { RouteContext } from '../web/routes/types.js'

const TOKEN = 'tok-' + 'x'.repeat(40)
const PW = 'super-secret-pw'
const dir = mkdtempSync(join(tmpdir(), 'pubhttps-'))

beforeAll(() => {
  initDatabase(join(dir, 'db.sqlite'))
  _setTotpStorePathForTest(join(dir, 'totp.json'))
})
beforeEach(() => {
  getDb().exec('DELETE FROM dashboard_users; DELETE FROM auth_sessions')
  _clearSessionCacheForTest()
  _resetThrottleForTest()
  _resetTotpStateForTest()
})

function fakeReq(opts: { headers?: Record<string, string>; public?: boolean; body?: unknown } = {}): http.IncomingMessage {
  const payload = opts.body === undefined ? [] : [Buffer.from(JSON.stringify(opts.body))]
  const req = Readable.from(payload) as unknown as http.IncomingMessage
  req.headers = (opts.headers ?? {}) as http.IncomingHttpHeaders
  if (opts.public) markPublicRequest(req)
  return req
}

let lastSecret = ''
async function seed(username = 'alice', totp = false): Promise<number> {
  const u = createDashboardUser(username, await hashPassword(PW))
  if (totp) {
    lastSecret = generateTotpSecret()
    saveTotpRecord(u.id, { secret: lastSecret, last_step: 0, backup: [], enabled_at: 1 })
    setDashboardUserTotpEnabled(u.id, true)
  }
  return u.id
}

const auth = (req: http.IncomingMessage, path = '/api/x', method = 'GET', url = `http://127.0.0.1${path}`) =>
  resolveAuth(req, new URL(url), path, method, TOKEN)

describe('configuration: off by default, no default port', () => {
  it('WEB_PUBLIC_PORT: unset, empty, garbage, 0 and out of range give null; a number gives the port', () => {
    for (const v of [undefined, '', ' ', 'abc', '0', '70000', '34-21', '3421x']) expect(publicListenerPort(v)).toBeNull()
    expect(publicListenerPort('3999')).toBe(3999)
    expect(publicListenerPort(' 4100 ')).toBe(4100)
  })
  it('the decision: not configured (unset or empty) is the default and is silent', () => {
    expect(publicListenerDecision({ publicPortRaw: undefined, internalPort: 3420, publicUrl: '', users: [] })).toEqual({ start: false, reason: 'not-configured' })
    for (const raw of ['', '  ']) expect(publicListenerDecision({ publicPortRaw: raw, internalPort: 3420, publicUrl: 'https://a.example', users: [{ username: 'a', disabled: 0, totp_enabled: 1 }] })).toEqual({ start: false, reason: 'not-configured' })
  })
  it('KISPROGI 6416 K2: a WEB_PUBLIC_PORT that is set but not a port is refused WITH a reason (logged), not silently', () => {
    for (const raw of [':3500', '0', '70000', 'abc', '34-21']) {
      const d = publicListenerDecision({ publicPortRaw: raw, internalPort: 3420, publicUrl: 'https://a.example', users: [{ username: 'a', disabled: 0, totp_enabled: 1 }] })
      expect([raw, d.start, (d as { reason?: string }).reason]).toEqual([raw, false, 'bad-port'])
      expect((d as { detail?: string }).detail).toContain('WEB_PUBLIC_PORT')
    }
  })
  it('refused with a reason: the same port, a non-https public URL, no login, a login without TOTP', () => {
    const ok = [{ username: 'a', disabled: 0, totp_enabled: 1 }]
    const d = (o: Partial<Parameters<typeof publicListenerDecision>[0]>) => publicListenerDecision({ publicPortRaw: '3999', internalPort: 3420, publicUrl: 'https://dash.example.org', users: ok, ...o })
    expect(d({ publicPortRaw: '3420' }).start).toBe(false)
    expect((d({ publicPortRaw: '3420' }) as { reason: string }).reason).toBe('same-port')
    for (const u of ['', 'http://dash.example.org', 'not a url', 'ftp://x']) expect((d({ publicUrl: u }) as { reason: string }).reason).toBe('bad-public-url')
    expect((d({ users: [] }) as { reason: string }).reason).toBe('no-login')
    expect((d({ users: [{ username: 'a', disabled: 1, totp_enabled: 1 }] }) as { reason: string }).reason).toBe('no-login')
    const miss = d({ users: [{ username: 'a', disabled: 0, totp_enabled: 1 }, { username: 'b', disabled: 0, totp_enabled: 0 }] })
    expect(miss).toEqual({ start: false, reason: 'totp-missing', detail: 'two-step sign-in is off for: b' })
    expect(d({ users: [{ username: 'a', disabled: 0, totp_enabled: 1 }, { username: 'b', disabled: 1, totp_enabled: 0 }] })).toEqual({ start: true, port: 3999 })
  })
  it('the users come from the database flags (listDashboardUserTotpState: usernames and flags only, no hash)', async () => {
    await seed('alice', true); await seed('bob', false)
    const rows = listDashboardUserTotpState()
    expect(rows).toEqual([{ username: 'alice', disabled: 0, totp_enabled: 1 }, { username: 'bob', disabled: 0, totp_enabled: 0 }])
    expect(JSON.stringify(rows)).not.toContain('password')
  })
})

describe('the gate on the public listener', () => {
  it('the bearer token, a ?token= on the SSE stream and a device-key bearer are not looked at there', async () => {
    await seed('alice', true)
    expect(auth(fakeReq({ headers: { authorization: `Bearer ${TOKEN}` } })).kind).toBe('token') // internal: unchanged
    expect(auth(fakeReq({ headers: { authorization: `Bearer ${TOKEN}` }, public: true })).kind).toBe('none')
    const sse = '/api/agents/x/pane/stream'
    expect(auth(fakeReq({ public: true }), sse, 'GET', `http://127.0.0.1${sse}?token=${TOKEN}`).kind).toBe('none')
    expect(auth(fakeReq({ headers: { authorization: 'Bearer mvdk_anything' }, public: true })).kind).toBe('none')
  })
  it('a login session counts only while its user has the second factor on (and is not disabled)', async () => {
    const id = await seed('alice', false)
    const tok = createSession({ userId: id, username: 'alice' }, { userAgent: null, remoteNote: 'loopback' })
    const cookie = { cookie: `${SESSION_COOKIE_NAME}=${tok}` }
    expect(auth(fakeReq({ headers: cookie }))).toEqual({ kind: 'session', user: 'alice' }) // internal: unchanged
    expect(auth(fakeReq({ headers: cookie, public: true })).kind).toBe('none') // no TOTP: not on the public address
    setDashboardUserTotpEnabled(id, true)
    expect(auth(fakeReq({ headers: cookie, public: true }))).toEqual({ kind: 'session', user: 'alice' })
    setDashboardUserTotpEnabled(id, false) // turned off later: shut out at once
    expect(auth(fakeReq({ headers: cookie, public: true })).kind).toBe('none')
    setDashboardUserTotpEnabled(id, true)
    getDb().prepare('UPDATE dashboard_users SET disabled = 1 WHERE id = ?').run(id)
    expect(auth(fakeReq({ headers: cookie, public: true })).kind).toBe('none')
  })
  it('without a session only the closed list is open; the static UI stays reachable', () => {
    expect(publicPathOpenWithoutSession('/api/auth/status', 'GET')).toBe(true)
    expect(publicPathOpenWithoutSession('/api/auth/login', 'POST')).toBe(true)
    expect(publicPathOpenWithoutSession('/api/auth/login/totp', 'POST')).toBe(true)
    for (const [p, m] of [['/api/marveen/avatar', 'GET'], ['/api/agents/x/avatar', 'GET'], ['/api/auth/login', 'GET'], ['/api/auth/totp/setup', 'POST'], ['/.well-known/fleetq', 'GET'], ['/api/messages', 'POST']]) {
      expect([p, m, publicPathOpenWithoutSession(p!, m!)]).toEqual([p, m, false])
    }
    for (const p of ['/', '/index.html', '/app.js', '/style.css', '/lang/hu.js', '/manifest.json']) expect(publicPathOpenWithoutSession(p, 'GET')).toBe(true)
  })
})

describe('the login on the public listener', () => {
  async function login(pub: boolean, username = 'alice', path = '/api/auth/login', body: unknown = { username, password: PW }) {
    const req = fakeReq({ public: pub, body })
    const out: { status: number; headers: Record<string, unknown>; body: string } = { status: 0, headers: {}, body: '' }
    const res = {
      writeHead(s: number, h?: Record<string, unknown>) { out.status = s; Object.assign(out.headers, h ?? {}); return this },
      setHeader(k: string, v: unknown) { out.headers[k] = v },
      end(d?: string) { if (d) out.body += d },
    } as unknown as http.ServerResponse
    const ctx: RouteContext = { req, res, path, method: 'POST', url: new URL(`http://127.0.0.1${path}`), auth: undefined }
    await tryHandleAuth(ctx)
    return out
  }
  it('a login without the second factor: the right password gets 403 public_totp_required and NO cookie there; internally unchanged', async () => {
    await seed('alice', false)
    const pub = await login(true)
    expect(pub.status).toBe(403)
    expect(JSON.parse(pub.body).code).toBe('public_totp_required')
    expect(pub.headers['Set-Cookie']).toBeUndefined()
    const internal = await login(false)
    expect(internal.status).toBe(200)
    expect(String(internal.headers['Set-Cookie'])).not.toContain('Secure') // plain http loopback: no Secure (Safari)
  })
  it('a login with the second factor: the password step gives the pending token on the public address too', async () => {
    await seed('alice', true)
    const pub = await login(true)
    expect(pub.status).toBe(401)
    expect(JSON.parse(pub.body).code).toBe('totp_required')
    // the code step completes it, and the cookie is Secure there (behind the HTTPS proxy), with no X-Forwarded-Proto
    const done = await login(true, 'alice', '/api/auth/login/totp', { pending: JSON.parse(pub.body).pending, code: hotp(base32Decode(lastSecret), totpStepAt(Date.now())) })
    expect(done.status).toBe(200)
    expect(String(done.headers['Set-Cookie'])).toMatch(/; Secure$/)
  })
})

describe('the listener itself (real sockets)', () => {
  let internal: http.Server
  let pub: http.Server | null = null
  const seen: boolean[] = []
  const log = { warn: vi.fn(), error: vi.fn() }
  beforeEach(async () => {
    seen.length = 0
    internal = http.createServer((req, res) => { seen.push(isPublicRequest(req)); res.end('ok') })
    await new Promise<void>((r) => internal.listen(0, '127.0.0.1', r))
  })
  afterEach(async () => {
    if (pub) await new Promise((r) => pub!.close(r))
    pub = null
    await new Promise((r) => internal.close(r))
  })
  const get = (port: number) => new Promise<string>((resolve, reject) => {
    http.get({ host: '127.0.0.1', port, path: '/' }, (res) => { let b = ''; res.on('data', (d) => (b += d)); res.on('end', () => resolve(b)) }).on('error', reject)
  })
  it('binds 127.0.0.1 only; its requests are marked public, the internal ones are not', async () => {
    const probe = http.createServer(); await new Promise<void>((r) => probe.listen(0, '127.0.0.1', r))
    const port = (probe.address() as { port: number }).port; await new Promise((r) => probe.close(r))
    pub = openPublicListener(internal, port, log, http.createServer)
    await new Promise((r) => pub!.once('listening', r))
    expect(PUBLIC_LISTENER_HOST).toBe('127.0.0.1')
    expect((pub.address() as { address: string }).address).toBe('127.0.0.1')
    expect(await get(port)).toBe('ok')
    expect(await get((internal.address() as { port: number }).port)).toBe('ok')
    expect(seen).toEqual([true, false])
  })
  it('a taken port: logged, no throw, the internal server keeps serving', async () => {
    const taken = (internal.address() as { port: number }).port
    pub = openPublicListener(internal, taken, log, http.createServer)
    await new Promise((r) => setTimeout(r, 100))
    expect(log.error).toHaveBeenCalled()
    expect(await get(taken)).toBe('ok')
    pub = null
  })
})

describe('web.ts wiring (source pins)', () => {
  const WEB = readFileSync(join(__dirname, '..', 'web.ts'), 'utf-8')
  it('the listener opens only on a start decision, only where the services run, via openPublicListener', () => {
    // KISPROGI 6416 H1: the public listener's own key, never DASHBOARD_PUBLIC_URL (the agents' API address)
    expect(WEB).toContain('const decision = publicListenerDecision({ publicPortRaw: WEB_PUBLIC_PORT_RAW, internalPort: port, publicUrl: WEB_PUBLIC_URL, users })')
    expect(WEB).toContain("...(WEB_PUBLIC_URL ? [WEB_PUBLIC_URL.replace(/\\/$/, '')] : []),")
    expect(WEB).not.toMatch(/publicUrl: DASHBOARD_PUBLIC_URL/)
    expect(WEB).toContain('if (decision.start && servicesOff) {')
    expect(WEB).toContain('publicServer = openPublicListener(server, decision.port, logger, http.createServer)')
    expect(WEB.match(/openPublicListener\(/g)?.length).toBe(1)
    expect(WEB).not.toMatch(/listen\([^)]*0\.0\.0\.0/)
  })
  it('the closed list: 404 only for paths open on 3420; gated paths keep their 401', () => {
    expect(WEB).toContain("if (isPublicRequest(req) && auth.kind === 'none' && !requiresAuth(path, method) && !publicPathOpenWithoutSession(path, method)) {")
  })
  it('the user lookup the gate does on the public listener exists', () => {
    expect(getDashboardUser).toBeTypeOf('function')
  })
})

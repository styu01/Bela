// DOCKERHTTPS1119 (upstream 139ac6db / 3a8b912b, ported to this systemd install, BELA 6404): the optional public
// HTTPS access path of the dashboard. OFF by default, and off it adds no listener at all.
//
// How it works when the owner turns it on: a reverse proxy on THIS machine (Caddy, docs/public-https.md) terminates TLS
// for the public host name and forwards to a SECOND listener of the dashboard, WEB_PUBLIC_PORT, bound to 127.0.0.1
// only (never 0.0.0.0, never a LAN address: the proxy is local). The usual 127.0.0.1:3420 stays the internal path
// (the agents, the bearer-token API, an SSH tunnel), unchanged. Which listener a request came in on is a fact of the
// socket, not of a header a client could forge (X-Forwarded-*) or of the source address (the proxy is local, so every
// public request comes from 127.0.0.1 too).
//
// On the public listener the gate accepts ONLY a browser login session (auth-gate.ts resolveAuth): no shared token, no
// device key, no SSE ?token=, no federation token. Without a session it reaches only a closed list of paths (the
// login itself and the auth status); every other /api path answers 404 there. Its cookies are always Secure.
//
// This fork's own rule (Istvan 2026-11-26, BELA 6404): the public HTTPS access is usable ONLY together with the TOTP
// second factor. Enforced three times:
//   1. at start: the listener does not open unless every enabled dashboard login has the second factor on
//      (publicListenerDecision);
//   2. at the password login on the public listener: a login without the second factor gets no session
//      (routes/auth.ts, code public_totp_required);
//   3. on every public request: a session counts only while its user still has the second factor on (resolveAuth),
//      so turning it off later shuts that user out of the public address at once.
//
// Not ported from upstream: the one-time setup code and the setup session (139ac6db, 335c1df3). They exist for a
// fresh Docker install whose first login is created over the internet; this install already has its login, and a
// login created over the public address could not be used there anyway (rule 2). With no enabled login the public
// listener does not open (rule 1).

import type http from 'node:http'

/** The only address the public listener binds: the reverse proxy runs on this machine. */
export const PUBLIC_LISTENER_HOST = '127.0.0.1'

const publicRequests = new WeakSet<http.IncomingMessage>()

/** Called by the public listener for each of its requests, before the shared handler runs. */
export function markPublicRequest(req: http.IncomingMessage): void {
  publicRequests.add(req)
}

/** True when the request arrived on the public listener (the reverse proxy's upstream). */
export function isPublicRequest(req: http.IncomingMessage | undefined): boolean {
  return req !== undefined && publicRequests.has(req)
}

/** WEB_PUBLIC_PORT as a port number, or null (unset, empty, malformed, out of range). There is NO default: an unset
 *  value means no public listener. (3421, upstream's default, is taken on this machine by another local service.) */
export function publicListenerPort(raw: string | undefined): number | null {
  const v = (raw ?? '').trim()
  if (!/^\d{1,5}$/.test(v)) return null
  const n = Number(v)
  return n >= 1 && n <= 65535 ? n : null
}

/** Paths a request with NO principal may reach on the public listener (upstream Dani 81108 N1). web.ts answers 404
 *  for any other path that requiresAuth() leaves open on 3420 (avatars, a future exception), so it does not reach the
 *  internet by itself; gated paths keep their 401 (the login overlay keys on it). Non-/api paths (the static UI:
 *  index, app.js, style.css, lang, manifest) stay reachable, the login page needs them. */
export function publicPathOpenWithoutSession(path: string, method: string): boolean {
  if (!path.startsWith('/api/') && path !== '/.well-known/fleetq') return true
  if (path === '/api/auth/status' && method === 'GET') return true
  if (path === '/api/auth/login' && method === 'POST') return true
  if (path === '/api/auth/login/totp' && method === 'POST') return true
  return false
}

export interface PublicListenerUser { username: string; disabled: number; totp_enabled?: number }

export type PublicListenerDecision =
  | { start: false; reason: 'not-configured' }
  | { start: false; reason: 'same-port' | 'bad-public-url' | 'no-login' | 'totp-missing'; detail: string }
  | { start: true; port: number }

/** Whether the public listener may open, and why not. Pure over its inputs.
 *  - not configured (no WEB_PUBLIC_PORT): no listener, and nothing to tell (the default);
 *  - the same port as the internal listener, or a DASHBOARD_PUBLIC_URL that is not https://: refused, told;
 *  - no enabled dashboard login, or any enabled login without the second factor: refused, told (this fork's rule:
 *    the public address only together with TOTP). */
export function publicListenerDecision(opts: {
  publicPortRaw: string | undefined
  internalPort: number
  publicUrl: string
  users: readonly PublicListenerUser[]
}): PublicListenerDecision {
  const port = publicListenerPort(opts.publicPortRaw)
  if (port === null) return { start: false, reason: 'not-configured' }
  if (port === opts.internalPort) return { start: false, reason: 'same-port', detail: `WEB_PUBLIC_PORT (${port}) equals the internal WEB_PORT` }
  let url: URL | null = null
  try { url = new URL(opts.publicUrl) } catch { url = null }
  if (!url || url.protocol !== 'https:' || !url.hostname) {
    return { start: false, reason: 'bad-public-url', detail: 'DASHBOARD_PUBLIC_URL must be the https:// address the proxy serves' }
  }
  const enabled = opts.users.filter((u) => !u.disabled)
  if (enabled.length === 0) return { start: false, reason: 'no-login', detail: 'no enabled dashboard login exists' }
  const without = enabled.filter((u) => u.totp_enabled !== 1).map((u) => u.username)
  if (without.length) return { start: false, reason: 'totp-missing', detail: `two-step sign-in is off for: ${without.join(', ')}` }
  return { start: true, port }
}

export interface PublicListenerLogger {
  warn: (obj: object, msg: string) => void
  error: (obj: object, msg: string) => void
}

/** Opens the public listener: a second http.Server on PUBLIC_LISTENER_HOST:port whose requests are marked public and
 *  then handed to the internal server's own 'request' handler. A listen error (the port taken, ...) is logged and the
 *  listener stays down; it never throws and never touches the internal server. */
export function openPublicListener(internal: http.Server, port: number, log: PublicListenerLogger, createServer: typeof http.createServer): http.Server {
  const ps = createServer((req, res) => {
    markPublicRequest(req)
    internal.emit('request', req, res)
  })
  ps.on('error', (err) => log.error({ err, port }, 'public listener error; it stays down, the dashboard keeps running'))
  ps.listen(port, PUBLIC_LISTENER_HOST, () => {
    log.warn({ port, host: PUBLIC_LISTENER_HOST }, 'Web dashboard: PUBLIC listener open (login session with two-step sign-in only)')
  })
  return ps
}

// Route-level test helpers for the vault main-only rule (VAULTMAINONLY): the fleet's callers, each holding one client
// socket in its own scope on the in-memory /proc, and a fake request / response pair for the route handlers.
import { EventEmitter } from 'node:events'
import { expect } from 'vitest'
import type { RouteContext } from '../../web/routes/types.js'
import type { CallerIdentityDeps } from '../../web/caller-identity.js'
import { fakeDeps, MAIN, KISPROGI, WORKER_SHELL, PANES, type FakeProc } from './fake-proc.js'

export const CALLERS: Record<string, FakeProc & { port: number }> = {
  main: { pid: 9101, ppid: 775, comm: 'curl', cgroup: MAIN.cgroup, sockets: ['501'], port: 51001 },
  mainDetached: { pid: 9102, ppid: 1, comm: 'node', cgroup: MAIN.cgroup, sockets: ['502'], port: 51002 },
  kisprogi: { pid: 9103, ppid: 2725, comm: 'curl', cgroup: KISPROGI.cgroup, sockets: ['503'], port: 51003 },
  kisprogiSetsid: { pid: 9104, ppid: 1, comm: 'python3', cgroup: KISPROGI.cgroup, sockets: ['504'], port: 51004 },
  worker: { pid: 9105, ppid: 1799, comm: 'claude', cgroup: WORKER_SHELL.cgroup, sockets: ['505'], port: 51005 },
  webchat: { pid: 9106, ppid: 1, comm: 'python3', cgroup: '/user.slice/user-1000.slice/user@1000.service/app.slice/bela-webchat.service', sockets: ['506'], port: 51006 },
}
export const RELAY_PORT = 51007 // a uid 0 socket (the WSL relay)

export function fleetDeps(): CallerIdentityDeps {
  const socks = [
    ...Object.values(CALLERS).map((c) => ({ clientPort: c.port, serverPort: 3420, uid: 1000, inode: c.sockets![0] })),
    { clientPort: RELAY_PORT, serverPort: 3420, uid: 0, inode: '507' },
  ]
  return fakeDeps([MAIN, KISPROGI, WORKER_SHELL, ...Object.values(CALLERS)], socks, PANES)
}

export type Auth = RouteContext['auth']
export interface CallOptions { port?: number; auth?: Auth; body?: unknown; headers?: Record<string, string> }

export function ctxFor(method: string, path: string, opts: CallOptions = {}) {
  const req = new EventEmitter() as unknown as RouteContext['req'] & { destroy(): void }
  ;(req as unknown as { headers: Record<string, string> }).headers = opts.headers ?? {}
  ;(req as { destroy(): void }).destroy = () => {}
  ;(req as unknown as { socket: unknown }).socket = { remoteAddress: '127.0.0.1', remotePort: opts.port, localPort: 3420 }
  const state = { statusCode: 0, body: '' }
  const res = {
    writeHead(code: number) { state.statusCode = code; return res },
    end(data?: unknown) { state.body = String(data ?? '') },
    setHeader() {},
  } as unknown as RouteContext['res']
  process.nextTick(() => {
    ;(req as unknown as EventEmitter).emit('data', Buffer.from(opts.body !== undefined ? JSON.stringify(opts.body) : ''))
    ;(req as unknown as EventEmitter).emit('end')
  })
  const url = new URL(`http://localhost${path}`)
  const ctx: RouteContext = { req, res, path: url.pathname, method, url, fedPeer: null, auth: opts.auth ?? { kind: 'token' } }
  return { ctx, state }
}

export async function call(handler: (c: RouteContext) => Promise<boolean>, method: string, path: string, opts: CallOptions = {}) {
  const { ctx, state } = ctxFor(method, path, opts)
  expect(await handler(ctx)).toBe(true)
  let body: unknown = null
  try { body = state.body ? JSON.parse(state.body) : null } catch { body = state.body }
  return { status: state.statusCode, body: body as Record<string, unknown> }
}

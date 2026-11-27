// Fork-side cases of mayActAsOwner (APIAGENTWRITE1104 port, BELA 6404) that upstream's agent-write-owner-gated suite
// does not reach: the first-run branch (no dashboard user yet) is for the SHARED token on the local listener only.
// A per-agent token and a request that arrived on the public HTTPS listener never take it.
import { describe, it, expect, beforeAll, beforeEach } from 'vitest'
import type http from 'node:http'
import { Readable } from 'node:stream'

const { initDatabase, getDb, createDashboardUser } = await import('../db.js')
const { mayActAsOwner } = await import('../web/routes/auth.js')
const { markPublicRequest } = await import('../web/public-access.js')

const req = (): http.IncomingMessage => {
  const r = Readable.from([]) as unknown as http.IncomingMessage
  r.headers = {}
  return r
}

beforeAll(() => { initDatabase(':memory:') })

describe('mayActAsOwner before the first dashboard user exists (first-run branch)', () => {
  beforeEach(() => { getDb().exec('DELETE FROM auth_sessions; DELETE FROM dashboard_users') })

  it('the shared token on the local listener passes', () => {
    expect(mayActAsOwner({ kind: 'token' }, req())).toBe(true)
  })

  it('a per-agent token does not', () => {
    expect(mayActAsOwner({ kind: 'token', tokenAgent: 'samu' } as never, req())).toBe(false)
  })

  it('the shared token on the public listener does not', () => {
    const r = req()
    markPublicRequest(r)
    expect(mayActAsOwner({ kind: 'token' }, r)).toBe(false)
  })

  it('a device key or a federation peer does not', () => {
    expect(mayActAsOwner({ kind: 'device', device: 'x', deviceId: 1 }, req())).toBe(false)
    expect(mayActAsOwner({ kind: 'federation', peer: 'codex' }, req())).toBe(false)
    expect(mayActAsOwner(undefined, req())).toBe(false)
  })
})

describe('mayActAsOwner once a dashboard user exists', () => {
  beforeEach(() => {
    getDb().exec('DELETE FROM auth_sessions; DELETE FROM dashboard_users')
    createDashboardUser('owner', 'x'.repeat(60))
  })

  it('the bare shared token no longer passes', () => {
    expect(mayActAsOwner({ kind: 'token' }, req())).toBe(false)
  })

  it('a disabled-only user table still closes the token (disabled users count)', () => {
    getDb().exec('UPDATE dashboard_users SET disabled = 1')
    expect(mayActAsOwner({ kind: 'token' }, req())).toBe(false)
  })

  it('a login session passes', () => {
    expect(mayActAsOwner({ kind: 'session', user: 'owner' }, req())).toBe(true)
  })
})

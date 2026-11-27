import { Readable } from 'node:stream'
import { existsSync, unlinkSync, mkdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it, expect, beforeEach, beforeAll, afterAll } from 'vitest'
import { tryHandleAgents } from '../web/routes/agents.js'
import { requiresAuth } from '../web/auth-gate.js'
import { MAIN_AGENT_ID, PROJECT_ROOT } from '../config.js'
import { DEFAULT_GATE_CONFIG } from '../context-restart-gate.js'
import type { RouteContext } from '../web/routes/types.js'

// GET/PUT /api/agents/:name/context-restart-gate (kanban 0d8bf173). HTTP-level
// coverage complementing the pure-logic tests in context-restart-gate.test.ts
// and the key-check tests in agent-put-fields.test.ts -- this is the layer
// those two cannot see: routing, name decoding, 404s, and the actual JSON
// shape a caller gets back. Uses the same fakeCtx() pattern already
// established for this router file (main-agent-detail-guards.test.ts) and for
// other route modules (costops-api.test.ts).
//
// `agents/` is entirely gitignored (see .gitignore) -- a checked-in name like
// "progi" does NOT exist in a fresh worktree checkout (confirmed: this file
// 404'd on every REAL_AGENT case the first time it was actually run). So the
// happy-path agent is a throwaway directory created here, under the real
// (unmocked) PROJECT_ROOT -- safe because the project's own vitest setup
// (assert-not-live-install.ts) refuses to run this suite anywhere but a
// disposable worktree.
const REAL_AGENT = 'zzz-context-restart-gate-route-test-agent'
const REAL_AGENT_DIR = join(PROJECT_ROOT, 'agents', REAL_AGENT)
const UNKNOWN_AGENT = 'zzz-nonexistent-test-agent-xyz-12345'

beforeAll(() => { mkdirSync(REAL_AGENT_DIR, { recursive: true }) })
afterAll(() => { rmSync(REAL_AGENT_DIR, { recursive: true, force: true }) })

function fakeCtx(path: string, method: string, body?: unknown): {
  ctx: RouteContext
  out: { status: number; body: Record<string, unknown> | null }
} {
  const out: { status: number; body: Record<string, unknown> | null } = { status: 0, body: null }
  const res: any = {
    writeHead(status: number) { out.status = status; return res },
    end(chunk?: string) { if (chunk) out.body = JSON.parse(chunk) as Record<string, unknown> },
  }
  const req: any = body !== undefined
    ? Readable.from([Buffer.from(JSON.stringify(body))])
    : Readable.from([])
  const url = new URL(`http://localhost:3420${path}`)
  const ctx = { req, res, path: url.pathname, method, url } as RouteContext
  return { ctx, out }
}

// The store is a real file (src/web/context-restart-gate-store.ts,
// store/context-restart-gate.json), not an in-memory fake -- unlike
// costops-api.test.ts's initDatabase(':memory:'), there is no reset hook for
// it. Delete it before EACH test so "GET returns defaults" is not order- or
// rerun-dependent (a prior test's write, or a prior `vitest run` of this same
// file in a non-throwaway checkout, must not leak into the next assertion).
//
// `store/` itself is also gitignored (confirmed: PUT ENOENT'd on the first
// real run in a fresh worktree, same class of gap as REAL_AGENT_DIR above).
// On a live install it always exists by the time the dashboard serves a
// request (src/index.ts, dashboard-auth.ts, db.ts, agent-taskstate.ts all
// mkdirSync it at boot/first-write) -- mirror that guarantee here instead of
// assuming it.
const STORE_DIR = join(PROJECT_ROOT, 'store')
const GATE_STORE_PATH = join(STORE_DIR, 'context-restart-gate.json')
beforeEach(() => {
  mkdirSync(STORE_DIR, { recursive: true })
  if (existsSync(GATE_STORE_PATH)) unlinkSync(GATE_STORE_PATH)
})

describe('GET/PUT /api/agents/:name/context-restart-gate', () => {
  it('is gated by the auth predicate like every other /api/agents/* config endpoint', () => {
    // Route-specific auth code would be redundant AND a place for this one
    // endpoint to drift from the rest -- protection here comes structurally
    // from web.ts's single global gate (resolveAuth + requiresAuth), applied
    // before tryHandleAgents is ever called. Pinning the predicate result is
    // the cheapest proof that this path was not accidentally added to the
    // public-probe allowlist in auth-gate.ts.
    expect(requiresAuth(`/api/agents/${REAL_AGENT}/context-restart-gate`, 'GET')).toBe(true)
    expect(requiresAuth(`/api/agents/${REAL_AGENT}/context-restart-gate`, 'PUT')).toBe(true)
  })

  it('GET returns the disabled defaults for an agent with no store entry', async () => {
    const { ctx, out } = fakeCtx(`/api/agents/${REAL_AGENT}/context-restart-gate`, 'GET')
    const handled = await tryHandleAgents(ctx, PROJECT_ROOT)
    expect(handled).toBe(true)
    expect(out.status).toBe(200)
    expect(out.body).toEqual({ ok: true, contextRestartGate: DEFAULT_GATE_CONFIG })
  })

  it('GET/PUT work for the main agent (no agents/<id>/ directory required)', async () => {
    const { ctx, out } = fakeCtx(`/api/agents/${MAIN_AGENT_ID}/context-restart-gate`, 'GET')
    expect(await tryHandleAgents(ctx, PROJECT_ROOT)).toBe(true)
    expect(out.status).toBe(200)
  })

  it('PUT round-trips a full config, normalized', async () => {
    const put = fakeCtx(`/api/agents/${REAL_AGENT}/context-restart-gate`, 'PUT', {
      enabled: true, thresholdTokens: 300_000, staleCutoffMs: 60_000,
      retryIntervalMs: 30_000, persistentBlockAlertMs: 90_000,
    })
    expect(await tryHandleAgents(put.ctx, PROJECT_ROOT)).toBe(true)
    expect(put.out.status).toBe(200)
    expect(put.out.body).toEqual({
      ok: true,
      contextRestartGate: {
        enabled: true, thresholdTokens: 300_000, staleCutoffMs: 60_000,
        retryIntervalMs: 30_000, persistentBlockAlertMs: 90_000,
        wakeAfterClear: true, // omitted in the body -> the default (on)
      },
    })

    const get = fakeCtx(`/api/agents/${REAL_AGENT}/context-restart-gate`, 'GET')
    await tryHandleAgents(get.ctx, PROJECT_ROOT)
    expect(get.out.body?.contextRestartGate).toEqual(put.out.body?.contextRestartGate)
  })

  it('PUT rejects an unknown field with 400, and does not save it', async () => {
    const bad = fakeCtx(`/api/agents/${REAL_AGENT}/context-restart-gate`, 'PUT', {
      enabled: true, thresholdTokns: 1, nonsense: true,
    })
    expect(await tryHandleAgents(bad.ctx, PROJECT_ROOT)).toBe(true)
    expect(bad.out.status).toBe(400)
    expect(bad.out.body?.rejected).toEqual(['thresholdTokns', 'nonsense'])
    expect(bad.out.body?.known).toEqual(Object.keys(DEFAULT_GATE_CONFIG))

    // and the store must still hold the DEFAULT, not a half-applied write
    const get = fakeCtx(`/api/agents/${REAL_AGENT}/context-restart-gate`, 'GET')
    await tryHandleAgents(get.ctx, PROJECT_ROOT)
    expect(get.out.body).toEqual({ ok: true, contextRestartGate: DEFAULT_GATE_CONFIG })
  })

  it('PUT rejects a non-JSON body with 400 instead of throwing', async () => {
    const { ctx, out } = fakeCtx(`/api/agents/${REAL_AGENT}/context-restart-gate`, 'PUT')
    ;(ctx.req as any) = Readable.from([Buffer.from('not json{')])
    expect(await tryHandleAgents(ctx, PROJECT_ROOT)).toBe(true)
    expect(out.status).toBe(400)
  })

  // Codex review point 2 (kanban 0d8bf173): decodeURIComponent on a malformed
  // percent-encoding throws URIError. Before the fix that propagated out of
  // this handler uncaught; now it is a clean 400.
  it('a malformed percent-encoded agent name is a clean 400, not a crash', async () => {
    const { ctx, out } = fakeCtx('/api/agents/%/context-restart-gate', 'GET')
    const handled = await tryHandleAgents(ctx, PROJECT_ROOT)
    expect(handled).toBe(true)
    expect(out.status).toBe(400)
    expect(out.body?.error).toMatch(/encoding/i)
  })

  // Codex review point 2: a traversal-shaped name must not reach agentDir()'s
  // safeJoin() as an uncaught throw either -- isKnownAgent() absorbs it and
  // the route answers the same 404 as any other unknown name.
  it('a path-traversal-shaped agent name resolves to 404, not a 500', async () => {
    const encoded = encodeURIComponent('../../etc/passwd')
    const { ctx, out } = fakeCtx(`/api/agents/${encoded}/context-restart-gate`, 'GET')
    const handled = await tryHandleAgents(ctx, PROJECT_ROOT)
    expect(handled).toBe(true)
    expect(out.status).toBe(404)
  })

  it('GET and PUT both 404 for an agent nobody has heard of', async () => {
    const get = fakeCtx(`/api/agents/${UNKNOWN_AGENT}/context-restart-gate`, 'GET')
    expect(await tryHandleAgents(get.ctx, PROJECT_ROOT)).toBe(true)
    expect(get.out.status).toBe(404)

    const put = fakeCtx(`/api/agents/${UNKNOWN_AGENT}/context-restart-gate`, 'PUT', { enabled: true })
    expect(await tryHandleAgents(put.ctx, PROJECT_ROOT)).toBe(true)
    expect(put.out.status).toBe(404)
  })

  // Codex review point 3 (kanban 0d8bf173): PUT does not merge onto the
  // stored config -- it replaces it wholesale, filling anything the body
  // omits from DEFAULT_GATE_CONFIG. A caller sending {enabled:true} after a
  // custom threshold was already saved LOSES that threshold. This is the
  // documented, existing contract (matches auto-restart/context-guard), but
  // it is exactly the kind of thing that "looks like partial update" and
  // silently is not -- pinned here so the semantics cannot drift unnoticed.
  it('a partial PUT is NOT a merge -- unspecified fields revert to default', async () => {
    const full = fakeCtx(`/api/agents/${REAL_AGENT}/context-restart-gate`, 'PUT', {
      enabled: true, thresholdTokens: 111_111, staleCutoffMs: 111_111,
      retryIntervalMs: 111_111, persistentBlockAlertMs: 111_111,
    })
    await tryHandleAgents(full.ctx, PROJECT_ROOT)
    expect(full.out.status).toBe(200)

    const partial = fakeCtx(`/api/agents/${REAL_AGENT}/context-restart-gate`, 'PUT', { enabled: false })
    await tryHandleAgents(partial.ctx, PROJECT_ROOT)
    expect(partial.out.status).toBe(200)
    expect(partial.out.body).toEqual({
      ok: true,
      contextRestartGate: { ...DEFAULT_GATE_CONFIG, enabled: false },
    })
    // explicitly: the custom threshold from the first PUT is GONE, not carried forward
    expect((partial.out.body?.contextRestartGate as any).thresholdTokens).toBe(DEFAULT_GATE_CONFIG.thresholdTokens)
    expect((partial.out.body?.contextRestartGate as any).thresholdTokens).not.toBe(111_111)
  })

  it('rejects out-of-range values by falling back to default, at the HTTP layer too', async () => {
    // NaN/Infinity cannot survive JSON.stringify (they serialize to `null`),
    // so a wire-level test exercises the values a real HTTP body CAN carry:
    // negative, zero, a numeric string, and a non-boolean truthy `enabled`.
    // The NaN/Infinity/-Infinity cases are covered directly against
    // normalizeGateConfig() in context-restart-gate.test.ts, where they can
    // be passed as actual JS values.
    const { ctx, out } = fakeCtx(`/api/agents/${REAL_AGENT}/context-restart-gate`, 'PUT', {
      enabled: 'true',        // truthy but not the literal boolean -- must NOT arm the gate
      thresholdTokens: -5,    // negative
      staleCutoffMs: 0,       // zero
      retryIntervalMs: '30000', // numeric string, not a number
      persistentBlockAlertMs: null,
    })
    expect(await tryHandleAgents(ctx, PROJECT_ROOT)).toBe(true)
    expect(out.status).toBe(200)
    expect(out.body).toEqual({ ok: true, contextRestartGate: DEFAULT_GATE_CONFIG })
  })
})

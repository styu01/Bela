// LOCAL (BELA msg 6167) on top of the upstream cca1e519 port: the strict unknown-parameter 400 on
// GET /api/kanban must not break what callers on this install really send. Inventory 2026-11-27:
// no non-agent caller sends a query parameter to /api/kanban or /api/messages/backlog; the agents
// send ?limit= and ?assignee=. So `limit` is ACCEPTED AND APPLIED (not refused, not ignored), a bad
// value is refused loudly, and everything else unknown still gets the 400 naming the accepted set.
// The backlog endpoint's new agent filter is pinned here too.
import { describe, it, expect, beforeEach } from 'vitest'
import { Readable } from 'node:stream'
import { initDatabase, createKanbanCard, createAgentMessage, getDb, listKanbanCards } from '../db.js'
import { tryHandleKanban, parseKanbanLimit, KANBAN_LIST_LIMIT_MAX } from '../web/routes/kanban.js'
import { tryHandleMessages } from '../web/routes/messages.js'
import type { RouteContext } from '../web/routes/types.js'

function getCtx(path: string, query: string): { ctx: RouteContext; out: { status: number; body: any } } {
  const out: { status: number; body: any } = { status: 200, body: null }
  const res: any = {
    writeHead(status: number) { out.status = status; return res },
    setHeader() { return res },
    end(chunk?: string | Buffer) { if (chunk) out.body = JSON.parse(chunk.toString()) },
  }
  const req: any = Readable.from([])
  req.headers = {}
  const url = new URL(`http://localhost:3420${path}${query}`)
  return { ctx: { req, res, path: url.pathname, method: 'GET', url } as RouteContext, out }
}

describe('parseKanbanLimit', () => {
  it('absent -> no limit; a positive integer up to the max -> that number', () => {
    expect(parseKanbanLimit(null)).toBeUndefined()
    expect(parseKanbanLimit('1')).toBe(1)
    expect(parseKanbanLimit(' 3 ')).toBe(3)
    expect(parseKanbanLimit(String(KANBAN_LIST_LIMIT_MAX))).toBe(KANBAN_LIST_LIMIT_MAX)
  })
  it.each(['', '0', '-1', '1.5', 'abc', '3x', String(KANBAN_LIST_LIMIT_MAX + 1), '1e3'])('rejects %j (null = 400)', (raw) => {
    expect(parseKanbanLimit(raw)).toBeNull()
  })
})

describe('GET /api/kanban?limit= is honoured', () => {
  beforeEach(() => {
    initDatabase(':memory:')
    createKanbanCard({ id: 'k1', title: 'first', assignee: 'progi' })
    createKanbanCard({ id: 'k2', title: 'second', assignee: 'bela' })
    createKanbanCard({ id: 'k3', title: 'third', assignee: 'progi' })
  })

  it('control: no limit -> every card, in board order', async () => {
    const { ctx, out } = getCtx('/api/kanban', '')
    await tryHandleKanban(ctx)
    expect(out.status).toBe(200)
    expect(out.body.map((c: any) => c.id)).toEqual(['k1', 'k2', 'k3'])
  })

  it('?limit=2 -> the first 2 cards in board order (a real effect, not ignored)', async () => {
    const { ctx, out } = getCtx('/api/kanban', '?limit=2')
    await tryHandleKanban(ctx)
    expect(out.status).toBe(200)
    expect(out.body.map((c: any) => c.id)).toEqual(['k1', 'k2'])
  })

  it('?limit= combines with the agent filter (limit applies after the filter)', async () => {
    const { ctx, out } = getCtx('/api/kanban', '?assignee=progi&limit=1')
    await tryHandleKanban(ctx)
    expect(out.status).toBe(200)
    expect(out.body.map((c: any) => c.id)).toEqual(['k1'])
    const two = getCtx('/api/kanban', '?agent=PROGI&limit=5')
    await tryHandleKanban(two.ctx)
    expect(two.out.body.map((c: any) => c.id)).toEqual(['k1', 'k3'])
  })

  it.each(['?limit=0', '?limit=abc', '?limit=', `?limit=${KANBAN_LIST_LIMIT_MAX + 1}`])('%s -> a loud 400, never a silent "no limit"', async (q) => {
    const { ctx, out } = getCtx('/api/kanban', q)
    await tryHandleKanban(ctx)
    expect(out.status).toBe(400)
    expect(String(out.body.error)).toContain('limit')
  })

  it('another unknown parameter still 400s, and the answer names limit among the accepted ones', async () => {
    const { ctx, out } = getCtx('/api/kanban', '?status=done')
    await tryHandleKanban(ctx)
    expect(out.status).toBe(400)
    expect(out.body.unknown).toEqual(['status'])
    expect(out.body.known).toEqual(['agent', 'assignee', 'includeArchived', 'limit'])
  })

  it('the data layer: limit counts only the visible (non-archived) cards unless includeArchived', () => {
    getDb().prepare("UPDATE kanban_cards SET archived_at = 1 WHERE id = 'k1'").run()
    expect(listKanbanCards({ limit: 1 }).map((c) => c.id)).toEqual(['k2'])
    expect(listKanbanCards({ limit: 1, includeArchived: true }).map((c) => c.id)).toEqual(['k1'])
  })
})

describe('GET /api/messages/backlog?agent= filters server-side', () => {
  beforeEach(() => {
    initDatabase(':memory:')
    createAgentMessage('bela', 'progi', 'egy')
    createAgentMessage('bela', 'progi', 'ketto')
    createAgentMessage('bela', 'okoska', 'harom')
  })

  it('control: no parameter -> the whole fleet', async () => {
    const { ctx, out } = getCtx('/api/messages/backlog', '')
    expect(await tryHandleMessages(ctx)).toBe(true)
    expect(out.status).toBe(200)
    expect(out.body.map((r: any) => r.agent).sort()).toEqual(['okoska', 'progi'])
  })

  it('?agent=progi -> only progi (it used to return the whole fleet)', async () => {
    const { ctx, out } = getCtx('/api/messages/backlog', '?agent=progi')
    await tryHandleMessages(ctx)
    expect(out.status).toBe(200)
    expect(out.body).toHaveLength(1)
    expect(out.body[0]).toMatchObject({ agent: 'progi', pending: 2 })
  })

  it('?assignee= is the same filter', async () => {
    const { ctx, out } = getCtx('/api/messages/backlog', '?assignee=okoska')
    await tryHandleMessages(ctx)
    expect(out.body.map((r: any) => r.agent)).toEqual(['okoska'])
  })

  it('an unknown parameter -> 400 naming the accepted set', async () => {
    const { ctx, out } = getCtx('/api/messages/backlog', '?limit=5')
    await tryHandleMessages(ctx)
    expect(out.status).toBe(400)
    expect(out.body.unknown).toEqual(['limit'])
    expect(out.body.known).toEqual(['agent', 'assignee'])
  })
})

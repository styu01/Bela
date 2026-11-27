// LOCAL fix: GET /api/kanban returns is_recurring_template as the stored INTEGER (0/1), and the
// dashboard PUTs the whole card back on an inline assignee / parent edit ({...card, assignee}).
// A boolean-only check refused every such edit with a 400.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Readable } from 'node:stream'
import type http from 'node:http'

vi.mock('../logger.js', () => ({ logger: { info: vi.fn(), warn: vi.fn(), debug: vi.fn(), error: vi.fn() } }))

import { initDatabase, createKanbanCard, getKanbanCard } from '../db.js'
import { tryHandleKanban } from '../web/routes/kanban.js'

async function put(id: string, body: unknown): Promise<number> {
  const req = Readable.from([Buffer.from(JSON.stringify(body))]) as unknown as http.IncomingMessage
  let status = 200
  const res = { writeHead: vi.fn((s: number) => { status = s }), end: vi.fn(), setHeader: vi.fn() } as unknown as http.ServerResponse
  await tryHandleKanban({ req, res, path: `/api/kanban/${id}`, method: 'PUT', url: new URL(`http://localhost/api/kanban/${id}`) } as never)
  return status
}

beforeEach(() => { initDatabase(':memory:') })

describe('PUT /api/kanban/:id with the card as GET returned it', () => {
  it('the dashboard round trip ({...card, assignee}) with is_recurring_template 0 is accepted and written', async () => {
    createKanbanCard({ id: 'r1', title: 'Card', assignee: 'a' })
    const card = getKanbanCard('r1')!
    expect(card.is_recurring_template).toBe(0)
    expect(await put('r1', { ...card, assignee: 'b' })).toBe(200)
    expect(getKanbanCard('r1')?.assignee).toBe('b')
  })
  it('1 and booleans are accepted; anything else is still a 400', async () => {
    createKanbanCard({ id: 'r2', title: 'Card' })
    expect(await put('r2', { is_recurring_template: 1 })).toBe(200)
    expect(getKanbanCard('r2')?.is_recurring_template).toBe(1)
    expect(await put('r2', { is_recurring_template: false })).toBe(200)
    expect(getKanbanCard('r2')?.is_recurring_template).toBe(0)
    for (const bad of ['1', 2, null, 'true']) expect(await put('r2', { is_recurring_template: bad })).toBe(400)
  })
})

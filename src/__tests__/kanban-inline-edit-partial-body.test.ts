// PROGI review msg 6187 (on d7817d7f): the dashboard's inline assignee and parent edits PUT the card
// object captured when the detail view opened ({...card, assignee}); everything an agent wrote into
// the card meanwhile (status, description, ...) went back to the old values. They now send ONLY the
// field the control changes, and the server merges a partial body over the stored card.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { Readable } from 'node:stream'
import type http from 'node:http'

vi.mock('../logger.js', () => ({ logger: { info: vi.fn(), warn: vi.fn(), debug: vi.fn(), error: vi.fn() } }))

import { initDatabase, createKanbanCard, getKanbanCard, updateKanbanCard } from '../db.js'
import { tryHandleKanban } from '../web/routes/kanban.js'

async function put(id: string, body: unknown): Promise<number> {
  const req = Readable.from([Buffer.from(JSON.stringify(body))]) as unknown as http.IncomingMessage
  let status = 200
  const res = { writeHead: vi.fn((s: number) => { status = s }), end: vi.fn(), setHeader: vi.fn() } as unknown as http.ServerResponse
  await tryHandleKanban({ req, res, path: `/api/kanban/${id}`, method: 'PUT', url: new URL(`http://localhost/api/kanban/${id}`) } as never)
  return status
}

beforeEach(() => { initDatabase(':memory:') })

describe('the server side of a one-field inline edit', () => {
  it('{assignee} keeps the status and description an agent wrote after the view opened', async () => {
    createKanbanCard({ id: 'p1', title: 'Card', assignee: 'a' })
    // the detail view opens here (the dashboard holds the old copy) ...
    updateKanbanCard('p1', { status: 'waiting', description: 'agent wrote this' })   // ... an agent writes meanwhile
    expect(await put('p1', { assignee: 'b' })).toBe(200)
    expect(getKanbanCard('p1')).toMatchObject({ assignee: 'b', status: 'waiting', description: 'agent wrote this' })
  })
  it('{parent_id} the same', async () => {
    createKanbanCard({ id: 'p0', title: 'Parent' })
    createKanbanCard({ id: 'p2', title: 'Child' })
    updateKanbanCard('p2', { description: 'agent wrote this' })
    expect(await put('p2', { parent_id: 'p0' })).toBe(200)
    expect(getKanbanCard('p2')).toMatchObject({ parent_id: 'p0', description: 'agent wrote this' })
  })
})

describe('web/app.js: the inline assignee and parent edits send only their field', () => {
  const app = readFileSync(join(__dirname, '..', '..', 'web', 'app.js'), 'utf-8')
  it('the assignee edit body is { assignee: newVal } and nothing else', () => {
    expect(app).toContain('body: JSON.stringify({ assignee: newVal }),')
    expect(app).not.toContain('{ ...card, assignee: newVal }')
  })
  it('the parent edit body is { parent_id: newParentId } and nothing else', () => {
    expect(app).toContain('body: JSON.stringify({ parent_id: newParentId }),')
    expect(app).not.toContain('{ ...card, parent_id: newParentId }')
  })
  it('no PUT to /api/kanban/<id> anywhere in app.js spreads a whole card object', () => {
    expect(app).not.toMatch(/JSON\.stringify\(\{\s*\.\.\.card\b/)
  })
})

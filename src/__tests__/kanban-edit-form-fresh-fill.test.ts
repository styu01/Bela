// PROGI review msg 6192 (HIBA 1 + 2) on 3cf71aac / f49c3434: the dashboard edit form took its base
// version from the `card` object captured when the detail view opened.
//   A) the user's own status change (/move) bumped the version, the object kept the old one -> the
//      form save got a FALSE 409;
//   B) the inline edits copied the response's NEW version into that old object -> a form save sent
//      old fields with a version that already covered an agent's newer description -> 200, text lost.
// The fix: the Edit button fills the form from a fresh load (fields + version from the same moment),
// and the inline edits no longer copy the version. These tests drive the real routes the way the
// dashboard does, for the fixed flow AND for the old flow (to show the tests tell them apart).
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { Readable } from 'node:stream'
import type http from 'node:http'

vi.mock('../logger.js', () => ({ logger: { info: vi.fn(), warn: vi.fn(), debug: vi.fn(), error: vi.fn() } }))

import { initDatabase, createKanbanCard, getKanbanCard, listKanbanCards } from '../db.js'
import { tryHandleKanban } from '../web/routes/kanban.js'

async function call(method: string, path: string, body: unknown): Promise<{ status: number; body: any }> {
  const req = Readable.from([Buffer.from(JSON.stringify(body))]) as unknown as http.IncomingMessage
  let status = 200; let sent = ''
  const res = { writeHead: vi.fn((s: number) => { status = s }), end: vi.fn((c?: unknown) => { if (c != null) sent += String(c) }), setHeader: vi.fn() } as unknown as http.ServerResponse
  await tryHandleKanban({ req, res, path, method, url: new URL(`http://localhost${path}`) } as never)
  return { status, body: sent ? JSON.parse(sent) : {} }
}
const put = (id: string, b: unknown) => call('PUT', `/api/kanban/${id}`, b)
const move = (id: string, status: string) => call('POST', `/api/kanban/${id}/move`, { status, sort_order: 0, actor: 'Istvan' })
const freshLoad = (id: string) => listKanbanCards().find((c) => c.id === id)!   // what loadKanban() gives the form
// what the edit form PUTs: its fields, built from the card it was filled from, plus that card's version
const formBody = (filled: any, edits: Record<string, unknown>) => ({
  title: filled.title, description: filled.description, assignee: filled.assignee, priority: filled.priority,
  project: filled.project, is_recurring_template: !!filled.is_recurring_template, due_date: filled.due_date,
  ...edits, expected_version: filled.version,
})

beforeEach(() => { initDatabase(':memory:') })

describe('A: the user changes the status in the detail view, then edits', () => {
  it('fixed flow (fresh fill): no false 409, the edit is saved', async () => {
    createKanbanCard({ id: 'a1', title: 'Card' })
    await move('a1', 'waiting')                                  // the detail view's status control
    const r = await put('a1', formBody(freshLoad('a1'), { description: 'hosszu leiras' }))
    expect(r.status).toBe(200)
    expect(getKanbanCard('a1')?.description).toBe('hosszu leiras')
  })
  it('old flow (the object from before the move): the false 409 the fix removes', async () => {
    createKanbanCard({ id: 'a2', title: 'Card' })
    const opened = { ...getKanbanCard('a2')! }
    await move('a2', 'waiting')
    expect((await put('a2', formBody(opened, { description: 'x' }))).status).toBe(409)
  })
})

describe('B: an agent writes the description, the user edits inline, then opens the form', () => {
  it('fixed flow: the form shows the agent text; if the agent writes AGAIN while the form is open, the save is refused', async () => {
    createKanbanCard({ id: 'b1', title: 'Card', assignee: 'a' })
    await put('b1', { description: 'agent v1' })                // an agent, meanwhile
    expect((await put('b1', { assignee: 'b' })).status).toBe(200) // the inline edit: one field, no version
    const filled = freshLoad('b1')                                // Edit: fresh fill
    expect(filled.description).toBe('agent v1')
    await put('b1', { description: 'agent v2' })                // the agent writes again while the form is open
    const r = await put('b1', formBody(filled, { title: 'Card (edited)' }))
    expect(r.status).toBe(409)
    expect(getKanbanCard('b1')?.description).toBe('agent v2')
  })
  it('old flow (version copied from the inline response into the opened object): the agent text is overwritten with a 200', async () => {
    createKanbanCard({ id: 'b2', title: 'Card', assignee: 'a' })
    const opened = { ...getKanbanCard('b2')! }
    await put('b2', { description: 'agent v1' })
    const inline = await put('b2', { assignee: 'b' })
    const stale = { ...opened, assignee: 'b', version: inline.body.version }   // what f49c3434 left in the object
    expect((await put('b2', formBody(stale, { title: 'Card (edited)' }))).status).toBe(200)
    expect(getKanbanCard('b2')?.description).toBeNull()                        // the lost update
  })
})

describe('web/app.js wiring', () => {
  const app = readFileSync(join(__dirname, '..', '..', 'web', 'app.js'), 'utf-8')
  it('the Edit button awaits a fresh load and fills from kanbanCards by id, BEFORE taking the version', () => {
    const at = app.indexOf("document.getElementById('cardEditBtn').onclick = async () => {")
    expect(at).toBeGreaterThan(0)
    const body = app.slice(at, app.indexOf('openModal(cardModalOverlay)', at))
    const loadIdx = body.indexOf('await loadKanban()')
    const freshIdx = body.indexOf('kanbanCards.find((c) => c.id === card.id)')
    const versionIdx = body.indexOf('card.version')
    expect(loadIdx).toBeGreaterThan(0)
    expect(freshIdx).toBeGreaterThan(loadIdx)
    expect(versionIdx).toBeGreaterThan(freshIdx)
  })
  it('no inline edit copies a response version into the card object', () => {
    expect(app).not.toMatch(/card\.version\s*=\s*saved\.version/)
  })
})

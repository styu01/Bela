// PROGI review msg 6192, KOCKAZAT 4: the edit form's lost-update guard is FIELD-LEVEL. The form sends,
// for the fields it writes, the values it was filled from (`expected_fields`); the server refuses only
// when one of THOSE fields changed meanwhile. A status / sort_order / archived_at / parent_id move by
// an agent or the scheduler while the owner types must not refuse the owner's save; a description (or
// any written field) changed meanwhile still must.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { Readable } from 'node:stream'
import type http from 'node:http'

vi.mock('../logger.js', () => ({ logger: { info: vi.fn(), warn: vi.fn(), debug: vi.fn(), error: vi.fn() } }))

import { initDatabase, createKanbanCard, getKanbanCard, listKanbanCards, archiveKanbanCard } from '../db.js'
import { tryHandleKanban } from '../web/routes/kanban.js'

async function call(method: string, path: string, body: unknown): Promise<{ status: number; body: any }> {
  const req = Readable.from([Buffer.from(JSON.stringify(body))]) as unknown as http.IncomingMessage
  let status = 200; let sent = ''
  const res = { writeHead: vi.fn((s: number) => { status = s }), end: vi.fn((c?: unknown) => { if (c != null) sent += String(c) }), setHeader: vi.fn() } as unknown as http.ServerResponse
  await tryHandleKanban({ req, res, path, method, url: new URL(`http://localhost${path}`) } as never)
  return { status, body: sent ? JSON.parse(sent) : {} }
}
const put = (id: string, b: unknown) => call('PUT', `/api/kanban/${id}`, b)
const move = (id: string, status: string) => call('POST', `/api/kanban/${id}/move`, { status, sort_order: 3, actor: 'agent' })
const FORM = ['title', 'description', 'assignee', 'priority', 'project', 'is_recurring_template', 'due_date'] as const
// exactly what web/app.js sends: the form fields + expected_fields from the card it was filled from
function formSave(filled: any, edits: Record<string, unknown>) {
  const body: Record<string, unknown> = {
    title: filled.title, description: filled.description, assignee: filled.assignee, priority: filled.priority,
    project: filled.project, is_recurring_template: !!filled.is_recurring_template, due_date: filled.due_date, ...edits,
  }
  body.expected_fields = Object.fromEntries(FORM.map((k) => [k, filled[k] === undefined ? null : filled[k]]))
  return body
}
const fresh = (id: string) => ({ ...listKanbanCards({ includeArchived: true }).find((c) => c.id === id)! })

beforeEach(() => { initDatabase(':memory:') })

describe('changes the form does NOT write do not refuse the save', () => {
  it('an agent moves the card (status + sort_order) while the owner types -> 200, the move is kept', async () => {
    createKanbanCard({ id: 'f1', title: 'Card', description: 'old' })
    const filled = fresh('f1')
    await move('f1', 'waiting')
    const r = await put('f1', formSave(filled, { description: 'owner text' }))
    expect(r.status).toBe(200)
    expect(getKanbanCard('f1')).toMatchObject({ description: 'owner text', status: 'waiting', sort_order: 3 })
  })
  it('archived meanwhile -> 200, still archived', async () => {
    createKanbanCard({ id: 'f2', title: 'Card' })
    const filled = fresh('f2')
    archiveKanbanCard('f2')
    expect((await put('f2', formSave(filled, { title: 'Card 2' }))).status).toBe(200)
    expect(getKanbanCard('f2')?.archived_at).not.toBeNull()
  })
  it('parent set meanwhile -> 200, the parent is kept', async () => {
    createKanbanCard({ id: 'p', title: 'Parent' }); createKanbanCard({ id: 'f3', title: 'Card' })
    const filled = fresh('f3')
    await put('f3', { parent_id: 'p' })
    expect((await put('f3', formSave(filled, { priority: 'high' }))).status).toBe(200)
    expect(getKanbanCard('f3')).toMatchObject({ parent_id: 'p', priority: 'high' })
  })
  it('the recurring flag: a 1 in the row and true in the snapshot are the same value (no false 409)', async () => {
    createKanbanCard({ id: 'f4', title: 'Card', is_recurring_template: true } as any)
    expect(getKanbanCard('f4')?.is_recurring_template).toBe(1)
    expect((await put('f4', formSave(fresh('f4'), { description: 'x' }))).status).toBe(200)
    // a client that sends the flag as a boolean in expected_fields (the form's own value type)
    const body = formSave(fresh('f4'), { description: 'y' }) as any
    body.expected_fields.is_recurring_template = true
    expect((await put('f4', body)).status).toBe(200)
  })
})

describe('a change to a field the form writes still refuses it (the lost-update guard)', () => {
  it('description written by an agent meanwhile -> 409 stale_fields [description], the agent text survives', async () => {
    createKanbanCard({ id: 'g1', title: 'Card', description: 'old' })
    const filled = fresh('g1')
    await put('g1', { description: 'agent text' })
    const r = await put('g1', formSave(filled, { priority: 'high' }))
    expect(r.status).toBe(409)
    expect(r.body).toMatchObject({ conflict: 'stale_fields', fields: ['description'] })
    expect(getKanbanCard('g1')).toMatchObject({ description: 'agent text', priority: 'normal' })
  })
  it('title or assignee changed meanwhile -> 409 naming them', async () => {
    createKanbanCard({ id: 'g2', title: 'Card', assignee: 'a' })
    const filled = fresh('g2')
    await put('g2', { title: 'Renamed', assignee: 'b' })
    const r = await put('g2', formSave(filled, { description: 'x' }))
    expect(r.status).toBe(409)
    expect(r.body.fields.sort()).toEqual(['assignee', 'title'])
    expect(getKanbanCard('g2')?.description).toBeNull()
  })
  it('with both expected_fields and a stale version, the version check still refuses', async () => {
    createKanbanCard({ id: 'g3', title: 'Card' })
    const filled = fresh('g3')
    await move('g3', 'waiting')
    const r = await put('g3', { ...formSave(filled, { description: 'x' }), expected_version: filled.version })
    expect(r.status).toBe(409)
    expect(r.body.conflict).toBe('stale_version')
  })
})

describe('expected_fields is validated, never silently ignored', () => {
  it.each([
    ['not an object', [1, 2]],
    ['null', null],
    ['an unknown field', { title: 'x', dispatched_at: 1 }],
    ['a non-scalar value', { title: { a: 1 } }],
  ])('%s -> 400 and nothing written', async (_n, ef) => {
    createKanbanCard({ id: 'v1', title: 'Card' })
    const r = await put('v1', { title: 'changed', expected_fields: ef })
    expect(r.status).toBe(400)
    expect(getKanbanCard('v1')?.title).toBe('Card')
  })
})

// PROGI review 6206 item 3: the check must cover every field the body writes.
describe('expected_fields must cover every written field', () => {
  it('{description, expected_fields: {title}} -> 400 naming description, nothing written', async () => {
    createKanbanCard({ id: 'c1', title: 'Card', description: 'old' })
    const r = await put('c1', { description: 'unchecked', expected_fields: { title: 'Card' } })
    expect(r.status).toBe(400)
    expect(r.body.error).toContain('missing: description')
    expect(getKanbanCard('c1')?.description).toBe('old')
  })

  it('two written fields, one covered -> 400 naming only the uncovered one', async () => {
    createKanbanCard({ id: 'c2', title: 'Card', description: 'old' })
    const r = await put('c2', { title: 'New', priority: 'high', expected_fields: { title: 'Card' } })
    expect(r.status).toBe(400)
    expect(r.body.error).toContain('missing: priority')
    expect(r.body.error).not.toContain('title,')
    expect(getKanbanCard('c2')?.title).toBe('Card')
  })

  it('a partial body whose written fields are all covered -> 200 (the full form is not required)', async () => {
    createKanbanCard({ id: 'c3', title: 'Card', description: 'old' })
    const r = await put('c3', { description: 'new', expected_fields: { description: 'old' } })
    expect(r.status).toBe(200)
    expect(getKanbanCard('c3')?.description).toBe('new')
  })

  it('expected_fields may check MORE fields than it writes -> 200', async () => {
    createKanbanCard({ id: 'c4', title: 'Card', description: 'old' })
    const r = await put('c4', { description: 'new', expected_fields: { description: 'old', title: 'Card' } })
    expect(r.status).toBe(200)
  })

  it('the dashboard form body (all 7 fields written, all 7 checked) still saves', async () => {
    createKanbanCard({ id: 'c5', title: 'Card', description: 'old' })
    const r = await put('c5', formSave(fresh('c5'), { description: 'new' }))
    expect(r.status).toBe(200)
    expect(getKanbanCard('c5')?.description).toBe('new')
  })

  it('without expected_fields nothing changes (a partial body is still last-writer-wins)', async () => {
    createKanbanCard({ id: 'c6', title: 'Card', description: 'old' })
    const r = await put('c6', { description: 'new' })
    expect(r.status).toBe(200)
  })
})

describe('web/app.js sends expected_fields from the fresh card the form was filled from', () => {
  const app = readFileSync(join(__dirname, '..', '..', 'web', 'app.js'), 'utf-8')
  it('the save builds expected_fields over CARD_FORM_FIELDS from cardEditBase', () => {
    expect(app).toContain("const CARD_FORM_FIELDS = ['title', 'description', 'assignee', 'priority', 'project', 'is_recurring_template', 'due_date']")
    expect(app).toContain('data.expected_fields = Object.fromEntries(CARD_FORM_FIELDS.map((k) => [k, cardEditBase[k] === undefined ? null : cardEditBase[k]]))')
  })
  it('the Edit button takes cardEditBase from the freshly loaded card', () => {
    const at = app.indexOf("document.getElementById('cardEditBtn').onclick = async () => {")
    const body = app.slice(at, app.indexOf('openModal(cardModalOverlay)', at))
    expect(body.indexOf('cardEditBase = { ...card }')).toBeGreaterThan(body.indexOf('kanbanCards.find((c) => c.id === card.id)'))
  })
  it('PROGI 6206 item 4: a card gone from the board on Edit says archived vs gone, not a generic save error', () => {
    const at = app.indexOf("document.getElementById('cardEditBtn').onclick = async () => {")
    const body = app.slice(at, app.indexOf('openModal(cardModalOverlay)', at))
    const missing = body.slice(body.indexOf('if (!fresh) {'), body.indexOf('card = fresh'))
    expect(missing).toContain("fetch('/api/kanban?includeArchived=1')")
    expect(missing).toContain("if (hit && hit.archived_at) gone = 'kanban.toast.edit_archived'")
    expect(missing).toContain("let gone = 'kanban.toast.edit_gone'")
    expect(missing).not.toContain("t('kanban.toast.save_error')")
    for (const lang of ['hu', 'en']) {
      const src = readFileSync(join(__dirname, '..', '..', 'web', 'lang', `${lang}.js`), 'utf-8')
      expect(src).toMatch(/'kanban\.toast\.edit_archived':\s+'[^']+'/)
      expect(src).toMatch(/'kanban\.toast\.edit_gone':\s+'[^']+'/)
    }
  })

  it('...and the list it reads (?includeArchived=1) carries the archived card with archived_at', async () => {
    initDatabase(':memory:')
    createKanbanCard({ id: 'arch1', title: 'Card' })
    archiveKanbanCard('arch1')
    const get = async (url: string) => {
      const req = Object.assign(Readable.from([]), { headers: {} }) as unknown as http.IncomingMessage
      let sent = ''
      const res = { writeHead: vi.fn(), end: vi.fn((c?: unknown) => { if (c != null) sent += String(c) }), setHeader: vi.fn() } as unknown as http.ServerResponse
      const u = new URL(`http://localhost${url}`)
      await tryHandleKanban({ req, res, path: u.pathname, method: 'GET', url: u } as never)
      return JSON.parse(sent) as Array<{ id: string; archived_at: number | null }>
    }
    expect((await get('/api/kanban')).some((c) => c.id === 'arch1')).toBe(false)
    const hit = (await get('/api/kanban?includeArchived=1')).find((c) => c.id === 'arch1')
    expect(hit?.archived_at).toBeTruthy()
  })

  it('a stale_fields 409 keeps the form open like a stale_version one', () => {
    expect(app).toContain("e.conflict === 'stale_version' || e.conflict === 'stale_fields'")
  })
})

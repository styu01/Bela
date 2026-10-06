// a18c0f83: PATCH /api/memories/:id supports a category-only update while
// validating the category before it reaches the database.
import { beforeEach, describe, expect, it } from 'vitest'
import { Readable } from 'node:stream'
import { getDb, initDatabase, saveAgentMemory } from '../db.js'
import { tryHandleMemories } from '../web/routes/memories.js'
import type { RouteContext } from '../web/routes/types.js'

function patchCtx(id: number, body: unknown): { ctx: RouteContext; out: { status: number; body: unknown } } {
  const out: { status: number; body: unknown } = { status: 200, body: null }
  const res: any = {
    writeHead(status: number) { out.status = status; return res },
    setHeader() { return res },
    end(chunk?: string) { out.body = chunk ? JSON.parse(chunk) : null },
  }
  const req: any = Readable.from([Buffer.from(JSON.stringify(body))])
  req.headers = {}
  const url = new URL(`http://localhost:3420/api/memories/${id}`)
  return { ctx: { req, res, path: url.pathname, method: 'PATCH', url } as RouteContext, out }
}

describe('PATCH /api/memories/:id', () => {
  beforeEach(() => initDatabase(':memory:'))

  it('changes only the category and preserves the existing content', async () => {
    const { id } = saveAgentMemory('bela', 'Eredeti emlék', 'warm', 'teszt')
    const { ctx, out } = patchCtx(id, { category: 'cold' })

    expect(await tryHandleMemories(ctx)).toBe(true)
    expect(out.status).toBe(200)
    expect(getDb().prepare('SELECT content, category FROM memories WHERE id = ?').get(id))
      .toEqual({ content: 'Eredeti emlék', category: 'cold' })
  })

  it('rejects an invalid category without altering the memory', async () => {
    const { id } = saveAgentMemory('bela', 'Megőrzendő emlék', 'warm', 'teszt')
    const { ctx, out } = patchCtx(id, { category: 'invalid' })

    expect(await tryHandleMemories(ctx)).toBe(true)
    expect(out.status).toBe(400)
    expect(getDb().prepare('SELECT content, category FROM memories WHERE id = ?').get(id))
      .toEqual({ content: 'Megőrzendő emlék', category: 'warm' })
  })

  it('returns 404 for a category-only PATCH of a missing memory', async () => {
    const { ctx, out } = patchCtx(999_999, { category: 'cold' })
    expect(await tryHandleMemories(ctx)).toBe(true)
    expect(out.status).toBe(404)
  })
})

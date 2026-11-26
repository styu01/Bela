// MEMKERESVAK917 -- `category` is a filter on the QUERY, not on the answer.
//
// It used to be applied by the route AFTER the search had already cut its
// result to `limit`, so a filtered search silently truncated. Measured on the
// owner store before this change: `q=billingo&category=warm` returned 9 rows at
// limit=50 and 39 at limit=200, while 38 warm rows contain the word. The label
// said `relaxed=false` -- "matched as asked" -- on an answer missing three
// quarters of its matches, which is the same failure #1374/#1380 were about:
// an answer that is not what was asked for, with nothing saying so.
//
// The seed is built so the wanted rows are the ones a post-filter would lose,
// and it took a measurement to build it: the obvious version (four short warm
// rows, thirty short cold ones) put ALL FOUR warm rows in the top five, because
// FTS5 rank favours the shorter document. So the warm rows here are long and
// mention the term once, the cold rows are short and mention it twice, and the
// cold rows win the ranking. Measured on this seed: the top ten of an
// unfiltered search contains zero warm rows, so a post-filter returns nothing
// while four warm rows match.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { Readable } from 'node:stream'
import { initDatabase, saveAgentMemory, searchAgentMemories, getDb, hybridSearch, type HybridSearchTrace } from '../db.js'
import { tryHandleMemories } from '../web/routes/memories.js'
import type { RouteContext } from '../web/routes/types.js'

function fakeCtx(pathAndQuery: string, method = 'GET') {
  const out: { status: number; body: any; headers: Record<string, string> } = { status: 200, body: null, headers: {} }
  const res: any = {
    writeHead(status: number, headers?: Record<string, string>) {
      out.status = status
      if (headers) for (const [k, v] of Object.entries(headers)) out.headers[k.toLowerCase()] = String(v)
      return res
    },
    setHeader(k: string, v: string) { out.headers[k.toLowerCase()] = String(v) },
    end(chunk?: any) { if (chunk) { try { out.body = JSON.parse(chunk.toString()) } catch { out.body = chunk.toString() } } },
  }
  const req: any = Readable.from([])
  req.headers = {}
  const url = new URL(`http://localhost:3420${pathAndQuery}`)
  return { ctx: { req, res, path: url.pathname, method, url } as RouteContext, out }
}

const WARM_ROWS = 4
const COLD_ROWS = 30

const PAD = 'egyeb reszletek '.repeat(12)

function seed() {
  for (let i = 0; i < WARM_ROWS; i++) {
    saveAgentMemory('agent-a', `${PAD} billingo ${PAD} ${i}`, 'warm', '')
  }
  for (let i = 0; i < COLD_ROWS; i++) {
    saveAgentMemory('agent-a', `billingo billingo hibanaplo ${i}`, 'cold', 'billingo')
  }
  // A row that must never appear in a billingo search, whatever the category.
  saveAgentMemory('agent-a', 'teljesen mas tema, kanban oszlopok', 'warm', 'kanban')
}

// Adapted from upstream c9cc4f2f: this fork has no allowRelaxed/strict mode and no X-Memory-Search header on the
// non-hybrid branches (predecessor commits not pulled in), so those assertions and the strict=1 case are dropped.
describe('a category search is filtered in the query, not after the limit', () => {
  beforeEach(() => { initDatabase(':memory:'); seed() })

  it('the measure is not vacuous -- the seed really is dominated by the other tier', () => {
    const unfiltered = searchAgentMemories('agent-a', 'billingo', 10)
    expect(unfiltered.length).toBe(10)
    // This is the truncation, stated as a number: filtering this answer AFTER
    // the fact is what used to happen, and it loses most of the warm rows.
    const postFiltered = unfiltered.filter(m => m.category === 'warm')
    expect(postFiltered).toHaveLength(0)
  })

  it('returns every matching row of the asked-for tier, up to the limit', () => {
    const rows = searchAgentMemories('agent-a', 'billingo', 10, undefined, 'warm')
    expect(rows.length).toBe(WARM_ROWS)
    expect(rows.every(m => m.category === 'warm')).toBe(true)
  })

  it('does not widen the search: a row of the right tier that does not match stays out', () => {
    const rows = searchAgentMemories('agent-a', 'billingo', 10, undefined, 'warm')
    expect(rows.some(m => m.content.includes('kanban'))).toBe(false)
  })

  // The sharper form of the same property, and the one that survives a
  // mutation: search for a term that exists ONLY in the other tier. The right
  // answer is zero rows. A filter written as OR rather than AND turns this into
  // "every warm row", which the kanban assertion above did NOT catch.
  it('a term that lives only in another tier returns nothing for this tier', () => {
    expect(searchAgentMemories('agent-a', 'hibanaplo', 10, undefined, 'warm')).toEqual([])
    // Not vacuous: the term really is there, in the tier that owns it.
    expect(searchAgentMemories('agent-a', 'hibanaplo', 10, undefined, 'cold').length).toBeGreaterThan(0)
  })

  it('still honours the limit inside the tier', () => {
    const rows = searchAgentMemories('agent-a', 'billingo', 2, undefined, 'warm')
    expect(rows.length).toBe(2)
    expect(rows.every(m => m.category === 'warm')).toBe(true)
  })

  it('an unfiltered search is unchanged', () => {
    expect(searchAgentMemories('agent-a', 'billingo', 10).length).toBe(10)
  })

  it('the endpoint takes the same route: category= is no longer a post-filter', async () => {
    const { ctx, out } = fakeCtx('/api/memories?agent=agent-a&q=billingo&category=warm&limit=10')
    expect(await tryHandleMemories(ctx)).toBe(true)
    expect(out.body).toHaveLength(WARM_ROWS)
  })

  it('tier= is the same parameter and behaves the same', async () => {
    const { ctx, out } = fakeCtx('/api/memories?agent=agent-a&q=billingo&tier=warm&limit=10')
    expect(await tryHandleMemories(ctx)).toBe(true)
    expect(out.body).toHaveLength(WARM_ROWS)
  })
})

// PROGI review: the two other search branches of GET /api/memories take the
// category too (hybrid, and the agent-less ?q= branch). The route keeps a
// post-filter as a backstop, and that backstop is exactly what would hide a
// regression here: with the filter dropped from the query, the post-filter
// still returns only warm rows, just fewer of them (zero on this seed). So the
// assertion is the COUNT, not the category of what came back.
describe('the hybrid and the agent-less search branches filter in the query too', () => {
  // Embeddings are stubbed at the fetch boundary. Cold rows get the query's own
  // direction and warm rows an almost orthogonal one, so the vector branch also
  // ranks every cold row above every warm row: a post-filter would lose them.
  const QUERY_VEC = [1, 0, 0]
  const COLD_VEC = [1, 0.01, 0]
  const WARM_VEC = [0.05, 1, 0]
  let embedded = 0

  beforeEach(async () => {
    embedded = 0
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: { body?: string }) => {
      const prompt: string = JSON.parse(init?.body ?? '{}').prompt ?? ''
      embedded++
      const vec = prompt === 'billingo' ? QUERY_VEC : prompt.startsWith('billingo billingo') ? COLD_VEC : WARM_VEC
      return { json: async () => ({ embedding: vec }) }
    }))
    initDatabase(':memory:')
    seed()
    // saveAgentMemory embeds fire-and-forget: wait until every seeded row has its vector.
    await vi.waitFor(() => {
      const missing = (getDb().prepare('SELECT COUNT(*) AS n FROM memories WHERE embedding IS NULL').get() as { n: number }).n
      expect(missing).toBe(0)
    })
  })
  afterEach(() => { vi.unstubAllGlobals() })

  // At the db level, without the route's post-filter: BOTH hybrid branches must
  // have filtered. The vector branch has no text test, so with the category in
  // its query it sees exactly the warm rows (the four matches plus the off-topic
  // one); without it, it would fill limit*2 with cold rows.
  it('hybridSearch passes the category to the FTS AND the vector branch', async () => {
    const trace: HybridSearchTrace = { ftsHits: 0, vectorHits: 0, ftsRelaxed: false, vectorOnly: false }
    const rows = await hybridSearch('agent-a', 'billingo', 10, trace, 'warm')
    expect(rows.every(m => m.category === 'warm')).toBe(true)
    expect(trace.ftsHits).toBe(WARM_ROWS)
    expect(trace.vectorHits).toBe(WARM_ROWS + 1)
  })

  it('the measure is not vacuous: the unfiltered agent-less and hybrid answers hold no warm row', async () => {
    const plain = fakeCtx('/api/memories?q=billingo&limit=10')
    expect(await tryHandleMemories(plain.ctx)).toBe(true)
    expect(plain.out.body.filter((m: any) => m.category === 'warm')).toHaveLength(0)
    const hybrid = fakeCtx('/api/memories?agent=agent-a&q=billingo&mode=hybrid&limit=10')
    expect(await tryHandleMemories(hybrid.ctx)).toBe(true)
    expect(hybrid.out.body.filter((m: any) => m.category === 'warm')).toHaveLength(0)
    expect(embedded).toBeGreaterThan(WARM_ROWS + COLD_ROWS)
  })

  it('agent-less ?q=&category=warm returns every matching warm row', async () => {
    const { ctx, out } = fakeCtx('/api/memories?q=billingo&category=warm&limit=10')
    expect(await tryHandleMemories(ctx)).toBe(true)
    expect(out.body).toHaveLength(WARM_ROWS)
    expect(out.body.every((m: any) => m.category === 'warm')).toBe(true)
  })

  it('hybrid with category=warm returns every matching warm row and nothing from another tier', async () => {
    const { ctx, out } = fakeCtx('/api/memories?agent=agent-a&q=billingo&mode=hybrid&category=warm&limit=10')
    expect(await tryHandleMemories(ctx)).toBe(true)
    // The vector branch has no text test, so it may add the off-topic warm row; the
    // count that matters is the matching warm rows, all four of them.
    const matching = out.body.filter((m: any) => m.content.includes('billingo'))
    expect(matching).toHaveLength(WARM_ROWS)
    expect(out.body.every((m: any) => m.category === 'warm')).toBe(true)
    expect(out.headers['x-memory-search']).toContain('vector-only=false')
  })
})

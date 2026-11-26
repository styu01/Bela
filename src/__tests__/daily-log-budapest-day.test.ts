// GET /api/daily-log without ?date= must use the same calendar day appendDailyLog writes (APP_TZ),
// not the UTC day: between 00:00 and 02:00 local time the two differ and a dateless "today"
// query used to return yesterday's entries (upstream 1d6f7851).
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { Readable } from 'node:stream'

vi.hoisted(() => { process.env.SCHEDULER_TZ = 'Europe/Budapest' })

import { initDatabase, appendDailyLog } from '../db.js'
import { tryHandleDailyLog } from '../web/routes/daily-log.js'
import type { RouteContext } from '../web/routes/types.js'

function get(qs: string): any {
  const out: { body: any } = { body: null }
  const res: any = {
    writeHead() { return res },
    setHeader() { return res },
    end(chunk?: string) { if (chunk) out.body = JSON.parse(chunk) },
  }
  const url = new URL(`http://localhost:3420/api/daily-log${qs}`)
  const req: any = Readable.from([])
  req.headers = {}
  return tryHandleDailyLog({ req, res, path: url.pathname, method: 'GET', url } as RouteContext).then(() => out.body)
}

describe('GET /api/daily-log without date uses the Budapest calendar day', () => {
  beforeEach(() => { initDatabase(':memory:'); vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('00:30 local (winter, UTC+1) is still "today" for the dateless query', async () => {
    vi.setSystemTime(new Date('2026-11-26T23:30:00Z')) // 2026-11-27 00:30 in Budapest
    appendDailyLog('bela', 'written just after local midnight')
    const rows = await get('?agent=bela')
    expect(rows.map((r: any) => r.content)).toEqual(['written just after local midnight'])
  })

  it('01:30 local in summer (UTC+2) too', async () => {
    vi.setSystemTime(new Date('2026-07-14T23:30:00Z')) // 2026-07-15 01:30 in Budapest
    appendDailyLog('bela', 'summer entry')
    const rows = await get('?agent=bela')
    expect(rows.map((r: any) => r.content)).toEqual(['summer entry'])
  })

  it('an explicit date still wins, and the UTC day holds nothing', async () => {
    vi.setSystemTime(new Date('2026-11-26T23:30:00Z'))
    appendDailyLog('bela', 'x')
    expect(await get('?agent=bela&date=2026-11-26')).toEqual([])
    expect((await get('?agent=bela&date=2026-11-27')).length).toBe(1)
  })
})

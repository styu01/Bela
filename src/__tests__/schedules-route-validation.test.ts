import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const SOURCE = readFileSync(join(__dirname, '../web/routes/schedules.ts'), 'utf-8')

describe('schedule route quotaExempt validation', () => {
  it('rejects non-boolean quotaExempt on the update route before writing', () => {
    const putRoute = SOURCE.slice(SOURCE.indexOf("scheduleUpdateMatch && method === 'PUT'"))
    const validation = putRoute.slice(0, putRoute.indexOf('writeScheduledTask(name, data)'))
    expect(validation).toContain("typeof data.quotaExempt !== 'boolean'")
    expect(validation).toContain("quotaExempt must be a boolean")
    expect(validation).toContain("}, 400)")
  })
})

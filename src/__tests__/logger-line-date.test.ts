import { describe, expect, it } from 'vitest'
import { prettyFactory } from 'pino-pretty'
import { PRETTY_OPTIONS } from '../logger.js'

describe('logger pretty line format', () => {
  const render = (time: number) =>
    prettyFactory({ ...PRETTY_OPTIONS, colorize: false })(
      JSON.stringify({ level: 40, time, pid: 70, msg: 'test log line' }) + '\n',
    )

  it('prefixes every line with a date, time, and UTC offset', () => {
    const line = render(Date.UTC(2026, 8, 15, 12, 45, 30, 920))
    expect(line).toMatch(/^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{4}\] WARN \(70\): /)
  })

  it('distinguishes the same time of day on two dates', () => {
    const t = Date.UTC(2026, 8, 15, 12, 45, 30, 920)
    const day = 24 * 60 * 60 * 1000
    const stamp = (line: string) => line.slice(0, line.indexOf(']') + 1)
    expect(stamp(render(t))).not.toEqual(stamp(render(t + day)))
  })
})

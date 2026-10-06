import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const SOURCE = readFileSync(join(process.cwd(), 'src/web/auto-restart-runner.ts'), 'utf8')

describe('auto-restart failure stand-down', () => {
  it('records the failed slot before logging, preventing a retry loop', () => {
    const catchBlock = SOURCE.match(/catch \(err\) \{([\s\S]*?)\n  \}/)
    expect(catchBlock, 'checkAgent catch block').not.toBeNull()
    expect(catchBlock![1]).toContain('lastRestart.set(name, nowMs)')
    expect(catchBlock![1]).toContain('standing down until next scheduled slot')
    expect(catchBlock![1].indexOf('lastRestart.set(name, nowMs)')).toBeLessThan(
      catchBlock![1].indexOf('standing down until next scheduled slot'),
    )
  })
})

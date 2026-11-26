import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { silentTaskTag } from '../web/schedule-runner.js'

// Upstream #2284 / 23654701: a dream-engine task ran with "Az eredmenyt kuldd el Telegramon"
// in its prompt while its own SKILL.md says twice not to message the channel at 02:07.
// The silent types get a bare tag, every other type keeps the delivery instruction.

const SRC = readFileSync(join(__dirname, '../web/schedule-runner.ts'), 'utf-8')

describe('silentTaskTag', () => {
  it('heartbeat keeps the bare [Heartbeat: name] tag', () => {
    expect(silentTaskTag({ type: 'heartbeat', name: 'kanban-audit' } as any)).toBe('[Heartbeat: kanban-audit] ')
  })
  it('dream-engine gets the bare [Utemezett feladat: name] tag (the resubmit marker matches it)', () => {
    expect(silentTaskTag({ type: 'dream-engine', name: 'dream-engine' } as any)).toBe('[Utemezett feladat: dream-engine] ')
  })
  it('task, command and an unknown type keep the delivery instruction (null)', () => {
    for (const type of ['task', 'command', 'something-new', undefined]) {
      expect(silentTaskTag({ type, name: 'x' } as any), String(type)).toBeNull()
    }
  })
})

describe('the prompt builder uses it', () => {
  it('the silent branch takes silentTaskTag() before any delivery resolution, and nothing else builds a Heartbeat tag', () => {
    expect(SRC).toContain('const silentTag = silentTaskTag(task)')
    expect(SRC).toContain('if (silentTag !== null) {')
    expect(SRC).toContain('prefix = silentTag')
    expect(SRC).not.toMatch(/prefix = `\[Heartbeat:/)
  })
})

describe('the live dream-engine task is a silent type whose SKILL.md forbids sending', () => {
  const home = process.env.HOME ?? ''
  const dir = join(home, '.claude', 'scheduled-tasks', 'dream-engine')
  let skill: string | null = null
  let cfg: { type?: string } | null = null
  try {
    skill = readFileSync(join(dir, 'SKILL.md'), 'utf-8')
    cfg = JSON.parse(readFileSync(join(dir, 'task-config.json'), 'utf-8'))
  } catch { /* not installed on this machine: the cases below skip */ }

  it.skipIf(!skill || !cfg)('type is dream-engine, so the runner sends the bare tag', () => {
    expect(silentTaskTag({ type: cfg!.type as any, name: 'dream-engine' } as any)).not.toBeNull()
  })
  it.skipIf(!skill || !cfg)('SKILL.md still tells the agent not to message the channel', () => {
    expect(skill).toMatch(/NE küldj üzenetet a (beállított )?csatornára/)
  })
})

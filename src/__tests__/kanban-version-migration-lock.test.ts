// PROGI review msg 6192, KOCKAZAT 3: the version migration must never leave a half-migrated DB
// (trigger without the column). Reproduced by PROGI: another connection holding the write lock
// during init -> ALTER failed "database is locked", the error was swallowed, the trigger was created
// anyway, and every later PUT / move / archive failed with "no such column: version".
import { afterEach, describe, expect, it } from 'vitest'
import Database from 'better-sqlite3'
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { ensureKanbanVersionColumnAndTrigger, initDatabase } from '../db.js'

const dirs: string[] = []
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }) })

function preVersionDb(): string {
  const dir = mkdtempSync(join(tmpdir(), 'kv-lock-')); dirs.push(dir)
  const p = join(dir, 'pre.db')
  initDatabase(p)                                     // full schema, then strip the version back out
  const d = new Database(p)
  d.exec('DROP TRIGGER IF EXISTS kanban_cards_version_bump')
  d.exec('ALTER TABLE kanban_cards DROP COLUMN version')
  d.close()
  return p
}
const state = (p: string) => {
  const d = new Database(p, { readonly: true })
  const col = (d.prepare('PRAGMA table_info(kanban_cards)').all() as Array<{ name: string }>).some((c) => c.name === 'version')
  const trg = !!d.prepare("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='kanban_cards_version_bump'").get()
  d.close()
  return { col, trg }
}

describe('ensureKanbanVersionColumnAndTrigger', () => {
  it('an ALTER that fails for any reason but "duplicate column" throws, and no trigger is created', () => {
    const p = preVersionDb()
    const real = new Database(p)
    const fake = new Proxy(real, {
      get(t, k) {
        if (k === 'exec') return (sql: string) => { if (/ADD COLUMN version/.test(sql)) throw new Error('SQLITE_BUSY: database is locked'); return t.exec(sql) }
        const v = (t as any)[k]; return typeof v === 'function' ? v.bind(t) : v
      },
    }) as unknown as Database.Database
    expect(() => ensureKanbanVersionColumnAndTrigger(fake)).toThrow(/locked/)
    real.close()
    expect(state(p)).toEqual({ col: false, trg: false })
  })

  it('an ALTER that "succeeds" without adding the column: the post-check throws, still no trigger', () => {
    const p = preVersionDb()
    const real = new Database(p)
    const fake = new Proxy(real, {
      get(t, k) {
        if (k === 'exec') return (sql: string) => { if (/ADD COLUMN version/.test(sql)) return t; return t.exec(sql) }
        const v = (t as any)[k]; return typeof v === 'function' ? v.bind(t) : v
      },
    }) as unknown as Database.Database
    expect(() => ensureKanbanVersionColumnAndTrigger(fake)).toThrow(/did not add the column/)
    real.close()
    expect(state(p)).toEqual({ col: false, trg: false })
  })

  it('a concurrent process that added the column first ("duplicate column name") is fine', () => {
    const p = preVersionDb()
    const real = new Database(p)
    const fake = new Proxy(real, {
      get(t, k) {
        if (k === 'exec') return (sql: string) => {
          if (/ADD COLUMN version/.test(sql)) { t.exec(sql); throw new Error('duplicate column name: version') }
          return t.exec(sql)
        }
        const v = (t as any)[k]; return typeof v === 'function' ? v.bind(t) : v
      },
    }) as unknown as Database.Database
    expect(() => ensureKanbanVersionColumnAndTrigger(fake)).not.toThrow()
    real.close()
    expect(state(p)).toEqual({ col: true, trg: true })
  })

  it('REAL lock: another connection holds the write lock longer than the busy timeout -> init throws, column and trigger both absent; after release init completes both', async () => {
    const p = preVersionDb()
    const holder = spawn(process.execPath, ['-e', `
      const D = require(${JSON.stringify(require.resolve('better-sqlite3'))});
      const d = new D(${JSON.stringify(p)}); d.exec('BEGIN IMMEDIATE'); process.stdout.write('locked\\n');
      setTimeout(() => { d.exec('COMMIT'); d.close(); process.exit(0) }, 7000)`])
    await new Promise<void>((resolve) => holder.stdout.once('data', () => resolve()))
    expect(() => initDatabase(p)).toThrow(/locked|busy/i)
    expect(state(p)).toEqual({ col: false, trg: false })
    await new Promise<void>((resolve) => holder.once('exit', () => resolve()))
    initDatabase(p)
    expect(state(p)).toEqual({ col: true, trg: true })
  }, 30_000)
})

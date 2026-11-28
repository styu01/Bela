import { describe, it, expect } from 'vitest'
import Database from 'better-sqlite3'
import { migrateConversationLogColumns, CONVERSATION_LOG_MIGRATION_COLUMNS } from '../db.js'

// PROGI 6537 K1: the ledger hooks migrate conversation_log in parallel with each other and with a
// dashboard start. A concurrent winner makes our ALTER fail with "duplicate column name"; that alone
// is tolerated. Every other error still throws, and a column still missing afterwards throws.
const OLD = `CREATE TABLE conversation_log (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, chat_id TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('in','out')), message_id TEXT, text TEXT, ts TEXT, created_at INTEGER NOT NULL,
  UNIQUE(agent_id, chat_id, direction, message_id))`

type Hooks = { stalePragma?: Set<string>; exec?: (sql: string, real: () => unknown) => unknown }

// A real in-memory DB where each listed column stays hidden from PRAGMA until an ALTER for it was
// tried: exactly the window in which another process added it between our read and our ALTER.
function wrap(db: Database.Database, hooks: Hooks): Database.Database {
  const stale = new Set(hooks.stalePragma ?? [])
  return new Proxy(db, {
    get(target, prop, recv) {
      if (prop === 'prepare') return (sql: string) => {
        const stmt = target.prepare(sql)
        if (!/^PRAGMA table_info\(conversation_log\)$/.test(sql)) return stmt
        return { all: () => (stmt.all() as Array<{ name: string }>).filter((r) => !stale.has(r.name)) }
      }
      if (prop === 'exec') return (sql: string) => {
        const m = /^ALTER TABLE conversation_log ADD COLUMN (\w+)/.exec(sql)
        if (m) stale.delete(m[1])
        return hooks.exec ? hooks.exec(sql, () => target.exec(sql)) : target.exec(sql)
      }
      const v = Reflect.get(target, prop, recv)
      return typeof v === 'function' ? v.bind(target) : v
    },
  })
}

const cols = (db: Database.Database) => (db.prepare('PRAGMA table_info(conversation_log)').all() as Array<{ name: string }>).map((c) => c.name)

describe('migrateConversationLogColumns (PROGI 6537 K1)', () => {
  it('migrates an old ledger and is a no-op the second time', () => {
    const db = new Database(':memory:'); db.exec(OLD)
    migrateConversationLogColumns(db); migrateConversationLogColumns(db)
    for (const [c] of CONVERSATION_LOG_MIGRATION_COLUMNS) expect(cols(db).filter((x) => x === c)).toHaveLength(1)
  })

  it('tolerates a concurrent winner ("duplicate column name") for every column', () => {
    const db = new Database(':memory:'); db.exec(OLD)
    for (const [c, t] of CONVERSATION_LOG_MIGRATION_COLUMNS) db.exec(`ALTER TABLE conversation_log ADD COLUMN ${c} ${t}`)
    const stale = new Set(CONVERSATION_LOG_MIGRATION_COLUMNS.map(([c]) => c as string))
    let alters = 0
    const w = wrap(db, { stalePragma: stale, exec: (sql, real) => { if (/^ALTER/.test(sql)) alters++; return real() } })
    expect(() => migrateConversationLogColumns(w)).not.toThrow()
    expect(alters).toBe(CONVERSATION_LOG_MIGRATION_COLUMNS.length)
  })

  it('still throws any other ALTER error (a locked database is not swallowed)', () => {
    const db = new Database(':memory:'); db.exec(OLD)
    const w = wrap(db, { exec: (sql, real) => { if (/^ALTER/.test(sql)) throw new Error('database is locked'); return real() } })
    expect(() => migrateConversationLogColumns(w)).toThrow(/database is locked/)
  })

  it('throws when the column is still missing after a tolerated ALTER', () => {
    const db = new Database(':memory:'); db.exec(OLD)
    const w = wrap(db, { exec: (sql, real) => { if (/^ALTER/.test(sql)) throw new Error('duplicate column name: attachment_kind'); return real() } })
    expect(() => migrateConversationLogColumns(w)).toThrow(/did not add the column/)
  })
})

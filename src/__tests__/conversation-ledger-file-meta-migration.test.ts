import { describe, it, expect } from 'vitest'
import Database from 'better-sqlite3'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { initDatabase } from '../db.js'

// P5.9.9.F: an existing ledger without the attachment display columns is upgraded in
// place by the dashboard migration, its rows untouched; a second run is a no-op.
describe('conversation_log attachment metadata migration (db.ts)', () => {
  it('adds attachment_name/size/mime to an old ledger and keeps its rows', () => {
    const dir = mkdtempSync(join(tmpdir(), 'ledger-meta-'))
    try {
      const path = join(dir, 'old.db')
      const old = new Database(path)
      old.exec(`CREATE TABLE conversation_log (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('in','out')), message_id TEXT, text TEXT, ts TEXT, created_at INTEGER NOT NULL,
        attachment_kind TEXT, attachment_file_id TEXT, UNIQUE(agent_id, chat_id, direction, message_id))`)
      old.prepare(`INSERT INTO conversation_log (agent_id, chat_id, direction, message_id, text, created_at, attachment_kind, attachment_file_id)
        VALUES ('bela', '1', 'in', '7', '(document: a.pdf)', 1, 'document', 'fid')`).run()
      old.close()
      initDatabase(path)
      initDatabase(path)
      const db = new Database(path, { readonly: true })
      const cols = (db.prepare('PRAGMA table_info(conversation_log)').all() as { name: string; type: string }[])
        .map(c => `${c.name}:${c.type}`)
      expect(cols).toEqual(expect.arrayContaining(['attachment_name:TEXT', 'attachment_size:INTEGER', 'attachment_mime:TEXT']))
      expect(cols.filter(c => c.startsWith('attachment_name:'))).toHaveLength(1)
      expect(db.prepare("SELECT text, attachment_kind, attachment_name FROM conversation_log WHERE message_id='7'").get())
        .toEqual({ text: '(document: a.pdf)', attachment_kind: 'document', attachment_name: null })
      db.close()
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { statSync, existsSync, chmodSync } from 'node:fs'
import { join } from 'node:path'
import {
  initDatabase,
  getSession,
  setSession,
  clearSession,
  saveMemory,
  recentMemories,
  decayMemories,
  getMemoriesForChat,
  buildFtsMatchExpression,
  getDb,
  upsertPendingTaskRetry,
  insertPendingTaskRetryIfNew,
  updatePendingTaskRetry,
  listPendingTaskRetries,
  getPendingTaskRetry,
  deletePendingTaskRetry,
  deletePendingTaskRetryById,
  markPendingTaskRetryAlert,
  clearPendingTaskRetryAlert,
  createAgentMessage,
  getDispatchedPendingStats,
  getWaitingOutboundMessages,
  getRepliedButUnclosedInboundMessages,
  markMessageDelivered,
  markMessageDone,
  markMessageFailed,
  COMPLETION_REPORT_PREFIX,
  GATE_ALERT_ORIGIN_NOTE,
  GATE_NOTICE_ORIGIN_NOTE,
  closeMessagesWithoutDelivery,
} from '../db.js'
import { DB_FILENAME } from '../config.js'

beforeAll(() => {
  // Teszt adatbázis inicializálás -- in-memory, hogy a teszt SOHA ne irjon a
  // valodi store/claudeclaw.db-be (kulonben a sessions/memories/pending-task
  // insertek -- pl. "Szeretem a kavet" -- prod junk-memoriakent landolnak).
  process.env.NODE_ENV = 'test'
  initDatabase(':memory:')
})

describe('sessions', () => {
  it('munkamenetet ment es visszaolvas', () => {
    setSession('test-chat-1', 'session-abc')
    const s = getSession('test-chat-1')
    expect(s?.sessionId).toBe('session-abc')
    expect(s?.messageCount).toBe(0)
  })

  it('munkamenetet felulir', () => {
    setSession('test-chat-2', 'old-session')
    setSession('test-chat-2', 'new-session')
    expect(getSession('test-chat-2')?.sessionId).toBe('new-session')
  })

  it('munkamenetet torol', () => {
    setSession('test-chat-3', 'session-xyz')
    clearSession('test-chat-3')
    expect(getSession('test-chat-3')).toBeUndefined()
  })

  it('undefined ad vissza ha nem letezik', () => {
    expect(getSession('nem-letezik')).toBeUndefined()
  })
})

describe('memories', () => {
  it('emlek mentest es lekerdezest vegez', () => {
    saveMemory('mem-chat-1', 'Szeretem a kavét', 'semantic')
    const mems = recentMemories('mem-chat-1', 5)
    expect(mems.length).toBeGreaterThan(0)
    expect(mems[0].content).toBe('Szeretem a kavét')
    expect(mems[0].sector).toBe('semantic')
  })

  it('epizodikus emleket ment', () => {
    saveMemory('mem-chat-2', 'Mai megbeszeles eredmenye', 'episodic')
    const mems = getMemoriesForChat('mem-chat-2')
    expect(mems.length).toBeGreaterThan(0)
    expect(mems[0].sector).toBe('episodic')
  })

  it('leepulesi soprest vegrehajt hiba nelkul', () => {
    expect(() => decayMemories()).not.toThrow()
  })
})

describe('buildFtsMatchExpression', () => {
  it('produces prefix-matched tokens for a plain query', () => {
    expect(buildFtsMatchExpression('hello world')).toBe('hello* world*')
  })

  it('returns empty string for whitespace-only or empty input', () => {
    expect(buildFtsMatchExpression('')).toBe('')
    expect(buildFtsMatchExpression('   ')).toBe('')
    expect(buildFtsMatchExpression('!!!***???')).toBe('')
  })

  it('lowercases to neutralize FTS5 AND/OR/NOT/NEAR operators', () => {
    const out = buildFtsMatchExpression('foo OR bar AND baz NOT qux')
    // No uppercase operator keywords should survive as standalone tokens.
    expect(out).not.toMatch(/\bOR\b/)
    expect(out).not.toMatch(/\bAND\b/)
    expect(out).not.toMatch(/\bNOT\b/)
    expect(out).toBe('foo* or* bar* and* baz* not* qux*')
  })

  it('strips FTS5 punctuation (quotes, parens, colons); * is ours', () => {
    const out = buildFtsMatchExpression('"foo" (bar) baz qux:zap')
    expect(out).not.toMatch(/["():]/)
    // Every * in the output is our own prefix-match suffix, appended to a token.
    // No bare *, no doubled **.
    expect(out).not.toMatch(/\*\*/)
    expect(out).not.toMatch(/(^| )\*/)
    expect(out).toBe('foo* bar* baz* qux* zap*')
  })

  it('caps at 20 tokens', () => {
    const many = Array.from({ length: 30 }, (_, i) => `word${i}`).join(' ')
    const out = buildFtsMatchExpression(many)
    expect(out.split(' ').length).toBe(20)
  })

  it('truncates individual tokens longer than 64 chars', () => {
    const long = 'a'.repeat(200)
    const out = buildFtsMatchExpression(long)
    // 64 'a's + '*'
    expect(out).toBe('a'.repeat(64) + '*')
  })

  it('preserves unicode letters and digits', () => {
    expect(buildFtsMatchExpression('Árvíztűrő 42')).toBe('árvíztűrő* 42*')
  })
})

describe('pending task retries', () => {
  // The persistent test DB is shared with the running dashboard (both
  // resolve STORE_DIR to the same absolute path), so a blanket DELETE
  // would wipe the operator's real pending retries. Scope the cleanup to
  // the exact fixture names used below, and run it both before and after
  // so a re-run cleans up after itself even if an assertion throws.
  const FIXTURE_NAMES = [
    'task-a', 'task-b', 'task-c', 'task-d', 'task-e', 'task-f',
    'task-old', 'task-new', 'task-new-only', 'task-upd-only',
    'task-clear-alert',
  ]
  const wipeFixtures = () => {
    const stmt = getDb().prepare('DELETE FROM pending_task_retries WHERE task_name = ?')
    for (const n of FIXTURE_NAMES) stmt.run(n)
  }
  beforeAll(wipeFixtures)
  afterAll(wipeFixtures)

  it('inserts a new row on first upsert', () => {
    upsertPendingTaskRetry('task-a', 'main', 1_000_000, 'busy')
    const row = getPendingTaskRetry('task-a', 'main')
    expect(row).toMatchObject({
      task_name: 'task-a',
      agent_name: 'main',
      first_attempt: 1_000_000,
      last_attempt: 1_000_000,
      attempt_count: 1,
      last_reason: 'busy',
      alert_sent_at: null,
    })
  })

  it('bumps attempt_count and last_attempt on subsequent upserts, preserves first_attempt', () => {
    upsertPendingTaskRetry('task-b', 'main', 2_000_000, 'busy')
    upsertPendingTaskRetry('task-b', 'main', 2_000_500, 'busy')
    upsertPendingTaskRetry('task-b', 'main', 2_001_000, 'busy')
    const row = getPendingTaskRetry('task-b', 'main')!
    expect(row.first_attempt).toBe(2_000_000)
    expect(row.last_attempt).toBe(2_001_000)
    expect(row.attempt_count).toBe(3)
  })

  it('lists entries ordered by first_attempt ASC', () => {
    upsertPendingTaskRetry('task-old', 'main', 3_000_000, 'busy')
    upsertPendingTaskRetry('task-new', 'main', 4_000_000, 'busy')
    const rows = listPendingTaskRetries().filter(r => ['task-old', 'task-new'].includes(r.task_name))
    expect(rows[0].task_name).toBe('task-old')
    expect(rows[1].task_name).toBe('task-new')
  })

  it('deletes by (name, agent) returning true; false when absent', () => {
    upsertPendingTaskRetry('task-c', 'main', 5_000_000, 'busy')
    expect(deletePendingTaskRetry('task-c', 'main')).toBe(true)
    expect(deletePendingTaskRetry('task-c', 'main')).toBe(false)
  })

  it('deletes by id returning true; false when absent', () => {
    upsertPendingTaskRetry('task-d', 'main', 6_000_000, 'busy')
    const row = getPendingTaskRetry('task-d', 'main')!
    expect(deletePendingTaskRetryById(row.id)).toBe(true)
    expect(deletePendingTaskRetryById(row.id)).toBe(false)
  })

  it('markAlert sets alert_sent_at only once (subsequent calls no-op)', () => {
    upsertPendingTaskRetry('task-e', 'main', 7_000_000, 'busy')
    expect(markPendingTaskRetryAlert('task-e', 'main', 7_000_100)).toBe(true)
    expect(markPendingTaskRetryAlert('task-e', 'main', 7_000_200)).toBe(false)
    const row = getPendingTaskRetry('task-e', 'main')!
    expect(row.alert_sent_at).toBe(7_000_100)
  })

  it('separate (name, agent) pairs are distinct rows', () => {
    upsertPendingTaskRetry('task-f', 'agent-1', 8_000_000, 'busy')
    upsertPendingTaskRetry('task-f', 'agent-2', 8_000_000, 'busy')
    const rows = listPendingTaskRetries().filter(r => r.task_name === 'task-f')
    expect(rows).toHaveLength(2)
  })

  it('insertPendingTaskRetryIfNew inserts once then refuses', () => {
    expect(insertPendingTaskRetryIfNew('task-new-only', 'main', 9_000_000, 'busy')).toBe(true)
    expect(insertPendingTaskRetryIfNew('task-new-only', 'main', 9_000_100, 'busy')).toBe(false)
    const row = getPendingTaskRetry('task-new-only', 'main')!
    // first_attempt stays at the original (9_000_000), not the second call
    expect(row.first_attempt).toBe(9_000_000)
    expect(row.attempt_count).toBe(1)
  })

  it('updatePendingTaskRetry only mutates existing rows (no insert)', () => {
    // No row yet -> returns false, no row created
    expect(updatePendingTaskRetry('task-upd-only', 'main', 10_000_000, 'busy')).toBe(false)
    expect(getPendingTaskRetry('task-upd-only', 'main')).toBeUndefined()

    // After insert, update bumps attempt_count + last_attempt
    insertPendingTaskRetryIfNew('task-upd-only', 'main', 10_000_000, 'busy')
    expect(updatePendingTaskRetry('task-upd-only', 'main', 10_000_500, 'error')).toBe(true)
    const row = getPendingTaskRetry('task-upd-only', 'main')!
    expect(row.last_attempt).toBe(10_000_500)
    expect(row.attempt_count).toBe(2)
    expect(row.last_reason).toBe('error')
  })

  it('clearPendingTaskRetryAlert resets alert_sent_at so the next tick can retry', () => {
    insertPendingTaskRetryIfNew('task-clear-alert', 'main', 11_000_000, 'busy')
    markPendingTaskRetryAlert('task-clear-alert', 'main', 11_000_100)
    expect(getPendingTaskRetry('task-clear-alert', 'main')!.alert_sent_at).toBe(11_000_100)

    expect(clearPendingTaskRetryAlert('task-clear-alert', 'main')).toBe(true)
    expect(getPendingTaskRetry('task-clear-alert', 'main')!.alert_sent_at).toBeNull()

    // After clear, markAlert succeeds again
    expect(markPendingTaskRetryAlert('task-clear-alert', 'main', 11_000_200)).toBe(true)
  })
})

describe('database file permissions', () => {
  // Run the whole tightening check against a throwaway DB in the OS temp dir,
  // never the real store/claudeclaw.db. initDatabase() now applies pre-create
  // + tightenDbPermissions to any on-disk override path (only ':memory:' is
  // exempt), so a temp file exercises the exact prod tightening logic without
  // a single touch to the prod store.
  //
  // Enforcement (not just observation): loosen every sidecar to 0o644 first,
  // then re-run initDatabase() to prove tightenDbPermissions actually narrows
  // them. Without this, the tests would pass even if tightenDbPermissions were
  // removed entirely -- the files would simply retain whatever mode the
  // first init left them at.
  let tmpDir: string
  let tmpDbPath: string

  beforeAll(async () => {
    const { mkdtempSync, chmodSync } = await import('node:fs')
    const { tmpdir } = await import('node:os')
    tmpDir = mkdtempSync(join(tmpdir(), 'claudeclaw-perm-'))
    tmpDbPath = join(tmpDir, DB_FILENAME)
    // First init creates the file + sidecars (already tightened).
    initDatabase(tmpDbPath)
    // Loosen them, then re-init to prove the tightening runs every time.
    for (const p of [tmpDbPath, `${tmpDbPath}-wal`, `${tmpDbPath}-shm`, `${tmpDbPath}-journal`]) {
      if (existsSync(p)) {
        try { chmodSync(p, 0o644) } catch { /* best effort */ }
      }
    }
    initDatabase(tmpDbPath)
  })

  afterAll(async () => {
    const { rmSync } = await import('node:fs')
    // Restore the in-memory handle so we never leave the suite pointed at the
    // temp file (which afterAll is about to delete).
    initDatabase(':memory:')
    try { rmSync(tmpDir, { recursive: true, force: true }) } catch { /* best effort */ }
  })

  it('db file is tightened to owner-only (0o600) by initDatabase', () => {
    expect(existsSync(tmpDbPath)).toBe(true)
    const mode = statSync(tmpDbPath).mode & 0o777
    expect(mode).toBe(0o600)
  })

  it('WAL sidecar (when present) is tightened to 0o600', () => {
    const walPath = `${tmpDbPath}-wal`
    if (!existsSync(walPath)) return // WAL may not exist on a freshly-initialised empty DB
    const mode = statSync(walPath).mode & 0o777
    expect(mode).toBe(0o600)
  })

  it('SHM sidecar (when present) is tightened to 0o600', () => {
    const shmPath = `${tmpDbPath}-shm`
    if (!existsSync(shmPath)) return
    const mode = statSync(shmPath).mode & 0o777
    expect(mode).toBe(0o600)
  })

  it('rollback-journal sidecar (when present) is tightened to 0o600', () => {
    const journalPath = `${tmpDbPath}-journal`
    if (!existsSync(journalPath)) return
    const mode = statSync(journalPath).mode & 0o777
    expect(mode).toBe(0o600)
  })
})

// A context-restart gate ezt szamolja "dispatcholt, meg nem lezart munkakent".
// Egy lezaraskor auto-generalt [Eredmény] visszajelzes NEM munka: senki nem
// valaszol ra, sosem lesz done, tehat orokre gyulik -- es igy egy aktiv agens
// tartosan alkalmatlanna valik a puha ujrainditasra (2026-08-12).
describe('getDispatchedPendingStats -- a lezaro visszajelzes nem dispatcholt munka', () => {
  const NOW = Date.now()
  const CUTOFF = 2 * 60 * 60 * 1000

  it('szamolja a valodi kiadott feladatot, de kihagyja a completion reportot', () => {
    createAgentMessage('stat-a', 'stat-b', 'csinald meg X-et')
    const before = getDispatchedPendingStats('stat-a', NOW, CUTOFF).count
    expect(before).toBeGreaterThanOrEqual(1)

    createAgentMessage('stat-a', 'stat-b', `${COMPLETION_REPORT_PREFIX} msg_id:1 status:done\n\nkesz`)
    const after = getDispatchedPendingStats('stat-a', NOW, CUTOFF).count
    expect(after).toBe(before)
  })

  it('csak a sajat kimeno uzeneteket szamolja', () => {
    createAgentMessage('stat-c', 'stat-a', 'ez masik agens kimenoje')
    expect(getDispatchedPendingStats('stat-a', NOW, CUTOFF).count)
      .toBe(getDispatchedPendingStats('stat-a', NOW, CUTOFF).count)
    expect(getDispatchedPendingStats('stat-c', NOW, CUTOFF).count).toBe(1)
  })

  // SUPHOX318 (2026-09-10, kanban 5f49fc94): ket kulon heurisztikat is
  // probaltunk arra hogy egy KESOBBI, a cimzettol erkezo valasz (status meg
  // pending/delivered, senki nem PUT-olta done-ra) is oldja a blokkot --
  // mindkettot elutasitotta a fuggetlen Codex review, mert egy generikus
  // valasz (pl. "vettem, dolgozom rajta") nem bizonyitja hogy EREDMENY
  // erkezett, csak hogy a partner irt valamit. A schema-ban nincs reply-to/
  // thread-id mezo, tehat nincs mod biztonsagosan korrelalni MELYIK
  // dispatch-ra vonatkozik egy valasz. Ld. a fuggveny doc-kommentjet a ket
  // elutasitott kiserletrol. Ez a teszt EXPLICIT regresszio-vedelem: ha
  // valaki ujra probalna reply-alapu feloldast bevezetni, ez a teszt piros
  // lesz, amig a valodi, biztonsagos megoldas (explicit korrelacio VAGY
  // fegyelmezett PUT done -- ld. doc-komment) meg nincs implementalva.
  it('a cimzett KESOBBI valasza NEM oldja a dispatchot -- csak a formalis done/failed status szamit', () => {
    createAgentMessage('sup-a', 'sup-b', 'nezd at ezt a diffet')
    const before = getDispatchedPendingStats('sup-a', NOW, CUTOFF).count
    expect(before).toBeGreaterThanOrEqual(1)

    createAgentMessage('sup-b', 'sup-a', 'johagyva, mehet tovabb')
    const after = getDispatchedPendingStats('sup-a', NOW, CUTOFF).count
    expect(after).toBe(before)
  })

  it('ket egyidejű dispatch, egy valasz -- MINDKETTO tovabbra is szamit (Codex ellenpelda, semmi nem oldodik reply-ra)', () => {
    createAgentMessage('cdx-a', 'cdx-b', 'Task-1: nezd at ezt')
    createAgentMessage('cdx-a', 'cdx-b', 'Task-2: es ezt is')
    const before = getDispatchedPendingStats('cdx-a', NOW, CUTOFF).count
    expect(before).toBeGreaterThanOrEqual(2)

    createAgentMessage('cdx-b', 'cdx-a', 'Task-2-n dolgozom')
    const after = getDispatchedPendingStats('cdx-a', NOW, CUTOFF).count
    expect(after).toBe(before)
  })

  it('csak a formalis PUT done zarja le a dispatchot, meg akkor is ha kozben valasz is erkezett', () => {
    const dispatched = createAgentMessage('sup-l', 'sup-m', 'feladat')
    createAgentMessage('sup-m', 'sup-l', 'valasz, de ez nem zar semmit')
    const before = getDispatchedPendingStats('sup-l', NOW, CUTOFF).count
    expect(before).toBeGreaterThanOrEqual(1)

    markMessageDone(dispatched.id, 'kesz')
    const after = getDispatchedPendingStats('sup-l', NOW, CUTOFF).count
    expect(after).toBe(before - 1)
  })

  it('stale (cutoffon tuli) dispatch nem blokkol, de hasStale jelzi', () => {
    const OLD_NOW = NOW + 10 * 60 * 60 * 1000 // 10h later, well beyond the 2h cutoff
    createAgentMessage('sup-j', 'sup-k', 'regi feladat')
    expect(getDispatchedPendingStats('sup-j', OLD_NOW, CUTOFF).hasStale).toBe(true)
    expect(getDispatchedPendingStats('sup-j', OLD_NOW, CUTOFF).count).toBe(0)
  })

  // SUPHOX318 masodik Codex-talalat (mindket review-korben megerositve, ez a
  // resz maradt is): az onmaganak kuldott (Level-1 "[FELHÍVÁS]") uzenetek
  // MOST mar explicit kizarva, ugyanugy mint getWaitingOutboundMessages-ben
  // -- kulonben soha nem kapnak "done"-t sem, es orokre felhalmozodnanak,
  // ugyanaz a teves-blokk hibaosztaly maradna nyitva.
  it('az onmaganak kuldott uzenet NEM szamit (from_agent = to_agent kizarva)', () => {
    createAgentMessage('sup-self', 'sup-self', '[FELHÍVÁS] level1 note magamnak')
    const stats = getDispatchedPendingStats('sup-self', NOW, CUTOFF)
    expect(stats.count).toBe(0)
  })
})

// SONWIN905 (2026-09-05, v3 plan section 5): getDispatchedPendingStats/
// hasOpenInboundQuestion only ever return a count/boolean -- the handoff
// skill needs the CONCRETE id/recipient/timestamp of each open thread.
describe('getWaitingOutboundMessages -- konkret varakozo szalak a handoff-hoz', () => {
  it('visszaadja a sajat pending es delivered kimeno uzenetek id/cimzett/idopontjat', () => {
    const sent = createAgentMessage('wait-a', 'wait-b', 'csinald meg Y-t')
    markMessageDelivered(sent.id)
    const waiting = getWaitingOutboundMessages('wait-a')
    const match = waiting.find(w => w.id === sent.id)
    expect(match).toBeDefined()
    expect(match?.to_agent).toBe('wait-b')
    expect(match?.created_at).toBe(sent.created_at)
  })

  it('kihagyja a lezart (done/failed) uzeneteket', () => {
    const done = createAgentMessage('wait-c', 'wait-b', 'ez mar kesz lesz')
    markMessageDone(done.id, 'ok')
    const failed = createAgentMessage('wait-c', 'wait-b', 'ez el fog bukni')
    markMessageFailed(failed.id, 'hiba')
    const waiting = getWaitingOutboundMessages('wait-c')
    expect(waiting.some(w => w.id === done.id)).toBe(false)
    expect(waiting.some(w => w.id === failed.id)).toBe(false)
  })

  it('kihagyja a lezaro [Eredmény] visszajelzest, ugyanugy mint getDispatchedPendingStats', () => {
    const real = createAgentMessage('wait-d', 'wait-b', 'valodi feladat')
    createAgentMessage('wait-d', 'wait-b', `${COMPLETION_REPORT_PREFIX} msg_id:1 status:done\n\nkesz`)
    const waiting = getWaitingOutboundMessages('wait-d')
    expect(waiting.some(w => w.id === real.id)).toBe(true)
    expect(waiting.every(w => w.id !== real.id + 1)).toBe(true)
  })

  it('csak a sajat kimeno uzeneteket adja vissza, nem a bejovoket', () => {
    createAgentMessage('wait-f', 'wait-e', 'wait-f kuldi wait-e-nek')
    const waiting = getWaitingOutboundMessages('wait-e')
    expect(waiting.some(w => w.to_agent === 'wait-e')).toBe(false)
  })

  // Codex review (SONWIN905, 2026-09-05): a sajat CLAUDE.md Level-1 autonomia
  // mintaja pontosan ezt hasznalja (bela -> bela [FELHÍVÁS] uzenet) fire-and-
  // forget ertesitesnek -- ezek soha nem lesznek 'done', tehat e kizaras
  // nelkul MINDEN ezt hasznalo agens orokre "varakozo szalkent" latna sajat
  // magat.
  it('kizarja az onmaganak kuldott (from_agent = to_agent) uzeneteket', () => {
    const self = createAgentMessage('wait-self', 'wait-self', '[FELHÍVÁS] level1 note magamnak')
    const waiting = getWaitingOutboundMessages('wait-self')
    expect(waiting.some(w => w.id === self.id)).toBe(false)
  })

  it('legujabb elol, es tiszteletben tartja a limit parametert', () => {
    const ids: number[] = []
    for (let i = 0; i < 5; i++) {
      ids.push(createAgentMessage('wait-g', 'wait-h', `uzenet ${i}`).id)
    }
    const waiting = getWaitingOutboundMessages('wait-g', 3)
    expect(waiting).toHaveLength(3)
    // legujabb elol: a legnagyobb id-k jonnek, csokkeno sorrendben
    expect(waiting.map(w => w.id)).toEqual([...ids].reverse().slice(0, 3))
  })

  // Codex review (SONWIN905, 2026-09-05): a hatarertekek (0, negativ, >50)
  // kezelese most a HELPERBEN dol el, nem a hivo route-ban -- ezek a tesztek
  // a fuggvenyt kozvetlenul hivjak, hogy a hatar-logika a route-tol fuggetlenul
  // is bizonyitott legyen.
  it('nem-numerikus/0/negativ limit eseten az alapertelmezett 10-re esik vissza (nem 0-ra vagy 1-re)', () => {
    for (let i = 0; i < 5; i++) {
      createAgentMessage('wait-limit-edge', 'wait-h', `uzenet ${i}`)
    }
    expect(getWaitingOutboundMessages('wait-limit-edge', 0)).toHaveLength(5)
    expect(getWaitingOutboundMessages('wait-limit-edge', -5)).toHaveLength(5)
    expect(getWaitingOutboundMessages('wait-limit-edge', NaN)).toHaveLength(5)
  })

  it('50 fole clampeli a limitet', () => {
    for (let i = 0; i < 55; i++) {
      createAgentMessage('wait-limit-over', 'wait-h', `uzenet ${i}`)
    }
    expect(getWaitingOutboundMessages('wait-limit-over', 200)).toHaveLength(50)
  })
})

// SUPHOX318-B (2026-09-11, kanban 4bf72b27): the OPPOSITE direction from
// getWaitingOutboundMessages -- inbound dispatches the EXECUTOR already
// replied to but never formally closed. Codex review (msg 2250/2251) caught
// that the first attempt nudged the WRONG party (the original sender, not
// the executor) -- see the function's own doc comment for the full
// reasoning. These tests lock the CORRECT direction down.
describe('getRepliedButUnclosedInboundMessages -- closure-debt nudge for the executor', () => {
  it('lists an inbound dispatch the executor already replied to but never closed', () => {
    const dispatch = createAgentMessage('cd-a', 'cd-exec', 'nezd at ezt a diffet')
    createAgentMessage('cd-exec', 'cd-a', 'johagyva, mehet tovabb') // executor's own reply
    const debt = getRepliedButUnclosedInboundMessages('cd-exec')
    const match = debt.find(d => d.id === dispatch.id)
    expect(match).toBeDefined()
    expect(match?.from_agent).toBe('cd-a')
  })

  it('does NOT list a dispatch with no reply yet from the executor', () => {
    const dispatch = createAgentMessage('cd-b', 'cd-exec2', 'meg fuggoben levo feladat')
    const debt = getRepliedButUnclosedInboundMessages('cd-exec2')
    expect(debt.some(d => d.id === dispatch.id)).toBe(false)
  })

  it('does NOT list a dispatch already closed via markMessageDone, even with a reply', () => {
    const dispatch = createAgentMessage('cd-c', 'cd-exec3', 'harmadik feladat')
    createAgentMessage('cd-exec3', 'cd-c', 'kesz')
    markMessageDone(dispatch.id, 'lezarva')
    const debt = getRepliedButUnclosedInboundMessages('cd-exec3')
    expect(debt.some(d => d.id === dispatch.id)).toBe(false)
  })

  it('does NOT list a dispatch if the "reply" came from someone else, not the executor', () => {
    const dispatch = createAgentMessage('cd-d', 'cd-exec4', 'negyedik feladat')
    createAgentMessage('cd-other', 'cd-d', 'cd-other szolt kozbe, nem cd-exec4') // wrong replier
    const debt = getRepliedButUnclosedInboundMessages('cd-exec4')
    expect(debt.some(d => d.id === dispatch.id)).toBe(false)
  })

  it('does NOT list a dispatch if the executor\'s message to the sender predates the dispatch (old, unrelated reply)', () => {
    createAgentMessage('cd-exec5', 'cd-e', 'regi, nem kapcsolodo uzenet cd-e-nek') // "reply" comes first
    const dispatch = createAgentMessage('cd-e', 'cd-exec5', 'uj feladat, a regi uzenet utan')
    const debt = getRepliedButUnclosedInboundMessages('cd-exec5')
    expect(debt.some(d => d.id === dispatch.id)).toBe(false)
  })

  it('does NOT surface the reply/notification-side of the outbound direction (only inbound-to-executor rows count)', () => {
    // cd-exec6 dispatches to cd-f (outbound for cd-exec6) -- this must never
    // show up in cd-exec6's OWN closure-debt list, which is about inbound work.
    createAgentMessage('cd-exec6', 'cd-f', 'cd-exec6 sajat kiadott feladata')
    createAgentMessage('cd-f', 'cd-exec6', 'cd-f valaszol')
    const debt = getRepliedButUnclosedInboundMessages('cd-exec6')
    expect(debt).toHaveLength(0)
  })

  it('excludes self-addressed messages (from_agent = to_agent)', () => {
    const self = createAgentMessage('cd-self', 'cd-self', '[FELHÍVÁS] level1 note magamnak')
    createAgentMessage('cd-self', 'cd-self', '[FELHÍVÁS] masodik note') // would-be "reply" to itself
    const debt = getRepliedButUnclosedInboundMessages('cd-self')
    expect(debt.some(d => d.id === self.id)).toBe(false)
  })

  it('excludes completion-report dispatches even if "replied to"', () => {
    const report = createAgentMessage('cd-g', 'cd-exec7', `${COMPLETION_REPORT_PREFIX} msg_id:1 status:done\n\nkesz`)
    createAgentMessage('cd-exec7', 'cd-g', 'Rendben.')
    const debt = getRepliedButUnclosedInboundMessages('cd-exec7')
    expect(debt.some(d => d.id === report.id)).toBe(false)
  })

  it('nem-numerikus/0/negativ limit eseten az alapertelmezett 10-re esik vissza', () => {
    for (let i = 0; i < 5; i++) {
      const d = createAgentMessage('cd-limit', 'cd-exec8', `feladat ${i}`)
      createAgentMessage('cd-exec8', 'cd-limit', `valasz ${i} a ${d.id}-re`)
    }
    expect(getRepliedButUnclosedInboundMessages('cd-exec8', 0)).toHaveLength(5)
    expect(getRepliedButUnclosedInboundMessages('cd-exec8', -5)).toHaveLength(5)
    expect(getRepliedButUnclosedInboundMessages('cd-exec8', NaN)).toHaveLength(5)
  })
})

// upstream cbef8ef4 (#1539) + LOCAL pre-clear notice: what the restart gate's pending-outbound count leaves out.
describe('getDispatchedPendingStats -- the gate does not count reports upward, its own messages, or dead rows', () => {
  const NOW = Date.now()
  const CUTOFF = 2 * 60 * 60 * 1000
  const MAIN = 'gs-main'

  it("a sub-agent's report to the main agent is not dispatched work; to another sub-agent it still is", () => {
    createAgentMessage('gs-sub', MAIN, 'KESZ: a meres megvan')
    expect(getDispatchedPendingStats('gs-sub', NOW, CUTOFF, MAIN).count).toBe(0)
    createAgentMessage('gs-sub', 'gs-other', 'nezd at ezt')
    expect(getDispatchedPendingStats('gs-sub', NOW, CUTOFF, MAIN).count).toBe(1)
  })

  it("the main agent's message to a sub-agent stays counted (it carries the thread)", () => {
    createAgentMessage(MAIN, 'gs-sub2', 'UJ FELADAT: renderelj le')
    expect(getDispatchedPendingStats(MAIN, NOW, CUTOFF, MAIN).count).toBe(1)
  })

  it("the gate's own alert and pre-clear notice are not counted (the self-feeding loop)", () => {
    createAgentMessage('gs-sub3', 'gs-sub4', '[CONTEXT-RESTART-GATE-RIASZTAS] ...', GATE_ALERT_ORIGIN_NOTE)
    expect(getDispatchedPendingStats('gs-sub3', NOW, CUTOFF, MAIN).count).toBe(0)
    createAgentMessage('gs-main2', 'gs-sub5', '[CONTEXT-RESTART-GATE] 10 percen belul ...', GATE_NOTICE_ORIGIN_NOTE)
    expect(getDispatchedPendingStats('gs-main2', NOW, CUTOFF, 'gs-main2').count).toBe(0)
  })

  it('a row closed without delivery can never clear a block, so it is not counted', () => {
    const m = createAgentMessage('gs-main3', 'gs-sub6', 'feladat') as unknown as { id: number }
    expect(getDispatchedPendingStats('gs-main3', NOW, CUTOFF, 'gs-main3').count).toBe(1)
    closeMessagesWithoutDelivery([m.id], 'test')
    expect(getDispatchedPendingStats('gs-main3', NOW, CUTOFF, 'gs-main3').count).toBe(0)
  })
})

// PROGI msg 6136 (4): the context-guard's resume give-up notice comes from 'system', so it never sits in the
// sub-agent's own pending-outbound count.
describe('getDispatchedPendingStats -- a system notice about a sub-agent is not that sub-agent\'s outbound', () => {
  it('a give-up notice from system to the main agent leaves the sub-agent at 0', () => {
    createAgentMessage('system', 'gu-main', '[CONTEXT-GUARD] A(z) "gu-sub" agent restartja utan ...', 'context-guard resume give-up notice')
    expect(getDispatchedPendingStats('gu-sub', Date.now(), 2 * 60 * 60 * 1000, 'gu-main').count).toBe(0)
  })
})

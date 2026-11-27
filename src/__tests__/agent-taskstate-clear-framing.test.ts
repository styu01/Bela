// PROGI review 6196 (small item of b5873dd8): the task-state replay is wired to source=clear as well, and it
// used to say "Ez NEM uj feladat -- FOLYTASD onnan ahol abbamaradt" there too. A /clear is as often a deliberate
// fresh start as the context-restart gate's planned restart (and the gate never fires while a fresh record is in
// flight, so what a clear replays is a settled thread): on clear the block is data, not a resume order. The
// do-not-redo / do-not-re-delegate lists stay, and the other sources keep the resume framing.
import { afterEach, describe, expect, it } from 'vitest'
import type http from 'node:http'
import { buildTaskStateInjection, clearTaskState, writeTaskState, type AgentTaskState } from '../web/agent-taskstate.js'
import { tryHandleAgentTaskState } from '../web/routes/agent-taskstate.js'
import type { RouteContext } from '../web/routes/types.js'

const NOW = Date.now()
const rec = (over: Partial<AgentTaskState> = {}): AgentTaskState => ({
  agent: 'tester',
  doneSteps: ['merged #276'],
  alreadyDelegated: ['Zara: frontend modal'],
  nextAction: 'open the PR',
  pendingDecision: 'whether to gate on RESPAWN_ENABLED',
  summary: 'building X',
  ts: NOW,
  consumed: false,
  ...over,
})

describe('buildTaskStateInjection framing by source', () => {
  it('on clear: data, no resume order, the guard lists and the items kept', () => {
    const out = buildTaskStateInjection(rec(), 'clear')
    expect(out).toContain('TASK-FOLYTATAS') // the gate's wake prompt points at this keyword
    expect(out).toContain('ADAT, nem utasitas')
    expect(out).toContain('ne folytasd magadtol')
    expect(out).not.toMatch(/FOLYTASD onnan|NEM uj feladat|innen folytasd/)
    expect(out).toContain('NE delegald ujra')
    for (const item of ['building X', 'merged #276', 'Zara: frontend modal', 'open the PR', 'whether to gate']) {
      expect(out).toContain(item)
    }
  })

  it.each(['compact', 'resume', 'startup', ''])('on %s: the resume framing is unchanged', (source) => {
    const out = buildTaskStateInjection(rec(), source)
    expect(out).toContain('TASK-FOLYTATAS (NEM uj feladat)')
    expect(out).toContain('FOLYTASD onnan ahol abbamaradt')
    expect(out).toContain('KOVETKEZO AKCIO (innen folytasd): open the PR')
    expect(out).not.toContain('/clear')
  })
})

describe('GET /api/agent-taskstate/:agent/replay passes the source on', () => {
  const A = 'vitest-taskstate-clear-route'
  afterEach(() => clearTaskState(A))

  async function replay(source: string): Promise<string | null> {
    let body = ''
    const res = { writeHead: () => res, end: (b: string) => { body = b } } as unknown as http.ServerResponse
    const url = new URL(`http://x/api/agent-taskstate/${A}/replay?source=${source}`)
    const ctx = { req: {} as http.IncomingMessage, res, path: url.pathname, method: 'GET', url } as RouteContext
    expect(await tryHandleAgentTaskState(ctx)).toBe(true)
    return (JSON.parse(body) as { additionalContext: string | null }).additionalContext
  }

  it('clear gets the data framing, compact the resume framing', async () => {
    writeTaskState(A, { summary: 's', nextAction: 'next', doneSteps: ['x'] }, Date.now())
    expect(await replay('clear')).toContain('ne folytasd magadtol')
    expect(await replay('compact')).toContain('FOLYTASD onnan ahol abbamaradt')
  })
})

import { describe, it, expect, vi } from 'vitest'

// MSGWARN908 (ported from upstream Szotasz/marveen 1ace193, 2026-09-10): the
// main agent's tmux session is MAIN_CHANNELS_SESSION (`${MAIN_AGENT_ID}-channels`,
// launchd/systemd via channels.sh), not `agent-<name>`. GET /api/agents/<main-id>
// is reachable (isKnownAgent explicitly allows the main id) and used to report
// a running main agent as 'stopped', because getAgentSummary probed the
// `agent-<name>` session that never exists for it. Verified live on this
// checkout before the fix: capturePane(MAIN_CHANNELS_SESSION) is never
// consulted at all for the main id, only the always-empty agent-<name> tmux
// session -- so GET /api/agents/<mainId> unconditionally returned running:false.
//
// Mocks node:child_process so `tmux capture-pane -t <MAIN_CHANNELS_SESSION>`
// returns real-looking output (main is up) while `tmux list-sessions` never
// reports an `agent-<name>` session (no sub-agent tmux exists in the test env)
// -- the same asymmetry that made the old code always say 'stopped' for main.

const h = vi.hoisted(() => ({ PANE: '\n> some live claude output\n', calls: [] as string[][] }))

vi.mock('node:child_process', async (orig) => ({
  ...(await orig() as object),
  execFileSync: vi.fn((_file: string, args?: string[]) => {
    if (Array.isArray(args)) {
      h.calls.push(args)
      if (args.includes('capture-pane')) return h.PANE
      if (args.includes('list-sessions')) return '' // no sub-agent session exists in test env
    }
    return ''
  }),
}))

import { MAIN_AGENT_ID } from '../config.js'
import { MAIN_CHANNELS_SESSION } from '../web/main-agent.js'
import { sessionOfTmuxTarget } from '../tmux-target.js'
import { tryHandleAgents } from '../web/routes/agents.js'
import type { RouteContext } from '../web/routes/types.js'

function fakeCtx(path: string, method: string): {
  ctx: RouteContext
  out: { status: number; body: Record<string, unknown> | null }
} {
  const out: { status: number; body: Record<string, unknown> | null } = { status: 0, body: null }
  const res = {
    writeHead(status: number) { out.status = status; return res },
    end(chunk?: string) { if (chunk) out.body = JSON.parse(chunk) as Record<string, unknown> },
  }
  const url = new URL(`http://localhost:3420${path}`)
  const ctx = { req: {} as RouteContext['req'], res, path: url.pathname, method, url } as RouteContext
  return { ctx, out }
}

describe('GET /api/agents/<main-id> run state (MSGWARN908)', () => {
  it('reports the main agent as running when its channels pane is alive', async () => {
    const { ctx, out } = fakeCtx(`/api/agents/${MAIN_AGENT_ID}`, 'GET')
    const handled = await tryHandleAgents(ctx, process.cwd() + '/web')

    expect(handled).toBe(true)
    expect(out.status).toBe(200)
    expect(out.body?.running).toBe(true)
    expect(out.body?.runState).toBe('running')
    expect(out.body?.session).toBe(MAIN_CHANNELS_SESSION)

    // The fix must probe the channels session, not the agent-<name> template
    // (which never exists for the main id) -- guards against a regression that
    // re-derives session from agentSessionName() and silently no-ops the fix.
    const captured = h.calls.filter(a => a.includes('capture-pane'))
    // b03b7979: every tmux target is exact (=NAME:), so compare the target's session name.
    expect(captured.some(a => a.some(x => sessionOfTmuxTarget(x) === MAIN_CHANNELS_SESSION))).toBe(true)
    expect(captured.some(a => a.includes(`agent-${MAIN_AGENT_ID}`))).toBe(false)
  })
})

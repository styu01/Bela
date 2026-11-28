// KISPROGI 6584 K2: agentPositivelyStopped() is true only on PROOF that a local agent's session is gone: tmux
// answered without it, or tmux exited with a numeric status (no server). A timed-out or killed probe, a spawn failure
// and a remote agent are never "positively stopped" (the context-restart gate drops state only on that proof).
import { describe, it, expect, vi, beforeEach } from 'vitest'

const h = vi.hoisted(() => ({ behaviour: (() => '') as () => string, remote: null as string | null }))
vi.mock('node:child_process', async (importOriginal) => {
  const real = await importOriginal<typeof import('node:child_process')>()
  return { ...real, execFileSync: vi.fn(() => h.behaviour()) }
})
vi.mock('../logger.js', () => ({ logger: { info: vi.fn(), warn: vi.fn(), debug: vi.fn(), error: vi.fn() } }))
vi.mock('../web/agent-config.js', async (importOriginal) => {
  const real = await importOriginal<typeof import('../web/agent-config.js')>()
  return { ...real, readAgentRemoteHost: vi.fn(() => h.remote) }
})

const { agentPositivelyStopped } = await import('../web/agent-process.js')

function fail(props: Record<string, unknown>): never {
  throw Object.assign(new Error('tmux probe failed'), props)
}

beforeEach(() => { h.remote = null })

describe('agentPositivelyStopped (KISPROGI 6584 K2)', () => {
  it('tmux answered: stopped exactly when the session is not in the list', () => {
    h.behaviour = () => 'agent-other\nbela-channels\n'
    expect(agentPositivelyStopped('iris')).toBe(true)
    h.behaviour = () => 'agent-iris\nbela-channels\n'
    expect(agentPositivelyStopped('iris')).toBe(false)
  })
  it('tmux exited with a status (no server running): stopped', () => {
    h.behaviour = () => fail({ status: 1, signal: null })
    expect(agentPositivelyStopped('iris')).toBe(true)
  })
  it('a timed-out, killed or unspawnable probe proves nothing', () => {
    h.behaviour = () => fail({ status: null, signal: 'SIGTERM', code: 'ETIMEDOUT' })
    expect(agentPositivelyStopped('iris')).toBe(false)
    h.behaviour = () => fail({ status: null, signal: 'SIGKILL' })
    expect(agentPositivelyStopped('iris')).toBe(false)
    h.behaviour = () => fail({ code: 'ENOENT', errno: -2 })
    expect(agentPositivelyStopped('iris')).toBe(false)
  })
  it('a remote agent is never positively stopped here', () => {
    h.remote = 'laptop'
    h.behaviour = () => ''
    expect(agentPositivelyStopped('iris')).toBe(false)
  })
})

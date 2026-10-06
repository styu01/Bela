import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  restartAgentProcess: vi.fn(),
  createAgentMessage: vi.fn(),
}))

vi.mock('../config.js', () => ({ MAIN_AGENT_ID: 'bela', SERVICE_ID: 'bela' }))
vi.mock('../logger.js', () => ({ logger: { info: vi.fn(), warn: vi.fn(), debug: vi.fn() } }))
vi.mock('../web/agent-config.js', () => ({ listAgentNames: () => [], readAgentRemoteHost: () => null }))
vi.mock('../web/agent-process.js', () => ({
  agentRunState: () => 'running',
  agentSessionName: (name: string) => `agent-${name}`,
  restartAgentProcess: mocks.restartAgentProcess,
  capturePane: () => 'idle',
}))
vi.mock('../web/main-agent.js', () => ({ MAIN_CHANNELS_SESSION: 'bela-channels' }))
vi.mock('../web/channel-monitor.js', () => ({ respawnMainSessionFresh: vi.fn() }))
vi.mock('../pane-state.js', () => ({ paneLooksIdle: () => true }))
vi.mock('../web/auto-restart-store.js', () => ({
  readAutoRestartConfig: () => ({ enabled: true, mode: 'fresh', dailyTime: null, intervalHours: 1, handoff: false }),
}))
vi.mock('../db.js', () => ({ createAgentMessage: mocks.createAgentMessage }))

import { checkAgent, resetAutoRestartStateForTests } from '../web/auto-restart-runner.js'

describe('auto-restart failure stand-down', () => {
  beforeEach(() => {
    resetAutoRestartStateForTests()
    mocks.restartAgentProcess.mockReset()
    mocks.createAgentMessage.mockReset()
  })

  it('queues a BÉLA-first alert when a due restart fails, then stands down', async () => {
    mocks.restartAgentProcess.mockRejectedValueOnce(new Error('tmux unavailable'))
    await checkAgent('progi', 0) // seed
    await checkAgent('progi', 3_600_000) // due
    expect(mocks.createAgentMessage).toHaveBeenCalledTimes(1)
    expect(mocks.createAgentMessage).toHaveBeenCalledWith(
      'system', 'bela', expect.stringContaining('[AUTO-RESTART]'), 'auto-restart failure alert',
    )

    // The failed slot is consumed: a following sweep must not restart or alert
    // again until the next scheduled interval.
    await checkAgent('progi', 3_600_001)
    expect(mocks.restartAgentProcess).toHaveBeenCalledTimes(1)
    expect(mocks.createAgentMessage).toHaveBeenCalledTimes(1)
  })

  it('does not alert when a due restart succeeds', async () => {
    mocks.restartAgentProcess.mockResolvedValueOnce(undefined)
    await checkAgent('progi', 0)
    await checkAgent('progi', 3_600_000)
    expect(mocks.restartAgentProcess).toHaveBeenCalledTimes(1)
    expect(mocks.createAgentMessage).not.toHaveBeenCalled()
  })
})

// PROGI review msg 6176: the kanban archive runner's delayed first pass (70 s) is a stored timer,
// cleared on server close together with the hourly interval.
import { afterEach, describe, expect, it, vi } from 'vitest'

const sweep = vi.fn(() => 0)
vi.mock('../db.js', () => ({ sweepArchivedKanbanCards: () => sweep() }))
vi.mock('../logger.js', () => ({ logger: { info: vi.fn(), error: vi.fn(), warn: vi.fn(), debug: vi.fn() } }))

import { startKanbanArchiveRunner, stopKanbanArchiveRunner } from '../web/kanban-archive-runner.js'

afterEach(() => { stopKanbanArchiveRunner(); vi.useRealTimers(); sweep.mockClear() })

describe('kanban archive runner stop', () => {
  it('control: left running, the first pass sweeps at 70 s and then hourly', async () => {
    vi.useFakeTimers()
    startKanbanArchiveRunner()
    await vi.advanceTimersByTimeAsync(70_001)
    expect(sweep).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(60 * 60_000)
    expect(sweep).toHaveBeenCalledTimes(2)
  })
  it('stopped within the first 70 s: no sweep at all, neither the first pass nor the hourly one', async () => {
    vi.useFakeTimers()
    startKanbanArchiveRunner()
    await vi.advanceTimersByTimeAsync(30_000)
    stopKanbanArchiveRunner()
    await vi.advanceTimersByTimeAsync(3 * 60 * 60_000)
    expect(sweep).not.toHaveBeenCalled()
  })
})

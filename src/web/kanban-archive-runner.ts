import { logger } from '../logger.js'
import { sweepArchivedKanbanCards } from '../db.js'

// Upstream cca1e519: this sweep used to run inside listKanbanCards(), i.e. on every READ of the
// board -- the act of looking at the kanban changed the kanban (on this install: every dashboard
// board load and every channel-monitor recovery brief). listKanbanCards() is a pure read now and
// the archiving happens on a clock instead of on whoever happens to look.
//
// The danger in this change is the opposite one, and the wiring is asserted by a test: if the sweep
// leaves the read path and is not started anywhere, KANBAN_ARCHIVE_DONE_DAYS becomes a silent no-op.
//
// Hourly, not per-minute: the cutoff is measured in DAYS (default 30). The first pass is delayed
// like the neighbouring runners so boot is not a burst of work.

const INITIAL_DELAY_MS = 70_000
const INTERVAL_MS = 60 * 60_000

function sweep(): void {
  try {
    const archived = sweepArchivedKanbanCards()
    if (archived > 0) logger.info({ archived }, 'Kanban archive sweep')
  } catch (err) {
    // Never let a maintenance sweep take the process down: best-effort, the next tick retries.
    logger.error({ err }, 'Kanban archive sweep failed')
  }
}

// Both timers are kept, so a server close stops the delayed first pass too (PROGI review msg 6176):
// otherwise a close within the first 70 s still ran one sweep against a closing process.
let firstPass: NodeJS.Timeout | null = null
let interval: NodeJS.Timeout | null = null

export function startKanbanArchiveRunner(): NodeJS.Timeout {
  firstPass = setTimeout(() => { firstPass = null; sweep() }, INITIAL_DELAY_MS)
  interval = setInterval(sweep, INTERVAL_MS)
  return interval
}

export function stopKanbanArchiveRunner(): void {
  if (firstPass) { clearTimeout(firstPass); firstPass = null }
  if (interval) { clearInterval(interval); interval = null }
}

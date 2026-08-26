// Tracks the exact text most recently typed into each session's pane via
// sendPromptToSession(), so a stuck-input machine-origin check can fall back
// to "is the visible fragment part of something we ACTUALLY just sent?" when
// the position-anchored prefix check (pane-state.ts MACHINE_ORIGIN_PREFIXES)
// misses because the true opening line has scrolled out of the TUI's own
// bounded input-box view.
//
// Root cause (measured 2026-08-19, not assumed): sendPromptToSession()
// collapses a multi-line prompt into ONE long line and types it via chunked
// `send-keys -l`. When that line overflows the input box's visible height,
// Claude Code's own TUI auto-scrolls its bounded viewport to the cursor --
// this is an in-place redraw, not terminal-native scrollback. A live A/B
// test (capture-pane -p vs. capture-pane -p -S -200) on a genuine stuck
// state confirmed the missing opening text is not recoverable from ANY
// amount of extra scrollback: it was never painted to a terminal row that
// persists. Re-deriving "was this machine-injected?" purely from a screen
// scrape can therefore never fully solve this -- only knowing what was
// actually sent can.
//
// Deliberately kept OUT of pane-state.ts: that module documents itself as
// dependency-free and pure (screen-buffer in, verdict out) specifically for
// straightforward unit testing against captured pane fixtures. This module
// holds mutable, time-based state instead, so it stays separate.

import { SCHEDULED_TASK_ORIGIN_PREFIXES } from '../pane-state.js'

export interface SentTextEntry {
  text: string
  sentAt: number
}

// Session -> the last prompt text sent via sendPromptToSession(), with the
// timestamp it was sent at. One entry per session (a later send overwrites
// an earlier one -- only the most recent injection is ever relevant for the
// machine-origin fallback check).
const registry: Map<string, SentTextEntry> = new Map()

// How long a recorded send stays eligible for the fallback match. Sized
// against the measured stuck-input recovery timeline (channel-monitor.ts
// MAIN_STUCK_THRESHOLDS): confirmMs 90s + up to 4 attempts at ~45s dedup
// spacing is ~270s (4.5 min) worst case before soft recovery exhausts and
// hard-restart escalation can kick in. 10 minutes leaves a generous margin
// over that whole window without keeping stale entries around indefinitely.
export const SENT_TEXT_TTL_MS = 10 * 60 * 1000

// Same whitespace normalisation pane-state.ts's parkedInputText() applies to
// a captured box, so both sides of the substring comparison are collapsed
// the same way (terminal soft-wrap folds a long line's spaces/newlines
// inconsistently depending on exact column width at capture time).
function normalise(s: string): string {
  return s.replace(/\s+/g, ' ').trim()
}

/** Record that `text` was just sent (or is about to be sent) to `session`. */
export function recordSentText(session: string, text: string, now: number = Date.now()): void {
  registry.set(session, { text: normalise(text), sentAt: now })
}

/**
 * True when `visibleFragment` (the flat, normalised text currently sitting
 * parked in `session`'s input box -- i.e. pane-state.ts's parkedInputText()
 * output) is a substring of the most recent text we actually sent to that
 * session, AND that send is still within SENT_TEXT_TTL_MS.
 *
 * This is a FALLBACK only: pane-state.ts's own prefix-anchored
 * parkedMachineOriginInput() stays the fast primary path and is unaffected
 * by this module. Call this only when that check has already returned
 * false, so the common (non-scrolled) case never pays for a Map lookup.
 */
export function isKnownRecentInjection(session: string, visibleFragment: string, now: number = Date.now()): boolean {
  const entry = registry.get(session)
  if (entry == null) return false
  if (now - entry.sentAt > SENT_TEXT_TTL_MS) return false
  const flat = normalise(visibleFragment)
  if (flat.length === 0) return false
  return entry.text.includes(flat)
}

/**
 * Like isKnownRecentInjection(), but ALSO confirms the full originally-sent
 * text was specifically a scheduled-task delivery (not just any machine-
 * origin send, e.g. an inter-agent notice). The full sent text always
 * contains its true opening line -- unlike the visible pane fragment, which
 * can be a scrolled-into-view LATER portion of a long injected block -- so
 * the prefix check reliably works here even when pane-state.ts's
 * parkedScheduledTaskInput() (anchored to that same scrolled fragment)
 * misses it.
 *
 * Root cause this fixes (2026-08-25/26, Kanban c4aef78c): a multi-row
 * scheduled-task prompt got stuck with the input box showing a mid-block
 * fragment, not its "SCHEDULED TASK NOTICE" opening line. machineOrigin
 * correctly came back true via isKnownRecentInjection() (card d8c16050/
 * d417788), but parkedScheduledTaskInput() -- prefix-anchored to that same
 * scrolled fragment -- came back false, so decideStuckInputAction() fell
 * through to the generic 'hold' branch instead of the safe 'clear-scheduled'
 * remedy. softRemedy became false, and the busy-guard's deadlock carve-out
 * (machineOrigin && !softRemedy, see applyStuckRestartBusyGuard) then
 * allowed a full hard-restart for what was actually a routine, safely
 * clearable scheduled-task tick.
 */
export function isKnownRecentScheduledTaskInjection(session: string, visibleFragment: string, now: number = Date.now()): boolean {
  const entry = registry.get(session)
  if (entry == null) return false
  if (now - entry.sentAt > SENT_TEXT_TTL_MS) return false
  const flat = normalise(visibleFragment)
  if (flat.length === 0) return false
  if (!entry.text.includes(flat)) return false
  return SCHEDULED_TASK_ORIGIN_PREFIXES.some((rx) => rx.test(entry.text))
}

// Test-only: clears all recorded state so unit tests don't leak entries
// across cases sharing the module-level Map.
export function _resetSentTextRegistryForTests(): void {
  registry.clear()
}

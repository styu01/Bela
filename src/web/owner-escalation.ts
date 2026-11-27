// Generic two-stage alert escalation: BÉLA first (inter-agent notice),
// Istvan only if BÉLA has had a chance to react and the problem is still
// there (direct Telegram). Istvan's standing rule (first stated 2026-08-29
// for the scheduler's pending-retry/task-timeout alerts, restated 2026-09-02
// for stuck-input-watcher.ts): "a rendszer sajat riasztasai elobb BELA-hoz
// menjenek, csak utana Istvanhoz ha nem oldodik meg" -- the system's OWN
// internal alerts go to BÉLA first, only to Istvan if unresolved.
//
// This module is the SHARED core for every call site that alerts about a
// SUB-AGENT or fleet-infrastructure problem (as opposed to BÉLA's own main
// channel/session breaking, where paging BÉLA first makes no sense -- see
// each call site's own comment for why it's exempt). Deliberately generic
// (a string key + two pre-built message strings) rather than tied to any
// one watcher's state shape, so 15 different sites across stuck-input-
// watcher.ts, channel-monitor.ts, agent-process.ts, and agent-worker.ts can
// all share ONE implementation instead of a dozen copies of the same
// two-stage logic (kanban cf12a93a). This is the module's THIRD design
// iteration -- see the two sections below for what each review round found
// and why the shape changed each time. Read both before touching this file.
//
// ---- Round 2 (2026-09-02): timer-driven, NOT re-invocation-driven ----
// The first version evaluated "is stage 2 due" only inside escalateToOwner
// itself -- so stage 2 could only fire if a caller happened to call
// escalateToOwner AGAIN, later, for the same key. Several call sites are
// demand-driven (agent-worker.ts's alertWorkerStuck fires only when someone
// dispatches a worker task; agent-process.ts's maybeAlertSharedConfigCollision
// only on a sub-agent spawn attempt; two of channel-monitor.ts's channel-
// down alerts wrap escalateToOwner in their own pre-existing one-shot
// gates) -- for those, if nothing re-triggered the check, stage 2 would
// NEVER fire, no matter how long the problem lasted. Fixed by giving each
// key a real, cancelable Node timer scheduled the moment stage 1 succeeds,
// independent of whether the caller calls escalateToOwner again.
// Also fixed in the same round: stage2At used to stamp unconditionally
// (notifyChannel swallows send failures and never rejects) -- now routed
// through notifyChannelOrThrow (src/notify.ts) and only stamped on a
// CONFIRMED successful send, with classifyTelegramSendError (reused from
// pending-retries.ts) distinguishing a transient failure (retry soon) from
// a permanent one (back off harder, keep trying in case the config gets
// fixed mid-incident).
//
// ---- Round 3 (2026-09-02): the SAME re-invocation gap existed for stage 1
// too, one step earlier -- plus the send path itself could hang forever ----
// Two more real gaps, found reviewing the round-2 fix:
//   (1) If createAgentMessage (the stage-1 BÉLA notice) threw on a demand-
//       driven site's ONLY invocation, nothing retried -- the exact same
//       lost-escalation pattern the round-2 timer fixed for stage 2, just
//       one step earlier in the sequence, and the round-2 fix did nothing
//       for it.
//   (2) src/channel-provider.ts's telegramHttpPost had NO timeout at all
//       (raw https.request, no req.setTimeout/AbortController) -- a stalled
//       TCP connection would leave notifyChannelOrThrow's promise pending
//       forever, which round 2's fireStage2 now AWAITs directly. Fixed
//       separately in channel-provider.ts (TOOL_TIMEOUTS['telegram'], the
//       same 10s deadline src/web/telegram.ts's sendTelegramMessage already
//       uses) -- but even with that fix, THIS module still needed its own
//       retry-on-failure story for stage 1, which it never had.
//
// Fixed by unifying stage 1 and stage 2 into ONE recurring, self-scheduling
// tick per key (`_tick`), rather than two separately-shaped mechanisms:
//   - stage1At === null: attempt the BÉLA notice. Success -> stamp
//     stage1At, schedule the stage-2 tick after stage2ExtraMs. Failure ->
//     schedule a SHORT retry (STAGE1_RETRY_MS) and increment an attempt
//     counter; after STAGE1_MAX_ATTEMPTS consecutive failures, GIVE UP on
//     reaching BÉLA and fire the direct owner alert immediately instead
//     (clearly labeled as a BÉLA-unreachable fallback, not a normal
//     "BÉLA was notified" stage-2 message -- see FALLBACK_PREFIX) so
//     Istvan is still guaranteed to learn about it even if the inter-agent
//     notice path itself is broken.
//   - stage1At set, stage2At === null: attempt the direct owner alert (same
//     success/transient/permanent retry logic as round 2).
//   - stage2At set: nothing left to do, no further ticks.
// A single call to escalateToOwner is now sufficient to drive the ENTIRE
// sequence to completion (or a labeled give-up) on its own, regardless of
// whether the caller invokes it again -- closing the gap for both stages
// at all 15 sites without needing any site's own outer gate touched.
//
// ---- decideOwnerEscalation (pure predicate) is NOT what drives live timing
// ---- (documentation correction, round 3 point a)
// An earlier version of this comment implied shouldSendAlert/
// decideOwnerEscalation governed the production stage-2 decision by reuse.
// That stopped being true once the timer/tick design landed: the pure
// function below is kept as an independently useful, independently tested
// predicate (and its own tests still exercise the shouldSendAlert reuse
// directly), but escalateToOwner/`_tick` do not call it -- the timers
// scheduled by setTimeout are what actually drives stage-1/stage-2 timing
// in production now. Do not assume changing decideOwnerEscalation's
// thresholds affects live behavior; DEFAULT_STAGE2_EXTRA_MS does.
//
// ---- Process-local state, not persisted (documented tradeoff, unchanged
// from round 2) ----
// escalationState and all pending timers are in-memory only, lost on a
// dashboard restart (this happened multiple times the same day this module
// was built). Deliberately NOT persisted to disk/DB -- these watchers
// detect and resolve on a 1-5 minute scale, and a persisted version would
// need its own startup-recovery/replay logic, real complexity
// disproportionate to that window. The residual risk is a genuine,
// asymmetric PRODUCT RISK, not a "small window" to wave off (round 3 point
// c pushed back on that framing, correctly):
//   - Periodic-sweep sites (stuck-input-watcher.ts, most of channel-
//     monitor.ts) self-heal within one sweep interval after a restart -- a
//     few seconds to a minute of extra delay, not a lost incident.
//   - Demand-driven sites (agent-worker.ts's alertWorkerStuck, agent-
//     process.ts's maybeAlertSharedConfigCollision): if a restart happens
//     while an escalation is in flight, that SPECIFIC incident's
//     escalation state is gone, and recovery depends entirely on whether
//     that site's own natural trigger (a worker dispatch attempt, a
//     sub-agent spawn attempt) happens to recur afterward -- which, per
//     Codex's own example, could be never for a rarely-used worker
//     function. This is a real, standing gap for those two sites
//     specifically, not a rounding error. Closing it properly would mean
//     giving those two watchers their own dedicated periodic re-check
//     (turning them from demand-driven into periodic-sweep, matching every
//     OTHER site this module serves) -- a real architecture addition to
//     files this card did not otherwise need to touch, flagged as a
//     concrete follow-up rather than bundled into this pass.
//
// ---- Key-space is bounded, so no TTL/prune (round 3 point d) ----
// Considered adding a TTL/prune for stale Map entries (e.g. the
// agent-worker.ts key, which can sit in a 30-minute permanent-failure retry
// loop indefinitely if the underlying problem never resolves and nobody
// calls clearOwnerEscalation). Not implemented: every key in production is
// `${literal-prefix}:${session}` (or a single bare global literal) where
// `session` is drawn from a small, FIXED set (the fleet's own agent/worker
// names -- see listAgentNames()/the two hardcoded WorkerCtx sessions in
// agent-worker.ts), never user-controlled or dynamically generated per
// request. The Map's size is therefore inherently bounded by the fleet's
// own size regardless of how long any single entry survives; a stale entry
// retrying every 30 minutes against a permanently broken config is exactly
// the intended "keep trying in case it gets fixed" behavior, not a leak.
// Revisit if a future call site ever keys on something unbounded (e.g. a
// per-message or per-user identifier).
import { createAgentMessage } from '../db.js'
import { MAIN_AGENT_ID } from '../config.js'
import { notifyChannelOrThrow } from '../notify.js'
import { shouldSendAlert, classifyTelegramSendError } from '../pending-retries.js'
import { logger } from '../logger.js'

/** How long BÉLA gets, after the stage-1 notice, before an unresolved
 * problem escalates to Istvan directly. Callers may override per call site,
 * but this is the sane default for "sub-agent stuck/down" class alerts. */
export const DEFAULT_STAGE2_EXTRA_MS = 5 * 60 * 1000

/** Backoff before retrying a failed stage-1 (BÉLA) notice. Short: a DB
 * hiccup is expected to clear quickly, and we want to reach BÉLA fast. */
export const STAGE1_RETRY_MS = 30 * 1000
/** After this many consecutive stage-1 failures, stop trying to reach
 * BÉLA and fall back to alerting Istvan directly instead (labeled as a
 * BÉLA-unreachable fallback, not a normal stage-2 "already notified"
 * message) -- ~2.5 minutes of attempts before giving up on BÉLA. */
export const STAGE1_MAX_ATTEMPTS = 5
/** Prefix stamped on the owner text when stage 2 fires because BÉLA could
 * never be reached, not because BÉLA was reached but didn't resolve it in
 * time -- these are different situations and must not read the same. */
const STAGE1_UNREACHABLE_PREFIX = '🔴 [BÉLA nem volt elerheto ismetelt probalkozas utan -- kozvetlenul Neked szol]\n\n'

/** Backoff before retrying a TRANSIENT stage-2 delivery failure (network
 * blip, 429, 5xx, or the connection just hanging -- see channel-provider.ts's
 * timeout fix). Short, because these are expected to self-resolve fast. */
export const STAGE2_TRANSIENT_RETRY_MS = 60 * 1000

/** Backoff before retrying a PERMANENT stage-2 delivery failure (bad token/
 * chat id -- will not fix itself). Much longer: retrying every minute
 * against a broken config just spams the log for no benefit. Still retries
 * (rather than giving up forever) so a config fix during a long incident is
 * picked up eventually, without a dashboard restart. */
export const STAGE2_PERMANENT_RETRY_MS = 30 * 60 * 1000

interface OwnerEscalationState {
  stage1At: number | null
  stage1Attempts: number
  /** True once STAGE1_MAX_ATTEMPTS has been exhausted and this key has
   * permanently given up on reaching BÉLA for this incident. Without this
   * flag, a retry of the FALLBACK owner send (itself possible -- see
   * sendOwnerText's own retry logic) would loop back into the
   * `stage1At === null` branch on the next tick and re-attempt
   * createAgentMessage forever instead of retrying the fallback send. */
  stage1GivenUp: boolean
  stage2At: number | null
  latestBelaText: string
  latestOwnerText: string
}

const escalationState = new Map<string, OwnerEscalationState>()
const pendingTimers = new Map<string, ReturnType<typeof setTimeout>>()

function scheduleTick(key: string, delayMs: number): void {
  const existing = pendingTimers.get(key)
  if (existing) clearTimeout(existing)
  const t = setTimeout(() => { void _tick(key) }, delayMs)
  // Never let a pending escalation timer keep the process alive on its own
  // (relevant for graceful shutdown / test runners) -- unref is a no-op if
  // unavailable (browser-shimmed timer types), guarded defensively.
  if (typeof (t as unknown as { unref?: () => void }).unref === 'function') {
    ;(t as unknown as { unref: () => void }).unref()
  }
  pendingTimers.set(key, t)
}

async function sendOwnerText(key: string, text: string, onSuccess: (now: number) => void): Promise<void> {
  try {
    await notifyChannelOrThrow(text)
    logger.info({ key }, 'owner-escalation: direct owner alert sent')
    onSuccess(Date.now())
  } catch (err) {
    const kind = classifyTelegramSendError(err instanceof Error ? err.message : String(err))
    const retryMs = kind === 'transient' ? STAGE2_TRANSIENT_RETRY_MS : STAGE2_PERMANENT_RETRY_MS
    logger.warn({ err, key, kind, retryInMs: retryMs }, 'owner-escalation: owner alert delivery failed, will retry')
    if (escalationState.has(key)) scheduleTick(key, retryMs)
  }
}

async function _tick(key: string): Promise<void> {
  pendingTimers.delete(key)
  const state = escalationState.get(key)
  if (!state || state.stage2At !== null) return // cleared or already fully done

  // Still trying to reach BÉLA (and haven't yet given up permanently on
  // this incident) -- attempt stage 1. Once stage1GivenUp is true, fall
  // through to the unified owner-send attempt below on every subsequent
  // tick instead of re-entering this branch (see the field's own comment).
  if (state.stage1At === null && !state.stage1GivenUp) {
    try {
      createAgentMessage('system', MAIN_AGENT_ID, state.latestBelaText)
      logger.info({ key }, 'owner-escalation: stage-1 BÉLA notice sent')
      const now = Date.now()
      escalationState.set(key, { ...state, stage1At: now })
      scheduleTick(key, DEFAULT_STAGE2_EXTRA_MS_FOR(key))
      // A FRESH stage-1 success on THIS tick must wait for the timer just
      // scheduled above, not fall through to the stage-2 attempt below
      // immediately -- that would fire stage 2 the instant stage 1 lands,
      // skipping BÉLA's whole reaction window. Only the stage1GivenUp
      // fallback path (the catch branch below) is meant to fall through
      // within the same tick.
      return
    } catch (err) {
      const attempts = state.stage1Attempts + 1
      if (attempts >= STAGE1_MAX_ATTEMPTS) {
        logger.warn({ err, key, attempts }, 'owner-escalation: BÉLA unreachable after max attempts, falling back to direct owner alert')
        escalationState.set(key, { ...state, stage1Attempts: attempts, stage1GivenUp: true })
        // Fall through to the unified send below THIS SAME tick -- no need
        // to wait for another timer round-trip just to reach it.
      } else {
        logger.warn({ err, key, attempts }, 'owner-escalation: stage-1 BÉLA notice failed, will retry')
        escalationState.set(key, { ...state, stage1Attempts: attempts })
        scheduleTick(key, STAGE1_RETRY_MS)
        return
      }
    }
  }

  // Reached once stage 1 has EITHER succeeded (normal case: this is the
  // real stage-2 attempt) OR been permanently given up on (fallback case:
  // this sends the BÉLA-unreachable-labeled text instead). Both cases --
  // and any retry of either, on a later tick -- share the same send-and-
  // retry-on-failure logic; the ONLY difference is which text is used.
  const current = escalationState.get(key)
  if (!current || current.stage2At !== null) return
  const text = current.stage1At !== null
    ? current.latestOwnerText
    : STAGE1_UNREACHABLE_PREFIX + current.latestOwnerText
  await sendOwnerText(key, text, (now) => {
    const c = escalationState.get(key)
    if (c) escalationState.set(key, { ...c, stage2At: now })
  })
}

// Per-key stage2ExtraMs override, captured at first escalateToOwner call and
// reused by _tick (which only receives the key, not the original options).
const stage2ExtraMsByKey = new Map<string, number>()
function DEFAULT_STAGE2_EXTRA_MS_FOR(key: string): number {
  return stage2ExtraMsByKey.get(key) ?? DEFAULT_STAGE2_EXTRA_MS
}

export interface EscalateToOwnerOptions {
  /** Stable identity for this incident -- typically `${alertType}:${session}`
   * (or a bare global name for a non-session-scoped alert). Independent keys
   * never interfere with each other's stage timers; distinct alert TYPES on
   * the SAME session must use distinct keys too (see channel-monitor.ts's
   * separate channel-down-busy/channel-down-max-restarts keys) so a more
   * severe follow-up alert can never be silently swallowed by an earlier,
   * already-completed escalation cycle on a shared key. */
  key: string
  /** Stage-1 inter-agent notice text sent to BÉLA. */
  belaText: string
  /** Stage-2 direct Telegram text sent to Istvan, only if stage 1 didn't
   * resolve things by the time the escalation timer fires (or, if BÉLA
   * could never be reached at all, sent with an explicit unreachable-
   * fallback prefix instead of this text implying BÉLA was notified). */
  ownerText: string
  stage2ExtraMs?: number
}

/**
 * Call this every time your own detection logic decides "this is alert-
 * worthy right now". The FIRST call for a key starts a self-driving retry
 * sequence (see the module header for the full state machine) that no
 * longer depends on being called again: it will keep retrying stage 1 on
 * failure, then keep retrying stage 2 on failure, entirely on its own
 * timers, until one of: stage 2 confirms sent, or clearOwnerEscalation(key)
 * cancels it. A demand-driven caller that only ever calls this once still
 * gets a guaranteed outcome. Calling this again before the sequence
 * completes is safe and useful -- it refreshes the text future attempts
 * will use -- but is no longer REQUIRED.
 */
export function escalateToOwner(opts: EscalateToOwnerOptions): void {
  const { key, belaText, ownerText, stage2ExtraMs = DEFAULT_STAGE2_EXTRA_MS } = opts
  const prev = escalationState.get(key)

  if (!prev) {
    stage2ExtraMsByKey.set(key, stage2ExtraMs)
    escalationState.set(key, {
      stage1At: null,
      stage1Attempts: 0,
      stage1GivenUp: false,
      stage2At: null,
      latestBelaText: belaText,
      latestOwnerText: ownerText,
    })
    void _tick(key)
    return
  }

  // A cycle is already in flight (or done) for this key -- just keep the
  // text fresh for whatever attempt happens next; do not restart or
  // duplicate the retry sequence.
  escalationState.set(key, { ...prev, latestBelaText: belaText, latestOwnerText: ownerText })
}

/** Reset the escalation state for a key once the underlying problem is
 * confirmed resolved, so a FUTURE spell on the same key starts a fresh
 * sequence rather than being stuck permanently in "already escalated" --
 * and, critically, cancels any pending retry timer so a problem that
 * resolves mid-retry never fires a stale "still broken" alert afterward. */
/** Has the direct owner alert (stage 2) of this escalation already gone out? Lets a caller that clears a
 *  resolved incident tell the owner too, when the owner was told about it (gate self-test, PROGI 6268 d). */
export function ownerEscalationStage2Sent(key: string): boolean {
  return escalationState.get(key)?.stage2At != null
}

export function clearOwnerEscalation(key: string): void {
  escalationState.delete(key)
  stage2ExtraMsByKey.delete(key)
  const t = pendingTimers.get(key)
  if (t) {
    clearTimeout(t)
    pendingTimers.delete(key)
  }
}

/** Test seam: clears ALL escalation state and cancels ALL pending timers.
 * Not for production use. */
export function _resetAllOwnerEscalationsForTest(): void {
  for (const t of pendingTimers.values()) clearTimeout(t)
  pendingTimers.clear()
  stage2ExtraMsByKey.clear()
  escalationState.clear()
}

/** Test/introspection seam: read-only snapshot of a key's current state, or
 * null if never escalated / already cleared. Not used by production call
 * sites -- exists so tests can assert on stage1At/stage2At/stage1Attempts
 * without reaching into module-private state. */
export function _getOwnerEscalationStateForTest(key: string): Readonly<OwnerEscalationState> | null {
  return escalationState.get(key) ?? null
}

// Standalone, independently-testable pure predicate -- see the module
// header's "decideOwnerEscalation is NOT what drives live timing" note.
// Kept because it's a useful, self-contained demonstration of the
// shouldSendAlert-reuse contract, not because production code calls it.
export function decideOwnerEscalation(
  state: { stage1At: number | null; stage2At: number | null },
  nowMs: number,
  stage2ExtraMs: number = DEFAULT_STAGE2_EXTRA_MS,
): { stage1Due: boolean; stage2Due: boolean } {
  if (state.stage1At === null) return { stage1Due: true, stage2Due: false }
  const stage2Due = shouldSendAlert(nowMs, state.stage1At, state.stage2At, stage2ExtraMs)
  return { stage1Due: false, stage2Due }
}

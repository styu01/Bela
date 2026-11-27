import {
  createAgentMessage, getPendingMessages, listAgentMessages,
  getAgentConversation, getAgentConversationThreads,
  getKanbanSeqByIdPrefix,
  markMessageDone, markMessageFailed, getAgentMessage,
  closeOtelSpan,
  getPendingBacklogByAgent,
  getWaitingOutboundMessages,
  getRepliedButUnclosedInboundMessages,
  COMPLETION_REPORT_PREFIX,
  type AgentMessage,
} from '../../db.js'
import { logger } from '../../logger.js'
import { COORDINATOR_AGENT_ID } from '../../channel-coordinator/ingest.js'
import { sanitizeAgentIdent } from '../../prompt-safety.js'
import { isKnownAgent } from '../agent-config.js'
import { OWNER_NAME, SYSTEM_SENDER_IDS, parseSystemSenderIds } from '../../config.js'
import { readBody, json, jsonMaybeGzip } from '../http-helpers.js'
import { normalizeKanbanRefs } from '../kanban-ref-normalize.js'
import { parseQualifiedId, formatQualifiedId } from '../federation/address.js'
import { getFederationConfig } from '../federation/config.js'
import type { RouteContext } from './types.js'

// Should closing a message produce a reverse "[Eredmény]" notification to its sender?
//
// Exported and tested directly, rather than inlined in the PUT handler: the previous
// tests re-implemented this condition inside the test file, so they passed no matter
// what the route actually did.
//
// Three senders get no notification:
//   1. self-messages -- the sender already knows;
//   2. senders that are not addressable agents. `system` posts the [session-stuck] and
//      [handoff-failure] notices but owns no tmux session, so a reply to it can never be
//      delivered: it fails, the retry window expires, and the resulting [handoff-failure]
//      wakes the main agent -- which, once closed, produced the next one. Measured on the
//      Acrobot install 2026-08-17: 44 of the last 200 rows were `-> system`, all failed.
//      `isKnownAgent` is the right test here because it accepts MAIN_AGENT_ID as well as
//      the sub-agent directories; a plain registry lookup would have suppressed every
//      notification back to the MAIN agent, which is the case this feature exists for.
//   3. contents that are themselves completion reports -- breaks ping-pong chains.
//   4. contents that are themselves a trivial, content-free closing ack -- see
//      isTrivialClosureAck below.
export function shouldNotifyDelegator(fromAgent: string, toAgent: string, content: string): boolean {
  if (fromAgent === toAgent) return false
  if (!isKnownAgent(fromAgent)) return false
  if (content.startsWith(COMPLETION_REPORT_PREFIX)) return false
  if (isTrivialClosureAck(content)) return false
  return true
}

// SUPHOX318 (2026-09-11, kanban bcbf4511): the [Eredmény]-prefix guard above
// stops closing an auto-generated report from producing another one, but it
// does NOT cover the mechanism that actually caused the documented 8-message
// ping-pong (2026-08-28/29, codex<->bela, msg 1183-1201, traced with direct
// DB evidence): A closes a real dispatch -> auto-[Eredmény] to B -> B replies
// a plain, content-free "Rendben." (NOT itself [Eredmény]-prefixed) -> if
// THAT reply is later closed via PUT done, the guard above does not fire
// (its content doesn't start with the prefix) -> a NEW [Eredmény] goes back
// to B -> B acks again -> repeat. This is a narrow, exact-match check
// (trim + case-fold + full-string, not a prefix match) so a real, substantive
// reply that merely STARTS with an ack word ("Rendben, de meg ellenorzom
// X-et is") is never suppressed.
//
// Deliberately NOT memory.ts's SKIP_PATTERN: that list serves a different
// concern (memory-relevance filtering, where e.g. a delegated yes/no
// decision's "igen"/"nem" IS a meaningful result) and includes casual
// greeting-openers (hello/szia/hi/hey) that have no place in a closing-ack
// list for THIS route. A separate, narrow, purpose-built pattern avoids
// coupling two conceptually distinct filters that happen to share syntax
// today but could diverge tomorrow.
// Both accented and unaccented spellings included (agent-authored Hungarian
// text is not reliably accent-complete) -- igen/nem and any prefix-style
// match are deliberately EXCLUDED (Codex review, 2026-09-11): a delegated
// yes/no decision can be a real, substantive result, not a content-free ack.
const TRIVIAL_CLOSURE_ACK_PATTERN = /^(ok|oké|oke|rendben|köszönöm|koszonom|köszi|koszi|vettem|nyugtázva|nyugtazva)[.!]?$/i

export function isTrivialClosureAck(content: string): boolean {
  return TRIVIAL_CLOSURE_ACK_PATTERN.test(content.trim())
}

// Frozen at module load, like the config constant it derives from.
const SYSTEM_SENDERS = parseSystemSenderIds(SYSTEM_SENDER_IDS, sanitizeAgentIdent)

export async function tryHandleMessages(ctx: RouteContext): Promise<boolean> {
  const { req, res, path, method, url } = ctx

  if (path === '/api/messages' && method === 'POST') {
    const body = await readBody(req)
    const { from, to, content, origin_note } = JSON.parse(body.toString()) as
      { from: string; to: string; content: string; origin_note?: string }
    if (!from?.trim() || !to?.trim() || !content?.trim()) {
      json(res, { error: 'from, to, and content are required' }, 400)
      return true
    }
    // Security: the channel-coordinator id grants channel-inbound delivery
    // (verbatim <channel> + reply-expected framing) in the message-router. The
    // ONLY legitimate writer of that id is the in-process coordinator, which
    // inserts directly into the DB -- it never POSTs here. The dashboard token
    // is readable by every sub-agent, so without this guard any sub-agent could
    // forge a reply-expected message addressed at the main agent. Reject it.
    //
    // CRITICAL: normalize with the EXACT function the router matches on
    // (sanitizeAgentIdent), NOT from.trim(). The router does
    // CHANNEL_COORDINATOR_AGENTS.has(sanitizeAgentIdent(from)), and
    // sanitizeAgentIdent STRIPS [^a-zA-Z0-9_-] rather than trimming. A bypass
    // like from="@telegram-coordinator" / "telegram-coordinator." survives
    // .trim() (!= the constant) yet sanitizes to "telegram-coordinator" in the
    // router -> channel-inbound with an attacker-controlled body. Matching the
    // router's normalization here closes that asymmetry.
    if (sanitizeAgentIdent(from) === COORDINATOR_AGENT_ID) {
      logger.warn({ from: from.trim(), to: to.trim() }, 'Rejected /api/messages POST forging channel-coordinator id')
      json(res, { error: 'from is reserved for the in-process channel coordinator' }, 403)
      return true
    }
    // Federation spoof guard: a slash-qualified from ("teodor/teodor") is the
    // provenance mark of a REMOTE sender and may only ever be written by the
    // token-authenticated /api/federation/inbox. Accepting it here would let
    // any dashboard-token holder (i.e. every local sub-agent) impersonate a
    // federation peer toward another local agent.
    if (from.includes('/')) {
      logger.warn({ from: from.trim(), to: to.trim() }, 'Rejected /api/messages POST with qualified from (federation impersonation guard)')
      json(res, { error: 'from must be a local agent id without "/" -- federated senders are only accepted via /api/federation/inbox' }, 403)
      return true
    }
    // From-authentication: accept messages only from registered fleet agents.
    // The shared Bearer token is readable by any sub-agent, so without this
    // check any process with the token could inject messages as an arbitrary
    // sender ("from": "zack" from an external attacker who obtained the token).
    // Server-side validation: the `from` claim must match a known agent on the
    // filesystem (agents/<id>/ directory, or MAIN_AGENT_ID). This is not
    // impersonation-proof between fleet agents (they share the same token) but
    // it closes the "unknown sender" injection path without per-agent secrets.
    //
    // The human OWNER is a legitimate sender too: the dashboard "Messages" page
    // composes with from=OWNER_NAME (resolveOwnerName -> the owner assignee), so
    // without this exemption the operator's own dashboard messages 403 with
    // "unknown agent". The owner is not a fleet agent (no agents/<id>/ dir), so
    // isKnownAgent alone rejects it. Match on the router's normalization to stay
    // symmetric with the other guards above.
    //
    // Neighbouring SYSTEMS are legitimate senders too, on the same reasoning as
    // the owner: they are token-authenticated and only notify an agent, but they
    // have no agents/<id>/ directory. Opt-in via SYSTEM_SENDER_IDS in .env
    // (empty by default). Without this, such a system silently loses its push
    // channel the moment this guard ships -- observed here: an external case
    // manager pushed 4182 messages, then every call 403'd for nine days while
    // its fail-soft caller logged nothing.
    const isOwnerSender = sanitizeAgentIdent(from) === sanitizeAgentIdent(OWNER_NAME)
    const isSystemSender = SYSTEM_SENDERS.has(sanitizeAgentIdent(from))
    if (!isOwnerSender && !isSystemSender && !isKnownAgent(sanitizeAgentIdent(from))) {
      logger.warn({ from: from.trim(), to: to.trim() }, 'Rejected /api/messages POST from unregistered agent')
      json(res, { error: `unknown agent '${from.trim()}' -- from must be a registered fleet agent id` }, 403)
      return true
    }
    // Qualified to ("peer/agent"): validate at creation time so the sender
    // gets an actionable error NOW instead of a silent 1h abandon. Local
    // (slash-free) recipients are untouched.
    let storedTo = to.trim()
    if (storedTo.includes('/')) {
      const target = parseQualifiedId(storedTo)
      if (!target) {
        json(res, { error: 'Invalid federated address in to (expected "<system>/<agent>")' }, 400)
        return true
      }
      const cfg = getFederationConfig()
      if (!cfg.enabled) {
        json(res, { error: 'Federation is disabled on this system' }, 400)
        return true
      }
      // System ids are case-insensitive (stored lowercase in the config).
      // Normalize the STORED prefix too: the per-peer purge SQL and the
      // bridge's peer lookup key on it, and thread grouping in the UI should
      // not split 'Teodor/x' from 'teodor/x'. The agent segment is the
      // PEER's namespace -- leave its case alone.
      const targetSystem = target.system.toLowerCase()
      if (targetSystem === cfg.systemId) {
        json(res, { error: `'${target.system}' is this system -- address the agent locally as '${target.agent}'` }, 400)
        return true
      }
      if (!cfg.peers.some((p) => p.id === targetSystem)) {
        json(res, { error: `Unknown federation peer '${target.system}'` }, 400)
        return true
      }
      storedTo = formatQualifiedId(targetSystem, target.agent)
    } else if (storedTo.includes(':')) {
      // A colon-form 'to' ("federation:teodor:teodor", copied from an
      // <untrusted source> attribute) is NOT a valid address: it has no '/',
      // so it would be treated as a LOCAL recipient, never match a session,
      // and silently sit pending until the 1h abandon window. Reject it now
      // with the correct form. Safe: sanitizeAgentIdent strips ':', so no
      // legitimate local agent id can contain one, and the channel
      // coordinator inserts directly into the DB, bypassing this endpoint.
      json(res, { error: 'Invalid recipient: use "<system>/<agent>" (slash) for a federated address, not the "federation:x:y" source form' }, 400)
      return true
    }
    // Code-side enforcement of the kanban-ref convention: rewrite any
    // `#<hex8>` token that maps to a real kanban_cards row into its
    // human-facing `#<seq>` form before persistence, so the dashboard and
    // every downstream consumer sees the canonical reference even when a
    // sub-agent forgets the CLAUDE.md rule (#75 Cuzcoo dispatch).
    const normalizedContent = normalizeKanbanRefs(content.trim(), getKanbanSeqByIdPrefix)
    // Card 06f062e4: optional attributability tag, self-declared like `from`
    // itself -- capped short so it stays a label, not a second content field.
    const trimmedOriginNote = origin_note?.trim().slice(0, 120) || null
    const msg = createAgentMessage(from.trim(), storedTo, normalizedContent, trimmedOriginNote)
    logger.info({ id: msg.id, from: msg.from_agent, to: msg.to_agent, originNote: msg.origin_note }, 'Agent message created')
    json(res, msg)
    return true
  }

  // Sidebar threads: one row per conversation peer (system agents excluded),
  // each with its count + most-recent message, recency computed per-peer.
  if (path === '/api/messages/threads' && method === 'GET') {
    json(res, getAgentConversationThreads())
    return true
  }

  // Backlog per agent: count + how long the oldest has been waiting. Cheap
  // enough to curl on a schedule; the point is that a growing queue behind a
  // busy agent becomes visible BEFORE someone mistakes it for lost messages.
  if (path === '/api/messages/backlog' && method === 'GET') {
    // upstream cca1e519: the same rule as /api/messages and /api/kanban -- an unknown parameter is
    // a LOUD 400, a strict set. The endpoint used to drop `agent=` silently and return the whole
    // fleet's backlog. Inventory 2026-11-27: no non-agent caller sends a query parameter here.
    const KNOWN_PARAMS = new Set(['agent', 'assignee'])
    const unknown = [...url.searchParams.keys()].filter((k) => !KNOWN_PARAMS.has(k))
    if (unknown.length) {
      json(res, {
        error: 'unknown query parameter',
        unknown,
        known: [...KNOWN_PARAMS],
        hint: 'a backlog szurese "agent" (vagy "assignee"); parameter nelkul a TELJES flotta jon',
      }, 400)
      return true
    }
    const agent = (url.searchParams.get('agent') ?? url.searchParams.get('assignee') ?? '').trim() || undefined
    json(res, getPendingBacklogByAgent(agent))
    return true
  }

  // SONWIN905 (2026-09-05, v3 plan section 5): the CONCRETE outbound messages
  // `agent` dispatched whose ORIGINAL message has not reached done/failed --
  // id/recipient/timestamp, not just a count. Codex review: this is NOT proof
  // no reply ever came (a reply can arrive as a separate new message without
  // the original ever being closed) -- an "open thread" signal, not a
  // confirmed-still-waiting one; see getWaitingOutboundMessages's own doc
  // comment for the full reasoning.
  // Distinct on purpose from `?agent=` on the main GET below (that filters the
  // agent's MAILBOX -- inbound via getPendingMessages, or both directions via
  // getAgentConversation) and from /backlog above (INBOUND queue depth,
  // getPendingBacklogByAgent) -- this one is OUTBOUND only, the question a
  // resuming session needs answered for its handoff: "which of MY OWN
  // dispatched requests are still open". Exists as an HTTP route (not just the
  // db.ts helper) because the handoff skill only has curl/bash access, no
  // direct DB access, same as every other data source it reads.
  if (path === '/api/messages/waiting-outbound' && method === 'GET') {
    const agent = (url.searchParams.get('agent') || '').trim()
    if (!agent) {
      json(res, { error: 'agent query parameter is required' }, 400)
      return true
    }
    // Parsing only -- getWaitingOutboundMessages owns the default/clamp (see
    // its doc comment). An earlier draft duplicated that logic here AND in
    // the helper, and the two treated limit=0 vs. a negative limit
    // inconsistently as a result. A missing/non-numeric `limit` is passed
    // through as `undefined`/NaN and the helper's own bounds-checking applies.
    const limitParam = url.searchParams.get('limit')
    const limitRaw = limitParam !== null ? parseInt(limitParam, 10) : undefined
    json(res, getWaitingOutboundMessages(agent, limitRaw))
    return true
  }

  // SUPHOX318-B (2026-09-11, kanban 4bf72b27): the OPPOSITE direction from
  // /waiting-outbound above -- inbound dispatches TO `agent` that `agent` has
  // already replied to but never formally closed with PUT done/failed. Same
  // reason this exists as an HTTP route rather than just the db.ts helper:
  // skills only have curl/bash access. See getRepliedButUnclosedInboundMessages's
  // doc comment for why the sender/executor direction matters here (closing
  // the WRONG side's dispatch fabricates a reverse [Eredmény] that appears to
  // come from someone who never said it).
  if (path === '/api/messages/closure-debt' && method === 'GET') {
    const agent = (url.searchParams.get('agent') || '').trim()
    if (!agent) {
      json(res, { error: 'agent query parameter is required' }, 400)
      return true
    }
    const limitParam = url.searchParams.get('limit')
    const limitRaw = limitParam !== null ? parseInt(limitParam, 10) : undefined
    json(res, getRepliedButUnclosedInboundMessages(agent, limitRaw))
    return true
  }

  if (path === '/api/messages' && method === 'GET') {
    // An UNKNOWN filter param used to fall through to the global list: a typo,
    // or the plausible-but-wrong `agent_id`, silently returned the fleet's last
    // N messages instead of one agent's mailbox. Not an error, not an empty
    // list -- MORE than asked for, which is the expensive direction to be wrong
    // in: a caller acting in good faith on that answer reads other agents'
    // traffic as its own. Fail loudly instead.
    const KNOWN_PARAMS = new Set(['agent', 'status', 'limit', 'before'])
    const unknown = [...url.searchParams.keys()].filter((k) => !KNOWN_PARAMS.has(k))
    if (unknown.length) {
      json(res, {
        error: 'unknown query parameter',
        unknown,
        known: [...KNOWN_PARAMS],
        hint: 'the mailbox filter is "agent"; "agent_id" and "to" are not read',
      }, 400)
      return true
    }
    const agent = url.searchParams.get('agent') || ''
    const status = url.searchParams.get('status') || ''
    const limit = Math.min(parseInt(url.searchParams.get('limit') || '50', 10), 200)
    const beforeRaw = url.searchParams.get('before')
    const before = beforeRaw !== null ? parseInt(beforeRaw, 10) : undefined

    let messages: AgentMessage[]
    if (status === 'pending' && agent) {
      messages = getPendingMessages(agent)
    } else if (status === 'pending') {
      messages = getPendingMessages()
    } else if (agent) {
      // SQL-filtered to THIS agent's last N (+ before-cursor pagination), not
      // global-last-N-then-JS-filter which starved rarely-active threads.
      messages = getAgentConversation(agent, limit, Number.isFinite(before as number) ? before : undefined)
    } else {
      messages = listAgentMessages(limit)
    }

    jsonMaybeGzip(req, res, messages)
    return true
  }

  const msgUpdateMatch = path.match(/^\/api\/messages\/(\d+)$/)
  if (msgUpdateMatch && method === 'PUT') {
    const id = parseInt(msgUpdateMatch[1], 10)
    const body = await readBody(req)
    const parsedBody = JSON.parse(body.toString()) as { status: string; result?: string; silent?: unknown }
    const { status: newStatus, result } = parsedBody
    // silent: strictly boolean (2026-09-11, kanban bcbf4511, Codex review) --
    // a truthy string ("false") must not be accepted as true. Suppresses ONLY
    // the reverse createAgentMessage notification below; terminal status,
    // result, delivered_at backfill, and OTel span closure are UNCHANGED. An
    // explicit, caller-declared alternative to content-sniffing for cases the
    // isTrivialClosureAck heuristic does not (and should not try to) cover.
    if (parsedBody.silent !== undefined && typeof parsedBody.silent !== 'boolean') {
      json(res, { error: 'silent must be a boolean' }, 400)
      return true
    }
    const silentClose = parsedBody.silent === true

    // Idempotent terminal-state guard (2026-09-11, kanban 4bf72b27/bcbf4511,
    // Codex review): a repeat PUT on an ALREADY-closed message is a common,
    // harmless caller mistake (retry, double-processing) -- without this it
    // would re-run markMessageDone/Failed (re-touching completed_at/result,
    // see the guarded UPDATE in db.ts) AND fire a SECOND reverse [Eredmény]
    // notification, the same ping-pong mechanism class as a trivial-ack
    // close, just triggered by a repeat close instead. Checked BEFORE the
    // write so a repeat close can answer {ok:true, alreadyTerminal:true}
    // rather than falling through to "not found" once the guarded UPDATE
    // below returns changes=0 for the same reason.
    const existing = getAgentMessage(id)
    if (!existing) {
      json(res, { error: 'Message not found or invalid status' }, 404)
      return true
    }
    if (existing.status === 'done' || existing.status === 'failed') {
      json(res, { ok: true, alreadyTerminal: true })
      return true
    }

    let ok = false
    if (newStatus === 'done') ok = markMessageDone(id, result)
    else if (newStatus === 'failed') ok = markMessageFailed(id, result)

    // TOCTOU close (2026-09-11, Codex review, kanban 4bf72b27/bcbf4511): the
    // precheck above and the guarded UPDATE are two separate steps, so two
    // concurrent PUTs can both read existing.status as pending/delivered
    // before either write lands -- the loser's guarded UPDATE then correctly
    // affects 0 rows (no double notification, no double completed_at write),
    // but without this recheck it would fall through to the generic 404
    // below, breaking the idempotent {ok:true, alreadyTerminal:true}
    // contract for a message that in fact got closed just fine (by the other
    // caller). Re-read only on the failure path -- the common, non-racing
    // case never pays this extra query.
    if (!ok) {
      const raced = getAgentMessage(id)
      if (raced && (raced.status === 'done' || raced.status === 'failed')) {
        json(res, { ok: true, alreadyTerminal: true })
        return true
      }
    }

    if (ok) {
      const done = getAgentMessage(id)
      // Close the OTel span now that the message has a terminal status.
      if (done?.trace_id && done?.span_id) {
        closeOtelSpan(done.trace_id, done.span_id, Date.now(), newStatus === 'done' ? 'ok' : 'error')
      }
      // Notify the delegator: create a reverse message from executor → delegator so
      // they learn the result without polling. See shouldNotifyDelegator for which
      // senders are skipped and why.
      if (silentClose) {
        logger.info({ id, silent: true }, 'PUT /api/messages: reverse notification suppressed by caller')
      } else if (done && shouldNotifyDelegator(done.from_agent, done.to_agent, done.content)) {
        const summary = result ? result.slice(0, 500) : '(nincs eredmény)'
        createAgentMessage(
          done.to_agent,
          done.from_agent,
          `${COMPLETION_REPORT_PREFIX} msg_id:${id} status:${newStatus}\n\n${summary}`,
        )
      }
      json(res, { ok: true }); return true
    }
    json(res, { error: 'Message not found or invalid status' }, 404)
    return true
  }

  return false
}

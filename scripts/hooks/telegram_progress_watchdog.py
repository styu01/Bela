#!/usr/bin/env python3
"""
telegram_progress_watchdog.py -- the "őrszem" (sentry) for the Telegram progress
indicator. Runs independently of the agent sessions (via launchd/systemd), so it
can speak even when an agent is wedged or down.

Problem it solves: the UserPromptSubmit hook posts a "✍️ Dolgozom rajta…"
placeholder; the Stop hook deletes it when the turn ends. If a turn never ends
(agent crashed, session killed, or WEDGED on a dropped MCP reply-tool call that
never returns), the placeholder would sit there forever and the user is left
wondering "is it working or broken?".

Two delivery modes, best-effort per pending placeholder:
  - REAL ANSWER (preferred): if the agent's final answer is recoverable from the
    transcript, deliver it for real (sendMessage) and remove the placeholder --
    the same answer the Stop hook's guaranteed fallback would have sent, but
    without waiting for a turn end that may never come. This is the fix for the
    "reply tool dropped mid-turn -> round hangs -> Stop hook never fires ->
    the owner has to restart" freeze: the user gets the actual answer, restart-free.
  - GENERIC ERROR (fallback): if no answer is recoverable, rewrite the
    placeholder into a clear error (editMessageText), as before.

Detection (per pending placeholder ENTRY -- see TGORPHANFILE908 below for why
this is entry-level, not file-level):
  - agent DOWN (its tmux `agent-<name>` session is gone) and the placeholder is
    older than DOWN_GRACE_SEC -> fire (crash / unreachable), or
  - agent UP but the transcript shows a HUNG reply -- the most recent tool call
    is the Telegram `reply` and it has no result yet -- and the placeholder is
    older than WEDGED_UP_SEC -> fire FAST. This precisely targets the dropped-
    MCP freeze and does NOT misfire on a legitimately long task (which has no
    dangling reply call), so the threshold can be far below the blunt backstop.
  - agent UP with no hung-reply signal but the placeholder is older than
    WEDGED_SEC -> fire (blunt backstop for genuinely stuck turns; generous so
    long legit tasks aren't cut short).
  - placeholder older than STALE_SEC (default 24h) -> DEAD round: deliver
    nothing, drop the marker (and the placeholder message while Telegram still
    allows deletion). TGORPHAN908: without this bound a post-outage scan walked
    28-day orphans into the backstop and sent internal work logs to the owner.

The recovered answer is scoped to the round that posted the placeholder (see
read_transcript): the transcript keeps growing after that round, so its last
text may be a later internal turn's monologue -- never deliverable here.

TGORPHANFILE908 (2026-09-09, found in Codex review of TGORPHAN908): the state
file is keyed by session_id, NOT by placeholder, and telegram_progress.py
APPENDS a fresh entry to the SAME file when an earlier one is still pending
(Stop hook never fired for it). That means one `*.json` file can hold several
entries from DIFFERENT rounds, and every append bumps the FILE's mtime --
which used to be the only age signal this watchdog had. A fresh append could
therefore mask an old orphan sitting right next to it in the same array (the
file always "looks" recently touched), and -- worse -- age/turn_start/answer
were computed ONCE per file and applied to every entry in it, so an old
orphan could inherit a brand-new round's answer outright. Every entry now
carries its own `created_at` (stamped by telegram_progress.py) and is aged,
scoped and delivered INDEPENDENTLY of its file-mates; the file's mtime is
used only as a last-resort age estimate for entries written before this field
existed (see the fail-closed legacy path in handle_dir), and even then it
never gates a transcript-derived answer.

Standalone: scans every agent's per-agent telegram state dir. No marveen src
dependency; only Python stdlib + the `tmux` binary. Bot API base is overridable
via TELEGRAM_API_BASE (tests point it at a local stub).
"""
import datetime, os, glob, json, time, subprocess, urllib.request

# State dirs to scan: per-agent dirs under the fleet, plus the default dir.
# No hardcoded user paths -- derive from $HOME (override with MARVEEN_ROOT).
FLEET_ROOT = os.environ.get("MARVEEN_ROOT") or os.path.expanduser("~/marveen")
SCAN_GLOBS = [
    os.path.join(FLEET_ROOT, "agents", "*", ".claude", "channels", "telegram", "progress"),
    os.path.expanduser("~/.claude/channels/telegram/progress"),
]
DOWN_GRACE_SEC = 120        # agent down + placeholder older than this -> fire
WEDGED_SEC = 15 * 60        # agent up, no hung-reply signal, this old -> fire (backstop)
# UPPER age bound (TGORPHAN908): a marker older than this marks a DEAD round,
# not a stuck one -- there is no question behind it that needs an answer today.
# Deliver NOTHING; drop the marker. Without this bound, a fleet restart after a
# long outage walked 20-28 day old markers into the wedged-backstop branch and
# sent six internal work logs to the owner's channel (2026-09-08).
DEFAULT_STALE_SEC = 24 * 3600
# Telegram refuses deleteMessage on messages older than 48h; don't burn an API
# call (and an error log line) on a delete that cannot succeed.
TELEGRAM_DELETE_WINDOW_SEC = 47 * 3600
# A marker is written by the SAME submit hook that logs the user event into
# the transcript, AFTER that event -- so the round's opening user-prompt sits
# AT OR SHORTLY BEFORE the marker's created_at. This is the primary (backward)
# search window: how far BEFORE turn_start an anchor prompt may sit. Generous,
# to absorb a slow placeholder-send API call.
TURN_ANCHOR_SLACK_SEC = 120
# TGORPHANDUP908 (2026-09-09, found in Codex review): a SEPARATE, narrow
# window for a prompt sitting AFTER turn_start -- clock skew/write-order
# jitter only, NOT "the next round". Two rounds fired in quick succession
# (e.g. two rapid inbound messages) can land well within TURN_ANCHOR_SLACK_SEC
# of EACH OTHER; if the forward tolerance were as wide as the backward one,
# the earlier round's own marker could match the LATER round's prompt as
# "close enough" and adopt its answer. Kept tiny and used only when no
# backward (pre-marker) candidate exists at all -- see _find_anchor_index.
TURN_ANCHOR_FORWARD_SLACK_SEC = 5
# agent up + a HUNG reply detected + placeholder older than this -> fire FAST.
# Far below WEDGED_SEC because the hung-reply signal is precise. Env-tunable so
# a live install can adjust without a code change.
DEFAULT_WEDGED_UP_SEC = 180
ERROR_TEXT = ("⚠️ Valami elakadt, és erre nem érkezett válasz. "
              "Lehet, hogy újra kell indítani az ügynököt, vagy próbáld újra kicsit később.")

# Upstream #2269 (a21f3e5b, ported by hand onto the per-entry TGORPHANFILE908 structure): the CLI
# writes its OWN lines into the transcript as assistant messages -- the session/weekly limit wall,
# an API error, "No response requested." Measured on this install (main transcripts, last 30 days):
# ~190 such lines ("You've hit your weekly limit" 74, "API Error: Can't reach the API server" 70,
# session limit 37, "No response requested." 4, ...). Before this change a fallback delivered such a
# line to the owner as the agent's answer. A limit or error line is an OUTAGE: the placeholder is
# rewritten into OUTAGE_TEXT and stays, nothing is sent, no ledger row (the question stays open for
# the agent to answer after the outage). Any other CLI-written line under the model "<synthetic>"
# means the round has no answer: the no-answer path (ERROR_TEXT), never the outage line.
SYNTHETIC_MODEL = "<synthetic>"
SYNTHETIC_PREFIXES = (
    "you've hit your session limit", "you've hit your usage limit", "you've hit your weekly limit",
    "usage limit reached", "claude ai usage limit reached", "api error", "credit balance is too low",
    "invalid api key", "please run /login",
)
OUTAGE_TEXT = "⏳ A válasz egy átmeneti kiesés miatt késik."
# #2269: a real send of this watchdog goes into the conversation ledger (direction out), marked in
# the TEXT like the other machine rows, NOT in the source column (ledger_lib.source_for() style
# readers take the newest source of the chat). Without the row the reply guard
# (telegram-reply-guard.py, open_question_with_age) keeps seeing the question as unanswered.
LEDGER_PREFIX = "[telegram-progress-watchdog]"


def _synthetic_kind(ev, text):
    """What an assistant text is (#2269): "" the agent's own; "outage" a CLI limit or error line
    (isApiErrorMessage, or a known prefix on an event that names no model or the model
    "<synthetic>"); "none" any other CLI-written line, i.e. no answer. A real answer carries its own
    model id and may itself begin with "API error"."""
    msg = ev.get("message") or {}
    model = msg.get("model")
    if ev.get("isApiErrorMessage") is True:
        return "outage"
    if model and model != SYNTHETIC_MODEL:
        return ""
    if (text or "").strip().lower().startswith(SYNTHETIC_PREFIXES):
        return "outage"
    return "none" if model == SYNTHETIC_MODEL else ""


def _ledger_lib():
    """#2269: the ledger_lib of the install this watchdog scans (FLEET_ROOT/scripts/hooks), loaded by
    file path -- the installed copy of this file lives in ~/.claude/hooks, next to no ledger_lib of
    its own. None when the install has none (a hermetic test tree: nothing is written)."""
    import importlib.util
    p = os.path.join(FLEET_ROOT, "scripts", "hooks", "ledger_lib.py")
    if not os.path.isfile(p):
        return None
    spec = importlib.util.spec_from_file_location("ledger_lib", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ledger_log_out(progress_dir, chat_id, text, result):
    """#2269: one conversation_log row (direction out) for a message this watchdog really sent. The
    agent is the channel's: agents/<name> of the progress dir, else the install's main agent (the
    same ids ledger-outbound.py derives from the session cwd). Writes only into an EXISTING ledger.
    Called only after the entry left the marker file, and it swallows EVERYTHING, SystemExit
    included: a ledger step that stopped this tick before the marker was rewritten would make the
    next tick send the same answer again, every minute."""
    try:
        lib = _ledger_lib()
        if lib is None:
            log(progress_dir, f"ledger: no ledger_lib under {FLEET_ROOT}, send not logged")
            return False
        db = lib.db_path()
        if not os.path.isfile(db):
            log(progress_dir, f"ledger: no database at {db}, send not logged")
            return False
        res = result.get("result") if isinstance(result, dict) else None
        mid = res.get("message_id") if isinstance(res, dict) else None
        lib.log_outbound(agent_name_from(progress_dir) or lib.main_agent_id(), chat_id,
                         LEDGER_PREFIX + " " + text, mid)
        return True
    except BaseException as e:
        log(progress_dir, f"ledger write failed: {type(e).__name__}: {e}")
        return False


def _env_int(name, default):
    v = os.environ.get(name)
    if v:
        try:
            n = int(v)
            if n > 0:
                return n
        except ValueError:
            pass
    return default


def wedged_up_sec():
    return _env_int("TELEGRAM_WATCHDOG_WEDGED_UP_SEC", DEFAULT_WEDGED_UP_SEC)


def stale_sec():
    return _env_int("TELEGRAM_WATCHDOG_STALE_SEC", DEFAULT_STALE_SEC)


def token(state_dir):
    try:
        for line in open(os.path.join(state_dir, ".env"), encoding="utf-8"):
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    except Exception:
        return None
    return None


def api(tok, method, payload):
    base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
    url = f"{base}/bot{tok}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


def agent_name_from(progress_dir):
    # .../agents/<name>/.claude/channels/telegram/progress  -> <name>
    parts = progress_dir.split(os.sep)
    if "agents" in parts:
        i = parts.index("agents")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def tmux_session_alive(session):
    # Test/override seam: force the agent-up verdict without a real tmux probe.
    forced = os.environ.get("TELEGRAM_WATCHDOG_FORCE_AGENT_UP")
    if forced in ("0", "1"):
        return forced == "1"
    try:
        # Exact target (TMUXEXACT1771, PROGI review 6212): a bare -t NAME also matches a session whose name
        # merely STARTS with NAME, so a dead agent-X with a live agent-X2 read as alive.
        return subprocess.run(["tmux", "has-session", "-t", "=%s:" % session],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return True  # if tmux probe fails, assume alive (don't false-alarm)


def _iter_events(transcript_path):
    if not transcript_path:
        return
    try:
        f = open(transcript_path, encoding="utf-8")
    except Exception:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _is_reply_tool(name):
    n = (name or "").lower()
    return "telegram" in n and "reply" in n


def _ev_epoch(ev):
    ts = ev.get("timestamp")
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _is_user_prompt(ev):
    """A real inbound prompt (starts a turn) -- NOT a tool_result carrier, and NOT a line the harness
    writes INSIDE a running turn: an isMeta line with no origin (a skill load, a <system-reminder>,
    the Stop hook's own "Stop hook feedback: ... VÁLASZOLATLAN TELEGRAM" nudge) or a compaction
    summary (isCompactSummary). Such a line used to END the round's window, so the round's later
    answer and reply call fell outside it (#2269). An isMeta line that carries an origin still
    counts: on this install every inbound Telegram message is isMeta WITH
    origin {kind: channel} (measured, CC 2.1.326), and it does start a turn."""
    msg = ev.get("message") or {}
    role = msg.get("role") or ev.get("role")
    if not (ev.get("type") == "user" or role == "user"):
        return False
    if ev.get("isCompactSummary") or (ev.get("isMeta") and not ev.get("origin")):
        return False
    content = msg.get("content", ev.get("content"))
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") != "tool_result"
                   for b in content)
    return False


class _Acc:
    """Accumulator for one scan window of the transcript."""
    def __init__(self):
        self.text = ""
        self.results = set()      # tool_use_ids that have a tool_result
        self.reply_ids = set()    # tool_use_ids of Telegram reply calls
        self.last_tool_use = None  # (id, is_reply) of the most recent tool_use
        self.kind = ""             # #2269: what the last assistant text is (_synthetic_kind)

    def feed(self, ev):
        msg = ev.get("message") or {}
        role = msg.get("role") or ev.get("role")
        content = msg.get("content", ev.get("content"))
        is_assistant = ev.get("type") == "assistant" or role == "assistant"
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_use":
                    is_reply = _is_reply_tool(b.get("name"))
                    self.last_tool_use = (b.get("id"), is_reply)
                    if is_reply and b.get("id") is not None:
                        self.reply_ids.add(b.get("id"))
                elif bt == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid is not None:
                        self.results.add(tid)
                elif bt == "text" and is_assistant:
                    t = (b.get("text") or "").strip()
                    if t:
                        self.text = t
                        self.kind = _synthetic_kind(ev, t)
        elif isinstance(content, str) and is_assistant:
            if content.strip():
                self.text = content.strip()
                self.kind = _synthetic_kind(ev, self.text)

    def answer(self):
        """(text, synthetic) of the round (#2269): an outage line comes back with synthetic=True;
        another CLI-written line is no answer at all, an empty text."""
        if self.kind == "none":
            return "", False
        return self.text, self.kind == "outage"

    def reply_hung(self):
        return bool(self.last_tool_use and self.last_tool_use[1]
                    and self.last_tool_use[0] not in self.results)

    def reply_delivered(self):
        return bool(self.reply_ids & self.results)


def _find_anchor_index(ts_prompts, turn_start):
    """Pick the SINGLE unambiguous anchor prompt for turn_start, or None.

    TGORPHANDUP908: two rounds fired in quick succession can each have a
    prompt within TURN_ANCHOR_SLACK_SEC of the OTHER round's turn_start --
    "closest prompt within the slack window" is not unique in that case, and
    picking whichever one is scanned last (the previous approach) can hand an
    earlier marker the LATER round's answer. There is exactly one honest
    anchor for a given turn_start: the prompt that STARTED it, which -- because
    the marker is written by the submit hook right after that prompt arrives
    -- is always AT OR BEFORE turn_start, normally by well under a second and
    at most by TURN_ANCHOR_SLACK_SEC (a slow placeholder-send API call). A
    prompt AFTER turn_start is never this round's own opener; it is only
    tolerated within a tiny TURN_ANCHOR_FORWARD_SLACK_SEC window, and only
    when no at-or-before candidate exists at all, purely to absorb clock skew.

    ts_prompts: [(index_in_events, epoch), ...] for every timestamped
    user-prompt event, in file order.
    """
    best_idx, best_gap = None, None
    for idx, e in ts_prompts:
        if turn_start - TURN_ANCHOR_SLACK_SEC <= e <= turn_start:
            gap = turn_start - e
            if best_gap is None or gap < best_gap:
                best_idx, best_gap = idx, gap
    if best_idx is not None:
        return best_idx
    for idx, e in ts_prompts:
        if turn_start < e <= turn_start + TURN_ANCHOR_FORWARD_SLACK_SEC:
            gap = e - turn_start
            if best_gap is None or gap < best_gap:
                best_idx, best_gap = idx, gap
    return best_idx


def _round_window(events, turn_start):
    """(anchor_idx, end_idx) of the round that posted the marker, or None when nothing provably
    belongs to it. ONE definition shared by read_transcript and round_activity, so the answer and
    the activity are always read from the same window."""
    if turn_start is None:
        return None
    ts_prompts = [(i, _ev_epoch(ev)) for i, ev in enumerate(events)
                  if _is_user_prompt(ev) and _ev_epoch(ev) is not None]
    if not ts_prompts:
        return None
    anchor_idx = _find_anchor_index(ts_prompts, turn_start)
    if anchor_idx is None:
        return None
    end_idx = len(events)
    for idx in range(anchor_idx + 1, len(events)):
        if _is_user_prompt(events[idx]):
            end_idx = idx
            break
    return anchor_idx, end_idx


def round_activity(transcript_path, turn_start):
    """#2269 (upstream c9be04f8): (last_event_epoch, round_open) of the marker's round.

    The 15-minute backstop fired on an agent that was up and still working a long round (the Stop
    hook clears the marker only when the round ends), and the owner's waiting note became the error
    text or a half answer. A round that is still OPEN (no later prompt) and wrote a timestamped line
    within WEDGED_SEC is working, not wedged. Measured on this install: of the 2 wedged-backstop fires
    whose transcript still exists, the round had written 832 s and 628 s before the fire. What cannot
    be told keeps the old behaviour -- no anchor, or a round that already ended -> (None, False), and
    the backstop fires as before. Bounded: once the round is silent for WEDGED_SEC, it fires."""
    events = list(_iter_events(transcript_path))
    win = _round_window(events, turn_start)
    if win is None:
        return None, False
    anchor_idx, end_idx = win
    if end_idx != len(events):
        return None, False
    last = None
    for ev in events[anchor_idx:end_idx]:
        e = _ev_epoch(ev)
        if e is not None:
            last = e if last is None else max(last, e)
    return last, True


def read_transcript(transcript_path, turn_start=None):
    """Return (last_assistant_text, reply_is_hung, reply_delivered, synthetic).

    synthetic (#2269): the last assistant text is a CLI limit or error line (session limit, usage
    limit, API error), not the agent's answer; deliver() then sends nothing. Any other CLI-written
    last line ("No response requested.") comes back as an empty text: the round has no answer.

    last_assistant_text: the agent's final user-facing answer (last non-empty
    assistant text block) -- the same source the Stop hook's fallback uses.

    reply_is_hung: True iff the most recent tool call in scope is the Telegram
    `reply` tool with no matching tool_result yet (dropped MCP).

    reply_delivered: True iff a Telegram reply call in scope DID get a result
    -- the round's answer already reached the channel, so nothing may be resent.

    Scope (TGORPHAN908): a transcript outlives the round that posted the
    placeholder -- later scheduled/internal turns keep appending, so the LAST
    text of the whole file may be internal monologue that was never meant for
    the channel (six such leaked to the owner on 2026-09-08). When `turn_start`
    (this entry's own created_at) is given, exactly ONE anchor prompt is
    chosen for this round (see _find_anchor_index -- TGORPHANANCHOR908 /
    TGORPHANDUP908), and only the window from that anchor to the NEXT user
    prompt of ANY kind is read.

    TGORPHANFAILOPEN908 (2026-09-09, found in Codex review): two remaining
    fail-open paths, both closed now --
      (1) the window used to close only at the next TIMESTAMPED prompt, so an
          intervening user prompt WITHOUT a timestamp (mixed/corrupt format)
          did not end the round: a later, unrelated round's internal text
          could still be captured into "this" entry's scope. The window now
          closes at the first _is_user_prompt() event after the anchor,
          timestamped or not -- a tool_result carrier is never mistaken for a
          prompt (_is_user_prompt already excludes it).
      (2) `turn_start is not None` but the transcript carries NO timestamped
          prompt at all used to fall through to the WHOLE-FILE text (kept for
          "older format" compatibility) -- itself exactly the cross-round leak
          this feature exists to close, just reached a different way. A
          per-entry marker (every entry written after TGORPHANFILE908) that
          cannot be anchored is now unattributable -> generic-error, full
          stop, never whole-file text. The legacy (no created_at) path is
          unaffected: it never calls read_transcript at all.
    """
    events = list(_iter_events(transcript_path))

    win = _round_window(events, turn_start)
    if win is None:
        # Nothing provably belongs to this round -- fail closed. Covers: no
        # turn_start given, no timestamped prompt in the transcript at all,
        # and no prompt close enough to turn_start. Never whole-file text.
        return "", False, False, False
    anchor_idx, end_idx = win

    scoped = _Acc()
    for ev in events[anchor_idx:end_idx]:
        scoped.feed(ev)
    text, synthetic = scoped.answer()
    return text, scoped.reply_hung(), scoped.reply_delivered(), synthetic


def log(progress_dir, msg):
    try:
        with open(os.path.join(progress_dir, "debug.log"), "a", encoding="utf-8") as f:
            f.write(f"[watchdog {time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def fire_decision(agent_up, age, reply_hung, up_sec):
    """The shared fire/no-fire thresholds, factored out so both the normal
    (has created_at) and the legacy (fail-closed, no created_at) entry paths
    in handle_dir apply the exact same timing rules to their own `age`."""
    if not agent_up:
        return age > DOWN_GRACE_SEC, "agent-down"
    if reply_hung and age > up_sec:
        return True, "reply-hung"
    if age > WEDGED_SEC:
        return True, "wedged-backstop"
    return False, ""


def deliver(tok, chat_id, message_id, answer, progress_dir, synthetic=False, sent=None):
    """Deliver the real answer if we have one (sendMessage + drop the
    placeholder), else rewrite the placeholder into a generic error. Returns a
    short label for logging.

    #2269: a synthetic last text (CLI limit or error line) is never sent: the placeholder becomes
    OUTAGE_TEXT and stays, no ledger row. A real send is appended to `sent`; handle_dir ledgers it
    only after the marker file no longer holds the entry."""
    if answer and synthetic:
        try:
            api(tok, "editMessageText",
                {"chat_id": chat_id, "message_id": message_id, "text": OUTAGE_TEXT})
        except Exception as e:
            log(progress_dir, f"outage edit failed (mid={message_id}): {e}")
        return "synthetic-outage"
    if answer:
        try:
            res = api(tok, "sendMessage", {"chat_id": chat_id, "text": answer[:4000]})
        except Exception as e:
            log(progress_dir, f"real-answer send failed (mid={message_id}): {e}")
            return "send-failed"
        if sent is not None:
            sent.append((chat_id, answer[:4000], res))
        try:
            api(tok, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except Exception as e:
            log(progress_dir, f"placeholder delete failed (mid={message_id}): {e}")
        return "real-answer"
    # No recoverable answer -> generic error, keep the (edited) placeholder.
    try:
        api(tok, "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": ERROR_TEXT})
    except Exception as e:
        log(progress_dir, f"error edit failed (mid={message_id}): {e}")
    return "generic-error"


def handle_dir(progress_dir):
    state_dir = os.path.dirname(progress_dir)           # .../telegram
    name = agent_name_from(progress_dir)
    agent_up = tmux_session_alive(f"agent-{name}") if name else True
    now = time.time()
    # Sweep orphan dedup markers (normally removed by the Stop hook).
    for m in glob.glob(os.path.join(progress_dir, "seen-*.marker")):
        try:
            if now - os.path.getmtime(m) > 3600:
                os.remove(m)
        except Exception:
            pass
    tok = None
    up_sec = wedged_up_sec()
    max_age = stale_sec()
    for path in glob.glob(os.path.join(progress_dir, "*.json")):
        try:
            file_age = now - os.path.getmtime(path)
        except Exception:
            continue
        try:
            pend = json.load(open(path))
        except Exception:
            pend = []
        if not pend:
            continue

        # TGORPHANFILE908: every entry is aged, scoped and delivered on its
        # OWN terms -- a fresh append elsewhere in this same file must never
        # mask or contaminate an older sibling entry. `keep` collects the
        # entries still legitimately pending after this pass; the file is
        # rewritten to hold exactly those (same partial-rewrite pattern
        # telegram_progress_reply_clear.py already uses), not wiped wholesale.
        keep = []
        labels = []
        sent = []   # #2269: real sends, ledgered after the marker file is rewritten
        for p in pend:
            mid = p.get("message_id")
            created_at = p.get("created_at")
            has_ts = isinstance(created_at, (int, float)) and created_at > 0
            age = (now - created_at) if has_ts else file_age

            # UPPER age bound (TGORPHAN908): this entry alone marks a DEAD
            # round. Deliver NOTHING; drop just this entry. The placeholder
            # message is cleaned up only while Telegram still allows deletion
            # (<48h); past that the delete can only fail (HTTP 400).
            if age > max_age:
                if age < TELEGRAM_DELETE_WINDOW_SEC:
                    if tok is None:
                        tok = token(state_dir)
                    if tok:
                        try:
                            api(tok, "deleteMessage",
                                {"chat_id": p.get("chat_id"), "message_id": mid})
                        except Exception as e:
                            log(progress_dir,
                                f"stale placeholder delete failed (mid={mid}): {e}")
                labels.append("stale-dropped")
                log(progress_dir, f"entry dropped (stale): mid={mid} "
                                  f"age={int(age)}s has_created_at={has_ts}")
                continue

            if not has_ts:
                # Legacy entry, written before TGORPHANFILE908 shipped: no
                # per-entry timestamp, so its true round cannot be identified
                # inside a file it may share with newer entries. Fail CLOSED:
                # keep the existing up/down/backstop TIMING (best estimate =
                # file_age) so a genuinely stuck legacy placeholder still
                # eventually resolves, but NEVER scrape a transcript answer
                # for it -- a foreign entry's text leaking onto an
                # unidentifiable placeholder is exactly the TGORPHAN908
                # failure this feature exists to prevent.
                fire, reason = fire_decision(agent_up, age, False, up_sec)
                if not fire:
                    keep.append(p)
                    continue
                if tok is None:
                    tok = token(state_dir)
                if not tok:
                    keep.append(p)
                    continue
                mode = deliver(tok, p.get("chat_id"), mid, "", progress_dir)
                labels.append(f"legacy-{reason}:{mode}")
                log(progress_dir, f"entry handled (legacy, {reason}): mid={mid} "
                                  f"age={int(age)}s delivered={mode}")
                continue

            # Normal path: this entry has its own created_at, so its round can
            # be scoped precisely even inside a file shared with other rounds.
            transcript_path = p.get("transcript_path") or ""
            answer, reply_hung, reply_delivered, synthetic = read_transcript(
                transcript_path, turn_start=created_at)
            fire, reason = fire_decision(agent_up, age, reply_hung, up_sec)
            if fire and reason == "wedged-backstop":
                # #2269: an open round that wrote a line within WEDGED_SEC is working, not wedged;
                # anything that cannot be told (no anchor, an ended round) fires as before.
                last, round_open = round_activity(transcript_path, created_at)
                if round_open and last is not None and now - last < WEDGED_SEC:
                    fire = False
            if not fire:
                keep.append(p)
                continue
            if tok is None:
                tok = token(state_dir)
            if not tok:
                keep.append(p)
                continue

            # The round's own reply already reached the channel (a reply call
            # in this round's window has a result): this entry is leftover
            # bookkeeping from a missed Stop hook. Resending would duplicate
            # the answer -- and the transcript's LAST text may belong to a
            # later, internal turn. Clear silently.
            if reply_delivered and not reply_hung:
                try:
                    api(tok, "deleteMessage",
                        {"chat_id": p.get("chat_id"), "message_id": mid})
                except Exception as e:
                    log(progress_dir, f"placeholder delete failed (mid={mid}): {e}")
                labels.append("reply-already-delivered")
                log(progress_dir, f"entry cleared (reply-already-delivered): "
                                  f"mid={mid} agent_up={agent_up} age={int(age)}s")
                continue

            mode = deliver(tok, p.get("chat_id"), mid, answer, progress_dir,
                           synthetic=synthetic, sent=sent)
            labels.append(f"{reason}:{mode}")
            log(progress_dir, f"entry handled ({reason}): mid={mid} "
                              f"agent_up={agent_up} age={int(age)}s delivered={mode}")

        if not labels:
            continue  # nothing changed for this file -- avoid a needless rewrite
        if keep:
            try:
                json.dump(keep, open(path, "w"))
            except Exception as e:
                log(progress_dir, f"partial rewrite failed for "
                                  f"{os.path.basename(path)}: {e}")
        else:
            try:
                os.remove(path)
            except Exception:
                pass
        log(progress_dir, f"{os.path.basename(path)}: handled "
                          f"{len(labels)}/{len(pend)} entr{'y' if len(labels)==1 else 'ies'} "
                          f"({','.join(labels)}), {len(keep)} still pending")
        # #2269: the ledger rows only now, with the handled entries gone from the marker file.
        for chat_id, text, res in sent:
            ledger_log_out(progress_dir, chat_id, text, res)


def main():
    dirs = []
    for g in SCAN_GLOBS:
        dirs.extend(glob.glob(g))
    for d in dirs:
        if os.path.isdir(d):
            handle_dir(d)


if __name__ == "__main__":
    main()

import { describe, it, expect, vi, beforeEach } from 'vitest'

// Port of upstream 32ee1eea's binding test, adapted to the local protectTrailingSemicolon form.
// BORITEKVESZ927 review follow-up (#1625): computeTmuxChunk is pinned on its
// own in tmux-chunk-boundary.test.ts, but nothing asserted that the code that
// SHIPS -- sendPromptToSession's chunk stream -- goes through it. Replacing the
// call with the old inline dash-only loop left every test green. This drives
// the real sendPromptToSession and inspects each `send-keys -l` argument.
//
// Harness as in janitor-under-send-lane.test.ts: runTmux/capturePane are local
// to agent-process and go through node:child_process execFileSync, so that is
// mocked with argument inspection. The pane is an idle, empty prompt box, so
// the submit follow-up loop finishes on its first sample.

const h = vi.hoisted(() => {
  const SEP = '─'.repeat(80)
  const FOOTER = '  ⏵⏵ bypass permissions on (shift+tab to cycle)'
  const IDLE = ['', SEP, '❯ ', SEP, FOOTER].join('\n')
  return { IDLE, calls: [] as string[][] }
})

vi.mock('node:child_process', async (orig) => ({
  ...(await orig() as object),
  execFileSync: vi.fn((_file: string, args?: string[]) => {
    if (Array.isArray(args)) {
      h.calls.push(args)
      if (args.includes('capture-pane')) return h.IDLE
    }
    return ''
  }),
}))
vi.mock('../notify.js', () => ({ notifyChannel: vi.fn(async () => {}), notifyTelegram: vi.fn(async () => {}) }))

import { sendPromptToSession } from '../web/agent-process.js'
import { __resetSessionSendLocks } from '../web/session-send-lock.js'

function literalChunks(): string[] {
  return h.calls
    .filter(a => a.includes('send-keys') && a.includes('-l'))
    .map(a => a[a.indexOf('-l') + 1])
}

beforeEach(() => {
  h.calls.length = 0
  __resetSessionSendLocks()
})

describe('sendPromptToSession streams through computeTmuxChunk', () => {
  it("no literal chunk ends in ';' or starts with '-', and every ';' survives", async () => {
    // Boundaries are computed WITH the slides: chunk 1 would end on ';'
    // followed by 'x-' (the case where two sequential dodge loops typed a
    // space into the text) and slides to index 82; chunk 2 (82..161) ends on
    // a ';'; the prompt's very last character is a ';'.
    const text =
      'a'.repeat(79) + ';x-' +
      'b'.repeat(79) + ';' +
      'tail ' + 'd'.repeat(20) + ';'
    expect(await sendPromptToSession('chunk-binding-test', text, null, { waitForIdle: false })).toBe('sent')

    const chunks = literalChunks()
    expect(chunks.length).toBe(3)
    for (const c of chunks) {
      // Local adaptation: a chunk-final ';' is protected as '\\;' (tmux types it as ';'), never left bare.
      expect(c.endsWith(';') && !c.endsWith('\\;')).toBe(false)
      expect(c.startsWith('-')).toBe(false)
    }
    // What tmux types: the one added backslash before a trailing ';' is consumed. No space is added anywhere,
    // unlike upstream's pad after a prompt-final ';': the typed text is the prompt, byte for byte.
    const typed = chunks.map(c => (c.endsWith('\\;') ? c.slice(0, -2) + ';' : c)).join('')
    expect(typed).toBe(text)
  }, 15_000)
})

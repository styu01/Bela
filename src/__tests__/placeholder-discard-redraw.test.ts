import { describe, it, expect } from 'vitest'
import { detectsPastePlaceholder, inputBoxText } from '../pane-state.js'
import { discardPlaceholderBuffer } from '../web/agent-process.js'

// Upstream fc014409, ported: after each Ctrl-C the placeholder clear waits until the input box is
// REDRAWN (its text differs from the pressed frame) before another press is considered. On a loaded
// host the frame still showed the stub 450 ms after a press, the old loop pressed again onto a box the
// TUI had already emptied, and Ctrl-C on an empty box quits the session.

const SEP = '─'.repeat(80)
const FOOTER = '  ⏵⏵ bypass permissions on (shift+tab to cycle)'
const pane = (box: string) => ['  ⎿  earlier output', '', SEP, box, SEP, FOOTER].join('\n')
const STUB = pane('❯ [Pasted text #1 +812 chars]')
const STUB2 = pane('❯ [Pasted text #2 +812 chars]')
const EMPTY = pane('❯ ')

function io(frames: (string | null)[]) {
  const sent: string[] = []
  let n = 0
  return {
    sent,
    captures: () => n,
    io: {
      capture: () => frames[Math.min(n++, frames.length - 1)],
      send: (_s: string, _h: string | null, k: string) => { sent.push(k) },
      wait: async () => {},
    },
  }
}

describe('fixtures', () => {
  it('the stub frames are placeholders, the empty one is not; inputBoxText reads the box only', () => {
    expect(detectsPastePlaceholder(STUB)).toBe(true)
    expect(detectsPastePlaceholder(EMPTY)).toBe(false)
    expect(inputBoxText(STUB)).toBe('❯ [Pasted text #1 +812 chars]')
    expect(inputBoxText(EMPTY)).toBe('❯ ')
    expect(inputBoxText('no separators here\n❯ x')).toBeNull()
    expect(inputBoxText(null)).toBeNull()
  })
})

describe('discardPlaceholderBuffer', () => {
  it('one press, the box is redrawn empty: cleared, exactly one Ctrl-C', async () => {
    const f = io([STUB, EMPTY])
    expect(await discardPlaceholderBuffer('agent-x', null, f.io)).toBe(true)
    expect(f.sent).toEqual(['C-c'])
  })

  it('LATE REDRAW (the upstream incident): the stale frame keeps the stub for two polls, still ONE press', async () => {
    const f = io([STUB, STUB, STUB, EMPTY])
    expect(await discardPlaceholderBuffer('agent-x', null, f.io)).toBe(true)
    expect(f.sent).toEqual(['C-c'])
  })

  it('never redrawn within the wait: false, and NO second Ctrl-C', async () => {
    const f = io([STUB, STUB, STUB, STUB, STUB, STUB, STUB, STUB, STUB, STUB])
    expect(await discardPlaceholderBuffer('agent-x', null, f.io)).toBe(false)
    expect(f.sent).toEqual(['C-c'])
    // the wait is bounded: 1 initial capture + 7 settle polls
    expect(f.captures()).toBe(8)
  })

  it('redrawn but the stub is back (the first press was eaten mid-render): a second press is allowed', async () => {
    const f = io([STUB, STUB2, EMPTY])
    expect(await discardPlaceholderBuffer('agent-x', null, f.io)).toBe(true)
    expect(f.sent).toEqual(['C-c', 'C-c'])
  })

  it('at most three presses', async () => {
    const f = io([STUB, STUB2, STUB, STUB2])
    expect(await discardPlaceholderBuffer('agent-x', null, f.io)).toBe(false)
    expect(f.sent).toEqual(['C-c', 'C-c', 'C-c'])
  })

  it('LOCAL: a failed first capture sends nothing (no blind press on an unseen box)', async () => {
    const f = io([null])
    expect(await discardPlaceholderBuffer('agent-x', null, f.io)).toBe(false)
    expect(f.sent).toEqual([])
  })

  it('a failed capture after the press stops the clear without another press', async () => {
    const f = io([STUB, null])
    expect(await discardPlaceholderBuffer('agent-x', null, f.io)).toBe(false)
    expect(f.sent).toEqual(['C-c'])
  })

  it('no stub on the first look: nothing pressed, true', async () => {
    const f = io([EMPTY])
    expect(await discardPlaceholderBuffer('agent-x', null, f.io)).toBe(true)
    expect(f.sent).toEqual([])
  })
})

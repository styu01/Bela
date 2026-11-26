/**
 * BORITEKVESZ927 (upstream 32ee1eea), local form: computeTmuxChunk splits a prompt into `send-keys -l` chunks.
 * A chunk that ends in ';' lost that ';' in tmux (measured on tmux 3.6, 2026-11-26), and a chunk that starts with
 * '-' parses as a flag. Here the ';' is protected (protectTrailingSemicolon) instead of upstream's slide-and-pad,
 * so the TYPED text must equal the prompt byte for byte, wherever the boundaries fall. The real-tmux case below
 * proves it end to end on a private tmux socket (its own server, a `cat` pane, never a fleet session).
 */
import { describe, it, expect } from 'vitest'
import { execFileSync } from 'node:child_process'
import { computeTmuxChunk, TMUX_CHUNK_MAX_SLIDE } from '../web/agent-process.js'

function chunksOf(text: string, size: number): string[] {
  const out: string[] = []
  let i = 0
  while (i < text.length) {
    const { chunk, end } = computeTmuxChunk(text, i, size)
    expect(end).toBeGreaterThan(i)
    out.push(chunk)
    i = end
  }
  return out
}

// What tmux types for one -l argument: a trailing '\;' loses exactly one backslash, a bare trailing ';' is dropped.
function tmuxTypes(arg: string): string {
  if (arg.endsWith('\\;')) return arg.slice(0, -2) + ';'
  if (arg.endsWith(';')) return arg.slice(0, -1)
  return arg
}

const NASTY = [
  'a'.repeat(79) + ';x-' + 'b'.repeat(79) + ';tail;',
  'x'.repeat(6) + ';;;;;;;;;;;;' + 'y',
  'print(t[4:]);print(1);',
  'find . -exec rm {} \\;',
  'abc-szal-vel-ban;-x;',
  ';',
  'z'.repeat(5) + '\\;' + 'w',
]

describe('computeTmuxChunk', () => {
  for (const text of NASTY) {
    for (const size of [1, 2, 3, 5, 7, 80]) {
      it(`types ${JSON.stringify(text.slice(0, 24))} exactly at chunk size ${size}`, () => {
        const chunks = chunksOf(text, size)
        for (const c of chunks) {
          // never a bare trailing ';' (an escaped one is the protection itself)
          expect(c.endsWith(';') && !c.endsWith('\\;')).toBe(false)
          expect(c.startsWith('-')).toBe(false)
        }
        // The only permitted change is the pre-existing ' ' pad in front of a leading '-' once the dash slide
        // hits its cap; none of these fixtures has a dash run that long, so the typed text must be exact.
        expect(chunks.map(tmuxTypes).join('')).toBe(text)
      })
    }
  }

  it('slides at most TMUX_CHUNK_MAX_SLIDE past dashes, then pads', () => {
    const t = 'a'.repeat(10) + '-'.repeat(20) + 'b'
    const first = computeTmuxChunk(t, 0, 10)
    expect(first.end).toBe(10 + TMUX_CHUNK_MAX_SLIDE)
    expect(computeTmuxChunk(t, first.end, 10).chunk.startsWith(' -')).toBe(true)
  })
})

let tmuxOk = false
try { execFileSync('tmux', ['-V'], { stdio: 'ignore' }); tmuxOk = true } catch { /* no tmux */ }

describe.skipIf(!tmuxOk)('real tmux (private socket): the chunked stream types the prompt byte for byte', () => {
  it('every ;, backslash and dash survives, at many boundary positions', () => {
    const sock = `chunktest-${process.pid}`
    const tmux = (...a: string[]) => execFileSync('tmux', ['-L', sock, ...a], { encoding: 'utf-8' })
    const text = 'b=t[4:];print(1);;' + 'x'.repeat(3) + ';;;;;;;;;;;' + 'find . -exec rm {} \\;' + 'k-szal;end;'
    try {
      for (const size of [1, 3, 7, 80]) {
        const sess = 's' + size
        tmux('new-session', '-d', '-s', sess, '-x', '400', '-y', '5', 'cat')
        for (const c of chunksOf(text, size)) tmux('send-keys', '-t', sess, '-l', c)
        execFileSync('/bin/sleep', ['0.3'])
        expect(tmux('capture-pane', '-p', '-t', sess).split('\n')[0], `size ${size}`).toBe(text)
      }
    } finally {
      try { tmux('kill-server') } catch { /* ignore */ }
    }
  })
})

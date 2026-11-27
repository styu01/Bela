import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { detectsFirstRunGate, detectsBlockingMenu, firstRunAcceptKeys } from '../pane-state.js'
import { answerAcceptDialog, answerFirstRunGates } from '../web/agent-process.js'

// TRUSTGATE901 (upstream ef83f266, ported). The trust and bypass first-run dialogs are answered from
// the pane, never by number. The fixtures are REAL captures of Claude Code 2.1.326, taken 2026-11-27
// in a private tmux server with a throwaway HOME and project dir (capture-pane only, no key sent).
// Key behaviour measured the same day in a private pty (docs/projects/upstream-w3/dialog-results.txt):
// on both dialogs Enter, Tab+Enter, "1"+Enter and "2"+Enter EXIT the session; Down+Enter accepts.

const FX = join(__dirname, 'fixtures', 'pane-2.1.326')
const load = (n: string) => readFileSync(join(FX, n), 'utf-8')
const TRUST = ['first-run-trust-80x24.txt', 'first-run-trust-120x40.txt']
const BYPASS = ['first-run-bypass-80x24.txt', 'first-run-bypass-120x40.txt']

/** Move the cursor glyph from the row containing `from` to the row containing `to` (a redraw after Down). */
function moveCursor(pane: string, from: RegExp, to: RegExp): string {
  return pane.split('\n').map((l) => {
    if (from.test(l)) return l.replace('❯', ' ')
    if (to.test(l)) return l.replace(/^( *) {2}(\S)/, '$1❯ $2')
    return l
  }).join('\n')
}

describe('2.1.326 real captures', () => {
  for (const f of TRUST) {
    it(`${f}: detected as the trust gate (the old question string is gone)`, () => {
      const p = load(f)
      expect(p).not.toContain('Do you trust the files in this folder?')
      expect(p).toContain('❯ No, exit')
      expect(detectsFirstRunGate(p)).toBe('trust')
      // and it IS a generic blocking menu too, whose Escape would be "No, exit": the gate must win
      expect(detectsBlockingMenu(p)).toBe(true)
    })
    it(`${f}: the answer is Down + Enter, NOT a bare Enter and not a number`, () => {
      const keys = firstRunAcceptKeys(load(f))
      expect(keys).toEqual(['Down', 'Enter'])
      expect(keys).not.toEqual(['Enter'])
      expect(keys!.some((k) => /^\d$/.test(k))).toBe(false)
    })
  }
  for (const f of BYPASS) {
    it(`${f}: detected as the bypass gate, answer Down + Enter`, () => {
      const p = load(f)
      expect(detectsFirstRunGate(p)).toBe('bypass-permissions')
      expect(firstRunAcceptKeys(p)).toEqual(['Down', 'Enter'])
    })
  }
  it('after the Down lands (cursor redrawn on the Yes row) the answer is a bare Enter', () => {
    const t = moveCursor(load(TRUST[0]), /❯ No, exit/, /Yes, I trust this folder/)
    expect(t).toContain('❯ Yes, I trust this folder')
    expect(firstRunAcceptKeys(t)).toEqual(['Enter'])
    const b = moveCursor(load(BYPASS[0]), /❯ No, exit/, /Yes, I accept/)
    expect(firstRunAcceptKeys(b)).toEqual(['Enter'])
  })
})

describe('other layouts the rule must handle', () => {
  it('2.1.246 numbered, Yes first and highlighted -> Enter', () => {
    const p = [' Quick safety check: ...', '', ' ❯ 1. Yes, I trust this folder', '   2. No, exit', '', ' Enter to confirm · Esc to cancel'].join('\n')
    expect(detectsFirstRunGate(p)).toBe('trust')
    expect(firstRunAcceptKeys(p)).toEqual(['Enter'])
  })
  it('the old boxed trust dialog (bordered blank rows separate prose from options) -> Enter', () => {
    const p = [
      '╭──────────────────────────────╮',
      '│ Do you trust the files in this folder?│',
      '│                              │',
      '│ ❯ 1. Yes, proceed            │',
      '│   2. No, exit                │',
      '╰──────────────────────────────╯',
    ].join('\n')
    expect(firstRunAcceptKeys(p)).toEqual(['Enter'])
  })
  it('a boxed dialog whose PROSE says "yes": bordered blank rows split the blocks, so only the option row counts', () => {
    const p = [
      '│ Answer yes only if you wrote this code. │',
      '│                                        │',
      '│ ❯ No, exit                             │',
      '│   Yes, I trust this folder             │',
      '│                                        │',
    ].join('\n')
    expect(firstRunAcceptKeys(p)).toEqual(['Down', 'Enter'])
  })
  it('the old numbered bypass dialog (No first, highlighted) -> Down + Enter, never "2"', () => {
    const p = ['│ Bypass Permissions mode │', '│                        │', '│ ❯ 1. No, exit          │', '│   2. Yes, I accept     │'].join('\n')
    expect(firstRunAcceptKeys(p)).toEqual(['Down', 'Enter'])
  })
  it('cursor BELOW the Yes row -> Up + Enter', () => {
    const p = ['   Yes, I trust this folder', ' ❯ No, exit'].join('\n')
    expect(firstRunAcceptKeys(p)).toEqual(['Up', 'Enter'])
  })
})

describe('unrecognised shapes send nothing (null)', () => {
  it('empty / no cursor', () => {
    expect(firstRunAcceptKeys('')).toBeNull()
    expect(firstRunAcceptKeys('   No, exit\n   Yes, I trust this folder')).toBeNull()
  })
  it('two yes-looking rows', () => {
    expect(firstRunAcceptKeys(' ❯ No, exit\n   Yes, I trust this folder\n   Yes, always')).toBeNull()
  })
  it('a yes row without the "No, exit" refusal beside it is not one of these dialogs', () => {
    expect(firstRunAcceptKeys(' ❯ 1. Dark mode\n   2. Yes please')).toBeNull()
  })
  it('a "Yes, ... exit"-shaped row is not consent', () => {
    expect(firstRunAcceptKeys(' ❯ No, exit\n   Yes, and exit')).toBeNull()
  })
  it('the yes row in ANOTHER block (separated by a blank line) is not reachable from the cursor', () => {
    expect(firstRunAcceptKeys(' Yes, I trust this folder\n\n ❯ No, exit\n   Maybe later')).toBeNull()
  })
})

describe('answerAcceptDialog (tmux layer injected)', () => {
  function fakeIo(captures: (string | null)[]) {
    const sent: string[] = []
    let n = 0
    return {
      sent,
      captures: () => n,
      io: {
        capture: () => { const c = captures[Math.min(n, captures.length - 1)]; n++; return c },
        send: (_s: string, _h: string | null, k: string) => { sent.push(k) },
        wait: async () => {},
      },
    }
  }
  const trust = load(TRUST[0])
  const landed = moveCursor(trust, /❯ No, exit/, /Yes, I trust this folder/)

  it('real 2.1.326 trust: Down, re-capture shows the cursor on Yes, then Enter', async () => {
    const f = fakeIo([landed])
    expect(await answerAcceptDialog('agent-x', null, trust, f.io)).toBe('answered')
    expect(f.sent).toEqual(['Down', 'Enter'])
    expect(f.captures()).toBe(1)
  })
  it('the Down did not land (cursor still on "No, exit"): blocked, NO Enter', async () => {
    const f = fakeIo([trust])
    expect(await answerAcceptDialog('agent-x', null, trust, f.io)).toBe('blocked')
    expect(f.sent).toEqual(['Down'])
  })
  it('the re-capture failed: blocked, NO Enter', async () => {
    const f = fakeIo([null])
    expect(await answerAcceptDialog('agent-x', null, trust, f.io)).toBe('blocked')
    expect(f.sent).not.toContain('Enter')
  })
  it('the re-capture shows something else entirely (dialog gone, prompt up): blocked, NO Enter', async () => {
    const f = fakeIo(['──────\n ❯ \n──────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'])
    expect(await answerAcceptDialog('agent-x', null, trust, f.io)).toBe('blocked')
    expect(f.sent).not.toContain('Enter')
  })
  it('cursor already on Yes: a single Enter, no re-capture needed', async () => {
    const f = fakeIo([])
    expect(await answerAcceptDialog('agent-x', null, landed, f.io)).toBe('answered')
    expect(f.sent).toEqual(['Enter'])
    expect(f.captures()).toBe(0)
  })
  it('unknown shape: blocked, nothing sent at all', async () => {
    const f = fakeIo([])
    expect(await answerAcceptDialog('agent-x', null, ' some dialog\n ❯ Ok', f.io)).toBe('blocked')
    expect(f.sent).toEqual([])
  })
})

describe('answerFirstRunGates (tmux layer injected)', () => {
  const trust = load(TRUST[0])
  const landed = moveCursor(trust, /❯ No, exit/, /Yes, I trust this folder/)
  const idle = '──────\n ❯ \n──────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)'
  function io(seq: (string | null)[]) {
    const sent: string[] = []
    let n = 0
    return { sent, io: { capture: () => seq[Math.min(n++, seq.length - 1)], send: (_s: string, _h: string | null, k: string) => { sent.push(k) }, wait: async () => {} } }
  }
  it('trust on 2.1.326: Down, verified, Enter, then the prompt -> cleared', async () => {
    const f = io([trust, landed, idle])
    expect(await answerFirstRunGates('agent-x', null, f.io)).toBe('cleared')
    expect(f.sent).toEqual(['Down', 'Enter'])
  })
  it('trust then bypass (the fresh-dir chain): both answered from the pane', async () => {
    const bypass = load(BYPASS[0])
    const bLanded = moveCursor(bypass, /❯ No, exit/, /Yes, I accept/)
    const f = io([trust, landed, bypass, bLanded, idle])
    expect(await answerFirstRunGates('agent-x', null, f.io)).toBe('cleared')
    expect(f.sent).toEqual(['Down', 'Enter', 'Down', 'Enter'])
  })
  it('the Down does not land: blocked at once, NO Enter, no further walking', async () => {
    const f = io([trust, trust, trust, trust])
    expect(await answerFirstRunGates('agent-x', null, f.io)).toBe('blocked')
    expect(f.sent).toEqual(['Down'])
  })
  it('the login picker is never answered', async () => {
    const f = io([' Welcome to Claude Code\n\n Select login method:\n\n ❯ 1. Claude account with subscription\n   2. Anthropic Console account'])
    expect(await answerFirstRunGates('agent-x', null, f.io)).toBe('login')
    expect(f.sent).toEqual([])
  })
  it('no gate: unchanged, nothing sent', async () => {
    const f = io([idle])
    expect(await answerFirstRunGates('agent-x', null, f.io)).toBe('unchanged')
    expect(f.sent).toEqual([])
  })
})

const AGENT_PROCESS = readFileSync(join(__dirname, '../web/agent-process.ts'), 'utf-8')
const CHANNEL_MONITOR = readFileSync(join(__dirname, '../web/channel-monitor.ts'), 'utf-8')

describe('wiring contracts', () => {
  it('answerFirstRunGates answers trust and bypass through answerAcceptDialog, never by number', () => {
    const i = AGENT_PROCESS.indexOf('export async function answerFirstRunGates(')
    const fn = AGENT_PROCESS.slice(i, AGENT_PROCESS.indexOf('// Post-(re)start identity setup', i))
    expect(fn).toContain("if (gate === 'trust' || gate === 'bypass-permissions')")
    expect(fn).toContain('await answerAcceptDialog(session, host, pane as string, io)')
    expect(fn).toContain("return 'blocked'")
    expect(fn).not.toMatch(/'send-keys',[^\]]*'[12]'/)
  })
  it('the EPERM fallback answers through the same rule (no "1"/"2" + Enter left in it)', () => {
    const i = AGENT_PROCESS.indexOf('const checkEperm = () => {')
    const fn = AGENT_PROCESS.slice(i, AGENT_PROCESS.indexOf('setTimeout(checkEperm, 1500)', i))
    expect(fn.length).toBeGreaterThan(500)
    expect(fn).not.toMatch(/'send-keys', '-t', exactTmuxTarget\(session\), '[12]'/)
    expect(fn).not.toContain('Do you trust the files in this folder')
    expect(fn).toContain('void answerAcceptDialog(session, null, pane)')
    expect(fn).toContain("} else if (gate === 'welcome') {")
  })
  it('the channel-monitor alerts on a blocked answer instead of reporting it as advanced', () => {
    expect(CHANNEL_MONITOR).toMatch(/\} else if \(res === 'blocked'\) \{[\s\S]{0,900}?alertOwnerOrBela\(t, 'first-run-blocked'/)
  })
})

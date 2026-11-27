import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { spawnSync } from 'node:child_process'
import { cpSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, appendFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { failClosedGateRun, GATE_FAIL_CLOSED_TAG } from '../web/agent-scaffold.js'
import { PROJECT_ROOT } from '../config.js'

// Card 723bbb70 (upstream 0e681c83, ported): the main agent's PROJECT settings carry the same fail-closed
// tail around its security gate, outgoing-copy-gate.py, on the Bash / send_email / Gmail arms.
// LOCAL DECISION (BELA msg 6213): the Telegram reply arm stays WITHOUT the tail. Telegram is the owner's
// only supervision channel and is fail-open by design inside the gate too ("Telegram stays fail-open by
// design"); a broken gate file must not also silence the channel that would report the breakage.
// Every entry is run here exactly as written, with CLAUDE_PROJECT_DIR pointing at a disposable copy.

const GATE = 'scripts/hooks/outgoing-copy-gate.py'
const RAW = 'python3 "$CLAUDE_PROJECT_DIR/scripts/hooks/outgoing-copy-gate.py"'
const TG = /telegram/
const TG_GUARD = '[ -f "$CLAUDE_PROJECT_DIR/scripts/hooks/outgoing-copy-gate.py" ] || exit 0; '

let tmp: string
let root: string
let env: NodeJS.ProcessEnv
beforeAll(() => {
  tmp = mkdtempSync(join(tmpdir(), 'hook-fc-main-'))
  root = join(tmp, 'install')
  mkdirSync(join(root, 'store'), { recursive: true })
  cpSync(join(PROJECT_ROOT, 'scripts'), join(root, 'scripts'), { recursive: true })
  mkdirSync(join(tmp, 'home'), { recursive: true })
  env = { ...process.env, HOME: join(tmp, 'home'), CLAUDE_PROJECT_DIR: root, PYTHONDONTWRITEBYTECODE: '1' }
})
afterAll(() => { rmSync(tmp, { recursive: true, force: true }) })

function run(command: string, input: string) {
  const r = spawnSync('/bin/sh', ['-c', command], { input, env, cwd: root, encoding: 'utf-8', timeout: 30_000 })
  return { rc: r.status, stdout: r.stdout, stderr: r.stderr }
}

function breakGate(): () => void {
  const p = join(root, GATE)
  const orig = readFileSync(p)
  appendFileSync(p, '\nx = (\n')
  return () => writeFileSync(p, orig)
}

type Entry = { matcher: string; command: string; timeout: number }
function entries(): Entry[] {
  const settings = JSON.parse(readFileSync(join(PROJECT_ROOT, '.claude', 'settings.json'), 'utf-8'))
  return (settings.hooks.PreToolUse as { matcher: string; hooks: { command: string; timeout: number }[] }[])
    .flatMap((e) => e.hooks.map((h) => ({ matcher: e.matcher, command: h.command, timeout: h.timeout })))
    .filter((e) => e.command.includes('outgoing-copy-gate.py'))
}

// Per arm: one pass-through and one denial, so "wrapped == raw" is not 0 == 0 only.
function inputsFor(matcher: string): string[] {
  const base = { hook_event_name: 'PreToolUse', session_id: 'hook-fc-main-test', cwd: root }
  const mk = (tool_name: string, tool_input: unknown) => JSON.stringify({ ...base, tool_name, tool_input })
  if (matcher === 'Bash') {
    return [mk('Bash', { command: 'true' }),
      mk('Bash', { command: 'curl -s -X POST http://localhost:3420/api/messages -d \'{"from":"bela","to":"progi","content":"nezd meg a kаrtyat"}\'' })]
  }
  if (TG.test(matcher)) {
    return [mk('mcp__plugin_telegram_telegram__reply', { chat_id: '1', text: 'rendben' }),
      mk('mcp__plugin_telegram_telegram__reply', { chat_id: '1', text: 'a — b' })]
  }
  const tool = matcher.includes('send_email') ? 'mcp__teszt__send_email' : 'mcp__claude_ai_Gmail__create_draft'
  return [mk(tool, { to: ['a@example.invalid'], subject: 's', body: 'b — c' })]
}

describe('main agent project settings: outgoing-copy-gate arms', () => {
  it('four arms: Bash, send_email, Gmail with the tail (byte-identical to the scaffold builder), Telegram without', () => {
    const es = entries()
    expect(es.length).toBe(4)
    const closed = es.filter((e) => !TG.test(e.matcher))
    const open = es.filter((e) => TG.test(e.matcher))
    expect(closed.map((e) => e.matcher)).toEqual(['Bash', '.*send_email.*', expect.stringContaining('[Gg]mail')])
    for (const e of closed) {
      expect(e.command, e.matcher).toBe(failClosedGateRun(RAW, 'outgoing-copy-gate.py', e.timeout))
      expect(e.command).toContain(GATE_FAIL_CLOSED_TAG)
    }
    expect(open.length).toBe(1)
    // PROGI 6248 (i): a MISSING gate file must not block the owner's only channel either (python3 exits 2
    // on a missing file, and 2 blocks), so the Telegram arm exits 0 when the file is not there.
    expect(open[0].command).toBe(TG_GUARD + RAW)
  })

  for (const e of entries()) {
    const closed = !TG.test(e.matcher)
    it(`${e.matcher}: intact, the entry's decisions equal the raw call (incl. a denial)`, () => {
      let denied = false
      for (const input of inputsFor(e.matcher)) {
        const a = run(RAW, input)
        const b = run(e.command, input)
        expect(a.rc === 0 || a.rc === 2, `raw rc ${a.rc}: ${a.stderr.slice(0, 300)}`).toBe(true)
        expect({ rc: b.rc, stdout: b.stdout }).toEqual({ rc: a.rc, stdout: a.stdout })
        expect(b.stderr).not.toContain(GATE_FAIL_CLOSED_TAG)
        if (a.rc === 2) denied = true
      }
      expect(denied).toBe(true)
    }, 60_000)

    it(`${e.matcher}: a syntax error in the gate file ${closed ? 'BLOCKS (rc 2)' : 'stays fail-open (rc 1, the deliberate Telegram exception)'}`, () => {
      const restore = breakGate()
      try {
        const input = inputsFor(e.matcher)[0]
        expect(run(RAW, input).rc).toBe(1)
        const b = run(e.command, input)
        expect(b.rc).toBe(closed ? 2 : 1)
        if (closed) expect(b.stderr).toContain(`${GATE_FAIL_CLOSED_TAG}: outgoing-copy-gate.py:`)
      } finally {
        restore()
      }
    }, 60_000)
  }
})

describe('the Telegram arm with the gate file MISSING (PROGI 6248 i)', () => {
  it('exits 0 (passes) where the bare python3 call exits 2 (blocks); the closed arms still block', () => {
    const p = join(root, GATE)
    const orig = readFileSync(p)
    rmSync(p)
    try {
      const input = inputsFor('Bash')[0]
      expect(run(RAW, input).rc).toBe(2)
      for (const e of entries()) {
        expect(run(e.command, input).rc, e.matcher).toBe(TG.test(e.matcher) ? 0 : 2)
      }
    } finally {
      writeFileSync(p, orig)
    }
  }, 60_000)
})

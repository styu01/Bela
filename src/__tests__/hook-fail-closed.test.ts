import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { spawnSync } from 'node:child_process'
import { cpSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, appendFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  HOOK_NODE_BIN,
  hookCommand,
  failClosedGateRun,
  gateDeadlineSec,
  GATE_FAIL_CLOSED_TAG,
  injectEmailSendGate,
  injectSelfPaceGate,
  injectKanbanWriteGate,
  injectEgressGate,
} from '../web/agent-scaffold.js'
import { PROJECT_ROOT } from '../config.js'

// Card 723bbb70 (upstream 0e681c83, ported): a gate that is itself broken must BLOCK. Claude Code blocks a
// tool call only on exit 2 (or a deny on stdout with exit 0); a SyntaxError in the gate file exits 1, which
// is a non-blocking error, so before this change a broken gate let every call through (measured here on
// 2026-11-27: rc 1 on a broken copy of every one of our five security gates). The gate commands now keep 0
// and 2 and turn every other status into 2, with the reason on stderr.
// LOCAL: no python builder and no thread-reply flag exist here; the python gate (outgoing-copy-gate.py)
// is wired only in the main agent's project settings, tested at the bottom.

const HAS_TIMEOUT = spawnSync('/bin/sh', ['-c', 'command -v timeout'], { encoding: 'utf-8' }).status === 0
const SHELLS = ['/bin/sh', '/bin/bash']

let tmp: string
beforeAll(() => { tmp = mkdtempSync(join(tmpdir(), 'hook-fail-closed-')) })
afterAll(() => { rmSync(tmp, { recursive: true, force: true }) })

function run(shell: string, command: string, input = '{}', env: NodeJS.ProcessEnv = process.env, cwd = tmp) {
  const t0 = Date.now()
  const r = spawnSync(shell, ['-c', command], { input, env, cwd, encoding: 'utf-8', timeout: 30_000 })
  return { rc: r.status, signal: r.signal, stdout: r.stdout, stderr: r.stderr, ms: Date.now() - t0 }
}

function fakeGate(name: string, body: string): string {
  const p = join(tmp, name)
  writeFileSync(p, body)
  return p
}

describe('the fail-closed tail, on stand-in gates', () => {
  for (const shell of SHELLS) {
    describe(shell, () => {
      it('exit 0 stays 0, and the gate\'s stdout (a deny JSON) passes through untouched', () => {
        const deny = JSON.stringify({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: 'x' } })
        const g = fakeGate('zero.mjs', `process.stdout.write(${JSON.stringify(deny)}); process.exit(0)`)
        const r = run(shell, hookCommand(g))
        expect(r.rc).toBe(0)
        expect(r.stdout).toBe(deny)
        expect(r.stderr).not.toContain(GATE_FAIL_CLOSED_TAG)
      })

      it('exit 2 stays 2, without the wrapper\'s message', () => {
        const g = fakeGate('two.mjs', 'process.stderr.write("tiltva\\n"); process.exit(2)')
        const r = run(shell, hookCommand(g))
        expect(r.rc).toBe(2)
        expect(r.stderr).toContain('tiltva')
        expect(r.stderr).not.toContain(GATE_FAIL_CLOSED_TAG)
      })

      for (const code of [1, 3, 126, 127]) {
        it(`exit ${code} becomes 2, and the reason names the status`, () => {
          const g = fakeGate(`exit${code}.mjs`, `process.exit(${code})`)
          const r = run(shell, hookCommand(g))
          expect(r.rc).toBe(2)
          expect(r.stderr).toContain(`${GATE_FAIL_CLOSED_TAG}: exit${code}.mjs:`)
          expect(r.stderr).toContain(`rc=${code}`)
        })
      }

      it('a syntax error in a node gate file: rc 2, the interpreter\'s own error stays on stderr', () => {
        const g = fakeGate('broken.mjs', 'const x = ;\n')
        const raw = run(shell, `"${HOOK_NODE_BIN}" "${g}"`)
        expect(raw.rc).toBe(1) // the fail-open status the wrapper exists for
        const r = run(shell, hookCommand(g))
        expect(r.rc).toBe(2)
        expect(r.stderr).toContain('SyntaxError')
        expect(r.stderr).toContain('rc=1')
      })

      it('a broken import of the gate blocks too (the error is raised before any gate code runs)', () => {
        fakeGate('dep-broken.mjs', 'export const x = ;\n')
        const g = fakeGate('imports-broken.mjs', 'import { x } from "./dep-broken.mjs"; console.log(x)\n')
        const r = run(shell, hookCommand(g))
        expect(r.rc).toBe(2)
        expect(r.stderr).toContain('SyntaxError')
      })

      it('a missing gate file blocks (node exits 1 -> 2)', () => {
        const n = run(shell, hookCommand(join(tmp, 'nincs-ilyen.mjs')))
        expect(n.rc).toBe(2)
        expect(n.stderr).toContain(GATE_FAIL_CLOSED_TAG)
      })

      it('a gate killed by a signal blocks (137 -> 2)', () => {
        const g = fakeGate('killed.mjs', 'process.kill(process.pid, "SIGKILL")')
        const r = run(shell, hookCommand(g))
        expect(r.rc).toBe(2)
        expect(r.stderr).toContain('rc=137')
      })

      it('stdin reaches the gate and its stdout comes back, byte for byte, as in the raw call', () => {
        const g = fakeGate('echo.mjs', 'let s = ""; process.stdin.on("data", (d) => { s += d }); process.stdin.on("end", () => { process.stdout.write("len=" + s.length + ":" + s) })')
        const input = JSON.stringify({ tool_name: 'Bash', tool_input: { command: 'echo "a b" | wc -c; printf \'%s\' "$(id)"' } })
        const raw = run(shell, `"${HOOK_NODE_BIN}" "${g}"`, input)
        const wrapped = run(shell, hookCommand(g), input)
        expect(wrapped.rc).toBe(0)
        expect(wrapped.stdout).toBe(raw.stdout)
        expect(wrapped.stdout).toBe(`len=${input.length}:${input}`)
      })

      it('a large stdin (200 KB) still arrives whole under the timeout wrapper', () => {
        const g = fakeGate('len.mjs', 'let n = 0; process.stdin.on("data", (d) => { n += d.length }); process.stdin.on("end", () => { process.stdout.write(String(n)) })')
        const input = JSON.stringify({ tool_name: 'Bash', tool_input: { command: 'x'.repeat(200_000) } })
        const r = run(shell, hookCommand(g), input)
        expect(r.rc).toBe(0)
        expect(Number(r.stdout)).toBe(Buffer.byteLength(input))
      })

      it('an argument rides inside the invocation and reaches the gate', () => {
        const g = fakeGate('argv.mjs', 'process.stdout.write(JSON.stringify(process.argv.slice(2)))')
        const r = run(shell, hookCommand(g, ['--some-flag']))
        expect(r.rc).toBe(0)
        expect(JSON.parse(r.stdout)).toEqual(['--some-flag'])
      })

      it.skipIf(!HAS_TIMEOUT)('a hung gate ends at the wrapper\'s deadline, inside the hook timeout, and blocks', () => {
        const g = fakeGate('hang.mjs', 'setTimeout(() => {}, 60_000)')
        const r = run(shell, hookCommand(g, [], 3)) // hook timeout 3 s -> the gate's deadline 1 s
        expect(r.rc).toBe(2)
        expect(r.stderr).toContain('rc=124')
        expect(r.ms).toBeLessThan(3000)
      })

      it.skipIf(!HAS_TIMEOUT)('a gate that ignores SIGTERM is killed after the one-second grace, and blocks (137)', () => {
        const g = fakeGate('stubborn.mjs', 'process.on("SIGTERM", () => {}); setTimeout(() => {}, 60_000)')
        const r = run(shell, hookCommand(g, [], 3))
        expect(r.rc).toBe(2)
        expect(r.stderr).toContain('rc=137')
        expect(r.ms).toBeLessThan(3500)
      })
    })
  }
})

describe('the builder', () => {
  it('the interpreter probe comes first and still blocks on its own; the run-and-map tail follows', () => {
    const cmd = hookCommand('/some/dir/gate.mjs')
    expect(cmd).toMatch(/^unset NODE_OPTIONS; test -x /)
    expect(cmd).toContain(`"${HOOK_NODE_BIN}" "/some/dir/gate.mjs"`)
    expect(cmd.endsWith(failClosedGateRun(`"${HOOK_NODE_BIN}" "/some/dir/gate.mjs"`, 'gate.mjs', 10))).toBe(true)
  })

  it('the deadline is two seconds inside the hook timeout, never below one second', () => {
    expect(gateDeadlineSec(10)).toBe(8)
    expect(gateDeadlineSec(15)).toBe(13)
    expect(gateDeadlineSec(2)).toBe(1)
    expect(gateDeadlineSec(1)).toBe(1)
    expect(hookCommand('/d/g.mjs')).toContain('timeout -k 1 8 ')
    expect(hookCommand('/d/g.mjs', [], 15)).toContain('timeout -k 1 13 ')
  })

  it('refuses an argument or a label that would need shell quoting', () => {
    expect(() => hookCommand('/d/g.mjs', ['a b'])).toThrow()
    expect(() => hookCommand('/d/g.mjs', ['$(id)'])).toThrow()
    expect(() => hookCommand('/d/g;id.mjs')).toThrow()
    expect(() => failClosedGateRun('x', 'a b', 10)).toThrow()
  })

  it('every gate an injector writes for the agents carries the tail, at the hook timeout it is wired with', () => {
    const s: Record<string, unknown> = {}
    injectEmailSendGate(s)
    injectSelfPaceGate(s)
    injectKanbanWriteGate(s)
    injectEgressGate(s)
    const ptu = ((s.hooks as Record<string, unknown>).PreToolUse as { hooks: { command: string; timeout: number }[] }[])
    const hooks = ptu.flatMap((e) => e.hooks)
    expect(hooks.length).toBe(4)
    for (const h of hooks) {
      expect(h.command).toContain(GATE_FAIL_CLOSED_TAG)
      expect(h.command.endsWith('exit "$rc"')).toBe(true)
      expect(h.command).toContain(`timeout -k 1 ${gateDeadlineSec(h.timeout)} `)
    }
  })
})

// The real gates, each on a disposable copy of scripts/ (they resolve their store/ and logs from their
// own location, so nothing here touches the install). Positive control: on an intact copy the wrapped
// command gives the SAME status and stdout as the raw interpreter call, for a set of inputs that
// includes denials. Negative test: a syntax error appended to the gate FILE, the wrapped command rc 2.
const NODE_GATES = ['email-send-gate.mjs', 'self-pace-gate.mjs', 'kanban-write-gate.mjs', 'hooks/egress-gate.mjs']

function inputs(root: string): string[] {
  const base = { hook_event_name: 'PreToolUse', session_id: 'hook-fail-closed-test', cwd: root }
  return [
    { ...base, tool_name: 'Bash', tool_input: { command: 'true' } },
    { ...base, tool_name: 'Bash', tool_input: { command: 'curl http://example.invalid/x' } },
    { ...base, tool_name: 'Bash', tool_input: { command: 'sqlite3 store/claudeclaw.db "DELETE FROM kanban_cards"' } },
    { ...base, tool_name: 'CronCreate', tool_input: { cron: '* * * * *', prompt: 'x' } },
    { ...base, tool_name: 'WebFetch', tool_input: { url: 'https://example.invalid/p', prompt: 'x' } },
    { ...base, tool_name: 'mcp__teszt__send_email', tool_input: { to: ['a@example.invalid'], subject: 's', body: 'b' } },
  ].map((o) => JSON.stringify(o))
}

let root: string
let env: NodeJS.ProcessEnv
function setupCopy() {
  root = join(tmp, 'install')
  mkdirSync(join(root, 'store'), { recursive: true })
  cpSync(join(PROJECT_ROOT, 'scripts'), join(root, 'scripts'), { recursive: true })
  mkdirSync(join(tmp, 'home'), { recursive: true })
  env = { ...process.env, HOME: join(tmp, 'home'), CLAUDE_PROJECT_DIR: root, PYTHONDONTWRITEBYTECODE: '1' }
}

function breakFile(rel: string): () => void {
  const p = join(root, 'scripts', rel)
  const orig = readFileSync(p)
  appendFileSync(p, rel.endsWith('.py') ? '\nx = (\n' : '\nconst = ;\n')
  return () => writeFileSync(p, orig)
}

describe('the real security gates on a disposable copy', () => {
  beforeAll(setupCopy)

  it('the inputs include at least one denial per gate, so the equality below is not 0 == 0 only', () => {
    const denied = new Set<string>()
    for (const rel of NODE_GATES) {
      const p = join(root, 'scripts', rel)
      for (const input of inputs(root)) {
        const a = run('/bin/sh', `"${HOOK_NODE_BIN}" "${p}"`, input, env, root)
        if (a.rc === 2 || /"deny"/.test(a.stdout)) denied.add(rel)
      }
    }
    expect([...denied].sort()).toEqual([...NODE_GATES].sort())
  }, 60_000)

  for (const rel of NODE_GATES) {
    it(`${rel}: intact, the wrapped decisions equal the raw ones on every input`, () => {
      const p = join(root, 'scripts', rel)
      for (const input of inputs(root)) {
        const a = run('/bin/sh', `"${HOOK_NODE_BIN}" "${p}"`, input, env, root)
        const b = run('/bin/sh', hookCommand(p), input, env, root)
        expect(a.rc === 0 || a.rc === 2, `${rel} raw rc ${a.rc}: ${a.stderr.slice(0, 300)}`).toBe(true)
        expect({ rc: b.rc, stdout: b.stdout }).toEqual({ rc: a.rc, stdout: a.stdout })
        expect(b.stderr).not.toContain(GATE_FAIL_CLOSED_TAG)
      }
    }, 60_000)

    it(`${rel}: a syntax error in the gate file blocks (rc 2), where the raw call let it through`, () => {
      const restore = breakFile(rel)
      try {
        const p = join(root, 'scripts', rel)
        const input = inputs(root)[0]
        const a = run('/bin/sh', `"${HOOK_NODE_BIN}" "${p}"`, input, env, root)
        expect(a.rc).toBe(1)
        const b = run('/bin/sh', hookCommand(p), input, env, root)
        expect(b.rc).toBe(2)
        expect(b.stderr).toContain(GATE_FAIL_CLOSED_TAG)
      } finally {
        restore()
      }
    }, 60_000)
  }
})

describe('the boot prune knows the wrapper', () => {
  it('FAIL_CLOSED_MARK (hook-registration-guard) is the scaffold\'s GATE_FAIL_CLOSED_TAG', async () => {
    const { FAIL_CLOSED_MARK } = await import('../web/hook-registration-guard.js')
    expect(FAIL_CLOSED_MARK).toBe(GATE_FAIL_CLOSED_TAG)
  })
})

// BELA 6223 (b): the fail-closed message names the way out that needs no Bash. The claim is pinned: the main
// agent's tracked project settings put no gate on Read / Edit / Write (MultiEdit, NotebookEdit).
describe('the fail-closed message names the Bash-free way out', () => {
  it('the tail carries the sentence', () => {
    expect(failClosedGateRun('x', 'g.mjs', 10)).toContain('Bash nelkul javithato: a fo agens Read/Edit/Write eszkoze nincs kapuzva.')
  })
  it('...and it is true for the main agent: no gate script on a Read / Edit / Write matcher in its project settings', () => {
    const settings = JSON.parse(readFileSync(join(PROJECT_ROOT, '.claude', 'settings.json'), 'utf-8'))
    const gates = /email-send-gate|self-pace-gate|kanban-write-gate|egress-gate|outgoing-copy-gate/
    for (const e of (settings.hooks?.PreToolUse ?? []) as { matcher: string; hooks: { command: string }[] }[]) {
      const re = new RegExp(`^(?:${e.matcher})$`)
      const hitsFileTool = ['Read', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit'].some((t) => re.test(t))
      if (hitsFileTool) for (const h of e.hooks) expect(gates.test(h.command), `${e.matcher}: ${h.command.slice(0, 80)}`).toBe(false)
    }
  })
})

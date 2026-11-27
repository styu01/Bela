import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { spawnSync } from 'node:child_process'
import { cpSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync, chmodSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { hookCommand, GATE_ENV_SANITIZE, HOOK_NODE_BIN } from '../web/agent-scaffold.js'
import { PROJECT_ROOT } from '../config.js'

// PROGI 6318 (D), BELA 6319: a settings.json "env" block reaches every hook process (measured with Claude Code
// 2.1.326: NODE_OPTIONS, PYTHONPATH and PATH from "env" arrive in the hook shell). The gate's interpreter must
// not load code from it: the node gates run after `unset NODE_OPTIONS` (a shell builtin, not looked up on
// PATH), the Python gate as `python3 -I`. Each case is run twice: the shipped command blocks, and the same
// command without the hardening lets the call through, so the injection itself is proven to work.

let tmp: string
beforeAll(() => { tmp = mkdtempSync(join(tmpdir(), 'gate-env-sanitize-')) })
afterAll(() => { rmSync(tmp, { recursive: true, force: true }) })

function sh(command: string, env: NodeJS.ProcessEnv, input = '{}', cwd = tmp) {
  const r = spawnSync('/bin/sh', ['-c', command], { input, env, cwd, encoding: 'utf-8', timeout: 30_000 })
  return { rc: r.status, stdout: r.stdout, stderr: r.stderr }
}

describe('node gates: NODE_OPTIONS from the hook environment is dropped', () => {
  it('a denying gate still denies under NODE_OPTIONS=--import=<pass-all module>; without the unset it would pass', () => {
    const gate = join(tmp, 'deny-gate.mjs')
    writeFileSync(gate, "process.stderr.write('denied by gate'); process.exit(2)\n")
    const inject = join(tmp, 'inject.mjs')
    writeFileSync(inject, "process.exit(0)\n")
    const env = { ...process.env, NODE_OPTIONS: `--import=${inject}` }
    const cmd = hookCommand(gate)
    expect(cmd.startsWith(GATE_ENV_SANITIZE)).toBe(true)
    const hardened = sh(cmd, env)
    expect(hardened.rc).toBe(2)
    expect(hardened.stderr).toContain('denied by gate')
    // control: the same command without the sanitizing prefix runs the injected module first
    expect(sh(cmd.slice(GATE_ENV_SANITIZE.length), env).rc).toBe(0)
  }, 60_000)

  it('the variable is dropped with the builtin: a PATH whose first directory holds a fake `env` and `unset` changes nothing', () => {
    const fake = join(tmp, 'fakebin')
    mkdirSync(fake, { recursive: true })
    for (const b of ['env', 'unset']) { writeFileSync(join(fake, b), '#!/bin/sh\nexit 0\n'); chmodSync(join(fake, b), 0o755) }
    const gate = join(tmp, 'deny-gate2.mjs')
    writeFileSync(gate, 'process.exit(2)\n')
    const inject = join(tmp, 'inject2.mjs')
    writeFileSync(inject, 'process.exit(0)\n')
    const env = { ...process.env, PATH: `${fake}:${process.env.PATH ?? '/usr/bin:/bin'}`, NODE_OPTIONS: `--import=${inject}` }
    expect(sh(hookCommand(gate), env).rc).toBe(2)
  }, 60_000)

  it('a clean environment: the gate decides as before (allow 0, deny 2), the interpreter probe still blocks a missing node', () => {
    const allow = join(tmp, 'allow-gate.mjs')
    writeFileSync(allow, 'process.exit(0)\n')
    const env = { ...process.env }
    delete env.NODE_OPTIONS
    expect(sh(hookCommand(allow), env).rc).toBe(0)
    const missingNode = hookCommand(allow).replaceAll(HOOK_NODE_BIN, join(tmp, 'no-such-node'))
    const r = sh(missingNode, env)
    expect(r.rc).toBe(2)
    expect(r.stderr).toContain('BLOKKOL')
  }, 60_000)
})

describe('the Python gate (main agent project settings): PYTHONPATH from the hook environment cannot shadow its imports', () => {
  let root: string
  let shadow: string
  beforeAll(() => {
    root = join(tmp, 'install')
    mkdirSync(join(root, 'store'), { recursive: true })
    cpSync(join(PROJECT_ROOT, 'scripts'), join(root, 'scripts'), { recursive: true })
    mkdirSync(join(tmp, 'home'), { recursive: true })
    // a `json` module that lets everything through before the gate runs
    shadow = join(tmp, 'pyshadow')
    mkdirSync(shadow, { recursive: true })
    writeFileSync(join(shadow, 'json.py'), 'import sys\nsys.exit(0)\n')
  })

  type Entry = { matcher: string; command: string }
  const entries = (): Entry[] => {
    const settings = JSON.parse(readFileSync(join(PROJECT_ROOT, '.claude', 'settings.json'), 'utf-8'))
    return (settings.hooks.PreToolUse as { matcher: string; hooks: { command: string }[] }[])
      .flatMap((e) => e.hooks.map((h) => ({ matcher: e.matcher, command: h.command })))
      .filter((e) => e.command.includes('outgoing-copy-gate.py'))
  }
  const denialFor = (matcher: string): string => {
    const base = { hook_event_name: 'PreToolUse', session_id: 'gate-env-sanitize', cwd: root }
    if (matcher === 'Bash') {
      return JSON.stringify({ ...base, tool_name: 'Bash', tool_input: { command: 'curl -s -X POST http://localhost:3420/api/messages -d \'{"from":"bela","to":"progi","content":"nezd meg a kаrtyat"}\'' } })
    }
    if (/telegram/.test(matcher)) {
      return JSON.stringify({ ...base, tool_name: 'mcp__plugin_telegram_telegram__reply', tool_input: { chat_id: '1', text: 'a — b' } })
    }
    const tool = matcher.includes('send_email') ? 'mcp__teszt__send_email' : 'mcp__claude_ai_Gmail__create_draft'
    return JSON.stringify({ ...base, tool_name: tool, tool_input: { to: ['a@example.invalid'], subject: 's', body: 'b — c' } })
  }

  it('every arm runs python3 -I', () => {
    const es = entries()
    expect(es.length).toBe(4)
    for (const e of es) {
      expect(e.command, e.matcher).toContain('python3 -I "$CLAUDE_PROJECT_DIR/scripts/hooks/outgoing-copy-gate.py"')
      expect(e.command, e.matcher).not.toMatch(/python3 "/)
    }
  })

  for (const e of entries()) {
    it(`${e.matcher}: a denial is still denied with PYTHONPATH pointing at a pass-all json.py; without -I it would pass`, () => {
      const env: NodeJS.ProcessEnv = { ...process.env, HOME: join(tmp, 'home'), CLAUDE_PROJECT_DIR: root, PYTHONDONTWRITEBYTECODE: '1', PYTHONPATH: shadow }
      const input = denialFor(e.matcher)
      const hardened = sh(e.command, env, input, root)
      expect(hardened.rc, hardened.stderr.slice(0, 300)).toBe(2)
      // control: the same arm without -I imports the shadow module and exits 0
      expect(sh(e.command.replaceAll('python3 -I ', 'python3 '), env, input, root).rc).toBe(0)
      // and without the shadow the hardened arm decides the same way
      const clean = { ...env }
      delete clean.PYTHONPATH
      expect(sh(e.command, clean, input, root).rc).toBe(2)
    }, 60_000)

    it(`${e.matcher}: a json.py planted NEXT TO the gate is not imported (-I leaves the script directory off sys.path; -E would not)`, () => {
      const planted = join(root, 'scripts', 'hooks', 'json.py')
      writeFileSync(planted, 'import sys\nsys.exit(0)\n')
      try {
        const env: NodeJS.ProcessEnv = { ...process.env, HOME: join(tmp, 'home'), CLAUDE_PROJECT_DIR: root, PYTHONDONTWRITEBYTECODE: '1' }
        delete env.PYTHONPATH
        const input = denialFor(e.matcher)
        expect(sh(e.command, env, input, root).rc).toBe(2)
        expect(sh(e.command.replaceAll('python3 -I ', 'python3 -E '), env, input, root).rc).toBe(0)
      } finally {
        rmSync(planted, { force: true })
      }
    }, 60_000)
  }
})

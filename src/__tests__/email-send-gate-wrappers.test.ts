import { describe, it, expect } from 'vitest'
import { spawnSync } from 'node:child_process'
import { join } from 'node:path'

// WRAPGATE1126: the sub-agent hard send gate looked at the first token of a
// segment only, so a wrapper ('env sendmail x', 'sudo ...', 'nohup ...',
// 'timeout 5 ...', 'stdbuf -oL ...', 'eval ...') or a command-position keyword
// ('if x; then sendmail x; fi') let a sub-agent send past it. Measured
// 2026-11-26 with this very hook, run the way Claude Code runs it: the bare
// 'sendmail a@b.hu' was denied, 'env sendmail a@b.hu' was allowed.
// The per-command contract lives in send-invocation-cases.json (both twins);
// this file pins the END-TO-END hook decision, deny and allow.
const HOOK = join(__dirname, '..', '..', 'scripts', 'email-send-gate.mjs')

function decide(command: string): 'deny' | 'allow' {
  const r = spawnSync('node', [HOOK], {
    input: JSON.stringify({ tool_name: 'Bash', tool_input: { command }, hook_event_name: 'PreToolUse' }),
    encoding: 'utf-8',
  })
  expect(r.status).toBe(0)
  return r.stdout.includes('"permissionDecision":"deny"') ? 'deny' : 'allow'
}

describe('email-send-gate hook: a wrapper or keyword in front of the sender is still refused', () => {
  const DENY = [
    'sendmail a@b.hu',
    'env sendmail a@b.hu',
    'env A=1 sendmail a@b.hu',
    'sudo sendmail a@b.hu',
    'nohup sendmail a@b.hu',
    'timeout 5 sendmail a@b.hu',
    'timeout 2 -- sendmail a@b.hu',
    'exec sendmail a@b.hu',
    'command sendmail a@b.hu',
    'nice -n 5 sendmail a@b.hu',
    'time -p sendmail a@b.hu',
    'stdbuf -oL sendmail a@b.hu',
    'setsid sendmail a@b.hu',
    'xargs sendmail < list',
    "eval 'sendmail a@b.hu'",
    'builtin eval sendmail a@b.hu',
    'if true; then sendmail a@b.hu; fi',
    'env python3 /x/send.py --to a@b.hu --body x',
  ]
  for (const cmd of DENY) it(`deny: ${cmd}`, () => expect(decide(cmd)).toBe('deny'))

  const ALLOW = ['ls -la', 'sudo ls', 'env', 'timeout 5 ls', 'command -v sendmail', 'eval echo sendmail',
    'nice -n 5 grep sendmail log', 'git status']
  for (const cmd of ALLOW) it(`allow: ${cmd}`, () => expect(decide(cmd)).toBe('allow'))
})

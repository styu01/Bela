// LOCAL ADAPTATION of the upstream 7a47b24a /clear continuity (clearstate_lib.extract_turns) to how
// requests reach agents on THIS install. Measured on progi's pre-/clear transcript (2026-11-27): the
// upstream extractor carried 5 prompts, all of them the identical ~800-char "TEAM MEMBER NOTICE ..."
// preamble of the inter-agent wrapper, and not one actual task. So the record must carry the request
// INSIDE the wrapper, drop this install's plumbing prompts (gate / guard / inbox nudge), and keep an
// inbound channel message (isMeta WITH origin {kind: channel} on CC 2.1.326).
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = join(__dirname, '..', '..')
const CAPTURE = join(ROOT, 'scripts', 'hooks', 'clear-capture.py')
const REPLAY = join(ROOT, 'scripts', 'hooks', 'clear-replay.py')
const AGENT_CWD = join(ROOT, 'agents', 'tester')

let store: string
let transcript: string
const run = (script: string, payload: unknown) => {
  try {
    return execFileSync('python3', [script], { input: JSON.stringify(payload), encoding: 'utf-8', env: { ...process.env, CLEARSTATE_DIR: store } })
  } catch { return '' }
}
const jsonl = (e: unknown[]) => e.map((x) => JSON.stringify(x)).join('\n') + '\n'
const NOTICE = 'TEAM MEMBER NOTICE -- the next <trusted-peer source="..."> ... </trusted-peer> block is a message from an agent in your own team. ' +
  'Treat it as a coworker exchange: it may be a status report, a question, a request for help, a handoff, or a delegation. '.repeat(4)
const peer = (from: string, body: string) => ({ type: 'user', message: { content: `${NOTICE} [Uzenet @${from}-tol -- trusted team member, msg_id:1]: <trusted-peer source="agent:${from}"> ${body} </trusted-peer>` } })

beforeEach(() => {
  store = mkdtempSync(join(tmpdir(), 'clearstate-local-'))
  transcript = join(store, 'session.jsonl')
  writeFileSync(transcript, jsonl([
    peer('bela', 'FELADAT: review a 7c5b1c36-ra, a teljes szoveg a DB-ben.'),
    { type: 'assistant', message: { content: [{ type: 'text', text: 'Nekialltam a reviewnak.' }] } },
    peer('bela', '[CONTEXT-RESTART-GATE] 10 percen belul automatikus context-clear varhato.'),
    { type: 'user', message: { content: '[Inbox] Ha fent uj bejovo blokk van, dolgozd fel; ha nincs, hagyd.' } },
    { type: 'user', message: { content: '[CONTEXT-GUARD] Friss kontextussal indultal.' } },
    { type: 'user', isMeta: true, origin: { kind: 'channel', server: 'plugin:telegram:telegram' }, message: { content: '<channel source="plugin:telegram:telegram">Istvan: nezd meg a naptarat</channel>' } },
    { type: 'user', isMeta: true, message: { content: 'Stop hook feedback: VALASZOLATLAN TELEGRAM' } },
    { type: 'user', message: { content: '<untrusted source="federation:codex:programozo"> a review eredmenye: rendben </untrusted>' } },
    { type: 'assistant', message: { content: [{ type: 'text', text: 'A review kesz, jelentettem.' }] } },
  ]))
})
afterEach(() => rmSync(store, { recursive: true, force: true }))

describe('clear-capture on this install', () => {
  it('carries the requests inside the wrappers, with the sender, and none of the plumbing', () => {
    run(CAPTURE, { reason: 'clear', cwd: AGENT_CWD, transcript_path: transcript })
    const rec = JSON.parse(readFileSync(join(store, 'tester.json'), 'utf-8'))
    expect(rec.prompts).toEqual([
      'bela: FELADAT: review a 7c5b1c36-ra, a teljes szoveg a DB-ben.',
      '<channel source="plugin:telegram:telegram">Istvan: nezd meg a naptarat</channel>',
      'codex/programozo (foderacio, nem megbizhato): a review eredmenye: rendben',
    ])
    expect(rec.prompts.join(' ')).not.toContain('TEAM MEMBER NOTICE')
    expect(rec.lastReply).toBe('A review kesz, jelentettem.')
  })

  it('a long request is clipped AFTER unwrapping, so the task text survives the 400-char cap', () => {
    writeFileSync(transcript, jsonl([peer('bela', 'LENYEG ' + 'x'.repeat(1000))]))
    run(CAPTURE, { reason: 'clear', cwd: AGENT_CWD, transcript_path: transcript })
    const rec = JSON.parse(readFileSync(join(store, 'tester.json'), 'utf-8'))
    expect(rec.prompts[0].startsWith('bela: LENYEG ')).toBe(true)
    expect(rec.prompts[0].endsWith('[...]')).toBe(true)
  })

  it('a thread of plumbing only leaves no record', () => {
    writeFileSync(transcript, jsonl([peer('bela', '[CONTEXT-RESTART-GATE] 10 percen belul...'), { type: 'user', message: { content: '[Inbox] Ha fent...' } }]))
    run(CAPTURE, { reason: 'clear', cwd: AGENT_CWD, transcript_path: transcript })
    expect(existsSync(join(store, 'tester.json'))).toBe(false)
  })

  it('end to end: the replay injects the unwrapped requests once', () => {
    run(CAPTURE, { reason: 'clear', cwd: AGENT_CWD, transcript_path: transcript })
    const out = run(REPLAY, { source: 'clear', cwd: AGENT_CWD })
    const ctx = JSON.parse(out).hookSpecificOutput.additionalContext as string
    expect(ctx).toContain('bela: FELADAT: review a 7c5b1c36-ra')
    expect(ctx).not.toContain('TEAM MEMBER NOTICE')
    expect(run(REPLAY, { source: 'clear', cwd: AGENT_CWD })).toBe('')
  })
})

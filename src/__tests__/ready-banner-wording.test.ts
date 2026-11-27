// upstream 21fc3eac, ported: Claude Code 2.1.296 renamed the channels ready banner from
// "Listening for channel messages from: <x>" to "Listening for messages from <x>". The installed
// 2.1.326 binary carries ONLY the new wording (measured 2026-11-27: 0 hits for the old text, 2 for
// the new). On this install both readiness checks still matched the old text, so the channels.sh
// startup guard never left early (it always ran its full window, and with the 90 s guard of
// aca174dd that would have been 90 s on every boot), and the agent-process EPERM watcher never
// stopped before its 14 s deadline.
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const CHANNELS = readFileSync(join(ROOT, 'scripts', 'channels.sh'), 'utf-8')
const AGENT_PROCESS = readFileSync(join(ROOT, 'src', 'web', 'agent-process.ts'), 'utf-8')

const NEW = 'Listening for messages from plugin:telegram:telegram'
const OLD = 'Listening for channel messages from: plugin:telegram@claude-plugins-official'

describe('the channels ready banner, old and new wording', () => {
  it('channels.sh: the startup guard breaks on both wordings', () => {
    const loopStart = CHANNELS.indexOf('while [ "$i" -lt "$_boot_guard_s" ]; do')
    const loop = CHANNELS.slice(loopStart, CHANNELS.indexOf('\ndone\n', loopStart))
    const branch = loop.match(/^\s*(\*"Listening[^\n]*)\)\s*\n\s*break/m)
    expect(branch).not.toBeNull()
    // The case patterns, as bash would match them: *"text"* is "contains text".
    const needles = [...branch![1].matchAll(/\*"([^"]+)"\*/g)].map((m) => m[1])
    expect(needles.some((n) => NEW.includes(n))).toBe(true)
    expect(needles.some((n) => OLD.includes(n))).toBe(true)
  })

  it('agent-process.ts: the EPERM watcher stops on both wordings', () => {
    const m = AGENT_PROCESS.match(/if \(\/(Listening for [^/]+)\/\.test\(pane\)\) return/)
    expect(m).not.toBeNull()
    const rx = new RegExp(m![1])
    expect(rx.test(NEW)).toBe(true)
    expect(rx.test(OLD)).toBe(true)
  })
})

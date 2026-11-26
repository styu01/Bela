import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// Upstream a8477d1f: the optional session-feedback survey ("How is Claude doing
// this session? 1: Bad 2: Fine 3: Good 0: Dismiss") waits on a keypress, and an
// unattended agent never gets one, so the session stops taking turns while the
// dashboard, the router and the scheduler all look healthy. Every place an agent
// can come up must disable it, otherwise a respawn silently brings it back.
// Verified on Claude Code 2.1.326 (2026-11-26): the survey gate reads
// `if (env.CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY) return false`.
const ROOT = join(__dirname, '..', '..')
const read = (rel: string) => readFileSync(join(ROOT, rel), 'utf-8')
const SET = /CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1\b/

describe('the session-feedback survey is disabled at every agent launch point', () => {
  it('sub-agent spawn (agent-process.ts): next to the prompt-suggestion disable on the launch command', () => {
    const src = read('src/web/agent-process.ts')
    const m = src.match(/const promptSuggestionEnv =\s*'([^']*)'/)
    expect(m).not.toBeNull()
    expect(m![1]).toMatch(SET)
    expect(src).toMatch(/\$\{promptSuggestionEnv\}/)
  })

  it('main channels session (channels.sh): exported, inline on the launch env, and in the tmux global env', () => {
    const sh = read('scripts/channels.sh')
    expect(sh).toMatch(/^export CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1$/m)
    const batch = sh.match(/^MCP_BATCH_ENV="([^"]*)/m)
    expect(batch).not.toBeNull()
    expect(batch![1]).toMatch(SET)
    expect(sh).toMatch(/set-environment -g CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY 1/)
  })

  it('keepalive respawn (channel-watchdog.sh): on the respawn command', () => {
    const wd = read('scripts/channel-watchdog.sh')
    const m = wd.match(/^RESPAWN_CMD="(.*)"$/m)
    expect(m).not.toBeNull()
    expect(m![1]).toMatch(SET)
  })
})

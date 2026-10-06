import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const repo = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const source = readFileSync(join(repo, 'scripts', 'channels.sh'), 'utf-8')
const launches = source.split('\n').filter((line) => /^\s*(env -u "\$STATE_ENV_VAR" )?\$TMUX new-session -d -s "\$SESSION"/.test(line))

describe('channels tmux state-dir isolation wiring', () => {
  it('keeps both auth-env launch expansions while clearing state-dir inheritance', () => {
    expect(launches).toHaveLength(2)
    for (const launch of launches) {
      expect(launch).toContain('${TMUX_AUTH_ENV[@]+"${TMUX_AUTH_ENV[@]}"}')
      expect(launch.trim().startsWith('env -u "$STATE_ENV_VAR" $TMUX new-session')).toBe(true)
    }
    expect(source).toContain('$TMUX set-environment -g -u "$STATE_ENV_VAR"')
  })

  it('retains remain-on-exit for both the normal and /tmp fallback launch', () => {
    const first = source.indexOf('env -u "$STATE_ENV_VAR" $TMUX new-session')
    const second = source.indexOf('env -u "$STATE_ENV_VAR" $TMUX new-session', first + 1)
    expect(first).toBeGreaterThan(-1)
    expect(second).toBeGreaterThan(first)
    expect(source.indexOf('$TMUX set-option -t "$SESSION" remain-on-exit on', first)).toBeGreaterThan(first)
    expect(source.indexOf('$TMUX set-option -t "$SESSION" remain-on-exit on', second)).toBeGreaterThan(second)
  })
})

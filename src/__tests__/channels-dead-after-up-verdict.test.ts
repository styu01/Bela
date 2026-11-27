import { describe, it, expect } from 'vitest'
import { execFileSync } from 'node:child_process'
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

// RESTARTCOALESCE1103 / RELOADDEADGRACE1040: the died-after-up watchdog tick in
// scripts/channels.sh. A plugin that disappears after having been up is
// (a) left alone while store/.channel-last-respawn is within the settle budget
// (the restart that wrote the stamp is still settling), (b) given one
// /reload-plugins after PLUGIN_RELOAD_AFTER, and (c) restarted after
// PLUGIN_DEAD_GRACE. These tests run the SHIPPED verdict function and the
// SHIPPED constants, so a changed budget or a reordered branch fails here.

const ROOT = join(__dirname, '..', '..')
const CHANNELS = readFileSync(join(ROOT, 'scripts', 'channels.sh'), 'utf-8')

function sliceShellFn(src: string, name: string): string {
  const start = src.indexOf(`${name}() {`)
  if (start < 0) throw new Error(`function ${name}() not found`)
  const end = src.indexOf('\n}', start)
  if (end < 0) throw new Error(`unterminated ${name}()`)
  return src.slice(start, end + 2)
}

function constant(name: string): number {
  const m = CHANNELS.match(new RegExp(`^${name}=(\\d+)$`, 'm'))
  if (!m) throw new Error(`${name} not found in channels.sh`)
  return parseInt(m[1], 10)
}

const BUDGET = constant('RESPAWN_SETTLE_BUDGET')
const GRACE = constant('PLUGIN_DEAD_GRACE')
const RELOAD_AFTER = constant('PLUGIN_RELOAD_AFTER')

type Tick = { now: number; deadSince: number; stamp: string; tried: boolean }

/** Run the shipped dead_after_up_verdict over a list of ticks, one bash run. */
function verdicts(ticks: Tick[]): string[] {
  const dir = mkdtempSync(join(tmpdir(), 'deadafterup-'))
  try {
    const p = join(dir, 'probe.sh')
    const body = [
      `RESPAWN_SETTLE_BUDGET=${BUDGET}`,
      `PLUGIN_DEAD_GRACE=${GRACE}`,
      `PLUGIN_RELOAD_AFTER=${RELOAD_AFTER}`,
      sliceShellFn(CHANNELS, 'dead_after_up_verdict'),
      ...ticks.map((t) => `dead_after_up_verdict ${t.now} ${t.deadSince} '${t.stamp}' ${t.tried}`),
    ].join('\n')
    writeFileSync(p, body + '\n')
    return execFileSync('bash', [p], { encoding: 'utf-8' }).trim().split('\n')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

const one = (t: Tick) => verdicts([t])[0]

describe('channels.sh dead_after_up_verdict (RESTARTCOALESCE1103)', () => {
  const T0 = 1_800_000_000

  it('the shipped constants keep the reload inside the grace and the budget at least the grace', () => {
    expect(RELOAD_AFTER).toBeLessThan(GRACE)
    expect(BUDGET).toBeGreaterThanOrEqual(GRACE)
  })

  it('stamp INSIDE the budget -> no restart, even when dead well past the grace', () => {
    const stamp = String(T0)
    const now = T0 + BUDGET
    expect(one({ now, deadSince: now - GRACE - 60, stamp, tried: true })).toBe('suppress')
  })

  it('NEGATIVE CONTROL: the same death with the stamp OUTSIDE the budget -> restart', () => {
    const stamp = String(T0)
    const now = T0 + BUDGET + 1
    expect(one({ now, deadSince: now - GRACE - 60, stamp, tried: true })).toBe('exit')
  })

  it('the budget is measured from the FIXED stamp, not from the death time: it cannot slide', () => {
    // Death right at the stamp, ticks every 5s across the budget edge: the
    // verdict flips exactly once, at stamp + BUDGET + 1, and never flips back.
    const stamp = String(T0)
    const ticks: Tick[] = []
    for (let now = T0; now <= T0 + BUDGET + 30; now += 5) ticks.push({ now, deadSince: T0, stamp, tried: true })
    ticks.push({ now: T0 + BUDGET + 1, deadSince: T0, stamp, tried: true })
    const out = verdicts(ticks)
    const edge = out.pop()
    expect(edge).toBe('exit')
    const firstExit = out.indexOf('exit')
    expect(firstExit).toBeGreaterThan(0)
    expect(out.slice(0, firstExit).every((v) => v === 'suppress')).toBe(true)
    expect(out.slice(firstExit).every((v) => v === 'exit')).toBe(true)
  })

  it('no stamp, or a malformed one, explains nothing', () => {
    const now = T0
    for (const stamp of ['', '0', 'abc', '12x']) {
      expect(one({ now, deadSince: now - GRACE, stamp, tried: false })).toBe('exit')
    }
  })

  it('outside the budget: wait, then ONE reload, then wait, then restart at the grace', () => {
    const stamp = String(T0 - BUDGET - 1000)
    const since = T0
    expect(one({ now: since + RELOAD_AFTER - 1, deadSince: since, stamp, tried: false })).toBe('wait')
    expect(one({ now: since + RELOAD_AFTER, deadSince: since, stamp, tried: false })).toBe('reload')
    expect(one({ now: since + RELOAD_AFTER + 10, deadSince: since, stamp, tried: true })).toBe('wait')
    expect(one({ now: since + GRACE, deadSince: since, stamp, tried: true })).toBe('exit')
    // The grace wins over a not-yet-tried reload: no reload past the grace.
    expect(one({ now: since + GRACE, deadSince: since, stamp, tried: false })).toBe('exit')
  })

  it('the watchdog loop branches on the verdict function, not on a private copy of the arithmetic', () => {
    expect(CHANNELS).toContain('_dead_verdict="$(dead_after_up_verdict "$NOW" "$PLUGIN_DEAD_SINCE" "$_respawn_stamp" "$PLUGIN_RELOAD_ATTEMPTED")"')
    for (const v of ['suppress', 'exit', 'reload']) {
      expect(CHANNELS).toContain(`[ "$_dead_verdict" = "${v}" ]`)
    }
  })
})

// LOCAL additions (this install's port of 058ad090, channels.sh part only).
describe('channels.sh died-after-up branch wiring (local)', () => {
  const loopStart = CHANNELS.indexOf('while $TMUX has-session -t "=$SESSION:"')
  const loop = CHANNELS.slice(loopStart, CHANNELS.indexOf('\ndone\n', loopStart))
  const TYPE_RELOAD = ['$TMUX', 'send' + '-keys', '-t', '"=$SESSION:"', "'/reload-plugins'", 'Enter'].join(' ')

  it('the /reload-plugins is typed only after the pane is probed idle, and only once per dead spell', () => {
    expect(loopStart).toBeGreaterThan(0)
    const branch = loop.slice(loop.indexOf('[ "$_dead_verdict" = "reload" ]'))
    const probe = branch.indexOf('probe_pane_input_state)"')
    const idle = branch.indexOf('if [ "$_reload_probe" = "idle" ]; then')
    const typed = branch.indexOf(TYPE_RELOAD)
    const mark = branch.indexOf('PLUGIN_RELOAD_ATTEMPTED=true')
    expect(probe).toBeGreaterThan(0)
    expect(idle).toBeGreaterThan(probe)
    expect(typed).toBeGreaterThan(idle)
    expect(mark).toBeGreaterThan(typed)
  })

  it('a plugin seen alive again re-arms the reload and the suppression log', () => {
    const alive = loop.slice(loop.indexOf('if [ "$_plugin_alive" = "true" ]; then'), loop.indexOf('elif [ "$PLUGIN_SEEN_ONCE" = "true" ]; then'))
    expect(alive).toContain('PLUGIN_RELOAD_ATTEMPTED=false')
    expect(alive).toContain('PLUGIN_SUPPRESS_LOGGED=false')
  })

  it('the suppression is logged once per dead spell, not on every 5 s tick', () => {
    const branch = loop.slice(loop.indexOf('[ "$_dead_verdict" = "suppress" ]'), loop.indexOf('[ "$_dead_verdict" = "exit" ]'))
    expect(branch).toContain('[ "$PLUGIN_SUPPRESS_LOGGED" = "true" ] || respawn_log "died-after-up-suppressed:')
    expect(branch).toContain('PLUGIN_SUPPRESS_LOGGED=true')
  })

  it('the exit still requests the service-manager restart (RESTART_REQUESTED=1, break)', () => {
    const branch = loop.slice(loop.indexOf('[ "$_dead_verdict" = "exit" ]'), loop.indexOf('[ "$_dead_verdict" = "reload" ]'))
    expect(branch).toContain('RESTART_REQUESTED=1')
    expect(branch).toContain('break')
  })
})

// upstream 2b961fcd / 2c093a10 (#2415, #2419), ported: a second instance (worktree checkout, temp-dir clone)
// runs no background services, as if WEB_ONLY were set, unless the process environment opts in; it never
// seeds the user-global scheduled tasks and never runs the orphan background-task sweep. The upstream
// incident: 2026-11-01 15:23 a worktree instance without WEB_ONLY fired two of the live fleet's scheduled
// tasks into live sessions. Here every review / test checkout (/home/kisss/marveen-upstream-*,
// /home/kisss/marveen-progi-review-*) is such a linked worktree.
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { shouldRunBackgroundServices, shouldRegisterHooks, ALLOW_SECOND_INSTANCE_SERVICES_KEY } from '../web/hook-registration-guard.js'

const notAGitFile = () => false

describe('shouldRunBackgroundServices', () => {
  it('the install itself runs them', () => {
    expect(shouldRunBackgroundServices({ projectRoot: '/home/x/marveen', webOnly: false, allowSecondInstance: false, isGitFile: notAGitFile, tmpDir: '/tmp' }))
      .toEqual({ run: true })
  })

  it('WEB_ONLY turns them off, the opt-in included', () => {
    for (const allowSecondInstance of [false, true]) {
      const d = shouldRunBackgroundServices({ projectRoot: '/home/x/marveen', webOnly: true, allowSecondInstance, isGitFile: notAGitFile })
      expect(d).toEqual({ run: false, reason: 'WEB_ONLY staging mode' })
    }
  })

  it('every second-instance kind is off without the opt-in', () => {
    const cases = [
      { projectRoot: '/home/x/marveen/.claude/worktrees/agent-1', isGitFile: notAGitFile }, // worktree path
      { projectRoot: '/home/x/marveen-upstream-w3t', isGitFile: () => true }, // linked worktree (.git is a file)
      { projectRoot: '/tmp/marveen-work', isGitFile: notAGitFile }, // temp root
      { projectRoot: '/var/tmp-own/clone', isGitFile: notAGitFile, tmpDir: '/var/tmp-own' }, // the OS tmpdir
    ]
    for (const c of cases) {
      const d = shouldRunBackgroundServices({ ...c, webOnly: false, allowSecondInstance: false })
      expect(d.run, c.projectRoot).toBe(false)
      expect(d.reason, c.projectRoot).toMatch(/worktree|temp dir/)
      expect(d.override, c.projectRoot).toBeUndefined()
    }
  })

  it('a second instance with the opt-in runs them, and says it is an override', () => {
    const d = shouldRunBackgroundServices({ projectRoot: '/home/x/marveen-upstream-w3t', webOnly: false, allowSecondInstance: true, isGitFile: () => true })
    expect(d).toEqual({ run: true, override: true, reason: expect.stringMatching(/worktree/) })
  })

  it('the hook decision (which the seed follows) says no for every second-instance kind and yes for the install', () => {
    expect(shouldRegisterHooks({ projectRoot: '/home/x/marveen/.claude/worktrees/wt1', webOnly: false, isGitFile: notAGitFile }).register).toBe(false)
    expect(shouldRegisterHooks({ projectRoot: '/home/x/marveen-upstream-w3t', webOnly: false, isGitFile: () => true }).register).toBe(false)
    expect(shouldRegisterHooks({ projectRoot: '/tmp/canary', webOnly: false, isGitFile: notAGitFile }).register).toBe(false)
    expect(shouldRegisterHooks({ projectRoot: '/home/x/marveen', webOnly: true, isGitFile: notAGitFile }).register).toBe(false)
    expect(shouldRegisterHooks({ projectRoot: '/home/x/marveen', webOnly: false, isGitFile: notAGitFile, tmpDir: '/tmp' }).register).toBe(true)
  })
})

describe('web.ts wiring (source pins)', () => {
  const src = readFileSync(join(__dirname, '..', 'web.ts'), 'utf-8').replace(/^\s*\/\/.*$/gm, '')
  const decision = src.indexOf('const services = shouldRunBackgroundServices(')
  const servicesOff = src.indexOf('const servicesOff = !services.run')
  const hookDecision = src.indexOf('const hookDecision = shouldRegisterHooks(')

  it('the decision reads the opt-in from the process environment, as WEB_ONLY, and comes before every service', () => {
    expect(ALLOW_SECOND_INSTANCE_SERVICES_KEY).toBe('MARVEEN_ALLOW_SECOND_INSTANCE_SERVICES')
    expect(decision).toBeGreaterThan(-1)
    expect(src.slice(decision, servicesOff)).toContain("allowSecondInstance: process.env[ALLOW_SECOND_INSTANCE_SERVICES_KEY] === '1'")
    expect(src).toContain("const webOnly = process.env['WEB_ONLY'] === 'true'")
    expect(src.slice(decision, servicesOff)).not.toContain('readEnvFile')
    expect(src.slice(decision, servicesOff)).toContain('webOnly')
    expect(servicesOff).toBeGreaterThan(decision)
    expect(src.indexOf('startMessageRouter()')).toBeGreaterThan(servicesOff)
  })

  it('the schedule runner, the router, the context guard / restart gate and the worker pre-start sit behind servicesOff', () => {
    expect(src).toContain('const scheduleInterval = servicesOff ? undefined : startScheduleRunner()')
    expect(src).toContain('const routerInterval = servicesOff ? undefined : startMessageRouter()')
    expect(src).toContain('const contextGuardInterval = servicesOff ? undefined : startContextGuardRunner()')
    expect(src.match(/startScheduleRunner\(\)/g)).toHaveLength(1)
    expect(src.match(/startMessageRouter\(\)/g)).toHaveLength(1)
    expect(src.match(/startContextRestartGateRunner\(\)/g)).toHaveLength(1)
    expect(src.lastIndexOf('if (!servicesOff) {', src.indexOf('startContextRestartGateRunner()'))).toBeGreaterThan(servicesOff)
    expect(src.match(/startWorkerSession\(\)/g)?.length ?? 0).toBeGreaterThan(0)
    for (const m of src.matchAll(/startWorkerSession\(\)/g)) {
      expect(src.lastIndexOf('if (!servicesOff', m.index)).toBeGreaterThan(servicesOff)
    }
  })

  it('no service between the decision and the hook decision is gated by webOnly alone', () => {
    const firstService = src.indexOf('const routerInterval =')
    expect(firstService).toBeGreaterThan(servicesOff)
    expect(hookDecision).toBeGreaterThan(firstService)
    expect(src.slice(firstService, hookDecision)).not.toMatch(/\bwebOnly\b/)
  })

  it('the default task seed sits in the hook decision\'s register branch, its only call site', () => {
    const guard = src.indexOf("  if (!hookDecision.register) {\n    logger.info({ reason: hookDecision.reason }, 'Default scheduled tasks not seeded (second instance)')\n  } else {")
    const call = src.indexOf('      ensureDefaultScheduledTasks()')
    expect(guard).toBeGreaterThan(hookDecision)
    expect(call).toBeGreaterThan(guard)
    expect(src.slice(guard, call).split('\n  }\n').length - 1).toBe(0)
    expect(src.split('ensureDefaultScheduledTasks()').length - 1).toBe(1)
  })

  it('the orphan background-task sweep (it kills tmux sessions past their limit) sits behind servicesOff', () => {
    const call = src.indexOf('      sweepOrphanedBackgroundTasks()')
    expect(call).toBeGreaterThan(servicesOff)
    const gate = '  if (!servicesOff) {\n    try {\n'
    expect(src.lastIndexOf(gate, call)).toBe(call - gate.length)
    expect(src.match(/sweepOrphanedBackgroundTasks\(\)/g)).toHaveLength(1)
  })
})

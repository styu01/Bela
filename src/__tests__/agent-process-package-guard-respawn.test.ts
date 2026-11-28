// This fork (KISPROGI 6464 H1): startAgentProcess asks the package guard at its top, then awaits (3 s after the kill)
// before the spawn; a package update can be applied in that window. The guard is asked again right before the spawn,
// with nothing awaited in between, and the EPERM /tmp fallback relaunch (up to 14 s later) asks the whole guard
// (the approval too) before it spawns. Source pins: the function needs a live tmux to run.
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const src = readFileSync(join(process.cwd(), 'src', 'web', 'agent-process.ts'), 'utf-8')
const body = src.slice(src.indexOf('export async function startAgentProcess('), src.indexOf('export async function stopAgentProcess('))

describe('startAgentProcess: the package guard at the spawn (KISPROGI 6464 H1)', () => {
  it('the guard helper is upstream refusals then the owner approval', () => {
    expect(src).toMatch(/function packageGuardRefusal\(name: string, dir: string\): string \| null \{\n  return packageStartRefusal\(name, dir\) \?\? packageApprovalRefusal\(name\)\n\}/)
  })
  it('asked at the top, before the remote path', () => {
    const top = body.indexOf('const packageRefusal = packageStartRefusal(name, dir) ?? packageApprovalRefusal(name)')
    expect(top).toBeGreaterThan(0)
    expect(top).toBeLessThan(body.indexOf('startRemoteAgentProcess('))
  })
  it('asked again right before the main spawn, with no await between the two', () => {
    const spawn = body.indexOf("runTmux(null, ['new-session', '-d', '-s', session, buildLaunchCmd(dir)]")
    const late = body.lastIndexOf('const lateRefusal = packageGuardRefusal(name, dir)', spawn)
    expect(spawn).toBeGreaterThan(0)
    expect(late).toBeGreaterThan(body.indexOf('await delay(3000)'))
    const between = body.slice(late, spawn)
    expect(between).not.toMatch(/\bawait\b/)
    expect(between).toContain("return { ok: false, error: lateRefusal }")
  })
  it('the EPERM fallback relaunch asks the whole guard before its spawn', () => {
    const fb = body.indexOf("runTmux(null, ['new-session', '-d', '-s', session, buildLaunchCmd(fallbackCwd)]")
    const guard = body.lastIndexOf('const fallbackRefusal = packageGuardRefusal(name, dir)', fb)
    expect(guard).toBeGreaterThan(body.indexOf('epermRestarted = true'))
    expect(body.slice(guard, fb)).toMatch(/if \(fallbackRefusal !== null\) \{[\s\S]*?return\n/)
  })
})

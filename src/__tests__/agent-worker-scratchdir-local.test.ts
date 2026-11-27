import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// Upstream ded815c1, ported: the request's scratchDir is (re)created before each request, so a worker
// home wiped while the tmux session stayed up cannot make the .done sentinel unwritable (the request
// would be lost to a timeout). The upstream suite covers the /clear wait only; this pins the mkdir.
const SRC = readFileSync(join(__dirname, '..', 'web', 'agent-worker.ts'), 'utf-8')

describe('runWorkerAttempt recreates the scratchDir before the request', () => {
  it('mkdirSync(ctx.scratchDir, recursive) comes after the paths are built and before the send', () => {
    const fn = SRC.slice(SRC.indexOf('async function runWorkerAttempt('))
    const done = fn.indexOf('const donePath = join(ctx.scratchDir')
    const mk = fn.indexOf('mkdirSync(ctx.scratchDir, { recursive: true })')
    const send = fn.indexOf('await clearWorkerContext(ctx)')
    expect(done).toBeGreaterThan(0)
    expect(mk).toBeGreaterThan(done)
    expect(send).toBeGreaterThan(mk)
  })
})

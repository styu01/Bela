// MIOAGENTINSTALL1114 plan 6.4 (upstream 3f30ff34, ported; BELA 6404): the agent package device token is one per
// machine and never carried, so scripts/backup.sh never archives store/agent-package-credential.json nor its
// interrupted-write tmp files. This fork's backup.sh lists store/ files with one find denylist; the test runs that
// exact find expression, cut from the script, on a stand-in store.
import { describe, it, expect, afterAll } from 'vitest'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const SH = readFileSync(join(process.cwd(), 'scripts', 'backup.sh'), 'utf-8')
const start = SH.indexOf('  find store -mindepth 1 -maxdepth 1')
const end = SH.indexOf('-print >> "${REPOLIST}"', start)
const FIND = SH.slice(start, end) + '-print'
const root = mkdtempSync(join(tmpdir(), 'backup-cred-'))
afterAll(() => rmSync(root, { recursive: true, force: true }))

describe('backup.sh store/ file list', () => {
  it('the find expression was found in the script', () => {
    expect(start).toBeGreaterThan(-1)
    expect(end).toBeGreaterThan(start)
  })

  it('leaves out the device credential and its tmp leftovers, keeps an ordinary state file', () => {
    mkdirSync(join(root, 'store'))
    for (const f of ['agent-package-credential.json', 'agent-package-credential.json.123.456.ab12.tmp', 'vault-bindings.json']) {
      writeFileSync(join(root, 'store', f), 'x')
    }
    const r = spawnSync('bash', ['-c', FIND], { cwd: root, encoding: 'utf-8' })
    expect(r.status).toBe(0)
    const listed = r.stdout.trim().split('\n').sort()
    expect(listed).toEqual(['store/vault-bindings.json'])
  })
})

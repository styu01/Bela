// agent-package/approval.ts (BELA 6438/6444): a package agent starts only when an approval names exactly the package
// in its folder. The server-side twin of watchdog-package-guard.test.ts: the same fail-closed cases on the TS side.
import { describe, it, expect, vi, afterAll, beforeEach } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, symlinkSync, renameSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

const root = mkdtempSync(join(tmpdir(), 'pkg-approval-'))
vi.mock('../config.js', async (orig) => ({ ...(await orig<object>()), PROJECT_ROOT: root, STORE_DIR: join(root, 'store') }))
const A = await import('../web/agent-package/approval.js')
const { PACKAGE_SHELL_PREFIX_TEXT } = await import('../web/agent-package/marker.js')
afterAll(() => rmSync(root, { recursive: true, force: true }))
const SHA = 'a'.repeat(64)
const APPR = join(root, 'store', 'agent-package-approved')
const NOW = new Date('2026-11-28T00:00:00Z')

function pkgAgent(name: string, version = '1.0.0', sha = SHA): string {
  const d = join(root, 'agents', name)
  mkdirSync(join(d, '.package'), { recursive: true })
  writeFileSync(join(d, 'CLAUDE.md'), PACKAGE_SHELL_PREFIX_TEXT + '\nrest\n')
  writeFileSync(join(d, 'PACKAGE.md'), 'persona\n')
  writeFileSync(join(d, '.package', 'manifest.json'), JSON.stringify({ schema: 1, payloadSha256: sha, kid: 'k', installedAt: 'x', updatedAt: 'x', payload: { slug: name, version } }))
  return d
}
beforeEach(() => { rmSync(join(root, 'agents'), { recursive: true, force: true }); rmSync(join(root, 'store'), { recursive: true, force: true }) })

describe('packageApprovalState / packageApprovalRefusal', () => {
  it('a hand-made agent is not a package agent and may start', () => {
    mkdirSync(join(root, 'agents', 'hand'), { recursive: true })
    writeFileSync(join(root, 'agents', 'hand', 'CLAUDE.md'), '# hand\n')
    expect(A.packageApprovalState('hand').state).toBe('not_package')
    expect(A.packageApprovalRefusal('hand')).toBeNull()
  })

  it('approvePackage writes a 0600 record and the agent may start', () => {
    pkgAgent('p1')
    const r = A.approvePackage('p1', 'owner', NOW)
    expect(r.ok).toBe(true)
    expect(A.packageApprovalRefusal('p1')).toBeNull()
    expect(JSON.parse(readFileSync(join(APPR, 'p1.json'), 'utf8'))).toMatchObject({ schema: 1, name: 'p1', slug: 'p1', version: '1.0.0', payloadSha256: SHA, approvedBy: 'owner', approvedAt: NOW.toISOString() })
  })

  it('a new payload under the same version needs a new approval', () => {
    pkgAgent('p2'); A.approvePackage('p2', 'owner', NOW)
    pkgAgent('p2', '1.0.0', 'b'.repeat(64))
    expect(A.packageApprovalRefusal('p2')).toBe(A.REFUSAL_PACKAGE_NOT_APPROVED)
    expect(A.pendingPackageAgents()).toEqual(['p2'])
  })

  it('an approval file reached through a link does not count', () => {
    pkgAgent('p3'); A.approvePackage('p3', 'owner', NOW)
    renameSync(join(APPR, 'p3.json'), join(APPR, 'real.json'))
    symlinkSync(join(APPR, 'real.json'), join(APPR, 'p3.json'))
    expect(A.packageApprovalRefusal('p3')).toBe(A.REFUSAL_PACKAGE_NOT_APPROVED)
  })

  it('a malformed approval or one for another name does not count', () => {
    pkgAgent('p4'); mkdirSync(APPR, { recursive: true })
    writeFileSync(join(APPR, 'p4.json'), '{not json')
    expect(A.packageApprovalRefusal('p4')).toBe(A.REFUSAL_PACKAGE_NOT_APPROVED)
    writeFileSync(join(APPR, 'p4.json'), JSON.stringify({ schema: 1, name: 'other', slug: 'p4', version: '1.0.0', payloadSha256: SHA, approvedAt: 't', approvedBy: 'o' }))
    expect(A.packageApprovalRefusal('p4')).toBe(A.REFUSAL_PACKAGE_NOT_APPROVED)
  })

  it('a package agent whose record is unreadable never starts and cannot be approved', () => {
    const d = pkgAgent('p5')
    writeFileSync(join(d, '.package', 'manifest.json'), '{broken')
    expect(A.packageApprovalState('p5').state).toBe('unreadable_record')
    expect(A.packageApprovalRefusal('p5')).toBe(A.REFUSAL_PACKAGE_NOT_APPROVED)
    expect(A.approvePackage('p5', 'owner', NOW)).toMatchObject({ ok: false, status: 409, error: 'package_record_unreadable' })
  })
})

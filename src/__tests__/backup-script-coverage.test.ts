import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync, cpSync, chmodSync, statSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { spawnSync } from 'node:child_process'

// BACKUP904 (2026-09-08): scripts/backup.sh's store/ coverage was a 5-name
// whitelist (only claudeclaw.db+wal/shm, .dashboard-token, config-overrides)
// -- every other state/credential file in store/ (vault.json, per-service
// credentials, autonomy/context-guard/context-restart-gate state, federation
// config, kitchen state, ...) was silently missing from every archive, and
// the file-based memory system (~/.claude/projects/<encoded-path>/memory)
// was never covered at all. The manifest also never verified the archive it
// wrote actually contained what it claimed.
//
// 2026-09-08 Codex review round 2: an explicit name-list is inherently
// incomplete (it already missed real files -- vault-bindings.json,
// .github-fleet-token, .gdocs-oauth.json, etc. -- the first time it was
// written), so store/ coverage switched to a DENYLIST (everything except a
// few explicitly-known-wrong things). The verify step also switched from a
// file-COUNT comparison (same count, different contents would pass
// undetected) to an exact relative-PATH-SET comparison, and umask 077 +
// explicit chmod now protect the now-much-more-credential-heavy archive.
//
// These tests run the REAL script end to end against a fully isolated
// sandbox (fake REPO_ROOT + fake HOME), not just a source-pattern check, so
// a regression in the actual bash logic (not just the presence of a string)
// fails here.
const REPO_ROOT = join(__dirname, '..', '..')
const REAL_BACKUP_SCRIPT = join(REPO_ROOT, 'scripts', 'backup.sh')

let SANDBOX = ''
let FAKE_REPO = ''
let FAKE_HOME = ''

function runBackup(): ReturnType<typeof spawnSync> {
  return spawnSync('bash', [join(FAKE_REPO, 'scripts', 'backup.sh')], {
    env: { ...process.env, HOME: FAKE_HOME },
    encoding: 'utf-8',
  })
}

function latestArchivePath(): string {
  const backupsDir = join(FAKE_REPO, 'backups')
  const archives = readdirSync(backupsDir).filter((f) => f.endsWith('.tar.gz'))
  expect(archives.length).toBeGreaterThan(0)
  return join(backupsDir, archives[archives.length - 1])
}

function extractedManifest(): string {
  const archive = latestArchivePath()
  const out = join(SANDBOX, 'extract')
  mkdirSync(out, { recursive: true })
  spawnSync('tar', ['-xzf', archive, '-C', out])
  return readFileSync(join(out, 'MANIFEST.txt'), 'utf-8')
}

beforeEach(() => {
  SANDBOX = mkdtempSync(join(tmpdir(), 'backupsh-'))
  FAKE_REPO = join(SANDBOX, 'repo')
  FAKE_HOME = join(SANDBOX, 'home')
  mkdirSync(join(FAKE_REPO, 'scripts'), { recursive: true })
  mkdirSync(join(FAKE_REPO, 'store'), { recursive: true })
  mkdirSync(FAKE_HOME, { recursive: true })
  cpSync(REAL_BACKUP_SCRIPT, join(FAKE_REPO, 'scripts', 'backup.sh'))
  chmodSync(join(FAKE_REPO, 'scripts', 'backup.sh'), 0o755)
})
afterEach(() => { rmSync(SANDBOX, { recursive: true, force: true }) })

describe('scripts/backup.sh: store/ coverage is a DENYLIST, not a whitelist', () => {
  it('picks up known state/credential files', () => {
    writeFileSync(join(FAKE_REPO, 'store', 'vault.json'), '{}')
    writeFileSync(join(FAKE_REPO, 'store', 'autonomy-config.json'), '{}')
    writeFileSync(join(FAKE_REPO, 'store', 'context-guard.json'), '{}')
    writeFileSync(join(FAKE_REPO, 'store', 'federation.json'), '{}')
    writeFileSync(join(FAKE_REPO, 'store', '.agent-testagent-last-respawn'), '123')

    const res = runBackup()
    expect(res.status).toBe(0)

    const manifest = extractedManifest()
    expect(manifest).toContain('repo/store/vault.json')
    expect(manifest).toContain('repo/store/autonomy-config.json')
    expect(manifest).toContain('repo/store/context-guard.json')
    expect(manifest).toContain('repo/store/federation.json')
    expect(manifest).toContain('repo/store/.agent-testagent-last-respawn')
  })

  it('also picks up a state file that was NEVER explicitly named anywhere -- proves this is a real denylist, not a whitelist with extra entries', () => {
    // This exact filename does not appear in backup.sh, in this test file, or
    // in any prior review comment -- if this passes, the mechanism generalizes.
    writeFileSync(join(FAKE_REPO, 'store', 'some-future-feature-state-nobody-named-yet.json'), '{}')
    const res = runBackup()
    expect(res.status).toBe(0)
    expect(extractedManifest()).toContain('repo/store/some-future-feature-state-nobody-named-yet.json')
  })

  it('excludes rotating logs, PID/lock files, .bak snapshots, and pane-capture debug dumps', () => {
    writeFileSync(join(FAKE_REPO, 'store', 'channels-debug.log'), 'noise')
    writeFileSync(join(FAKE_REPO, 'store', 'channels-debug.log.1'), 'noise')
    writeFileSync(join(FAKE_REPO, 'store', 'dashboard.pid'), '12345')
    writeFileSync(join(FAKE_REPO, 'store', 'usage-statusline-latest.json.lock'), '')
    writeFileSync(join(FAKE_REPO, 'store', 'context-guard.json.bak-pre-fix-20260101'), '{}')
    writeFileSync(join(FAKE_REPO, 'store', 'context-guard-last-pane-testagent.txt'), 'debug dump')
    writeFileSync(join(FAKE_REPO, 'store', 'usage-history.jsonl'), '{}')
    // A real state file too, so we know the run actually processed store/.
    writeFileSync(join(FAKE_REPO, 'store', 'vault.json'), '{}')

    const res = runBackup()
    expect(res.status).toBe(0)
    const manifest = extractedManifest()
    expect(manifest).toContain('repo/store/vault.json')
    expect(manifest).not.toContain('channels-debug.log')
    expect(manifest).not.toContain('dashboard.pid')
    expect(manifest).not.toContain('.lock')
    expect(manifest).not.toContain('.bak-pre-fix')
    expect(manifest).not.toContain('context-guard-last-pane')
    expect(manifest).not.toContain('usage-history.jsonl')
  })

  it('excludes store/projects, store/reference-docs, and store/backups entirely (deliberate size/scope exclusion)', () => {
    // A guaranteed-included file too, so the run actually has something to
    // archive (an all-excluded store/ would otherwise hit the "nothing to
    // archive" early exit and produce no archive to inspect at all).
    writeFileSync(join(FAKE_REPO, 'store', 'vault.json'), '{}')
    mkdirSync(join(FAKE_REPO, 'store', 'projects', 'some-client'), { recursive: true })
    writeFileSync(join(FAKE_REPO, 'store', 'projects', 'some-client', 'contract.pdf'), 'binary-ish content')
    mkdirSync(join(FAKE_REPO, 'store', 'reference-docs'), { recursive: true })
    writeFileSync(join(FAKE_REPO, 'store', 'reference-docs', 'big.pdf'), 'binary-ish content')
    mkdirSync(join(FAKE_REPO, 'store', 'backups'), { recursive: true })
    writeFileSync(join(FAKE_REPO, 'store', 'backups', 'old-snapshot.tar.gz'), 'not a real tar')

    const res = runBackup()
    expect(res.status).toBe(0)
    const manifest = extractedManifest()
    expect(manifest).not.toContain('projects/')
    expect(manifest).not.toContain('reference-docs/')
    expect(manifest).not.toContain('store/backups')
  })

  it('the DB files are still handled exactly once (via the explicit WAL-checkpoint path, not double-listed by the denylist sweep)', () => {
    writeFileSync(join(FAKE_REPO, 'store', 'claudeclaw.db'), 'fake-db-content')
    const res = runBackup()
    expect(res.status).toBe(0)
    const manifest = extractedManifest()
    const occurrences = manifest.split('repo/store/claudeclaw.db\n').length - 1
    expect(occurrences).toBe(1)
  })
})

// 2026-09-08 Codex review round 2b: directories at store/ top-level use the
// OPPOSITE default from files. Files/symlinks are a denylist (auto-included
// unless excluded); directories are an ALLOWLIST (nothing swept in unless
// explicitly named) -- an unknown/future directory (a model cache, a venv, a
// browser profile, a generated export dump) must never be pulled in by
// default just because it happens to exist at store/ top-level one day.
describe('scripts/backup.sh: store/ directories are ALLOWLIST-only, not swept in by default', () => {
  it('backs up store/agent-taskstate/ (the one explicitly allowlisted directory)', () => {
    mkdirSync(join(FAKE_REPO, 'store', 'agent-taskstate'), { recursive: true })
    writeFileSync(join(FAKE_REPO, 'store', 'agent-taskstate', 'testagent.json'), '{"summary":"in progress"}')

    const res = runBackup()
    expect(res.status).toBe(0)
    // The manifest lists directories as a single top-level line (same
    // convention as .claude/skills etc. elsewhere in this file) -- the
    // nested file's actual presence in the ARCHIVE is what matters, checked
    // directly against the extracted tree, not the manifest text.
    const manifest = extractedManifest()
    expect(manifest).toContain('repo/store/agent-taskstate')
    const extractedFile = join(SANDBOX, 'extract', 'repo', 'store', 'agent-taskstate', 'testagent.json')
    expect(readFileSync(extractedFile, 'utf-8')).toContain('in progress')
  })

  it('does NOT back up an arbitrary, never-named directory at store/ top-level -- proves this is a real allowlist, not a widened denylist', () => {
    // A guaranteed-included file too, so the run has something to archive.
    writeFileSync(join(FAKE_REPO, 'store', 'vault.json'), '{}')
    // This directory name appears nowhere in backup.sh, in any prior review
    // comment, or in this test file's other cases -- if it's excluded, the
    // allowlist mechanism genuinely works, not just for the 3 names Codex
    // happened to call out.
    mkdirSync(join(FAKE_REPO, 'store', 'some-future-model-cache-nobody-allowlisted'), { recursive: true })
    writeFileSync(join(FAKE_REPO, 'store', 'some-future-model-cache-nobody-allowlisted', 'weights.bin'), 'x'.repeat(1000))

    const res = runBackup()
    expect(res.status).toBe(0)
    const manifest = extractedManifest()
    expect(manifest).toContain('repo/store/vault.json')
    expect(manifest).not.toContain('some-future-model-cache-nobody-allowlisted')
  })
})

describe('scripts/backup.sh: file-based memory backup', () => {
  it('backs up the file-based memory directory, deriving its name from REPO_ROOT', () => {
    const encoded = FAKE_REPO.replaceAll('/', '-')
    const memDir = join(FAKE_HOME, '.claude', 'projects', encoded, 'memory')
    mkdirSync(memDir, { recursive: true })
    writeFileSync(join(memDir, 'warm_example.md'), '---\nname: warm_example\n---\nsomething worth remembering')

    const res = runBackup()
    expect(res.status).toBe(0)
    expect(extractedManifest()).toContain(`home/.claude/projects/${encoded}/memory`)
  })
})

describe('scripts/backup.sh: self-verify (exact path-set, not just a count)', () => {
  it('succeeds on a clean run and reports the verified count', () => {
    writeFileSync(join(FAKE_REPO, 'store', 'vault.json'), '{}')
    const res = runBackup()
    expect(res.status).toBe(0)
    expect(res.stdout).toMatch(/backup: verified -- archive contains exactly the \d+ staged file\(s\)\/symlink\(s\)/)
  })
})

describe('scripts/backup.sh: output permissions', () => {
  it('the backups directory is 0700 and the archive is 0600, even under a permissive umask', () => {
    writeFileSync(join(FAKE_REPO, 'store', 'vault.json'), '{}')
    const res = spawnSync('bash', ['-c', `umask 022; exec bash '${join(FAKE_REPO, 'scripts', 'backup.sh')}'`], {
      env: { ...process.env, HOME: FAKE_HOME },
      encoding: 'utf-8',
    })
    expect(res.status).toBe(0)
    const backupsDir = join(FAKE_REPO, 'backups')
    expect(statSync(backupsDir).mode & 0o777).toBe(0o700)
    expect(statSync(latestArchivePath()).mode & 0o777).toBe(0o600)
  })
})

// The failure path (a genuinely truncated/corrupted write, or a written
// archive whose contents don't match what was staged) is not simulated
// behaviorally here -- that would mean racing or corrupting `tar` itself
// mid-write, which is its own hazard to script reliably. Pinning the SHAPE
// of the verify-and-refuse-to-prune logic in the source instead, mirroring
// this repo's existing convention for hard-to-simulate failure paths (see
// update-checker-branch.test.ts's stale-ref retry test).
describe('scripts/backup.sh: verify-failure path (source-pattern, see comment above)', () => {
  it('a failed archive listing is fatal on its own, not swallowed into a misleading count', () => {
    const src = readFileSync(REAL_BACKUP_SCRIPT, 'utf-8')
    expect(src).toMatch(/if ! tar -tzf "\$\{ARCHIVE\}" > "\$\{ACTUAL_LIST\}" 2>\/dev\/null; then/)
  })

  it('exits non-zero and does not reach the prune step when the archived path set does not exactly match staged', () => {
    const src = readFileSync(REAL_BACKUP_SCRIPT, 'utf-8')
    const verifyStart = src.indexOf('ACTUAL_LIST="$(mktemp')
    expect(verifyStart).toBeGreaterThan(0)
    const pruneStart = src.indexOf('# Keep the newest')
    expect(pruneStart).toBeGreaterThan(verifyStart)
    const verifyBlock = src.slice(verifyStart, pruneStart)
    expect(verifyBlock).toMatch(/diff -q "\$\{EXPECTED_LIST\}" "\$\{ACTUAL_LIST\}\.sorted"/)
    expect(verifyBlock).toMatch(/exit 1/)
    // NOT pruning on failure is the whole point -- a bad new archive must
    // never cost a good old one.
    expect(verifyBlock).toMatch(/NOT pruning old archives/)
  })
})

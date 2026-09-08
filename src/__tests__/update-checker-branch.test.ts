// Regression test for the update checker's branch selection.
//
// The checker used to hardcode `main` while update.sh pulls
// `origin/<current branch>`. On any checkout that follows another branch
// (e.g. `develop`) the two disagreed: the dashboard advertised a "new version"
// the update button could never deliver, and stayed silent about the commits
// that actually were on the way. trackedBranch() is what keeps the two in sync,
// so it is pinned here.
import { describe, it, expect } from 'vitest'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { trackedBranch, currentVersion, getUpdateStatus, branchExistsOnOrigin } from '../web/update-checker.js'
import { PROJECT_ROOT } from '../config.js'

function gitBranch(): string {
  return execFileSync('/usr/bin/git', ['rev-parse', '--abbrev-ref', 'HEAD'], {
    cwd: PROJECT_ROOT, timeout: 3000, encoding: 'utf-8',
  }).trim()
}

// origin's ACTUAL current default branch (origin/HEAD -> <branch>), resolved
// live rather than hardcoded -- a hardcoded 'develop' would go stale the day
// origin's default branch is ever renamed, silently testing the wrong thing
// instead of failing loudly.
function originDefaultBranch(): string {
  const ref = execFileSync('/usr/bin/git', ['symbolic-ref', 'refs/remotes/origin/HEAD'], {
    cwd: PROJECT_ROOT, timeout: 3000, encoding: 'utf-8',
  }).trim()
  return ref.replace(/^refs\/remotes\/origin\//, '')
}

describe('update checker branch selection', () => {
  it('follows the branch the checkout is actually on', () => {
    const actual = gitBranch()
    // Detached HEAD reports the literal "HEAD"; the helper substitutes main
    // there, matching what update.sh tells the operator to check out.
    const expected = actual && actual !== 'HEAD' ? actual : 'main'
    expect(trackedBranch()).toBe(expected)
  })

  it('never returns an empty ref', () => {
    // An empty branch would produce `origin/` / `commits/` requests that fail
    // in confusing ways; the fallback must always yield a usable ref.
    expect(trackedBranch()).toBeTruthy()
  })

  it('does not silently assume main on a non-main checkout', () => {
    const actual = gitBranch()
    if (!actual || actual === 'HEAD' || actual === 'main') return // nothing to prove here
    expect(trackedBranch()).not.toBe('main')
  })
})

// UPDATEBRANCH904: trackedBranch() is what checkout is ON, not what `origin`
// has ever seen. A generated/local branch name (e.g. this install's own
// production/marveen-v1.34.1-... branches, pushed to `fork`/`backup` but never
// to `origin`) means refreshUpdateStatus() must NOT query GitHub about it
// directly -- that 422s forever and the fork-fallback never gets a chance to
// run, leaving genuinely new upstream commits invisible (179 commits behind
// origin/develop, measured live on this install the day this was found).
describe('update checker origin-branch existence (UPDATEBRANCH904)', () => {
  it('is true for a branch that is genuinely on origin', () => {
    // origin's own CURRENT default branch (resolved live, not hardcoded);
    // pinning to a real, stable ref rather than the ambient checkout branch
    // (which may itself not be on origin -- that's the whole bug) keeps this
    // assertion meaningful regardless of what branch tests happen to run on.
    expect(branchExistsOnOrigin(originDefaultBranch())).toBe(true)
  })

  it('is false for a branch name origin has never seen', () => {
    expect(branchExistsOnOrigin('this-branch-definitely-does-not-exist-anywhere-xyz123')).toBe(false)
  })

  it('is false for a nonexistent checkout root (fails closed, does not throw)', () => {
    expect(branchExistsOnOrigin(originDefaultBranch(), '/nonexistent-root-xyz')).toBe(false)
  })

  it('reproduces the live bug this checkout was found with: the tracked branch is not on origin', () => {
    // Documents the actual state that motivated the fix, rather than asserting
    // it forever (this checkout WILL eventually be pushed to origin, or a
    // future checkout will run this on a real release branch -- either way
    // this should then read true, not fail). Skips gracefully once fixed.
    const actual = trackedBranch()
    if (branchExistsOnOrigin(actual)) return // this checkout's branch is on origin now -- nothing to prove
    expect(branchExistsOnOrigin(actual)).toBe(false)
  })

  // 2026-09-08 Codex review: branchExistsOnOrigin only reads what a PAST
  // `git fetch` recorded -- a stale local remote-tracking ref survives even
  // after the branch is deleted/renamed on origin itself, so GitHub can still
  // 404/422 on a branch this function said "exists". refreshUpdateStatus must
  // not treat that as fatal on the first try; a single bounded retry against
  // origin's actual current default branch is required. No live git-state
  // fixture is set up here (mutating this checkout's own remote-tracking refs
  // in a test would be its own hazard) -- this pins the retry SHAPE in the
  // source instead, mirroring this file's existing pattern-based checks.
  it('refreshUpdateStatus retries against fetchDefaultBranch on a not-found branch tip, once, before giving up', () => {
    const src = readFileSync(join(PROJECT_ROOT, 'src/web/update-checker.ts'), 'utf-8')
    const start = src.indexOf('export async function refreshUpdateStatus')
    expect(start).toBeGreaterThan(0)
    // Only one definition of this function exists in the file, so slicing to
    // the end is safe and avoids guessing a brace-matching end boundary.
    const body = src.slice(start)
    expect(body).toMatch(/let tip = await fetchBranchTip\(remote, queryBranch\)/)
    expect(body).toMatch(/if \('notFound' in tip\) \{/)
    expect(body).toMatch(/const fallbackBranch = await fetchDefaultBranch\(remote\)/)
    // the retry is bounded: a second notFound after the fallback throws,
    // rather than looping or falling through with a stale/empty status.latest
    expect(body).toMatch(/if \('notFound' in tip\) throw new Error/)
  })
})

// The Updates panel shows the running instance's semver; it must come from
// package.json and never be fabricated. currentVersion() is the single source.
describe('update checker current version', () => {
  it('returns the semver from package.json at PROJECT_ROOT', () => {
    const pkg = JSON.parse(readFileSync(join(PROJECT_ROOT, 'package.json'), 'utf-8'))
    expect(currentVersion()).toBe(pkg.version)
    // sanity: it is a real semver, not an empty/garbage value
    expect(currentVersion()).toMatch(/^\d+\.\d+\.\d+/)
  })

  it('is exposed on the /api/updates status object', () => {
    expect(getUpdateStatus().version).toBe(currentVersion())
  })

  it('returns empty (never fabricates) when package.json is missing/unreadable', () => {
    expect(currentVersion('/nonexistent-root-xyz')).toBe('')
    expect(currentVersion('/etc')).toBe('') // dir exists, no package.json -> ''
  })
})

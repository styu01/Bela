import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { PROJECT_ROOT } from '../config.js'
import { TOOL_TIMEOUTS } from '../tool-timeouts.js'

export interface UpdateCommit {
  sha: string
  short: string
  message: string
  author: string
  date: string
}

export interface UpdateRelease {
  /** Release tag, e.g. "v1.20.0"; empty string for the not-yet-released group. */
  version: string
  /** Human-language summary for the version (release-commit subject after "--",
   * or the release-commit body when present). Empty when none is available. */
  summary: string
  commits: UpdateCommit[]
}

export interface UpdateStatus {
  current: string
  /** Semver of the running instance (package.json "version"), e.g. "1.32.1".
   * Resolved live per request. Empty/absent when package.json is missing,
   * unreadable, or malformed -- the UI then shows the SHA alone and NEVER a
   * fabricated version. */
  version?: string
  latest: string
  behind: number
  commits: UpdateCommit[]
  /** Commits grouped by release tag (newest first; the first group is the
   * not-yet-released "upcoming" commits with version=""). Derived from the
   * chore(release) commits in the list. Absent/empty when there is nothing to
   * group; the flat `commits` list is always populated for backward compat. */
  releases?: UpdateRelease[]
  remote: string
  lastChecked: number
  /** Branch this checkout follows (what update.sh pulls). The frontend warns
   * when it is not `main`: customer installs that landed on develop via a
   * branchless clone keep receiving unreleased code until switched back. */
  branch?: string
  error?: string
  /** True when this checkout doesn't map cleanly onto `origin` -- either the
   * local HEAD itself isn't a commit on the GitHub remote (a customised fork
   * carrying local commits), OR the checkout's own branch NAME isn't known to
   * `origin` at all (UPDATEBRANCH904: this install's generated production/*
   * branches, pushed only to `fork`/`backup`) and a stand-in ref (origin's
   * default branch) was queried instead. `behind`/`commits` are then computed
   * against the best available upstream reference rather than the literal
   * tracked branch. */
  fork?: boolean
}

let updateStatusCache: UpdateStatus = {
  current: '',
  latest: '',
  behind: 0,
  commits: [],
  remote: 'Szotasz/marveen',
  lastChecked: 0,
}

export function getUpdateStatus(): UpdateStatus {
  // branch and version are resolved live (cheap local rev-parse / file read):
  // the cache may predate the first refresh cycle, and a checkout switch or
  // in-place version bump should be visible immediately.
  return { ...updateStatusCache, branch: trackedBranch(), version: currentVersion() }
}

// Semver of the running instance, read from package.json at PROJECT_ROOT. Returns
// '' on ANY failure (missing / unreadable / malformed / no string "version"):
// the caller must never display a fabricated version, so the field is simply
// empty and the UI falls back to the commit SHA alone.
export function currentVersion(root: string = PROJECT_ROOT): string {
  try {
    const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf-8'))
    return typeof pkg?.version === 'string' ? pkg.version : ''
  } catch {
    return ''
  }
}

export function currentGitHead(): string {
  try {
    return execFileSync('/usr/bin/git', ['rev-parse', 'HEAD'], { cwd: PROJECT_ROOT, timeout: 3000, encoding: 'utf-8' }).trim()
  } catch {
    return ''
  }
}

// Branch this checkout actually follows. update.sh pulls `origin/<this>`, so
// the update check must compare against the same ref -- hardcoding `main` made
// every non-release checkout (e.g. `develop`) report a phantom "new version"
// that the update button could never deliver, while staying silent about the
// commits that WERE coming. Falls back to `main` on a detached HEAD, which is
// also the branch update.sh tells the operator to check out in that state.
export function trackedBranch(): string {
  try {
    const b = execFileSync('/usr/bin/git', ['rev-parse', '--abbrev-ref', 'HEAD'], { cwd: PROJECT_ROOT, timeout: 3000, encoding: 'utf-8' }).trim()
    return b && b !== 'HEAD' ? b : 'main'
  } catch {
    return 'main'
  }
}

export function parseGitHubRemote(): string {
  try {
    const url = execFileSync('/usr/bin/git', ['config', '--get', 'remote.origin.url'], { cwd: PROJECT_ROOT, timeout: 3000, encoding: 'utf-8' }).trim()
    // Normalize "git@github.com:Owner/Repo.git" or "https://github.com/Owner/Repo.git" to "Owner/Repo"
    const m = url.match(/github\.com[:/]([^/]+\/[^/]+?)(?:\.git)?$/i)
    if (m) return m[1]
  } catch { /* fall through */ }
  return 'Szotasz/marveen'
}

// Does `branch` exist as a remote-tracking ref for `origin`? Answered from
// local refs (kept current by `git fetch`), no network call needed.
//
// UPDATEBRANCH904 (2026-09-08): trackedBranch() returns whatever branch the
// checkout is ON, not what origin has ever seen. This install's own generated
// checkout name (production/marveen-v1.34.1-...) is pushed to the `fork`/
// `backup` remotes but was NEVER pushed to `origin` -- querying GitHub's
// /commits/<branch> endpoint about a name it doesn't recognize 422s, and that
// throw happens BEFORE the fork-fallback logic below ever runs (it only fires
// once status.latest has already resolved), so the whole check gets stuck on
// a permanent error while real upstream commits pile up invisibly (179
// commits behind origin/develop, measured live on this install the day this
// was found). The caller must check this FIRST and resolve to a real ref on
// `origin` instead of guessing.
export function branchExistsOnOrigin(branch: string, root: string = PROJECT_ROOT): boolean {
  try {
    execFileSync('/usr/bin/git', ['show-ref', '--verify', '--quiet', `refs/remotes/origin/${branch}`], { cwd: root, timeout: 3000 })
    return true
  } catch {
    return false
  }
}

// origin's own default branch (main/develop/whatever it's configured to),
// used only as a fallback when trackedBranch() isn't itself known to origin
// (branchExistsOnOrigin above) -- a local/generated branch name means nothing
// to GitHub, so there is nothing else sensible to query.
async function fetchDefaultBranch(remote: string): Promise<string> {
  const res = await fetch(`https://api.github.com/repos/${remote}`, { headers: GH_HEADERS, signal: AbortSignal.timeout(TOOL_TIMEOUTS['github']) })
  if (!res.ok) throw new Error(`GitHub /repos/${remote} -> ${res.status}`)
  const json = await res.json() as { default_branch?: unknown }
  const branch = typeof json.default_branch === 'string' ? json.default_branch.trim() : ''
  if (!branch) throw new Error(`No usable default_branch on /repos/${remote} response`)
  return branch
}

// The commits-endpoint lookup for a single branch, distinguishing "branch
// unknown to GitHub" (404/422) from every other failure. Kept separate from
// the direct throw-on-!ok shape refreshUpdateStatus used to have so the
// caller can retry with a different branch instead of treating a 404/422 as
// fatal.
async function fetchBranchTip(remote: string, branch: string): Promise<{ sha: string } | { notFound: true }> {
  const res = await fetch(`https://api.github.com/repos/${remote}/commits/${encodeURIComponent(branch)}`, {
    headers: { 'Accept': 'application/vnd.github+json', 'User-Agent': 'marveen-update-check' },
    signal: AbortSignal.timeout(TOOL_TIMEOUTS['github']),
  })
  if (res.status === 404 || res.status === 422) return { notFound: true }
  if (!res.ok) throw new Error(`GitHub /commits/${branch} -> ${res.status}`)
  const json = await res.json() as { sha?: unknown }
  if (typeof json.sha !== 'string' || !json.sha) throw new Error(`No sha on commits/${branch} response`)
  return { sha: json.sha }
}

type GhCompare = {
  ahead_by?: number
  commits?: { sha: string; commit: { message: string; author: { name: string; date: string } } }[]
}

const GH_HEADERS = { 'Accept': 'application/vnd.github+json', 'User-Agent': 'marveen-update-check' }

// Fetch the GitHub compare of base...head. Returns the parsed body, the
// sentinel { notFound: true } on a 404 (base or head not on the remote), or
// null on any other failure.
async function fetchCompare(remote: string, base: string, head: string): Promise<GhCompare | { notFound: true } | null> {
  const res = await fetch(`https://api.github.com/repos/${remote}/compare/${base}...${head}`, { headers: GH_HEADERS, signal: AbortSignal.timeout(TOOL_TIMEOUTS['github']) })
  if (res.ok) return await res.json() as GhCompare
  if (res.status === 404) return { notFound: true }
  return null
}

// Matches a `chore(release): vX.Y.Z` subject and captures the version + the
// human summary that follows a "--" / "—" separator (if any).
const RELEASE_RE = /^chore\(release\):\s*(v\d+\.\d+\.\d+)\s*(?:--|—)?\s*(.*)$/

// Strip trailing git trailers (Co-Authored-By, Signed-off-by) and blank lines
// from a release-commit body so only the human summary remains.
function releaseBodySummary(fullMessage: string): string {
  const lines = fullMessage.split('\n').slice(1) // drop the subject line
  const kept: string[] = []
  for (const line of lines) {
    if (/^(Co-Authored-By|Signed-off-by|Co-authored-by):/i.test(line.trim())) continue
    kept.push(line)
  }
  return kept.join('\n').trim()
}

// Map a GitHub compare body onto the status: the flat newest-first commit list
// (backward compat) plus a release-grouped view derived from the chore(release)
// commits already present in the list.
function applyCompare(status: UpdateStatus, cmp: GhCompare): void {
  status.behind = cmp.ahead_by ?? 0
  // GitHub returns commits oldest-first; flip to newest-first for the UI.
  const raw = (cmp.commits ?? []).slice().reverse()
  const commits: UpdateCommit[] = raw.map(c => ({
    sha: c.sha,
    short: c.sha.slice(0, 7),
    message: (c.commit.message || '').split('\n')[0],
    author: c.commit.author?.name || '',
    date: c.commit.author?.date || '',
  }))
  status.commits = commits
  status.releases = groupByRelease(commits, raw.map(c => c.commit.message || ''))
}

// Group a newest-first commit list into release buckets. A `chore(release): vX`
// commit starts a version group; the non-release commits OLDER than it (until
// the next release marker) are the changes shipped in vX. Commits newer than
// the newest release marker form the leading "upcoming" group (version="").
export function groupByRelease(commits: UpdateCommit[], fullMessages: string[]): UpdateRelease[] {
  const groups: UpdateRelease[] = []
  let cur: UpdateRelease | null = null
  const upcoming: UpdateRelease = { version: '', summary: '', commits: [] }
  for (let i = 0; i < commits.length; i++) {
    const c = commits[i]
    const m = c.message.match(RELEASE_RE)
    if (m) {
      const subjectSummary = (m[2] || '').trim()
      const bodySummary = releaseBodySummary(fullMessages[i] || '')
      cur = { version: m[1], summary: bodySummary || subjectSummary, commits: [] }
      groups.push(cur)
    } else if (cur) {
      cur.commits.push(c)
    } else {
      upcoming.commits.push(c)
    }
  }
  const out: UpdateRelease[] = []
  if (upcoming.commits.length) out.push(upcoming)
  return out.concat(groups)
}

// Merge-base of local HEAD with `origin/<branch>`. Takes the ALREADY-RESOLVED
// query branch (the one refreshUpdateStatus actually queried GitHub about),
// not a fresh trackedBranch() call -- re-deriving it here would reproduce
// UPDATEBRANCH904 in this half of the check too: if the checkout's own branch
// name doesn't exist on origin, `origin/<that name>` doesn't exist as a local
// ref either, so `git merge-base` would fail the exact same way. For a
// customised fork this merge-base is the fork point -- an actual upstream
// commit -- so it can be compared on GitHub even though local HEAD itself
// never landed there. Empty string when there is no such local ref.
function upstreamMergeBase(branch: string): string {
  try {
    return execFileSync('/usr/bin/git', ['merge-base', 'HEAD', `origin/${branch}`], { cwd: PROJECT_ROOT, timeout: 3000, encoding: 'utf-8' }).trim()
  } catch {
    return ''
  }
}

export async function refreshUpdateStatus(): Promise<UpdateStatus> {
  const current = currentGitHead()
  const remote = parseGitHubRemote()
  const status: UpdateStatus = {
    current,
    latest: '',
    behind: 0,
    commits: [],
    remote,
    lastChecked: Date.now(),
  }
  if (!current) {
    status.error = 'Not a git checkout'
    updateStatusCache = status
    return status
  }
  try {
    // 1) find HEAD of the branch this checkout follows via the commits endpoint.
    // UPDATEBRANCH904: if the checkout's own branch was never pushed to
    // `origin` (only to `fork`/`backup` -- this install's generated
    // production/* branches), querying origin about it 422s and the
    // fork-fallback below never gets a chance to run (it only fires once
    // status.latest already resolved). Resolve to origin's own default
    // branch instead in that case, and remember that this checkout is on a
    // branch origin has never seen (same signal the fork-fallback below uses).
    const branch = trackedBranch()
    let queryBranch = branchExistsOnOrigin(branch) ? branch : await fetchDefaultBranch(remote)
    if (queryBranch !== branch) status.fork = true
    let tip = await fetchBranchTip(remote, queryBranch)
    if ('notFound' in tip) {
      // branchExistsOnOrigin only reads what a PAST `git fetch` recorded into
      // local remote-tracking refs -- if `branch` was deleted or renamed on
      // origin since then, the stale local ref still says "exists" but
      // GitHub itself 404s/422s on it. One bounded retry against origin's
      // CURRENT default branch recovers from that without looping.
      const fallbackBranch = await fetchDefaultBranch(remote)
      if (fallbackBranch !== queryBranch) {
        queryBranch = fallbackBranch
        status.fork = true
        tip = await fetchBranchTip(remote, queryBranch)
      }
      if ('notFound' in tip) throw new Error(`GitHub /commits/${queryBranch} -> not found`)
    }
    status.latest = tip.sha

    if (status.latest === current) {
      updateStatusCache = status
      return status
    }

    // 2) list commits between local HEAD and the remote latest via compare.
    const cmp = await fetchCompare(remote, current, status.latest)
    if (cmp && !('notFound' in cmp)) {
      applyCompare(status, cmp)
    } else if (cmp && 'notFound' in cmp) {
      // Local HEAD is not a commit on the GitHub remote -- the normal state of a
      // customised fork carrying local commits on top of upstream. Comparing the
      // raw HEAD 404s forever, surfacing as a permanent scary error. Fall back to
      // the upstream merge-base (our fork point, which IS an upstream commit) so
      // `behind`/`commits` reflect genuinely new upstream commits rather than the
      // fork divergence.
      status.fork = true
      const base = upstreamMergeBase(queryBranch)
      if (!base || base === status.latest) {
        // No local upstream ref, or the fork point already is the upstream tip:
        // nothing new upstream. A fork being ahead of upstream is expected, not
        // an error.
        status.behind = 0
      } else {
        const baseCmp = await fetchCompare(remote, base, status.latest)
        if (baseCmp && !('notFound' in baseCmp)) {
          applyCompare(status, baseCmp)
        } else {
          status.error = 'Local HEAD not found on GitHub -- different fork or unpushed commits?'
        }
      }
    }
  } catch (err) {
    status.error = err instanceof Error ? err.message : String(err)
  }
  updateStatusCache = status
  return status
}

// Polls the GitHub branch this checkout follows for new commits and compares to the
// local HEAD. Lets the dashboard show a "new version available" badge
// without anyone having to SSH in and run update.sh.
export function startUpdateChecker(): NodeJS.Timeout {
  // First check shortly after startup; then every 15 minutes.
  setTimeout(() => { refreshUpdateStatus().catch(() => {}) }, 10_000)
  return setInterval(() => { refreshUpdateStatus().catch(() => {}) }, 15 * 60_000)
}

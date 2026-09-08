#!/usr/bin/env bash
# Marveen backup.
#
# The archive has two top-level groups so a restore is unambiguous about
# where each file belongs (see docs/MIGRATION.md):
#
#   repo/   -> extract under the project root (this repo)
#     store/claudeclaw.db (+ -shm/-wal; WAL-checkpointed before copy)
#     store/*  (every top-level FILE/SYMLINK except the DB files above and
#       provably-not-state items -- rotating logs, PID/lock files, .bak-*
#       snapshots, pane-capture debug dumps: DENYLIST)
#     store/agent-taskstate/**  (per-agent PreCompact task-state; ALLOWLISTED
#       directory -- see the comment at the store/ block below for why
#       directories use the opposite default from files)
#     .env                     (project root secrets)
#     scheduled-tasks.json     (legacy, if present)
#     assets/meetings/**       (meeting transcripts/memos)
#     agents/*/CLAUDE.md, SOUL.md, .mcp.json
#     agents/*/.claude/channels/{telegram,slack,discord}/.env, access.json
#
#   home/   -> extract under $HOME
#     .claude/skills/**            (the self-built skill library)
#     .claude/scheduled-tasks/**   (file-based scheduled tasks: SKILL.md + config)
#     .claude/projects/<encoded-REPO_ROOT>/memory/**  (file-based hot/warm/cold memories)
#     .claude/channels/*/.env      (MAIN orchestrator channel token)
#     .claude/channels/*/access.json, invites.json, approved/**  (pairing state)
#     Library/LaunchAgents/com.<MAIN_AGENT_ID>.*.plist (launchd jobs)
#
# store/projects/, store/reference-docs/, store/references/ (client/business
# content -- projects/ alone often 100MB+) and store/backups/ (a nested
# backup-of-something-else) are DELIBERATELY excluded -- directories are
# allowlist-only here (see below), so a size/scope decision to include one of
# these is an explicit, separate addition to STORE_STATE_DIRS, never silent.
#
# Output: backups/claudeclaw-YYYYmmdd-HHMMSS.tar.gz
# Retention: keeps the most recent 14 archives, prunes the rest.
#
# Restore (preserve modes so the 0600 token files stay private):
#   tar -xpzf <archive> -C /tmp/restore        # inspect first
#   then copy repo/* into the project root and home/* into $HOME.
# Full runbook: docs/MIGRATION.md.

set -euo pipefail

# 2026-09-08 Codex review: the archive now carries MANY more credentials than
# before (the expanded store/ coverage below). Under the default 022 umask
# every file this script creates -- staging copies, the manifest, the .tar.gz
# itself -- would land group/world-readable regardless of the source file's
# own mode, a local secret leak on any multi-user box. umask 077 covers every
# file/dir created from here on; the explicit chmod calls below are
# defense-in-depth for BACKUP_DIR and ARCHIVE specifically (a pre-existing,
# looser-permissioned backups/ directory from before this fix would otherwise
# keep its old mode forever, umask only affects NEW creation).
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${REPO_ROOT}/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="${BACKUP_DIR}/claudeclaw-${STAMP}.tar.gz"
KEEP=14

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
cd "${REPO_ROOT}"

# Checkpoint WAL into the main DB file so the snapshot is self-contained.
# Tolerate a missing sqlite3 CLI -- just fall back to copying the files as-is.
if [[ -f store/claudeclaw.db ]] && command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 store/claudeclaw.db 'PRAGMA wal_checkpoint(TRUNCATE);' >/dev/null || true
fi

# --- Build the two path lists (each relative to its own base). -------------
# tar refuses missing entries, which would fail the whole backup on a fresh
# machine (no agents yet) -- so we only list paths that actually exist.
REPOLIST="$(mktemp -t claudeclaw-repo.XXXXXX)"
HOMELIST="$(mktemp -t claudeclaw-home.XXXXXX)"
MANIFEST="$(mktemp -t claudeclaw-manifest.XXXXXX)"
STAGE="$(mktemp -d -t claudeclaw-stage.XXXXXX)"
trap 'rm -f "${REPOLIST}" "${HOMELIST}" "${MANIFEST}"; rm -rf "${STAGE}"' EXIT

# add_if <listfile> <base> <relpath>  -- append relpath when <base>/<relpath> exists.
add_if() {
  local list="$1" base="$2" rel="$3"
  if [[ -e "${base}/${rel}" ]]; then echo "${rel}" >> "${list}"; fi
}

# repo/ group (relative to REPO_ROOT)
add_if "${REPOLIST}" "${REPO_ROOT}" store/claudeclaw.db
add_if "${REPOLIST}" "${REPO_ROOT}" store/claudeclaw.db-shm
add_if "${REPOLIST}" "${REPO_ROOT}" store/claudeclaw.db-wal
# store/ coverage (2026-09-08, DENYLIST for files, ALLOWLIST for directories
# -- 2026-09-08 Codex review round 1 found a positive-name whitelist itself
# unfixable: an explicit list was already missing real, runtime-read
# state/credential files [vault-bindings.json, egress-allowlist.json,
# .github-fleet-token, .gdocs-oauth*.json, costops-config.json,
# outgoing-copy-gate-rules.json, watchdog-userbot state, ...] THE FIRST TIME
# it was written -- a positive list can only ever cover what its author
# already knew to name.
#
# Round 2 (Codex): a denylist covering ALL top-level entries (files AND
# directories alike) has the opposite failure mode -- ANY future or
# unnoticed directory at store/ top-level (a model/ML cache, a venv, a
# browser profile, a generated export dump) gets swept in WHOLESALE by
# default, risking a multi-GB archive, a slow run, or a full disk. A FIFO,
# socket, or device special file (also unfiltered by a bare `find -mindepth
# 1 -maxdepth 1`) could additionally hang `cp -pR` outright. So the two
# entry kinds now get opposite defaults:
#   - regular files + symlinks: DENYLIST (auto-included unless explicitly
#     excluded below) -- these are what state/credential files actually are,
#     and a stray large *file* is comparatively rare and easy to add to the
#     denylist if one ever shows up.
#   - directories: ALLOWLIST ONLY (nothing is swept in unless explicitly
#     named) -- a new directory must be a deliberate decision, never a
#     silent default. store/agent-taskstate/ is state, added explicitly.
if [[ -d store ]]; then
  find store -mindepth 1 -maxdepth 1 \( -type f -o -type l \) \
    ! -name claudeclaw.db ! -name 'claudeclaw.db-*' \
    ! -name .dashboard-token ! -name config-overrides.json \
    ! -name '*.log' ! -name '*.log.*' \
    ! -name '*.pid' ! -name '*.lock' \
    ! -name '*.bak-*' \
    ! -name 'context-guard-last-pane-*.txt' \
    ! -name usage-history.jsonl \
    -print >> "${REPOLIST}"
  # Explicit directory allowlist. store/projects/, store/reference-docs/, and
  # store/references/ (client/business content, real but potentially large --
  # a separate size/scope decision, not folded in silently here) and
  # store/backups/ (a nested backup-of-something-else) are DELIBERATELY not
  # in this list.
  STORE_STATE_DIRS=(agent-taskstate)
  for _d in "${STORE_STATE_DIRS[@]}"; do
    add_if "${REPOLIST}" "${REPO_ROOT}" "store/${_d}"
  done
  unset _d
fi
add_if "${REPOLIST}" "${REPO_ROOT}" store/.dashboard-token
add_if "${REPOLIST}" "${REPO_ROOT}" store/config-overrides.json
add_if "${REPOLIST}" "${REPO_ROOT}" .env
add_if "${REPOLIST}" "${REPO_ROOT}" scheduled-tasks.json
add_if "${REPOLIST}" "${REPO_ROOT}" assets/meetings
# Per-agent identity + channel secrets (glob; missing dir is not an error).
if [[ -d agents ]]; then
  find agents -type f \
    \( -name 'CLAUDE.md' -o -name 'SOUL.md' -o -name '.mcp.json' \
       -o -name 'access.json' -o -name '.env' \) \
    -print >> "${REPOLIST}"
fi

# home/ group (relative to $HOME)
add_if "${HOMELIST}" "${HOME}" .claude/skills
add_if "${HOMELIST}" "${HOME}" .claude/scheduled-tasks
# File-based memories (2026-09-08): the whole point of the hot/warm/cold
# memory system is that it survives a compact/restart -- an old backup that
# does not carry it defeats that. Claude Code names this dir by replacing
# every "/" in the project path with "-" (e.g. /home/kisss/marveen ->
# -home-kisss-marveen); derived here rather than hardcoded so this keeps
# working under any install path, not just this one.
add_if "${HOMELIST}" "${HOME}" ".claude/projects/${REPO_ROOT//\//-}/memory"
# MAIN orchestrator channel tokens + pairing state, per provider. bot.pid and
# inbox/ are runtime/transient and intentionally excluded.
if [[ -d "${HOME}/.claude/channels" ]]; then
  ( cd "${HOME}" && find .claude/channels -maxdepth 2 \
      \( -name '.env' -o -name 'access.json' -o -name 'invites.json' \) \
      -print ) >> "${HOMELIST}"
  ( cd "${HOME}" && find .claude/channels -maxdepth 2 -type d -name 'approved' -print ) >> "${HOMELIST}"
fi
# launchd jobs for this fleet. The job labels are com.<MAIN_AGENT_ID>.<service>
# (see src/web/main-agent.ts), so resolve MAIN_AGENT_ID the way the app does
# (src/env.ts: read from .env, default "marveen" when unset) instead of
# hardcoding one deployment's prefix. Parsing mirrors env.ts: last definition
# wins, surrounding matching quotes stripped.
MAIN_AGENT_ID="marveen"
if [[ -f "${REPO_ROOT}/.env" ]]; then
  # `|| true`: with `set -o pipefail`, a no-match grep would otherwise fail the
  # whole substitution (and, under `set -e`, abort the backup) on any install
  # that leaves MAIN_AGENT_ID unset and relies on the "marveen" default.
  _mid="$(grep -E '^[[:space:]]*MAIN_AGENT_ID[[:space:]]*=' "${REPO_ROOT}/.env" | tail -1 \
    | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*$//; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/' || true)"
  [[ -n "${_mid}" ]] && MAIN_AGENT_ID="${_mid}"
fi
if [[ -d "${HOME}/Library/LaunchAgents" ]]; then
  ( cd "${HOME}" && find Library/LaunchAgents -maxdepth 1 -name "com.${MAIN_AGENT_ID}.*.plist" -print ) >> "${HOMELIST}"
fi

if [[ ! -s "${REPOLIST}" && ! -s "${HOMELIST}" ]]; then
  echo "backup: nothing to archive" >&2
  exit 0
fi

# --- Manifest (stored at the archive root for self-description). -----------
{
  echo "Marveen backup ${STAMP}"
  echo "host: $(hostname 2>/dev/null || echo '?')   user: ${USER:-?}   home: ${HOME}"
  echo "repo root: ${REPO_ROOT}"
  echo "Restore: tar -xpzf <archive> -C <tmp>; copy repo/* -> project root, home/* -> \$HOME."
  echo "See docs/MIGRATION.md for the full runbook (TCC, launchd paths, one-bot-one-poller, venv rebuild)."
  echo "--- repo/ ---"; sed 's,^,repo/,' "${REPOLIST}" 2>/dev/null || true
  echo "--- home/ ---"; sed 's,^,home/,' "${HOMELIST}" 2>/dev/null || true
} > "${MANIFEST}"

# --- Assemble the archive via a staging dir, then one plain tar. -----------
# The repo/ and home/ groups are produced by copying into a staging tree, NOT
# by tar name-substitution: bsdtar's `-s` and GNU tar's `--transform` are
# mutually incompatible (on GNU tar, `-s` is `--same-order` and takes no
# argument), so a substitution-based build is not portable. Staging + a single
# `tar -czf -C "${STAGE}" .` works identically on macOS (bsdtar) and Linux
# (GNU tar). Everything backed up is small (a few MB), so the copy is cheap;
# `cp -pR` preserves modes so the 0600 token files stay private.
cp "${MANIFEST}" "${STAGE}/MANIFEST.txt"

stage_group() {  # stage_group <listfile> <base> <group>
  local list="$1" base="$2" group="$3" rel parent
  [[ -s "${list}" ]] || return 0
  while IFS= read -r rel; do
    [[ -z "${rel}" ]] && continue
    parent="$(dirname "${rel}")"
    mkdir -p "${STAGE}/${group}/${parent}"
    cp -pR "${base}/${rel}" "${STAGE}/${group}/${parent}/"
  done < "${list}"
}

stage_group "${REPOLIST}" "${REPO_ROOT}" repo
stage_group "${HOMELIST}" "${HOME}" home

# Archive only the top-level entries that exist (a group dir is absent when
# its list was empty), so tar never errors on a missing entry and the names
# stay clean (no leading "./").
( cd "${STAGE}" && tar -czf "${ARCHIVE}" MANIFEST.txt \
    $( [[ -d repo ]] && echo repo ) $( [[ -d home ]] && echo home ) )
# Explicit chmod, not just umask: umask only governs the mode a NEW file is
# CREATED with, so this is defense-in-depth against anything that could set a
# looser mode after creation (an inherited ACL, an unusual tar build, a
# future edit that creates ARCHIVE some other way) -- the archive now carries
# many more credentials than before, this must never be group/world-readable.
chmod 600 "${ARCHIVE}"
echo "backup: wrote ${ARCHIVE} ($(wc -c < "${ARCHIVE}" | awk '{print $1}') bytes)"

# --- Self-verify: re-read the WRITTEN archive and confirm it holds EXACTLY
# the path set that was staged, instead of trusting tar's exit code alone (or
# just a file count -- 2026-09-08 Codex review: same count with a dropped
# entry and an unrelated extra one would pass a count-only check undetected).
# A backup that "succeeds" by exit code but silently drops/corrupts files on
# the way to disk (a truncated write, a full disk mid-archive, a tar
# path/length limit) is worse than an honest failure -- nobody re-reads an
# old backup until the day they actually need it. Listing the archive is now
# fatal on its own failure too (no `|| true` swallowing a corrupt/truncated
# read as "0 entries, so it matched nothing, so who knows").
ACTUAL_LIST="$(mktemp -t claudeclaw-actual.XXXXXX)"
EXPECTED_LIST="$(mktemp -t claudeclaw-expected.XXXXXX)"
trap 'rm -f "${REPOLIST}" "${HOMELIST}" "${MANIFEST}" "${ACTUAL_LIST}" "${EXPECTED_LIST}" "${ACTUAL_LIST}.sorted"; rm -rf "${STAGE}"' EXIT

if ! tar -tzf "${ARCHIVE}" > "${ACTUAL_LIST}" 2>/dev/null; then
  echo "backup: VERIFY FAILED -- could not even list the contents of the written archive (corrupt/truncated write?)" >&2
  echo "backup: NOT pruning old archives -- ${ARCHIVE} is suspect, investigate before trusting it" >&2
  exit 1
fi
# Directory entries end in "/" in tar's listing; regular files and symlinks
# don't -- excluding them is what makes this a fair comparison against the
# staged FILE/SYMLINK set below (a directory always appears as its own tar
# entry in addition to what's inside it, which would otherwise false-positive
# as an "extra" entry on every single archive).
grep -v '/$' "${ACTUAL_LIST}" | LC_ALL=C sort -u > "${ACTUAL_LIST}.sorted"
# `-type f -o -type l` (not just `-type f`): a staged symlink is legitimate
# (e.g. a per-provider channel dir symlinked into place) and must count as
# present, not be silently excluded from the expected set and then reported
# as a false "extra" entry once tar lists it as itself.
( cd "${STAGE}" && find . \( -type f -o -type l \) | sed 's|^\./||' ) | LC_ALL=C sort -u > "${EXPECTED_LIST}"

if ! diff -q "${EXPECTED_LIST}" "${ACTUAL_LIST}.sorted" >/dev/null; then
  echo "backup: VERIFY FAILED -- the written archive's contents do not exactly match what was staged" >&2
  MISSING="$(comm -23 "${EXPECTED_LIST}" "${ACTUAL_LIST}.sorted" | head -5)"
  EXTRA="$(comm -13 "${EXPECTED_LIST}" "${ACTUAL_LIST}.sorted" | head -5)"
  [[ -n "${MISSING}" ]] && echo "backup: missing from archive (first 5): ${MISSING}" >&2
  [[ -n "${EXTRA}" ]] && echo "backup: unexpected extra entries in archive (first 5): ${EXTRA}" >&2
  echo "backup: NOT pruning old archives -- ${ARCHIVE} is suspect, investigate before trusting it" >&2
  exit 1
fi
echo "backup: verified -- archive contains exactly the $(wc -l < "${EXPECTED_LIST}" | tr -d ' ') staged file(s)/symlink(s)"

# The archive contains sensitive tokens (dashboard bearer, channel bot tokens,
# project .env secrets). Do not auto-sync ${BACKUP_DIR} to iCloud, Dropbox,
# Google Drive, or any other cloud-backup folder. Keep it local.
echo "backup: WARNING -- archive contains sensitive tokens; keep ${BACKUP_DIR} out of cloud-sync folders (iCloud / Dropbox / Google Drive)." >&2

# Keep the newest ${KEEP} archives, drop the rest. while-read (not mapfile)
# for macOS bash 3.2 compatibility.
ls -1t "${BACKUP_DIR}"/claudeclaw-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while IFS= read -r f; do
  [[ -z "${f}" ]] && continue
  rm -f "${f}"
  echo "backup: pruned $(basename "${f}")"
done

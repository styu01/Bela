#!/usr/bin/env bash
# Marveen backup.
#
# The archive has two top-level groups so a restore is unambiguous about
# where each file belongs (see docs/MIGRATION.md):
#
#   repo/   -> extract under the project root (this repo)
#     store/claudeclaw.db (a consistent hot-backup snapshot via the real
#       SQLite backup API -- scripts/backup-sqlite.mjs -- not a raw copy;
#       WAL-aware on its own, no separate -shm/-wal files needed)
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
# --- Encryption (2026-09-10, kanban e5c6ce03) -------------------------------
# A verified PLAINTEXT archive is written and retained locally, exactly as
# before (Output/Retention below). A SEPARATE, GPG AES256-encrypted copy of
# that SAME already-verified archive is then produced and published to
# BACKUP_DEST_ROOT/encrypted -- meant to be a Drive-synced folder Istvan
# designates via the BACKUP_DEST_ROOT env var (defaults to the local backups/
# dir when unset, i.e. encryption-only with no off-machine copy). The
# encryption step re-verifies itself independently (decrypt + byte-identical
# cmp against the plaintext archive) before publishing or pruning anything --
# a failed encryption pass never touches the plaintext backup or its own
# prior encrypted archives.
#
# The passphrase lives OUTSIDE this repo and OUTSIDE any backed-up directory
# (BACKUP_PASSPHRASE_FILE, default ~/.marveen-backup-passphrase) -- see the
# validation block below. It is never read into a shell variable; gpg reads
# it directly via --passphrase-file, and this script never echoes or logs it.
#
# Output: backups/claudeclaw-YYYYmmdd-HHMMSS.tar.gz (plaintext, local)
#         BACKUP_DEST_ROOT/encrypted/claudeclaw-YYYYmmdd-HHMMSS.tar.gz.gpg
# Retention: keeps the most recent 14 of EACH, pruned independently.
#
# Restore (preserve modes so the 0600 token files stay private):
#   plaintext:  tar -xpzf <archive> -C /tmp/restore
#   encrypted:  gpg --batch --no-tty --pinentry-mode loopback \
#                 --passphrase-file <passphrase-file> --decrypt <archive>.gpg \
#                 | tar -xpzf - -C /tmp/restore
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

# Loud, structured failure reporting on fd3 (kept open on the real stderr
# even if stdout/stderr are later redirected by a caller) -- accepted as part
# of the dev-spec (5 Codex rounds, kanban e5c6ce03) so an unattended daily run
# always leaves a short, greppable FAIL line behind, not just a bash trace.
exec 3>&2
FAIL_REPORTED=0
die() {
  local message="$1"
  FAIL_REPORTED=1
  printf 'backup: ERROR: %s\n' "${message}" >&2
  printf 'FAIL: %.175s\n' "${message}" >&3
  exit 1
}

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

canonical_dir() {
  (cd "$1" 2>/dev/null && pwd -P)
}

canonical_file() {
  local directory name
  directory="$(dirname "$1")"
  name="$(basename "$1")"
  printf '%s/%s\n' "$(canonical_dir "${directory}")" "${name}"
}

path_is_within() {
  local child="$1" parent="$2"
  [[ "${child}" == "${parent}" || "${child}" == "${parent}/"* ]]
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${REPO_ROOT}/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="${BACKUP_DIR}/claudeclaw-${STAMP}.tar.gz"
KEEP=14
OS_NAME="$(uname -s)"

# --- Encryption config -------------------------------------------------------
BACKUP_DEST_ROOT="${BACKUP_DEST_ROOT:-${BACKUP_DIR}}"
PASSPHRASE_FILE="${BACKUP_PASSPHRASE_FILE:-${HOME}/.marveen-backup-passphrase}"
ENCRYPTED_RETENTION_COUNT="${BACKUP_ENCRYPTED_RETENTION_COUNT:-14}"
is_positive_integer "${ENCRYPTED_RETENTION_COUNT}" \
  || die "BACKUP_ENCRYPTED_RETENTION_COUNT must be a positive integer"
command -v gpg >/dev/null 2>&1 || die "required command is missing: gpg"
command -v cmp >/dev/null 2>&1 || die "required command is missing: cmp"

# Passphrase validation is deliberately strict and runs BEFORE any work: the
# secret must not be part of this backup and must remain owned/readable only
# by the invoking user. The CONTENT is never read into a shell variable or
# logged -- only file metadata (existence, mode, owner, link count, line
# count) is inspected here; gpg reads the content directly via
# --passphrase-file.
[[ ! -L "${PASSPHRASE_FILE}" ]] || die "passphrase file must not be a symlink: ${PASSPHRASE_FILE}"
[[ -f "${PASSPHRASE_FILE}" && -s "${PASSPHRASE_FILE}" ]] \
  || die "passphrase file must be a non-empty regular file: ${PASSPHRASE_FILE}"
case "${OS_NAME}" in
  Darwin)
    PASSPHRASE_MODE="$(stat -f '%Lp' "${PASSPHRASE_FILE}")"
    PASSPHRASE_UID="$(stat -f '%u' "${PASSPHRASE_FILE}")"
    PASSPHRASE_LINKS="$(stat -f '%l' "${PASSPHRASE_FILE}")"
    ;;
  *)
    PASSPHRASE_MODE="$(stat -c '%a' "${PASSPHRASE_FILE}")"
    PASSPHRASE_UID="$(stat -c '%u' "${PASSPHRASE_FILE}")"
    PASSPHRASE_LINKS="$(stat -c '%h' "${PASSPHRASE_FILE}")"
    ;;
esac
[[ "${PASSPHRASE_MODE}" == "600" ]] \
  || die "passphrase file mode must be exactly 0600 (found ${PASSPHRASE_MODE}): ${PASSPHRASE_FILE}"
[[ "${PASSPHRASE_UID}" == "$(id -u)" ]] \
  || die "passphrase file must be owned by the invoking user: ${PASSPHRASE_FILE}"
[[ "${PASSPHRASE_LINKS}" == "1" ]] \
  || die "passphrase file must not have hard links: ${PASSPHRASE_FILE}"
awk 'NR == 1 { if (length($0) == 0) exit 1; next } { exit 1 } END { if (NR != 1) exit 1 }' \
  "${PASSPHRASE_FILE}" || die "passphrase file must contain exactly one non-empty line: ${PASSPHRASE_FILE}"
PASSPHRASE_REAL="$(canonical_file "${PASSPHRASE_FILE}")"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
cd "${REPO_ROOT}"
REPO_ROOT_REAL="$(canonical_dir "${REPO_ROOT}")"
path_is_within "${PASSPHRASE_REAL}" "${REPO_ROOT_REAL}" \
  && die "passphrase file is inside the project root (would be backed up): ${PASSPHRASE_REAL}"

# --- Encrypted destination + lock (fail fast, before any staging work) ------
# BACKUP_DEST_ROOT is a user-managed parent (e.g. a Drive-synced folder
# Istvan points this at) -- only our dedicated encrypted/ leaf underneath it
# is created and permission-managed by this script.
[[ -d "${BACKUP_DEST_ROOT}" ]] \
  || die "BACKUP_DEST_ROOT must already exist and be a directory: ${BACKUP_DEST_ROOT}"
DEST_ROOT_REAL="$(canonical_dir "${BACKUP_DEST_ROOT}")"
ENCRYPTED_DIR="${DEST_ROOT_REAL}/encrypted"
# Codex review (2026-09-10): checking only the encrypted/ leaf was too narrow.
# BACKUP_DEST_ROOT itself IS the designated Drive-synced folder (encrypted/
# is just this script's own leaf inside it) -- a passphrase placed anywhere
# else under that same root (BACKUP_DEST_ROOT/passphrase, a sibling folder,
# etc.) still gets synced to the cloud alongside the very backups it is
# supposed to protect, which is exactly what keeping it OUTSIDE any
# backed-up/published tree exists to prevent. Check the whole root, not just
# the leaf.
path_is_within "${PASSPHRASE_REAL}" "${DEST_ROOT_REAL}" \
  && die "passphrase file is inside BACKUP_DEST_ROOT (would sync alongside the backups it protects): ${PASSPHRASE_REAL}"
if [[ ! -e "${ENCRYPTED_DIR}" ]]; then mkdir -p "${ENCRYPTED_DIR}"; fi
[[ -d "${ENCRYPTED_DIR}" && ! -L "${ENCRYPTED_DIR}" ]] \
  || die "encrypted destination must be a regular directory, not a symlink: ${ENCRYPTED_DIR}"
chmod 700 "${ENCRYPTED_DIR}"
WRITE_PROBE="$(mktemp "${ENCRYPTED_DIR}/.write-test.XXXXXX" 2>/dev/null)" \
  || die "encrypted destination is not writable: ${ENCRYPTED_DIR}"
rm -f "${WRITE_PROBE}"

# A cron/launchd-fired run and a manual run must never interleave: both would
# stage/write/prune concurrently into the same directories. Kernel-backed
# flock on Linux; macOS has no flock in the base system, so an atomic mkdir
# lock covers manual macOS runs.
LOCK_FILE="${BACKUP_DIR}/.backup.lock"
LOCK_KIND=""
LOCK_ACQUIRED=0
if [[ "${OS_NAME}" == "Darwin" ]]; then
  LOCK_DIR_PATH="${LOCK_FILE}.d"
  if mkdir "${LOCK_DIR_PATH}" 2>/dev/null; then
    LOCK_ACQUIRED=1
    LOCK_KIND="mkdir"
    printf '%s\n' "$$" > "${LOCK_DIR_PATH}/pid"
  else
    die "another backup may be running; lock exists: ${LOCK_DIR_PATH}"
  fi
else
  command -v flock >/dev/null 2>&1 || die "required command is missing: flock"
  exec 9>"${LOCK_FILE}"
  flock -n 9 || die "another backup is already running (lock: ${LOCK_FILE})"
  LOCK_KIND="flock"
fi

# --- Build the two path lists (each relative to its own base). -------------
# tar refuses missing entries, which would fail the whole backup on a fresh
# machine (no agents yet) -- so we only list paths that actually exist.
REPOLIST="$(mktemp -t claudeclaw-repo.XXXXXX)"
HOMELIST="$(mktemp -t claudeclaw-home.XXXXXX)"
MANIFEST="$(mktemp -t claudeclaw-manifest.XXXXXX)"
STAGE="$(mktemp -d -t claudeclaw-stage.XXXXXX)"
ENCRYPTED_TMP=""
EXTRACTED_DB_DIR=""
cleanup() {
  set +e
  rm -f "${REPOLIST}" "${HOMELIST}" "${MANIFEST}" "${ACTUAL_LIST:-}" "${EXPECTED_LIST:-}" "${ACTUAL_LIST:-}.sorted"
  rm -rf "${STAGE}"
  [[ -n "${ENCRYPTED_TMP}" && -f "${ENCRYPTED_TMP}" ]] && rm -f "${ENCRYPTED_TMP}"
  # Codex review (2026-09-10, non-blocking hardening): the second
  # integrity_check's extraction dir held a real (if partial) copy of the
  # database; on a `tar -xzf` or integrity_check failure the inline `die`
  # exited before the manual `rm -rf` below it ran, leaking that copy into
  # /tmp. The global trap now owns it too, so ANY exit path cleans it up.
  [[ -n "${EXTRACTED_DB_DIR}" && -d "${EXTRACTED_DB_DIR}" ]] && rm -rf "${EXTRACTED_DB_DIR}"
  if [[ "${LOCK_KIND}" == "mkdir" && "${LOCK_ACQUIRED}" == "1" ]]; then
    rm -rf "${LOCK_DIR_PATH}"
    LOCK_ACQUIRED=0
  fi
}
on_exit() {
  local code="$?"
  trap - EXIT INT TERM
  set +e
  if [[ "${code}" -ne 0 && "${FAIL_REPORTED}" == "0" ]]; then
    printf 'FAIL: unexpected backup error (exit %s)\n' "${code}" >&3
  fi
  cleanup
  exit "${code}"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# add_if <listfile> <base> <relpath>  -- append relpath when <base>/<relpath> exists.
add_if() {
  local list="$1" base="$2" rel="$3"
  if [[ -e "${base}/${rel}" ]]; then echo "${rel}" >> "${list}"; fi
}

# Consistent SQLite snapshot via the real SQLite hot-backup API (2026-09-10,
# dev-spec kanban e5c6ce03). The accepted spec called for the `sqlite3` CLI's
# `.backup` dot-command + integrity_check; MEASURED on this production host:
# the sqlite3 CLI is not installed (only the shared library), which would
# make the daily backup fail outright on its own target machine. backup-
# sqlite.mjs uses better-sqlite3 instead -- the identical underlying SQLite
# engine and the identical backup C API, and already a hard, proven
# dependency of this app (src/db.ts), so `node` is the only requirement here,
# not a separate system package. It is WAL-aware on its own (the backup API
# reads a consistent snapshot including any not-yet-checkpointed WAL content),
# so a separate `PRAGMA wal_checkpoint` step and separate -wal/-shm files in
# the archive are no longer needed -- the staged claudeclaw.db is already a
# complete, self-contained, consistent snapshot.
SQLITE_HELPER="${REPO_ROOT}/scripts/backup-sqlite.mjs"
command -v node >/dev/null 2>&1 || die "required command is missing: node"
[[ -f "${SQLITE_HELPER}" ]] || die "sqlite backup helper is missing: ${SQLITE_HELPER}"

# --- Build the two path lists (each relative to its own base). -------------
# repo/ group (relative to REPO_ROOT). The DB itself is NOT added here -- it
# is hot-backed-up directly into staging further down (a raw cp of a live
# WAL-mode DB is not a consistent snapshot on its own).
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
#   agent-package-credential.json (and its interrupted-write tmp files): the agent package device token
#     (upstream 3f30ff34 MIOAGENTINSTALL1114, plan 6.4): one per machine, never carried; after a restore the
#     owner connects again.
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
    ! -name agent-package-credential.json ! -name 'agent-package-credential.json.*.tmp' \
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

if [[ ! -s "${REPOLIST}" && ! -s "${HOMELIST}" && ! -f "${REPO_ROOT}/store/claudeclaw.db" ]]; then
  echo "backup: nothing to archive" >&2
  exit 0
fi

# Three-way path exclusion (dev-spec, kanban e5c6ce03): the passphrase must
# not itself be one of the paths this run is about to stage, however it got
# there (a future add_if, a symlink alias, a HOME subtree overlap). Checked
# against the RESOLVED staged list, not just the static source-dir list
# above, so it also catches an indirect inclusion (e.g. a symlink inside
# agents/ pointing at the passphrase file).
#
# Codex review (2026-09-10): the first version of this check only compared
# each LIST ENTRY's own canonical path against the passphrase -- an exact
# equality. Several entries (.claude/skills, .claude/scheduled-tasks, the
# memory dir, assets/meetings, store/agent-taskstate, .claude/channels/*
# /approved) are DIRECTORIES that stage_group copies WHOLESALE via `cp -pR`,
# so a passphrase placed anywhere INSIDE one of those trees (e.g.
# BACKUP_PASSPHRASE_FILE=$HOME/.claude/skills/private-passphrase) passed the
# old equality check yet was still physically copied into the archive. Each
# entry now branches on file-vs-directory: a file is still an exact-match
# check, a directory is a CONTAINMENT check (path_is_within) against its own
# resolved path, so nothing inside it can hide from this guard.
check_not_passphrase() {  # check_not_passphrase <base> <rel> <group-label>
  local base="$1" rel="$2" group="$3" target
  target="${base}/${rel}"
  if [[ -L "${target}" ]]; then
    : # a symlink itself is staged as a link, not walked; nothing to leak from it here
  elif [[ -d "${target}" ]]; then
    local resolved
    resolved="$(canonical_dir "${target}" 2>/dev/null)" || true
    if [[ -n "${resolved}" ]] && path_is_within "${PASSPHRASE_REAL}" "${resolved}"; then
      die "passphrase file is inside a directory this run would archive wholesale (${group}/${rel}): ${PASSPHRASE_REAL}"
    fi
  elif [[ -e "${target}" ]]; then
    if [[ "$(canonical_file "${target}" 2>/dev/null)" == "${PASSPHRASE_REAL}" ]]; then
      die "passphrase file would be included in the ${group}/ backup group via: ${rel}"
    fi
  fi
  # Explicit, unconditional success: under `set -e` a function's return
  # status is whatever its LAST command left behind, and both `[[ ]] && die`
  # above and a `-d`/`-e` test that comes up false in the expected (no match)
  # case return 1 -- which, as an unguarded statement in the caller's `while`
  # loop, would silently abort the whole script via `set -e`, never reaching
  # `die` at all (measured: this exact bug happened here during review).
  return 0
}
while IFS= read -r _rel; do
  [[ -z "${_rel}" ]] && continue
  check_not_passphrase "${REPO_ROOT}" "${_rel}" repo
done < "${REPOLIST}"
while IFS= read -r _rel; do
  [[ -z "${_rel}" ]] && continue
  check_not_passphrase "${HOME}" "${_rel}" home
done < "${HOMELIST}"
unset _rel

# --- Manifest (stored at the archive root for self-description). -----------
{
  echo "Marveen backup ${STAMP}"
  echo "host: $(hostname 2>/dev/null || echo '?')   user: ${USER:-?}   home: ${HOME}"
  echo "repo root: ${REPO_ROOT}"
  echo "Restore: tar -xpzf <archive> -C <tmp>; copy repo/* -> project root, home/* -> \$HOME."
  echo "See docs/MIGRATION.md for the full runbook (TCC, launchd paths, one-bot-one-poller, venv rebuild)."
  echo "--- repo/ ---"
  if [[ -f "${REPO_ROOT}/store/claudeclaw.db" ]]; then
    echo "(consistent hot-backup snapshot, not a raw copy:)"
    echo "repo/store/claudeclaw.db"
  fi
  sed 's,^,repo/,' "${REPOLIST}" 2>/dev/null || true
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

# Hot-backup the live database straight into staging (tolerates a missing DB
# -- a fresh install with no agents yet -- exactly like the rest of this
# script tolerates missing sources). First integrity_check (dev-spec:
# "twice") runs immediately on that staged snapshot, before it is packaged.
if [[ -f "${REPO_ROOT}/store/claudeclaw.db" ]]; then
  mkdir -p "${STAGE}/repo/store"
  node "${SQLITE_HELPER}" backup "${REPO_ROOT}/store/claudeclaw.db" "${STAGE}/repo/store/claudeclaw.db" \
    || die "SQLite hot-backup failed"
  node "${SQLITE_HELPER}" check "${STAGE}/repo/store/claudeclaw.db" \
    || die "staged SQLite database failed integrity_check -- refusing to back up a corrupt database"
fi

# No-clobber (Codex review, 2026-09-10): STAMP has 1-second resolution, so a
# manual re-run in the same second as a prior one (or a clock issue) would
# otherwise silently overwrite an existing plaintext archive via `tar -czf`.
# The encrypted side already had this guard (ENCRYPTED_FINAL below); the
# plaintext side needs the identical protection.
[[ ! -e "${ARCHIVE}" ]] || die "refusing to overwrite existing plaintext backup: ${ARCHIVE}"

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

# Second integrity_check (dev-spec: "twice"): re-extract the DB from the
# WRITTEN archive itself and check it there too -- catches any corruption
# introduced by the tar write, not just the staging copy.
if tar -tzf "${ARCHIVE}" 2>/dev/null | grep -qx 'repo/store/claudeclaw.db'; then
  EXTRACTED_DB_DIR="$(mktemp -d -t claudeclaw-dbcheck.XXXXXX)"
  tar -xzf "${ARCHIVE}" -C "${EXTRACTED_DB_DIR}" repo/store/claudeclaw.db \
    || die "could not extract the archived database for the second integrity_check"
  if ! node "${SQLITE_HELPER}" check "${EXTRACTED_DB_DIR}/repo/store/claudeclaw.db"; then
    rm -rf "${EXTRACTED_DB_DIR}"
    die "archived SQLite database failed integrity_check -- the written archive is suspect, investigate before trusting it"
  fi
  rm -rf "${EXTRACTED_DB_DIR}"
fi

# The archive contains sensitive tokens (dashboard bearer, channel bot tokens,
# project .env secrets). Do not auto-sync ${BACKUP_DIR} to iCloud, Dropbox,
# Google Drive, or any other cloud-backup folder. Keep it local -- only the
# GPG-encrypted copy below is meant to leave the machine.
echo "backup: WARNING -- plaintext archive contains sensitive tokens; keep ${BACKUP_DIR} out of cloud-sync folders (iCloud / Dropbox / Google Drive)." >&2

# Plaintext retention is deliberately NOT run here (Codex review, 2026-09-10:
# the first version pruned old plaintext archives at this point, BEFORE the
# encryption pipeline below had even attempted GPG -- so a GPG failure could
# leave the run with neither a fresh encrypted copy NOR the older plaintext
# generations that used to be its fallback, exactly contradicting this
# script's own promise that a failed encryption pass never touches existing
# good backups). Pruning moved to after the encrypted archive is verified and
# published; see the retention block below.

# --- Encrypt the already-verified plaintext archive -------------------------
# GPG symmetric AES256, passphrase read directly from the validated file (never
# through a shell variable). The encrypted copy is verified by decrypting it
# straight back and comparing BYTE-FOR-BYTE against the plaintext archive that
# was just verified above -- a stronger check than re-parsing the decrypted
# tar, since it also catches any corruption gpg itself might introduce.
# --yes: the --output target below is ENCRYPTED_TMP, a file this script just
# created itself via mktemp (so it always already exists, empty) -- gpg's
# symmetric mode otherwise refuses to write over an existing file. This is
# not a real overwrite risk: the no-clobber check against the FINAL published
# path (ENCRYPTED_FINAL) still applies further down, unaffected by this flag.
GPG_ARGS=(--batch --yes --no-tty --pinentry-mode loopback --passphrase-file "${PASSPHRASE_FILE}")
ENCRYPTED_TMP="$(mktemp "${ENCRYPTED_DIR}/.claudeclaw-encrypting.XXXXXX")"
if ! gpg "${GPG_ARGS[@]}" --symmetric --cipher-algo AES256 \
    --output "${ENCRYPTED_TMP}" "${ARCHIVE}"; then
  rm -f "${ENCRYPTED_TMP}"
  die "GPG encryption failed -- plaintext backup is intact at ${ARCHIVE}, encrypted copy NOT published"
fi
chmod 600 "${ENCRYPTED_TMP}"

if ! gpg "${GPG_ARGS[@]}" --decrypt "${ENCRYPTED_TMP}" 2>/dev/null | cmp -s - "${ARCHIVE}"; then
  rm -f "${ENCRYPTED_TMP}"
  die "decrypted archive is not byte-identical to the plaintext archive -- encrypted copy NOT published"
fi
echo "backup: encrypted archive verified byte-identical to plaintext after a decrypt round-trip"

ENCRYPTED_FINAL="${ENCRYPTED_DIR}/$(basename "${ARCHIVE}").gpg"
[[ ! -e "${ENCRYPTED_FINAL}" ]] \
  || die "refusing to overwrite existing encrypted backup: ${ENCRYPTED_FINAL}"
mv "${ENCRYPTED_TMP}" "${ENCRYPTED_FINAL}"
ENCRYPTED_TMP=""
chmod 600 "${ENCRYPTED_FINAL}"
echo "backup: published verified encrypted archive ${ENCRYPTED_FINAL}"

# Retention runs only after the new encrypted archive passed every check and
# was atomically published (no-clobber move above). A failed run never prunes
# known-good encrypted backups, and this is entirely independent of the
# plaintext retention below (separate counter, separate directory).
ls -1t "${ENCRYPTED_DIR}"/claudeclaw-*.tar.gz.gpg 2>/dev/null | tail -n +$((ENCRYPTED_RETENTION_COUNT + 1)) | while IFS= read -r f; do
  [[ -z "${f}" ]] && continue
  rm -f "${f}"
  echo "backup: pruned encrypted $(basename "${f}")"
done

# Keep the newest ${KEEP} plaintext archives, drop the rest. Runs LAST, only
# after the encrypted copy is fully published above -- see the comment where
# this used to run (right after the plaintext WARNING) for why. while-read
# (not mapfile) for macOS bash 3.2 compatibility.
ls -1t "${BACKUP_DIR}"/claudeclaw-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while IFS= read -r f; do
  [[ -z "${f}" ]] && continue
  rm -f "${f}"
  echo "backup: pruned plaintext $(basename "${f}")"
done

echo "backup: SUCCESS -- plaintext=${ARCHIVE} encrypted=${ENCRYPTED_FINAL}"

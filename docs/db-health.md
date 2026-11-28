# db-health: hourly read-only check of store/claudeclaw.db

Owner decision 2026-11-28 (after the 10:11 incident in which a test run deleted
the live `store/claudeclaw.db` while the dashboard kept writing into the
unlinked inode): the nightly backup stays the only backup, a per-minute watcher
is not wanted, machine-enforced sandboxing is not wanted for now. Instead a
model-free systemd user timer looks at the database once an hour.

Files:

| Path | Role |
|------|------|
| `scripts/db-health.py` | the check (stdlib Python, no model, no network except the alert) |
| `scripts/systemd/db-health.service` | oneshot user-unit template |
| `scripts/systemd/db-health.timer` | hourly at :17 Europe/Budapest, `Persistent=true` |
| `scripts/__tests__/db-health.test.py` | isolated unittest suite (scratch DBs, fake dashboard process) |

## What one run checks

1. `store/claudeclaw.db` exists, is a regular file (not a symlink), is not 0 bytes,
   and did not shrink below half of the last healthy size.
2. It opens read-only and closes the connection right after the check. While the
   dashboard runs (a `-shm` or a non-empty `-wal` exists) the open is `mode=ro`, so
   the live WAL counts and the dashboard's writer is never blocked. With the
   dashboard stopped (no `-shm`, no WAL content) a plain `mode=ro` open of a
   WAL-mode database would CREATE empty `-wal`/`-shm` files next to it (SQLite
   behaviour, measured by PROGI 6582), so it opens `mode=ro&immutable=1` instead
   and creates nothing. A dashboard starting in that very second can at worst
   cause a bogus finding, which the 2 s re-check resolves. The only state in which
   SQLite may still create a file is a `-wal` with content but no `-shm` (an
   unclean stop): then the open is `mode=ro` and may create the `-shm`, as the
   dashboard itself would. The run's log line says which mode was used
   (`immutable`).
3. `agent_messages`, `conversation_log`, `memories`, `kanban_cards` exist.
4. `PRAGMA integrity_check` is `ok` (measured on the 61 MB live DB: 0.28 s, the
   whole run 0.41 s; `--quick` switches to `quick_check` for a much larger DB).
5. The AUTOINCREMENT high-water marks in `sqlite_sequence` of `agent_messages`,
   `conversation_log`, `memories` did not go down (ordinary deletes never lower
   them; an older copy put on the path with the dashboard STOPPED does). With the
   dashboard RUNNING the read-only open still sees the dashboard's live WAL laid
   over the older file, so the sequence may not drop; that case is caught by the
   ghost check instead (the dashboard's open inode no longer matches the path),
   so it is never silent (PROGI 6582, measured). No core table lost more than half of
   its rows (and at least 10). `kanban_cards` has a text key, so only the row
   count applies there.
6. The fds of the dashboard `MainPID` (`<MAIN_AGENT_ID>-dashboard.service`) that
   point at the DB, `-wal` or `-shm`: one whose file has no link left
   (`st_nlink == 0`, shown as `(deleted)`) or whose inode differs from the file now
   at the path means the dashboard writes into a ghost.

An open/read/integrity/missing-table finding is re-checked once after 2 s, so a
transient busy/IO error does not page anyone.

## Rescue (only in the ghost case)

The ghost DB and WAL are copied TOGETHER from `/proc/<pid>/fd/N` into
`store/recovery-db-YYYYmmdd-HHMMSS/` (mode 0700, files 0600):

- `raw/claudeclaw.db` + `raw/claudeclaw.db-wal`: byte copies, to be used as a pair;
  `raw/claudeclaw.db-shm`: forensic only.
- `claudeclaw.verified.db`: a WAL-free copy made with the SQLite backup API from a
  scratch copy of the pair, then `integrity_check` + core tables verified.
- `MANIFEST.json`: source pid/fd/inode, sha256 of every file, row counts, sequence
  values, attempts, `consistent_copy`.

Consistency: the WAL is copied first, then the DB. In WAL mode only a checkpoint
writes the DB file, so the pair is accepted as one state when the DB file
(size, mtime) and the WAL header (checkpoint sequence, salts) are unchanged from
before the WAL copy to after the DB copy; otherwise up to 10 attempts.
Stress-tested: with the default `wal_autocheckpoint` (the dashboard does not
change it) and a writer far busier than the live one, 12/12 rescues were
consistent and verified; with a pathological checkpoint every 8 pages no attempt
is consistent, the alert says so, and `consistent_copy=true` was never wrong.

The live path is never written and the dashboard is never restarted: restoring
is the main agent's decision. The same ghost is copied again at most every 6
hours; `--force-rescue` takes a fresh copy on demand (e.g. right before a restore,
to get the newest rows; it is ignored together with `--dry-run`, which never
writes anything). A rescue is skipped with a `no_space` note when free
space is below 3x the DB+WAL size plus 256 MB.

## Alerting

- Problem: one inter-agent message `from=<MAIN_AGENT_ID> to=<MAIN_AGENT_ID>`
  (the existing system-sender convention of kanban-audit / usage-fleet-monitor;
  `POST /api/messages` rejects unregistered senders such as `db-health` with 403),
  prefixed `[DB-HEALTH]`: what is wrong, where the rescue is, the nightly backups
  dir, and that nothing was restored or restarted.
- If the dashboard API does not accept it: plain-text Telegram message to
  `MARVEEN_ALERT_CHAT_ID`, else `.env ALLOWED_CHAT_ID`, with the `.env` (or
  `~/.claude/channels/telegram/.env`) bot token.
- The same problem is re-announced at most every 6 hours; a changed problem or a
  fresh successful rescue is announced at once. A failed rescue (`no_space`,
  `copy_failed`) is retried every hour but re-announced only when its reason
  changes, after 6 hours, or when a later attempt succeeds; the alert also names
  an earlier successful rescue of the same ghost, if there is one; one "back to normal" notice follows an
  announced problem (with a warning when the problem was a drop event, which
  does not heal by itself).
- No channel accepted it: exit 1 (unit `failed`), nothing is marked as sent, the
  next hourly run retries.
- Healthy: silent, one JSON line in `store/logs/db-health.log` (rotated at 1 MB).
  No token, URL with a token, exception text or response body is ever logged.

State: `store/.db-health-state.json` (0600, atomic replace): last healthy size,
inode, sequence values, row counts, alert and rescue bookkeeping. An emptied or
foreign file never becomes the baseline. Concurrency: `flock` on
`store/.db-health.lock`; a second instance logs `busy` and exits 0.

## Install / enable (main agent's decision)

```bash
cp /home/kisss/marveen/scripts/systemd/db-health.service ~/.config/systemd/user/
cp /home/kisss/marveen/scripts/systemd/db-health.timer   ~/.config/systemd/user/
systemd-analyze --user verify ~/.config/systemd/user/db-health.{service,timer}
python3 /home/kisss/marveen/scripts/db-health.py --dry-run   # writes nothing, prints one line
systemctl --user daemon-reload
systemctl --user start db-health.service                     # first real run: baseline, silent if ok
journalctl --user -u db-health.service -n 20 --no-pager; tail -1 /home/kisss/marveen/store/logs/db-health.log
systemctl --user enable --now db-health.timer
systemctl --user list-timers db-health.timer
```

Disable: `systemctl --user disable --now db-health.timer` (the service only runs
from the timer). Remove completely: additionally delete the two unit files,
`daemon-reload`, and optionally `store/.db-health-state.json`,
`store/.db-health.lock`, `store/logs/db-health.log*`.

## Not covered (known limits)

- A DB renamed away (`mv`) and replaced while the dashboard runs: the dashboard
  fd then shows the new name, not `(deleted)`, so it is not matched as a ghost;
  the path checks (sequence/rows/size drop, missing tables) still fire.
- A dashboard that holds the DB through a child process instead of `MainPID`:
  the log line carries `dashboard_db_fds`, which would then be 0.
- Truncation in place (same inode): nothing to rescue from the open fd; the alert
  points at the nightly backups.

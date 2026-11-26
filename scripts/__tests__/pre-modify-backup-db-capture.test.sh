#!/usr/bin/env bash
# pre-modify-backup.sh must never say "backup ok" without the database (upstream 6f592e60, 6d419acf).
# Hermetic: each case runs a COPY of the script in a temp repo with a curated PATH, so a host sqlite3 CLI cannot
# change the verdict. The fixture db is written through python3's stdlib sqlite3 (no CLI needed).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../pre-modify-backup.sh"
FAILS=0
pass() { echo "  [PASS] $1"; }
fail() { echo "  [FAIL] $1"; FAILS=$((FAILS + 1)); }

TMP="$(mktemp -d)"
trap 'chmod -R u+rwx "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT

# curated bin: everything the script needs, plus python3 unless NO_PY, and NEVER sqlite3
mkbin() {
  local dir="$1" with_py="$2" t p
  mkdir -p "$dir"
  for t in bash env date mkdir cp rm ls tail du cut sha256sum shasum grep git dirname basename cat head tr sed sort awk touch chmod; do
    p="$(command -v "$t" 2>/dev/null)" && [ -n "$p" ] && ln -sf "$p" "$dir/$t"
  done
  if [ "$with_py" = "1" ]; then ln -sf "$(command -v python3)" "$dir/python3"; fi
}
BIN_PY="$TMP/bin-py"; mkbin "$BIN_PY" 1
BIN_NOPY="$TMP/bin-nopy"; mkbin "$BIN_NOPY" 0

# fresh repo: scripts/pre-modify-backup.sh + store/claudeclaw.db (WAL mode, newest row only in the -wal)
newrepo() {
  local r="$TMP/$1"
  mkdir -p "$r/scripts" "$r/store"
  cp "$SCRIPT" "$r/scripts/pre-modify-backup.sh"
  JM="${2:-wal}" python3 - "$r/store/claudeclaw.db" <<'PY'
import sqlite3, sys, os
c = sqlite3.connect(sys.argv[1])
if os.environ.get("JM") == "delete":
    c.execute("CREATE TABLE t(x TEXT)"); c.execute("INSERT INTO t VALUES ('one')"); c.commit(); c.close(); sys.exit(0)
c.execute("PRAGMA journal_mode=WAL")
c.execute("CREATE TABLE t(x TEXT)")
c.execute("INSERT INTO t VALUES ('one')")
c.commit()
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.execute("INSERT INTO t VALUES ('newest-only-in-wal')")
c.commit()
# keep the connection (and so the -wal) alive until the process exits is not possible here, so leave the -wal file
# on disk by copying it aside and restoring it after close, which is what a crashed writer leaves behind
import shutil, os
shutil.copy(sys.argv[1] + "-wal", sys.argv[1] + "-wal.keep")
c.close()
if os.path.exists(sys.argv[1] + "-wal.keep"):
    shutil.move(sys.argv[1] + "-wal.keep", sys.argv[1] + "-wal")
PY
  echo "secret" > "$r/store/.dashboard-token"
  echo "$r"
}
latest() { ls -1dt "$1"/store/backups/*/ 2>/dev/null | head -1; }
rows() { python3 - "$1" <<'PY'
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1]); print(",".join(r[0] for r in c.execute("SELECT x FROM t ORDER BY rowid")))
except Exception as e:
    print("ERR " + str(e))
PY
}
run() { local r="$1" bin="$2"; shift 2; (cd "$r" && env -i HOME="$TMP" PATH="$bin" ${PRE_MODIFY_BACKUP_PY_DEADLINE_S:+PRE_MODIFY_BACKUP_PY_DEADLINE_S="$PRE_MODIFY_BACKUP_PY_DEADLINE_S"} bash scripts/pre-modify-backup.sh "$@" ) ; }

echo "case 1: no sqlite3 CLI, python3 present -> the database is in the snapshot, the line names the mode"
R="$(newrepo c1)"; OUT="$(run "$R" "$BIN_PY" t1 2>&1)"; RC=$?
S="$(latest "$R")"
[ "$RC" = 0 ] && pass "exit 0" || fail "exit $RC: $OUT"
[ -s "${S}claudeclaw.db" ] && pass "claudeclaw.db is in the snapshot" || fail "claudeclaw.db missing from snapshot: $OUT"
case "$OUT" in *"db: "*) pass "the closing line names the db mode";; *) fail "no db mode in output: $OUT";; esac
V="$(rows "${S}claudeclaw.db")"
[ "$V" = "one,newest-only-in-wal" ] && pass "the row that lived only in the -wal is readable in the snapshot" || fail "rows: $V"

echo "case 2: a sqlite3 that fails (shim) -> the database is still captured"
R="$(newrepo c2)"; SHIM="$TMP/shim2"; mkbin "$SHIM" 1
printf '#!/bin/sh\necho "disk full" >&2\nexit 1\n' > "$SHIM/sqlite3"; chmod +x "$SHIM/sqlite3"
OUT="$(run "$R" "$SHIM" t2 2>&1)"; RC=$?; S="$(latest "$R")"
[ "$RC" = 0 ] && [ -s "${S}claudeclaw.db" ] && pass "captured despite the failing sqlite3" || fail "rc=$RC out=$OUT"
V="$(rows "${S}claudeclaw.db")"
[ "$V" = "one,newest-only-in-wal" ] && pass "rows readable" || fail "rows: $V"

echo "case 3: nothing can capture the database -> BACKUP INCOMPLETE, exit 1, and the older snapshots survive"
if [ "$(id -u)" = 0 ]; then
  echo "  [SKIP] running as root: chmod 000 does not make the file unreadable"
else
  R="$(newrepo c3)"; mkdir -p "$R/store/backups"
  for i in 01 02 03 04 05 06 07 08 09 10; do mkdir -p "$R/store/backups/20200101-0000$i-old"; touch -d "2020-01-01 00:00:$i" "$R/store/backups/20200101-0000$i-old"; done
  chmod 000 "$R/store/claudeclaw.db"
  OUT="$(run "$R" "$BIN_NOPY" t3 2>&1)"; RC=$?
  [ "$RC" = 1 ] && pass "exit 1" || fail "exit $RC: $OUT"
  case "$OUT" in *"BACKUP INCOMPLETE"*) pass "says BACKUP INCOMPLETE";; *) fail "no BACKUP INCOMPLETE: $OUT";; esac
  case "$OUT" in *"backup ok"*) fail "printed backup ok without the database";; *) pass "does not print backup ok";; esac
  N="$(ls -1d "$R"/store/backups/*-old 2>/dev/null | wc -l)"
  [ "$N" = 10 ] && pass "all 10 older snapshots survive (no rotation on an incomplete snapshot)" || fail "$N older snapshots left"
fi

echo "case 4: an install with no database at all is not an error"
R="$TMP/c4"; mkdir -p "$R/scripts" "$R/store"; cp "$SCRIPT" "$R/scripts/"
OUT="$(run "$R" "$BIN_NOPY" t4 2>&1)"; RC=$?
[ "$RC" = 0 ] && pass "exit 0" || fail "exit $RC: $OUT"
case "$OUT" in *"backup ok"*) pass "backup ok";; *) fail "no backup ok: $OUT";; esac

echo "case 5: python3 path is a CONSISTENT snapshot and is named so (6d419acf)"
R="$(newrepo c5)"; OUT="$(run "$R" "$BIN_PY" t5 2>&1)"
case "$OUT" in *"python3 sqlite3 backup"*) pass "mode names the python3 online backup";; *) fail "mode not named: $OUT";; esac
case "$OUT" in *"NOT a consistent"*) fail "fell to the raw copy although python3 is available: $OUT";; *) pass "no raw copy";; esac

echo "case 6: a failing sqlite3 that leaves a torn file and stale sidecars -> clean python3 snapshot"
R="$(newrepo c6)"; SHIM="$TMP/shim6"; mkbin "$SHIM" 1
cat > "$SHIM/sqlite3" <<'SH'
#!/bin/sh
dest="${2#.backup \'}"; dest="${dest%\'}"
printf 'torn' > "$dest"; printf 'x' > "$dest-journal"; printf 'x' > "$dest-wal"
exit 1
SH
chmod +x "$SHIM/sqlite3"
OUT="$(run "$R" "$SHIM" t6 2>&1)"; S="$(latest "$R")"
V="$(rows "${S}claudeclaw.db")"
[ "$V" = "one,newest-only-in-wal" ] && pass "readable, no torn bytes" || fail "rows: $V ($OUT)"
[ ! -e "${S}claudeclaw.db-journal" ] && pass "no stale hot journal next to the snapshot" || fail "stale journal left"

echo "case 7: a source locked exclusively -> bounded by the deadline, then the raw copy (never a hang)"
R="$(newrepo c7 delete)"
python3 - "$R/store/claudeclaw.db" "$TMP/locked.flag" <<'PY' &
import sqlite3, sys, time, pathlib
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("BEGIN EXCLUSIVE")
pathlib.Path(sys.argv[2]).write_text("locked")
time.sleep(25)
PY
LOCKPID=$!
for _ in $(seq 1 50); do [ -f "$TMP/locked.flag" ] && break; sleep 0.1; done
T0=$(date +%s)
OUT="$(PRE_MODIFY_BACKUP_PY_DEADLINE_S=2 run "$R" "$BIN_PY" t7 2>&1)"; RC=$?
T1=$(date +%s)
[ $((T1 - T0)) -lt 20 ] && pass "returned in $((T1 - T0))s with deadline 2s" || fail "took $((T1 - T0))s"
[ "$RC" = 0 ] && [ -s "$(latest "$R")claudeclaw.db" ] && pass "raw copy captured the database" || fail "rc=$RC $OUT"
echo "case 7b: the same with SIGALRM ignored by the caller"
R="$(newrepo c7b delete)"; kill $LOCKPID 2>/dev/null; wait $LOCKPID 2>/dev/null
rm -f "$TMP/locked.flag"
python3 - "$R/store/claudeclaw.db" "$TMP/locked.flag" <<'PY' &
import sqlite3, sys, time, pathlib
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("BEGIN EXCLUSIVE")
pathlib.Path(sys.argv[2]).write_text("locked")
time.sleep(25)
PY
LOCKPID=$!
for _ in $(seq 1 50); do [ -f "$TMP/locked.flag" ] && break; sleep 0.1; done
T0=$(date +%s)
OUT="$( trap '' ALRM; PRE_MODIFY_BACKUP_PY_DEADLINE_S=2 run "$R" "$BIN_PY" t7b 2>&1 )"; RC=$?
T1=$(date +%s)
[ $((T1 - T0)) -lt 20 ] && pass "deadline still enforced ($((T1 - T0))s)" || fail "took $((T1 - T0))s"
kill $LOCKPID 2>/dev/null; wait $LOCKPID 2>/dev/null

echo "case 8: a malformed deadline value falls back to the default and still snapshots"
R="$(newrepo c8)"; OUT="$(PRE_MODIFY_BACKUP_PY_DEADLINE_S=abc run "$R" "$BIN_PY" t8 2>&1)"
case "$OUT" in *"python3 sqlite3 backup"*) pass "ok with malformed deadline";; *) fail "$OUT";; esac

echo
if [ "$FAILS" -gt 0 ]; then echo "$FAILS FAILED"; exit 1; fi
echo "All pre-modify-backup db-capture tests passed."

#!/usr/bin/env python3
"""PreToolUse gate: no agent writes the dashboard login's second-factor state (BELA 6410, the wave-end item).

The TOTP second factor (src/web/dashboard-totp.ts) keeps its secret in store/.dashboard-totp.json and its on/off
flag in the dashboard_users table of store/claudeclaw.db. Every agent runs as the same OS user, so without a gate
any agent's Bash could replace the secret, read it, or clear the flag, and the second factor would protect only
against a remote password-knower. This is the narrow gate BELA asked for instead of porting upstream's whole
db-write-gate (BELA 6410): three rules, for every agent, the main one included.

  R1  The second-factor store, by name or by a glob or brace list that matches it: any use outside a data position
      (Bash), any path tool on it (Read, Write, Edit, MultiEdit, NotebookEdit, Grep), a recursive search aimed at the
      store folder (grep -r, rg, ag, ack; the Grep tool there needs a glob that cannot match it), and a script written
      with its name in it. Reading is refused too: the secret is the factor.
  R2  An SQL write naming dashboard_users (INSERT/REPLACE/UPDATE/DELETE/DROP/ALTER/CREATE TRIGGER ON, the sqlite3
      shell's .import), and the schema route around a table name (PRAGMA writable_schema, writes on
      sqlite_master/sqlite_schema), in text a program runs: an interpreter's arguments, its stdin (heredoc,
      here-string, a pipe from echo/printf/cat), a command substitution, sh -c / eval, and a script file written with it.
  R3  The dashboard DB file as a write target: rm, mv (either side), cp/install/rsync/ln/scp destination, dd of=,
      tee, truncate, shred, sed/perl -i, a redirection, find -fprint/-fls, the sqlite3 shell's .backup/.save/.clone,
      a Write/Edit of it, and interpreter code that both names the file and calls a file-writing API; the store folder
      (or a folder holding it) removed or moved away. Opening it through sqlite3 to write other tables passes (the
      kanban, memories and messages are written that way).

A find is read in its own left-to-right order (KISPROGI 6490 L1): only the filters before an action count, near the
store only name-printing commands and one-file readers/removers may run, and every -exec command is checked as a
command of its own; a find piped into xargs (anywhere later in the pipeline) counts as -exec of the xargs command. The
agents' Bash runs Claude Code's shell functions, find = bfs 4.1.1 and grep = ugrep 7.8.4 (KISPROGI 6497), so find is
read the bfs way (a path anywhere, -f PATH, -rm; an unknown dash word near the store is refused) and a grep with the
store folder as an operand counts as recursive (ugrep searches it without -r); the GNU tools by full path are covered
by the same reading.

The cwd is a set of places (KISPROGI 6524 P5): a cd adds its target and keeps the old place, since it may fail or sit
in a branch, a function body, a background job or a case arm; only the commands joined to it by && run in the target
alone. A path is checked under every place the command may be in, so "cd X; rm -rf store" from the install root is
refused (if the cd fails it removes the live store) and "cd X && rm -rf store" passes. Heredocs and comments are found
by one scan of the whole text with bash's quoting, arithmetic and expansion contexts (P2).

Data positions pass, so a message or a note that QUOTES such a command is not a write: curl's -d/--data*/--json/
-F/--form values (but not an @file value, which reads the file), echo/printf arguments and a heredoc fed to cat or
tee, unless the same pipeline feeds an interpreter.

A text brake, not an OS boundary (the same user can always build the name or the SQL at run time, e.g. from
variables, base64 or string concatenation, a cd to a variable or through shopt -s cdable_vars, or output that a
command other than echo / printf / cat <<< prints and transforms: rev, tr, sed). It closes the direct and the accidental routes; the dashboard itself
writes these files as before. Known routes it does not close (KISPROGI 6483, 6490): a recursive search from the
install root or above (grep -rn x . reads the store's hidden file; it cannot be told from everyday searches), a copy of
the whole store folder read afterwards (cp -r store /tmp/s, then /tmp/s/.d*), a script or an SQL file written in one
step and run in a later one (in the same call or a later one: echo ... > /tmp/x.sql; sqlite3 db ".read /tmp/x.sql"),
names listed by something other than find and fed to xargs (ls -a store | xargs ...) or read in a loop
(find store | while read f; do cat "$f"; done), a names-only search used as an oracle (grep -rl with a regex of
the secret's first characters, repeated: KISPROGI 6497 L3, the same class as a name built at run time), and sqlite3 .restore /
Connection.backup() INTO the live DB (it replaces the whole DB; the direction cannot be told from a legitimate backup in
text). KISPROGI 6539 Q9, the same "built at run time" class: a redefined cd (cd() { :; }, function cd, an alias with
expand_aliases), a symlink made in the same call (ln -s ROOT /tmp/m && cd /tmp/m), a command or name kept in a
variable (printf -v c ..., x=$(echo rm); $x ...), a file written earlier and read back (echo NAME > /tmp/l; xargs -a
/tmp/l rm), and an archive extracted into the store folder (tar -C store -x: its member names are not in the text).
Matched against a fixed vocabulary only: anything else passes. More than 32 possible working folders in one command
are refused (fail closed; KISPROGI 6539 Q10: the per-place check is quadratic). Unparsable Bash that mentions one of the
protected names is refused (fail closed); unparsable Bash without them passes. Above 200 KB only the text rules run,
and then the word "store" or a dot-name glob is refused too.

Protocol: JSON on stdin ({tool_name, tool_input, cwd}); a refusal is a PreToolUse deny decision on stdout, exit 0.
"""
import fnmatch
import glob as _glob
import json
import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
STORE = os.path.join(ROOT, "store")

TOTP_NAME = ".dashboard-totp.json"
TOTP_RX = re.compile(r"\.dashboard-totp", re.I)
DB_NAMES = ("claudeclaw.db", "claudeclaw.db-wal", "claudeclaw.db-shm", "claudeclaw.db-journal")
DB_RX = re.compile(r"claudeclaw\.db(?:-wal|-shm|-journal)?\b", re.I)

# KISPROGI 6483 H (measured in real SQLite, every one rewrote the row): an alias (AS u), NOT INDEXED / INDEXED BY, a
# comment in place of the white space, a line comment, a single-quoted identifier, and ATTACH with any schema prefix all
# passed the first version. KISPROGI 6490 H: then a quoted schema name with a non-word character ("m x", [m x], `m x`,
# "m-1", """q") and a quoted name with no space before it (UPDATE`t`SET, FROM"t", FROM[t]). Now: a schema prefix is any
# quoted identifier ("..." and `...` with their doubled quote inside, [...], '...') or a bare word, every quote may carry
# a backslash before it (raw code text), no white space is required before a quoted name, no SET is required after
# UPDATE <table>, a word boundary follows the name (dashboard_users_archive passes), and the text is matched with its SQL
# comments cut out as well as as it stands.
_SCHEMA = (r"(?:(?:\\?\"(?:[^\"]|\"\"){0,64}\\?\"|\\?`(?:[^`]|``){0,64}\\?`|\\?\[[^\]]{0,64}\\?\]"
           r"|\\?'(?:[^']|''){0,64}\\?'|\w+)\s*\.\s*)?")
_Q = r"\\?[\"`\['\]]?"
_TABLE = _SCHEMA + _Q + r"dashboard_users" + _Q + r"(?!\w)"
_SCHEMA_TABLE = _SCHEMA + _Q + r"sqlite_(?:temp_)?(?:master|schema)\b"
SQL_WRITE_RXS = [
    re.compile(r"\binsert\s+(?:or\s+\w+\s+)?into\s*" + _TABLE, re.I),
    re.compile(r"\breplace\s+into\s*" + _TABLE, re.I),
    re.compile(r"\bupdate\s*(?:or\s+\w+\s*)?" + _TABLE, re.I),
    re.compile(r"\bdelete\s+from\s*" + _TABLE, re.I),
    re.compile(r"\bdrop\s+table\s*(?:if\s+exists\s*)?" + _TABLE, re.I),
    re.compile(r"\balter\s+table\s*" + _TABLE, re.I),
    re.compile(r"\bcreate\s+(?:temp\w*\s+)?trigger\b[\s\S]{0,400}?\bon\s*" + _TABLE, re.I),
    re.compile(r"\bwritable_schema\b", re.I),
    re.compile(r"\b(?:insert\s+(?:or\s+\w+\s+)?into|update(?:\s+or\s+\w+)?|delete\s+from|replace\s+into)\s*" + _SCHEMA_TABLE, re.I),
    # the sqlite3 shell's .import inserts a file's rows into the named table (a new login row, no SQL keyword at all)
    re.compile(r"(?:^|[\s;'\"])\.import\s[^\n]{0,300}?\s" + _TABLE + r"\s*(?:$|[\n;'\"\\])", re.I),
]
_SQL_COMMENT = re.compile(r"/\*[\s\S]*?(?:\*/|$)|--[^\n]*")
# Interpreter code that hands the DB file to a file-writing call (R3 inside a script): the name inside the call's
# arguments, or a Path(...claudeclaw.db...) with a writing method. A bare sqlite3 connect is not a file write.
_DBN = r"claudeclaw\.db(?:-wal|-shm|-journal)?"
FILE_WRITE_API_RXS = [
    re.compile(r"(?:shutil\s*\.\s*\w+|os\s*\.\s*(?:remove|unlink|rename|replace|truncate)|\b(?:copyFileSync|renameSync|rmSync|"
               r"unlinkSync|writeFileSync|appendFileSync|truncateSync|createWriteStream|copyFile|rename|unlink|writeFile|rm))"
               r"\s*\([^)]{0,300}?" + _DBN, re.I),
    re.compile(r"\bopen\s*\(\s*[^,)]{0,200}?" + _DBN + r"[^,)]{0,40},\s*['\"][rbt]*[wax+]", re.I),
    re.compile(_DBN + r"[^)\n]{0,80}\)\s*\.\s*(?:unlink|write_bytes|write_text|rename|replace|touch|open\s*\(\s*['\"][rbt]*[wax+])", re.I),
]
# the sqlite3 shell's .backup / .save / .clone write a whole DB file to the path they name (the last word)
_SQLITE_DOT_WRITE_RX = re.compile(r"(?:^|[\s;'\"])\.(?:backup|save|clone)[ \t]+([^\n;]{1,400})", re.I)


def _sqlite_dot_writes_live_db(text: str, cwd: str) -> bool:
    """A .backup / .save / .clone whose target resolves to the live DB file (a backup INTO it replaces the whole DB). A
    target in a variable folder ($DEST/claudeclaw.db) is a backup copy, not the live file."""
    for m in _SQLITE_DOT_WRITE_RX.finditer(text):
        try:
            words = shlex.split(m.group(1).strip().rstrip("'\""))
        except ValueError:
            words = m.group(1).split()
        target = words[-1].strip("'\"") if words else ""
        if target and not _unknown_place(target) and _is_live_db_path(target, cwd):
            return True
    return False


def _code_writes_db(text: str) -> bool:
    return any(rx.search(text) for rx in FILE_WRITE_API_RXS)


INTERPRETERS = {
    "python", "python2", "python3", "pypy", "pypy3", "node", "nodejs", "bun", "deno", "perl", "ruby", "php",
    "sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "busybox", "sqlite3", "eval", "source", ".", "awk",
    "gawk", "mawk", "nawk", "osascript", "pwsh", "tclsh", "lua", "R", "Rscript",
}
# KISPROGI 6566 R3, R5, R6: ONE table for every program that runs another command after its own options. Each entry:
# sv = short option letters that take a value (bundled: -vC DIR, or attached: -CDIR), lv = long options that take a
# value (--opt VALUE or --opt=VALUE), pos = positional operands before the command word (timeout's DURATION), cd_s /
# cd_l = the options that move the command's working folder, root_s / root_l = those that change its root or its user's
# home (the command then runs in a place not known here), opt_s / opt_l = options whose value is optional and only
# attached (-wDIR, --wd=DIR; alone they mean "a place not known here"), split_s / split_l = options whose value is a
# command line (env -S), shell_s / shell_l = options whose value is shell text (flock -c), lv2 / lv3 = long options with
# two / three values (bwrap --bind SRC DEST), assigns = NAME=VALUE words before the command are environment, home =
# the command starts in a place not known here unless a cd option names one (systemd-run: the manager's folder).
# A program that is not here is the command itself. Adding a wrapper or a folder-changing option is one row.
WRAPPER_SPECS = {
    "env": dict(sv="uCS", lv={"--unset", "--chdir", "--split-string"}, cd_s="C", cd_l={"--chdir"}, split_s="S",
                split_l={"--split-string"}, assigns=True),
    "sudo": dict(sv="ugCDhprtUTR", lv={"--user", "--group", "--close-from", "--chdir", "--host", "--prompt", "--role",
                                      "--type", "--other-user", "--command-timeout", "--chroot"},
                 cd_s="D", cd_l={"--chdir"}, root_s="Ri", root_l={"--chroot", "--login"}, assigns=True),
    "doas": dict(sv="uC"),
    "nice": dict(sv="n", lv={"--adjustment"}),
    "nohup": {}, "setsid": {}, "unbuffer": {}, "chronic": {}, "builtin": {}, "command": {},
    "caffeinate": dict(sv="tw"),
    "exec": dict(sv="a"),
    "time": dict(sv="fo", lv={"--format", "--output"}),
    "timeout": dict(sv="ks", lv={"--kill-after", "--signal"}, pos=1),
    "stdbuf": dict(sv="ioe", lv={"--input", "--output", "--error"}),
    "ionice": dict(sv="cnpPu", lv={"--class", "--classdata", "--pid", "--pgid", "--uid"}),
    "chrt": dict(sv="TPD", lv={"--sched-runtime", "--sched-period", "--sched-deadline"}, pos=1),
    "taskset": dict(pos=1),
    "flock": dict(sv="wEc", lv={"--timeout", "--wait", "--conflict-exit-code", "--command"}, pos=1, shell_s="c",
                  shell_l={"--command"}),
    "xargs": dict(sv="ILPdEansa", lv={"--arg-file", "--delimiter", "--eof", "--max-lines", "--max-args", "--max-procs",
                                     "--max-chars", "--replace", "--process-slot-var"}),
    "unshare": dict(sv="SGRw", lv={"--setuid", "--setgid", "--root", "--wd", "--propagation", "--map-user",
                                   "--map-group", "--map-users", "--map-groups"},
                    cd_s="w", cd_l={"--wd"}, root_s="R", root_l={"--root"}),
    "nsenter": dict(sv="tSG", lv={"--target", "--setuid", "--setgid"}, opt_s="wr", opt_l={"--wd", "--root"},
                    cd_s="w", cd_l={"--wd"}, root_s="r", root_l={"--root"}),
    "bwrap": dict(lv={"--chdir", "--tmpfs", "--dev", "--proc", "--dir", "--remount-ro", "--unsetenv", "--hostname",
                      "--uid", "--gid", "--exec-label", "--file-label", "--seccomp", "--add-seccomp-fd", "--info-fd",
                      "--json-status-fd", "--block-fd", "--userns-block-fd", "--sync-fd", "--lock-file", "--cap-add",
                      "--cap-drop", "--perms", "--size", "--args", "--userns", "--userns2", "--pidns", "--argv0",
                      "--mqueue", "--overlay-src", "--tmp-overlay", "--ro-overlay"},
                  lv2={"--bind", "--bind-try", "--ro-bind", "--ro-bind-try", "--dev-bind", "--dev-bind-try",
                       "--setenv", "--symlink", "--file", "--bind-data", "--ro-bind-data", "--chmod", "--bind-fd",
                       "--ro-bind-fd"},
                  lv3={"--overlay"}, cd_l={"--chdir"}),
    "systemd-run": dict(sv="pEHMu", lv={"--unit", "--property", "--description", "--slot", "--setenv", "--uid",
                                        "--gid", "--nice", "--working-directory", "--on-active", "--on-boot",
                                        "--on-startup", "--on-unit-active", "--on-unit-inactive", "--on-calendar",
                                        "--path-property", "--socket-property", "--timer-property", "--host",
                                        "--machine", "--service-type", "--background"},
                        cd_l={"--working-directory"}, here={"-d", "--same-dir"}, home=True),
    "then": {}, "do": {}, "else": {}, "if": {}, "while": {}, "until": {}, "!": {}, "{": {}, "(": {}, "elif": {},
}
WRAPPERS = set(WRAPPER_SPECS)
_ASSIGN_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)


def _unwrap(words):
    """Walk the assignments and wrappers before a command word (WRAPPER_SPECS). Returns a dict: idx (the command
    word's index, len(words) when there is none), chdirs (each folder value a wrapper moves the command to, None for a
    place not known here), splits (env -S strings), shells (flock -c texts), assigns ([(NAME, VALUE)] before the
    command, the env wrapper's too), wrappers (their names)."""
    out = {"chdirs": [], "splits": [], "shells": [], "assigns": [], "wrappers": []}
    i = 0
    while i < len(words):
        w = _unmark(words[i])
        if _ASSIGN_RX.fullmatch(w):
            out["assigns"].append(tuple(w.split("=", 1)))
            i += 1
            continue
        spec = WRAPPER_SPECS.get(os.path.basename(w))
        if spec is None:
            break
        out["wrappers"].append(os.path.basename(w))
        i += 1
        sv, lv = spec.get("sv", ""), spec.get("lv", set())
        lv2, lv3 = spec.get("lv2", set()), spec.get("lv3", set())
        cd_s, cd_l = spec.get("cd_s", ""), spec.get("cd_l", set())
        root_s, root_l = spec.get("root_s", ""), spec.get("root_l", set())
        opt_s, opt_l = spec.get("opt_s", ""), spec.get("opt_l", set())
        moved = False

        def take(kind, value):
            nonlocal moved
            if kind == "cd":
                out["chdirs"].append(value)
                moved = True
            elif kind == "root":
                out["chdirs"].append(None)
                moved = True
            elif kind == "split":
                out["splits"].append(value)
            elif kind == "shell":
                out["shells"].append(value)

        def kind_of(letter=None, long=None):
            if letter is not None:
                return ("cd" if letter in cd_s else "root" if letter in root_s else
                        "split" if letter in spec.get("split_s", "") else "shell" if letter in spec.get("shell_s", "") else None)
            return ("cd" if long in cd_l else "root" if long in root_l else "split" if long in spec.get("split_l", set())
                    else "shell" if long in spec.get("shell_l", set()) else None)

        pos = spec.get("pos", 0)
        while i < len(words):
            o = _unmark(words[i])
            if o == "--":
                i += 1
                break
            if spec.get("assigns") and _ASSIGN_RX.fullmatch(o):
                out["assigns"].append(tuple(o.split("=", 1)))
                i += 1
                continue
            if o in spec.get("here", set()):
                i += 1
                continue
            if o.startswith("--") and len(o) > 2:
                name, eq, val = o.partition("=")
                if name in opt_l:
                    take(kind_of(long=name), val if eq else None)
                    i += 1
                    continue
                if name in lv or name in lv2 or name in lv3:
                    n_vals = 3 if name in lv3 else 2 if name in lv2 else 1
                    if eq:
                        vals, i = [val], i + 1
                    else:
                        vals, i = [_unmark(v) for v in words[i + 1:i + 1 + n_vals]], i + 1 + n_vals
                    k = kind_of(long=name)
                    if k and vals:
                        take(k, vals[0])
                    continue
                k = kind_of(long=name)
                if k == "root":
                    take(k, None)
                i += 1
                continue
            if re.fullmatch(r"-\d[\d.]*[smhd]?", o):
                i += 1  # an old-style number option (nice -5)
                continue
            if o.startswith("-") and len(o) > 1:
                j = 1
                while j < len(o):
                    letter = o[j]
                    if letter in opt_s:
                        take(kind_of(letter=letter), o[j + 1:] or None)
                        break
                    if letter in sv:
                        val = o[j + 1:]
                        if not val and i + 1 < len(words):
                            i += 1
                            val = _unmark(words[i])
                        k = kind_of(letter=letter)
                        if k:
                            take(k, val)
                        break
                    if kind_of(letter=letter) == "root":
                        take("root", None)
                    j += 1
                i += 1
                continue
            if pos > 0:
                pos -= 1
                i += 1
                continue
            break
        if spec.get("home") and not moved:
            out["chdirs"].append(None)
    out["idx"] = i
    return out


# KISPROGI 6566 R2, R3, R4: commands whose OWN options move the folder they read or write in, in the same shape as
# WRAPPER_SPECS (sv / lv / cd_s / cd_l). cumulative: a second -C is relative to the first (make, git). old_style: tar's
# first word may be a dash-less letter bundle whose value letters take the following words in order (tar cvfC - DIR).
# stop_at_operand: the options end at the first operand (git's subcommand). worktree: the long option and the
# environment variable that name git's work tree (git clean deletes there).
PROGRAM_SPECS = {
    "tar": dict(sv="bCfFgHIKLNTVX", lv={"--directory", "--file", "--files-from", "--exclude-from", "--use-compress-program",
                                       "--to-command", "--info-script", "--new-volume-script", "--rsh-command",
                                       "--index-file", "--format", "--label", "--newer", "--after-date", "--exclude",
                                       "--transform", "--xform", "--owner", "--group", "--mode", "--mtime", "--level",
                                       "--listed-incremental", "--suffix", "--volno-file", "--tape-length",
                                       "--blocking-factor", "--record-size", "--starting-file", "--checkpoint-action",
                                       "--strip-components", "--newer-mtime", "--atime-preserve", "--sparse-version"},
                cd_s="C", cd_l={"--directory"}, old_style=True),
    "make": dict(sv="CfIoWjl", lv={"--directory", "--file", "--makefile", "--include-dir", "--old-file", "--assume-old",
                                  "--what-if", "--new-file", "--assume-new", "--eval", "--jobs", "--load-average"},
                 cd_s="C", cd_l={"--directory"}, cumulative=True),
    "git": dict(sv="Cc", lv={"--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix",
                             "--list-cmds", "--attr-source"},
                cd_s="C", cumulative=True, stop_at_operand=True, worktree=("--work-tree", "GIT_WORK_TREE")),
}
for _alias, _base in (("gtar", "tar"), ("bsdtar", "tar"), ("gmake", "make")):
    PROGRAM_SPECS[_alias] = PROGRAM_SPECS[_base]


def _program_options(head: str, args):
    """Parse a PROGRAM_SPECS command's own options. Returns {"opts": [(option, value or None, operand index)],
    "operands": [words], "rest": the words after git's subcommand option block (its subcommand first)}. The
    operand index of an option is how many operands came before it (tar's -C applies to the operands after it)."""
    spec = PROGRAM_SPECS[head]
    sv, lv = spec.get("sv", ""), spec.get("lv", set())
    words = [_unmark(a) for a in args]
    opts, operands, i = [], [], 0
    if spec.get("old_style") and words and not words[0].startswith("-") and re.fullmatch(r"[A-Za-z]+", words[0]):
        letters, i = words[0], 1
        for letter in letters:
            if letter in sv and i < len(words):
                opts.append(("-" + letter, words[i], 0))
                i += 1
            else:
                opts.append(("-" + letter, None, 0))
    while i < len(words):
        o = words[i]
        if o == "--":
            operands += words[i + 1:]
            break
        if o.startswith("--") and len(o) > 2:
            name, eq, val = o.partition("=")
            if name in lv:
                if not eq:
                    val = words[i + 1] if i + 1 < len(words) else ""
                    i += 1
                opts.append((name, val, len(operands)))
            else:
                opts.append((name, val if eq else None, len(operands)))
            i += 1
            continue
        if o.startswith("-") and len(o) > 1:
            j = 1
            while j < len(o):
                letter = o[j]
                if letter in sv:
                    val = o[j + 1:]
                    if not val and i + 1 < len(words):
                        i += 1
                        val = words[i]
                    opts.append(("-" + letter, val, len(operands)))
                    break
                opts.append(("-" + letter, None, len(operands)))
                j += 1
            i += 1
            continue
        if spec.get("stop_at_operand"):
            return {"opts": opts, "operands": [], "rest": words[i:]}
        operands.append(o)
        i += 1
    return {"opts": opts, "operands": operands, "rest": []}


def _program_folders(head: str, args, cwd: str, assigns=(), place=None):
    """The folders a PROGRAM_SPECS command moves to with its own options, in order (cumulative where the program says
    so), plus git's work tree (--work-tree, or GIT_WORK_TREE among the assignments) resolved from the last one. A
    variable or glob folder is UNKNOWN_CWD."""
    spec = PROGRAM_SPECS[head]
    place = place or (lambda v, base: _resolve(v, base))
    out, d = [], cwd
    parsed = _program_options(head, args)
    cd_s, cd_l = spec.get("cd_s", ""), spec.get("cd_l", set())
    for name, val, _k in parsed["opts"]:
        if val is None:
            continue
        if (name[1:] in cd_s and not name.startswith("--")) or name in cd_l:
            base = d if spec.get("cumulative") else cwd
            d2 = UNKNOWN_CWD if base == UNKNOWN_CWD and not os.path.isabs(os.path.expanduser(val)) else place(val, base)
            if spec.get("cumulative"):
                d = d2
            out.append(d2)
    wt = spec.get("worktree")
    if wt:
        vals = [v for name, v, _k in parsed["opts"] if name == wt[0] and v is not None]
        vals += [v for n, v in assigns if n == wt[1]]
        for v in vals:
            out.append(UNKNOWN_CWD if d == UNKNOWN_CWD and not os.path.isabs(os.path.expanduser(v)) else place(v, d))
    return out


DATA_ONLY_HEADS = {"echo", "printf"}
STDIN_TO_FILE_HEADS = {"cat", "tee"}
CURL_DATA_FLAGS = {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode", "--json",
                   "-F", "--form", "--form-string"}
METADATA_HEADS = {"ls", "stat", "test", "[", "[[", "realpath", "readlink", "dirname", "basename"}
SEARCH_HEADS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "zgrep", "rgrep", "ugrep"}
FILE_OP_HEADS = {"rm", "unlink", "shred", "truncate", "srm", "trash", "trash-put", "mv", "cp", "install", "rsync", "scp", "ln",
                 "link", "ditto", "dd", "tee", "sed", "gsed", "perl"}
SEPARATORS = {";", "&", "&&", "||", "\n", ";;", "(", ")"}
PIPES = {"|", "|&"}
REDIRS = {">", ">>", ">|", "&>", "&>>", "<", "<>", "<<<", ">&", "<&"}

MSG = (
    "AUTH-STORE KAPU (BELA 6410): TILTVA. A dashboard-belepes masodik faktorahoz (store/.dashboard-totp.json, "
    "a dashboard_users tabla, a store/claudeclaw.db fajl) ugynok nem nyulhat: {why}. "
    "Ezeket csak a dashboard maga irja (Beallitasok, TOTP), vagy a tulajdonos a CLI-vel (npm run dashboard-user). "
    "Ha egy uzenetben IDEZED a parancsot, kuldd adatkent (curl -d, echo/printf, cat <<EOF fajlba), ne futtasd."
)


class Deny(Exception):
    pass


def _deny(why: str) -> None:
    raise Deny(why)


# --------------------------------------------------------------------------------------------------------------
# Path checks
# --------------------------------------------------------------------------------------------------------------

UNKNOWN_CWD = "\0unknown-cwd"
BIG_TEXT = 200 * 1024
_BIG_TEXT_STORE_RX = re.compile(r"\bstore\b|(?<![\w.])\.[\w.-]{0,40}[*?\[{]")


def _resolve(path: str, cwd: str) -> str:
    """The real path; a relative path under an unknown cwd (a cd to a variable) resolves to a place that is never the
    store, so only a spelled-out name can still match there (the callers check the name for unknown places)."""
    if re.match(r"~-(?:/|$)", path):
        return UNKNOWN_CWD  # bash's ~- is the previous cwd (KISPROGI 6524 P5): not known here
    if re.match(r"~\+(?:/|$)", path):
        path = "." + path[2:]  # ~+ is the cwd itself
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        if cwd == UNKNOWN_CWD:
            return UNKNOWN_CWD
        p = os.path.join(cwd or ROOT, p)
    try:
        return os.path.realpath(p)
    except (OSError, ValueError):
        return os.path.normpath(p)


def _is_totp_path(path: str, cwd: str) -> bool:
    if TOTP_RX.search(path):
        return True
    return _resolve(path, cwd) == os.path.join(os.path.realpath(STORE), TOTP_NAME)


def _db_name_matches(base: str) -> bool:
    """A DB file name, or a glob that matches one (claudeclaw.*, *.db, claudeclaw.db*)."""
    b = base.lower()
    if b in DB_NAMES:
        return True
    if re.search(r"[*?\[]", b):
        try:
            return any(fnmatch.fnmatchcase(n, b) for n in DB_NAMES)
        except re.error:
            return True
    return False


def _unknown_place(path: str) -> bool:
    """A variable, a substitution, or find's {} (the matched path, e.g. "{}/claudeclaw.db" under -exec or xargs -I{})."""
    return bool(re.search(r"[$`]|__SUBST\d+__|\{\}|^~-(?:/|$)", path))


_BRACE_LIST_RX = re.compile(r"\{([^{}]*,[^{}]*)\}")
_BRACE_SEQ_RX = re.compile(r"\{(-?\d{1,4}|[A-Za-z])\.\.(-?\d{1,4}|[A-Za-z])\}")


def _brace_variants(word: str, limit: int = 64):
    """Bash brace expansion of a word (store/.dashboard-{totp,x}.json, claudeclaw.d{a..c}, {store,x}), innermost first,
    at most `limit` words; a word without a brace list or sequence is returned as it is."""
    out = [word]
    for _ in range(8):
        nxt, changed = [], False
        for w in out:
            m = _BRACE_LIST_RX.search(w)
            if m:
                parts = m.group(1).split(",")
            else:
                m = _BRACE_SEQ_RX.search(w)
                if m:
                    a, b = m.group(1), m.group(2)
                    if a.lstrip("-").isdigit() and b.lstrip("-").isdigit():
                        lo, hi = sorted((int(a), int(b)))
                        parts = [str(k) for k in range(lo, min(hi, lo + limit) + 1)]
                    elif a.isalpha() and b.isalpha():
                        lo, hi = sorted((ord(a), ord(b)))
                        parts = [chr(k) for k in range(lo, min(hi, lo + limit) + 1)]
                    else:
                        parts = None
                    if parts is None:
                        m = None
            if m:
                changed = True
                nxt.extend(w[:m.start()] + p + w[m.end():] for p in parts)
            else:
                nxt.append(w)
        out = nxt[:limit]
        if not changed:
            break
    return out


def _any_variant(fn, word: str, cwd: str) -> bool:
    return any(fn(v, cwd) for v in (_brace_variants(word) if "{" in word else [word]))


def _is_live_db_path(path: str, cwd: str) -> bool:
    """The live DB file (or a sidecar): its folder resolves to the live store and its name (or glob) matches; or the
    place is not known (a variable, a substitution) and the name is spelled out. Brace lists count as their words."""
    return _any_variant(_is_live_db_path1, path, cwd)


def _is_live_db_path1(path: str, cwd: str) -> bool:
    base = os.path.basename(path.rstrip("/"))
    if _unknown_place(path):
        return bool(DB_RX.search(base))
    if not _db_name_matches(base):
        return False
    where = _resolve(os.path.dirname(path) or ".", cwd)
    if where == UNKNOWN_CWD:
        return bool(DB_RX.search(base))
    return _is_store_dir(os.path.dirname(path) or ".", cwd)  # a glob folder counts (rm st*/claudeclaw.db)


def _is_live_totp_word(word: str, cwd: str) -> bool:
    """A Bash word naming the live second-factor store: its folder resolves to the live store and the name (or a glob)
    matches it; or the place is not known and the name is spelled out. A worktree's or a test copy's own store is not
    the live one (resolved through links, so a link to the live store still counts). Brace lists count as their words."""
    return _any_variant(_is_live_totp_word1, word[1:] if word.startswith("@") else word, cwd)


def _is_live_totp_word1(w: str, cwd: str) -> bool:
    base = os.path.basename(w.rstrip("/"))
    if _unknown_place(w):
        return bool(TOTP_RX.search(w))
    if not (TOTP_RX.search(base) or _glob_matches_totp(base)):
        return False
    where = _resolve(os.path.dirname(w) or ".", cwd)
    if where == UNKNOWN_CWD:
        return bool(TOTP_RX.search(base))
    return _is_store_dir(os.path.dirname(w) or ".", cwd)  # a glob folder counts (cat st*/.dashboard-totp.json)


def _store_and_ancestors():
    store = os.path.realpath(STORE)
    out = [store]
    while os.path.dirname(out[-1]) != out[-1]:
        out.append(os.path.dirname(out[-1]))
    return out


def _glob_names_dir(word: str, cwd: str, targets) -> bool:
    """Whether a Bash glob word can expand to one of the target folders: component by component, as bash matches it (a
    * stays inside one component and does not match a leading dot)."""
    p = os.path.expanduser(word.rstrip("/") or "/")
    if not os.path.isabs(p):
        if cwd == UNKNOWN_CWD:
            return False
        p = os.path.join(os.path.realpath(cwd or ROOT), p)
    pc = [c for c in os.path.normpath(p).split(os.sep) if c]
    for t in targets:
        tc = [c for c in t.split(os.sep) if c]
        if len(tc) == len(pc) and all(fnmatch.fnmatchcase(a, b) and not (a.startswith(".") and not b.startswith((".", "[")))
                                      for a, b in zip(tc, pc)):
            return True
    return False


def _is_store_or_ancestor(path: str, cwd: str) -> bool:
    """The live store folder itself or a folder that holds it (removing or moving it removes the live DB); a brace list
    or a glob that can expand to one of them counts (rm -rf st*, rm -rf {store,x}). A variable is not known here."""
    return _any_variant(_is_store_or_ancestor1, path, cwd)


def _is_store_or_ancestor1(path: str, cwd: str) -> bool:
    if re.search(r"[$`{]|__SUBST\d+__", path):
        return False
    if re.search(r"[*?\[]", path):
        return _glob_names_dir(path, cwd, _store_and_ancestors())
    real = _resolve(path, cwd).rstrip(os.sep) or os.sep
    store = os.path.realpath(STORE)
    return real == store or store.startswith(real + os.sep) or real == os.sep


def _is_store_dir(path: str, cwd: str) -> bool:
    """The live store folder itself (a glob or brace list that can expand to it counts)."""
    def one(p: str, c: str) -> bool:
        if re.search(r"[$`{]|__SUBST\d+__", p):
            return False
        if re.search(r"[*?\[]", p):
            return _glob_names_dir(p, c, [os.path.realpath(STORE)])
        return _resolve(p, c) == os.path.realpath(STORE)
    return _any_variant(one, path, cwd)


def _glob_matches_totp(word: str) -> bool:
    if not re.search(r"[*?\[]", word):
        return False
    base = word.rstrip("/").split("/")[-1]
    if not base:
        return False
    # bash: a leading * or ? does not match a leading dot (no dotglob)
    if not base.startswith(".") and not base.startswith("["):
        return False
    try:
        return fnmatch.fnmatchcase(TOTP_NAME, base)
    except re.error:
        return True


def _totp_word(word: str) -> bool:
    vs = _brace_variants(word) if "{" in word else [word]
    return any(bool(TOTP_RX.search(v)) or _glob_matches_totp(v) for v in vs)


def _sql_write(text: str) -> bool:
    cut = _SQL_COMMENT.sub(" ", text)
    return any(rx.search(cut) or rx.search(text) for rx in SQL_WRITE_RXS)


# KISPROGI 6566 R1: the file and process calls of code interpreters, in one table for every language (Python, Perl,
# Ruby, PHP, Node, Lua, R, Tcl, awk). Three kinds of call:
#   delete: a call that deletes, moves away or truncates its first argument (no parentheses needed in Perl's
#           unlink "x" / rmtree "x", or Tcl's file delete x); the argument is checked like a path word of rm;
#   shell:  a call that runs a shell command line (system, exec, popen, shell_exec, backticks, qx{}, os.execute,
#           subprocess.*): its string arguments, joined, are checked as shell;
#   chdir:  chdir / os.chdir / Dir.chdir / process.chdir / setwd / cd: the strings above are resolved there too (a
#           non-string argument is a place not known here).
# A module or object prefix (os., FileUtils., fs., File::Path::) is allowed before any name. Adding a name is one word.
_CODE_PREFIX = r"(?<![\w$@%-])(?:[A-Za-z_]\w*\s*(?:\.|::|->)\s*)*"
# File-specific names count with any prefix; the generic ones (h.delete("key") is a hash) only bare or after a file
# module (os.remove, File.delete, FileUtils.mv, fs.rename, file.remove in R).
_CODE_DELETE_NAMES = ("unlink|unlinkSync|unlink_file|removeSync|remove_file|remove_dir|remove_dir_all|remove_tree|"
                      "rmtree|rmdir|rmdirSync|rm_rf|rm_r|rm_f|rmSync|deleteSync|truncateSync|renameSync|emptyDir|"
                      "emptyDirSync|outputFile|outputFileSync")
_CODE_DELETE_GENERIC = "remove|delete|truncate|rename|move|mv|rm"
_CODE_FILE_MODULES = "os|shutil|FileUtils|File|fs|fsp|fse|fsPromises|promises|Dir|Deno|Bun|file|FS|Path|pathlib"
_CODE_DELETE_RX = re.compile(
    _CODE_PREFIX + r"(?:" + _CODE_DELETE_NAMES + r")\s*\("
    r"|(?<![\w$@%.>:-])(?:(?:" + _CODE_FILE_MODULES + r"|require\s*\(\s*['\"][^'\"]*['\"]\s*\))\s*(?:\.|::)\s*)*"
    r"(?:" + _CODE_DELETE_GENERIC + r")\s*\(")
_CODE_DELETE_BARE_RX = re.compile(r"(?<![\w$@%.>:-])(?:unlink|rmtree|remove_tree|rename|truncate)\s+(?=['\"]|glob\b)")
_CODE_TCL_DELETE_RX = re.compile(r"\bfile\s+(?:delete|rename)\s+(?:-force\s+)?(?:--\s+)?")
_CODE_SHELL_NAMES = ("system|exec|execSync|execFile|execFileSync|spawn|spawnSync|popen|Popen|check_call|check_output|"
                     "getoutput|getstatusoutput|shell_exec|passthru|proc_open|pcntl_exec|capture2|capture2e|capture3|popen3")
_CODE_SHELL_RX = re.compile(
    _CODE_PREFIX + r"(?:" + _CODE_SHELL_NAMES + r")\b\s*(?=[(\s'\"])"
    r"|(?<![\w$-])(?:subprocess|sp|os|Open3|IO|io|Kernel|child_process|cp|Process|Bun|Deno|Runtime)\s*(?:\.|::)\s*"
    r"(?:run|call|execute|spawn|Command|exec)\b\s*(?=[(\s'\"])")
_CODE_BACKTICK_RX = re.compile(r"`([^`]{1,2000})`|\bqx\s*([{(\[<])|%x\s*([{(\[<])")
_CODE_CHDIR_RX = re.compile(_CODE_PREFIX + r"(?:chdir|setwd|Set-Location|cd)\s*(?=[(\s'\"])\(?\s*")
_CODE_LITERAL_RX = re.compile(r"""[rRbBfFuU]{0,2}(?:'((?:[^'\\\n]|\\.){0,2000})'|"((?:[^"\\\n]|\\.){0,2000})")""")
_CODE_PATH_CALL_RX = re.compile(r"(?:[A-Za-z_]\w*\s*(?:\.|::)\s*)*(?:Path|join|realpath|abspath|resolve|expand_path)\s*\(")
_CLOSERS = {"(": ")", "{": "}", "[": "]", "<": ">"}


def _code_literal(m) -> str:
    raw = m.group(1) if m.group(1) is not None else m.group(2)
    return re.sub(r"\\(.)", r"\1", raw)


def _call_span(text: str, i: int) -> str:
    """The argument text of a call starting at i: a balanced (...) when it starts with "(", else up to the end of the
    statement (; or newline outside quotes). Quote-aware, at most 4000 characters."""
    while i < len(text) and text[i] in " \t":
        i += 1
    start, depth, q, j = i, 0, None, i
    paren = i < len(text) and text[i] == "("
    while j < len(text) and j - start < 4000:
        c = text[j]
        if q:
            if c == "\\":
                j += 2
                continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if paren and depth == 0:
                return text[start + 1:j]
            if depth < 0:
                break
        elif c in ";\n" and depth == 0:
            break
        j += 1
    return text[start + (1 if paren else 0):j]


def _first_path_argument(text: str, i: int):
    """The first argument of a delete call at i when it is a string (or a path constructor of strings: Path("x"),
    os.path.join("store", "claudeclaw.db"), glob("...")); None for a variable or an expression."""
    while i < len(text) and text[i] in " \t(":
        i += 1
    if text.startswith("glob", i):
        i += 4
        while i < len(text) and text[i] in " \t(":
            i += 1
    m = _CODE_PATH_CALL_RX.match(text, i)
    if m:
        lits = [_code_literal(x) for x in _CODE_LITERAL_RX.finditer(_call_span(text, m.end() - 1))]
        return "/".join(lits) if lits else None
    m = _CODE_LITERAL_RX.match(text, i)
    return _code_literal(m) if m else None


def _mask_literals(text: str) -> str:
    """The text with every string literal's content (and backticks') blanked out, same length: a call name is only
    looked for in code, never inside a string ("-exec" in a Python list is no exec call)."""
    out, q, i = list(text), None, 0
    while i < len(text):
        c = text[i]
        if q:
            if c == "\\" and i + 1 < len(text):
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if len(q) == 3 and text.startswith(q, i):
                q, i = None, i + 3  # the end of a Python triple-quoted string
                continue
            if len(q) == 1 and (c == q or c == "\n"):
                q = None
            else:
                out[i] = " "
        elif c in "'\"":
            q = c * 3 if text.startswith(c * 3, i) else c
            i += len(q)
            continue
        i += 1
    return "".join(out)


_BACKTICK_SHELL_LANGS = re.compile(r"(?:perl|ruby|jruby|truffleruby|php)[\d.]*")


def _code_call_checks(text: str, cwd: str, depth: int, lang: str = "") -> None:
    """KISPROGI 6566 R1: the delete / shell / chdir calls of interpreter code (see _CODE_DELETE_NAMES). The names are
    matched in the code with its strings blanked out; the strings themselves are read from the original text."""
    code = _mask_literals(text)
    places = [cwd]
    for m in _CODE_CHDIR_RX.finditer(code):
        lit = _CODE_LITERAL_RX.match(text, m.end())
        if lit:
            target = _code_literal(lit)
            places.append(UNKNOWN_CWD if _unknown_place(target) or (cwd == UNKNOWN_CWD and not os.path.isabs(target))
                          else _resolve(target, cwd))
        else:
            places.append(UNKNOWN_CWD)
    places = list(dict.fromkeys(places))
    targets = []
    for rx in (_CODE_DELETE_RX, _CODE_DELETE_BARE_RX):
        for m in rx.finditer(code):
            arg = _first_path_argument(text, m.end() - (1 if text[m.end() - 1:m.end()] == "(" else 0))
            if arg is not None:
                targets.append(arg)
    for m in _CODE_TCL_DELETE_RX.finditer(code):
        lit = _CODE_LITERAL_RX.match(text, m.end())
        word = _code_literal(lit) if lit else (re.match(r"[^\s;\]}]+", text[m.end():]) or [None])[0]
        if word:
            targets.append(word)
    for t in targets:
        if "#{" in t or "${" in t:
            continue  # an interpolated string: a variable place, the known limit
        for c in places:
            if _names_protected(t, c):
                _deny("a dashboard adatbazis-fajljanak vagy a store mappanak torlese egy ertelmezo kodjaban")
    if depth >= 4:
        return
    shells = []
    for m in _CODE_SHELL_RX.finditer(code):
        lits = [_code_literal(x) for x in _CODE_LITERAL_RX.finditer(_call_span(text, m.end()))]
        if lits:
            shells.append(" ".join(lits))
    for m in _CODE_BACKTICK_RX.finditer(text if _BACKTICK_SHELL_LANGS.fullmatch(lang or "") else ""):
        if code[m.start()] != text[m.start()]:
            continue  # a backtick inside a string literal: text, not a command
        if m.group(1) is not None:
            shells.append(m.group(1))
        else:
            opener = m.group(2) or m.group(3)
            end = text.find(_CLOSERS[opener], m.end())
            if end > m.end():
                shells.append(text[m.end():end])
    for sh in shells:
        for c in places:
            check_bash(sh, c, depth + 1, best_effort=True)


def _code_text_checks(text: str, where: str, cwd: str, depth: int, code: str = "") -> None:
    """Text a program runs: all three rules, and shell text is parsed again (a nested command). code: the text is a
    code interpreter's (not a shell's; its name): its delete / shell / chdir calls are checked too (KISPROGI 6566 R1)."""
    if _sql_write(text):
        _deny(f"SQL-iras a dashboard_users tablara ({where})")
    if TOTP_RX.search(text):
        _deny(f"a masodik faktor tara egy futtatott szovegben ({where})")
    if _code_writes_db(text) or _sqlite_dot_writes_live_db(text, cwd):
        _deny(f"a dashboard adatbazis-fajljanak irasa egy futtatott kodban ({where})")
    if code:
        _code_call_checks(text, cwd, depth, code)
    if depth < 4 and re.search(r"[;&|<>$`\n]|\b(?:rm|mv|cp|tee|dd|truncate|ln|install|rsync|find)\b", text):
        try:
            check_bash(text, cwd, depth + 1, best_effort=True)
        except Deny:
            raise
        except Exception:
            pass  # not shell (Python, SQL, JS): the three text rules above already ran


# --------------------------------------------------------------------------------------------------------------
# Bash: heredocs and substitutions out first, then words, segments and pipelines
# --------------------------------------------------------------------------------------------------------------

# The heredoc operator and its delimiter word, matched at a "<<" the scanner has already found in shell code (so a
# here-string's "<<<" never gets here).
_ASSIGN_INDEX_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\[")
_COMPOUND_ASSIGN_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=\(")
_MARKER_RX = re.compile(r"__HEREDOC\d+__")
_CODE_CTX = ("top", "cs", "bq")  # shell code: a heredoc starts here (and a comment, except inside backticks)


def _assignment_index_end(cmd: str, i: int) -> int:
    """For NAME[ at i: the index of its "[" when the word is an array assignment (NAME[...]= or NAME[...]+=), else -1.
    bash reads the index as arithmetic there (a[1<<2]=x), but in any other word a << is a heredoc (echo a[1<<2])."""
    m = _ASSIGN_INDEX_RX.match(cmd, i)
    if not m:
        return -1
    j, depth = m.end(), 1
    while j < len(cmd) and depth:
        if cmd[j] == "[":
            depth += 1
        elif cmd[j] == "]":
            depth -= 1
        elif cmd[j] == "\n":
            return -1
        j += 1
    return m.end() - 1 if depth == 0 and (cmd.startswith("=", j) or cmd.startswith("+=", j)) else -1


def _heredoc_word(cmd: str, j: int):
    r"""The heredoc delimiter word starting at j, as bash reads it (KISPROGI 6539 Q7): one shell word, its quotes
    removed ('E'O"F" and E\OF are EOF), a backslash-newline joining it. (delimiter, quoted, end) or None when no word
    follows (bash: a syntax error). quoted: any quote or escape in it, so the body is plain data (no expansion)."""
    n = len(cmd)
    while j < n and cmd[j] in " \t":
        j += 1
    word, quoted, k = [], False, j
    while k < n:
        ch = cmd[k]
        if ch in " \t\n;&|()<>":
            break
        if ch == "\\":
            if k + 1 < n and cmd[k + 1] == "\n":
                k += 2
                continue
            if k + 1 < n:
                word.append(cmd[k + 1])
                quoted = True
            k += 2
            continue
        if ch == "'":
            end = cmd.find("'", k + 1)
            if end < 0:
                return None
            word.append(cmd[k + 1:end])
            quoted, k = True, end + 1
            continue
        if ch == "$" and k + 1 < n and cmd[k + 1] == "'":
            e = k + 2
            while e < n and cmd[e] != "'":
                e += 2 if cmd[e] == "\\" else 1
            if e >= n:
                return None
            word.append(_ansi_c_decode(cmd[k + 2:e]))
            quoted, k = True, e + 1
            continue
        if ch == '"':
            e, part = k + 1, []
            while e < n and cmd[e] != '"':
                if cmd[e] == "\\" and e + 1 < n and cmd[e + 1] in '"\\$`\n':
                    if cmd[e + 1] != "\n":
                        part.append(cmd[e + 1])
                    e += 2
                    continue
                part.append(cmd[e])
                e += 1
            if e >= n:
                return None
            word.append("".join(part))
            quoted, k = True, e + 1
            continue
        word.append(ch)
        k += 1
    if not word and not quoted:
        return None
    return "".join(word), quoted, k


def _strip_heredocs(cmd: str):
    r"""Heredoc bodies and comments out of the text: (text with a marker word per heredoc, {marker: body},
    {marker: {"delim", "quoted", "ref"}}).

    One left-to-right scan of the WHOLE text with bash's lexical contexts (KISPROGI 6524 P2): quotes ('...', "...",
    $'...') carry across lines, a backslash escapes, and a << is a heredoc only in shell code: not after an escape
    (echo \<<2), not in a comment (true # <<2), not in arithmetic ($((...)), ((...)), $[...], also over several lines),
    not in ${...} (${PATH:1<<2}), not in an array assignment's index (a[1<<2]=x) and not in a compound array assignment
    (a=(1<<2), a=([1<<2]=x); KISPROGI 6539 Q7). Read as heredocs, each of these hid every following line from the gate.
    A comment (a # starting a word in shell code, not inside backticks) is cut out as bash does, so a quote inside it
    (# it's) can no longer swallow the following lines into one quoted word. KISPROGI 6539 Q2: a backslash-newline is
    removed as bash's reader does (outside quotes and inside "..."; not in '...', $'...' or a comment), so r\<nl>m is
    rm. The delimiter is a whole shell word with its quotes removed (Q7); an unquoted heredoc's body has its
    backslash-newlines joined too, and is marked unquoted (its $(...) and `...` run: Q3). The bodies start after the
    next newline in shell code, one per pending heredoc, each up to its delimiter line. A delimiter that is a marker
    (__HEREDOCn__, as an outer scan left it in a substitution's text) takes no body lines: its body is the outer one
    ("ref"), so an inner scan can no longer swallow the substitution's remaining lines as a body (found 2026-11-28)."""
    out, bodies, meta, pending = [], {}, {}, []
    stack = [["top", 0]]
    i, n, count, word_start = 0, len(cmd), 0, True

    def push(kind: str, depth: int = 0) -> None:
        stack.append([kind, depth])

    while i < n:
        kind = stack[-1][0]
        c = cmd[i]
        if c == "\n" and kind in _CODE_CTX:
            out.append(c)
            i += 1
            word_start = True
            for marker, delim, strip_tabs, quoted in pending:
                body = []
                while i < n:
                    j = cmd.find("\n", i)
                    j = n if j < 0 else j
                    line = cmd[i:j]
                    i = j + 1
                    # an unquoted body joins a line ending in an odd number of backslashes with the next one
                    while not quoted and (len(line) - len(line.rstrip("\\"))) % 2 == 1 and i < n:
                        j = cmd.find("\n", i)
                        j = n if j < 0 else j
                        line = line[:-1] + cmd[i:j]
                        i = j + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
                    body.append(line)
                bodies[marker] = "\n".join(body)
            pending = []
            continue
        if kind == "sq":
            out.append(c)
            i += 1
            if c == "'":
                stack.pop()
            continue
        if c == "\\" and i + 1 < n:
            if cmd[i + 1] == "\n" and kind != "ansi":
                i += 2  # a line continuation: bash removes it before reading words
                continue
            out.append(cmd[i:i + 2])
            word_start = False
            i += 2
            continue
        if kind == "ansi":
            out.append(c)
            i += 1
            if c == "'":
                stack.pop()
            continue
        if kind == "dq":
            if c == '"':
                stack.pop()
                out.append(c)
                i += 1
                continue
        elif kind in ("ar", "arb", "pe", "ix"):
            opener, closer = {"ar": "()", "arb": "[]", "pe": "{}", "ix": "[]"}[kind]
            if c == opener:
                stack[-1][1] += 1
            elif c == closer:
                stack[-1][1] -= 1
                if stack[-1][1] == 0:
                    stack.pop()
                    out.append(c)
                    i += 1
                    word_start = False
                    continue
            if kind == "pe" and c in "'\"":
                push("sq" if c == "'" else "dq")
                out.append(c)
                i += 1
                continue
        elif kind == "ca":  # a compound array assignment a=( ... ): words, quotes, comments, no heredoc
            if c == "#" and word_start:
                j = cmd.find("\n", i)
                i = n if j < 0 else j
                continue
            if c == "'":
                push("sq")
            elif c == '"':
                push("dq")
            elif c == "(":
                stack[-1][1] += 1
            elif c == ")":
                stack[-1][1] -= 1
                if stack[-1][1] == 0:
                    stack.pop()
                    out.append(c)
                    i += 1
                    word_start = False
                    continue
        else:  # shell code: top, cs (inside $( )), bq (inside backticks)
            if c == "#" and word_start and kind != "bq":
                j = cmd.find("\n", i)
                i = n if j < 0 else j
                continue
            if c == "'":
                push("sq")
            elif c == '"':
                push("dq")
            elif c == "`" and kind == "bq":
                stack.pop()
                out.append(c)
                i += 1
                word_start = False
                continue
            elif cmd.startswith("<<", i):
                if cmd.startswith("<<<", i):
                    out.append("<<<")
                    i += 3
                    word_start = True
                    continue
                dash = cmd.startswith("<<-", i)
                w = _heredoc_word(cmd, i + (3 if dash else 2))
                if w:
                    delim, quoted, end = w
                    marker = f"__HEREDOC{count}__"
                    count += 1
                    ref = delim if (not quoted and _MARKER_RX.fullmatch(delim)) else None
                    meta[marker] = {"delim": delim, "quoted": quoted, "ref": ref}
                    if ref:
                        bodies[marker] = ""
                    else:
                        pending.append((marker, delim, dash, quoted))
                    out.append("<< " + marker)
                    i = end
                    word_start = False
                    continue
            elif cmd.startswith("((", i) and word_start:
                push("ar", 2)
                out.append("((")
                i += 2
                continue
            elif word_start and _assignment_index_end(cmd, i) >= 0:
                j = _assignment_index_end(cmd, i)
                out.append(cmd[i:j + 1])
                push("ix", 1)
                i = j + 1
                word_start = False
                continue
            elif word_start and _COMPOUND_ASSIGN_RX.match(cmd, i):
                m = _COMPOUND_ASSIGN_RX.match(cmd, i)
                out.append(m.group(0))
                push("ca", 1)
                i = m.end()
                word_start = True
                continue
            elif kind == "cs" and c == "(":
                stack[-1][1] += 1
            elif kind == "cs" and c == ")":
                stack[-1][1] -= 1
                if stack[-1][1] == 0:
                    stack.pop()
                    out.append(c)
                    i += 1
                    word_start = False
                    continue
        # the $ forms open a context wherever they stand (shell code, "...", arithmetic, ${...}, an index)
        if c == "$" and kind not in ("sq", "ansi"):
            if cmd.startswith("$((", i):
                push("ar", 2)
                out.append("$((")
                i += 3
                continue
            if cmd.startswith("$(", i):
                push("cs", 1)
                out.append("$(")
                i += 2
                word_start = True
                continue
            if cmd.startswith("${", i):
                push("pe", 1)
                out.append("${")
                i += 2
                continue
            if cmd.startswith("$[", i):
                push("arb", 1)
                out.append("$[")
                i += 2
                continue
            if kind in _CODE_CTX + ("ca",) and cmd.startswith("$'", i):
                push("ansi")
                out.append("$'")
                i += 2
                word_start = False
                continue
        if c == "`" and kind != "bq":
            push("bq")
            out.append(c)
            i += 1
            word_start = True
            continue
        out.append(c)
        i += 1
        if kind in _CODE_CTX or kind == "ca":
            word_start = c in " \t\r\n;&|()<>"
    for marker, _delim, _strip, _quoted in pending:
        bodies[marker] = ""
    return "".join(out), bodies, meta


def _heredoc_substitutions(body: str):
    """The $(...) and `...` parts of an UNQUOTED heredoc body: bash runs them when the heredoc is read (KISPROGI 6539
    Q3: cat <<EOF + $(rm store/claudeclaw.db)). Quotes in the body are plain text (it's $(cmd) still runs); a backslash
    escapes $ and ` there."""
    subs, i, n = [], 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            i += 2
            continue
        if body.startswith("$(", i) and not body.startswith("$((", i):
            end, closed = _substitution_end(body, i + 2)
            subs.append(body[i + 2:end - 1] if closed else body[i + 2:])
            i = end
            continue
        if c == "`":
            j = i + 1
            while j < n and body[j] != "`":
                j += 2 if body[j] == "\\" else 1
            subs.append(body[i + 1:j])
            i = j + 1
            continue
        i += 1
    return subs


def _substitution_end(cmd: str, j: int):
    """For a $( whose text starts at j: (index just after its closing paren, closed), quotes inside respected."""
    depth, q1, q2 = 1, False, False
    while j < len(cmd) and depth:
        d = cmd[j]
        if d == "\\" and not q1:
            j += 2
            continue
        if d == "'" and not q2:
            q1 = not q1
        elif d == '"' and not q1:
            q2 = not q2
        elif not q1 and not q2:
            if d == "(":
                depth += 1
            elif d == ")":
                depth -= 1
        j += 1
    return j, depth == 0


def _strip_substitutions(cmd: str):
    """$(...), `...`, <(...), >(...) out of the text (not inside single quotes): (text, [executed contents])."""
    out, subs, i, sq, dq = [], [], 0, False, False
    while i < len(cmd):
        c = cmd[i]
        if c == "\\" and not sq and i + 1 < len(cmd):
            out.append(cmd[i:i + 2])
            i += 2
            continue
        if c == "'" and not dq:
            sq = not sq
        elif c == '"' and not sq:
            dq = not dq
        if not sq and (cmd.startswith("$(", i) or (not dq and cmd[i:i + 2] in ("<(", ">("))) and not cmd.startswith("$((", i):
            depth, j, q1, q2 = 1, i + 2, False, False
            while j < len(cmd) and depth:
                d = cmd[j]
                if d == "\\" and not q1:
                    j += 2
                    continue
                if d == "'" and not q2:
                    q1 = not q1
                elif d == '"' and not q1:
                    q2 = not q2
                elif not q1 and not q2:
                    if d == "(":
                        depth += 1
                    elif d == ")":
                        depth -= 1
                j += 1
            subs.append(cmd[i + 2:j - 1] if depth == 0 else cmd[i + 2:])
            out.append(f"__SUBST{len(subs) - 1}__")
            i = j
            continue
        if not sq and c == "`":
            j = cmd.find("`", i + 1)
            j = len(cmd) if j < 0 else j
            subs.append(cmd[i + 1:j])
            out.append(f"__SUBST{len(subs) - 1}__")
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), subs


_ANSI_C_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
                  "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _ansi_c_decode(body: str) -> str:
    """The text of bash's $'...' quoting: \\n \\t ... \\xHH \\NNN (octal) \\uHHHH \\UHHHHHHHH \\cX \\' \\\\. A NUL ends
    it: bash drops the rest of the $'...' part (KISPROGI 6524 V3, measured: $'ab\\0cd'ef is abef), so
    rm $'store/claudeclaw.db\\0x' removes the DB file; \\0, \\x00, \\c@, \\u0000, \\U00000000 and \\000 all count."""
    out, i, n = [], 0, len(body)
    while i < n:
        c = body[i]
        if c != "\\" or i + 1 >= n:
            ch, i = c, i + 1
        else:
            e = body[i + 1]
            if e in _ANSI_C_SIMPLE:
                ch, i = _ANSI_C_SIMPLE[e], i + 2
            elif e in "01234567":
                m = re.match(r"[0-7]{1,3}", body[i + 1:])
                ch, i = chr(int(m.group(0), 8) & 0xFF), i + 1 + len(m.group(0))
            elif e in "xuU":
                m = re.match(r"[0-9A-Fa-f]{1,%d}" % {"x": 2, "u": 4, "U": 8}[e], body[i + 2:])
                if m:
                    try:
                        ch = chr(int(m.group(0), 16))
                    except ValueError:
                        ch = "?"
                    i += 2 + len(m.group(0))
                else:
                    ch, i = body[i:i + 2], i + 2
            elif e == "c" and i + 2 < n:
                ch, i = chr(ord(body[i + 2]) & 0x1F), i + 3
            else:
                ch, i = body[i:i + 2], i + 2
        if ch == "\0":
            break
        out.append(ch)
    return "".join(out)


def _decode_dollar_quotes(text: str) -> str:
    """KISPROGI 6511 HS1: bash's $'...' (ANSI-C) and $"..." (locale) quoting, outside other quotes, become the plain
    quoted word they stand for, so every rule sees the real word: rm -rf $'store' removes the store folder, and
    sh <<< $'\\x72m ...' runs rm. shlex alone read $'rm X' as a variable $rm. $"..." is "..." (no translation here)."""
    if "$'" not in text and '$"' not in text:
        return text
    out, i, n, sq, dq = [], 0, len(text), False, False
    while i < n:
        c = text[i]
        if c == "\\" and not sq and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if not sq and not dq and c == "$" and i + 1 < n and text[i + 1] == "'":
            j = i + 2
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                out.append(text[i:])  # unterminated: bash fails on it too
                break
            out.append(shlex.quote(_ansi_c_decode(text[i + 2:j])))
            i = j + 1
            continue
        if not sq and not dq and c == "$" and i + 1 < n and text[i + 1] == '"':
            i += 1  # $"..." is a double-quoted word
            continue
        if c == "'" and not dq:
            sq = not sq
        elif c == '"' and not sq:
            dq = not dq
        out.append(c)
        i += 1
    return "".join(out)


_OP_CHARS = ";&|<>()"
QOP = "\0"  # marks a quoted / escaped operator word: QOP + ";" is find's terminator, QOP + "(" a group


def _unmark(word: str) -> str:
    """The text of a word as the program receives it (a marked operator word back to its operator character)."""
    return word[1:] if word.startswith(QOP) else word
_QUOTED_OP_RX = re.compile(r"\\([;&|<>()])|'([;&|<>()]{1,3})'|\"([;&|<>()]{1,3})\"")


def _protect_quoted_ops(text: str):
    """A quoted or escaped operator standing as a word (find's \\; and \\( \\), a ';' or '|' argument) is a word to
    the shell, not an operator, but shlex returns it as the same bare ';' or '(' an operator gives, so the find
    expression after it was cut off as a separate command (KISPROGI 6490 L1: "-exec cat {} ';' -name nope", and
    "-exec ls {} \\; -o -delete", whose -delete then went unseen). Each one becomes a placeholder word first:
    (text, {placeholder: QOP + operator}). QOP is a NUL, which no shell word can hold, so a LITERAL backslash word
    ('\\;' or "\\;", which find takes as a plain argument, not as its terminator) stays the different word \\;
    (KISPROGI 6497 M1: "-exec rm '\\;' store/claudeclaw.db \\;" ended the -exec early)."""
    out, ph, i, n, sq, dq = [], {}, 0, len(text), False, False
    while i < n:
        c = text[i]
        if not sq and not dq and (i == 0 or text[i - 1] in " \t\r\n" + _OP_CHARS):
            m = _QUOTED_OP_RX.match(text, i)
            if m and (m.end() == n or text[m.end()] in " \t\r\n" + _OP_CHARS):
                key = f"__QOP{len(ph)}__"
                ph[key] = QOP + (m.group(1) or m.group(2) or m.group(3))
                out.append(key)
                i = m.end()
                continue
        if c == "\\" and not sq and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == "'" and not dq:
            sq = not sq
        elif c == '"' and not sq:
            dq = not dq
        out.append(c)
        i += 1
    return "".join(out), ph


_EXTGLOB_LEAD = "@+?*!"


def _extglob_to_glob(text: str) -> str:
    """KISPROGI 6566 R8: an extglob pattern (@(a|b), +(x), ?(x), *(x), !(x)) is ONE word to bash, but its "(" looked
    like a subshell here, so cd /home/u/@(marveen) went to a folder named "@". Each one becomes a plain * (a superset
    of what it can match) outside quotes: glued to a word, or at a word start (except !( there, which may be bash's
    negation of a subshell). Without extglob these are syntax errors, so nothing that runs is misread."""
    out, i, n, sq, dq = [], 0, len(text), False, False
    while i < n:
        c = text[i]
        if c == "\\" and not sq and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == "'" and not dq:
            sq = not sq
        elif c == '"' and not sq:
            dq = not dq
        elif not sq and not dq and c in _EXTGLOB_LEAD and i + 1 < n and text[i + 1] == "(":
            glued = i > 0 and text[i - 1] not in " \t\r\n" + _OP_CHARS
            if glued or c != "!":
                depth, j = 0, i + 1
                while j < n:
                    if text[j] == "(":
                        depth += 1
                    elif text[j] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if j < n:
                    out.append("*")
                    i = j + 1
                    continue
        out.append(c)
        i += 1
    return "".join(out)


def _tokens(text: str):
    text = _extglob_to_glob(text)
    text, ph = _protect_quoted_ops(text)
    lx = shlex.shlex(text, posix=True, punctuation_chars=";&|<>()\n")
    lx.whitespace = " \t\r"
    lx.whitespace_split = True
    lx.commenters = ""
    toks = []
    for t in lx:
        if t in ph:
            toks.append(ph[t])
        elif len(t) > 1 and all(c in _OP_CHARS + "\n" for c in t):
            toks.extend(_split_ops(t))
        else:
            toks.append(t)
    return toks


_OPS_BY_LENGTH = ("&>>", "<<<", "&&", "||", ";;", "|&", ">>", "<<", ">&", "<&", "&>", ">|", "<>",
                  ";", "&", "|", "<", ">", "(", ")", "\n")


def _split_ops(tok: str):
    """shlex returns a run of operator characters as ONE token, so ";" + newline, ")" + newline, ");", "))" and
    "&&" + newline were none of the separators, and the next command became words of the one before it, never checked
    as a command (found 2026-11-28 while fixing KISPROGI 6511 AR: "true;" + newline + "rm store/claudeclaw.db"
    passed). Split it into bash's operators, longest first."""
    out, i = [], 0
    while i < len(tok):
        op = next(o for o in _OPS_BY_LENGTH if tok.startswith(o, i))
        out.append(op)
        i += len(op)
    return out


SUBSHELL_OPEN, SUBSHELL_CLOSE = QOP + "subshell(", QOP + "subshell)"


_CMD_PREFIX_WORDS = {"then", "do", "else", "elif", "if", "while", "until", "{", "!", "time"}


def _pipelines(toks):
    """([[simple command tokens, ...] per pipeline], [the separator that ends each one]). A subshell's ( and ) are
    pipelines of their own (a ")" gets the separator after it). KISPROGI 6524 V2:
    a function head (NAME ( ), function NAME [( )]) is no subshell, and a case pattern is no command: its ) closes
    the pattern, not a subshell ((case a in a) cd /tmp;; esac) closed the subshell early), and ;; ;& ;;& end an arm."""
    pipes, ends, cur, cmd, last = [], [], [], [], None
    case = []  # per open case: "header" (up to "in"), "pattern" (up to its ")"), "body" (up to ;; / ;& / ;;& / esac)
    fn_name = False  # "function NAME" seen: a "( )" right after it is part of the head
    i, n = 0, len(toks)

    def end_command(sep=None):
        nonlocal cur, cmd
        if cmd:
            cur.append(cmd)
        if cur:
            pipes.append(cur)
            ends.append(sep)
        elif sep is not None and pipes and pipes[-1] == [[SUBSHELL_CLOSE]] and ends[-1] is None:
            ends[-1] = sep  # "( ... ) && next"
        cur, cmd = [], []

    def command_position() -> bool:
        return all(w in _CMD_PREFIX_WORDS for w in cmd)

    while i < n:
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < n else None
        i += 1
        if t == "\n" and last in ("|", "|&", "&&", "||"):
            continue  # a line ending in | && || goes on in the next line
        if case and case[-1] == "pattern":
            last = t
            if t == "esac":
                case.pop()
            elif t == ")":
                case[-1] = "body"
            continue  # pattern words, its "(" and "|", newlines: not commands
        if case and case[-1] == "header":
            last = t
            if t == "in" and len(cmd) >= 2:
                end_command()
                case[-1] = "pattern"
            elif t != "\n":
                cmd.append(t)
            continue
        if t == "case" and command_position():
            cmd.append(t)
            case.append("header")
            last = t
            continue
        if case and t == "esac" and command_position():
            end_command(";")
            case.pop()
            last = t
            continue
        if case and t in (";;", ";") and (t == ";;" or nxt == "&"):
            end_command(";")
            if nxt == "&":
                i += 1
            case[-1] = "pattern"
            last = t
            continue
        if t == "(" and nxt == ")" and (fn_name or (len(cmd) == 1 and re.fullmatch(r"[^\s=$`'\"/]+", cmd[0]) and not cur)):
            cmd, fn_name = [], False  # a function head: NAME ( )
            i += 1
            last = ")"
            continue
        if cmd == ["function"] and t not in SEPARATORS and t not in PIPES:
            cmd, fn_name = [], True  # function NAME: the body follows
            last = t
            continue
        if t != "\n":
            fn_name = False
        last = t
        if t in SEPARATORS:
            end_command(t if t not in ("(", ")") else ";")
            if t in ("(", ")"):
                pipes.append([[SUBSHELL_OPEN if t == "(" else SUBSHELL_CLOSE]])
                ends.append(None)
        elif t in PIPES:
            if cmd:
                cur.append(cmd)
            cmd = []
        else:
            cmd.append(t)
    end_command()
    return pipes, ends


def _split_command(toks):
    """(head, args, redirections [(op, target)], here-strings, heredoc markers). Wrappers and assignments skipped."""
    words, redirs, herestr, heredocs = [], [], [], []
    k = 0
    while k < len(toks):
        t = toks[k]
        if t == "<<" and k + 1 < len(toks):
            heredocs.append(toks[k + 1])
            k += 2
            continue
        if t == "<<<" and k + 1 < len(toks):
            herestr.append(toks[k + 1])
            k += 2
            continue
        if t in REDIRS or re.fullmatch(r"[<>&|]+", t):
            if k + 1 < len(toks):
                redirs.append((t, toks[k + 1]))
            k += 2
            continue
        words.append(t)
        k += 1
    # assignments and wrappers before the real command word (one table: WRAPPER_SPECS)
    u = _unwrap(words)
    i = u["idx"]
    assigns = [v for _n, v in u["assigns"]]
    head = os.path.basename(words[i]) if i < len(words) else ""
    return head, words[i + 1:] if i < len(words) else [], redirs, herestr, heredocs, assigns


def _curl_data_positions(args):
    """Indexes of curl args that are data payload values (not an @file read)."""
    data = set()
    k = 0
    while k < len(args):
        a = args[k]
        flag, val = a, None
        if "=" in a and a.startswith("--"):
            flag, val = a.split("=", 1)
        if flag in CURL_DATA_FLAGS:
            if val is not None:
                if not val.startswith("@") and not re.match(r"[^=]*=@", val):
                    data.add(k)
            elif k + 1 < len(args):
                v = args[k + 1]
                if not v.startswith("@") and not re.match(r"[^=]*=[@<]", v):
                    data.add(k + 1)
                k += 1
        elif re.fullmatch(r"-d.+", a) and not a[2:].startswith("@"):
            data.add(k)
        k += 1
    return data


def _search_pattern_positions(head: str, args, skip_values: bool = True):
    """Indexes of a search command's PATTERN arguments (a search string is data, not a file), read by the one
    normalizer _search_walk. The protected-name exemption uses only the words that are a pattern in BOTH readings, with
    and without the value skipping (KISPROGI 6511 R1: a wrong value table must not turn a file operand into a pattern)."""
    return _search_walk(head, args, skip_values)["pats"]


def _destinations(head: str, args):
    """The args a file-writing command writes to (R3)."""
    pos = [a for a in args if not a.startswith("-")]
    if head in ("rm", "unlink", "shred", "truncate", "srm", "trash", "trash-put"):
        return pos
    if head == "mv":
        return pos  # either side: moving the live DB away is a write too
    if head in ("cp", "install", "rsync", "scp", "ln", "link", "ditto"):
        tgt = []
        for k, a in enumerate(args):
            if a in ("-t", "--target-directory") and k + 1 < len(args):
                tgt.append(args[k + 1])
            elif a.startswith("--target-directory="):
                tgt.append(a.split("=", 1)[1])
        return tgt + (pos[-1:] if len(pos) >= 2 else [])
    if head == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if head == "tee":
        return pos
    if head in ("sed", "perl", "gsed") and any(re.fullmatch(r"-[a-zA-Z]*i\S*|--in-place\S*", a) for a in args):
        return pos
    return []


_FIND_FILTERS = ("-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex")
_FIND_NEGATIONS = ("!", "-not", "-exclude")  # bfs -exclude EXPR leaves out what EXPR matches: a negation of it
_FIND_ALTERNATIVES = ("-o", "-or", ",")
_FIND_EXECS = ("-exec", "-execdir", "-ok", "-okdir")
_FIND_WRITE_TARGETS = ("-fprint", "-fprint0", "-fls", "-fprintf")
_FIND_STDOUT_ACTIONS = ("-print", "-print0", "-printf", "-ls", "-printx")
_FIND_DELETES = ("-delete", "-rm")  # bfs: -rm is -delete
# Every primary / option of GNU find 4.10 and bfs 4.1.1 by the number of words it takes (KISPROGI 6497 H1: the agents'
# Bash runs bfs, a shell function from Claude Code; /usr/bin/find is GNU). A dash word outside these is not known.
_FIND_FLAGS = {"-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x"}  # bfs/GNU flags before or among the paths
_FIND_FLAGS_WITH_ARG = {"-D", "-S"}
_FIND_NOARG = {"-print", "-print0", "-printx", "-ls", "-prune", "-quit", "-delete", "-rm", "-true", "-false", "-empty",
               "-executable", "-readable", "-writable", "-hidden", "-nohidden", "-nogroup", "-nouser", "-sparse", "-xattr",
               "-daystart", "-follow", "-ignore_readdir_race", "-noignore_readdir_race", "-mount", "-noerror", "-noleaf",
               "-status", "-unique", "-warn", "-nowarn", "-xdev", "-color", "-nocolor", "-a", "-and", "-version", "-help",
               "--help", "--version", "-depth", "-exit"}
_FIND_ONEARG = {"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex", "-lname", "-ilname",
                "-maxdepth", "-mindepth", "-type", "-xtype", "-size", "-perm", "-user", "-group", "-uid", "-gid", "-inum",
                "-links", "-samefile", "-newer", "-amin", "-Bmin", "-cmin", "-mmin", "-atime", "-Btime", "-ctime", "-mtime",
                "-anewer", "-Bnewer", "-cnewer", "-mnewer", "-asince", "-Bsince", "-csince", "-msince", "-since", "-used",
                "-fstype", "-context", "-regextype", "-files0-from", "-printf", "-fprint", "-fprint0", "-fls", "-limit",
                "-xattrname"}
_PROTECTED_NAMES = (TOTP_NAME,) + DB_NAMES
# -exec commands that only show names or metadata: they read no file's content.
_FIND_NAME_ONLY_CMDS = {"echo", "printf", "ls", "stat", "basename", "dirname", "realpath", "readlink", "test", "[", "true", "du"}
# -exec commands that read or remove ONE file and fail on a directory without a recursive flag: for these a filter that
# cannot select a protected file proves the call safe, since a matched folder gives them nothing. (diff and cmp are not
# here: diff on two folders prints the content of the files they share.) grep is here because the grep that -exec and
# xargs start is the GNU one found on PATH ("Is a directory"); the ugrep that searches a folder without -r is a Claude
# Code shell function, which only the agent's own shell line reaches (KISPROGI 6497 H2, handled at the search rule).
_FIND_ONE_FILE_CMDS = {"cat", "tac", "nl", "head", "tail", "less", "more", "wc", "md5sum", "sha1sum", "sha224sum", "sha256sum",
                       "sha384sum", "sha512sum", "b2sum", "cksum", "sum", "file", "strings", "xxd", "od", "hexdump", "base64",
                       "base32", "grep", "egrep", "fgrep", "zgrep", "jq", "rm", "unlink", "shred", "truncate"}
_RECURSIVE_FLAG_RX = re.compile(r"^-(?:-recursive|-archive|-dereference-recursive|-directories=recurse|[A-Za-z]*[rRa][A-Za-z]*)$")


# GNU grep 3.11's long options: getopt takes any unambiguous prefix of one (KISPROGI 6505 M2)
_GNU_GREP_LONG = ("--extended-regexp", "--fixed-strings", "--basic-regexp", "--perl-regexp", "--regexp", "--file",
                  "--ignore-case", "--no-ignore-case", "--word-regexp", "--line-regexp", "--null-data", "--no-messages",
                  "--invert-match", "--version", "--help", "--max-count", "--byte-offset", "--line-number",
                  "--line-buffered", "--with-filename", "--no-filename", "--label", "--only-matching", "--quiet", "--silent",
                  "--binary-files", "--text", "--directories", "--devices", "--recursive", "--dereference-recursive",
                  "--include", "--exclude", "--exclude-from", "--exclude-dir", "--files-without-match",
                  "--files-with-matches", "--count", "--initial-tab", "--null", "--before-context", "--after-context",
                  "--context", "--group-separator", "--no-group-separator", "--color", "--colour", "--binary")


def _gnu_grep_long(opt: str) -> str:
    """The full GNU grep long option an abbreviation stands for (--recur -> --recursive), or the word as it is."""
    if not opt.startswith("--") or opt in _GNU_GREP_LONG:
        return opt
    hits = [o for o in _GNU_GREP_LONG if o.startswith(opt)]
    return hits[0] if len(hits) == 1 else opt


def _gnu_long_prefix(word: str, full: str, least: int) -> bool:
    """GNU getopt takes any unambiguous prefix of a long option (KISPROGI 6505 M2: --recur, --dereference-rec, --dir=rec).
    least: the shortest prefix that is still unambiguous among GNU grep's long options."""
    return least <= len(word) <= len(full) and full.startswith(word)


def _recurse_value(v) -> bool:
    """A -d / --directories value that GNU grep reads as "recurse" (it takes a prefix of 3 letters or more)."""
    return isinstance(v, str) and 3 <= len(v) <= 7 and "recurse".startswith(v)


def _gnu_recursive_long(opt: str, val) -> bool:
    """A GNU grep long option that makes it recursive, abbreviated or not: --recursive (from --rec), --dereference-recursive
    (from --der), --directories=recurse (from --di, with a value from rec)."""
    return (_gnu_long_prefix(opt, "--recursive", 5) or _gnu_long_prefix(opt, "--dereference-recursive", 5)
            or (_gnu_long_prefix(opt, "--directories", 4) and _recurse_value(val)))


def _has_recursive_flag(words, cmd: str = "") -> bool:
    """Whether a command's words carry a recursive flag: a short cluster with r/R/a (rm -rf, cp -a, grep -R), GNU grep's
    long forms and their abbreviations, or -d / --directories with a "recurse" value (-d rec, --dir=rec, and for grep a
    short group ending in d: -sd rec, KISPROGI 6511 M2b)."""
    for k, b in enumerate(words):
        if cmd in _GREP_HEADS and re.fullmatch(r"-[A-Za-z0-9]+", b):
            hit = next((n for n, ch in enumerate(b[1:], 1) if ch in _GREP_VALUE_LETTERS), None)
            if hit is not None and b[hit] == "d":
                rest = b[hit + 1:]
                if _recurse_value(rest or (words[k + 1] if k + 1 < len(words) else None)):
                    return True
        nxt = words[k + 1] if k + 1 < len(words) else None
        opt, has_val, val = b.partition("=")
        if _RECURSIVE_FLAG_RX.match(b) or "recurse" in b:
            return True
        if b.startswith("--") and _gnu_recursive_long(opt, val if has_val else nxt):
            return True
        if (b == "-d" and _recurse_value(nxt)) or (b.startswith("-d") and not b.startswith("--") and _recurse_value(b[2:])):
            return True
    return False


def _vague_word(word: str) -> bool:
    """A word whose place or name is not known from the text: a variable, a substitution, a glob, a brace."""
    return bool(re.search(r"[$`*?\[{]|__SUBST\d+__", word))


def _find_parse(args):
    """One pass over a find's words, the bfs way (paths, flags and the expression may be mixed in any order; GNU's
    order is a special case of it): (starting points, unknown dash words, expression items). An expression item is
    (index, word) of an operator or a primary; a primary's own words are skipped. KISPROGI 6497 H1: "find src -type f
    store -delete" (a path in the middle), "find -f PATH" and bfs -rm all slipped through the GNU-only reading."""
    starts, unknown, i, n = [], [], 0, len(args)
    while i < n:
        a = args[i]
        if a in _FIND_FLAGS or re.fullmatch(r"-O\d*|-j\d+", a) or a == "--":
            i += 1
        elif a in _FIND_FLAGS_WITH_ARG:
            i += 2
        elif a == "-f":
            if i + 1 < n:
                starts.append(args[i + 1])
            i += 2
        elif a in _FIND_EXECS:
            j = i + 1
            while j < n and not (args[j] == QOP + ";" or (args[j] == "+" and j > i + 1 and args[j - 1] == "{}"
                                                           and a in ("-exec", "-execdir"))):
                j += 1
            i = j + 1
        elif a in ("-depth", "-exit") and i + 1 < n and re.fullmatch(r"[-+]?\d+", args[i + 1]):
            i += 2
        elif a == "-fprintf":
            i += 3
        elif a in _FIND_ONEARG or re.fullmatch(r"-newer[aBcm][aBcmt]", a):
            i += 2
        elif a in _FIND_NOARG or a in _FIND_NEGATIONS or a in _FIND_ALTERNATIVES or a in (QOP + "(", QOP + ")"):
            i += 1
        elif a.startswith("-"):
            unknown.append(a)
            i += 1
        else:
            starts.append(_unmark(a))  # bfs takes any other word as a path, anywhere; GNU fails on it
            i += 1
    return starts, unknown


def _find_reach(args, starts, cwd: str):
    """(near, printed paths of the protected files, place unknown). near: a start is the live store, holds it, sits below
    it, or is not known (a variable, a glob, -files0-from, an unknown cwd). The printed paths are what -path sees."""
    store = os.path.realpath(STORE)
    near, unknown, printed = False, False, []
    if "-files0-from" in args:
        near = unknown = True
    for st in starts or ["."]:
        r = _resolve(st, cwd)
        if _vague_word(st) or r == UNKNOWN_CWD:
            near = unknown = True
            continue
        if r == store or store.startswith(r.rstrip(os.sep) + os.sep):
            near = True
            base = st if st.endswith("/") else st + "/"
            for n in _PROTECTED_NAMES:
                printed.append(base + os.path.relpath(os.path.join(store, n), r))
        elif r.startswith(store + os.sep):
            near = unknown = True  # below the store: the protected files only through a link, at a path not known
    return near, printed, unknown


def _find_filter_may_match(flag: str, value: str, printed, unknown: bool) -> bool:
    """Whether one find filter can select a protected file (the second-factor store, the DB file or a sidecar). -name
    against the file names; -path / -wholename against the whole printed path, since find's * matches a slash too
    (KISPROGI 6490 follow-up: -path '*/s*' from the install root selects ./store/claudeclaw.db, its last segment does
    not). A [ or a backslash may match (Python's fnmatch reads [^x] and \\w differently from find), and -regex always."""
    if flag in ("-regex", "-iregex") or "[" in value or "\\" in value:
        return True
    ci = flag.startswith("-i")
    v = value.lower() if ci else value
    if flag in ("-name", "-iname"):
        names = _PROTECTED_NAMES
    elif unknown:
        return True
    else:
        names = printed
    return any(fnmatch.fnmatchcase(n.lower() if ci else n, v) for n in names)


def _check_find(args, cwd: str, depth: int, piped=(), feeds_interpreter: bool = False) -> None:
    """KISPROGI 6483 L1, redone after KISPROGI 6490 and 6497. find runs its expression left to right, so only the filters
    BEFORE an action decide what the action gets ("find store -type f -delete -name nope" deletes the whole store).
    Read the bfs way (the agents' find): a path may stand anywhere, -f PATH is a path, -rm is -delete; a dash word that
    neither GNU find nor bfs knows is refused near the store (fail closed).

    From ANY start: an -exec/-execdir/-ok command is checked as a command of its own (rm store/claudeclaw.db from a find
    in /tmp), and an -fprint/-fprint0/-fls/-fprintf target is an R3 write target (-fprint store/claudeclaw.db empties
    the DB even with no match).

    Near the live store (a start at, above or below it, or not known): -delete/-rm, and an -exec of a command that can
    read or remove a file, passes only when one filter ANDed before it cannot select a protected file; a ! / -not /
    -exclude takes only the next filter (or parenthesised group) out of the narrowing, a -o / -or / comma ends it for
    the rest. The -exec command must be a name-only one, or a one-file reader/remover without a recursive flag; anything
    else (shells, wrappers like busybox/nohup/stdbuf, rg, mv, cp, tar, interpreters) is refused. piped: the commands a
    later "| xargs CMD" in the same pipeline runs on find's output (they get what -print would print)."""
    starts, unknown_words = _find_parse(args)
    near, printed, unknown = _find_reach(args, starts, cwd)
    if near and unknown_words:
        _deny(f"find a store kozeleben ismeretlen kifejezessel ({unknown_words[0]}); csak ismert GNU find / bfs elem futhat ott")
    if feeds_interpreter:
        # find's output is run as code (find ... -printf 'rm %p\\n' | sh): near the store it names the store's files to
        # the interpreter; elsewhere every word of it is code text, with -printf's escapes decoded
        if near:
            _deny("a store mappan futo find kimenete egy ertelmezobe megy")
        for a in args:
            _code_text_checks(_unmark(a).replace("\\n", "\n").replace("\\t", "\t"), "find -> ertelmezo", cwd, depth)
    filters, alternative, acted = [], False, False
    negate_next, groups = False, []  # groups: per open parenthesis, whether it is negated (or inside a negated one)

    def narrowed() -> bool:
        return not alternative and any(ok and f != "-type" and not _find_filter_may_match(f, v, printed, unknown)
                                       for f, v, ok in filters)

    def run_command(words, kind: str) -> None:
        if not words:
            return
        words = [_unmark(w) for w in words]
        cmd = os.path.basename(words[0])
        if depth < 4:
            check_bash(shlex.join(words), UNKNOWN_CWD if kind in ("-execdir", "-okdir") else cwd, depth + 1)
        if not near or cmd in _FIND_NAME_ONLY_CMDS:
            return
        if cmd in _FIND_ONE_FILE_CMDS and not _has_recursive_flag(words[1:], cmd):
            if not narrowed():
                _deny(f"find {kind} {cmd} a store mappan, olyan szurovel, ami a vedett fajlt is kivalaszthatja")
            return
        _deny(f"find {kind} {cmd} a store mappan (ott csak nevkiiro vagy egyfajlos olvaso/torlo parancs futhat)")

    def run_piped() -> None:
        for p in piped:
            run_command(p, "| xargs")

    i = 0
    while i < len(args):
        a = args[i]
        if a in _FIND_FLAGS or re.fullmatch(r"-O\d*|-j\d+", a) or a == "--":
            pass
        elif a in _FIND_FLAGS_WITH_ARG or a == "-f":
            i += 1
        elif a in _FIND_ALTERNATIVES:
            alternative = True
        elif a in _FIND_NEGATIONS:
            negate_next = True
        elif a == QOP + "(":
            groups.append(negate_next or bool(groups and groups[-1]))
            negate_next = False
        elif a == QOP + ")":
            if groups:
                groups.pop()
        elif (a in _FIND_FILTERS or a == "-type") and i + 1 < len(args):
            filters.append((a, _unmark(args[i + 1]), not negate_next and not (groups and groups[-1])))
            negate_next = False
            i += 1
        elif a in _FIND_WRITE_TARGETS and i + 1 < len(args):
            acted = True
            if _is_live_db_path(_unmark(args[i + 1]), cwd):
                _deny(f"find {a} a dashboard adatbazis-fajljaba ir")
            i += 2 if a == "-fprintf" else 1
        elif a in _FIND_DELETES:
            acted = True
            if near and not narrowed():
                _deny(f"find {a} a store mappan, olyan szurovel, ami a vedett fajlt is kivalaszthatja")
        elif a in _FIND_EXECS and i + 1 < len(args):
            acted = True
            j, words = i + 1, []
            while j < len(args):
                b = args[j]
                if b == QOP + ";" or (b == "+" and words and words[-1] == "{}" and a in ("-exec", "-execdir")):
                    break
                words.append(b)
                j += 1
            run_command(words, a)
            i = j
        elif a in _FIND_STDOUT_ACTIONS:
            acted = True
            run_piped()
            if a == "-printf":
                i += 1
        elif a in ("-depth", "-exit") and i + 1 < len(args) and re.fullmatch(r"[-+]?\d+", args[i + 1]):
            negate_next = False
            i += 1
        elif a in _FIND_ONEARG or re.fullmatch(r"-newer[aBcm][aBcmt]", a):
            negate_next = False  # a test the negation applied to (it narrows nothing either way)
            i += 1
        elif a in _FIND_NOARG:
            negate_next = False
        i += 1
    if not acted:
        run_piped()  # find's default action is -print, at the end of the expression


def _unparsable_hits(cmd: str) -> bool:
    """Fail-closed test for Bash that cannot be parsed: a protected thing in a writing or reading shape only (a plain
    read of the DB through sqlite3 stays allowed)."""
    return bool(_sql_write(cmd) or TOTP_RX.search(cmd) or _code_writes_db(cmd)
                or re.search(r"\b(?:rm|mv|cp|install|rsync|ln|truncate|shred|dd|tee)\b[^\n;|&]*" + _DBN, cmd, re.I)
                or re.search(r">\s*['\"]?[^\s;|&]*" + _DBN, cmd, re.I))


_HD_STACK = []  # per check_bash level being processed: (bodies, meta); a substitution's text refers to its parent's
MAX_CWDS = 32   # KISPROGI 6539 Q10, 6566 L1: the bound on the possible cwds at one point (see _cwd_overflow_risky)
# KISPROGI 6566 L1: the commands that read, copy, move or delete the files they name: a relative operand of theirs may
# reach the store (or a folder holding it) from one of too many possible places.
_FILE_TOUCH_HEADS = FILE_OP_HEADS | SEARCH_HEADS | {"find", "tar", "gtar", "bsdtar", "zip", "pax", "cpio", "git", "cat",
                                                    "tac", "head", "tail", "less", "more", "base64", "xxd", "od", "strings",
                                                    "sqlite3", "chmod", "chown", "rmdir"}


def _cwd_overflow_risky(cmd: str, pipes) -> bool:
    """KISPROGI 6566 L1: with more than MAX_CWDS possible places, a command is refused only when it could matter: it
    names a protected thing or the word store (or a dot-name glob), or a command that touches files has a relative
    operand (from too many places any relative name, even "marveen" from /home/kisss, may be the store's folder). A
    cd's own target is no operand."""
    if _unparsable_hits(cmd) or _BIG_TEXT_STORE_RX.search(cmd) or DB_RX.search(cmd) or TOTP_RX.search(cmd):
        return True
    for pl in pipes or []:
        for member in pl:
            if member and member[0] in (SUBSHELL_OPEN, SUBSHELL_CLOSE):
                continue
            head, args, *_ = _split_command(member)
            if head in ("cd", "pushd", "popd") or head not in _FILE_TOUCH_HEADS:
                continue
            for a in (_unmark(a) for a in args):
                if a and not a.startswith("-") and not os.path.isabs(os.path.expanduser(a)):
                    return True
    return False


def _resolve_heredoc_refs(bodies, meta) -> None:
    """A marker delimiter (a heredoc an outer scan cut out of a substitution's text) takes the outer body."""
    parent = _HD_STACK[-1] if _HD_STACK else None
    for m, md in meta.items():
        ref = md.get("ref")
        if ref and parent and ref in parent[0]:
            bodies[m] = parent[0][ref]
            md["quoted"] = parent[1].get(ref, {}).get("quoted", True)


def check_bash(cmd: str, cwd: str, depth: int = 0, best_effort: bool = False) -> None:
    """best_effort: a nested try on text that may not be shell at all (an interpreter's code); a parse failure there
    is not a refusal, the text rules already ran on it."""
    cmd = cmd.replace(QOP, "")  # bash drops NUL bytes; the gate uses NUL to mark quoted operator words
    if len(cmd) > BIG_TEXT:
        # KISPROGI 6483 L2: shlex is quadratic on one long quoted token (365 KB: 4 s, 750 KB: 16.5 s, past the hook's
        # deadline, which fails the main agent's Bash closed). Above the threshold only the text rules run, the same set
        # as for unparsable Bash. Real commands peak far below it (measured max 41 KB, p99.9 15 KB).
        # KISPROGI 6490 L2: the name-free forms (rm -rf store, cat store/.d*, find store -delete) would slip through
        # the text rules, so above the threshold a word "store" or a dot-name glob is refused as well (0 real hits).
        if not best_effort and (_unparsable_hits(cmd) or _BIG_TEXT_STORE_RX.search(cmd)):
            _deny("nagyon hosszu parancs a vedett nevekkel vagy a store mappaval (csak szoveges ellenorzes)")
        return
    text, bodies, hmeta = _strip_heredocs(cmd)
    _resolve_heredoc_refs(bodies, hmeta)
    text = _decode_dollar_quotes(text)
    text, subs = _strip_substitutions(text)
    sub_text = {f"__SUBST{k}__": sub for k, sub in enumerate(subs)}
    try:
        pipes, ends = _pipelines(_tokens(text))
    except ValueError:
        pipes = ends = None
    _HD_STACK.append((bodies, hmeta))
    try:
        if pipes is not None:
            sets, every = _cwd_sets(pipes, ends, [cwd], cmd, sub_text, bodies, depth)
        else:
            sets, every = [], {cwd: None}
        for s_ in subs:
            # a substitution is shell that runs, in any place the command may be by then (its data positions stay data)
            for c in every:
                if depth < 4:
                    check_bash(s_, c, depth + 1, best_effort)
                elif _unparsable_hits(s_):
                    _deny("tul mely parancs-behelyettesites a vedett nevekkel (fail-closed)")
        # KISPROGI 6539 Q3: an UNQUOTED heredoc's body is expanded when it is read: its $(...) and `...` run
        for m, body in bodies.items():
            if body and not hmeta.get(m, {}).get("quoted", True):
                for s_ in _heredoc_substitutions(body):
                    for c in every:
                        if depth < 4:
                            check_bash(s_, c, depth + 1, best_effort)
                        elif _unparsable_hits(s_):
                            _deny("tul mely heredoc-behelyettesites a vedett nevekkel (fail-closed)")
        if pipes is None:
            if not best_effort and _unparsable_hits(cmd):
                _deny("nem elemezheto parancs a vedett nevekkel (fail-closed)")
            return
        for pipeline, cands in zip(pipes, sets):
            if cands is None:
                continue
            for c in cands:
                # KISPROGI 6539 Q4: env -C, tar -C, git -C, make -C run the command (or read its paths) elsewhere
                for place in [c] + [d for d in _pipeline_chdirs(pipeline, c) if d != c]:
                    try:
                        _check_pipeline(pipeline, place, depth, bodies, sub_text)
                    except Deny as d:
                        if len(cands) > 1 and c != cands[-1] and c != UNKNOWN_CWD and "cd MAPPA &&" not in str(d):
                            raise Deny(f"{d}; ha egy korabbi cd nem sikerul, ez a(z) {c} mappaban futna (cd MAPPA && "
                                       "PARANCS alakban csak a cd celjaban fut)") from None
                        raise
    finally:
        _HD_STACK.pop()


def _cwd_sets(pipes, ends, start, cmd: str, sub_text, bodies, depth: int):
    """KISPROGI 6524 V2/P5: the cwd is a SET of places a command may run in. A cd adds its target and does not drop the
    old place (it may fail: a missing folder; it may sit in a branch, a function body, a background job or a case
    arm); only the commands joined to it by && run in the target alone (they run only if it succeeded), and only when
    every connector before the cd in its and-or list is && (KISPROGI 6539 Q1: "A || cd X && B" is "(A || cd X) && B",
    so B runs where A ran when A succeeds). cd - / ~- / popd add an unknown place, and every path is checked under
    every candidate. A subshell's own cds end with it. KISPROGI 6539 Q5: eval's words are shell (their cd counts), and
    source / . of a substitution, of stdin or of a file adds an unknown place (plus what a known text does).
    Returns (per pipeline: its candidate list or None for a subshell marker, every place ever reached)."""
    sets, every = [], dict.fromkeys(start)
    cur, base, stack, andor_ok = list(start), list(start), [], True
    overflow_risky = None
    for pl, end in zip(pipes, ends):
        if pl == [[SUBSHELL_OPEN]]:
            stack.append((cur, base, andor_ok))
            base, andor_ok = cur, True
            sets.append(None)
            continue
        if pl == [[SUBSHELL_CLOSE]]:
            cur, base, andor_ok = stack.pop() if stack else (cur, base, andor_ok)
            if end != "&&":
                cur = base
            andor_ok = False if end == "||" else (andor_ok if end == "&&" else True)
            sets.append(None)
            continue
        sets.append(cur)
        targets = effect = None
        if len(pl) == 1:
            head, args, _redirs, herestr, heredocs, _assigns = _split_command(pl[0])
            if head in ("cd", "pushd", "popd"):
                targets = _cd_targets(cur, head, args, cmd)
            elif head == "eval" and depth < 4:
                words = [_unmark(a) for a in args]
                if words[:1] == ["--"]:
                    words = words[1:]
                effect = _cwd_effect(" ".join(words), cur, depth)
                # KISPROGI 6566 R7: an eval word holding a substitution or a variable is text not known here, like
                # source's: an unknown place, plus where the substitution's text leads when it only prints
                # (eval "$(echo cd DIR)", eval "$(printf 'cd %s' DIR)", eval `echo cd DIR`)
                marks = [mk for w in words for mk in re.findall(r"__SUBST\d+__", w)]
                if any(re.search(r"[$`]", w) for w in words):
                    effect = list(dict.fromkeys(effect + [UNKNOWN_CWD]))
                for mk in marks:
                    printed = _printer_output(sub_text.get(mk, ""))
                    if printed is None:
                        effect = list(dict.fromkeys(effect + [UNKNOWN_CWD]))
                    else:
                        effect = list(dict.fromkeys(effect + _cwd_effect(printed, cur, depth)))
            elif head in ("source", ".") and depth < 4:
                effect = list(dict.fromkeys(cur + [UNKNOWN_CWD]))
                texts = [h for h in herestr] + [bodies.get(m, "") for m in heredocs]
                for w in args[:1]:
                    for mk in re.findall(r"__SUBST\d+__", w):
                        out = _printer_output(sub_text.get(mk, ""))
                        if out is not None:
                            texts.append(out)
                for t in texts:
                    effect = list(dict.fromkeys(effect + _cwd_effect(t, cur, depth)))
        if targets is not None:
            every.update(dict.fromkeys(targets))
            if end == "&&" and andor_ok and pl[0][:1] != ["!"]:
                cur, base = targets, list(dict.fromkeys(base + targets))
            else:
                base = list(dict.fromkeys(base + targets))
                cur = base
        elif effect is not None:
            every.update(dict.fromkeys(effect))
            base = list(dict.fromkeys(base + effect))
            cur = base
        elif end != "&&":
            cur = base
        andor_ok = False if end == "||" else (andor_ok if end == "&&" else True)
        if len(cur) > MAX_CWDS or len(base) > MAX_CWDS or len(every) > 4 * MAX_CWDS:
            # KISPROGI 6566 L1: the bound is on the places possible at this point; past it the command is refused
            # only when it could matter (see _cwd_overflow_risky), otherwise the places beyond the bound become ONE
            # place not known here (a superset: every later path is checked there too), which keeps the work bounded
            if overflow_risky is None:
                overflow_risky = _cwd_overflow_risky(cmd, pipes)
            if overflow_risky:
                _deny(f"tul sok lehetseges munkamappa egy parancsban (tobb mint {MAX_CWDS} cd-cel), es a parancs vedett "
                      "nevet, a store szot vagy fajlkezelo parancsban relativ utat emlit; bontsd tobb hivasra")
            if len(cur) > MAX_CWDS:
                cur = list(dict.fromkeys(cur[:MAX_CWDS] + [UNKNOWN_CWD]))
            if len(base) > MAX_CWDS:
                base = list(dict.fromkeys(base[:MAX_CWDS] + [UNKNOWN_CWD]))
            if len(every) > 4 * MAX_CWDS:
                every = dict.fromkeys(list(every)[:MAX_CWDS] + [UNKNOWN_CWD])
    return sets, every


def _cwd_effect(text: str, start, depth: int):
    """Every place a piece of shell text (eval's words, a sourced text) may leave the cwd in, from the start places."""
    try:
        t, b, m = _strip_heredocs(text)
        t, subs = _strip_substitutions(_decode_dollar_quotes(t))
        pipes, ends = _pipelines(_tokens(t))
    except ValueError:
        return list(dict.fromkeys(list(start) + [UNKNOWN_CWD]))
    _sets, every = _cwd_sets(pipes, ends, list(start), text, {f"__SUBST{k}__": s_ for k, s_ in enumerate(subs)}, b, depth + 1)
    return list(every)


def _pipeline_chdirs(pipeline, cwd: str):
    """KISPROGI 6539 Q4: the folders a pipeline member works in besides the cwd: env -C / --chdir (before the command),
    tar -C / --directory, git -C (cumulative), make -C / --directory. A variable folder is an unknown place."""
    out = []

    def place(v: str, base: str) -> str:
        v = _unmark(v)
        if _unknown_place(v) or re.search(r"[*?\[]", v):
            return UNKNOWN_CWD
        if base == UNKNOWN_CWD and not os.path.isabs(os.path.expanduser(v)):
            return UNKNOWN_CWD
        return _resolve(v, base)

    for toks in pipeline:
        words = [t for t in toks if t not in REDIRS]
        u = _unwrap(words)  # KISPROGI 6566 R3, R5, R6: every wrapper's folder options, bundled or attached too
        i = u["idx"]
        for v in u["chdirs"]:
            out.append(UNKNOWN_CWD if v is None else place(v, cwd))
        if i >= len(words):
            continue
        head, rest = os.path.basename(_unmark(words[i])), words[i + 1:]
        if head in PROGRAM_SPECS:
            out += _program_folders(head, rest, cwd, u["assigns"], place)
    return list(dict.fromkeys(out))


_CDPATH_RX = re.compile(r"\bCDPATH=(?:'([^']*)'|\"([^\"]*)\"|([^\s;&|()<>]*))")


def _cd_targets(cur, head: str, args, cmd: str):
    """Where a cd / pushd / popd goes when it succeeds, from each cwd candidate (KISPROGI 6524 P5). cd -, ~-, popd,
    pushd with no folder or +N/-N, and a variable target go to an unknown place. A relative target is also looked up
    in CDPATH (the hook's own and any CDPATH= the command sets; a variable in it is an unknown place)."""
    if head == "popd":
        return [UNKNOWN_CWD]
    words = [_unmark(a) for a in args]
    k = 0
    while k < len(words) and re.fullmatch(r"-[LPe@]+", words[k]):
        k += 1
    if k < len(words) and words[k] == "--":
        k += 1
    target = words[k] if k < len(words) else ("" if head == "pushd" else "~")
    if not target or target == "-" or re.fullmatch(r"[+-]\d+", target) or _unknown_place(target) \
            or (target.startswith("~") and re.match(r"~[-+]\d*(?:/|$)", target)):
        return [UNKNOWN_CWD]
    out = {}
    if re.search(r"[*?\[]", target):
        # KISPROGI 6539 Q6: bash expands a glob target first (cd /home/kisss/marvee? goes to the install): the
        # folders it matches here, or an unknown place when nothing (or too much) matches
        for c in cur:
            pat = os.path.expanduser(target)
            if not os.path.isabs(pat):
                if c == UNKNOWN_CWD:
                    out[UNKNOWN_CWD] = None
                    continue
                pat = os.path.join(c, pat)
            try:
                hits = [h for h in _glob.glob(pat) if os.path.isdir(h)][:MAX_CWDS + 1]
            except (OSError, ValueError, re.error):
                hits = []
            if not hits or len(hits) > MAX_CWDS:
                out[UNKNOWN_CWD] = None
            for h in hits[:MAX_CWDS]:
                out[_resolve(h, c)] = None
        return list(out)
    for c in cur:
        if c == UNKNOWN_CWD and not os.path.isabs(os.path.expanduser(target)):
            out[UNKNOWN_CWD] = None
        else:
            out[_resolve(target, c)] = None
    if not os.path.isabs(os.path.expanduser(target)) and not re.match(r"\.\.?(?:/|$)", target):
        cdpaths = [os.environ.get("CDPATH", "")] + [next(g for g in m.groups() if g is not None)
                                                     for m in _CDPATH_RX.finditer(cmd)]
        for entry in (e for v in cdpaths for e in v.split(":") if e):
            if _unknown_place(entry):
                out[UNKNOWN_CWD] = None
            else:
                for c in cur:
                    out[_resolve(os.path.join(entry, target), c)] = None
    return list(out)


def _names_protected(word: str, d: str) -> bool:
    """A path word that names the live store, a folder holding it, the DB or the second-factor store from folder d."""
    return (_is_store_or_ancestor(word, d) or _is_store_dir(word, d) or _is_live_db_path(word, d)
            or _is_live_totp_word(word, d))


_TAR_CREATE = {"-c", "--create"}
_TAR_SHELL_OPTS = {"--to-command", "--use-compress-program", "-I", "--rsh-command", "--info-script", "-F",
                   "--new-volume-script"}


def _tar_checks(args, cwd: str, depth: int) -> None:
    """KISPROGI 6566 R2: tar -c writing its archive to stdout (-f -, or no -f: GNU tar's default) reads every file it
    packs in one step (tar -cf - store | tar -xOf -): a member naming the store, a folder holding it, the DB or the
    second-factor store is refused, each from the folder its -C chain gives it. A copy into an archive FILE stays
    allowed, like cp. The options whose value tar runs as a command (--to-command, -I, --checkpoint-action=exec=...)
    are shell."""
    parsed = _program_options("tar", args)
    for name, val, _k in parsed["opts"]:
        if val is None or depth >= 4:
            continue
        if name in _TAR_SHELL_OPTS:
            check_bash(val, cwd, depth + 1)
        elif name == "--checkpoint-action" and val.startswith("exec="):
            check_bash(val[5:], cwd, depth + 1)
    if not any(name in _TAR_CREATE for name, _v, _k in parsed["opts"]):
        return
    archives = [v for name, v, _k in parsed["opts"] if name in ("-f", "--file") and v is not None]
    if archives and archives[-1] != "-":
        return
    d, opts = cwd, [(k, v) for name, v, k in parsed["opts"] if name in ("-C", "--directory") and v is not None]
    for idx, op in enumerate(parsed["operands"]):
        for k, v in opts:
            if k <= idx and k >= 0:
                d = UNKNOWN_CWD if _unknown_place(v) or (d == UNKNOWN_CWD and not os.path.isabs(v)) else _resolve(v, d)
        opts = [(k, v) for k, v in opts if k > idx]
        if _names_protected(op, d):
            _deny("tar archivum a kimenetre a store tartalmabol (egy lepesben olvasna a dashboard adatbazisat vagy a "
                  "masodik faktor tarat); fajlba csomagolva mehet")


_ZIP_VALUE_OPTS = {"-b", "-n", "-t", "-tt", "-O", "--output-file", "-P", "--password", "-Z", "--compression-method", "-s",
                   "--split-size", "-sp"}


def _zip_to_stdout_reads_store(args, cwd: str) -> bool:
    """zip with "-" as the archive (or -O -) writes the packed files to stdout: one-step read, like tar -cf -."""
    words, pos, out_dash, i, skip_list = [_unmark(a) for a in args], [], False, 0, False
    while i < len(words):
        w = words[i]
        if w in ("-x", "-i", "--exclude", "--include"):
            skip_list = True
        elif w in _ZIP_VALUE_OPTS and i + 1 < len(words):
            out_dash = out_dash or (w in ("-O", "--output-file") and words[i + 1] == "-")
            i += 1
            skip_list = False
        elif w.startswith("-") and w != "-":
            skip_list = False
        elif not skip_list:
            pos.append(w)
        i += 1
    if not pos or not (pos[0] == "-" or out_dash):
        return False
    return any(_names_protected(src, cwd) for src in pos[1:])


def _pax_to_stdout_reads_store(args, cwd: str) -> bool:
    """pax -w without -f (or -f -) writes the archive to stdout."""
    words, ops, mode_w, mode_r, archive, i = [_unmark(a) for a in args], [], False, False, None, 0
    while i < len(words):
        w = words[i]
        if w.startswith("-") and len(w) > 1:
            letters = w[1:]
            for j, letter in enumerate(letters):
                if letter in "fsoxbBTGUp":
                    val = letters[j + 1:] or (words[i + 1] if i + 1 < len(words) else "")
                    if not letters[j + 1:]:
                        i += 1
                    if letter == "f":
                        archive = val
                    break
                mode_w = mode_w or letter == "w"
                mode_r = mode_r or letter == "r"
        else:
            ops.append(w)
        i += 1
    if not mode_w or mode_r or (archive not in (None, "-")):
        return False
    return any(_names_protected(op, cwd) for op in ops)


_MAKE_STDIN = {"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
_MAKE_SHELL_RX = re.compile(r"\$[({]shell[ \t]+([^)}]*)[)}]")


def _make_text_checks(text: str, cwd: str, depth: int) -> None:
    """KISPROGI 6566 R3: makefile text the gate can see (a makefile read from stdin, --eval): its recipe lines (after a
    tab, or after "target: ;") and $(shell ...) calls are shell, run in make's folder."""
    _code_text_checks(text, "make", cwd, depth)
    if depth >= 4:
        return
    for line in text.splitlines():
        m = re.match(r"^[^\t#=:]*::?[^=;]*;(.*)$", line)
        recipe = line[1:] if line.startswith("\t") else (m.group(1) if m else None)
        if recipe is not None:
            recipe = recipe.lstrip("@-+ \t")
            if recipe:
                check_bash(recipe, cwd, depth + 1)
    for m in _MAKE_SHELL_RX.finditer(text):
        check_bash(m.group(1), cwd, depth + 1)


def _check_pipeline(pipeline, cwd: str, depth: int, bodies, sub_text) -> None:
    """One pipeline under one cwd candidate: every rule on every member."""
    feeds_interpreter = False
    parsed = [_split_command(c) for c in pipeline]
    heads = [p[0] for p in parsed]
    for idx, (head, args, redirs, herestr, heredocs, assigns) in enumerate(parsed):
        is_interp = head in INTERPRETERS or re.fullmatch(r"python\d[\d.]*|pypy\d*|node\d*|perl\d[\d.]*|ruby\d[\d.]*|php\d[\d.]*", head or "")
        later_interp = any(h in INTERPRETERS or re.fullmatch(r"python\d[\d.]*", h or "") for h in heads[idx + 1:])
        # KISPROGI 6566 R1: code (not shell) for this member, or for the interpreter its output feeds
        later_heads0 = [h for h in heads[idx + 1:] if h in INTERPRETERS or re.fullmatch(r"python\d[\d.]*", h or "")]
        code_here = head if is_interp and head not in _SHELLS and head not in ("eval", "source", ".", "trap") else ""
        code_later = (later_heads0[0] if later_heads0 and later_heads0[0] not in _SHELLS
                      and later_heads0[0] not in ("eval", "source", ".") else "")
        feeds_interpreter = feeds_interpreter or bool(is_interp)
        # values of assignments are data unless something runs them; a protected name there is still refused
        for v in assigns:
            if _is_live_totp_word(v, cwd):
                _deny("a masodik faktor taranak neve egy valtozoban")
        # redirection targets: any protected file is a write/read of it
        for op, target in redirs:
            if _is_live_totp_word(target, cwd):
                _deny("atiranyitas a masodik faktor tarara")
            if (">" in op) and _is_live_db_path(target, cwd):
                _deny("atiranyitas a dashboard adatbazis-fajljara")
        # heredoc bodies and here-strings: code when an interpreter reads them (directly or down the pipe)
        for m in heredocs:
            body = bodies.get(m, "")
            if is_interp or (head in STDIN_TO_FILE_HEADS and later_interp):
                _code_text_checks(body, "heredoc", cwd, depth, code=code_here or (not is_interp and code_later))
        for h in herestr:
            if is_interp or later_interp:
                _code_text_checks(h, "here-string", cwd, depth, code=code_here or (not is_interp and code_later))
            elif _is_live_totp_word(h, cwd):
                _deny("a masodik faktor taranak neve")
        # env -S / --split-string: the string is split into the command line env runs (KISPROGI 6539 Q4 family); it
        # takes the command word's place, so it is checked before a command without a word is passed over
        if depth < 4:
            for text in _env_split_strings(pipeline[idx]):
                _code_text_checks(text, "env -S", cwd, depth)
                check_bash(text, cwd, depth + 1)
        if not head:
            continue
        # KISPROGI 6511 HS3: a code reader (a shell, an interpreter, eval, trap, source) that gets a substitution's
        # output as its script, its stdin (< <(...), <<< "$(...)"), its -c text or an eval word runs that output as
        # code: the same as CMD | sh (bash <(echo rm ...), eval "$(echo rm ...)", sh -c "$(echo rm ...)"). KISPROGI
        # 6524 P4: any command before an interpreter in the pipeline that reads a substitution's output (as a file
        # operand or its stdin) passes it on, as cat does (tac <(echo rm ...) | sh, head <(...) | sh).
        later_heads = [h for h in heads[idx + 1:] if h in INTERPRETERS or re.fullmatch(r"python\d[\d.]*", h or "")]
        if (is_interp or head in ("eval", "trap", "source", ".") or later_heads) and depth < 4:
            sink = later_heads[0] if not is_interp and later_heads else head
            sink = "sh" if sink in _SHELLS or sink in ("eval", "trap", "source", ".") else sink
            places = list(args) + [t for op, t in redirs if "<" in op and ">" not in op] + list(herestr)
            for w in places:
                for mk in re.findall(r"__SUBST\d+__", w):
                    if mk in sub_text:
                        check_bash(sub_text[mk] + " | " + sink, cwd, depth + 1)
        if depth < 4:
            _check_substituted_words(pipeline[idx], head, cwd, depth, sub_text)
        # KISPROGI 6511 (7), 6524 P4: xargs appends what it reads to its command: a here-string or a heredoc of its
        # own, what an echo/printf (or a cat-like command's here-string, heredoc or substitution) before it in the
        # pipeline prints, and a substitution it reads through "<" or -a / --arg-file ("xargs rm <<< store/...",
        # "cat <<< store/... | xargs rm", "xargs rm < <(echo store/...)", "xargs -a <(echo store/...) rm").
        if depth < 4 and _starts_with_xargs(pipeline[idx]):
            extra = []
            for h in herestr:
                try:
                    extra += shlex.split(h)
                except ValueError:
                    extra += h.split()
            for m in heredocs:
                extra += bodies.get(m, "").split()
            own, rest = _xargs_input_substitutions(pipeline[idx])
            for mk in own:
                if mk in sub_text:
                    check_bash(sub_text[mk] + " | " + _join_tokens(rest), cwd, depth + 1)
            if idx > 0:
                ph, pargs, predirs, pherestr, pheredocs, _ = parsed[idx - 1]
                if ph in DATA_ONLY_HEADS:
                    for line in _printed_text(ph, pargs):
                        extra += line.split()
                elif ph in _PASS_THROUGH_HEADS:
                    for h in pherestr:
                        extra += h.split()
                    for m in pheredocs:
                        extra += bodies.get(m, "").split()
                    for w in list(pargs) + [t for op, t in predirs if "<" in op and ">" not in op]:
                        for mk in re.findall(r"__SUBST\d+__", w):
                            if mk in sub_text:
                                check_bash(sub_text[mk] + " | " + _join_tokens(pipeline[idx]), cwd, depth + 1)
            if extra:
                check_bash(shlex.join([head] + [_unmark(a) for a in args] + extra), cwd, depth + 1)
        if head in ("cd", "pushd", "popd") and len(pipeline) == 1:
            continue  # the cwd candidates were worked out before (check_bash)
        if head in ("eval", "trap"):
            # KISPROGI 6497 M2: eval joins its words with spaces and runs the result as shell again, so a quoted ';'
            # among them is a separator there ("eval echo x ';' rm store/claudeclaw.db"); bash's eval drops a
            # leading "--" (KISPROGI 6505 M1). trap runs its first argument as shell when the signal comes (EXIT,
            # ERR: KISPROGI 6505 L): "trap 'rm store/claudeclaw.db' EXIT".
            words = [_unmark(a) for a in args]
            if head == "trap":
                words = [w for w in words if not re.fullmatch(r"-[lpP]+", w)]
                if words[:1] == ["--"]:
                    words = words[1:]
                words = words[:1] if len(words) >= 2 else []
            elif words[:1] == ["--"]:
                words = words[1:]
            joined = " ".join(words)
            if joined.strip():
                _code_text_checks(joined, head, cwd, depth)
                if depth < 4:
                    check_bash(joined, cwd, depth + 1)
            continue
        if is_interp:
            for a in (_unmark(a) for a in args):
                if _totp_word(a):
                    _deny("a masodik faktor tara egy ertelmezo argumentumaban")
                _code_text_checks(a, f"{head} argumentum", cwd, depth, code=code_here)
            continue
        if head in DATA_ONLY_HEADS:
            if later_interp:
                for a in args:
                    _code_text_checks(a, f"{head} -> ertelmezo", cwd, depth, code=code_later)
                # what echo / printf actually prints is one line of code ("echo -n rm store/... | sh",
                # "printf '%s ' rm store/... | sh"; KISPROGI 6505, 6511)
                for line in _printed_text(head, args):
                    _code_text_checks(line, f"{head} -> ertelmezo", cwd, depth, code=code_later)
            continue
        data_pos = _curl_data_positions(args) if head in ("curl", "wget", "http", "https", "xh") else set()
        search_pats = set()
        if head in SEARCH_HEADS or head == "git":
            search_pats = _search_pattern_positions(head, args)
            data_pos |= search_pats & _search_pattern_positions(head, args, skip_values=False)
        for k, a in enumerate(args):
            if k in data_pos or head in METADATA_HEADS:
                continue
            for piece in [a] + ([a.split("=", 1)[1]] if "=" in a else []):
                if _is_live_totp_word(piece, cwd):
                    _deny(f"a masodik faktor tara a(z) {head} parancsban")
        if head in FILE_OP_HEADS and any("{" in a for a in args):
            # bash expands a brace list into separate words before the command sees them (mv {store,/tmp/s})
            args = [v for a in args for v in (_brace_variants(a) if "{" in a else [a])]
        for d in _destinations(head, args):
            if _is_live_db_path(d, cwd):
                _deny(f"a dashboard adatbazis-fajlja a(z) {head} celjakent")
        pos_args = [a for a in args if not a.startswith("-")]
        gone = []
        if head in ("rm", "shred", "srm", "trash", "trash-put", "unlink"):
            gone = pos_args
        elif head == "mv":
            gone = pos_args[:-1]  # the store (or a folder holding it) moved away; moving INTO the root is fine
        elif head == "rsync" and any(a.startswith("--delete") for a in args):
            gone = pos_args[-1:]
        for d in gone:
            if _is_store_or_ancestor(d, cwd):
                _deny(f"a store mappa (benne a dashboard adatbazisaval) a(z) {head} celjakent")
        # cp/install/rsync/mv INTO the live store folder: a source named like the DB lands on the live DB
        if head in ("cp", "install", "rsync", "scp", "mv", "ln", "ditto"):
            pos = [a for a in args if not a.startswith("-")]
            tdirs = [d for d in _destinations(head, args) if not _db_name_matches(os.path.basename(d.rstrip("/")))]
            if len(pos) >= 2 and any(_resolve(t, cwd) == os.path.realpath(STORE) for t in tdirs):
                if any(_db_name_matches(os.path.basename(src.rstrip("/"))) for src in pos[:-1]):
                    _deny(f"a dashboard adatbazis-fajljanak felulirasa a store mappaba masolva ({head})")
        if head in SEARCH_HEADS and _recursive_search_on_store(head, args, cwd):
            _deny(f"rekurziv kereses ({head}) a store mappaban (a rejtett masodik faktor tarat is olvasna)")
        if head == "git" and _git_clean_on_store(args, cwd):
            _deny("git clean -x/-X a store mappara (a gitignore-olt store-t, benne a dashboard adatbazisat torolne)")
        if head == "git" and _git_grep_on_store(args, cwd):
            _deny("git grep --no-index/--untracked a store mappaban (a rejtett masodik faktor tarat is olvasna)")
        if head in ("tar", "gtar", "bsdtar"):
            _tar_checks(args, cwd, depth)
        if head == "zip" and _zip_to_stdout_reads_store(args, cwd):
            _deny("zip archivum a kimenetre a store tartalmabol (egy lepesben olvasna); fajlba csomagolva mehet")
        if head == "pax" and _pax_to_stdout_reads_store(args, cwd):
            _deny("pax archivum a kimenetre a store tartalmabol (egy lepesben olvasna); fajlba csomagolva mehet")
        if head in ("make", "gmake"):
            mk = _program_options(head, args)
            texts = [v for name, v, _k in mk["opts"] if name == "--eval" and v]
            if any(name in ("-f", "--file", "--makefile") and v in _MAKE_STDIN for name, v, _k in mk["opts"]):
                texts += [bodies.get(m, "") for m in heredocs] + list(herestr)
                if idx > 0 and parsed[idx - 1][0] in DATA_ONLY_HEADS:
                    texts += ["\n".join(_printed_text(parsed[idx - 1][0], parsed[idx - 1][1]))]
            for t in texts:
                _make_text_checks(t, cwd, depth)
        if head == "find":
            # KISPROGI 6497 L1: any later xargs in the pipeline gets find's output (find store | sort | xargs rm)
            piped = [[parsed[j][0]] + parsed[j][1] for j in range(idx + 1, len(pipeline))
                     if parsed[j][0] and _starts_with_xargs(pipeline[j])]
            code_sink = any(_reads_code_from_stdin(h, a) for h, a, *_ in parsed[idx + 1:])
            _check_find(args, cwd, depth, piped=piped, feeds_interpreter=code_sink)


_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "busybox"}
_PRINTF_SPEC = re.compile(r"%([-+ #0]*)(\*|\d+)?(?:\.(\*|\d*))?([diouxXeEfFgGaAcsbq%])")
_SIMPLE_ESC = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
               "\\": "\\"}


def _bash_escapes(text: str, mode: str):
    r"""bash's backslash escapes as one of its builtins prints them (KISPROGI 6524 P3), measured in bash 5:
    mode "echo" (echo -e): \0nnn is octal (a 0 and up to three digits), \1..\7 stay as they are, \c ends ALL output;
    mode "b" (printf's %b argument): \0nnn and \nnn are octal, \c ends all output;
    mode "fmt" (printf's format): \nnn is octal, \c stays as it is.
    \xHH, \uHHHH, \UHHHHHHHH and \a \b \e \f \n \r \t \v \\ in all three; any other \X stays (printf's format drops the
    backslash of \' \" \?). Returns (text, ended)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        e = text[i + 1]
        if e in _SIMPLE_ESC:
            out.append(_SIMPLE_ESC[e])
            i += 2
        elif e in "01234567" and (mode != "echo" or e == "0"):
            most = 4 if (e == "0" and mode in ("echo", "b")) else 3
            m = re.match(r"[0-7]{1,%d}" % most, text[i + 1:])
            out.append(chr(int(m.group(0), 8) & 0xFF))
            i += 1 + len(m.group(0))
        elif e in "xuU":
            m = re.match(r"[0-9A-Fa-f]{1,%d}" % {"x": 2, "u": 4, "U": 8}[e], text[i + 2:])
            if m:
                try:
                    out.append(chr(int(m.group(0), 16)))
                except ValueError:
                    out.append("?")
                i += 2 + len(m.group(0))
            else:
                out.append(text[i:i + 2])
                i += 2
        elif e == "c" and mode in ("echo", "b"):
            return "".join(out), True
        elif e in "'\"?" and mode == "fmt":
            out.append(e)
            i += 2
        else:
            out.append(text[i:i + 2])
            i += 2
    return "".join(out), False


def _printf_output(fmt: str, args):
    r"""What bash's printf prints (KISPROGI 6524 P3): the format's own escapes, %s / %b / %c / %q with width, - flag,
    precision and * (taken from the words), %b's escapes (\c ends all output), the format reused while words remain."""
    fmt_text, _ = _bash_escapes(fmt, "fmt")
    words, out, rounds = list(args), [], 0
    while True:
        used, ended = False, False

        def take():
            nonlocal used
            used = True
            return words.pop(0) if words else ""

        def number(w: str) -> int:
            try:
                return int(w)
            except ValueError:
                return 0
        pos = 0
        for m in _PRINTF_SPEC.finditer(fmt_text):
            out.append(fmt_text[pos:m.start()])
            pos = m.end()
            flags, width, prec, conv = m.groups()
            if conv == "%":
                out.append("%")
                continue
            if width == "*":
                width = str(number(take()))
            if prec == "*":
                prec = str(number(take()))
            arg = take()
            if conv == "s":
                val = arg
            elif conv == "b":
                val, ended = _bash_escapes(arg, "b")
            elif conv == "c":
                val = arg[:1]
            elif conv == "q":
                val = shlex.quote(arg) if arg else "''"
            else:
                val = str(number(arg))
            if prec is not None and conv in "sbq":
                val = val[:number(prec or "0")]
            w = number(width) if width else 0
            val = val.ljust(w) if "-" in flags else val.rjust(w)
            out.append(val)
            if ended:
                break
        if ended:
            break
        out.append(fmt_text[pos:])
        rounds += 1
        if not used or not words or rounds > 32:
            break
    return "".join(out)


def _printed_text(head: str, args):
    r"""What echo / printf print, as the lines to check (KISPROGI 6511 (6), 6524 P3): echo without its leading -n/-e/-E
    words (the last of -e/-E decides; escapes decoded the echo -e way, \c ends the output), joined by spaces; printf
    without a leading -- (and -v NAME), its output as bash makes it, plus its words joined (a second, plain reading)."""
    words = [_unmark(a) for a in args]
    if head == "echo":
        escapes = False
        while words and re.fullmatch(r"-[neE]+", words[0]):
            for ch in words.pop(0)[1:]:
                if ch in "eE":
                    escapes = ch == "e"
        if not words:
            return []
        if not escapes:
            return [" ".join(words)]
        printed = []
        for w in words:
            text, ended = _bash_escapes(w, "echo")
            printed.append(text)
            if ended:
                break
        return [" ".join(printed)]
    if words[:1] == ["-v"]:
        words = words[2:]
    if words[:1] == ["--"]:
        words = words[1:]
    if not words:
        return []
    lines = [_printf_output(words[0], words[1:])]
    if words[1:]:
        lines.append(" ".join(words[1:]))
    return lines


_PASS_THROUGH_HEADS = {"cat", "tac", "head", "tail", "sort", "uniq", "tee"}


def _join_tokens(toks) -> str:
    """Tokens back to shell text: operators as they are, every other word quoted."""
    return " ".join(t if (t in REDIRS or t == "<<" or re.fullmatch(r"[<>&|]+", t)) else shlex.quote(_unmark(t))
                    for t in toks)


def _printer_output(sub: str):
    """What a substitution prints when it is ONE echo / printf (or cat <<< WORD / cat <<EOF) command, else None."""
    if re.search(r"[$`]\(|`", sub):
        return None
    try:
        text, bodies, meta = _strip_heredocs(sub)
        _resolve_heredoc_refs(bodies, meta)
        pipes, _ends = _pipelines(_tokens(_decode_dollar_quotes(text)))
    except ValueError:
        return None
    if len(pipes) != 1 or len(pipes[0]) != 1:
        return None
    head, args, _redirs, herestr, heredocs, _assigns = _split_command(pipes[0][0])
    if head in DATA_ONLY_HEADS:
        lines = _printed_text(head, args)
        return lines[0] if lines else ""
    if head == "cat" and not args and herestr:
        return herestr[0]
    if head == "cat" and not args and heredocs:
        return bodies.get(heredocs[0], "")
    return None


def _check_substituted_words(toks, head: str, cwd: str, depth: int, sub_text) -> None:
    """KISPROGI 6524 P4: a substitution standing as the command itself runs its output as a command: checked as
    SUB | sh ($(echo rm store/...), `echo rm store/...`). And a word built from an echo / printf substitution is the
    text it prints, so the command is checked again with it in place, word-split and as one word (rm $(echo
    store/claudeclaw.db), $(echo rm) store/..., r$(echo m) store/...)."""
    for mk in re.findall(r"__SUBST\d+__", head):
        if mk in sub_text:
            check_bash(sub_text[mk] + " | sh", cwd, depth + 1)
    known = {}
    for t in toks:
        for mk in re.findall(r"__SUBST\d+__", t):
            if mk in sub_text and mk not in known:
                out = _printer_output(sub_text[mk])
                if out is not None:
                    known[mk] = out.rstrip("\n")
    if not known:
        return
    for split in (True, False):
        words = []
        for t in toks:
            if split and t in known:
                words.extend(known[t].split())
                continue
            for mk, out in known.items():
                t = t.replace(mk, out)
            words.append(t)
        check_bash(_join_tokens(words), cwd, depth + 1)


def _xargs_input_substitutions(toks):
    """An xargs member's input substitutions, "< SUBST" and -a / --arg-file SUBST, and its tokens without them."""
    srcs, rest, k, in_opts = [], [], 0, False
    while k < len(toks):
        t = toks[k]
        nxt = toks[k + 1] if k + 1 < len(toks) else ""
        if t == "<" and re.search(r"__SUBST\d+__", nxt):
            srcs += re.findall(r"__SUBST\d+__", nxt)
            k += 2
            continue
        if os.path.basename(t) == "xargs":
            in_opts = True
        elif in_opts and not t.startswith("-"):
            in_opts = False
        elif in_opts and t in ("-a", "--arg-file") and re.search(r"__SUBST\d+__", nxt):
            srcs += re.findall(r"__SUBST\d+__", nxt)
            k += 2
            continue
        elif in_opts and re.fullmatch(r"(?:-a|--arg-file=).*__SUBST\d+__.*", t):
            srcs += re.findall(r"__SUBST\d+__", t)
            k += 1
            continue
        rest.append(t)
        k += 1
    return srcs, rest


def _reads_code_from_stdin(head: str, args) -> bool:
    """Whether a pipeline member runs its stdin as code: a shell without -c or a script file, an interpreter with no
    script or with "-", sqlite3 with a database and no SQL argument. awk, or python3 with a script, reads it as data."""
    pos = [a for a in args if not a.startswith("-")]
    if head in _SHELLS:
        return "-s" in args or not ("-c" in args or any(re.fullmatch(r"-[a-z]*c[a-z]*", a) for a in args) or pos)
    if head == "sqlite3":
        return len(pos) <= 1
    if head in INTERPRETERS or re.fullmatch(r"python\d[\d.]*|pypy\d*|node\d*|perl\d[\d.]*|ruby\d[\d.]*|php\d[\d.]*", head or ""):
        if head in ("awk", "gawk", "mawk", "nawk", "eval", "source", ".", "osascript"):
            return False
        return "-" in args or not (pos or any(a in ("-c", "-e", "-m", "--eval", "-r") for a in args))
    return False


def _starts_with_xargs(toks) -> bool:
    """Whether a pipeline member runs xargs (after assignments and other wrappers)."""
    return "xargs" in _unwrap([t for t in toks if t not in REDIRS])["wrappers"]


# grep and its kin: options whose value is the next word (a pattern, a number, a glob), so it is not a file operand.
# The grep set covers GNU grep and ugrep 7.8 (the agents' grep is ugrep, a Claude Code shell function; KISPROGI 6497).
_GREP_VALUE_OPTS = {"-e", "--regexp", "-f", "--file", "-m", "--max-count", "-A", "-B", "-C", "-d", "-D", "--directories",
                    "--devices", "--include", "--exclude", "--exclude-dir", "--include-dir", "--label", "--binary-files",
                    "-t", "--file-type", "-O", "--file-extension", "-g", "--glob", "--iglob", "-M",
                    "--file-magic", "-N", "--neg-regexp", "-K", "--range", "-J", "--jobs", "--include-from",
                    "--exclude-from", "--and", "--andnot", "--not"}
_GREP_VALUE_LETTERS = "efmABCdDtOgMNKJ"
_RG_VALUE_OPTS = {"-e", "--regexp", "-f", "--file", "-m", "--max-count", "-A", "-B", "-C", "-g", "--glob", "--iglob", "-t",
                  "--type", "-T", "--type-not", "-j", "--threads", "-M", "--max-columns", "-d", "--max-depth", "-E",
                  "--encoding", "--pre", "--pre-glob", "--sort", "--sortr", "--type-add", "--type-clear", "--ignore-file",
                  "--max-filesize", "-r", "--replace", "--path-separator", "--colors", "--context-separator", "-G",
                  "--field-match-separator", "--field-context-separator", "--engine"}
_RG_VALUE_LETTERS = "efmABCgtTjMdEr"
_GREP_HEADS = ("grep", "egrep", "fgrep", "zgrep", "rgrep", "ugrep")
# The names-only exception holds only when every option is one of these (KISPROGI 6497 M3: ugrep's --format, --json,
# --csv, --xml, --replace and the like print lines even beside -l). Short letters without a value, then long options.
_NAMES_ONLY_SHORT = set("rRlLcqsiwxFEGPHhIavnj0123456789")
_NAMES_ONLY_LONG = {"--recursive", "--dereference-recursive", "--files-with-matches", "--files-without-match", "--count",
                    "--quiet", "--silent", "--no-messages", "--ignore-case", "--no-ignore-case", "--smart-case",
                    "--word-regexp", "--line-regexp", "--fixed-strings", "--extended-regexp", "--basic-regexp",
                    "--perl-regexp", "--with-filename", "--no-filename", "--null", "--text", "--invert-match",
                    "--line-number", "--ignore-binary", "--hidden", "--ignore-files", "--no-ignore-files", "--binary-files",
                    "--include", "--exclude", "--exclude-dir", "--directories", "--devices", "--regexp", "--file",
                    "--max-count", "--color", "--colour", "--encoding", "--depth"}
# options that pick files besides --include / --exclude / --exclude-dir: with one of them the include/exclude reading
# below proves nothing
_GREP_SELECT_OPTS = {"-g", "--glob", "--iglob", "-O", "--file-extension", "-t", "--file-type", "-M", "--file-magic",
                     "--include-from", "--exclude-from", "--include-dir", "--include-fs", "--filter", "--glob-ignore-case"}


def _glob_may_match_totp(g: str, ci: bool = False) -> bool:
    if not g or "[" in g or "\\" in g or "{" in g or g.startswith("!") or TOTP_RX.search(g):
        return True
    last = g.rstrip("/").split("/")[-1]
    return not last or fnmatch.fnmatchcase(TOTP_NAME.lower() if ci else TOTP_NAME, last.lower() if ci else last)


def _grep_include_rules_out(order) -> bool:
    """GNU grep: the LAST --include / --exclude that matches a file decides; if none matches, the file is searched
    unless the FIRST of them is an --include (KISPROGI 6497 M3: "--exclude=zz --include='*.log'" searched the secret).
    Ruled out here only when every matching one is an --exclude, or none matches and the first is an --include. A glob
    that cannot be read here counts as matching for --include and as not matching for --exclude."""
    if not order:
        return False

    def matches(kind: str, g: str) -> bool:
        readable = bool(g) and not g.startswith("!") and not any(c in g for c in "[\\{")
        if not readable:
            return kind == "include"
        if kind == "exclude" and "/" in g:
            # KISPROGI 6505 H1: GNU grep -r compares a base name, so a glob with a slash excludes nothing, and ugrep's
            # trailing slash matches folders only ("--exclude=x/.dashboard-totp.json", "--exclude='.dash*/'", "*/")
            return False
        return fnmatch.fnmatchcase(TOTP_NAME, g.rstrip("/").split("/")[-1])

    hits = [kind for kind, g in order if matches(kind, g)]
    if hits:
        return all(k == "exclude" for k in hits)  # GNU: the last match decides; ugrep: taken the strict way
    return order[0][0] == "include"


# Explicit pattern sources (KISPROGI 6524 V1/P1): with one of them no positional word is the pattern, so every
# positional word is a file operand. A value option is a PATTERN (a search string, data) or a FILE of patterns (read,
# so its value stays checked as a path). ugrep's --and/--andnot/--not take a pattern ("--and -e P" is the flag alone).
_GREP_PATTERN_LONG = ("--regexp", "--and", "--andnot", "--not")
_RG_PATTERN_LONG = ("--regexp",)
_PATTERN_FILE_LONG = ("--file",)


def _search_walk(head: str, args, skip_values: bool = True) -> dict:
    """KISPROGI 6524 V1/P1: the ONE reading of a search command's words that both the pattern exemption and the
    recursive-search rule use (two hand-written readers drifted apart twice: R1, then V1). An explicit pattern source
    is found in every spelling: -e P / -f F, --regexp P, --regexp=P, --file=F, attached -eP / -fF, and as the first
    value letter of a short group (-rie P, -rf F, -uue P, -fpats), for grep (GNU and ugrep) and rg alike; git grep / log
    take -e / -f / -S / -G. skip_values: a value option (-m 5, -d rec, -sd rec) takes its value; without it only a
    pattern source does (the second reading of the exemption). Returns pats (indexes that are pattern text), explicit,
    operands (indexes of the file operands), and for the recursive rule rec, quiet, plain, order, globs, select."""
    r = {"pats": set(), "explicit": False, "operands": [], "rec": False, "quiet": False, "plain": True, "order": [],
         "globs": [], "select": False}
    git = head == "git"
    offset = 0
    positional_pattern = True
    if git:
        if not args or args[0] not in ("grep", "log"):
            return r
        positional_pattern = args[0] == "grep"
        offset, args = 1, args[1:]
    grep = head in _GREP_HEADS
    value_opts = _GREP_VALUE_OPTS if grep else _RG_VALUE_OPTS
    value_letters = _GREP_VALUE_LETTERS if grep else _RG_VALUE_LETTERS
    pattern_long = _GREP_PATTERN_LONG if grep else _RG_PATTERN_LONG
    r["rec"] = head == "rgrep" or not grep
    positionals, k, n, after_dd = [], 0, len(args), False
    while k < n:
        a = args[k]
        nxt = args[k + 1] if k + 1 < n else ""
        if after_dd or a == "-" or not a.startswith("-"):
            positionals.append(k)
            k += 1
            continue
        if a == "--":
            after_dd = True
            k += 1
            continue
        if git:
            if a in ("-e", "--regexp", "-S", "-G") and k + 1 < n:
                r["pats"].add(k + 1 + offset)
                r["explicit"] = True
                k += 2
                continue
            if re.fullmatch(r"-[eSG].+|--regexp=.+", a):
                r["pats"].add(k + offset)
                r["explicit"] = True
            elif a in ("-f", "--file"):
                r["explicit"] = True
                k += 1
            elif re.fullmatch(r"-f.+|--file=.+", a):
                r["explicit"] = True
            k += 1
            continue
        if a.startswith("--"):
            opt, has_val, val = a.partition("=")
            if grep:
                opt = _gnu_grep_long(opt)
            if opt not in _NAMES_ONLY_LONG:
                r["plain"] = False
            if opt in pattern_long:
                r["explicit"] = True
                if has_val:
                    r["pats"].add(k + offset)
                elif opt in ("--and", "--andnot", "--not") and nxt == "-e":
                    pass  # ugrep: --and -e PATTERN, the -e comes next
                elif k + 1 < n:
                    r["pats"].add(k + 1 + offset)
                    k += 1
                k += 1
                continue
            if opt in _PATTERN_FILE_LONG:
                r["explicit"] = True
                k += 1 if has_val else 2
                continue
            takes = not has_val and opt in value_opts
            if takes:
                val = nxt
            if _gnu_recursive_long(opt, val if has_val or takes else None) or (
                    grep and not has_val and _gnu_long_prefix(opt, "--directories", 4) and _recurse_value(nxt)):
                r["rec"] = True
            if opt in ("--files-with-matches", "--files-without-match", "--count", "--count-matches", "--quiet",
                       "--silent", "--files"):
                r["quiet"] = True
            if opt in ("--include", "--exclude"):
                r["order"].append((opt[2:], val))
            elif opt in ("--glob", "--iglob"):
                r["globs"].append((val, opt == "--iglob"))
            if (grep and opt in _GREP_SELECT_OPTS) or (not grep and opt in ("--type", "--type-not", "--type-add")):
                r["select"] = True
            k += 2 if takes and skip_values else 1
            continue
        # a short group: letters left to right; the first one that takes a value takes the rest of the word, or else
        # the next word (getopt): -rie P, -ieP, -rf F, -fF, -sd rec, -C5
        consumed = 0
        for pos, ch in enumerate(a[1:], 1):
            rest = a[pos + 1:]
            if ch in "rR":
                r["rec"] = r["rec"] or grep
            if ch in "lLcq" and (grep or ch in "lc"):
                r["quiet"] = True
            if ch == "e":
                r["explicit"] = True
                if rest:
                    r["pats"].add(k + offset)
                elif k + 1 < n:
                    r["pats"].add(k + 1 + offset)
                    consumed = 1
                break
            if ch == "f":
                r["explicit"] = True
                consumed = 0 if rest else 1
                break
            if ch in value_letters:
                val = rest or nxt
                if not rest and skip_values:
                    consumed = 1
                if ch == "d" and grep and _recurse_value(val):
                    r["rec"] = True
                if ch == "g":
                    r["globs"].append((val, False))
                if ch not in "efmdD":
                    r["plain"] = False
                if (grep and ch in "gOtM") or (not grep and ch in "tT"):
                    r["select"] = True
                break
            if ch not in _NAMES_ONLY_SHORT:
                r["plain"] = False
        k += 1 + consumed
    if not r["explicit"] and positionals and positional_pattern:
        r["pats"].add(positionals[0] + offset)
        positionals = positionals[1:]
    r["operands"] = [p + offset for p in positionals]
    return r


def _recursive_search_on_store(head: str, args, cwd: str) -> bool:
    """KISPROGI 6490 M1 and 6497 H2/M3/L2: a search that can read the second-factor store in the live store folder and
    print its lines. Aimed at the store: a folder operand that is (or a glob / brace that expands to) the store, or the
    store as cwd with grep -r and no operand. The grep family counts as recursive on a folder operand even without -r
    (the agents' grep is ugrep, which searches folders recursively and hidden files too); rgrep is grep -r; rg, ag
    and ack always recurse. It passes, for the grep family only, when it prints names or counts (-l, -L, -c, -q with no
    option outside a small known set), or when its --include / --exclude rule the file out in GNU's order; for rg, when
    its include globs (-g / --glob / --iglob, the last case-insensitively) cannot match the file. A search below the
    store cannot reach the file (it sits in the store itself); a search from the install root or above stays a known
    open route (it is every everyday search), and so does a names-only search used as an oracle (L3). The words are
    read by _search_walk, the same reader the pattern exemption uses (KISPROGI 6524 V1)."""
    grep = head in _GREP_HEADS
    w = _search_walk(head, args)
    operands = [args[k] for k in w["operands"]]
    rec = w["rec"]
    if operands:
        aimed = any(_is_store_dir(o, cwd) for o in operands)
    else:
        aimed = rec and cwd != UNKNOWN_CWD and _resolve(".", cwd) == os.path.realpath(STORE)
    if not aimed:
        return False
    if grep:
        if w["quiet"] and w["plain"]:
            return False  # names or counts only: the real everyday store searches are all grep -rl (replay 2026-11-28)
        if not w["select"] and _grep_include_rules_out(w["order"]):
            return False
        return True
    if head == "rg" and not w["select"] and w["globs"]:
        include = [(g, ci) for g, ci in w["globs"] if not g.startswith("!")]
        if include and not any(_glob_may_match_totp(g, ci) for g, ci in include):
            return False
    return True


def _env_split_strings(toks):
    """The command lines wrappers run from a string before the command word: env -S / --split-string, flock -c."""
    u = _unwrap([t for t in toks if t not in REDIRS])
    return u["splits"] + u["shells"]


def _git_clean_on_store(args, cwd: str) -> bool:
    """git clean -x / -X removes ignored files, and the store folder is git-ignored: from the install root (or with a
    pathspec reaching the store) it deletes the live DB. A dry run (-n) passes. (KISPROGI 6539 Q4: git -C ROOT clean
    -fdX store.)"""
    d, k = cwd, 0
    while k < len(args) and args[k].startswith("-"):
        if args[k] == "-C" and k + 1 < len(args):
            v = _unmark(args[k + 1])
            d = UNKNOWN_CWD if (_unknown_place(v) or d == UNKNOWN_CWD and not os.path.isabs(v)) else _resolve(v, d)
            k += 2
            continue
        k += 2 if args[k] in ("-c", "--git-dir", "--work-tree", "--namespace") else 1
    if k >= len(args) or args[k] != "clean":
        return False
    rest, specs, after_dd, ignored, dry = args[k + 1:], [], False, False, False
    for a in rest:
        a = _unmark(a)
        if after_dd or not a.startswith("-"):
            specs.append(a)
        elif a == "--":
            after_dd = True
        elif a == "--dry-run":
            dry = True
        elif re.fullmatch(r"-[A-Za-z]+", a):
            dry = dry or "n" in a
            ignored = ignored or "x" in a or "X" in a
    if dry or not ignored:
        return False
    for sp in specs or ["."]:
        sp = re.sub(r"^:\([^)]*\)|^:[!^/]?", "", sp) or "."
        if d == UNKNOWN_CWD and not os.path.isabs(sp):
            return True
        if _vague_word(sp):
            if _is_store_or_ancestor(".", d) or _is_store_dir(os.path.dirname(sp) or ".", d):
                return True
            continue
        if _is_store_or_ancestor(sp, d) or _is_store_dir(sp, d) or _is_live_db_path(sp, d) or _is_live_totp_word(sp, d):
            return True
    return False


def _git_grep_on_store(args, cwd: str) -> bool:
    """KISPROGI 6497 L2: git grep --no-index (or --untracked) searches files git does not track, so aimed at the store
    (a pathspec, or the store as cwd) it reads the second-factor store like grep -r."""
    if not args or args[0] != "grep" or not any(a in ("--no-index", "--untracked") for a in args[1:]):
        return False
    rest, specs, explicit, after_dd, k = args[1:], [], False, False, 0
    pattern_seen = False
    while k < len(rest):
        a = rest[k]
        if after_dd:
            specs.append(a)
        elif a == "--":
            after_dd = True
        elif a in ("-e", "-f", "-A", "-B", "-C", "-m", "--max-count", "--max-depth", "--threads"):
            explicit = explicit or a in ("-e", "-f")
            k += 1
        elif a.startswith("-"):
            explicit = explicit or bool(re.fullmatch(r"-e.+|--regexp=.+", a))
        elif not explicit and not pattern_seen:
            pattern_seen = True
        else:
            specs.append(a)
        k += 1
    if specs:
        return any(_is_store_dir(re.sub(r"^:\([^)]*\)|^:[!^/]?", "", sp) or ".", cwd) for sp in specs)
    return cwd != UNKNOWN_CWD and _resolve(".", cwd) == os.path.realpath(STORE)


# --------------------------------------------------------------------------------------------------------------
# Path tools
# --------------------------------------------------------------------------------------------------------------

def _grep_glob_may_match_totp(g: str) -> bool:
    """Whether the Grep tool's glob can let rg search the store's file. Claude Code (2.1.326) splits the glob on white
    space and then on commas (a piece holding both braces stays whole) and passes each piece as its own --glob, so a
    file is searched when any include piece matches it; with only "!" (exclude) pieces everything else is searched.
    A brace, a [ or a backslash count as "may match" (KISPROGI 6490 M1: Python's fnmatch reads [^x] and \\. differently
    from rg, and both let rg print the secret line), and so does the file's name anywhere in the glob."""
    if not g.strip() or "{" in g or "[" in g or "\\" in g or TOTP_RX.search(g):
        return True
    pieces = [p for chunk in g.split() for p in chunk.split(",") if p]
    include = [p for p in pieces if not p.startswith("!")]
    if not include:
        return True
    for p in include:
        last = p.split("/")[-1]
        if not last or fnmatch.fnmatchcase(TOTP_NAME, last):
            return True
    return False


def check_path_tool(tool: str, ti: dict, cwd: str) -> None:
    """Path checks only. A script file's TEXT is not checked: the install's own sources (db.ts, dashboard-totp.ts,
    their tests) legitimately hold these names and this SQL, worktrees sit outside the live root, and writing a
    script to run it later is the same two-step route a heredoc to cat already is (a text brake, documented)."""
    path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or ""
    if not isinstance(path, str):
        path = ""
    if path and _is_totp_path(path, cwd):
        _deny(f"{tool} a masodik faktor taran")
    if tool == "Grep":
        # KISPROGI 6483 M1: the Grep tool runs rg --hidden (Claude Code 2.1.326), and with an explicit path in the store
        # it reads the git-ignored hidden second-factor store too, printing the matching line. So a search in the live
        # store folder (or below it; no path = the cwd) is refused unless a glob narrows it to names the store's file
        # cannot have. From the install root rg skips the git-ignored store; a glob naming the file is refused there.
        g = ti.get("glob")
        target = _resolve(path or ".", cwd)
        store = os.path.realpath(STORE)
        if target == store or target.startswith(store + os.sep):
            if not (isinstance(g, str) and g.strip() and not _grep_glob_may_match_totp(g)):
                _deny("Grep a store mappaban (a rejtett masodik faktor tarat is olvasna); szukitsd egy ra nem illo globbal")
        elif target in (os.path.realpath(ROOT),) and isinstance(g, str) and g and _grep_glob_may_match_totp(g):
            _deny("Grep a masodik faktor tarara illo mintaval")
        return
    if tool == "Read":
        return
    if path and _is_live_db_path(path, cwd):
        _deny(f"{tool} a dashboard adatbazis-fajljan")


PATH_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Grep"}


def decide(payload: dict):
    """None (pass) or the refusal reason."""
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input")
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ROOT
    if tool != "Bash" and tool not in PATH_TOOLS:
        return None
    if not isinstance(ti, dict):
        return "a hivas parameterei nem olvashatok (fail-closed)"
    try:
        if tool == "Bash":
            cmd = ti.get("command")
            if isinstance(cmd, str) and cmd.strip():
                check_bash(cmd, cwd)
        else:
            check_path_tool(tool, ti, cwd)
    except Deny as d:
        return str(d)
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # not a tool call
    if not isinstance(payload, dict):
        sys.exit(0)
    why = decide(payload)
    if why is not None:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": MSG.format(why=why)}}))
    sys.exit(0)


if __name__ == "__main__":
    main()

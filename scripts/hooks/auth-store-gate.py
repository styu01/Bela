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
text). Matched against a fixed vocabulary only: anything else passes. Unparsable Bash that mentions one of the
protected names is refused (fail closed); unparsable Bash without them passes. Above 200 KB only the text rules run,
and then the word "store" or a dot-name glob is refused too.

Protocol: JSON on stdin ({tool_name, tool_input, cwd}); a refusal is a PreToolUse deny decision on stdout, exit 0.
"""
import fnmatch
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
WRAPPERS = {"sudo", "doas", "env", "nice", "nohup", "timeout", "stdbuf", "command", "builtin", "exec", "time",
            "xargs", "ionice", "chrt", "setsid", "unbuffer", "flock", "caffeinate", "taskset", "chronic", "then",
            "do", "else", "if", "while", "until", "!", "{", "(", "elif"}
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


def _code_text_checks(text: str, where: str, cwd: str, depth: int) -> None:
    """Text a program runs: all three rules, and shell text is parsed again (a nested command)."""
    if _sql_write(text):
        _deny(f"SQL-iras a dashboard_users tablara ({where})")
    if TOTP_RX.search(text):
        _deny(f"a masodik faktor tara egy futtatott szovegben ({where})")
    if _code_writes_db(text) or _sqlite_dot_writes_live_db(text, cwd):
        _deny(f"a dashboard adatbazis-fajljanak irasa egy futtatott kodban ({where})")
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
_HEREDOC_RX = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z0-9_.\-]+))")
_ASSIGN_INDEX_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\[")
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


def _strip_heredocs(cmd: str):
    """Heredoc bodies and comments out of the text: (text with a marker word per heredoc, {marker: body}).

    One left-to-right scan of the WHOLE text with bash's lexical contexts (KISPROGI 6524 P2): quotes ('...', "...",
    $'...') carry across lines, a backslash escapes, and a << is a heredoc only in shell code: not after an escape
    (echo \\<<2), not in a comment (true # <<2), not in arithmetic ($((...)), ((...)), $[...], also over several lines),
    not in ${...} (${PATH:1<<2}) and not in an array assignment's index (a[1<<2]=x). Read as heredocs, each of these hid
    every following line from the gate. A comment (a # starting a word in shell code, not inside backticks) is cut out
    as bash does, so a quote inside it (# it's) can no longer swallow the following lines into one quoted word. The
    bodies start after the next newline in shell code, one per pending heredoc, each up to its delimiter line."""
    out, bodies, pending = [], {}, []
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
            for marker, delim, strip_tabs in pending:
                body = []
                while i < n:
                    j = cmd.find("\n", i)
                    j = n if j < 0 else j
                    line = cmd[i:j]
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
            out.append(cmd[i:i + 2])
            if cmd[i + 1] != "\n":
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
                m = _HEREDOC_RX.match(cmd, i)
                if m:
                    delim = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
                    marker = f"__HEREDOC{count}__"
                    count += 1
                    pending.append((marker, delim, m.group(1) == "-"))
                    out.append("<< " + marker)
                    i = m.end()
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
            if kind in _CODE_CTX and cmd.startswith("$'", i):
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
        if kind in _CODE_CTX:
            word_start = c in " \t\r;&|()<>"
    for marker, _delim, _strip in pending:
        bodies[marker] = ""
    return "".join(out), bodies


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


def _tokens(text: str):
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
    # assignments and wrappers before the real command word
    i = 0
    assigns = []
    while i < len(words):
        w = words[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w, re.S):
            assigns.append(w.split("=", 1)[1])
            i += 1
            continue
        base = os.path.basename(w)
        if base in WRAPPERS:
            i += 1
            # skip the wrapper's own options and operands (env assignments, durations, nice levels)
            while i < len(words) and (words[i].startswith("-") or re.fullmatch(r"[0-9.]+[smhd]?", words[i])
                                      or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[i], re.S)):
                takes_arg = r"-[IiLPdEansx]|-[nkus]" if base == "xargs" else r"-[nkus]|--signal|--kill-after|-o|-e|-i"
                if re.fullmatch(takes_arg, words[i]) and i + 1 < len(words):
                    i += 1
                i += 1
            continue
        break
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
    text, bodies = _strip_heredocs(cmd)
    text = _decode_dollar_quotes(text)
    text, subs = _strip_substitutions(text)
    sub_text = {f"__SUBST{k}__": sub for k, sub in enumerate(subs)}
    try:
        pipes, ends = _pipelines(_tokens(text))
    except ValueError:
        pipes = ends = None
    # KISPROGI 6524 V2/P5: the cwd is a SET of places a command may run in. A cd adds its target and does not drop the
    # old place (it may fail: a missing folder; it may sit in a branch, a function body, a background job or a case
    # arm); only the commands joined to it by && run in the target alone (they run only if it succeeded). cd - / ~- /
    # popd add an unknown place, and every path is checked under every candidate. A subshell's own cds end with it.
    sets, every = [], {cwd: None}
    if pipes is not None:
        cur, base, stack = [cwd], [cwd], []
        for pl, end in zip(pipes, ends):
            if pl == [[SUBSHELL_OPEN]]:
                stack.append((cur, base))
                base = cur
                sets.append(None)
                continue
            if pl == [[SUBSHELL_CLOSE]]:
                cur, base = stack.pop() if stack else (cur, base)
                if end != "&&":
                    cur = base
                sets.append(None)
                continue
            sets.append(cur)
            targets = None
            if len(pl) == 1:
                head, args = _split_command(pl[0])[:2]
                if head in ("cd", "pushd", "popd"):
                    targets = _cd_targets(cur, head, args, cmd)
                    every.update(dict.fromkeys(targets))
            if targets is not None and end == "&&" and pl[0][:1] != ["!"]:
                cur, base = targets, list(dict.fromkeys(base + targets))
            elif targets is not None:
                base = list(dict.fromkeys(base + targets))
                cur = base
            elif end != "&&":
                cur = base
    for s_ in subs:
        # a substitution is shell that runs, in any place the command may be by then (its data positions stay data)
        for c in every:
            if depth < 4:
                check_bash(s_, c, depth + 1, best_effort)
            elif _unparsable_hits(s_):
                _deny("tul mely parancs-behelyettesites a vedett nevekkel (fail-closed)")
    if pipes is None:
        if not best_effort and _unparsable_hits(cmd):
            _deny("nem elemezheto parancs a vedett nevekkel (fail-closed)")
        return
    for pipeline, cands in zip(pipes, sets):
        if cands is None:
            continue
        for c in cands:
            try:
                _check_pipeline(pipeline, c, depth, bodies, sub_text)
            except Deny as d:
                if len(cands) > 1 and c != cands[-1] and c != UNKNOWN_CWD and "cd MAPPA &&" not in str(d):
                    raise Deny(f"{d}; ha egy korabbi cd nem sikerul, ez a(z) {c} mappaban futna (cd MAPPA && PARANCS "
                               "alakban csak a cd celjaban fut)") from None
                raise


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


def _check_pipeline(pipeline, cwd: str, depth: int, bodies, sub_text) -> None:
    """One pipeline under one cwd candidate: every rule on every member."""
    feeds_interpreter = False
    parsed = [_split_command(c) for c in pipeline]
    heads = [p[0] for p in parsed]
    for idx, (head, args, redirs, herestr, heredocs, assigns) in enumerate(parsed):
        is_interp = head in INTERPRETERS or re.fullmatch(r"python\d[\d.]*|pypy\d*|node\d*|perl\d[\d.]*|ruby\d[\d.]*|php\d[\d.]*", head or "")
        later_interp = any(h in INTERPRETERS or re.fullmatch(r"python\d[\d.]*", h or "") for h in heads[idx + 1:])
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
                _code_text_checks(body, "heredoc", cwd, depth)
        for h in herestr:
            if is_interp or later_interp:
                _code_text_checks(h, "here-string", cwd, depth)
            elif _is_live_totp_word(h, cwd):
                _deny("a masodik faktor taranak neve")
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
                _code_text_checks(a, f"{head} argumentum", cwd, depth)
            continue
        if head in DATA_ONLY_HEADS:
            if later_interp:
                for a in args:
                    _code_text_checks(a, f"{head} -> ertelmezo", cwd, depth)
                # what echo / printf actually prints is one line of code ("echo -n rm store/... | sh",
                # "printf '%s ' rm store/... | sh"; KISPROGI 6505, 6511)
                for line in _printed_text(head, args):
                    _code_text_checks(line, f"{head} -> ertelmezo", cwd, depth)
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
        if head == "git" and _git_grep_on_store(args, cwd):
            _deny("git grep --no-index/--untracked a store mappaban (a rejtett masodik faktor tarat is olvasna)")
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
        text, bodies = _strip_heredocs(sub)
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
    for t in toks:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", t, re.S) or t.startswith("-"):
            continue
        b = os.path.basename(t)
        if b == "xargs":
            return True
        if b not in WRAPPERS and not re.fullmatch(r"[0-9.]+[smhd]?", t):
            return False
    return False


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

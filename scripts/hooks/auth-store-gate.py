#!/usr/bin/env python3
"""PreToolUse gate: no agent writes the dashboard login's second-factor state (BELA 6410, the wave-end item).

The TOTP second factor (src/web/dashboard-totp.ts) keeps its secret in store/.dashboard-totp.json and its on/off
flag in the dashboard_users table of store/claudeclaw.db. Every agent runs as the same OS user, so without a gate
any agent's Bash could replace the secret, read it, or clear the flag, and the second factor would protect only
against a remote password-knower. This is the narrow gate BELA asked for instead of porting upstream's whole
db-write-gate (BELA 6410): three rules, for every agent, the main one included.

  R1  The second-factor store, by name or by a glob that matches it: any use outside a data position (Bash), any
      path tool on it (Read, Write, Edit, MultiEdit, NotebookEdit, Grep), and a script written with its name in it.
      Reading is refused too: the secret is the factor.
  R2  An SQL write naming dashboard_users (INSERT/REPLACE/UPDATE/DELETE/DROP/ALTER/CREATE TRIGGER ON), and the
      schema route around a table name (PRAGMA writable_schema, writes on sqlite_master/sqlite_schema), in text a
      program runs: an interpreter's arguments, its stdin (heredoc, here-string, a pipe from echo/printf/cat), a
      command substitution, sh -c / eval, and a script file written with it.
  R3  The dashboard DB file as a write target: rm, mv (either side), cp/install/rsync/ln/scp destination, dd of=,
      tee, truncate, shred, sed/perl -i, a redirection, a Write/Edit of it, and interpreter code that both names the
      file and calls a file-writing API. Opening it through sqlite3 to write other tables passes (the kanban,
      memories and messages are written that way).

Data positions pass, so a message or a note that QUOTES such a command is not a write: curl's -d/--data*/--json/
-F/--form values (but not an @file value, which reads the file), echo/printf arguments and a heredoc fed to cat or
tee, unless the same pipeline feeds an interpreter.

A text brake, not an OS boundary (the same user can always build the name or the SQL at run time, e.g. from
variables, base64 or string concatenation). It closes the direct and the accidental routes; the dashboard itself
writes these files as before. Known routes it does not close (KISPROGI 6483): a recursive grep from the install root or
above (grep -rn x . reads the store's hidden file; it cannot be told from everyday searches), a copy of the whole store
folder read afterwards (cp -r store /tmp/s, then /tmp/s/.d*), a script written in one call and run in a later one, and
sqlite3 .restore / Connection.backup() INTO the live DB (it replaces the whole DB; the direction cannot be told from a
legitimate backup in text). Matched against a fixed vocabulary only: anything else passes. Unparsable Bash that mentions one of the
protected names is refused (fail closed); unparsable Bash without them passes.

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
# passed the first version. Now: any schema prefix, ' as well as " ` [ ] (an optional backslash before it, for raw code
# text), no SET required after UPDATE <table>, a word boundary after the name (dashboard_users_archive passes), and the
# text is matched with its SQL comments cut out as well as as it stands.
_Q = r"\\?[\"`\['\]]?"
_TABLE = r"(?:" + _Q + r"\w+" + _Q + r"\s*\.\s*)?" + _Q + r"dashboard_users" + _Q + r"(?!\w)"
_SCHEMA_TABLE = r"(?:" + _Q + r"\w+" + _Q + r"\s*\.\s*)?" + _Q + r"sqlite_(?:temp_)?(?:master|schema)\b"
SQL_WRITE_RXS = [
    re.compile(r"\binsert\s+(?:or\s+\w+\s+)?into\s+" + _TABLE, re.I),
    re.compile(r"\breplace\s+into\s+" + _TABLE, re.I),
    re.compile(r"\bupdate\s+(?:or\s+\w+\s+)?" + _TABLE, re.I),
    re.compile(r"\bdelete\s+from\s+" + _TABLE, re.I),
    re.compile(r"\bdrop\s+table\s+(?:if\s+exists\s+)?" + _TABLE, re.I),
    re.compile(r"\balter\s+table\s+" + _TABLE, re.I),
    re.compile(r"\bcreate\s+(?:temp\w*\s+)?trigger\b[\s\S]{0,400}?\bon\s+" + _TABLE, re.I),
    re.compile(r"\bwritable_schema\b", re.I),
    re.compile(r"\b(?:insert\s+(?:or\s+\w+\s+)?into|update(?:\s+or\s+\w+)?|delete\s+from|replace\s+into)\s+" + _SCHEMA_TABLE, re.I),
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
SEARCH_HEADS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "zgrep"}
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


def _resolve(path: str, cwd: str) -> str:
    """The real path; a relative path under an unknown cwd (a cd to a variable) resolves to a place that is never the
    store, so only a spelled-out name can still match there (the callers check the name for unknown places)."""
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
    return bool(re.search(r"[$`]|__SUBST\d+__", path))


def _is_live_db_path(path: str, cwd: str) -> bool:
    """The live DB file (or a sidecar): its folder resolves to the live store and its name (or glob) matches; or the
    place is not known (a variable, a substitution) and the name is spelled out."""
    base = os.path.basename(path.rstrip("/"))
    if _unknown_place(path):
        return bool(DB_RX.search(base))
    if not _db_name_matches(base):
        return False
    where = _resolve(os.path.dirname(path) or ".", cwd)
    if where == UNKNOWN_CWD:
        return bool(DB_RX.search(base))
    return where == os.path.realpath(STORE)


def _is_live_totp_word(word: str, cwd: str) -> bool:
    """A Bash word naming the live second-factor store: its folder resolves to the live store and the name (or a glob)
    matches it; or the place is not known and the name is spelled out. A worktree's or a test copy's own store is not
    the live one (resolved through links, so a link to the live store still counts)."""
    w = word[1:] if word.startswith("@") else word
    base = os.path.basename(w.rstrip("/"))
    if _unknown_place(w):
        return bool(TOTP_RX.search(w))
    if not (TOTP_RX.search(base) or _glob_matches_totp(base)):
        return False
    where = _resolve(os.path.dirname(w) or ".", cwd)
    if where == UNKNOWN_CWD:
        return bool(TOTP_RX.search(base))
    return where == os.path.realpath(STORE)


def _is_store_or_ancestor(path: str, cwd: str) -> bool:
    """The live store folder itself or a folder that holds it (removing or moving it removes the live DB)."""
    if re.search(r"[$`{*?\[]", path):
        return False
    real = _resolve(path, cwd).rstrip(os.sep) or os.sep
    store = os.path.realpath(STORE)
    return real == store or store.startswith(real + os.sep) or real == os.sep


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
    return bool(TOTP_RX.search(word)) or _glob_matches_totp(word)


def _sql_write(text: str) -> bool:
    cut = _SQL_COMMENT.sub(" ", text)
    return any(rx.search(cut) or rx.search(text) for rx in SQL_WRITE_RXS)


def _code_text_checks(text: str, where: str, cwd: str, depth: int) -> None:
    """Text a program runs: all three rules, and shell text is parsed again (a nested command)."""
    if _sql_write(text):
        _deny(f"SQL-iras a dashboard_users tablara ({where})")
    if TOTP_RX.search(text):
        _deny(f"a masodik faktor tara egy futtatott szovegben ({where})")
    if _code_writes_db(text):
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

_HEREDOC_RX = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z0-9_.\-]+))")


def _strip_heredocs(cmd: str):
    """Heredoc bodies out of the text: (text with a marker word per heredoc, {marker: body})."""
    lines = cmd.split("\n")
    out, bodies, i, n = [], {}, 0, 0
    while i < len(lines):
        line = lines[i]
        pending = []

        def repl(m):
            nonlocal n
            if _in_quotes(line, m.start()):
                return m.group(0)
            delim = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
            marker = f"__HEREDOC{n}__"
            n += 1
            pending.append((marker, delim, m.group(1) == "-"))
            return "<< " + marker

        new = _HEREDOC_RX.sub(repl, line) if "<<" in line else line
        out.append(new)
        i += 1
        for marker, delim, strip_tabs in pending:
            body = []
            while i < len(lines):
                cur = lines[i].lstrip("\t") if strip_tabs else lines[i]
                i += 1
                if cur == delim:
                    break
                body.append(lines[i - 1])
            bodies[marker] = "\n".join(body)
    return "\n".join(out), bodies


def _in_quotes(line: str, pos: int) -> bool:
    sq = dq = False
    k = 0
    while k < pos:
        c = line[k]
        if c == "\\" and not sq:
            k += 2
            continue
        if c == "'" and not dq:
            sq = not sq
        elif c == '"' and not sq:
            dq = not dq
        k += 1
    return sq or dq


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


def _tokens(text: str):
    lx = shlex.shlex(text, posix=True, punctuation_chars=";&|<>()\n")
    lx.whitespace = " \t\r"
    lx.whitespace_split = True
    lx.commenters = ""
    return list(lx)


def _pipelines(toks):
    """[[simple command tokens, ...] per pipeline]."""
    pipes, cur, cmd = [], [], []
    for t in toks:
        if t in SEPARATORS:
            if cmd:
                cur.append(cmd)
            if cur:
                pipes.append(cur)
            cur, cmd = [], []
        elif t in PIPES:
            if cmd:
                cur.append(cmd)
            cmd = []
        else:
            cmd.append(t)
    if cmd:
        cur.append(cmd)
    if cur:
        pipes.append(cur)
    return pipes


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
                if re.fullmatch(r"-[nkus]|--signal|--kill-after|-o|-e|-i", words[i]) and i + 1 < len(words):
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


def _search_pattern_positions(head: str, args):
    """Indexes of a search command's PATTERN arguments (a search string is data, not a file): the values of
    -e/--regexp (and git's -S/-G), else the first positional. The file operands stay checked."""
    pats, k, first_pos, explicit = set(), 0, None, False
    positional_pattern = head != "git"
    if head == "git":
        if not args or args[0] not in ("grep", "log"):
            return set()
        positional_pattern = args[0] == "grep"
        offset, args = 1, args[1:]
    else:
        offset = 0
    while k < len(args):
        a = args[k]
        if a in ("-e", "--regexp", "-S", "-G") and k + 1 < len(args):
            pats.add(k + 1 + offset)
            explicit = True
            k += 2
            continue
        if re.fullmatch(r"-[eSG].+|--regexp=.+", a):
            pats.add(k + offset)
            explicit = True
        elif a in ("-f", "--file"):
            explicit = True
            k += 2
            continue
        elif not a.startswith("-") and first_pos is None:
            first_pos = k
        k += 1
    if not explicit and first_pos is not None and positional_pattern:
        pats.add(first_pos + offset)
    return pats


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


def _find_on_store(args, cwd: str) -> bool:
    starts = []
    for a in args:
        if a.startswith("-") or a in ("(", "!", "\\("):
            break
        starts.append(a)
    if not starts:
        starts = ["."]
    destructive = any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls") for a in args)
    if not destructive:
        return False
    store = os.path.realpath(STORE)
    for s in starts:
        r = _resolve(s, cwd)
        if r == store or r.startswith(store + os.sep) or store.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def _unparsable_hits(cmd: str) -> bool:
    """Fail-closed test for Bash that cannot be parsed: a protected thing in a writing or reading shape only (a plain
    read of the DB through sqlite3 stays allowed)."""
    return bool(_sql_write(cmd) or TOTP_RX.search(cmd) or _code_writes_db(cmd)
                or re.search(r"\b(?:rm|mv|cp|install|rsync|ln|truncate|shred|dd|tee)\b[^\n;|&]*" + _DBN, cmd, re.I)
                or re.search(r">\s*['\"]?[^\s;|&]*" + _DBN, cmd, re.I))


def check_bash(cmd: str, cwd: str, depth: int = 0, best_effort: bool = False) -> None:
    """best_effort: a nested try on text that may not be shell at all (an interpreter's code); a parse failure there
    is not a refusal, the text rules already ran on it."""
    text, bodies = _strip_heredocs(cmd)
    text, subs = _strip_substitutions(text)
    for s in subs:
        # a substitution is shell that runs: parsed as such (its data positions stay data)
        if depth < 4:
            check_bash(s, cwd, depth + 1, best_effort)
        elif _unparsable_hits(s):
            _deny("tul mely parancs-behelyettesites a vedett nevekkel (fail-closed)")
    try:
        toks = _tokens(text)
    except ValueError:
        if not best_effort and _unparsable_hits(cmd):
            _deny("nem elemezheto parancs a vedett nevekkel (fail-closed)")
        return
    for pipeline in _pipelines(toks):
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
            if head in ("cd", "pushd") and len(pipeline) == 1:
                target = next((a for a in args if not a.startswith("-")), os.path.expanduser("~"))
                cwd = UNKNOWN_CWD if (_unknown_place(target) or target == "-" or cwd == UNKNOWN_CWD and not os.path.isabs(target)) \
                    else _resolve(target, cwd)
                continue
            if is_interp:
                for a in args:
                    if _totp_word(a):
                        _deny("a masodik faktor tara egy ertelmezo argumentumaban")
                    _code_text_checks(a, f"{head} argumentum", cwd, depth)
                continue
            if head in DATA_ONLY_HEADS:
                if later_interp:
                    for a in args:
                        _code_text_checks(a, f"{head} -> ertelmezo", cwd, depth)
                continue
            data_pos = _curl_data_positions(args) if head in ("curl", "wget", "http", "https", "xh") else set()
            if head in SEARCH_HEADS or head == "git":
                data_pos |= _search_pattern_positions(head, args)
            for k, a in enumerate(args):
                if k in data_pos or head in METADATA_HEADS:
                    continue
                for piece in [a] + ([a.split("=", 1)[1]] if "=" in a else []):
                    if _is_live_totp_word(piece, cwd):
                        _deny(f"a masodik faktor tara a(z) {head} parancsban")
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
            if head == "find" and _find_on_store(args, cwd):
                _deny("find -delete/-exec a store mappan (benne a masodik faktor taraval)")
            if head in ("git", "sqlite3"):
                pass


# --------------------------------------------------------------------------------------------------------------
# Path tools
# --------------------------------------------------------------------------------------------------------------

def _grep_glob_may_match_totp(g: str) -> bool:
    """Whether an rg glob (its last segment) can match the store's file name; braces and anything unparsable count."""
    last = g.strip().split("/")[-1]
    if not last or "{" in last or last.startswith("!"):
        return True
    try:
        return fnmatch.fnmatchcase(TOTP_NAME, last)
    except re.error:
        return True


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

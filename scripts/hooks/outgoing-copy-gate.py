#!/usr/bin/env python3
"""PreToolUse gate on the MAIN agent's outbound email: Hungarian copy QA.

Why this exists (Szabi, 2026-08-10 12:57): a licence-delivery email went out to a
client with every accent stripped ("Szia Balint, itt van a Marveen licenckulcsod
es a telepito"). It was the second accent incident that day -- the first was a
client-facing spreadsheet the same morning. Szabi asked for a gate that inspects
outgoing copy BEFORE the send and rejects it if something is wrong.

Note the seam this fills. `scripts/email-send-gate.mjs` already gates outbound
email, but it gates SUB-AGENTS (it is wired by writeAgentSettingsFromProfile()
guarded by `name !== MAIN_AGENT_ID`) and it is a hard deny, not a content check.
Nothing at all ran on the main agent's own sends -- and the main agent is the one
that actually writes to customers.

What it checks, all three from standing owner rules in CLAUDE.md:
  1. Hungarian text that is missing its accents (the incident above). Known limit (KAPUES1018): a text with fewer
     than 3 distinct HU markers can pass with an accentless 'es'; see the KNOWN LIMIT comment above
     AMBIGUOUS_TRIGGER.
  2. Em dash (U+2014) -- forbidden in every deliverable.
  3. Owner-specific NAME rules (misspelled surnames etc.) -- loaded from an
     untracked local rules file (GATEPERSIST816), never hardcoded here.

FAIL-CLOSED ON AN UNREADABLE BODY. If the call looks like a send but the body
cannot be recovered (e.g. `send.py ... < $SP/body.txt`, where $SP is a shell
variable this hook cannot resolve), the gate BLOCKS. A send whose content cannot
be inspected defeats the point of the gate, so "I could not read it" must not
mean "let it through". The block message says how to make it inspectable.

Contract: PreToolUse. Reads the hook payload on stdin, exit 0 = allow,
exit 2 = block (stderr goes back to the model).
"""
import html
import json
import os
import re
import sys

# --- what counts as an email send -------------------------------------------
# KAPUHATOKOR822 (2026-08-22, NEGY hamis pozitiv egy delutanon, HAROM
# muvelet-tipuson: inter-agent uzenet, sqlite-iras, fajl-OLVASAS): a korabbi
# szures a TELJES parancs-stringben kereste a kuldes-mintakat, igy egy
# inter-agent curl JSON-torzse ('"to":' a boritekban + 'send.py' a szoveg
# TARTALMABAN), egy hirlevel-szoveget iro sqlite-parancs vagy a send-script
# puszta elolvasasa is kuldesnek latszott. A kapu levelnek olvasta azt, ami
# uzenet A RENDSZERROL -- es pont arrol a temarol nemitotta volna el a
# flottat, amirol a legfontosabb beszelni (Iris tetje: egy valodi incidenst
# nem lehetne jelenteni rola).
#
# A szures ezert PARANCS-POZICIORA megy, nem tartalomra: elobb kivagjuk a
# heredoc-torzseket es az idezett stringeket (a tartalom igy nem tud parancs-
# nak latszani), majd pipeline/szekvencia-szegmensenkent a MEGHIVOTT programot
# nezzuk. Kuldes az, ahol a kuldo program fut:
#   - sendmail / msmtp / swaks a program-pozicioban (ezek csak kuldeni tudnak);
#   - send.py TENYLEGES futtatasa (python vagy kozvetlen ut) --to cimzettel a
#     SAJAT szegmenseben (a --help/olvasas igy nem trigger);
#   - graph-mail futtatasa `send` alparanccsal;
#   - curl/wget, amelynek IDEZETLEN URL-tokenje az api.resend.com-ra mutat
#     (a -d payloadban idezett elofordulas nem szamit -- az tartalom).
# MASODIK KOR (Marveen adverzarialis merese, msg 14282): az elso valtozat a
# quoted stringeket VAKON vagta ki, ezert ket hamis negativot nyitott -- az
# IDEZOJELES URL a curl sajat argumentum-helyen (a curl SZOKASOS irasmodja!)
# es a burkolo hejj `-c` string-argumentuma atment. A gyoker: az idezojel a
# TARTALOM ellen jo hatar, de nem mondja meg, hogy a token URL- vagy
# PROGRAM-POZICIOBAN all-e. Ezert a kivagas helyett QUOTE-TUDATOS tokenizalas
# fut (shlex): az idezett token EGY tokenkent, poziciojaval egyutt erkezik --
# a curl idezojeles URL-argumentuma igy vizsgalhato, mikozben egy -d payload
# belsejeben emlitett domain tovabbra is csak tartalom (az URL-minta a token
# ELEJERE horgonyzott). A wrapper hejj (`sh -c "..."`) string-argumentuma
# rekurzivan elemzodik.
# A heredoc-kivagas SORREND-FUGGETLEN (Marveen 3. kore, msg 14286): a
# hatarolo utani SOR-MARADEK (pl. atiranyitas: <<EOF > fajl) a parancs
# resze es MEGMARAD -- csak a torzs esik ki. Enelkul (a) forditott
# sorrendnel a torzs parancsnak latszott (FP), (b) a bevezeto sor
# eldobasa a heredoc-taplalt VALODI kuldot vesztette volna el (FN).
_HEREDOC = re.compile(r"(<<-?\s*'?(\w+)'?[^\n]*)\n.*?\n\2(?=\s|$)", re.S)
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
# KWSPLIT924: shell reserved words that put the NEXT word in command position.
# The segmenter splits on operators only, so `if true; then sendmail x; fi`
# gave the segment [then, sendmail, x] whose "program" was `then`: the send was
# not recognised and the copy audit was silently skipped (same for do / else /
# elif / { / !, and the condition after if / while / until). They are stripped
# ONLY at the head of a segment, i.e. in command position, never as separators:
# `echo then sendmail x` stays a single echo. `in` is deliberately NOT here: the
# words after it are data, and `for m in sendmail msmtp; do which $m; done` must
# stay false. `for` / `case` are not here either: the word after them is a name
# or a subject, not a command. Mirrored in email-send-gate.mjs (CMD_POSITION_KEYWORDS);
# the shared send-invocation-cases.json binds the two.
_CMD_POSITION_KEYWORDS = frozenset(("if", "then", "else", "elif", "do", "while", "until", "{", "!"))
_SENDER_PROG = re.compile(r"^(sendmail|msmtp|swaks)$", re.I)
_SENDPY = re.compile(r"^send\.py$", re.I)
_PYTHON = re.compile(r"^python3?$", re.I)
# A ket kapu (ez + scripts/email-send-gate.mjs) SZANDEKOSAN azonos
# felismeres-szemantikat visel, es ezt kozos eset-lista orzi
# (send-invocation-cases.json + konformancia-teszt): a divergencia
# teszt-hibakent jelenjen meg, ne incidenskent (Marveen, msg 14289).
_NODEISH = re.compile(r"^(node|tsx|ts-node|deno|bun|npx)$", re.I)
_GRAPHMAIL = re.compile(r"^graph-mail(\.ts|\.js)?$", re.I)
# WRAPSHELL1126: the ksh family (ksh = ksh93, mksh, lksh), busybox's ash, and the restricted variants all take
# `-c STRING` with the sh option rules (measured on the real shells). 'busybox sh' already steps to 'sh' through the
# launcher table. fish has its own argument rules: _FISH_SHELL / _fish_command_strings.
_WRAPPER_SHELL = re.compile(r"^(sh|bash|zsh|dash|ksh|ksh93|mksh|lksh|ash|rbash|rzsh|rksh|rksh93|rmksh|rlksh)$", re.I)
_FISH_SHELL = re.compile(r"^fish$", re.I)
_CURLISH = re.compile(r"^(curl|wget|http)$", re.I)
# Interpreter kod-string argumentum (python -c / node -e): az interpreternek
# atadott kod MUVELET, nem tartalom -- a kod-szintu kuldes-hivasokra szurunk.
#
# KIMONDOTT HATAR (Marveen, msg 14298): tetszoleges interpreter-kod statikus
# elemzese eldonthetetlen -- ez a kapu a VELETLEN kuldest fogja meg, nem egy
# elszant kikerulot. A lenti exec-heurisztika a NAIV alakokat fedi (a kod
# process-inditast ES kuldo-programnevet egyutt tartalmaz); ennel tobbet nem
# allit, es nem is allithat.
_CODE_SEND = re.compile(
    r"\bsmtplib\b|SMTP\s*\(|\bsendMail\s*\(|\bsendEmail\b|\bmail\.send\b", re.I
)
_CODE_EXECISH = re.compile(
    r"\bsubprocess\b|os\.system|\bpopen\b|child_process|\bexec[A-Za-z]*\s*\(|\bspawn[A-Za-z]*\s*\(",
    re.I,
)
_CODE_SENDER_LIT = re.compile(r"sendmail|msmtp|swaks|send\.py", re.I)
# EXECVEC1126 (PROGI msg 5993, measured): perl -e/-E, ruby -e and php -r/-B/-R/-E run code that can start the
# sender ('system("sendmail a")', qx, backticks, %x, open "|...", shell_exec, passthru) or send by library
# (Net::SMTP, MIME::Lite, php mail(), ruby net/smtp). Same naive-shape rule as _code_string_sends, with the process
# starters and mail libraries of these languages, applied to EVERY argument (no per-language option parsing).
# No left word boundary on the starters: an attached code argument ('-esystem(...)') glues the option letter on.
_SCRIPT_INTERP = re.compile(r"^(perl|perl5[\d.]*|ruby|ruby[\d.]+|jruby|php|php[\d.]+)$", re.I)
_SCRIPT_EXECISH = re.compile(r"system\b|qx\b|`|%x|exec\b|open\b|shell_exec|passthru|proc_open|popen|Open3|spawn\b", re.I)
_SCRIPT_MAIL_LIB = re.compile(
    r"Net::SMTP|Mail::Sendmail|MIME::Lite|Email::Sender|Mail::Mailer|net/smtp|\bmail\s*\(|PHPMailer|Swift_"
    r"|\bMail\.(new|deliver)\b|Pony\.mail|ActionMailer", re.I)
# EXECVEC1126: vi-family editors run ex commands from '-c CMD', '--cmd CMD' and '+CMD' ('!sendmail a',
# 'call system(...)', measured with vim). A command that names a sender is a send.
_EDITOR = re.compile(r"^(vim|vi|nvim|ex|view|vimdiff|gvim|rvim|evim|nvi)$", re.I)


def _code_string_sends(code: str) -> bool:
    if _CODE_SEND.search(code):
        return True
    return bool(_CODE_EXECISH.search(code) and _CODE_SENDER_LIT.search(code))


def _script_code_sends(code: str) -> bool:
    return (_code_string_sends(code) or bool(_SCRIPT_MAIL_LIB.search(code))
            or bool(_SCRIPT_EXECISH.search(code) and _CODE_SENDER_LIT.search(code)))
# Token-ELEJERE horgonyzott cel-minta: egy URL-argumentum vagy csupasz
# domain/utvonal illik ra; egy JSON-payload ('{...api.resend.com...}') nem.
_RESEND_TARGET = re.compile(r"^(https?://)?([^/@\s]*\.)?api\.resend\.com(/|$|\s|$)", re.I)

# RESENDGATE826: a resend-celu curl/wget csak akkor KULDES, ha a METODUS az.
# A korabbi minta metodus-vak volt, es egy read-only GET /domains (nincs torzs,
# nincs cimzett) ugyanugy fail-closed elutasitast kapott -- pont egy domain-
# verifikacios MERES akadt el rajta. A szukites iranya szigoru: a metodust
# FELISMERNI kell (explicit -X/--request/--method, vagy implicit POST a
# torzs-flagekbol); ha nem allapithato meg (valtozo, config-fajl, csonka flag),
# marad a fail-closed. Egy "nincs felismerheto torzs -> atmegy" szabaly a
# kaput utne ki, ezert ILYEN AG NINCS.
_CURL_BODY_OPTS = {
    "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
    "--data-ascii", "-F", "--form", "--form-string", "--json",
    "-T", "--upload-file",
    # wget torzs-flagek
    "--post-data", "--post-file", "--body-data", "--body-file",
}
_SAFE_METHODS = {"GET", "HEAD"}


def _curl_resend_verdict(rest):
    """'read' | 'send' | 'unknown' -- unknown a hivo oldalon fail-closed."""
    method = None
    has_body = False
    get_forced = False
    i, n = 0, len(rest)
    while i < n:
        t = rest[i]
        if t in ("-X", "--request", "--method"):
            if i + 1 >= n or not rest[i + 1].isalpha():
                return "unknown"  # csonka vagy valtozo ($METHOD) -- nem dontheto
            method = rest[i + 1].upper()
            i += 2
            continue
        if t.startswith("--request=") or t.startswith("--method="):
            m = t.split("=", 1)[1]
            if not m.isalpha():
                return "unknown"
            method = m.upper()
            i += 1
            continue
        if t in ("-G", "--get"):
            get_forced = True
            i += 1
            continue
        if t in ("-K", "--config"):
            return "unknown"  # a config-fajl rejtett metodust/torzset hordozhat
        if t in _CURL_BODY_OPTS or any(
            t.startswith(o + "=") for o in _CURL_BODY_OPTS if o.startswith("--")
        ):
            has_body = True
            i += 1
            continue
        if t.startswith("-") and not t.startswith("--") and len(t) > 1:
            # egy-kotojeles cluster (-sS, -sX POST, -sd '{}'): a betuk kotegelve
            letters = t[1:]
            if "X" in letters:
                after = letters.split("X", 1)[1]
                if after:
                    if not after.isalpha():
                        return "unknown"
                    method = after.upper()
                else:
                    if i + 1 >= n or not rest[i + 1].isalpha():
                        return "unknown"
                    method = rest[i + 1].upper()
                    i += 1
            elif "d" in letters or "F" in letters or "T" in letters:
                has_body = True
            elif "G" in letters:
                get_forced = True
            elif "K" in letters:
                return "unknown"
            i += 1
            continue
        i += 1
    if method is not None and method not in _SAFE_METHODS:
        return "send"
    if has_body and not get_forced:
        # implicit POST (curl -d/-F/--json/-T alapertelmezese), vagy egy
        # gyanus "GET torzzsel" alak -- mindketto kuldeskent kezelve
        return "send"
    return "read"
# A tovabbi kuldes-jellegu literalok, amikre a parse-hiba eseten (es CSAK
# akkor) konzervativan visszaesunk -- lasd is_send_invocation vegen.
_FALLBACK_LITERALS = re.compile(
    r"send\.py|api\.resend\.com|\bsendmail\b|\bmsmtp\b|\bswaks\b"
    r"|\bsmtplib\b|\bsendMail\s*\(", re.I
)


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


# REDIRHEAD1126: placeholders the mask puts in place of the '&' / '|' that belong to a redirection, and the shape of
# a redirection token at command position: optional fd (or {var}, or the '&' of '&>'), the operator, and the target
# either attached or as the NEXT token. bash applies leading redirections and runs the rest as the command:
# '2>&1 sendmail x', '</dev/null sendmail x', '>/dev/null sendmail x', '&>/dev/null sendmail x' all send.
_REDIR_AMP = "\x01"
_REDIR_BAR = "\x02"
_REDIR_RX = re.compile(r"^(?:\d+|\{[A-Za-z_]\w*\}|\x01)?(?:<<<|<<-?|<>|>>|>\x02|>\x01|<\x01|>|<)(.*)$", re.S)


def _mask_subshell_markers(cmd: str) -> str:
    """Idezojelen KIVULI ujsor/`$(`/backtick -> `;` szeparator, hogy a shlex
    szegmens-hatarkent lassa; idezojelen BELUL a szoveg erintetlen (tartalom).

    MAINGATENLCOMMENT1104: a shell comment is dropped HERE, as bash does: an unquoted `#` at the start of a word, up
    to the newline (the newline itself still becomes `;`). shlex's own comment handling is off (_segments_tokens):
    after this mask there is no newline left, so a shlex comment ran to the end of the command and swallowed the next
    line's send; an apostrophe inside a comment flipped this mask into quote mode; and shlex starts a comment at a `#`
    inside a word (`echo a#b; sendmail x` lost the send). The word-start set is whitespace and ;&|( plus a ) that
    does not close a $( ; a character escaped with a backslash never starts a word (`echo a\\ #b; sendmail x` runs the
    send in bash). The JS twin (email-send-gate.mjs) does not strip comments at all, which is the conservative side:
    it reads a commented-out send as a send."""
    out = []
    q = None  # None | "'" | '"'
    i, n = 0, len(cmd)
    parens = []  # Bence 69701: "s" for an open $( , "p" for a plain ( -- which ) closes a command substitution
    subst_close_at = -1
    # Index of the last character escaped by an unquoted backslash. -2, never a real index.
    escaped_at = -2
    # Index (in cmd) of the last character bash actually KEEPS on the line, -1 before the first one. The comment test
    # looks at this, not at cmd[i-1]: a line continuation (backslash-newline) is removed before the line is split
    # into words, so in 'echob\<NL>#x' the # sits in the middle of the word 'echob#x', not at a word start.
    prev = -1
    while i < n:
        ch = cmd[i]
        if q:
            if ch == "\\" and q == '"' and i + 1 < n:
                out.append(cmd[i:i + 2]); prev = i + 1; i += 2; continue
            if ch == q:
                q = None
            out.append(ch); prev = i; i += 1; continue
        if ch in "'\"":
            q = ch; out.append(ch); prev = i; i += 1; continue
        # bash line continuation: an unquoted backslash-newline pair is removed BEFORE the line is split into words,
        # so it neither escapes the next character nor separates anything (`cd /tmp &&\<NL>sendmail x` is one
        # command list). Kept as an escape it gave shlex a "\n" token at command position and hid the send.
        # `prev` is deliberately left alone: the pair is not part of the line.
        if ch == "\\" and i + 1 < n and cmd[i + 1] == "\n":
            i += 2; continue
        if ch == "\\" and i + 1 < n:
            escaped_at = i + 1
            out.append(cmd[i:i + 2]); prev = i + 1; i += 2; continue
        # A # starts a comment only at a word start: nothing kept before it, or the kept character before it is a
        # separator, or a ) that closes no $( (Bence 69701: `$(echo a)#x` is one word). An ESCAPED separator
        # (`a\ #b`, `a\;#b`) is part of the word, so the # after it is no comment.
        if ch == "#" and (prev == -1 or (escaped_at != prev and (
                cmd[prev] in " \t\n;&|(" or (cmd[prev] == ")" and subst_close_at != prev)))):
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        if ch == "\n" or ch == "`":
            out.append(";"); prev = i; i += 1; continue
        if ch == "$" and i + 1 < n and cmd[i + 1] == "(":
            parens.append("s")
            out.append(";"); prev = i + 1; i += 2; continue
        # REDIRHEAD1126: the '&' of '>&' / '<&' / '&>' and the '|' of '>|' belong to a REDIRECTION, not to a list
        # operator or a pipe. Left as is, shlex split '2>&1 sendmail x' into ['2>'] and ['1', 'sendmail', 'x'], so
        # the '1' became the program. They stay inside the redirection token as _REDIR_AMP / _REDIR_BAR.
        if ch in "<>" and i + 1 < n and cmd[i + 1] == "&":
            out.append(ch + _REDIR_AMP); prev = i + 1; i += 2; continue
        if ch == "&" and i + 1 < n and cmd[i + 1] == ">":
            out.append(_REDIR_AMP); prev = i; i += 1; continue
        if ch == ">" and i + 1 < n and cmd[i + 1] == "|":
            out.append(">" + _REDIR_BAR); prev = i + 1; i += 2; continue
        if ch == "(":
            parens.append("p")
        elif ch == ")" and parens and parens.pop() == "s":
            subst_close_at = i
        out.append(ch); prev = i; i += 1
    return "".join(out)


def _segments_tokens(cmd: str):
    """[[token, ...], ...] szegmensenkent -- quote-tudatosan, poziciot orizve."""
    import shlex
    lex = shlex.shlex(_mask_subshell_markers(_HEREDOC.sub(r"\1", cmd)),
                      posix=True, punctuation_chars="();|&")
    lex.whitespace_split = True
    lex.commenters = ""  # MAINGATENLCOMMENT1104: the mask drops comments as bash does; shlex's would not
    segments, cur = [], []
    for tok in lex:
        # SEGSPLIT923: shlex(punctuation_chars) returns a RUN of operator
        # characters as ONE token, so `$(date); sendmail ...` (after the
        # subshell mask: `;date); sendmail`) yielded the token ");" -- not in
        # the list, so it did not split, `sendmail` landed mid-segment, and the
        # send was NOT recognised: the copy audit was silently skipped. The JS
        # twin (email-send-gate.mjs) said true on the same input; nothing in
        # the conformance list covered it. A token made ONLY of operator
        # characters is always an operator sequence, so it separates.
        if tok in ("|", "||", "&", "&&", ";", "(", ")", ";;", "|&") or (tok and set(tok) <= set("();|&")):
            if cur:
                segments.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


# SENDWRAP924: a wrapper in front of the sender -- `sudo sendmail x`,
# `time -p sendmail x`, `env -i sendmail x`, `timeout 10 sendmail x` -- made
# the wrapper the "program", so the send was not recognised and the copy audit
# was silently skipped (13 measured shapes, both copies). A wrapper is stepped
# over WITH its own flags: a flag that takes a value consumes it, so in
# `sudo -u sendmail true` the program is `true`, not `sendmail`.
# name -> (short flags taking a value, long flags taking a value, positional
# arguments before the command). An UNKNOWN long flag without `=` may or may
# not take a value, so both readings are tried and either one being a send
# counts: an unknown flag errs toward auditing, never toward skipping.
# `command -v/-V` looks a name up and runs nothing. `function NAME` and
# `coproc [NAME]` put their body in command position. Mirrored in
# email-send-gate.mjs (WRAPPERS / commandHeads); send-invocation-cases.json
# binds the two.
_WRAPPERS = {
    "time": ("fo", ("format", "output"), 0),
    "sudo": ("ughpCUrtDRT", ("user", "group", "host", "prompt", "close-from", "other-user",
                             "role", "type", "chdir", "chroot", "command-timeout"), 0),
    "env": ("uCS", ("unset", "chdir", "split-string"), 0),
    "nohup": ("", (), 0),
    "nice": ("n", ("adjustment",), 0),
    "exec": ("a", (), 0),
    "command": ("", (), 0),
    "xargs": ("ILnPsdEa", ("arg-file", "delimiter", "eof", "replace", "max-lines", "max-args",
                           "max-procs", "max-chars", "process-slot-var"), 0),
    "timeout": ("sk", ("signal", "kill-after"), 1),
    # setsid: for future use and symmetry. NOT measured local traffic: the hits the
    # first count found all sat inside quoted remote (ssh) command strings or heredoc
    # bodies, and that pattern was not quote-aware (Marveen's correction, 2026-09-24).
    "setsid": ("", (), 0),
    # WRAPGATE1126: both run their argument as a command ('builtin' only shell builtins, so in practice
    # 'builtin eval ...'; 'stdbuf -oL sendmail x' runs sendmail) and both passed this gate and the JS twin.
    "builtin": ("", (), 0),
    "stdbuf": ("ioe", ("input", "output", "error"), 0),
    # LAUNCHGATE1126 (PROGI review 5977 point B): launchers that run their argument as a command and passed both
    # gates. Measured with bash where the tool exists here (flock, ionice, chrt, taskset, nsenter, systemd-run are
    # present; doas, strace, ltrace, unbuffer, sg, parallel, busybox are not, those are pinned by the shared rows
    # only). The flag lists are the value-taking ones; an option this table does not know is caught by the
    # sender-token layer (_sender_token_layer) rather than mis-read as the program.
    "flock": ("wE", ("timeout", "conflict-exit-code"), 1),          # flock [opts] FILE CMD (-c STRING: see below)
    "ionice": ("cnpPu", ("class", "classdata", "pid", "pgid", "uid"), 0),
    "chrt": ("TPD", ("sched-runtime", "sched-period", "sched-deadline"), 1),   # chrt [opts] PRIO CMD
    "taskset": ("", (), 1),                                         # taskset [-c] MASK CMD
    "doas": ("uC", (), 0),
    "runuser": ("ugG", ("user", "group", "supp-group", "shell", "whitelist-environment"), 0),
    "unbuffer": ("", (), 0),
    "nsenter": ("tSGwr", ("target", "setuid", "setgid", "wd", "root"), 0),
    "systemd-run": ("pEMuGH", ("property", "setenv", "machine", "unit", "uid", "gid", "host", "description",
                               "slice", "working-directory", "on-calendar", "on-active", "timer-property"), 0),
    "busybox": ("", (), 0),                                         # busybox APPLET ARGS
    "parallel": ("jS", ("jobs", "sshlogin"), 0),
    "strace": ("oeapsuIEPSXbO", ("output", "trace", "signal", "attach", "string-limit", "user", "env"), 0),
    "ltrace": ("oeapsunADFLS", ("output", "library", "align", "indent", "config", "user"), 0),
    "sg": ("", (), 1),                                              # sg GROUP CMD (-c STRING: see below)
}

# LAUNCHGATE1126: launchers whose option carries a whole SHELL COMMAND STRING (like `bash -c`). The string is
# re-evaluated, bounded by the same depth as `bash -c` and fail-closed at the bound.
_STRING_LAUNCHERS = {
    "su": ("c", "command"), "runuser": ("c", "command"), "script": ("c", "command"),
    "flock": ("c", "command"), "sg": ("c",),
}
# find's actions whose arguments, up to ';' or '+', are a command.
_FIND_EXEC = ("-exec", "-execdir", "-ok", "-okdir")
# LAUNCHGATE1126, the sender-token layer: programs that NEVER run an argument as a command. A segment that names a
# sender anywhere (a sendmail/msmtp/swaks token, send.py with --to, graph-mail with send) but whose real command
# is not one of these is refused, so a launcher missing from the tables above still cannot carry a send. Calibrated
# on 8045 real Bash commands from the fleet's transcripts: only two segments there name a sender as a token at
# all (a printf and a chmod of a test fixture), both covered here; 0 new refusals on real work.
_NON_EXEC_PROGS = frozenset({
    "echo", "printf", "cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "ag", "wc", "sort",
    "uniq", "diff", "cmp", "comm", "cut", "tr", "column", "jq", "yq", "nl", "tac", "rev", "fold", "fmt",
    "man", "info", "which", "whereis", "type", "apropos", "whatis", "ls", "stat", "file", "readlink", "realpath",
    "dirname", "basename", "du", "df", "tree", "md5sum", "sha1sum", "sha256sum", "xxd", "od", "hexdump", "strings",
    "chmod", "chown", "chgrp", "touch", "mkdir", "rmdir", "rm", "cp", "mv", "ln", "test", "[", "[[", "true",
    "false", "cd", "pwd", ":", "id", "whoami", "pgrep", "locate", "apt-cache",
})
# READONLYSUB1126: commands that never run an argument when called with one of these subcommands (PROGI msg 5993:
# 'git grep -n X', 'systemctl status X', 'pip show X'... were refused by the sender-token layer, and here as a loud
# "nothing to inspect" block). Editors stay out ('vim -c "!X"' runs X). Mirrors READ_ONLY_SUBCOMMANDS in
# email-send-gate.mjs.
_READ_ONLY_SUBCOMMANDS = {
    "git": frozenset({"grep", "log", "diff", "show", "status", "add"}),
    "systemctl": frozenset({"status"}),
    "pip": frozenset({"show"}), "pip3": frozenset({"show"}),
    "npm": frozenset({"view", "info", "show"}),
    "docker": frozenset({"images"}),
}
# git global options that cannot make git run a program: '-C DIR', '--git-dir', '--work-tree', '--no-pager'... ;
# '-c', '--config-env' and '--exec-path' can (core.pager, alias.X='!cmd', diff.external), so they are not here.
_GIT_SAFE_GLOBAL = frozenset({"--no-pager", "-P", "--bare", "--no-optional-locks", "--literal-pathspecs",
                              "--no-replace-objects", "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs"})
_GIT_SAFE_GLOBAL_VALUE = ("-C", "--git-dir", "--work-tree", "--namespace")


def _read_only_head(h) -> bool:
    prog = _basename(h[0])
    if prog == "dpkg":
        # only the query actions -S / -L (and their long names), nothing that installs or configures
        opts = [t for t in h[1:] if t.startswith("-")]
        return bool(opts) and all(t in ("-S", "-L", "--search", "--listfiles") for t in opts)
    subs = _READ_ONLY_SUBCOMMANDS.get(prog)
    if not subs:
        return False
    i = 1
    if prog == "git":
        while i < len(h) and h[i].startswith("-"):
            t = h[i]
            if t in _GIT_SAFE_GLOBAL:
                i += 1
                continue
            if t in _GIT_SAFE_GLOBAL_VALUE:
                i += 2
                continue
            if any(o.startswith("--") and t.startswith(o + "=") for o in _GIT_SAFE_GLOBAL_VALUE):
                i += 1
                continue
            return False
    else:
        while i < len(h) and h[i].startswith("-"):
            i += 1
    if i >= len(h) or h[i] not in subs:
        return False
    # ENVPREFIX1126: --ext-diff, and 'git add -e / -i / -p' run a program (the external diff, the editor, the loop).
    if prog == "git":
        args = h[i + 1:]
        if "--ext-diff" in args:
            return False
        if h[i] == "add" and any(t in ("--edit", "--interactive", "--patch")
                                 or (re.match(r"-[A-Za-z]+$", t) and re.search(r"[eip]", t)) for t in args):
            return False
    # 'git grep -O<prog>' / '--open-files-in-pager[=<prog>]' (any unique prefix) runs <prog> on the hits (measured).
    if prog == "git" and h[i] == "grep" and any(
            (re.match(r"-[A-Za-z]*O", t) and not t.startswith("--"))
            or (t.startswith("--o") and "open-files-in-pager".startswith(t[2:].split("=", 1)[0]))
            for t in h[i + 1:]):
        return False
    return True

_HEAD_DEPTH = 8


def _env_split_string(toks):
    """ENVSPLIT1126: (string, index after it) for `env ... -S STRING ...` / `--split-string[=]STRING` / an option
    cluster ending in S ('-iS STRING', '-SSTRING'), or None. env splits STRING into words and runs them as the
    command, followed by the remaining arguments: 'env -S "sendmail x"' sends (measured with bash), and the wrapper
    table only consumed STRING as the value of -S."""
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == "--":
            return None
        if t in ("-S", "--split-string"):
            return (toks[i + 1], i + 2) if i + 1 < len(toks) else None
        if t.startswith("--split-string="):
            return t.split("=", 1)[1], i + 1
        if t.startswith("-") and not t.startswith("--") and "S" in t[1:]:
            k = t.index("S", 1)
            if t[k + 1:]:
                return t[k + 1:], i + 1
            return (toks[i + 1], i + 2) if i + 1 < len(toks) else None
        if t in ("-u", "-C", "--unset", "--chdir"):
            i += 2
            continue
        if t.startswith("-") or _ENV_ASSIGN.match(t):
            i += 1
            continue
        return None
    return None


def _command_heads(toks, _d: int = 0, _positionals_done: bool = False):
    """Every token list that can be the real command of this segment, after the
    leading assignments, command-position keywords and wrappers are stepped over."""
    while toks:
        if _ENV_ASSIGN.match(toks[0]) or toks[0] in _CMD_POSITION_KEYWORDS:
            toks = toks[1:]
            continue
        # REDIRHEAD1126: a leading redirection is stepped over, with its target when that is the next token.
        r = _REDIR_RX.match(toks[0])
        if r:
            toks = toks[2:] if r.group(1) == "" else toks[1:]
            continue
        break
    if not toks:
        return [toks]
    w = _basename(toks[0])
    if _d >= _HEAD_DEPTH:
        # Still a wrapper at the depth bound: the real command is out of sight,
        # so it counts as a send (None) -- the bound errs toward auditing, like
        # an unknown flag does. It used to return the wrapper itself, and nine
        # nested wrappers + sendmail was silently skipped (Samu, #1521 review).
        return [None] if (w in _WRAPPERS or w in ("function", "coproc")) else [toks]
    if w == "function":
        return _command_heads(toks[2:], _d + 1)
    if w == "coproc":
        return _command_heads(toks[1:], _d + 1) + _command_heads(toks[2:], _d + 1)
    # ENVSPLIT1126: the -S string is a command line of its own; its words plus the remaining arguments are one more
    # candidate head. The ordinary wrapper stepping below still runs too, so nothing that matched before is lost.
    split_heads = []
    if w == "env":
        es = _env_split_string(toks)
        if es is not None:
            try:
                words = [t for seg in _segments_tokens(es[0]) for t in seg]
            except ValueError:
                words = es[0].split()
            split_heads = _command_heads(words + list(toks[es[1]:]), _d + 1)
    spec = _WRAPPERS.get(w)
    if spec is None:
        return [toks]
    # LAUNCHGATE1126: a launcher that is BOTH a wrapper and a string launcher ('flock FILE -c STRING', 'sg GROUP -c
    # STRING', 'runuser USER -c STRING') keeps its own head too, so _head_is_send sees its -c STRING.
    if w in _STRING_LAUNCHERS:
        split_heads = split_heads + [toks]
    short_val, long_val, positionals = spec
    if _positionals_done:
        positionals = 0
    i, starts = 1, []
    while i < len(toks):
        t = toks[i]
        if t == "--":
            i += 1
            break
        if t.startswith("--"):
            if "=" not in t and t[2:] in long_val:
                i += 2
                continue
            if "=" not in t:
                starts.append(i + 2)
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            if w == "command" and ("v" in t or "V" in t):
                return []
            k = next((j for j, ch in enumerate(t[1:], 1) if ch in short_val), -1)
            i += 2 if k == len(t) - 1 else 1
            continue
        break
    starts.insert(0, i)
    heads = list(split_heads)
    for s in starts:
        after = toks[s + positionals:]
        # WRAPGATE1126: GNU getopt permutes, so options may also follow the positional ('timeout 2 -- sendmail x',
        # 'timeout 2 -s KILL sendmail x' both run sendmail). After the positionals the flag loop runs once more.
        if positionals and after and after[0].startswith("-"):
            heads += _command_heads([toks[0]] + after, _d + 1, True)
        else:
            heads += _command_heads(after, _d + 1)
    return heads


# Only variables that name a program to RUN count ('env A=sendmail ls' runs no sender: WRAPGATE1126 control).
_RUNNER_VAR = re.compile(r"^(?:GIT_[A-Z_]*(?:DIFF|EDITOR|PAGER|SSH|SSH_COMMAND|ASKPASS|PROXY_COMMAND)|[A-Z_]*PAGER|[A-Z_]*EDITOR|VISUAL|BROWSER|LESSOPEN|LESSCLOSE|[A-Z_]*ASKPASS|BASH_ENV|ENV|PROMPT_COMMAND|SHELL)=")


def _prefix_assignments(toks):
    """ENVPREFIX1126 (PROGI msg 6001): the assignments in front of the command (and after 'env'). Their value runs
    through the program they configure ('GIT_EXTERNAL_DIFF="sendmail a" git diff'). Mirrors prefixAssignments."""
    out, i = [], 0
    while True:
        while i < len(toks) and _ENV_ASSIGN.match(toks[i]):
            out.append(toks[i])
            i += 1
        if i < len(toks) and _basename(toks[i]) == "env":
            i += 1
            while i < len(toks) and (toks[i].startswith("-") or _ENV_ASSIGN.match(toks[i])):
                if _ENV_ASSIGN.match(toks[i]):
                    out.append(toks[i])
                i += 1
            continue
        return out


def _segment_is_send(toks, depth: int) -> bool:
    if any(_RUNNER_VAR.match(a) and _CODE_SENDER_LIT.search(a.split("=", 1)[1]) for a in _prefix_assignments(toks)):
        return True
    heads = _command_heads(toks)
    if any(_head_is_send(h, depth) for h in heads):
        return True
    return _sender_token_layer(toks, heads)


def _segment_names_sender(toks) -> bool:
    if any(_SENDER_PROG.match(_basename(t)) for t in toks):
        return True
    if any(_SENDPY.match(_basename(t)) for t in toks) and any(t == "--to" or t.startswith("--to=") for t in toks):
        return True
    return any(_GRAPHMAIL.match(_basename(t)) for t in toks) and "send" in toks


# SENDERNAMEMSG1126: switched off only inside sender_named_only(), to tell whether a refusal comes from this
# layer alone.
_TOKEN_LAYER_ON = True


def _sender_token_layer(toks, heads) -> bool:
    """LAUNCHGATE1126: a sender named anywhere in the segment, run by something other than a never-exec program."""
    if not _TOKEN_LAYER_ON or not _segment_names_sender(toks):
        return False
    # eval re-parses its arguments and is judged on that re-parse in _head_is_send, so it is not judged here again.
    # find without an exec action runs nothing; with one, _head_is_send has already judged the action's command.
    # A wrapper head (kept only so a string launcher's -c STRING is seen) is represented by its stepped heads.
    hs = [h for h in heads
          if h and _basename(h[0]) not in ("eval", "find") and _basename(h[0]) not in _WRAPPERS]
    # ENVPREFIX1126: no read-only exemption when the segment carries any assignment (GIT_EXTERNAL_DIFF, GIT_EDITOR,
    # PAGER... can make the 'read-only' command run a program).
    any_assign = any(_ENV_ASSIGN.match(t) for t in toks)
    return bool(hs) and not all(_basename(h[0]) in _NON_EXEC_PROGS or (not any_assign and _read_only_head(h))
                                for h in hs)


def _launcher_string(rest, short: str, longs) -> "str | None":
    """The value of `-<short>` (alone, or last in a cluster like '-qc'), `--<long> X` or `--<long>=X`, anywhere."""
    for i, t in enumerate(rest):
        for lg in longs:
            if t == "--" + lg:
                return rest[i + 1] if i + 1 < len(rest) else None
            if t.startswith("--" + lg + "="):
                return t.split("=", 1)[1]
        if len(t) > 1 and t[0] == "-" and not t.startswith("--") and t.endswith(short):
            return rest[i + 1] if i + 1 < len(rest) else None
    return None


def _shell_string_is_send(s: str, depth: int) -> bool:
    if depth >= 3:
        return bool(s.strip())
    return is_send_invocation(s, _depth=depth + 1)


_SHELL_LONG_WITH_VALUE = frozenset({"--rcfile", "--init-file", "--emulate"})
# WRAPSHELL1126: mksh/lksh also take a value after 'T' ('-T /dev/ttyN', '-T -'; in bash 'T' is a plain flag), and
# 'ksh' may be mksh, so the whole ksh family reads it so. ksh93 RUNS its first operand as a command line when no
# file of that name exists (measured: "ksh 'sendmail a@b.hu'" sends, "ksh 'nonexist foo'" says "nonexist: not
# found"), so for ksh/ksh93 without -c the first operand is the command string.
_KSH_FAMILY = re.compile(r"^r?(ksh|ksh93|mksh|lksh)$", re.I)
_KSH93_OPERAND = re.compile(r"^r?ksh(93)?$", re.I)


def _shell_command_string(rest, prog: str = "sh"):
    """The command string of `sh|bash|zsh|dash [options] -c STRING`, or None (SHCLUSTER1126).

    The shell takes the first NON-option argument as the string once -c was given anywhere among the options, and
    -c can sit inside a cluster ('-lc', '-ec', '-xc'). Every 'o' / 'O' in an option word ('-o', '+O', '-euxo', '-co',
    '-oc') takes the next argument as its value, in order. '--rcfile FILE', '--init-file FILE' (bash) and
    '--emulate NAME' (zsh) take a value, every other '--long' option stands alone, '--' and a lone '-' end the
    options, a lone '+' is skipped. Before this, only a bare '-c' token followed directly by the string was seen:
    'bash -lc "sendmail x"' and 'sh -ec "sendmail x"' passed both gates while bash ran them.
    Mirrored in email-send-gate.mjs (shellCommandString)."""
    value_chars = "oOT" if _KSH_FAMILY.match(prog) else "oO"
    saw_c, i = False, 0
    while i < len(rest):
        t = rest[i]
        # A lone '-' ends the options like '--'; a lone '+' is an empty option word and the options go on (bash and
        # dash, measured: 'bash -c - "sendmail x"', 'bash -c + "sendmail x"' and 'bash + -c "sendmail x"' all send).
        if t in ("--", "-"):
            i += 1
            break
        if t == "+":
            i += 1
            continue
        if t in _SHELL_LONG_WITH_VALUE:
            i += 2
            continue
        if t.startswith("--"):
            i += 1
            continue
        if len(t) > 1 and t[0] in "-+":
            if t[0] == "-" and "c" in t[1:]:
                saw_c = True
            # SHCLUSTERFIX1126: each o/O of the word swallows one following argument ('-euxo pipefail -c ...').
            i += 1 + sum(1 for ch in t[1:] if ch in value_chars)
            continue
        break
    if i >= len(rest):
        return None
    return rest[i] if saw_c or _KSH93_OPERAND.match(prog) else None


# WRAPSHELL1126: the strings fish runs: the value of every -c / -C (alone, in a cluster, or attached: '-c"x"',
# '-Nc x'), '--command' / '--init-command' with '=' or the next argument, including the unique prefixes getopt
# accepts ('--comm', '--init'). Options end at the first non-option or '--'. Measured on fish 4.2.1. Mirrors
# fishCommandStrings in email-send-gate.mjs.
_FISH_SHORT_VALUE = "cCdopfD"
_FISH_LONG = ("command", "init-command", "debug", "debug-output", "debug-stack-frames", "profile", "profile-startup",
              "features", "interactive", "login", "no-config", "no-execute", "private", "print-rusage-self",
              "print-debug-categories", "version", "help")
_FISH_LONG_VALUE = frozenset({"command", "init-command", "debug", "debug-output", "debug-stack-frames", "profile",
                              "profile-startup", "features"})
_FISH_SPLIT = re.compile("[\\s;|&()'\"<>{}$`]+")


def _fish_command_strings(rest):
    out = []
    i = 0
    while i < len(rest):
        t = rest[i]
        if t == "--":
            break
        if t.startswith("--"):
            name, eq, value = t[2:].partition("=")
            hits = [name] if name in _FISH_LONG else [lg for lg in _FISH_LONG if lg.startswith(name)]
            # An ambiguous prefix that could be --command / --init-command is read as one (conservative).
            runs = any(lg in ("command", "init-command") for lg in hits)
            takes = any(lg in _FISH_LONG_VALUE for lg in hits)
            val = None
            if eq:
                val = value
            elif takes:
                val = rest[i + 1] if i + 1 < len(rest) else None
                i += 1
            if runs and val is not None:
                out.append(val)
            i += 1
            continue
        if len(t) > 1 and t[0] == "-":
            i += 1
            for k in range(1, len(t)):
                ch = t[k]
                if ch not in _FISH_SHORT_VALUE:
                    continue
                val = None
                if k + 1 < len(t):
                    val = t[k + 1:]
                elif i < len(rest):
                    val = rest[i]
                    i += 1
                if ch in "cC" and val is not None:
                    out.append(val)
                break
            continue
        break
    return out


# EXECVEC1126: git runs a program from its global '-c KEY=VALUE' (alias.X='!cmd', core.sshCommand, core.pager,
# diff.external...) and from 'git grep -O<prog>' / '--open-files-in-pager=<prog>' (measured: all send). A '!' alias
# is a shell command line; any other value that names a sender is a send. Only the options BEFORE the subcommand
# are global ('git grep -c X' is a count flag). Mirrors gitRunsSender in email-send-gate.mjs.
_GIT_GLOBAL_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix"})


def _git_runs_sender(rest, depth: int) -> bool:
    values = []
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        t = rest[i]
        if t == "-c":
            if i + 1 < len(rest):
                values.append(rest[i + 1])
            i += 2
            continue
        if t.startswith("-c") and not t.startswith("--"):
            values.append(t[2:])
            i += 1
            continue
        if t in _GIT_GLOBAL_VALUE:
            i += 2
            continue
        i += 1
    for kv in values:
        key, _, val = kv.partition("=")
        if _CODE_SENDER_LIT.search(val):
            return True
        if re.match(r"alias\.", key, re.I) and val.startswith("!"):
            if depth >= 3:
                if val[1:].strip():
                    return True
            elif is_send_invocation(val[1:], _depth=depth + 1):
                return True
    if i < len(rest) and rest[i] == "grep":
        for t in rest[i + 1:]:
            if t == "--":
                break
            prog = None
            if re.match(r"-[A-Za-z]*O", t) and not t.startswith("--"):
                prog = t[t.index("O") + 1:]
            elif t.startswith("--o") and "open-files-in-pager".startswith(t[2:].split("=", 1)[0]) and "=" in t:
                prog = t.split("=", 1)[1]
            if prog and _CODE_SENDER_LIT.search(prog):
                return True
    return False


def _substitution_output_sends(text: str, depth: int) -> bool:
    """SUBOUTRUN1126 (PROGI msg 6034): 'sh -c "$(...)"' and 'eval "$(...)"' RUN the substitution's OUTPUT. A body that
    names a sender, or whose echo/printf literal parses as a send, is a send. Mirrors substitutionOutputSends."""
    for b in _dquote_substitutions(text, any_context=True):
        if _CODE_SENDER_LIT.search(b):
            return True
        try:
            segs = _segments_tokens(b)
        except ValueError:
            continue
        for t in segs:
            hs = _command_heads(t)
            h = hs[0] if hs else None
            if h and _basename(h[0]) in ("echo", "printf") and depth < 3 and is_send_invocation(
                    " ".join(x for x in h[1:] if not re.match(r"-[neE]+$", x)), _depth=depth + 1):
                return True
    return False


def _head_is_send(toks, depth: int) -> bool:
    if toks is None:  # the depth bound was hit on a wrapper: audit it
        return True
    if not toks:
        return False
    prog = _basename(toks[0])
    rest = toks[1:]
    if _SENDER_PROG.match(prog):
        return True
    # LAUNCHGATE1126: a launcher's -c STRING, watch's joined arguments, and find's -exec command.
    spec_l = _STRING_LAUNCHERS.get(prog)
    if spec_l is not None:
        s = _launcher_string(rest, spec_l[0], spec_l[1:])
        if s is not None and _shell_string_is_send(s, depth):
            return True
    if prog == "watch":
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in ("-n", "--interval") else 1
        if rest[i:] and _shell_string_is_send(" ".join(rest[i:]), depth):
            return True
    if prog == "find":
        for k, t in enumerate(rest):
            if t in _FIND_EXEC:
                sub = []
                for u in rest[k + 1:]:
                    if u in (";", "+"):
                        break
                    sub.append(u)
                if sub and _segment_is_send(sub, depth):
                    return True
    # WRAPGATE1126: eval re-parses its arguments, joined by spaces, as a command line, the same as bash -c
    # ('eval sendmail x', "eval 'cd /tmp && sendmail x'" passed both gates). At the recursion bound the text is
    # out of sight: audit it (fail-closed), like a wrapper at the depth bound.
    if prog == "eval" and rest:
        if depth >= 3:
            return True
        if _substitution_output_sends(" ".join(rest), depth):
            return True
        if is_send_invocation(" ".join(rest), _depth=depth + 1):
            return True
    # burkolo hejj: a -c string-argumentum maga is parancs -- rekurzio
    if _WRAPPER_SHELL.match(prog):
        # SHCLUSTER1126: the command string is located the way the shell does it (_shell_command_string), so an
        # option cluster that carries c ('bash -lc ...', 'sh -ec ...') counts; it used to need a bare '-c' token.
        cmd_string = _shell_command_string(rest, prog)
        if cmd_string is not None and _substitution_output_sends(cmd_string, depth):
            return True
        if cmd_string is not None:
            # SHDEPTH1126: past the recursion bound the command string is out of sight. It used to be read as
            # "not a send", so four nested `bash -c` levels around sendmail passed both gates (measured: bash
            # runs the send). A non-empty command string at the bound is now refused (fail-closed), like a
            # wrapper at the head-depth bound and an eval at the same bound.
            if depth >= 3:
                if cmd_string.strip():
                    return True
            elif is_send_invocation(cmd_string, _depth=depth + 1):
                return True
    if _FISH_SHELL.match(prog):
        # WRAPSHELL1126: every -c / -C string fish would run. fish syntax is not sh syntax ('echo (sendmail x)' is a
        # command substitution there), so a sender NAMED anywhere in the string is a send (conservative; 0 fish
        # calls in the measured fleet corpus), on top of the sh-syntax reading. Same depth bound as the sh family.
        for fs in _fish_command_strings(rest):
            if depth >= 3:
                if fs.strip():
                    return True
                continue
            if _segment_names_sender([w for w in _FISH_SPLIT.split(fs) if w]):
                return True
            if is_send_invocation(fs, _depth=depth + 1):
                return True
    if _SCRIPT_INTERP.match(prog) and any(_script_code_sends(a) for a in rest):
        return True
    if _EDITOR.match(prog):
        for i, t in enumerate(rest):
            cmd = (rest[i + 1] if i + 1 < len(rest) else None) if t in ("-c", "--cmd") else (
                t[1:] if t.startswith("+") else None)
            if cmd and _CODE_SENDER_LIT.search(cmd):
                return True
    if prog == "git" and _git_runs_sender(rest, depth):
        return True
    # EXECVEC1126: curl speaks SMTP itself ('curl smtp://host --mail-rcpt a@b -T body').
    if prog == "curl" and any(
            re.match(r"(--url=)?smtps?://", t, re.I)
            or (t == "--url" and i + 1 < len(rest) and re.match(r"smtps?://", rest[i + 1], re.I))
            for i, t in enumerate(rest)):
        return True
    # interpreter kod-string: python -c / node -e / --eval, ami kuldest hiv
    if _PYTHON.match(prog) or _NODEISH.match(prog):
        for i, t in enumerate(rest):
            if t in ("-c", "-e", "--eval") and i + 1 < len(rest) and _code_string_sends(rest[i + 1]):
                return True
    # send.py futtatasa (kozvetlenul, vagy python/runner utan) --to cimzettel
    candidates = [prog] + (
        [_basename(rest[0])] if rest and (_PYTHON.match(prog) or _NODEISH.match(prog)) else []
    )
    if any(_SENDPY.match(c) for c in candidates) and any(
        t == "--to" or t.startswith("--to=") for t in rest
    ):
        return True
    # graph-mail kimeno alparanccsal (tsx/node runner utan is)
    if any(_GRAPHMAIL.match(_basename(t)) for t in toks) and "send" in rest:
        return True
    # curl/wget: a cel-token akkor is muvelet, ha idezojelben allt -- a
    # horgonyzott minta valasztja el a payload-belseji emlitestol.
    # RESENDGATE826: csak a TENYLEGES kuldes (POST/PUT/... vagy torzs) akad
    # fenn; a read-only GET/HEAD lekerdezes atmegy; a nem-donthato metodus
    # tovabbra is fail-closed.
    if _CURLISH.match(prog) and any(_RESEND_TARGET.match(t) for t in rest):
        return _curl_resend_verdict(rest) != "read"
    return False


def wrapper_depth_hit(cmd: str) -> bool:
    """True when some segment is still a wrapper at the depth bound, so it was
    counted as a send without the real command being seen (HEADDEPTH924). The
    gate uses it to say WHY it blocks: "I could not audit the letter" is the
    wrong reason for `nohup x9 git status` (Marveen, #1522 review)."""
    # Only when the depth bound is the WHOLE reason: if a visible segment is a
    # send on its own (`sendmail x; sudo x9 true`), that send is the reason,
    # and the ordinary wording must stay (Samu, #1523 review).
    try:
        segments = _segments_tokens(cmd)
    except ValueError:
        return False
    heads = [h for toks in segments for h in _command_heads(toks)]
    return any(h is None for h in heads) and not any(
        h is not None and _head_is_send(h, 0) for h in heads)


def sender_named_only(cmd: str) -> bool:
    """SENDERNAMEMSG1126: True when cmd reads as a send ONLY through the sender-token layer: a sender is named in it,
    but no rule sees a send being started. The copy gate still refuses it (fail-closed), with its own message."""
    global _TOKEN_LAYER_ON
    _TOKEN_LAYER_ON = False
    try:
        return not is_send_invocation(cmd)
    finally:
        _TOKEN_LAYER_ON = True


def _heredoc_substitutions(cmd: str):
    """HEREDOCSUB1126 (PROGI msg 6028, BELA msg 6029): the body of an UNQUOTED heredoc ('<<EOF', not '<<'EOF'' /
    '<<"EOF"' / '<<\\EOF') is expanded for ANY command. Quotes inside such a body are plain characters, so only \\,
    `...` and $(...) count. Mirrors heredocSubstitutions in email-send-gate.mjs."""
    out = []
    for m in _HEREDOC.finditer(cmd):
        if re.match(r"<<-?\s*['\"\\]", m.group(1)):
            continue
        body = m.group(0)[len(m.group(1)) + 1:len(m.group(0)) - len(m.group(2))]
        n = len(body)
        i = 0
        while i < n:
            ch = body[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "`":
                j = i + 1
                while j < n and body[j] != "`":
                    if body[j] == "\\":
                        j += 1
                    j += 1
                out.append(body[i + 1:j])
                i = j + 1
                continue
            if ch == "$" and i + 1 < n and body[i + 1] == "(":
                j, level, iq = i + 2, 1, None
                while j < n and level > 0:
                    c = body[j]
                    if iq:
                        if c == "\\" and iq == '"':
                            j += 1
                        elif c == iq:
                            iq = None
                    elif c in ("'", '"'):
                        iq = c
                    elif c == "\\":
                        j += 1
                    elif c == "(":
                        level += 1
                    elif c == ")":
                        level -= 1
                    j += 1
                out.append(body[i + 2:j - 1] if level == 0 else body[i + 2:j])
                i = j
                continue
            i += 1
    return out


def _dquote_substitutions(cmd: str, any_context: bool = False):
    """DQSUB1126 (PROGI msg 6001): the bodies of every $(...) and `...` inside DOUBLE quotes. bash runs them; the mask
    keeps quoted text as content, so 'echo "$(sendmail a)"' and 'X="$(sendmail a)"' passed both gates. Single quotes
    stay literal; heredoc bodies are cut first, like the tokenizer does. Mirrors dquoteSubstitutions in
    email-send-gate.mjs."""
    s = _HEREDOC.sub(r"\1", cmd)
    out = [] if any_context else _heredoc_substitutions(cmd)
    q = None
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if q == "'":
            if ch == "'":
                q = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if q is None:
            if ch in ("'", '"'):
                q = ch
                i += 1
                continue
            if not any_context:
                i += 1
                continue
        elif ch == '"':
            q = None
            i += 1
            continue
        if ch == "`":
            j = i + 1
            while j < n and s[j] != "`":
                if s[j] == "\\":
                    j += 1
                j += 1
            out.append(s[i + 1:j])
            i = j + 1
            continue
        if ch == "$" and i + 1 < n and s[i + 1] == "(":
            j = i + 2
            level = 1
            iq = None
            while j < n and level > 0:
                c = s[j]
                if iq:
                    if c == "\\" and iq == '"':
                        j += 1
                    elif c == iq:
                        iq = None
                elif c in ("'", '"'):
                    iq = c
                elif c == "\\":
                    j += 1
                elif c == "(":
                    level += 1
                elif c == ")":
                    level -= 1
                j += 1
            out.append(s[i + 2:j - 1] if level == 0 else s[i + 2:j])
            i = j
            continue
        i += 1
    return out


# STDINSHELL1126 (PROGI msg 6001): a shell that reads its commands from STDIN runs text the segment rules never see:
# 'echo "sendmail a" | sh', 'printf ... | bash -s', 'git log --format=%s | sh', 'bash <<< "sendmail a"',
# 'source /dev/stdin <<< ...' and the body of 'bash <<EOF ... EOF'. Mirrors stdinShellTexts in email-send-gate.mjs.
_STDIN_FILES = frozenset({"/dev/stdin", "-", "/proc/self/fd/0", "/dev/fd/0"})


def _without_redirections(rest):
    out = []
    i = 0
    while i < len(rest):
        m = _REDIR_RX.match(rest[i])
        if m:
            i += 2 if m.group(1) == "" else 1
            continue
        out.append(rest[i])
        i += 1
    return out


def _is_stdin_shell_head(h) -> bool:
    if not h:
        return False
    prog = _basename(h[0])
    rest = _without_redirections(h[1:])
    if prog in ("source", "."):
        return bool(rest) and rest[0] in _STDIN_FILES
    if _FISH_SHELL.match(prog):
        if _fish_command_strings(rest):
            return False
        return not any(not t.startswith("-") for t in rest)
    if not _WRAPPER_SHELL.match(prog):
        return False
    if _shell_command_string(rest, prog) is not None:
        return False
    value_chars = "oOT" if _KSH_FAMILY.match(prog) else "oO"
    saw_s, i = False, 0
    while i < len(rest):
        t = rest[i]
        if t in ("--", "-"):
            i += 1
            break
        if t == "+":
            i += 1
            continue
        if t in _SHELL_LONG_WITH_VALUE:
            i += 2
            continue
        if t.startswith("--"):
            i += 1
            continue
        if len(t) > 1 and t[0] in "-+":
            if t[0] == "-" and "s" in t[1:]:
                saw_s = True
            i += 1 + sum(1 for ch in t[1:] if ch in value_chars)
            continue
        break
    return saw_s or i >= len(rest) or rest[i] in _STDIN_FILES


# Calibrated on the fleet's real python heredocs (2255; the plain _code_string_sends rule refused 5, all scripts
# that EDIT or TEST mail code, the mail words inside string literals): blank string literals line by line, then a
# line sends when a mail call stands OUTSIDE strings, or a process starter stands outside strings and that same
# line names a sender. Mirrors interpCodeSends in email-send-gate.mjs.
_TRIPLE_STR = re.compile(r"'''[\s\S]*?'''|\"\"\"[\s\S]*?\"\"\"")
_LINE_STR = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`")


def _blank_strings(code: str) -> str:
    keep = lambda m: re.sub(r"[^\n]", " ", m.group(0))
    return _LINE_STR.sub(keep, _TRIPLE_STR.sub(keep, code))


def _interp_code_sends(code: str) -> bool:
    bare = _blank_strings(code).split("\n")
    orig = code.split("\n")
    return any(_CODE_SEND.search(l) or (_CODE_EXECISH.search(l) and _CODE_SENDER_LIT.search(orig[i] if i < len(orig) else ""))
               for i, l in enumerate(bare))


def _is_stdin_interpreter_head(h) -> bool:
    """INTERPSTDIN1126 (PROGI msg 6034): python / node reading their PROGRAM from stdin. That text is code, judged by
    _code_string_sends, never by name. Mirrors isStdinInterpreterHead in email-send-gate.mjs."""
    if not h:
        return False
    prog = _basename(h[0])
    is_py = bool(_PYTHON.match(prog) or re.match(r"python3\.\d+$", prog, re.I))
    if not is_py and prog != "node":
        return False
    rest = _without_redirections(h[1:])
    run_flags = ("-c", "-m") if is_py else ("-e", "--eval", "-p", "--print")
    for t in rest:
        if t in run_flags or (is_py and re.match(r"-[A-Za-z]*[cm]", t) and not t.startswith("--")):
            return False
        if t == "-":
            return True
        if not t.startswith("-"):
            return False
    return True


def _segments_with_separators(cmd: str):
    """[(tokens, separator that ENDED the segment)], the tokenizer of _segments_tokens; an empty segment (a run of
    operators) adds its separator to the previous one."""
    import shlex
    lex = shlex.shlex(_mask_subshell_markers(_HEREDOC.sub(r"\1", cmd)), posix=True, punctuation_chars="();|&")
    lex.whitespace_split = True
    lex.commenters = ""
    out, cur = [], []
    for tok in lex:
        if tok and set(tok) <= set("();|&"):
            if not cur and out:
                out[-1][1] += tok
            else:
                out.append([cur, tok])
            cur = []
        else:
            cur.append(tok)
    if cur or not out:
        out.append([cur, ""])
    return out


def _stdin_shell_texts(cmd: str):
    """(run, piped): command texts a stdin shell runs, and the pipeline texts it reads (judged by name only)."""
    run, piped, code = [], [], []
    try:
        segs = _segments_with_separators(cmd)
    except ValueError:
        return run, piped, code

    def is_pipe(sep):
        return sep.endswith("|") and not sep.endswith("||")

    for k, (toks, sep_k) in enumerate(segs):
        if not toks:
            continue
        # PROCSUB1126 (PROGI msg 6034): 'source <(...)', '. <(...)', 'bash <(...)', 'bash < <(...)' run the OUTPUT of
        # the process substitution as a script.
        if len(toks) >= 2 and toks[-1] == "<" and sep_k == "(":
            base = toks[:-1]
            if base and base[-1] == "<":
                base = base[:-1]
            phs = _command_heads(base) if base else []
            ph = phs[0] if phs else None
            pp = _basename(ph[0]) if ph else ""
            if pp in ("source", ".") or ((_WRAPPER_SHELL.match(pp) or _FISH_SHELL.match(pp))
                                         and _shell_command_string(_without_redirections(ph[1:]), pp) is None):
                body, depth, m2 = [], 1, k + 1
                while m2 < len(segs) and depth > 0:
                    body.append(segs[m2][0])
                    depth += segs[m2][1].count("(") - segs[m2][1].count(")")
                    m2 += 1
                piped.append(" ; ".join(" ".join(b) for b in body))
                for b in body:
                    bhs = _command_heads(b) if b else []
                    bh = bhs[0] if bhs else None
                    if bh and _basename(bh[0]) in ("echo", "printf"):
                        run.append(" ".join(t for t in bh[1:] if not re.match(r"-[neE]+$", t)))
        heads = _command_heads(toks)
        if heads and _is_stdin_interpreter_head(heads[0]):
            for i, t in enumerate(toks):
                if t == "<<<" and i + 1 < len(toks):
                    code.append(toks[i + 1])
                elif t.startswith("<<<") and len(t) > 3:
                    code.append(t[3:])
            if k > 0 and is_pipe(segs[k - 1][1]):
                lhs = _command_heads(segs[k - 1][0])
                lh = lhs[0] if lhs else None
                if lh and _basename(lh[0]) in ("echo", "printf"):
                    code.append(" ".join(lh[1:]))
            continue
        if not heads or not _is_stdin_shell_head(heads[0]):
            continue
        for i, t in enumerate(toks):
            if t == "<<<" and i + 1 < len(toks):
                run.append(toks[i + 1])
            elif t.startswith("<<<") and len(t) > 3:
                run.append(t[3:])
        j = k - 1
        left = []
        feeders = []
        while j >= 0 and is_pipe(segs[j][1]):
            left.insert(0, " ".join(segs[j][0]))
            feeders.insert(0, segs[j][0])
            j -= 1
        # GROUPPIPE1126 (PROGI msg 6034): '{ echo X; } | bash' -- collect the group back to its opening '{'.
        if feeders and feeders[0] == ["}"]:
            while j >= 0:
                left.insert(0, " ".join(segs[j][0]))
                feeders.insert(0, segs[j][0])
                if segs[j][0] and segs[j][0][0] == "{":
                    break
                j -= 1
        if left:
            piped.append(" | ".join(left))
            for f in feeders:
                lhs = _command_heads(f)
                lh = lhs[0] if lhs else None
                if lh and _basename(lh[0]) in ("echo", "printf"):
                    run.append(" ".join(t for t in lh[1:] if not re.match(r"-[neE]+$", t)))
    for m in _HEREDOC.finditer(cmd):
        line_start = cmd.rfind("\n", 0, m.start()) + 1
        try:
            head_segs = _segments_tokens(cmd[line_start:m.start()])
        except ValueError:
            continue
        if not head_segs:
            continue
        hs = _command_heads(head_segs[-1])
        body = m.group(0)[len(m.group(1)) + 1:len(m.group(0)) - len(m.group(2))]
        if hs and _is_stdin_shell_head(hs[0]):
            run.append(body)
            continue
        if hs and _is_stdin_interpreter_head(hs[0]):
            code.append(body)
            continue
        # 'cat <<EOF | bash': the heredoc feeds a command whose output is piped into a stdin shell on the same line.
        try:
            hl = _segments_with_separators(cmd[line_start:m.start() + len(m.group(1))])
        except ValueError:
            continue
        f = len(hl) - 1
        while f >= 0 and not any(t.startswith("<<") for t in hl[f][0]):
            f -= 1
        if f < 0:
            continue
        g, shell_after = f, False
        while g < len(hl) - 1 and is_pipe(hl[g][1]):
            g += 1
            gh = _command_heads(hl[g][0])
            if gh and _is_stdin_shell_head(gh[0]):
                shell_after = True
                break
        if not shell_after:
            continue
        fh = _command_heads(hl[f][0])
        if fh and fh[0] and _basename(fh[0][0]) == "cat":
            run.append(body)
        else:
            piped.append(body)
    return run, piped, code


def is_send_invocation(cmd: str, _depth: int = 0) -> bool:
    run, piped, code = _stdin_shell_texts(cmd)
    if any(_CODE_SENDER_LIT.search(t) for t in piped):
        return True
    if any(_interp_code_sends(t) for t in code):
        return True
    for text in run:
        if _depth >= 3:
            if text.strip():
                return True
        elif is_send_invocation(text, _depth=_depth + 1):
            return True
    for inner in _dquote_substitutions(cmd):
        if _depth >= 3:
            if inner.strip():
                return True
        elif is_send_invocation(inner, _depth=_depth + 1):
            return True
    try:
        segments = _segments_tokens(cmd)
    except ValueError:
        # Parse-hiba (pl. lezaratlan idezojel): nem tudunk poziciot mondani.
        # Konzervativ visszaeses: csak akkor auditalunk, ha eros kuldes-literal
        # all a szovegben -- igy egy fura, de valodi kuldes nem csuszik at
        # neman, a tipikus belso parancsok viszont nem kapnak hamis pozitivot.
        return bool(_FALLBACK_LITERALS.search(cmd))
    return any(_segment_is_send(toks, _depth) for toks in segments)

# --- Hungarian detection (accent-insensitive markers) -----------------------
# These fire on both the correct and the stripped spelling, so a transliterated
# mail is still recognised as Hungarian -- that is the whole point.
HU_MARKERS = [
    "hogy", "nem", "vagy", "amit", "ami", "mert", "ezt", "ez a", "van", "lesz",
    "kell", "tehat", "tehát", "koszonom", "köszönöm", "szia", "sziasztok",
    "kerlek", "kérlek", "csatolva", "udvozlettel", "üdvözlettel", "levelet",
    "kuldom", "küldöm", "jelezz", "irj", "írj", "mar", "már", "csak",
]
# A puszta "level" 2026-08-26-an kikerult a markerek kozul. Magyar markerkent
# gyenge (a gyakori alak a "levelet", az mar bent van), viszont az ANGOL "level"
# minden elofordulasa magyar-pontot adott egy angol szovegnek, es igy indithatta
# el az ekezet-vizsgalatot olyan uzeneten, ami nem is magyar.

# Accentless spellings of frequent Hungarian words -> the correct form. Every
# entry is a word that CANNOT be spelled without its accent, so a hit inside
# Hungarian text is an error, not a style choice.
ACCENTLESS = {
    "es": "és", "tehat": "tehát", "koszonom": "köszönöm", "koszi": "köszi",
    "koszonjuk": "köszönjük", "kerlek": "kérlek", "kerem": "kérem",
    "kerjuk": "kérjük", "kerdes": "kérdés", "kerdesem": "kérdésem",
    "valasz": "válasz", "valaszt": "választ", "valaszol": "válaszol",
    # "levelet" NEM tartozik ide (2026-08-11, hamis pozitiv eles levelen):
    # a szotar invarianca az, hogy minden bejegyzes olyan szo, amit ekezet
    # NELKUL nem lehet leirni. A "levelet" (levél -> levelet) pont ilyen helyes
    # alak, ezert onmagara mutato bejegyzeskent allt itt, es minden korrekt
    # magyar levelet megblokkolt "levelet -> levelet" javaslattal. A "levelét"
    # (birtokos) ekezetlen alakja EGYBEESIK vele, tehat szabalykent nem is
    # eldontheto -- ezt a kapu nem tudja megfogni, es nem is szabad neki.
    "level": "levél", "levelre": "levélre",
    "elore": "előre", "elott": "előtt", "utan": "után", "kozott": "között",
    "kesz": "kész", "keszult": "készült", "keszen": "készen",
    "ervenyes": "érvényes", "ervenytelen": "érvénytelen",
    "telepito": "telepítő", "telepites": "telepítés", "telepiteni": "telepíteni",
    "ujra": "újra", "uj": "új", "ujat": "újat", "igy": "így", "ugy": "úgy",
    "tobb": "több", "tobbi": "többi", "kulon": "külön", "kuldom": "küldöm",
    "kuldtem": "küldtem", "kuldes": "küldés", "kuldunk": "küldünk",
    "fajl": "fájl", "fajlt": "fájlt", "fajlok": "fájlok",
    "hatarido": "határidő", "hataridot": "határidőt",
    "lehetoseg": "lehetőség", "lehetoseget": "lehetőséget",
    "szukseges": "szükséges", "szuksege": "szüksége",
    "mukodik": "működik", "mukodes": "működés", "muszaki": "műszaki",
    "beallitas": "beállítás", "beallitani": "beállítani",
    "elofizetes": "előfizetés", "elofizetest": "előfizetést",
    "szamla": "számla", "szamlat": "számlát", "szamlazas": "számlázás",
    "arajanlat": "árajánlat", "ar": "ár", "arak": "árak",
    "ora": "óra", "orakor": "órakor", "ev": "év", "evi": "évi",
    "honap": "hónap", "het": "hét", "hetfo": "hétfő", "csutortok": "csütörtök",
    "pentek": "péntek", "januar": "január", "februar": "február",
    "marcius": "március", "aprilis": "április", "majus": "május",
    "junius": "június", "julius": "július", "oktober": "október",
    "ket": "két", "harom": "három", "negy": "négy", "ot": "öt",
    "szivesen": "szívesen", "erteket": "értéket", "ertem": "értem",
    "jol": "jól", "rovid": "rövid", "hosszu": "hosszú", "biztonsagos": "biztonságos",
    # "megnyitni" ugyanaz a hibaosztaly, mint a fenti "levelet": onmagara mutato
    # bejegyzes egy olyan szonal, ami ekezet nelkul is helyes. Kiveve 2026-08-11,
    # MIELOTT eles levelen elsult volna.
    "eleresi": "elérési", "elerheto": "elérhető",
    "sajat": "saját", "tovabbi": "további", "tovabb": "tovább",
    "figyelmeztetes": "figyelmeztetés", "ellenorizd": "ellenőrizd",
    "ellenorzes": "ellenőrzés", "reszletek": "részletek", "resz": "rész",
    "vegen": "végén", "vegre": "végre", "elinditja": "elindítja",
    "inditas": "indítás", "masold": "másold", "masolat": "másolat",
    "gepre": "gépre", "gep": "gép", "gepen": "gépen",
    "ervenyesites": "érvényesítés", "aktivalas": "aktiválás",
    "hozzajarulas": "hozzájárulás", "elofordul": "előfordul",
    # +48 bejegyzes 2026-08-13 (EKEZETLISTA812, Szabi GO: "Vedd"). A jeloltek a
    # sajat magyar szovegeinkbol jottek (69 fajl, 60 gyakori ekezetes szo, amit a
    # lista addig nem fogott); a szures KET lepcsos volt: lokalis modell (Muse
    # Glimmer 30B) itelete + sajat felulvizsgalat. SZANDEKOSAN KIMARADT, mert az
    # ekezetlen alak onmagaban is letezo magyar szo: mar (marni), meg (igekoto),
    # kor (eletkor/korszak), fonok (fonni), var (a seb varja), kod (kod ES kod),
    # hozza, meres, lepes, szamlazz, jon, tovabbra. Az "all" -> "áll" TUDATOS
    # kivetel: nem magyar szo, de gyakori ANGOL szo; a kapu csak magyarnak mert
    # szovegen fut, ezert Szabi vallalta a kockazatot.
    "elso": "első", "ezert": "ezért", "valodi": "valódi",
    "nelkul": "nélkül", "miert": "miért", "utana": "utána",
    "kovetkezo": "következő", "szekcio": "szekció", "tenyleg": "tényleg",
    "videot": "videót", "video": "videó", "azert": "azért",
    "hivas": "hívás", "szam": "szám", "szoveg": "szöveg",
    "mas": "más", "kulso": "külső", "dontes": "döntés",
    "letezik": "létezik", "kozvetlenul": "közvetlenül", "felhasznalo": "felhasználó",
    "nema": "néma", "verzio": "verzió", "erdemes": "érdemes",
    "irja": "írja", "mostantol": "mostantól", "latszik": "látszik",
    "szoval": "szóval", "kozos": "közös", "netto": "nettó",
    "cim": "cím", "futo": "futó", "javitas": "javítás",
    "kockazat": "kockázat", "ebbol": "ebből", "mindket": "mindkét",
    "eleg": "elég", "regi": "régi", "kulonbozo": "különböző",
    "kezzel": "kézzel", "peldaul": "például", "izolalt": "izolált",
    "kozben": "közben", "udvozlettel": "üdvözlettel", "oket": "őket",
    "afa": "áfa", "allapot": "állapot", "all": "áll",
    # EKEZET1018 (2026-10-18): +476 entries. The candidates are accented word forms collected from a live
    # install's own correctly accented Hungarian customer-facing copy (web pages, ad and post texts), after
    # stripping code, paths and hyphenated identifiers. The trigger: short accentless messages ('unnepek', 'lakas',
    # 'haloszoba', 'padlofutes', 'kulcsszef') went through the gate. Each key went through the filters below and a
    # manual review (three review rounds). LEFT OUT, with the reason:
    #   automatically: accentless forms of <= 3 letters (meg, kor, kod, var, fel, jo, mar, ...: too many homonyms),
    #   forms that are also words in correctly accented Hungarian prose (helyen), English words (panorama), and
    #   everything that was already in the dictionary;
    #   by manual judgement, because the accentless form is ANOTHER real Hungarian word: mert, mertek, mertem,
    #   merve, merne, merni, meri (merni = 'dare'), akar, kapna, kapcsolhatok, agyak, aggyal, varja, fele, teli,
    #   termek, hajra, deli, tora, futott (fut), mentes ('free of'), tartalmat (accusative of tartalom, the same
    #   class as 'levelet'), tartsatok (imperative), tarolunk (tarol is a verb), and instrumental -nal/-nel forms
    #   of place names; frequent English / international forms: dont, fore, datum, foto, hatter; proper names and
    #   place names in general (this dictionary holds no proper names).
    # Where the accentless form can stand for two correct forms, both are suggested (kornyeken, szoveget, ertek,
    # meglevo, szallashelyen, ...), and where the spelling rules require two words, that form is named first
    # (ujepitesu -> 'új építésű', nyitvatartast -> 'nyitva tartást'; AkH12).
    "ablakaba": "ablakába", "adataibol": "adataiból", "adatfeldolgozokent": "adatfeldolgozóként",
    "adatkezelesi": "adatkezelési", "adatkezelo": "adatkezelő", "adatkezeloje": "adatkezelője",
    "adatvedelmi": "adatvédelmi", "adozasi": "adózási", "ajanlat": "ajánlat",
    "ajanlatot": "ajánlatot", "alakjaban": "alakjában", "alapjan": "alapján", "alapveto": "alapvető",
    "allitas": "állítás", "allitashoz": "állításhoz", "allithatjatok": "állíthatjátok", "allithato": "állítható",
    "alvohely": "alvóhely", "alvohelyet": "alvóhelyet / alvóhelyét", "apartmantol": "apartmantól", "atjon": "átjön",
    "atment": "átment", "atnezve": "átnézve", "atutalas": "átutalás", "autoval": "autóval", "bajtokra": "bájtokra",
    "bajtra": "bájtra", "bankkartya": "bankkártya",
    "bankkartyas": "bankkártyás", "barki": "bárki", "barmikor": "bármikor", "beallitasok": "beállítások",
    "beallitasokban": "beállításokban", "bejutas": "bejutás", "belul": "belül", "berelt": "bérelt", "betu": "betű",
    "beture": "betűre", "bongeszod": "böngésződ", "bonthato": "bontható", "borvidek": "borvidék",
    "bringaut": "bringaút", "bringaval": "bringával", "cellaba": "cellába", "cimben": "címben",
    "csaladoknak": "családoknak", "csuszda": "csúszda", "datuma": "dátuma", "datumkorlat": "dátumkorlát",
    "datumok": "dátumok", "datumos": "dátumos", "datumot": "dátumot", "datumsor": "dátumsor",
    "dobozaban": "dobozában", "dohanyzas": "dohányzás", "dontese": "döntése", "dontesi": "döntési", "donto": "döntő",
    "edeny": "edény", "egesz": "egész", "egeszeben": "egészében", "egyeb": "egyéb", "egyesevel": "egyesével",
    "egyezest": "egyezést", "egymas": "egymás", "ejes": "éjes", "ejjel": "éjjel", "ejre": "éjre",
    "ejszaka": "éjszaka", "ejszam": "éjszám", "ejszamot": "éjszámot", "ekezet": "ékezet", "ekezetet": "ékezetet / ékezetét",
    "ekezettel": "ékezettel", "elemzese": "elemzése", "elemzesehez": "elemzéséhez", "elerhetoseg": "elérhetőség",
    "elerhetosegek": "elérhetőségek", "elirtuk": "elírtuk", "elkepzelheto": "elképzelhető",
    "ellenorizheted": "ellenőrizheted", "ellenorizve": "ellenőrizve", "ellenorzesek": "ellenőrzések",
    "elmeny": "élmény", "elobb": "előbb", "eloirasoknak": "előírásoknak", "elotte": "előtte", "elotti": "előtti",
    "elsodleges": "elsődleges", "elteres": "eltérés", "elutasitana": "elutasítana", "elutasitasi": "elutasítási",
    "epitesu": "építésű", "erdekeben": "érdekében", "erheto": "érhető", "erhetok": "érhetők", "erkezes": "érkezés",
    "erkezessel": "érkezéssel", "erkezik": "érkezik", "ertek": "érték / értek", "ertekeles": "értékelés",
    "ertekelesek": "értékelések", "eszkozt": "eszközt", "eszkozzel": "eszközzel", "etterem": "étterem",
    "ettol": "ettől", "evszakban": "évszakban", "felbontason": "felbontáson", "felhasznaloi": "felhasználói",
    "felnott": "felnőtt", "feltetelek": "feltételek", "felugyeletevel": "felügyeletével", "felul": "felül",
    "fizetes": "fizetés", "foglalas": "foglalás", "foglalasi": "foglalási", "foglalaskezeles": "foglaláskezelés",
    "foglalasnal": "foglalásnál", "foglalasra": "foglalásra", "foglalast": "foglalást", "foglalhato": "foglalható",
    "foglalo": "foglaló", "foglaloig": "foglalóig", "foldszint": "földszint", "foldszinti": "földszinti",
    "fooldal": "főoldal", "fooldalon": "főoldalon", "fooldalra": "főoldalra", "formatumban": "formátumban",
    "forras": "forrás", "forrashoz": "forráshoz", "forrasukhoz": "forrásukhoz", "foszezonban": "főszezonban",
    "fozolap": "főzőlap", "furdoszoba": "fürdőszoba", "futheto": "fűthető", "fuves": "füves", "graphbol": "graphból",
    "grillezo": "grillező", "grillezohely": "grillezőhely", "gyujtott": "gyűjtött / gyújtott", "hajszarito": "hajszárító",
    "haloban": "hálóban", "haloszoba": "hálószoba", "haloszobaban": "hálószobában", "haloszobaszam": "hálószobaszám",
    "haloszobaval": "hálószobával", "halozat": "hálózat", "hany": "hány", "harmas": "hármas", "hasznal": "használ",
    "hasznalhatjatok": "használhatjátok", "hasznalhatnak": "használhatnak", "hasznalhattok": "használhattok",
    "hasznaljak": "használják / használjak", "hasznaljuk": "használjuk", "hasznalt": "használt",
    "hasznalunk": "használunk", "hatalyos": "hatályos", "hatosag": "hatóság", "hazadat": "házadat / hazádat",
    "helyiseget": "helyiséget / helyiségét", "hetvege": "hétvége", "hetvegek": "hétvégék", "hetvegen": "hétvégén",
    "hetvegere": "hétvégére", "hirdetesbol": "hirdetésből", "hirdetesek": "hirdetések", "hirdetesen": "hirdetésen / hirdetésén",
    "hirdetesszovege": "hirdetésszövege", "hofok": "hőfok", "hofokot": "hőfokot", "homerseklet": "hőmérséklet",
    "homokozo": "homokozó", "honapig": "hónapig", "hoszivattyus": "hőszivattyús", "hoterkepek": "hőtérképek",
    "hozzajarul": "hozzájárul", "hozzajarulasat": "hozzájárulását", "hutes": "hűtés", "hutest": "hűtést",
    "hutheto": "hűthető", "idejeig": "idejéig", "idopontot": "időpontot", "iger": "ígér", "inditasa": "indítása",
    "indulas": "indulás", "informaciok": "információk", "informacioszabadsag": "információszabadság",
    "irhato": "írható", "irjon": "írjon", "irjuk": "írjuk", "jatszoter": "játszótér", "jatszoteret": "játszóteret / játszóterét",
    "jogszabalyi": "jogszabályi", "jogszabalyoknak": "jogszabályoknak", "jonnel": "jönnél",
    "jovahagyas": "jóváhagyás", "jovahagyott": "jóváhagyott", "jutast": "jutást", "kanape": "kanapé",
    "kanapeagy": "kanapéágy", "karacsony": "karácsony", "kartya": "kártya", "kattintassal": "kattintással",
    "kauciot": "kauciót", "kavefozo": "kávéfőző", "kedvezmeny": "kedvezmény", "kenyelmes": "kényelmes",
    "kepek": "képek", "kepen": "képen / képén", "kepernyo": "képernyő", "kepernyoje": "képernyője", "kepre": "képre",
    "kerdesekkel": "kérdésekkel", "kerekparokat": "kerékpárokat", "kerekpartarolo": "kerékpártároló",
    "kerekpartaroloban": "kerékpártárolóban", "keri": "kéri", "kertjeben": "kertjében", "kertjet": "kertjét",
    "kerul": "kerül", "kerunk": "kérünk", "kezeleseben": "kezelésében", "kezeljuk": "kezeljük",
    "kihuzhato": "kihúzható", "kikotoi": "kikötői", "kilato": "kilátó", "kitetel": "kitétel", "kivul": "kívül",
    "kockazatos": "kockázatos", "koddal": "kóddal", "kodjai": "kódjai", "kodot": "kódot / ködöt", "koncepcio": "koncepció",
    "konyhakep": "konyhakép", "konyhas": "konyhás", "konyhaval": "konyhával", "korbekeritve": "körbekerítve",
    "korben": "körben", "korbol": "körből / korból", "korfajl": "körfájl", "korfajlba": "körfájlba",
    "kornyeken": "környéken / környékén", "kornyezetben": "környezetben", "korul": "körül",
    "kotelezettseg": "kötelezettség", "kotelezo": "kötelező", "kotheto": "köthető", "kovess": "kövess",
    "kozel": "közel", "kozelben": "közelben", "kozelgo": "közelgő", "kozeli": "közeli",
    "kozremukodik": "közreműködik", "koztarsasag": "köztársaság", "kozti": "közti", "kozvetlen": "közvetlen",
    "kreativok": "kreatívok", "kulcsszeffel": "kulcsszéffel", "kulonallo": "különálló", "kulonben": "különben",
    "lakas": "lakás", "lakasban": "lakásban", "lakashoz": "lakáshoz", "lakassal": "lakással", "lakasunk": "lakásunk",
    "lakhatasi": "lakhatási", "lassuk": "lássuk", "latjuk": "látjuk", "latogato": "látogató",
    "latogatok": "látogatók / látogatok", "latogatottsag": "látogatottság", "latogatottsagot": "látogatottságot",
    "legkondicionalt": "légkondicionált", "legrovidebb": "legrövidebb", "leiras": "leírás", "leirast": "leírást",
    "lejar": "lejár", "lemondas": "lemondás", "lemondasi": "lemondási",
    "letszam": "létszám", "levo": "lévő / levő", "listaja": "listája", "listajahoz": "listájához", "majustol": "májustól",
    "masodik": "második", "masodperces": "másodperces", "medencek": "medencék", "medencet": "medencét",
    "megallitja": "megállítja", "megerkezhettek": "megérkezhettek", "megfelelo": "megfelelő",
    "megfeleloen": "megfelelően", "megfigyeles": "megfigyelés", "megis": "mégis",
    "megjelenitesehez": "megjelenítéséhez", "megkeresheto": "megkereshető", "megkulonbozteto": "megkülönböztető",
    "meglevo": "meglévő / meglevő", "megnezve": "megnézve", "megnyitasa": "megnyitása", "megtalalhato": "megtalálható",
    "megtekinteseig": "megtekintéséig", "mentese": "mentése", "meretu": "méretű", "merhetoseg": "mérhetőség",
    "merjuk": "mérjük / merjük", "mezoje": "mezője", "mikrohullamu": "mikrohullámú", "mindharom": "mindhárom",
    "mindkettoben": "mindkettőben", "molo": "móló", "mosogatogep": "mosogatógép", "mosogep": "mosógép",
    "mosogeppel": "mosógéppel", "mukodesehez": "működéséhez", "munkaterulet": "munkaterület", "mutans": "mutáns",
    "mutanson": "mutánson", "nalunk": "nálunk", "napfeny": "napfény", "napjan": "napján", "napozashoz": "napozáshoz",
    "nappalibol": "nappaliból", "naptar": "naptár", "naptarban": "naptárban", "naptarig": "naptárig",
    "nehany": "néhány", "nekunk": "nekünk", "nelkuli": "nélküli", "nezd": "nézd", "neztem": "néztem",
    "nezzetek": "nézzetek / nézzétek", "nyar": "nyár", "nyilik": "nyílik",
    "nyitvatartast": "nyitva tartást / nyitvatartást", "onallo": "önálló",
    "orszagbol": "országból", "orzesi": "őrzési", "orzi": "őrzi", "osszel": "ősszel", "oszi": "őszi",
    "padlofutes": "padlófűtés", "padlofutessel": "padlófűtéssel", "parkolas": "parkolás", "parkolo": "parkoló",
    "parkoloval": "parkolóval", "paroknak": "pároknak", "partisztasag": "pártisztaság",
    "partisztasaga": "pártisztasága", "partjan": "partján", "pezsgo": "pezsgő", "pihenes": "pihenés",
    "piheneshez": "pihenéshez", "pihenesre": "pihenésre", "privat": "privát", "proban": "próbán",
    "ralatas": "rálátás", "redony": "redőny", "regisztracio": "regisztráció",
    "rendezvenyek": "rendezvények", "resze": "része", "reszleteket": "részleteket", "rogzitett": "rögzített",
    "rogzitunk": "rögzítünk", "segitheti": "segítheti", "seta": "séta", "setany": "sétány", "soran": "során",
    "sotet": "sötét", "sotetitett": "sötétített", "strandfurdo": "strandfürdő", "surgos": "sürgős", "suti": "süti",
    "sutijei": "sütijei", "sutik": "sütik", "sutiket": "sütiket", "sutikhez": "sütikhez", "sutit": "sütit",
    "suto": "sütő", "szabaly": "szabály", "szabalyok": "szabályok", "szabalyon": "szabályon", "szallas": "szállás",
    "szallasdijbol": "szállásdíjból", "szallashelyen": "szálláshelyen / szálláshelyén", "szamjegy": "számjegy",
    "szamodra": "számodra", "szamozott": "számozott", "szandekosan": "szándékosan", "szazalek": "százalék",
    "szemelyes": "személyes", "szemelyre": "személyre", "szemenek": "szemének", "szol": "szól",
    "szolgaltatasa": "szolgáltatása", "szolgaltatast": "szolgáltatást", "szolgaltatunk": "szolgáltatunk",
    "szoljon": "szóljon", "szomszedban": "szomszédban", "szovegbe": "szövegbe", "szovegben": "szövegben",
    "szovege": "szövege", "szovegeben": "szövegében", "szovegek": "szövegek", "szoveget": "szöveget / szövegét",
    "szoveggel": "szöveggel", "szovegmezok": "szövegmezők", "szukseg": "szükség", "szuksegesek": "szükségesek",
    "tagas": "tágas", "talalhato": "található", "talalsz": "találsz", "tamogatja": "támogatja",
    "tarolhatjatok": "tárolhatjátok", "tartozkodas": "tartózkodás",
    "taviranyitos": "távirányítós", "taviranyitoval": "távirányítóval", "tavolsagok": "távolságok",
    "telefonszam": "telefonszám", "telen": "télen / telén", "terkep": "térkép", "terulet": "terület",
    "tetel": "tétel", "tietek": "tiétek", "toltodik": "töltődik", "torli": "törli",
    "torlodnek": "törlődnek", "tortenik": "történik", "tudnivalo": "tudnivaló", "tuzijatek": "tűzijáték",
    "udvarbol": "udvarból", "ugras": "ugrás", "ugyanigy": "ugyanígy", "ujepitesu": "új építésű / újépítésű",
    "ujrarender": "újrarender", "unnep": "ünnep", "unnepek": "ünnepek", "unnepi": "ünnepi", "urlapok": "űrlapok",
    "utja": "útja", "utoirat": "utóirat", "uzemelnek": "üzemelnek", "uzemelteto": "üzemeltető", "valaszd": "válaszd",
    "valaszolunk": "válaszolunk", "valo": "való", "valtozat": "változat", "valtozatlanul": "változatlanul",
    "valtozo": "változó", "varnotok": "várnotok", "vasalo": "vasaló", "vegeig": "végéig", "vegleges": "végleges",
    "velemenyek": "vélemények", "vendegek": "vendégek", "vendegeket": "vendégeket",
    "vendegertekelesek": "vendégértékelések", "vendeglo": "vendéglő", "veszit": "veszít",
    "vezerelheto": "vezérelhető", "vezerlopaneleken": "vezérlőpaneleken", "videolejatszast": "videolejátszást",
    "visszaellenorizhesse": "visszaellenőrizhesse", "visszaigazolas": "visszaigazolás",
    "visszaolvasasa": "visszaolvasása", "vitorlas": "vitorlás", "vizpartig": "vízpartig", "zaro": "záró",
    "zart": "zárt", "zuhanyzoval": "zuhanyzóval",
    # adatvedelem and kornyek are also typical page paths ('az /adatvedelem/ lap'). They stay in the dictionary:
    # the EKEZETMASZK1018 path mask covers a /path after a space, and in running prose they are still errors.
    "adatvedelem": "adatvédelem", "kornyek": "környék",
    # Not from the collected copy, but forms that went through the gate in the measurement above: 'kulcsszef' (the
    # copy only has 'kulcsszéffel') and 'olcso'. Neither is a real word without its accents.
    "kulcsszef": "kulcsszéf", "olcso": "olcsó",
}

# GATEHOMOGLIF816 (2026-08-16, Marveen merese): 33 cirill homoglifa ult a
# memoria-sorokban es kartya-cimekben -- olvasva lathatatlan, de a grep/FTS
# nema nulla-talalatot ad, ami hianyzo emleknek latszik, nem serult adatnak.
# A szabaly a VEGYES SZORA vonatkozik (egy szon belul latin ES nem-latin betu),
# nem a cirill puszta jelenletere -- egy szandekosan idegen nyelvu idezet
# tiszta nem-latin szavai atmennek. Unicode-tudatos tokenizalas kell: a WORD
# regex latin-only, egy homoglifas szot darabokra vagna.
UWORD = re.compile(r"[^\W\d_]+", re.UNICODE)

# HOMOGLYPHMICRO924 (upstream babbc71a): az "irasrendszer" itt a Unicode-nev ELSO SZAVA, es ez nehany jelnel nem
# irasrendszer, hanem a jel neve. A "40 us" (MICRO SIGN), a "100 m2" es "5 cm3" (SUPERSCRIPT TWO/THREE) es a "H2O"
# (SUBSCRIPT TWO) VEGYES SZOKENT blokkolt, mert a felso/also indexes szamjegy nem \d, igy az UWORD a szoba veszi. Ezek
# mertekegyseg- es kepletjelolesek, egyik sem alcaz latin betut. A lista SZANDEKOSAN explicit: a "minden nem-betu
# semleges" szabaly tul tag volna, mert a ROMAN NUMERAL ONE (U+2160) latin I-nek, a KELVIN SIGN (U+212A) latin K-nak
# latszik -- ezek maradnak fogva.
SCRIPT_NEUTRAL = frozenset(
    ["\u00b5", "\u00b2", "\u00b3", "\u00b9", "\u2070"]
    + [chr(cp) for cp in range(0x2074, 0x207A)]  # felso index 4..9
    + [chr(cp) for cp in range(0x2080, 0x208A)]  # also index 0..9
)


def _char_script(ch: str) -> str:
    import unicodedata
    if ch in SCRIPT_NEUTRAL:
        return "NEUTRAL"
    try:
        return unicodedata.name(ch).split(" ")[0]
    except ValueError:
        return "UNKNOWN"


def mixed_script_words(text: str):
    """Return [(word, bad_char, bad_char_name), ...] for words mixing LATIN
    with any other script. Pure non-Latin words (foreign quotes) pass."""
    import unicodedata
    out = []
    for word in UWORD.findall(text):
        scripts = {_char_script(ch) for ch in word} - {"NEUTRAL"}
        if "LATIN" in scripts and len(scripts) > 1:
            bad = next(ch for ch in word if _char_script(ch) not in ("LATIN", "NEUTRAL"))
            try:
                bad_name = unicodedata.name(bad)
            except ValueError:
                bad_name = "UNKNOWN"
            out.append((word, bad, f"{bad_name} (U+{ord(bad):04X})"))
    return out


# The owner rule bans typographic dashes in outgoing Hungarian copy.  Keep the
# characters explicit so HTML decoding below is covered too.
FORBIDDEN_DASHES = (("em dash", "—", "U+2014"), ("en dash", "–", "U+2013"))

# GATEPERSIST816: owner-specific NAME rules load from an UNTRACKED local file,
# not from this (public-repo) script. The generic checks (accents, em dash,
# double hyphen, mixed-script) are universal Hungarian-copy QA and ship in the
# repo; a personal-name rule names a private third party, and that must not be
# published as a side effect of persisting the gate. A missing rules file is
# NOT silent: every run appends a loud line to the gate log, because a
# protection whose absence is invisible only protects until someone touches
# the tree. File shape: {"bad_name_patterns": ["<python-regex>", ...]}
_LOCAL_RULES = os.environ.get(
    "OUTGOING_COPY_GATE_RULES",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                 "store", "outgoing-copy-gate-rules.json"),
)

_GATE_LOG = os.path.join(os.path.dirname(_LOCAL_RULES), "outgoing-copy-gate.log")


def _gate_log(message: str) -> None:
    """Append one TIMESTAMPED line to the gate log.

    The log used to carry bare text. Measured 2026-09-05: 8005 lines, four
    distinct messages, and one of them recorded a FAIL-OPEN pass-through on the
    Telegram branch -- a message that went out unaudited -- with no way to tell
    which day it happened on, let alone which message it was. A gate log whose
    entries cannot be placed in time cannot be used to check anything. Local
    time with the offset, never UTC.
    """
    try:
        from datetime import datetime
        stamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(_GATE_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message.rstrip()}\n")
    except OSError:
        pass


# CLCOPYGATEHIANY902 (owner decision, TG 14442) MERGED WITH GATEPERSIST816/3
# (PDB install, 2026-08-19). Two independent fixes to the same question, from
# opposite directions, so the merged policy has FIVE states:
#
#   "ok"         -> patterns loaded, the name check enforces as before.
#   "sanctioned" -> the file EXISTS and explicitly says "no_name_rule": true.
#                   Reported by the PDB install, 2026-08-19: their rules file
#                   was lost with no backup, and the owner decided not to
#                   reconstruct it. That is a taken decision, not a loss, and
#                   an install that has made it should not be nagged about it
#                   on every send. So this state is silent
#                   EVERYWHERE: no log line, no systemMessage, no block. Note
#                   the early return BELOW, which deliberately skips the
#                   logging tail. An ordinary empty list WITHOUT the flag does
#                   NOT reach this state, so a file emptied by accident can
#                   never masquerade as the sanctioned one.
#   "empty"      -> no patterns and no flag: the check is OFF.
#   "missing"    -> file absent, which is EVERY fresh customer install, since
#                   the file names a private person and is deliberately not
#                   shipped. Both of these fail-OPEN with a LOUD, user-visible
#                   warning on every send: the old fail-closed email path left
#                   a paying customer unable to send mail at all (Nova,
#                   2026-09-02).
#   "invalid"    -> the file EXISTS but cannot be used (bad JSON, wrong schema,
#                   uncompilable regex). Somebody TRIED to configure it and got
#                   it wrong: the email path stays fail-CLOSED until repaired.
#
# WHY THE MERGE IS NOT A CHOICE BETWEEN THE TWO: without the sanctioned state,
# a live rules file carrying the flag with an empty pattern list reads as
# "empty", and a loud warning gets stamped on EVERY outgoing letter, customer
# mail included. Without the missing/empty/invalid split, a fresh install with
# no file at all cannot send mail at all. Each half is wrong exactly where the
# other one is right, which is why both are here.
RULES_OK = "ok"
RULES_SANCTIONED = "sanctioned"
RULES_EMPTY = "empty"
RULES_MISSING = "missing"
RULES_INVALID = "invalid"

# The states where the name check does not run AND that is not a taken
# decision: the ones that must stay loud.
RULES_LOUD = (RULES_MISSING, RULES_EMPTY, RULES_INVALID)


def load_bad_name():
    """Return (compiled_regex_or_None, state) -- see the RULES_* names above."""
    try:
        with open(_LOCAL_RULES, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            state = RULES_INVALID
        else:
            pats = data.get("bad_name_patterns") or []
            if not isinstance(pats, list) or not all(isinstance(x, str) for x in pats):
                state = RULES_INVALID
            elif pats:
                return (re.compile("|".join(pats)), RULES_OK)
            elif data.get("no_name_rule") is True or data.get("name_check_disabled") is True:
                # Returns HERE, before the logging tail, on purpose: a taken
                # decision must not write a "the protection is gone" line into
                # the ledger on every single run. An alarm that can never be
                # answered is the one people learn to ignore -- it was drowning
                # the real signals in the same log (571 lines here by
                # 2026-09-04, CLCOPYGATEDONTES904).
                # TWO spellings of the SAME decision are accepted, because two
                # installs each documented one before the branches met:
                #   {"no_name_rule": true, "no_name_rule_reason": "why"}
                #     (GATEPERSIST816/3, the PDB install)
                #   {"bad_name_patterns": [], "name_check_disabled": true,
                #    "decided_at": "2026-09-04", "note": "why"}
                #     (CLCOPYGATEDONTES904)
                # Dropping either spelling would silently turn that install's
                # recorded decision back into a loud "empty".
                return (None, RULES_SANCTIONED)
            else:
                state = RULES_EMPTY
    except FileNotFoundError:
        state = RULES_MISSING
    except Exception:
        # Present but unusable: unreadable (permissions), unparseable JSON,
        # or an uncompilable pattern. All of these mean someone TRIED to
        # configure the rule and failed -- that must stay loud AND closed.
        state = RULES_INVALID
    # ONE logging tail for EVERY loud state (Marveen review on #1156: the
    # first cut logged only the exception branches, so empty/schema-invalid
    # left no log line while missing did -- same event class, inconsistent
    # ledger).
    _gate_log(f"outgoing-copy-gate: NEV-SZABALY {state.upper()} ({_LOCAL_RULES}) -- "
              "a nev-ellenorzes NEM fut.")
    return (None, state)


def _name_correction() -> str:
    try:
        with open(_LOCAL_RULES, encoding="utf-8") as fh:
            corr = json.load(fh).get("correction") or ""
        return (" " + corr) if corr else ""
    except Exception:
        return ""


BAD_NAME, RULES_STATE = load_bad_name()
ACCENTED = set("áéíóöőúüűÁÉÍÓÖŐÚÜŰ")
TAG = re.compile(r"<[^>]+>")

# GATEKOTOJEL817 + GATEHYPH816 (2026-08-19 este, ket hamis pozitiv elo
# gazda-beszelgetesben, ot perc alatt): a kapu nem tett kulonbseget PROZA es
# AZONOSITO kozott. (1) `Drive-ot` -- az idegen tulajdonnevhez a magyar
# toldalek kotojellel kapcsolodik (ez a HELYES iras), de a betu-only WORD
# tokenizalo a kotojelnel vagott, es a maradek `ot` darabot onallo magyar
# szonak nezte (ot -> öt). (2) `Video atalakitas` -- egy Drive-mappa NEVE a
# szovegben: mondatkozi nagybetus szo, azonosito, nem proza. A javitas a
# TOKENIZALAS, nem a szotar (szo-kivetel a valodi hibakat is atengedne):
#   - kotojeles alaknal a TELJES szoalak vizsgalando (a `drive-ot` egeszkent
#     nincs a szotarban -> atmegy; az onallo `ot` prozaban tovabbra is bukik);
#   - a MONDATKOZI nagybetus szo azonosito/tulajdonnev -> kimarad; mondat
#     elejen (. ! ? : ujsor vagy lista-jel utan) a nagybetu normal proza,
#     ott tovabbra is vizsgaljuk.
HYPHEN_WORD = re.compile(r"[a-záéíóöőúüűA-ZÁÉÍÓÖŐÚÜŰ]+(?:-[a-záéíóöőúüűA-ZÁÉÍÓÖŐÚÜŰ]+)*")


def _at_sentence_start(text: str, idx: int) -> bool:
    i = idx - 1
    while i >= 0 and text[i] in " \t\"'([{":
        i -= 1
    if i < 0:
        return True
    ch = text[i]
    if ch in ".!?:\n":
        return True
    if ch in "-*•":
        j = i - 1
        while j >= 0 and text[j] in " \t":
            j -= 1
        return j < 0 or text[j] == "\n"
    return False


def accent_check_tokens(prose: str):
    """(lowercase alak, kezdo-pozicio) parok az ekezet-vizsgalathoz."""
    out = []
    for m in HYPHEN_WORD.finditer(prose):
        tok = m.group(0)
        # DIGIT-HYPHEN SUFFIX (429-es, 403-as, 2026-os, 3420-as). HYPHEN_WORD only
        # admits LETTERS around the hyphen, so a Hungarian suffix attached to a
        # NUMBER is seen as a standalone word -- and "es" is then read as the
        # accent-stripped "és". These are not prose words; they carry no accent.
        # (2026-08-21: the gate blocked a correct message reading "429-es vagy
        # 403-as". GATEKOTOJEL817 covered letter-hyphen-letter forms, not this one.)
        if m.start() >= 2 and prose[m.start() - 1] == "-" and prose[m.start() - 2].isdigit():
            continue
        if "-" not in tok and tok[0].isupper() and not _at_sentence_start(prose, m.start()):
            continue
        out.append((tok.lower(), m.start()))
    return out


def _hit_context(prose: str, pos: int, length: int) -> str:
    """A talalat elotti/utani 3-3 szo + karakter-pozicio (GATEHYPH816 (B):
    elo beszelgetes kozben ne kelljen greppelni, melyik szorol van szo)."""
    before = prose[:pos].split()[-3:]
    token = prose[pos:pos + length]
    after = prose[pos + length:].split()[:3]
    frag = " ".join(before + [token] + after)
    return f'"...{frag}..." @{pos}'


# Technikai tokenek maszkolasa AZ EKEZET-ELLENORZES ELOTT. Merve 2026-08-13, a
# +48-as bovites negativ kontrolljan: egy HIBATLANUL ekezetezett eles level
# fennakadt a `video_view` esemenynevben levo "video"-n. A szobonto az aláhúzást
# hataroljelnek veszi, tehat minden snake_case azonosito, fajlnev, URL-slug es
# domain beszallit egy "magyar szot", ami ott ekezet nelkul HELYES. Ugyanaz az
# osztaly, mint a 2026-08-11-i `level` fajlnev-talalat. Ugyanide tartozik a
# 2026-08-25-i talalat ugyanebbol az osztalybol: a TULAJDONNEV + kotojeles toldalek
# ("Chrome-ot", "Drive-ra") is puszta "ot"/"ra" tokenre esik szet, es az "ot" benne
# van a szotarban (-> "ot"). Ezert a maszk a nagybetuvel kezdodo tovet is elfogadja,
# de CSAK ha a kotojel utan legfeljebb negy kisbetu all: egy valodi magyar osszetett
# szo masodik tagja ("Telegram-hidam") tipikusan hosszabb, tehat az tovabbra is fennakad.
# 2026-08-26-i talalat, HARMADIK a sorozatban, de mas alosztaly: nem toldalek volt,
# hanem egy ANGOL SZO, ami veletlenul egy ekezetes magyar szo ekezetlen alakja.
# A "level 1" (autonomia-szint) alak a szotarban "level -> level" javaslatot valtott ki,
# es ketszer blokkolta a reggeli uzenetet. A szotarbol NEM vettem ki a bejegyzest, mert
# a magyar "level" tenyleg hibas alak; a maszk csak a SZAM ELOTTI angol hasznalatot vagja ki.
# Ami igy is fennakad: az idezojelbe tett, magarol beszelo "level" szo. Arra a kod-span
# (backtick) a kijarat, az ugyanis maszkolva van.
# 2026-08-27, NEGYEDIK a sorozatban: a KOTOJELES KISBETUS AZONOSITO. Az utemezett
# feladatok es skillek nevei (folyamatos-ellenorzes, mentes-lemaradas-ellenorzes,
# kontextus-atadas-visszaallitas) ekezet nelkuli magyar szavakbol allnak, mert fajl- es
# mappanevek. A szotar ezeket valodi talalatnak latja, pedig AZONOSITOK, nem proza --
# a gazdanak pont igy, betuhiven kell leirni oket, kulonben nem talalja meg a gepen.
# A maszk ezert kivagja a legalabb ket tagu, csupa kisbetus, kotojellel osszekotott
# alakokat. Egy valodi magyar mondat nem all ilyen alakbol, es a NAGYBETUS kezdetu
# osszetetelekre (Chrome-ot) mar van kulon, szigorubb szabaly folotte.
# 2026-08-24-i talalat: a szambol es kotojeles magyar toldalekbol allo alak
# ("8:09-es", "2-es", "17:06-kor") a szobontonal puszta "es"/"kor" tokenne esik
# szet, amit a szotar hibanak lat -- pedig ott toldalek, nem szo. A javitas nem a szotarbol
# vesz ki (az elrontana a valodi talalatokat is), hanem a technikai regiokat
# vagja ki a vizsgalt szovegbol. A gondolatjel-ellenorzes NEM ezen fut (az a nyers
# szovegen mer), a nev-ellenorzes pedig a SAJAT, szukebb maszkjan -- lasd NAME_MASK.
#
# GATENEVSTRIP921 (kulso bejelentes 2026-09-21, sajat visszameres 2026-09-22):
# KET SZABALY, AMI UGYANARRA A SZOVEGRE NEZ, NEM UGYANAZT A MASZKOT AKARJA.
# A bejelentes az volt, hogy a nev-szabaly a NYERS szovegen fut, ezert URL-ben,
# kod-spanban vagy utvonalban allo nevalak is megallitja a teljes uzenetet
# (harom ilyen hamis pozitiv reprodukalva). A kezenfekvo javitas -- a nev-szabaly
# athelyezese a strip_technical UTANRA -- viszont LYUKAT NYIT, mert az alabbi ket
# alternativa a NEVET magat vagja ki a szovegbol a toldalekaval egyutt:
#   "Nev-val" / "Nev-nak" / "Nev-fele"  ->  a tulajdonnev+toldalek ag eszi meg
# Merve a sajat mintankon (kartya-komment 17237): harom prozai alakbol harom tunt
# el a strip utan, vagyis csendben ATMENTEK volna. Ezert a nev-szabaly a kozos,
# egyertelmuen technikai regiokat kapja meg maszknak (_TECH_COMMON), a toldalek-
# es azonosito-agakat NEM. Ezek az agak a token- es ekezet-vizsgalatnak kellenek,
# ahol epp az a dolguk, hogy a toldalek-toredeket ne nezzek onallo szonak.
#
# A KET MASZK EGY FORRASBOL EPUL, hogy ne drifteljenek szet: ha valaki uj
# technikai regiot vesz fel, a _TECH_COMMON-ba irva MINDKET ellenorzes latja.
_TECH_COMMON = r"""
        https?://\S+                # URL
      | [\w.+-]+@[\w-]+\.[\w.]+     # email
      | `[^`]*`                     # kod-span
      | \b\w+(?:_\w+)+\b            # snake_case azonosito
      | \b\w+\.[A-Za-z]{2,10}(?:-[a-záéíóöőúüű]{1,4})?\b   # fajlnev / domain, magyar toldalekkal (video.mp4, marveen.io, Mail.app-ot)
      | \b[\w-]*/[\w/-]+            # utvonal / slug
"""
# CSAK a token-/ekezet-vizsgalat vaghatja ki ezeket: mindharom ag kepes egy
# tulajdonnevet a toldalekaval egyutt elnyelni, ezert a nev-szabaly nem kapja meg.
# A shortened hex identifier keeps its Hungarian suffix across an ellipsis.
# Require >=4 hex characters, an explicit ellipsis, and an attached hyphen:
# a standalone "es" (even after an identifier) must remain a prose token.
_TECH_SUFFIXED = r"""
        (?<![\w.])[A-Fa-f0-9]{4,}(?:\.{3}|…)-(?:es|as)(?![\w-])
      | \d+(?:[.:,]\d+)*-[^\W\d_]+   # szam + magyar toldalek (8:09-es, 2-es, 17:06-kor)
      | \b[A-ZÁÉÍÓÖŐÚÜŰ][^\W\d_]*-[a-záéíóöőúüű]{1,4}\b   # tulajdonnev + toldalek (Chrome-ot, Drive-ra)
      | \blevel\s+\d+\b            # angol "level 1" (autonomia-szint, log-szint)
      | (?:(?<![\w-])--)?\b[a-z]+(?:-[a-z]+){1,4}\b    # kotojeles kisbetus azonosito (feladat- es skill-nevek), --flag alakban is
"""
# FLAGDASH1030 (upstream 7e9782cf): a kotojeles azonosito-ag korabban a '--select-release-current' flagnek csak a szo
# reszet vagta ki, a vezeto '--' ket szokoz kozott maradt, es a dupla-kotojel szabaly proza-gondolatjelnek szamolta.
# Az opcionalis '--' elotag az egesz flaget viszi. A valodi ' -- ' prozaban tovabbra is szamit: az elotag utan
# kozvetlenul betu kell, a proza-gondolatjel utan szokoz van.
TECHNICAL = re.compile(_TECH_COMMON + "|" + _TECH_SUFFIXED, re.X)
NAME_MASK = re.compile(_TECH_COMMON, re.X)

# EKEZETMASZK1018 (2026-10-18): all three review rounds of the EKEZET1018 dictionary extension found the same class:
# a dictionary word matched in a place that is not Hungarian prose. TECHNICAL does not cover an #anchor, a --flag or a
# /path that starts after a space (it does cover 'word/word': \b[\w-]*/[\w/-]+ needs a word boundary before the
# opening '/'). These three branches are a SEPARATE mask that only the accent check uses (hits, ratio, context),
# applied after TECHNICAL. TECHNICAL is also used by the homoglyph rule (mixed_script_words) and the double-hyphen
# count, and there the mask would hide a Cyrillic homoglyph inside '#fоglalas' or '--jоvahagyott'. The name-rule
# mask (NAME_MASK) does not get them either.
# The path branch does NOT swallow a sentence-final period ('/kapcsolat.'): otherwise the next sentence's capitalised
# first word would look like a mid-sentence capital, and the accent check would skip it.
# COPYMASK1019 (2026-10-20, measured on the #2065 corpus check): two more shapes that only carry identifiers, also
# accent-only for the same reason: a directory with its trailing slash ('kepek/', 'fizetes/*'; a word/word path is
# already in TECHNICAL) and a key=value pair ('funkcio=ertekeles'). NOT masked, deliberately:
#   - a QUOTED word or phrase. A quoted single token looked like a literal value ('lakas'), but it is also exactly how
#     a button label or a value to type is written for a customer ('kattints a "reszletek" gombra'), so a mask would
#     let accentless customer copy through (Samu, #2085 review). A quoted identifier goes in backticks;
#   - a bare table or page name in a sentence ('a velemenyek táblán'), which has no shape that tells it from prose:
#     backticks again.
_ACCENT_ONLY = r"""
        (?<![\w&])\#[\w-]+                   # anchor / hashtag (#reszletek): an identifier, not prose
      | (?<![\w-])--[A-Za-z][\w-]*            # command-line flag (--jovahagyott)
      | (?<![\w/])/[\w-]+(?:[./][\w-]+)*/?    # a path that starts with / after a space (az /adatvedelem/ lap), no final period
      | (?<![\w/.-])[\w.-]+/\**(?=[\s,;:.!?)\]'"]|$)   # a directory written with its trailing slash (kepek/, fizetes/*)
      | (?<![\w.-])[A-Za-z_][\w.-]*=[^\s,;)\]'"]+   # key=value (funkcio=ertekeles), a parameter, not prose
"""
ACCENT_MASK = re.compile(_ACCENT_ONLY, re.X)


def strip_technical(text: str) -> str:
    return TECHNICAL.sub(" ", text)


# STRIPDASH1109 (upstream e08d720f): a ' -- ' szamlalas a strip_technical() kimeneten futott, az pedig SZOKOZT tesz a
# technikai regio helyere, ezert maga a kivagas epitette fel a mintat: '--accept_routes=true' (snake_case ag) ' -- =true'
# lett, es proza-gondolatjelkent bukott, pedig a nyers szovegben nincs ' -- '. A szamlalas mostantol olyan masolaton fut,
# ahol minden regio egy szokoz- es kotojelmentes helyorzo, igy a kivagas csak elvehet gondolatjelet, letrehozni nem.
# A valodi ' -- ' egy regio mellett megtartja a sajat szokozeit, es tovabbra is szamit ('ez -- `kod`').
TECH_PLACEHOLDER = "_"


def strip_technical_closed(text: str) -> str:
    return TECHNICAL.sub(TECH_PLACEHOLDER, text)


def strip_for_accent(prose: str) -> str:
    """The accent check's own ADDITIONAL mask, applied to the strip_technical output (EKEZETMASZK1018)."""
    return ACCENT_MASK.sub(" ", prose)


def strip_for_name(text: str) -> str:
    """A nev-ellenorzes maszkja: csak az egyertelmuen technikai regiok esnek ki."""
    return NAME_MASK.sub(" ", text)


def is_hungarian(text: str) -> bool:
    low = text.lower()
    # Even this short article/conjunction fragment is Hungarian prose, not an
    # identifier suffix. It otherwise falls below the three-marker threshold.
    return (sum(1 for m in HU_MARKERS if m in low) >= 3
            or re.search(r"\baz\s+es\s+a\b", low) is not None)


# GATETG816 (2026-08-16, Marveen merese): az is_hungarian() funkcionalis szavakra
# szur, ezert a TOMOR, tenykozol, felsorolasos magyar uzenet (pont a fo agens
# Telegram-stilusa) nem eri el a 3 markert, es az ekezet-vizsgalat el sem indul --
# a mai napindito elso bekezdese ekezetlenul atment. A nyelv-detektor ezert nem
# EGYEDULI kapu tobbe: egyetlen olyan szotar-talalat, ami magyarul ekezet nelkul
# NEM letezik ES angol/technikai szokent sem ertelmezheto, onmagaban eleg ok a
# vizsgalatra. Az alabbi kizaras CSAK a triggerre vonatkozik: ha a szoveg mas
# uton magyarnak bizonyult, ezek a talalatok is jelentesre kerulnek -- de egyedul
# nem ranthatnak be egy angol szoveget az auditba ("the all-new level editor").
# KNOWN LIMIT (KAPUES1018, 2026-10-18): so a Hungarian text with fewer than 3 DISTINCT HU_MARKERS words (substring
# matches count), and without any ACCENTLESS word that is NOT listed here, can pass with an accentless "es", whatever
# its length (the >200-letter ratio branch also runs only once the audit has started). Measured size: of 267
# Hungarian sentences with one "és" -> "es" mutation, the gate lets 206 through. Measured and pinned case:
# "Örülünk, hogy jöttök, es nem sietünk." -> exit 0 (2 markers: hogy, nem); with "már" (a 3rd marker) the same text
# is blocked. A third trigger built on the text's other accented words gave false blocks in three review rounds on
# non-Hungarian customer text that quotes a Hungarian dish or place name (e.g. Spanish "Probamos pörkölt y
# kürtőskalács, es delicioso."), so it was rejected: the gate's main value is that it does not wrongly block a
# customer message. Test: scripts/__tests__/outgoing-copy-gate-kapues1018-hatar.test.py.
AMBIGUOUS_TRIGGER = {
    "es", "ar", "arak", "ev", "evi", "ot", "uj", "ujat", "het", "ora", "mas",
    "all", "level", "video", "netto",
    # EKEZET1018: foreign-looking forms that must not pull a non-Hungarian text into the audit on their own; in a
    # text that is Hungarian by other evidence they are still reported.
    "privat", "zart", "molo", "seta", "mutans", "kanape",
}

# EKEZET1018: the NAME class exempts only when CAPITALISED. In lowercase these are frequent Hungarian words (soran,
# kozel, hany, keri, levo), and in a short accentless message they are exactly the evidence; only the capitalised
# sentence-start form can be a foreign name ("Soran will ship", "Kozel agreed"). accent_check_tokens() already drops
# a capitalised word in mid-sentence, so a capitalised token here is always sentence-initial.
AMBIGUOUS_NAME_TRIGGER = {
    # sentence-start English words / names (Keri, Iger, Soran) that would pull an English text in on their own
    "keri", "iger", "soran",
    # the same class (sentence-start foreign names / brands: Hany, Korben, Kozel, Barki, Levo)
    "hany", "korben", "kozel", "barki", "levo",
}


def accentless_evidence(tok_pos, text):
    """The trigger evidence, from (lowercase form, position) pairs, using the original case in `text`."""
    return {w for w, p in tok_pos
            if w in ACCENTLESS and w not in AMBIGUOUS_TRIGGER
            and not (w in AMBIGUOUS_NAME_TRIGGER and text[p:p + 1].isupper())}


# The upstream snapshot delegates these collectors to email_extract.py. That
# module is not part of this approved package, so retain the worktree's
# equivalent local collectors instead of introducing an unrelated dependency.
def collect_bash_body(cmd: str):
    """Return (text, unreadable_reason). text is empty when nothing was recovered."""
    parts = []
    for match in re.finditer(r"--(?:body|subject)[= ]+(\"([^\"]*)\"|'([^']*)'|(\S+))", cmd):
        value = match.group(2) or match.group(3) or match.group(4) or ""
        if re.search(r"\$\(|\`|\$\{?\w", value):
            return (
                "\n".join(parts),
                "a --body shell-behelyettesitest tartalmaz, amit a hook nem old fel "
                f"({value[:60]}...) -- igy a parancs szoveget vizsgalnam, nem a levelet",
            )
        parts.append(value)
    for match in re.finditer(r"<<-?\s*'?(\w+)'?\n(.*?)\n\1", cmd, re.S):
        parts.append(match.group(2))
    redirect = re.search(r"(?<!<)<(?!<)\s*([^\s|;&<>]+)", cmd)
    if redirect:
        raw = redirect.group(1)
        path = os.path.expandvars(os.path.expanduser(raw))
        if "$" in path:
            return ("\n".join(parts), f"a torzs egy fel nem oldhato utvonalrol jon ({raw})")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                parts.append(fh.read())
        except OSError as exc:
            return ("\n".join(parts), f"a torzs-fajl nem olvashato ({path}: {exc})")
    if not parts and re.search(r"\|\s*(python3?|node|tsx)?[^|]*send", cmd):
        return ("", "a torzs egy pipe-bol jon, a hook nem latja")
    return ("\n".join(parts), None)


def collect_mcp_body(tool_input: dict):
    fields = ("body", "text", "html", "htmlBody", "message", "subject", "content")
    return "\n".join(str(tool_input[field]) for field in fields if tool_input.get(field))


# --- Telegram reply (GATETG816) ---------------------------------------------
# The reply tool sends MarkdownV2, where every special char arrives escaped
# (\. \( \) \-). Those backslashes sit inside the prose the audit reads, and
# they can split technical tokens or glue fragments in ways the email path
# never sees. Un-escape (backslash before a non-word char) BEFORE auditing --
# this is analysis-only, the outgoing payload is untouched.
MDV2_ESCAPE = re.compile(r"\\([^\w\s])")


# --- Human-facing HTTP channels via curl (GATEHTTP924) ------------------------
# The Bash arm recognised exactly one kind of send: email. Every other outbound
# path that a human reads, and that an agent can reach with a plain curl, left
# the machine with no audit at all. Measured 2026-09-24 with a synthetic em-dash
# payload: a comment to the community API, a post to Discord's REST API and a
# Telegram Bot API sendMessage all passed exit 0, while the SAME text through
# the Telegram reply tool was blocked. The gate was not failing on these paths,
# it simply had no door there.
#
# Covered here, each only when the method is a SEND (POST/PUT/PATCH or an
# implicit POST from a data flag; GET/HEAD reads pass untouched):
#   - the community agent API (api.marveen.io): feed posts, comments, mention
#     replies. Written from a vault-held base URL in practice ($B/feed/...), so
#     the path shape counts even when the host is an unresolved variable;
#   - Discord REST: .../channels/<id>/messages[/<id>];
#   - Telegram Bot API: .../bot<token>/send*|edit*.
# NOT covered, deliberately: /api/messages (inter-agent and federation). That
# channel is agent-to-agent, its traffic is written without accents, and it
# already has its own homoglyph-only gate (INTERAGENTHOMOGLIF923) above.
#
# Failure direction follows the email branch, not the Telegram one: a body the
# hook cannot read BLOCKS. These are deferrable writes (a community post is
# even queued for owner approval), and a public text that skipped the audit
# costs more than a retry with a readable body. An INTERNAL error still passes
# loudly, like every channel arm.
_HTTP_CHANNEL_TARGETS = (
    ("marveen.io", re.compile(
        r"^((https?://)?([^/\s]*\.)?api\.marveen\.io|\$\{?\w+\}?)(/[^\s]*)?"
        r"/(feed/posts(/[^/\s]+/comments)?|mentions/[^/\s]+/reply)/?(\?\S*)?$", re.I)),
    ("Discord API", re.compile(
        r"^(https?://)?([^/\s]*\.)?discord(app)?\.com/api(/v\d+)?/channels/[^/\s]+/messages(/[^/\s]+)?/?(\?\S*)?$",
        re.I)),
    ("Telegram API", re.compile(
        r"^(https?://)?api\.telegram\.org/bot[^/\s]+/(send|edit)\w*/?(\?\S*)?$", re.I)),
)
# Only the prose a human reads. ids, chat ids, parse modes and urls stay out:
# auditing them would block on tokens nobody reads as text.
_HTTP_TEXT_FIELDS = ("title", "content", "text", "caption", "message")


def _http_channel_segment(cmd: str):
    """(label, tokens) of the first curl segment that SENDS to a covered
    human-facing HTTP channel, or (None, None)."""
    try:
        segments = _segments_tokens(cmd)
    except ValueError:
        return None, None
    for toks in segments:
        while toks and _ENV_ASSIGN.match(toks[0]):
            toks = toks[1:]
        if not toks or not _CURLISH.match(_basename(toks[0])):
            continue
        rest = toks[1:]
        for label, target in _HTTP_CHANNEL_TARGETS:
            if any(target.match(t) for t in rest):
                if _curl_resend_verdict(rest) == "read":
                    return None, None  # a GET of the feed: nothing is sent
                return label, toks
    return None, None


def _http_channel_text(raw: str) -> str:
    """The human-read prose of a channel body: JSON object fields, or the same
    fields form-encoded (Telegram accepts both). Unknown shape: the raw body."""
    try:
        obj = json.loads(raw)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        return "\n".join(str(obj[f]) for f in _HTTP_TEXT_FIELDS
                         if isinstance(obj.get(f), str) and obj.get(f))
    if obj is None:
        from urllib.parse import parse_qs
        form = parse_qs(raw, keep_blank_values=False)
        got = [v for f in _HTTP_TEXT_FIELDS for v in form.get(f, [])]
        if got:
            return "\n".join(got)
    return raw


def http_channel_gate(cmd: str) -> None:
    """Exit 2 on a copy problem or an unreadable body, exit 0 when clean.
    RETURNS (does not exit) when the command is not a covered HTTP send, so
    the caller's other arms still run."""
    label, toks = _http_channel_segment(cmd)
    if label is None:
        return
    try:
        raw, unreadable = _curl_payload_raw(cmd, toks, all_flags=True)
        if unreadable:
            sys.stderr.write(
                f"KIMENO-SZOVEG KAPU ({label}): TILTVA, mert a kimeno szoveget nem tudtam "
                f"megvizsgalni.\nOk: {unreadable}.\n\n"
                "Ez szandekosan fail-closed: egy vizsgalhatatlan kuldes pont a kaput utne ki.\n"
                "Vizsgalhato alak: idezett heredoc (--data-binary @- <<'JSON'), "
                "@/abszolut/ut.json, vagy inline -d '...' behelyettesites nelkul.\n")
            sys.exit(2)
        if raw is None:
            sys.exit(0)  # a bare POST: nothing of ours is being sent
        text = _http_channel_text(raw)
        if not text.strip():
            sys.exit(0)
        problems = audit(text)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- deliberate blanket: fail-open-loud
        warn = f"outgoing-copy-gate: HTTP-csatorna-ag ({label}) belso hiba, FAIL-OPEN atengedes: {exc!r}"
        sys.stderr.write(warn + "\n")
        _gate_log(warn)
        sys.exit(0)
    if problems:
        sys.stderr.write(
            f"KIMENO-SZOVEG KAPU ({label}): TILTVA, az uzenet nem mehet ki igy.\n\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\nJavitsd a szoveget es kuldd ujra.\n")
        sys.exit(2)
    sys.exit(0)


def collect_telegram_body(tool_input: dict) -> str:
    fields = ("text", "caption", "message")
    got = [str(tool_input[f]) for f in fields if tool_input.get(f)]
    return MDV2_ESCAPE.sub(r"\1", "\n".join(got))


def telegram_gate(tool_input: dict) -> None:
    """Audit a Telegram reply. FAIL-OPEN on any internal error (exit 0 + loud
    log): email is deferrable, but Telegram is the owner's ONLY supervision
    channel -- a gate crash that silences it costs more than a slipped accent.
    A FOUND problem still blocks (exit 2): that is the gate's whole point."""
    try:
        text = collect_telegram_body(tool_input)
        if not text.strip():
            sys.exit(0)  # files-only reply or empty text: nothing to audit
        # GATECOPY827: a masolhato kodblokk CSAK markdownv2 modban lesz
        # kodblokk. A reply tool `format` parametere alapertelmezesben "text",
        # es plain textben a Telegram nem parsolja a harom backtickot, tehat
        # nincs copy gomb -- a szoveg nyersen, a backslash-escape-ekkel egyutt
        # jelenik meg. Ez 2026-08-17 ota OTSZOR ment ki igy (a memoriaban
        # feedback_telegram_codeblock_needs_markdownv2, plusz a kotelezove tett
        # telegram-copy-gomb skill), es egyik alkalommal sem tudashiany volt,
        # hanem kihagyott lepes. Ezert innentol gepi kapu allitja meg, nem
        # emlekezet. A vizsgalat a NYERS szovegen fut, mert a fence-t a
        # MDV2_ESCAPE feloldas nem erinti.
        raw = "\n".join(str(tool_input[f]) for f in ("text", "caption", "message")
                        if tool_input.get(f))
        if "```" in raw and str(tool_input.get("format", "")).lower() != "markdownv2":
            sys.stderr.write(
                "KIMENO-SZOVEG KAPU (Telegram): TILTVA, a kodblokk nem lenne masolhato.\n\n"
                "  - A szoveg harom backtickes kodblokkot tartalmaz, de a hivasban\n"
                "    format=\"" + str(tool_input.get("format") or "text") + "\". Plain textben a Telegram nem ad copy gombot,\n"
                "    es a MarkdownV2 escape-ek (\\. \\- \\() nyersen latszanak.\n\n"
                "Kuldd ujra ugyanezt a szoveget format=\"markdownv2\"-vel. Kodblokkon\n"
                "belul csak a backtickot es a backslasht kell escapelni, a blokkon\n"
                "kivuli prozat viszont teljesen (_*[]()~`>#+-=|{}.!).\n"
            )
            sys.exit(2)
        problems = audit(text)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- deliberate blanket: fail-open path
        warn = f"outgoing-copy-gate: TELEGRAM-ag belso hiba, FAIL-OPEN atengedes: {exc!r}"
        sys.stderr.write(warn + "\n")
        _gate_log(warn)
        sys.exit(0)
    if problems:
        sys.stderr.write(
            "KIMENO-SZOVEG KAPU (Telegram): TILTVA, az uzenet nem mehet ki igy.\n\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\nJavitsd a szoveget es kuldd ujra (a MarkdownV2 escape-eket a kapu "
              "az ellenorzes elott feloldja, azok nem szamitanak hibanak).\n"
        )
        sys.exit(2)
    # GATEPERSIST816(2): a hianyzo nev-szabaly a telegram-agon fail-open marad,
    # de a figyelmeztetes ODA megy, ahol a session tenyleg latja -- a hook
    # stdout systemMessage mezoje a futo sessionben jelenik meg, nem egy
    # logfajlban, amit senki nem olvas.
    # GATEPERSIST816/3 + CLCOPYGATEDONTES904: a tudatosan felfuggesztett
    # szabaly (RULES_SANCTIONED, barmelyik flag-irassal) mar nem veszteseg,
    # nincs mit jelezni, a Telegram-ag ott csendben marad. Minden mas nem-ok
    # allapot (missing/empty/invalid) figyelmeztetest kap.
    if RULES_STATE in RULES_LOUD:
        print(json.dumps({"systemMessage":
            "outgoing-copy-gate: a NEV-SZABALY fajl hianyzik/ures "
            f"({_LOCAL_RULES}) -- a nev-ellenorzes NEM fut a kimeno uzeneteken. "
            "Potold a store/outgoing-copy-gate-rules.json-t."}))
    sys.exit(0)


def audit(text: str):
    """Return a list of human-readable problems."""
    # COPYGATEENT914 (Marveen merese, 2026-09-14, egy VALODI vevo-levelen): a
    # tag-kiszedes onmagaban megkerulheto HTML-ENTITASSAL. A `&mdash;` (es a
    # szamos `&#8212;` / hex `&#x2014;` alak) atment a kapun, a cimzettnel
    # viszont gondolatjelkent renderel -- vagyis a kapu zoldet mondott arra,
    # amit tilt. Merve mind a harom alakon.
    # A SORREND SZANDEKOS: eloszor a tageket szedjuk ki, AZUTAN dekodolunk.
    # Forditva egy szovegkent mutatott, escape-elt jelolo (`&lt;b&gt;`) valodi
    # tagge dekodolodna, es a TAG.sub kitorolne a szoveg egy darabjat.
    plain = html.unescape(TAG.sub(" ", text))
    problems = []
    for dash_name, dash, codepoint in FORBIDDEN_DASHES:
        count = plain.count(dash)
        if not count:
            continue
        problems.append(
            f"GONDOLATJEL ({dash_name}, {codepoint}) {count} helyen -- allo szabaly, soha nem mehet ki."
        )
    # GATENEVSTRIP921: a nev-szabaly a SAJAT maszkjan fut (NAME_MASK), nem a
    # nyers szovegen es nem a strip_technical kimeneten -- lasd a NAME_MASK
    # feletti indoklast. Igy a kod-spanban/URL-ben allo nevalak atmegy, a
    # toldalekos prozai alak ("Nev-val") viszont tovabbra is bukik.
    bad = BAD_NAME.search(strip_for_name(plain)) if BAD_NAME else None
    if bad:
        problems.append(
            f"HELYTELEN NEV: {bad.group(0)!r} -- a lokal nev-szabaly (store/outgoing-copy-gate-rules.json) szerint helytelen alak; a helyes irast a szabaly-fajl correction mezoje adja." + _name_correction()
        )
    prose = strip_technical(plain)
    # DUPLA KOTOJEL gondolatjel-potlokent (Szabi 2. eszrevetele, 2026-08-16): a
    # " -- " prozaban ugyanugy zavaro, mint a tiltott em dash. A PROZAN merjuk
    # (strip_technical utan), igy a kodreszletek/parancsok --flag alakjai nem
    # erintettek -- azok amugy sem " -- " alakuak (nincs szokoz a kotojelek
    # utan), de a technikai regiok kivagasa a biztos hatar.
    # STRIPDASH1109: a szamlalas a szokoz nelkuli helyorzos kivagason fut, mert a
    # szokozos kivagas maga is letre tudta hozni a " -- " mintat.
    dh = strip_technical_closed(plain).count(" -- ")
    if dh:
        problems.append(
            f"DUPLA KOTOJEL gondolatjel-potlokent {dh} helyen (' -- ') -- Szabi jelzese: "
            "ugyanugy zavaro, mint az em dash. Ird at kotojel nelkul: kettospont, zarojel, vagy uj mondat."
        )
    # 4. ellenorzes (GATEHOMOGLIF816): vegyes irasrendszeru szo. SZANDEKOSAN
    # NEM magyar-kapuzott (elteres Marveen specjetol, ervvel): az FP-vedelem
    # maga a VEGYES-szo szabaly -- egy legitim idegen idezet szavai TISZTA
    # nem-latin betusek, sosem vegyesek. A magyar-kapu itt semmit nem vedene,
    # viszont lyukat utne: egy 2-markeres, hibatlanul ekezetes magyar szoveg
    # homoglifaja atcsuszna (merve: a 'kerlek+koszonom' paros keves a
    # nyelv-detektorhoz). A konkret szot ES karaktert nevezzuk meg, mert a
    # hiba szemre lathatatlan -- enelkul a javitas talalgatas lenne.
    mixed = mixed_script_words(prose)
    if mixed:
        shown = "; ".join(f"{w!r} -- benne {name}" for w, _c, name in mixed[:5])
        more = f" (+{len(mixed) - 5} tovabbi)" if len(mixed) > 5 else ""
        problems.append(
            f"VEGYES IRASRENDSZERU SZO (homoglifa), {len(mixed)} db: {shown}{more}. "
            "Latin szoba keveredett nem-latin betu: olvasva lathatatlan, de a keresest/grepet neman eltori."
        )
    # EKEZETMASZK1018: the accent check runs on the text after the ADDITIONAL identifier mask (anchor, flag, /path);
    # the homoglyph and double-hyphen rules above still see the TECHNICAL-masked prose, unchanged.
    accent_prose = strip_for_accent(prose)
    tok_pos = accent_check_tokens(accent_prose)
    words = [w for w, _ in tok_pos]
    if is_hungarian(plain) or accentless_evidence(tok_pos, accent_prose):
        hits = sorted({w for w in words if w in ACCENTLESS})
        # Az aranyot is a prozan merjuk: a technikai tokenekben nincs ekezet, tehat
        # egy kodban gazdag, egyebkent helyes level aranyat lefele huznak.
        letters = sum(1 for ch in accent_prose if ch.isalpha())
        acc = sum(1 for ch in accent_prose if ch in ACCENTED)
        ratio = (acc / letters) if letters else 0.0
        if hits:
            first_pos = {}
            for w, p in tok_pos:
                if w in ACCENTLESS and w not in first_pos:
                    first_pos[w] = p
            shown = ", ".join(
                f"{h} -> {ACCENTLESS[h]} ({_hit_context(accent_prose, first_pos[h], len(h))})"
                for h in hits[:12]
            )
            more = f" (+{len(hits) - 12} tovabbi)" if len(hits) > 12 else ""
            problems.append(f"HIANYZO EKEZETEK, {len(hits)} szo: {shown}{more}")
        elif letters > 200 and ratio < 0.01:
            problems.append(
                f"MAGYAR SZOVEG GYAKORLATILAG EKEZET NELKUL (ekezet-arany {ratio:.3%}, {letters} betun). "
                "A szolistam nem talalt konkret talalatot, de az arany onmagaban gepi atirasra utal -- olvasd vissza."
            )
    return problems


# Outbound-shaped operations of the multiplexed manage_email tool. Everything
# else it does (search, read, labels, trash, getAttachment...) produces no text
# of ours, so the gate must not touch it -- classifying those as sends is how
# the sibling email gate once denied plain mailbox READS (MANAGEOP904).
MANAGE_EMAIL_OUTBOUND_OPS = {"send", "reply", "replyall", "forward"}

# The fields of a Gmail-connector forward that hold no text of ours (recipients and ids), and the only value shapes
# they may have for the bare-forward exemption below: a string or a list of strings. A nested object (an id given as
# {"text": ...}, a recipient as {"name": ..., "email": ...}) can carry text, so it does not count as an address.
_FORWARD_ADDRESS_FIELDS = frozenset({
    "to", "cc", "bcc", "from", "messageId", "message_id", "messageIds", "threadId", "thread_id", "id", "userId",
})


def _is_address_value(v) -> bool:
    return isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v))


# Dedicated (non-multiplexed) outbound tools: the draft tools and the Gmail
# connector's three separate send-shaped tools. Kept in step with the matcher
# this hook is registered under in settings.json.
# GMAILCONNECTOR914: the server segment is NOT always exactly "gmail" -- the
# claude.ai connector is mcp__claude_ai_Gmail__send_message, one underscore
# before Gmail, and `(^|__)gmail__` never matched it, so its sends fell through
# to sys.exit(0) with no audit (measured 2026-08-30, 2026-09-08). Anything
# ending in "gmail__<send-shaped tool>" is a send now, whatever the prefix.
EMAIL_TOOL_RE = re.compile(
    r"(send_email|create_draft|draft_email|update_draft"
    r"|gmail__(reply|reply_all|send_message|forward)$)",
    re.I,
)


# --- Inter-agent messages: HOMOGLYPH-ONLY (INTERAGENTHOMOGLIF923) -----------
# `curl .../api/messages` is NOT an email send (send-invocation-cases.json pins
# it expected:false, and that stays), so none of the copy rules below ever ran
# on it: no accent audit, no name rule, no em dash -- correctly, because the
# fleet's internal traffic is written WITHOUT accents and the full audit would
# block most of it. One dimension is added here and nothing else: a MIXED-SCRIPT
# word (homoglyph). The fleet coordinates by card ids, agent names and file
# paths passed in messages; a Cyrillic 'a' in one of those does not look wrong,
# it silently points at something that does not exist. Measured 2026-09-16:
# four such characters in the lead agent's own messages, caught only by a
# manual scan.
#
# FAILURE DIRECTION IS THE OPPOSITE OF THE EMAIL BRANCH (Marveen, msg 28870):
#   - homoglyph FOUND           -> BLOCK (exit 2), naming the word and the char;
#   - body NOT INTERPRETABLE     -> PASS, with a loud named systemMessage and a
#     (unreadable path, $-path,     gate-log line. On this channel a false block
#     run-time substitution,        mutes an agent (the fleet's coordination
#     non-object JSON, unknown      backbone); the threat is our own agent
#     shape)                        emitting a lookalike by accident, not an
#                                   attacker, so fail-open-loud is the right side.
# All three shapes are covered, or the concept is not closed: quoted heredoc
# (`--data-binary @- <<'JSON'`), `@file`, and inline `-d '...'`.
_IA_TARGET = re.compile(r"^(https?://)?[^/\s]*/api/messages/?(\?\S*)?$", re.I)
_IA_DATA_FLAGS = ("-d", "--data", "--data-binary", "--data-raw", "--data-ascii", "--json")
_IA_SUBST = re.compile(r"\$\(|`|\$\{?\w")


def _ia_segment(cmd: str):
    """Tokens of the curl segment that POSTs to /api/messages, or None."""
    try:
        segments = _segments_tokens(cmd)
    except ValueError:
        return None
    for toks in segments:
        while toks and _ENV_ASSIGN.match(toks[0]):
            toks = toks[1:]
        if toks and _CURLISH.match(_basename(toks[0])) and any(_IA_TARGET.match(t) for t in toks[1:]):
            return toks
    return None


# The fleet's everyday form is `S=/abs/scratch; ... --data-binary @$S/m.json`:
# the variable is assigned a LITERAL earlier in the SAME command string. That
# is deterministic, so it is resolved here instead of warned about. Measured
# 2026-09-23 over 1153 real inter-agent POSTs: without this, most of the
# warnings were exactly this shape, and a warning that fires on half the
# traffic is noise. Only a plain literal value counts (no quotes-with-$,
# no substitution); anything else stays unresolved and is warned about.
_IA_ASSIGN = re.compile(r"""(?:^|[;&|\n(]\s*|\s)(?:export\s+)?([A-Za-z_]\w*)=(?:"([^"$`]*)"|'([^']*)'|([^\s;&|$`'"()]+))""")


def _ia_resolve_local_vars(cmd: str, ref: str) -> str:
    local = {}
    for m in _IA_ASSIGN.finditer(cmd):
        local[m.group(1)] = next(g for g in m.groups()[1:] if g is not None)
    def sub(m):
        name = m.group(1) or m.group(2)
        return local.get(name, m.group(0))
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", sub, ref)


def _curl_payload_raw(cmd: str, toks, all_flags: bool = False):
    """(raw_body, unreadable_reason) of a curl data flag, from the three shapes
    (inline -d, @file, quoted heredoc via @-). raw_body is None when the call
    carries no data flag at all. Shared by the inter-agent homoglyph gate and
    the HTTP channel gate (GATEHTTP924), so both read the SAME body the same way.
    all_flags: curl joins repeated -d values with '&' (a form body such as
    `-d chat_id=1 -d text=...`); the HTTP gate needs every part, or the text
    field hides behind the first flag. The inter-agent gate keeps first-only."""
    raw = None
    parts = []
    for i, t in enumerate(toks):
        val = None
        for f in _IA_DATA_FLAGS:
            if t == f and i + 1 < len(toks):
                val = toks[i + 1]
            elif t.startswith(f + "="):
                val = t[len(f) + 1:]
            elif f == "-d" and t.startswith("-d") and len(t) > 2 and not t.startswith("--"):
                val = t[2:]
            if val is not None:
                is_raw_flag = f == "--data-raw"
                break
        if val is None:
            continue
        if val.startswith("@") and not is_raw_flag:
            ref = val[1:]
            if ref == "-":
                m = re.search(r"<<-?\s*'?(\w+)'?[^\n]*\n(.*?)\n\1(?=\s|$)", cmd, re.S)
                if not m:
                    return None, "a torzs stdin-rol jon (@-), heredoc nelkul"
                if not re.search(r"<<-?\s*'", cmd) and _IA_SUBST.search(m.group(2)):
                    return None, "a heredoc NEM idezett, es shell-behelyettesitest tartalmaz"
                raw = m.group(2)
            else:
                path = os.path.expandvars(os.path.expanduser(_ia_resolve_local_vars(cmd, ref)))
                if "$" in path:
                    return None, f"a torzs fel nem oldhato @utvonalrol jon (@{ref})"
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        raw = fh.read()
                except OSError as exc:
                    return None, f"a torzs-fajl (@{ref}) nem olvashato ({exc.strerror or exc})"
        else:
            if _IA_SUBST.search(val):
                return None, "az inline torzs shell-behelyettesitest tartalmaz, futasidoben dol el"
            raw = val
        if not all_flags:
            break
        parts.append(raw)
    if all_flags and parts:
        return "&".join(parts), None
    return raw, None


def _ia_payload(cmd: str, toks):
    """(text, unreadable_reason) of the message body, from the three shapes."""
    raw, unreadable = _curl_payload_raw(cmd, toks)
    if unreadable:
        return None, unreadable
    if raw is None:
        # No data flag at all: a GET of the queue (the most frequent call on this
        # path) or a bare POST. Nothing is being SENT, so nothing to scan and
        # nothing to warn about -- a warning here would fire on every queue read.
        return "", None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None, "a torzs nem ervenyes JSON"
    if not isinstance(obj, dict):
        return None, "a torzs JSON, de nem objektum"
    # EVERY string field: a lookalike in `to` misroutes as silently as one in
    # `content` misleads.
    strings = [str(v) for v in obj.values() if isinstance(v, str)]
    return "\n".join(strings), None


def inter_agent_homoglyph_gate(cmd: str) -> None:
    """Exit 2 on a homoglyph, exit 0 otherwise (loudly when unreadable).
    Only called for a command that is NOT an email send."""
    toks = _ia_segment(cmd)
    if toks is None:
        sys.exit(0)
    text, unreadable = _ia_payload(cmd, toks)
    if unreadable:
        msg = ("outgoing-copy-gate (inter-agent, homoglifa): a torzs NEM vizsgalhato -- "
               f"{unreadable}. Az uzenet ATMENT, homoglifa-ellenorzes NELKUL. "
               "Vizsgalhato alak: idezett heredoc (--data-binary @- <<'JSON') vagy @/abszolut/ut.json.")
        _gate_log(msg)
        print(json.dumps({"systemMessage": msg}))
        sys.exit(0)
    mixed = mixed_script_words(text)
    if mixed:
        shown = "; ".join(f"{w!r} -- benne {name}" for w, _c, name in mixed[:5])
        more = f" (+{len(mixed) - 5} tovabbi)" if len(mixed) > 5 else ""
        sys.stderr.write(
            "KIMENO-SZOVEG KAPU (inter-agent): TILTVA -- VEGYES IRASRENDSZERU SZO (homoglifa), "
            f"{len(mixed)} db: {shown}{more}.\n"
            "Egy kartya-azonositoban, agens-nevben vagy utvonalban ez neman felreiranyit. "
            "Javitsd a szoveget es kuldd ujra. (Itt CSAK a homoglifa fut, ekezet- es copy-szabaly nem.)\n"
        )
        sys.exit(2)
    sys.exit(0)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # unparseable payload must not wedge the session

    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    # A tool_input that is not a dict crashed every branch on its first .get(),
    # which the Telegram arm then reported as an internal gate error and passed
    # through fail-open. Measured 2026-09-05: the log holds exactly one such
    # line, AttributeError("'int' object has no attribute 'get'"), and a payload
    # with an integer tool_input reproduces it byte for byte. Name the real
    # cause instead of the symptom, and keep each arm's designed failure
    # direction: nothing of ours is readable, so Telegram (the owner's only
    # supervision channel) still passes, email still blocks.
    if not isinstance(tool_input, dict):
        shape = type(tool_input).__name__
        gated = (re.search(r"telegram.*__reply$", tool, re.I)
                 or re.search(r"(^|__)manage_email$", tool, re.I)
                 or EMAIL_TOOL_RE.search(tool)
                 or tool == "Bash")
        if not gated:
            sys.exit(0)  # not a send: the net must never widen the gate
        _gate_log(f"outgoing-copy-gate: a tool_input nem szotar, hanem {shape} "
                  f"-- a vizsgalat nem futtathato (tool={tool!r}).")
        if re.search(r"telegram.*__reply$", tool, re.I):
            sys.exit(0)  # Telegram stays fail-open by design
        sys.stderr.write(
            "KIMENO-SZOVEG KAPU: TILTVA, a hivas tool_input mezoje nem szotar, hanem "
            f"{shape} -- a kimeno szoveg igy nem olvashato ki, tehat nem vizsgalhato.\n"
            "Fail-closed: egy vizsgalhatatlan kuldes pont a kaput utne ki. "
            "Hivd ujra rendes parameterekkel.\n"
        )
        sys.exit(2)

    # GATECOPY828 (#1184): the scaffold wires this hook onto reply AND
    # edit_message (an edit can replace a working code block with a broken
    # one). The dispatch must recognise BOTH, or the edit half of the matcher
    # invokes a hook that exits 0 without auditing anything.
    if re.search(r"telegram.*__(reply|edit_message)$", tool, re.I):
        telegram_gate(tool_input)  # exits; never falls through
    # COPYGATEMATCHER904: the hook is REGISTERED for manage_email, create_draft,
    # update_draft and the Gmail connector's reply/send_message/forward tools,
    # but this dispatch only ever recognised a tool NAME containing
    # "send_email" -- so on an install whose email tool is the multiplexed
    # mcp__google-workspace__manage_email, every letter fell through to
    # sys.exit(0) and the copy audit NEVER ran on an outgoing email. Measured
    # 2026-09-04: an em dash in a manage_email draft body passed the gate,
    # while the same text was blocked on the Telegram path. Same class as the
    # email-send-gate's MANAGEOP904 fix: a multiplexer cannot be classified by
    # its name, only by the operation it was asked to perform.
    if re.search(r"(^|__)manage_email$", tool, re.I):
        op = str(tool_input.get("operation") or tool_input.get("action") or "").strip().lower()
        if op not in MANAGE_EMAIL_OUTBOUND_OPS:
            sys.exit(0)  # search/read/labels/trash...: no outgoing text of ours
        # A bare forward carries someone else's text and an EMPTY note: there is
        # nothing of ours to audit, and the fail-closed "unreadable" branch below
        # would block it for no reason.
        if op == "forward" and not str(tool_input.get("body") or "").strip():
            sys.exit(0)
        text, unreadable = collect_mcp_body(tool_input), None
    elif EMAIL_TOOL_RE.search(tool):
        # A bare Gmail-connector forward carries someone else's text and no note of
        # ours, the same case as the manage_email forward above: the fail-closed
        # "unreadable" branch below would block it for no reason. Narrow on purpose:
        # only when every filled field is a recipient or an id, so a note in a field
        # the collector does not know still ends in the loud fail-closed block.
        if re.search(r"gmail__forward$", tool, re.I) and all(
                k in _FORWARD_ADDRESS_FIELDS and _is_address_value(v)
                for k, v in tool_input.items() if v not in (None, "", [], {})):
            sys.exit(0)
        text, unreadable = collect_mcp_body(tool_input), None
    elif tool == "Bash":
        cmd = str(tool_input.get("command") or "")
        if not is_send_invocation(cmd):
            http_channel_gate(cmd)  # exits if it is a human-facing HTTP send; else returns
            inter_agent_homoglyph_gate(cmd)  # exits; a no-op pass for anything else
        text, unreadable = collect_bash_body(cmd)
    else:
        sys.exit(0)

    if unreadable or not text.strip():
        if tool == "Bash" and wrapper_depth_hit(cmd):
            sys.stderr.write(
                "KIMENO-SZOVEG KAPU: TILTVA, mert a parancs valodi fejet nem latom.\n"
                f"Ok: a parancs a burkolo-korlatnal ({_HEAD_DEPTH} egymasba agyazott burkolo: sudo, "
                "time, env, nohup, nice, timeout...) is meg burkolo, tehat nem tudom eldonteni, "
                "hogy levelkuldes-e. Ez szandekosan fail-closed.\n\n"
                f"Ha ez NEM levelkuldes: csokkentsd a burkolok szamat {_HEAD_DEPTH} vagy kevesebb ala.\n"
                "Ha levelkuldes: tedd vizsgalhatova -- ABSZOLUT utvonalu stdin-atiranyitas "
                "(< /teljes/ut/body.txt), vagy --body-ban atadott szoveg.\n"
            )
            sys.exit(2)
        if tool == "Bash" and not unreadable and sender_named_only(cmd):
            # SENDERNAMEMSG1126 (PROGI msg 5993): refused only because a sender is NAMED in the command. The generic
            # "nothing to inspect" text below sent the agent looking for a mail body that does not exist.
            sys.stderr.write(
                "KIMENO-SZOVEG KAPU: TILTVA, mert a parancsban kuldo-nevu szo all (sendmail, msmtp, swaks, "
                "send.py --to vagy graph-mail send), es nem tudom eldonteni, hogy kuldes-e.\n"
                "A parancs nem ismert csak-olvaso program, ezert lehet, hogy ezt a szot parancskent futtatja. "
                "Ez szandekosan fail-closed.\n\n"
                "Ha ez NEM levelkuldes, a szo csak adat (kereses, listazas, csomagnev): ne onallo szokent add at, "
                "hanem egy hosszabb idezett mintaban (pl. 'sendmail(' vagy '[s]endmail'), vagy hasznalj "
                "csak olvaso eszkozt (grep, rg, cat, ls, git log, git grep).\n"
                "Ha levelkuldes: futtasd kozvetlenul, vizsgalhato torzzsel (ABSZOLUT utvonalu "
                "stdin-atiranyitas vagy --body).\n"
            )
            sys.exit(2)
        reason = unreadable or "a hook nem talalt vizsgalhato szoveget a hivasban"
        sys.stderr.write(
            "KIMENO-SZOVEG KAPU: TILTVA, mert a levelet nem tudtam megvizsgalni.\n"
            f"Ok: {reason}.\n\n"
            "Ez szandekosan fail-closed: egy vizsgalhatatlan kuldes pont a kaput utne ki.\n"
            "Tedd vizsgalhatova, aztan kuldd ujra -- ABSZOLUT utvonalu stdin-atiranyitas "
            "(< /teljes/ut/body.txt, shell-valtozo NELKUL), vagy --body-ban atadott szoveg.\n"
        )
        sys.exit(2)

    # GATEPERSIST816(2): az EMAIL ut a hianyzo nev-szabalyra FAIL-CLOSED. A
    # level halaszthato, es pont a vevo fele a legdragabb a rossz nev -- egy
    # csendben lealit nev-ellenorzes mellett kuldeni rosszabb, mint megvarni a
    # szabaly-fajl potlasat. (A telegram-ag fail-open marad systemMessage
    # figyelmeztetessel: az a felugyeleti csatorna, ott a nemulas a dragabb.)
    if RULES_STATE == RULES_INVALID:
        # The file EXISTS but cannot be used: someone configured it and got it
        # wrong. Silent enforcement-loss here would be invisible, so this stays
        # fail-closed until the file is repaired (negative control in tests).
        sys.stderr.write(
            "KIMENO-SZOVEG KAPU: TILTVA -- a NEV-SZABALY fajl LETEZIK, de "
            f"ervenytelen ({_LOCAL_RULES}): nem parse-olhato JSON, rossz sema "
            "vagy hibas regex.\n"
            "Javitsd a fajlt (bad_name_patterns: [regex, ...] + correction), "
            "aztan kuldd ujra.\n"
        )
        sys.exit(2)

    problems = audit(text)
    if problems:
        sys.stderr.write(
            "KIMENO-SZOVEG KAPU: TILTVA, a levél nem mehet ki így.\n\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\nJavitsd a szoveget es kuldd ujra. Ekezetes magyar szoveg a vevo fele "
              "nem stiluskerdes: Szabi 2026-08-10-en ket kulon esetben kerte szamon.\n"
        )
        sys.exit(2)

    if RULES_STATE in (RULES_MISSING, RULES_EMPTY):
        # Fail-OPEN, but never silent: the send goes out WITHOUT the name
        # check, and the user must see that on the surface they are using --
        # a log line nobody reads is the same as nothing (CLCOPYGATEHIANY902).
        print(json.dumps({"systemMessage":
            "outgoing-copy-gate: a NEV-SZABALY fajl "
            + ("HIANYZIK" if RULES_STATE == RULES_MISSING else "URES (nincs minta)")
            + f" ({_LOCAL_RULES}) -- ez a level a nev-ellenorzes NELKUL ment ki. "
            "Ha kell a vedelem, hozd letre a fajlt: "
            '{"bad_name_patterns": ["<python-regex>"], "correction": "<helyes alak>"}.'}))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- deliberate blanket: fail-closed net
        # An unhandled crash exits 1, and PreToolUse treats 1 as NON-blocking,
        # so the send would run UNCHECKED -- the exact opposite of the email
        # path's fail-closed contract (e.g. a non-dict tool_input used to
        # AttributeError inside collect_mcp_body). The telegram path never
        # reaches here: telegram_gate() catches its own errors and exits 0
        # (fail-open by design), so this net only ever catches the email/Bash
        # send paths, where blocking is the safe failure mode.
        sys.stderr.write(
            "KIMENO-SZOVEG KAPU: TILTVA, belso hiba a vizsgalat kozben "
            f"({exc!r}).\n"
            "Fail-closed: egy vizsgalhatatlan kuldes pont a kaput utne ki. "
            "Tedd vizsgalhatova a hivast, aztan kuldd ujra.\n"
        )
        sys.exit(2)

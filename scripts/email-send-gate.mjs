#!/usr/bin/env node
// PreToolUse hard-gate: blocks outbound email-send for sub-agents.
//
// Governance control (Szabi 2026-06-25, after the Boni incident: a sub-agent
// autonomously emailed a fabricated address asking for money in Szabi's name).
// Sub-agents may NOT send outbound email; any email must be routed through the
// main agent (Marveen) for approval -- only Marveen retains email-send.
//
// STATED LIMIT (msg 14298): this gate catches the ACCIDENTAL send, not a
// determined evader. Static analysis of arbitrary interpreter code is
// undecidable, so a sub-agent CAN send through interpreter code the patterns
// do not model -- the premise "a sub-agent cannot send outbound" does NOT
// hold against intent, only against accident. The exec-heuristic below covers
// the naive shapes (process-spawn plus a known mailer name in one code
// string) and claims no more. Our sub-agents are not adversaries; if that
// assumption ever changes, this gate is the wrong tool.
//
// KNOWN OPEN SHELL FORMS (BELA msg 6050, 2026-11-26; PROGI msgs 6034/6049; each measured with bash: it sends,
// both gates allow it). Closing them needs a real shell parser and data-flow tracking; after the 2026-11-26 round
// the gate work stops here by decision, and these stay open:
//   - a compound command piped into a stdin shell: 'if ..; then echo X; fi | bash', 'for .. do echo X; done | bash',
//     'f() { echo X; }; f | bash', '{ echo X; } 2>&1 | bash' (a redirection after the group);
//   - 'eval $(echo X)' with the substitution unquoted, 'eval "$(cat <<EOF ... EOF)"' (a heredoc inside it);
//   - a substitution in COMMAND position: '$(echo X)', '`echo X`', '"$(echo send...)" args';
//   - a variable set without an assignment word: 'printf -v V ...', 'read V <<< ...'; 'local V=...' inside a
//     function body; PS4='$(X)' with 'bash -x' / 'set -x';
//   - heredoc search is not quote- or comment-aware: '<<X' after '#' or inside quotes with a space ('<<X y')
//     starts a 'body' that hides the next lines;
//   - and, as before: xargs -I{} sh -c '{}', a script written to a file and run later, a two-step git config,
//     a name assembled at run time, python -c with an attached code argument (-c'...'; node refuses an attached -e).
// These forms hide ANY command, not only a mailer binary: the explicit send.py / Resend / graph-mail rules of this
// gate, and the Gmail-script / mail-credential rule MAILCRED1127, are bypassed the same way. Corrected 2026-11-27
// (PROGI msg 6118 (e)): this host DOES have a configured, live mail path -- scripts/send-gmail.py and the outreach
// scripts take a Gmail SMTP app password from the vault through GET /api/vault/<id> (src/web/routes/connectors.ts),
// which hands any secret to any holder of the dashboard token, and every agent can read that token. MAILCRED1127
// refuses running those scripts and touching that credential for a sub-agent, but it is a name rule; the real
// control is restricting the vault value route to the main agent. Also measured: no mailer binary on PATH, /usr/sbin
// or /usr/lib, and support-mail/send.py has no SUPPORT_* settings in .env.
// Mirrored in scripts/hooks/outgoing-copy-gate.py (KIMONDOTT HATAR block).
//
// Why a hook and not a permissions deny-list: permissive security profiles
// launch Claude Code with --dangerously-skip-permissions, which BYPASSES the
// settings.json allow/deny list. A PreToolUse hook runs regardless of
// permission mode, so it is the only reliable mode-independent gate.
//
// This file is wired into every sub-agent's .claude/settings.json by
// writeAgentSettingsFromProfile() (agent-scaffold.ts), guarded by
// name !== MAIN_AGENT_ID, and re-applied on every spawn (respawn-safe).

import { readFileSync, realpathSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

// Bash command patterns that send mail. SUBGATEPOZ822 (2026-08-22): these are
// no longer the primary trigger -- they matched CONTENT anywhere in the
// command string, and the header's old premise ("a sub-agent has no
// legitimate need to invoke these") broke measurably: the developer of the
// mail tooling IS a sub-agent, and the gate blocked the delivery of the
// mail-gate fix three times in one afternoon (commit message, PR body,
// card-comment sqlite write), plus five more content hits across the fleet
// the same day. The primary trigger is now isSendInvocation() below
// (command-position analysis); this list remains ONLY as the conservative
// fallback when a command cannot be tokenized (unbalanced quote) -- on
// unparseable input the gate behaves exactly as before, never weaker.
const SEND_PATTERNS = [
  /support-mail\/send\.py/i,
  /\bsend\.py\b/i,
  /api\.resend\.com/i,
  /\bresend\b[^\n]*\b(email|send|message)\b/i,
  /\bsendmail\b/i,
  /\bmsmtp\b/i,
  /\bswaks\b/i,
  /\bsmtplib\b|SMTP\s*\(/i,
  /\bmail\.send\b|\bsendEmail\b/i,
  // graph-mail.ts (PR #668, M365/Exchange Online client-credentials mailbox):
  // its CLI is `tsx scripts/graph-mail.ts send ...`, which none of the above
  // patterns catch (no "sendmail"/"mail.send" substring). Also gate any direct
  // call to the exported sendMail() (e.g. a one-off `node -e`/`tsx -e` that
  // imports the module without going through the CLI).
  /\bgraph-mail\b[^\n]*\bsend\b/i,
  /\bsendMail\s*\(/i,
]

// --- command-position analysis (SUBGATEPOZ822) ------------------------------
// Ported from scripts/hooks/outgoing-copy-gate.py (KAPUHATOKOR822, PR #1042,
// two adversarial rounds): quote-aware tokenization, then the INVOKED program
// of each pipeline/sequence segment decides. A quoted token keeps its
// POSITION, so a quoted provider URL in curl argument position still fires
// (the normal way curl is written), while the same domain inside a quoted -d
// payload stays content (the target pattern is anchored to the token start).
// Wrapper-shell -c strings recurse; interpreter code-string arguments
// (python -c / node -e) are scanned for code-level send calls -- code handed
// to an interpreter IS operation, not content.
// Order-independent heredoc stripping (round 3, msg 14286): the rest of
// the intro line (e.g. a redirect after the marker) is command text and
// is KEPT -- only the body is cut. Without this, marker-first spellings
// leaked the body into command position (FP), and dropping the intro
// would have lost a heredoc-fed real sender (FN).
// HEREDOCQUOTE1126 (PROGI msg 6042): the delimiter may also be "EOF" or \\EOF (bash does not expand those bodies
// either); they used to be read as commands (synthetic over-refusals). Mirrors _HEREDOC.
// HEREDOCDELIM1126 (PROGI msg 6049): the body must end where bash ends it, never later -- a gate that cuts later than
// bash swallows the real commands after bash's terminator, and one that cuts earlier can let a second '<<Z' in the
// rest of bash's body swallow them. So: the delimiter is a WHOLE word ('EOF', "EOF", \\EOF or EOF, then a shell
// metacharacter or the line end; a partly quoted one such as <<"E"OF or <<E'OF' does not match, the body is then
// read as commands, the cautious side); the terminator line is exactly the word (nothing after it but the newline),
// led by tabs only after <<- (group 2: '(?!\2)' fails when there is no dash, as an unmatched backreference is empty);
// and it may be the very first body line (an empty heredoc).
// Groups: 1 the intro line kept as command text, 2 the dash, 3 the quote, 4 the delimiter word.
const HEREDOC_RE = /(<<(-)?[ \t]*(?:(["'])|\\)?(\w+)\3(?=[\s;&|<>()]|$)[^\n]*)\n(?:[\s\S]*?\n)??(?:(?!\2)\t*)?\4(?=\n|$)/g
const ENV_ASSIGN = /^[A-Za-z_][A-Za-z_0-9]*=/
const SENDER_PROG = /^(sendmail|msmtp|swaks)$/i
const SENDPY = /^send\.py$/i
const PYTHON = /^python3?$/i
const NODEISH = /^(node|tsx|ts-node|deno|bun|npx)$/i
const GRAPHMAIL = /^graph-mail(\.ts|\.js)?$/i
// WRAPSHELL1126: the ksh family (ksh = ksh93, mksh, lksh), busybox's ash, and the restricted variants all take
// `-c STRING` with the sh option rules (measured on the real shells). 'busybox sh' already steps to 'sh' through
// the launcher table. fish has its own argument rules: FISH_SHELL / fishCommandStrings.
const WRAPPER_SHELL = /^(sh|bash|zsh|dash|ksh|ksh93|mksh|lksh|ash|rbash|rzsh|rksh|rksh93|rmksh|rlksh)$/i
const FISH_SHELL = /^fish$/i
const CURLISH = /^(curl|wget|http)$/i
const RESEND_TARGET = /^(https?:\/\/)?([^/@\s]*\.)?api\.resend\.com(\/|$)/i
const CODE_SEND = /\bsmtplib\b|SMTP\s*\(|\bsendMail\s*\(|\bsendEmail\b|\bmail\.send\b/i
// Naive-shape exec heuristic (msg 14298): process-spawn AND a known mailer
// name together in one interpreter code string. Covers the accidental shapes;
// see the STATED LIMIT in the header for what it deliberately does not claim.
const CODE_EXECISH = /\bsubprocess\b|os\.system|\bpopen\b|child_process|\bexec[A-Za-z]*\s*\(|\bspawn[A-Za-z]*\s*\(/i
const CODE_SENDER_LIT = /sendmail|msmtp|swaks|send\.py/i
const codeStringSends = (code) => CODE_SEND.test(code) || (CODE_EXECISH.test(code) && CODE_SENDER_LIT.test(code))
// EXECVEC1126 (PROGI msg 5993, measured): perl -e/-E, ruby -e and php -r/-B/-R/-E run code that can start the
// sender ('system("sendmail a")', qx, backticks, %x, open "|...", shell_exec, passthru) or send by library
// (Net::SMTP, MIME::Lite, php mail(), ruby net/smtp). Same naive-shape rule as codeStringSends, with the process
// starters and mail libraries of these languages, applied to EVERY argument (no per-language option parsing).
const SCRIPT_INTERP = /^(perl|perl5[\d.]*|ruby|ruby[\d.]+|jruby|php|php[\d.]+)$/i
// No left word boundary: an attached code argument ('-esystem(...)', '-rsystem(...)') glues the option letter on.
const SCRIPT_EXECISH = /system\b|qx\b|`|%x|exec\b|open\b|shell_exec|passthru|proc_open|popen|Open3|spawn\b/i
const SCRIPT_MAIL_LIB = /Net::SMTP|Mail::Sendmail|MIME::Lite|Email::Sender|Mail::Mailer|net\/smtp|\bmail\s*\(|PHPMailer|Swift_|\bMail\.(new|deliver)\b|Pony\.mail|ActionMailer/i
const scriptCodeSends = (code) => codeStringSends(code) || SCRIPT_MAIL_LIB.test(code) ||
  (SCRIPT_EXECISH.test(code) && CODE_SENDER_LIT.test(code))
// EXECVEC1126: vi-family editors run ex commands from '-c CMD', '--cmd CMD' and '+CMD' ('!sendmail a',
// 'call system(...)', measured with vim). A command that names a sender is a send.
const EDITOR = /^(vim|vi|nvim|ex|view|vimdiff|gvim|rvim|evim|nvi)$/i

// Unquoted newline / backtick / `$(` become segment separators; quoted text is
// untouched (it is content). Tracks quote state by hand -- no shell involved.
// REDIRHEAD1126: placeholders the mask puts in place of the '&' / '|' that belong to a redirection.
const REDIR_AMP = '\u0001'
const REDIR_BAR = '\u0002'
// A redirection token at command position: optional fd (or {var}, or the '&' of '&>'), the operator, and the
// target either attached or as the NEXT token. bash applies leading redirections and runs the rest as the command:
// '2>&1 sendmail x', '</dev/null sendmail x', '>/dev/null sendmail x', '&>/dev/null sendmail x' all send.
const REDIR_RX = /^(?:\d+|\{[A-Za-z_]\w*\}|\u0001)?(?:<<<|<<-?|<>|>>|>\u0002|>\u0001|<\u0001|>|<)(.*)$/s

export function maskSubshellMarkers(cmd) {
  let out = ''
  let q = null
  for (let i = 0; i < cmd.length; i++) {
    const ch = cmd[i]
    if (q) {
      if (ch === '\\' && q === '"' && i + 1 < cmd.length) { out += cmd[i] + cmd[i + 1]; i++; continue }
      if (ch === q) q = null
      out += ch
      continue
    }
    if (ch === "'" || ch === '"') { q = ch; out += ch; continue }
    // bash line continuation: an unquoted backslash-newline pair is removed BEFORE the line is split into words.
    // Kept as an escape it put a "\n" token at command position, so `cd /tmp &&\<NL>sendmail x` (the common
    // multi-line `cmd && \` style) was NOT recognised as a send and passed this hard gate. Same rule as the
    // python twin (outgoing-copy-gate.py _mask_subshell_markers); the shared rows pin it.
    if (ch === '\\' && cmd[i + 1] === '\n') { i++; continue }
    if (ch === '\\' && i + 1 < cmd.length) { out += cmd[i] + cmd[i + 1]; i++; continue }
    if (ch === '\n' || ch === '`') { out += ';'; continue }
    if (ch === '$' && cmd[i + 1] === '(') { out += ';'; i++; continue }
    // REDIRHEAD1126: the '&' of '>&' / '<&' / '&>' and the '|' of '>|' belong to a REDIRECTION, not to a list
    // operator or a pipe. Left as is, the tokenizer split '2>&1 sendmail x' into ['2>'] and ['1', 'sendmail', 'x'],
    // so the '1' became the program. They are kept inside the redirection token as REDIR_AMP / REDIR_BAR.
    if ((ch === '>' || ch === '<') && cmd[i + 1] === '&') { out += ch + REDIR_AMP; i++; continue }
    if (ch === '&' && cmd[i + 1] === '>') { out += REDIR_AMP; continue }
    if (ch === '>' && cmd[i + 1] === '|') { out += ch + REDIR_BAR; i++; continue }
    out += ch
  }
  return out
}

// Quote-aware tokenizer -> [[token, ...], ...] per segment. Throws on an
// unbalanced quote (the caller falls back to the legacy content patterns).
export function segmentsTokens(cmd) {
  const s = maskSubshellMarkers(cmd.replace(HEREDOC_RE, '$1'))
  const segments = []
  let cur = []
  let tok = ''
  let hasTok = false
  let q = null
  const pushTok = () => { if (hasTok) { cur.push(tok); tok = ''; hasTok = false } }
  const pushSeg = () => { pushTok(); if (cur.length) { segments.push(cur); cur = [] } }
  for (let i = 0; i < s.length; i++) {
    const ch = s[i]
    if (q) {
      if (ch === '\\' && q === '"' && i + 1 < s.length) { tok += s[++i]; continue }
      if (ch === q) { q = null; continue }
      tok += ch
      continue
    }
    if (ch === "'" || ch === '"') { q = ch; hasTok = true; continue }
    if (ch === '\\' && i + 1 < s.length) { tok += s[++i]; hasTok = true; continue }
    if (ch === ' ' || ch === '\t') { pushTok(); continue }
    if ('|&;()'.includes(ch)) { pushSeg(); continue }
    tok += ch
    hasTok = true
  }
  if (q) throw new Error('unbalanced quote')
  pushSeg()
  return segments
}

const basename = (t) => t.split('/').pop()

// WRAPGATE1126 (own gap; the shape follows upstream SENDWRAP924 068557b0 and
// HEADDEPTH924 bdfb09d4): this hard gate looked at the FIRST token of a segment
// only, so any wrapper in front of the sender ('env sendmail x', 'sudo ...',
// 'nohup ...', 'timeout 5 ...', 'exec', 'command', 'nice', 'time', 'xargs',
// 'setsid', 'stdbuf', 'builtin') and any command-position keyword ('if x; then
// sendmail x; fi', '{ sendmail x; }', '! sendmail x') let a sub-agent send past
// it. Measured 2026-11-26 with the real hook from a sub-agent cwd: 'sendmail x'
// deny, 'env sendmail x' allowed. A wrapper is now stepped over WITH its own
// flags, exactly as the python twin (_WRAPPERS / _command_heads in
// outgoing-copy-gate.py) does: a value-taking flag consumes its value ('sudo -u
// sendmail true' stays false), timeout skips its duration, an unknown long flag
// is read both ways so it errs toward refusing, 'command -v' runs nothing,
// function / coproc bodies are in command position, and a wrapper still found
// at the depth bound counts as a send (null head), never as a pass.
// send-invocation-cases.json binds the two copies.
const CMD_POSITION_KEYWORDS = new Set(['if', 'then', 'else', 'elif', 'do', 'while', 'until', '{', '!'])
const WRAPPERS = new Map([
  ['time', ['fo', ['format', 'output'], 0]],
  ['sudo', ['ughpCUrtDRT', ['user', 'group', 'host', 'prompt', 'close-from', 'other-user',
    'role', 'type', 'chdir', 'chroot', 'command-timeout'], 0]],
  ['env', ['uCS', ['unset', 'chdir', 'split-string'], 0]],
  ['nohup', ['', [], 0]],
  ['nice', ['n', ['adjustment'], 0]],
  ['exec', ['a', [], 0]],
  ['command', ['', [], 0]],
  ['builtin', ['', [], 0]],
  ['xargs', ['ILnPsdEa', ['arg-file', 'delimiter', 'eof', 'replace', 'max-lines', 'max-args',
    'max-procs', 'max-chars', 'process-slot-var'], 0]],
  ['timeout', ['sk', ['signal', 'kill-after'], 1]],
  ['setsid', ['', [], 0]],
  ['stdbuf', ['ioe', ['input', 'output', 'error'], 0]],
  // LAUNCHGATE1126 (PROGI review 5977 point B): launchers that run their argument as a command and passed both
  // gates. Same table as the python twin (_WRAPPERS); an option this table does not know is caught by the
  // sender-token layer (senderTokenLayer) rather than mis-read as the program.
  ['flock', ['wE', ['timeout', 'conflict-exit-code'], 1]],
  ['ionice', ['cnpPu', ['class', 'classdata', 'pid', 'pgid', 'uid'], 0]],
  ['chrt', ['TPD', ['sched-runtime', 'sched-period', 'sched-deadline'], 1]],
  ['taskset', ['', [], 1]],
  ['doas', ['uC', [], 0]],
  ['runuser', ['ugG', ['user', 'group', 'supp-group', 'shell', 'whitelist-environment'], 0]],
  ['unbuffer', ['', [], 0]],
  ['nsenter', ['tSGwr', ['target', 'setuid', 'setgid', 'wd', 'root'], 0]],
  ['systemd-run', ['pEMuGH', ['property', 'setenv', 'machine', 'unit', 'uid', 'gid', 'host', 'description',
    'slice', 'working-directory', 'on-calendar', 'on-active', 'timer-property'], 0]],
  ['busybox', ['', [], 0]],
  ['parallel', ['jS', ['jobs', 'sshlogin'], 0]],
  ['strace', ['oeapsuIEPSXbO', ['output', 'trace', 'signal', 'attach', 'string-limit', 'user', 'env'], 0]],
  ['ltrace', ['oeapsunADFLS', ['output', 'library', 'align', 'indent', 'config', 'user'], 0]],
  ['sg', ['', [], 1]],
])

// LAUNCHGATE1126: launchers whose option carries a whole SHELL COMMAND STRING (like `bash -c`), re-evaluated with
// the same depth bound, fail-closed at the bound. Mirrors _STRING_LAUNCHERS.
const STRING_LAUNCHERS = new Map([
  ['su', ['c', 'command']], ['runuser', ['c', 'command']], ['script', ['c', 'command']],
  ['flock', ['c', 'command']], ['sg', ['c']],
])
const FIND_EXEC = new Set(['-exec', '-execdir', '-ok', '-okdir'])
// LAUNCHGATE1126, the sender-token layer: programs that NEVER run an argument as a command (mirrors
// _NON_EXEC_PROGS). Calibrated on 8045 real Bash commands of the fleet: 0 new refusals on real work.
const NON_EXEC_PROGS = new Set([
  'echo', 'printf', 'cat', 'less', 'more', 'head', 'tail', 'grep', 'egrep', 'fgrep', 'rg', 'ag', 'wc', 'sort',
  'uniq', 'diff', 'cmp', 'comm', 'cut', 'tr', 'column', 'jq', 'yq', 'nl', 'tac', 'rev', 'fold', 'fmt',
  'man', 'info', 'which', 'whereis', 'type', 'apropos', 'whatis', 'ls', 'stat', 'file', 'readlink', 'realpath',
  'dirname', 'basename', 'du', 'df', 'tree', 'md5sum', 'sha1sum', 'sha256sum', 'xxd', 'od', 'hexdump', 'strings',
  'chmod', 'chown', 'chgrp', 'touch', 'mkdir', 'rmdir', 'rm', 'cp', 'mv', 'ln', 'test', '[', '[[', 'true',
  'false', 'cd', 'pwd', ':', 'id', 'whoami', 'pgrep', 'locate', 'apt-cache',
])
// READONLYSUB1126: commands that never run an argument when called with one of these subcommands (PROGI msg 5993:
// 'git grep -n X', 'systemctl status X', 'pip show X'... were refused by the sender-token layer, and on the main
// agent's copy gate as a loud "nothing to inspect" block). Editors stay out ('vim -c "!X"' runs X). Mirrors
// _READ_ONLY_SUBCOMMANDS in outgoing-copy-gate.py.
const READ_ONLY_SUBCOMMANDS = new Map([
  ['git', new Set(['grep', 'log', 'diff', 'show', 'status', 'add'])],
  ['systemctl', new Set(['status'])],
  ['pip', new Set(['show'])], ['pip3', new Set(['show'])],
  ['npm', new Set(['view', 'info', 'show'])],
  ['docker', new Set(['images'])],
])
// git global options that cannot make git run a program: '-C DIR', '--git-dir', '--work-tree', '--no-pager'... ;
// '-c', '--config-env' and '--exec-path' can (core.pager, alias.X='!cmd', diff.external), so they are not here.
// ENVPREFIX1126: '-p' / '--paginate' (run the pager) are not safe either.
const GIT_SAFE_GLOBAL = new Set(['--no-pager', '-P', '--bare', '--no-optional-locks', '--literal-pathspecs',
  '--no-replace-objects', '--glob-pathspecs', '--noglob-pathspecs', '--icase-pathspecs'])
const GIT_SAFE_GLOBAL_VALUE = new Set(['-C', '--git-dir', '--work-tree', '--namespace'])

function readOnlyHead(h) {
  const prog = basename(h[0])
  if (prog === 'dpkg') {
    // only the query actions -S / -L (and their long names), nothing that installs or configures
    const opts = h.slice(1).filter((t) => t.startsWith('-'))
    return opts.length > 0 && opts.every((t) => ['-S', '-L', '--search', '--listfiles'].includes(t))
  }
  const subs = READ_ONLY_SUBCOMMANDS.get(prog)
  if (!subs) return false
  let i = 1
  if (prog === 'git') {
    while (i < h.length && h[i].startsWith('-')) {
      const t = h[i]
      if (GIT_SAFE_GLOBAL.has(t)) { i++; continue }
      if (GIT_SAFE_GLOBAL_VALUE.has(t)) { i += 2; continue }
      if ([...GIT_SAFE_GLOBAL_VALUE].some((o) => o.startsWith('--') && t.startsWith(o + '='))) { i++; continue }
      return false
    }
  } else {
    while (i < h.length && h[i].startsWith('-')) i++
  }
  if (i >= h.length || !subs.has(h[i])) return false
  // ENVPREFIX1126: options that make a read-only git subcommand run a program: --ext-diff (diff.external /
  // GIT_EXTERNAL_DIFF), and 'git add -e / -i / -p' (the editor, the interactive loop).
  if (prog === 'git') {
    const args = h.slice(i + 1)
    if (args.some((t) => t === '--ext-diff')) return false
    if (h[i] === 'add' && args.some((t) => ['--edit', '--interactive', '--patch'].includes(t) || (/^-[A-Za-z]+$/.test(t) && /[eip]/.test(t)))) return false
  }
  // 'git grep -O<prog>' / '--open-files-in-pager[=<prog>]' (any unique prefix) runs <prog> on the hits (measured).
  if (prog === 'git' && h[i] === 'grep' && h.slice(i + 1).some((t) =>
    (/^-[A-Za-z]*O/.test(t) && !t.startsWith('--')) || (t.startsWith('--o') && 'open-files-in-pager'.startsWith(t.slice(2).split('=')[0])))) return false
  return true
}
const HEAD_DEPTH = 8

// ENVSPLIT1126: [string, index after it] for `env ... -S STRING ...` / `--split-string[=]STRING` / an option
// cluster ending in S ('-iS STRING', '-SSTRING'), or null. env splits STRING into words and runs them as the
// command, followed by the remaining arguments: 'env -S "sendmail x"' sends (measured with bash), and the wrapper
// table only consumed STRING as the value of -S. Mirrors _env_split_string in outgoing-copy-gate.py.
function envSplitString(toks) {
  let i = 1
  while (i < toks.length) {
    const t = toks[i]
    if (t === '--') return null
    if (t === '-S' || t === '--split-string') return i + 1 < toks.length ? [toks[i + 1], i + 2] : null
    if (t.startsWith('--split-string=')) return [t.slice('--split-string='.length), i + 1]
    if (t.startsWith('-') && !t.startsWith('--') && t.slice(1).includes('S')) {
      const k = t.indexOf('S', 1)
      if (t.slice(k + 1)) return [t.slice(k + 1), i + 1]
      return i + 1 < toks.length ? [toks[i + 1], i + 2] : null
    }
    if (t === '-u' || t === '-C' || t === '--unset' || t === '--chdir') { i += 2; continue }
    if (t.startsWith('-') || ENV_ASSIGN.test(t)) { i++; continue }
    return null
  }
  return null
}

function commandHeads(toksIn, d = 0, positionalsDone = false) {
  let toks = toksIn
  for (;;) {
    if (toks.length && (ENV_ASSIGN.test(toks[0]) || CMD_POSITION_KEYWORDS.has(toks[0]))) { toks = toks.slice(1); continue }
    // REDIRHEAD1126: a leading redirection is stepped over, with its target when that is the next token.
    const r = toks.length ? REDIR_RX.exec(toks[0]) : null
    if (r) { toks = toks.slice(r[1] === '' ? 2 : 1); continue }
    break
  }
  if (!toks.length) return [toks]
  const w = basename(toks[0])
  if (d >= HEAD_DEPTH) return (WRAPPERS.has(w) || w === 'function' || w === 'coproc') ? [null] : [toks]
  if (w === 'function') return commandHeads(toks.slice(2), d + 1)
  if (w === 'coproc') return [...commandHeads(toks.slice(1), d + 1), ...commandHeads(toks.slice(2), d + 1)]
  // ENVSPLIT1126: the -S string is a command line of its own; its words plus the remaining arguments are one more
  // candidate head. The ordinary wrapper stepping below still runs too, so nothing that matched before is lost.
  let splitHeads = []
  if (w === 'env') {
    const es = envSplitString(toks)
    if (es !== null) {
      let words
      try { words = segmentsTokens(es[0]).flat() } catch { words = es[0].split(/\s+/).filter(Boolean) }
      splitHeads = commandHeads([...words, ...toks.slice(es[1])], d + 1)
    }
  }
  const spec = WRAPPERS.get(w)
  if (!spec) return [toks]
  // LAUNCHGATE1126: a launcher that is BOTH a wrapper and a string launcher ('flock FILE -c STRING', 'sg GROUP -c
  // STRING', 'runuser USER -c STRING') keeps its own head too, so headIsSend sees its -c STRING.
  if (STRING_LAUNCHERS.has(w)) splitHeads = [...splitHeads, toks]
  const [shortVal, longVal, positionalsSpec] = spec
  const positionals = positionalsDone ? 0 : positionalsSpec
  let i = 1
  const starts = []
  while (i < toks.length) {
    const t = toks[i]
    if (t === '--') { i++; break }
    if (t.startsWith('--')) {
      if (!t.includes('=') && longVal.includes(t.slice(2))) { i += 2; continue }
      if (!t.includes('=')) starts.push(i + 2)
      i++
      continue
    }
    if (t.startsWith('-') && t.length > 1) {
      if (w === 'command' && (t.includes('v') || t.includes('V'))) return []
      let k = -1
      for (let j = 1; j < t.length; j++) if (shortVal.includes(t[j])) { k = j; break }
      i += k === t.length - 1 ? 2 : 1
      continue
    }
    break
  }
  starts.unshift(i)
  // GNU getopt permutes: options may also follow the positional ('timeout 2 -- sendmail x', 'timeout 2 -s KILL
  // sendmail x' both run sendmail). After the positionals the flag loop runs once more for the same wrapper.
  return [...splitHeads, ...starts.flatMap((s) => {
    const after = toks.slice(s + positionals)
    if (positionals && after.length && after[0].startsWith('-')) return commandHeads([toks[0], ...after], d + 1, true)
    return commandHeads(after, d + 1)
  })]
}

// ENVPREFIX1126 (PROGI msg 6001): an assignment in command position runs its value through the program it
// configures: 'GIT_EXTERNAL_DIFF="sendmail a" git diff', 'GIT_EDITOR=... git add -e', 'PAGER=... man x'. Production
// allowed these. The assignments in front of the command (and after 'env') whose value names a sender are a send.
// Only variables that name a program to RUN count ('env A=sendmail ls' runs no sender: WRAPGATE1126 control).
// ENVFORMS1126 (PROGI msg 6034): GIT_CONFIG_VALUE_n (with GIT_CONFIG_COUNT/KEY_n) and GIT_CONFIG_PARAMETERS carry
// git config, e.g. an alias '!cmd'.
const RUNNER_VAR = /^(?:GIT_CONFIG_VALUE_\d+|GIT_CONFIG_PARAMETERS|GIT_[A-Z_]*(?:DIFF|EDITOR|PAGER|SSH|SSH_COMMAND|ASKPASS|PROXY_COMMAND)|[A-Z_]*PAGER|[A-Z_]*EDITOR|VISUAL|BROWSER|LESSOPEN|LESSCLOSE|[A-Z_]*ASKPASS|BASH_ENV|ENV|PROMPT_COMMAND|SHELL)=/
// ENVFORMS1126: also after the declaration builtins ('export X=...', 'declare -x X=...': the value is used by a
// later command of the same shell), and after env's value-taking options ('env -u V X=...').
const DECL_BUILTINS = new Set(['export', 'declare', 'typeset', 'readonly', 'local'])
function prefixAssignments(toks) {
  const out = []
  let i = 0
  for (;;) {
    while (i < toks.length && ENV_ASSIGN.test(toks[i])) out.push(toks[i++])
    if (i < toks.length && DECL_BUILTINS.has(toks[i])) {
      i++
      while (i < toks.length) { if (ENV_ASSIGN.test(toks[i])) out.push(toks[i]); i++ }
      return out
    }
    if (i < toks.length && basename(toks[i]) === 'env') {
      i++
      while (i < toks.length && (toks[i].startsWith('-') || ENV_ASSIGN.test(toks[i]))) {
        if (ENV_ASSIGN.test(toks[i])) out.push(toks[i])
        if (['-u', '--unset', '-C', '--chdir'].includes(toks[i])) i++
        i++
      }
      continue
    }
    return out
  }
}

function segmentIsSend(toksIn, depth) {
  if (prefixAssignments(toksIn).some((a) => RUNNER_VAR.test(a) && CODE_SENDER_LIT.test(a.slice(a.indexOf('=') + 1)))) return true
  const heads = commandHeads(toksIn)
  if (heads.some((h) => headIsSend(h, depth))) return true
  return senderTokenLayer(toksIn, heads)
}

function segmentNamesSender(toks) {
  if (toks.some((t) => SENDER_PROG.test(basename(t)))) return true
  if (toks.some((t) => SENDPY.test(basename(t))) && toks.some((t) => t === '--to' || t.startsWith('--to='))) return true
  return toks.some((t) => GRAPHMAIL.test(basename(t))) && toks.includes('send')
}

// LAUNCHGATE1126: a sender named anywhere in the segment, run by something other than a never-exec program.
// eval re-parses its arguments and is judged on that re-parse in headIsSend, so it is not judged here again.
function senderTokenLayer(toks, heads) {
  if (!segmentNamesSender(toks)) return false
  // find without an exec action runs nothing; with one, headIsSend has already judged the action's command.
  // A wrapper head (kept only so a string launcher's -c STRING is seen) is represented by its stepped heads.
  const hs = heads.filter((h) => h && h.length)
    .filter((h) => { const p = basename(h[0]); return p !== 'eval' && p !== 'find' && !WRAPPERS.has(p) })
  // ENVPREFIX1126: no read-only exemption when the segment carries any assignment (GIT_EXTERNAL_DIFF, GIT_EDITOR,
  // PAGER... can make the 'read-only' command run a program; PROGI's PoC: 'GIT_EXTERNAL_DIFF=... git log -p --ext-diff').
  const anyAssign = toks.some((t) => ENV_ASSIGN.test(t))
  return hs.length > 0 && !hs.every((h) => NON_EXEC_PROGS.has(basename(h[0])) || (!anyAssign && readOnlyHead(h)))
}

// The value of `-<short>` (alone, or last in a cluster like '-qc'), `--<long> X` or `--<long>=X`, anywhere.
function launcherString(rest, short, longs) {
  for (let i = 0; i < rest.length; i++) {
    const t = rest[i]
    for (const lg of longs) {
      if (t === '--' + lg) return i + 1 < rest.length ? rest[i + 1] : null
      if (t.startsWith('--' + lg + '=')) return t.slice(lg.length + 3)
    }
    if (t.length > 1 && t[0] === '-' && !t.startsWith('--') && t.endsWith(short)) return i + 1 < rest.length ? rest[i + 1] : null
  }
  return null
}

function shellStringIsSend(s, depth) {
  if (depth >= 3) return s.trim().length > 0
  return isSendInvocation(s, depth + 1)
}

// SHCLUSTER1126: the command string of `sh|bash|zsh|dash [options] -c STRING`, or null. The shell takes the
// first NON-option argument as the string once -c was given anywhere among the options, and -c can sit inside a
// cluster ('-lc', '-ec', '-xc'). Every 'o' / 'O' in an option word ('-o', '+O', '-euxo', '-co', '-oc') takes the
// next argument as its value, in order. '--rcfile FILE', '--init-file FILE' (bash) and '--emulate NAME' (zsh) take a
// value, every other '--long' option stands alone, '--' and a lone '-' end the options, a lone '+' is skipped.
// Mirrors _shell_command_string in outgoing-copy-gate.py.
// WRAPSHELL1126: mksh/lksh also take a value after 'T' ('-T /dev/ttyN', '-T -'; in bash 'T' is a plain flag), and
// 'ksh' may be mksh, so the whole ksh family reads it so. ksh93 RUNS its first operand as a command line when no
// file of that name exists (measured: "ksh 'sendmail a@b.hu'" sends, "ksh 'nonexist foo'" says "nonexist: not
// found"), so for ksh/ksh93 without -c the first operand is the command string.
const SHELL_LONG_WITH_VALUE = new Set(['--rcfile', '--init-file', '--emulate'])
const KSH_FAMILY = /^r?(ksh|ksh93|mksh|lksh)$/i
const KSH93_OPERAND = /^r?ksh(93)?$/i
function shellCommandString(rest, prog = 'sh') {
  const valueChars = KSH_FAMILY.test(prog) ? 'oOT' : 'oO'
  let sawC = false
  let i = 0
  while (i < rest.length) {
    const t = rest[i]
    // A lone '-' ends the options like '--'; a lone '+' is an empty option word and the options go on (bash and
    // dash, measured: 'bash -c - "sendmail x"', 'bash -c + "sendmail x"' and 'bash + -c "sendmail x"' all send).
    if (t === '--' || t === '-') { i++; break }
    if (t === '+') { i++; continue }
    if (SHELL_LONG_WITH_VALUE.has(t)) { i += 2; continue }
    if (t.startsWith('--')) { i++; continue }
    if (t.length > 1 && (t[0] === '-' || t[0] === '+')) {
      if (t[0] === '-' && t.slice(1).includes('c')) sawC = true
      // SHCLUSTERFIX1126: each o/O of the word swallows one following argument ('-euxo pipefail -c ...').
      i += 1 + [...t.slice(1)].filter((ch) => valueChars.includes(ch)).length
      continue
    }
    break
  }
  if (i >= rest.length) return null
  return sawC || KSH93_OPERAND.test(prog) ? rest[i] : null
}

// WRAPSHELL1126: the strings fish runs: the value of every -c / -C (alone, in a cluster, or attached: '-c"x"',
// '-Nc x'), '--command' / '--init-command' with '=' or the next argument, including the unique prefixes getopt
// accepts ('--comm', '--init'). Options end at the first non-option or '--'. Measured on fish 4.2.1.
const FISH_SHORT_VALUE = 'cCdopfD'
const FISH_LONG = ['command', 'init-command', 'debug', 'debug-output', 'debug-stack-frames', 'profile',
  'profile-startup', 'features', 'interactive', 'login', 'no-config', 'no-execute', 'private', 'print-rusage-self',
  'print-debug-categories', 'version', 'help']
const FISH_LONG_VALUE = new Set(['command', 'init-command', 'debug', 'debug-output', 'debug-stack-frames', 'profile',
  'profile-startup', 'features'])
function fishCommandStrings(rest) {
  const out = []
  let i = 0
  while (i < rest.length) {
    const t = rest[i]
    if (t === '--') break
    if (t.startsWith('--')) {
      const eq = t.indexOf('=')
      const name = eq < 0 ? t.slice(2) : t.slice(2, eq)
      const exact = FISH_LONG.includes(name)
      const hits = exact ? [name] : FISH_LONG.filter((l) => l.startsWith(name))
      // An ambiguous prefix that could be --command / --init-command is read as one (conservative).
      const runs = hits.some((l) => l === 'command' || l === 'init-command')
      const takes = hits.some((l) => FISH_LONG_VALUE.has(l))
      let val = null
      if (eq >= 0) val = t.slice(eq + 1)
      else if (takes) { val = i + 1 < rest.length ? rest[i + 1] : null; i++ }
      if (runs && val !== null) out.push(val)
      i++
      continue
    }
    if (t.length > 1 && t[0] === '-') {
      i++
      for (let k = 1; k < t.length; k++) {
        const ch = t[k]
        if (!FISH_SHORT_VALUE.includes(ch)) continue
        let val = null
        if (k + 1 < t.length) val = t.slice(k + 1)
        else if (i < rest.length) { val = rest[i]; i++ }
        if ((ch === 'c' || ch === 'C') && val !== null) out.push(val)
        break
      }
      continue
    }
    break
  }
  return out
}

// EXECVEC1126: git runs a program from its global '-c KEY=VALUE' (alias.X='!cmd', core.sshCommand, core.pager,
// diff.external...) and from 'git grep -O<prog>' / '--open-files-in-pager=<prog>' (measured: all send). A '!' alias
// is a shell command line; any other value that names a sender is a send. Only the options BEFORE the subcommand
// are global ('git grep -c X' is a count flag).
const GIT_GLOBAL_VALUE = new Set(['-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env', '--super-prefix'])
function gitConfigValueSends(key, val, depth) {
  if (CODE_SENDER_LIT.test(val)) return true
  if (/^alias\./i.test(key) && val.startsWith('!')) {
    if (depth >= 3) return !!val.slice(1).trim()
    return isSendInvocation(val.slice(1), depth + 1)
  }
  return false
}
// ENVFORMS2 (PROGI msg 6049, BELA msg 6050): 'git --config-env=KEY=VAR' (or '--config-env KEY=VAR') takes the config
// value from the environment variable VAR. Returns [key, VAR] pairs of the global options before the subcommand.
function gitConfigEnvPairs(rest) {
  const out = []
  let i = 0
  while (i < rest.length && rest[i].startsWith('-')) {
    const t = rest[i]
    let kv = null
    if (t === '--config-env') { kv = rest[i + 1] ?? ''; i += 2 } else if (t.startsWith('--config-env=')) { kv = t.slice(13); i++ } else { i += GIT_GLOBAL_VALUE.has(t) ? 2 : 1 }
    if (kv === null) continue
    const eq = kv.lastIndexOf('=')
    if (eq > 0) out.push([kv.slice(0, eq), kv.slice(eq + 1)])
  }
  return out
}
// The variables a 'git --config-env' anywhere in this command line reads, and the config key each feeds; then any
// assignment of such a variable in the line (in front of a command, after env/export/declare, or alone) whose value
// would send through that key is a send -- the same judgement as 'git -c KEY=VALUE'. A plain, unexported
// assignment is counted too (cautious, like ENVFORMS1126's runner variables). Mirrors _config_env_sends.
function configEnvSends(segments, depth) {
  const keys = new Map()
  for (const t of segments) {
    for (const h of commandHeads(t)) {
      if (h && h.length && basename(h[0]) === 'git') for (const [k, v] of gitConfigEnvPairs(h.slice(1))) keys.set(v, [...(keys.get(v) || []), k])
    }
  }
  if (!keys.size) return false
  return segments.some((t) => prefixAssignments(t).some((a) => {
    const eq = a.indexOf('=')
    const ks = keys.get(a.slice(0, eq))
    return !!ks && ks.some((k) => gitConfigValueSends(k, a.slice(eq + 1), depth))
  }))
}
function gitRunsSender(rest, depth) {
  const values = []
  let i = 0
  while (i < rest.length && rest[i].startsWith('-')) {
    const t = rest[i]
    if (t === '-c') { if (i + 1 < rest.length) values.push(rest[i + 1]); i += 2; continue }
    if (t.startsWith('-c') && !t.startsWith('--')) { values.push(t.slice(2)); i++; continue }
    if (GIT_GLOBAL_VALUE.has(t)) { i += 2; continue }
    i++
  }
  for (const kv of values) {
    const eq = kv.indexOf('=')
    if (gitConfigValueSends(eq < 0 ? kv : kv.slice(0, eq), eq < 0 ? '' : kv.slice(eq + 1), depth)) return true
  }
  if (rest[i] === 'grep') {
    for (const t of rest.slice(i + 1)) {
      if (t === '--') break
      let prog = null
      if (/^-[A-Za-z]*O/.test(t) && !t.startsWith('--')) prog = t.slice(t.indexOf('O') + 1)
      else if (t.startsWith('--o') && 'open-files-in-pager'.startsWith(t.slice(2).split('=')[0]) && t.includes('=')) prog = t.slice(t.indexOf('=') + 1)
      if (prog && CODE_SENDER_LIT.test(prog)) return true
    }
  }
  return false
}

// SUBOUTRUN1126 (PROGI msg 6034): 'sh -c "$(...)"' and 'eval "$(...)"' RUN the OUTPUT of the substitution. The
// inner command ('echo "sendmail a"') is not a send by itself; its output is. A body that names a sender, or whose
// echo/printf literal parses as a send, is a send. 'eval "$(ssh-agent -s)"' stays allowed.
function substitutionOutputSends(str, depth) {
  return dquoteSubstitutions(str, true).some((b) => {
    if (CODE_SENDER_LIT.test(b)) return true
    let segs
    try { segs = segmentsTokens(b) } catch { return false }
    return segs.some((t) => {
      const h = commandHeads(t)[0]
      return h && (basename(h[0]) === 'echo' || basename(h[0]) === 'printf') && depth < 3 &&
        isSendInvocation(h.slice(1).filter((x) => !/^-[neE]+$/.test(x)).join(' '), depth + 1)
    })
  })
}

function headIsSend(toks, depth) {
  if (toks === null) return true // a wrapper at the depth bound: the real command is out of sight, refuse
  if (!toks.length) return false
  const prog = basename(toks[0])
  const rest = toks.slice(1)
  if (SENDER_PROG.test(prog)) return true
  // LAUNCHGATE1126: a launcher's -c STRING, watch's joined arguments, and find's -exec command.
  const specL = STRING_LAUNCHERS.get(prog)
  if (specL) {
    const s = launcherString(rest, specL[0], specL.slice(1))
    if (s !== null && shellStringIsSend(s, depth)) return true
  }
  if (prog === 'watch') {
    let i = 0
    while (i < rest.length && rest[i].startsWith('-')) i += (rest[i] === '-n' || rest[i] === '--interval') ? 2 : 1
    if (rest.length > i && shellStringIsSend(rest.slice(i).join(' '), depth)) return true
  }
  if (prog === 'find') {
    for (let k = 0; k < rest.length; k++) {
      if (!FIND_EXEC.has(rest[k])) continue
      const sub = []
      for (const u of rest.slice(k + 1)) { if (u === ';' || u === '+') break; sub.push(u) }
      if (sub.length && segmentIsSend(sub, depth)) return true
    }
  }
  // eval re-parses its arguments, joined by spaces, as a command line -- same as bash -c.
  // At the recursion bound the text is out of sight: refuse (fail-closed).
  if (prog === 'eval' && rest.length) {
    if (depth >= 3) return true
    if (substitutionOutputSends(rest.join(' '), depth)) return true
    if (isSendInvocation(rest.join(' '), depth + 1)) return true
  }
  if (WRAPPER_SHELL.test(prog)) {
    // SHCLUSTER1126: the command string is located the way the shell does it (shellCommandString), so an
    // option cluster that carries c ('bash -lc ...', 'sh -ec ...') counts; it used to need a bare '-c' token.
    const cmdString = shellCommandString(rest, prog)
    if (cmdString !== null && substitutionOutputSends(cmdString, depth)) return true
    if (cmdString !== null) {
      // SHDEPTH1126: past the recursion bound the command string is out of sight. It used to be read as
      // "not a send", so four nested `bash -c` levels around sendmail passed both gates (measured: bash runs
      // the send). A non-empty command string at the bound is now refused (fail-closed), like a wrapper at the
      // head-depth bound and an eval at the same bound.
      if (depth >= 3) { if (cmdString.trim()) return true }
      else if (isSendInvocation(cmdString, depth + 1)) return true
    }
  }
  if (FISH_SHELL.test(prog)) {
    // WRAPSHELL1126: every -c / -C string fish would run. fish syntax is not sh syntax ('echo (sendmail x)' is a
    // command substitution there), so a sender NAMED anywhere in the string is a send (conservative; 0 fish calls
    // in the measured fleet corpus), on top of the sh-syntax reading. Same depth bound as the sh family.
    for (const s of fishCommandStrings(rest)) {
      if (depth >= 3) { if (s.trim()) return true; continue }
      if (segmentNamesSender(s.split(/[\s;|&()'"<>{}$`]+/).filter(Boolean))) return true
      if (isSendInvocation(s, depth + 1)) return true
    }
  }
  if (SCRIPT_INTERP.test(prog) && rest.some((a) => scriptCodeSends(a))) return true
  if (EDITOR.test(prog)) {
    for (let i = 0; i < rest.length; i++) {
      const t = rest[i]
      const cmd = (t === '-c' || t === '--cmd') ? rest[i + 1] : t.startsWith('+') ? t.slice(1) : null
      if (cmd && CODE_SENDER_LIT.test(cmd)) return true
    }
  }
  if (prog === 'git' && gitRunsSender(rest, depth)) return true
  // EXECVEC1126: curl speaks SMTP itself ('curl smtp://host --mail-rcpt a@b -T body').
  if (prog === 'curl' && rest.some((t, i) => /^(--url=)?smtps?:\/\//i.test(t) ||
    (t === '--url' && /^smtps?:\/\//i.test(rest[i + 1] || '')))) return true
  if (PYTHON.test(prog) || NODEISH.test(prog)) {
    for (let i = 0; i < rest.length; i++) {
      if ((rest[i] === '-c' || rest[i] === '-e' || rest[i] === '--eval') &&
          rest[i + 1] && codeStringSends(rest[i + 1])) return true
    }
  }
  const candidates = [prog]
  if ((PYTHON.test(prog) || NODEISH.test(prog)) && rest.length) candidates.push(basename(rest[0]))
  if (candidates.some((c) => SENDPY.test(c)) &&
      rest.some((t) => t === '--to' || t.startsWith('--to='))) return true
  if (toks.some((t) => GRAPHMAIL.test(basename(t))) && rest.includes('send')) return true
  if (CURLISH.test(prog) && rest.some((t) => RESEND_TARGET.test(t))) return true
  return false
}

// DQSUB1126 (PROGI msg 6001): command substitution inside DOUBLE quotes runs. The mask keeps quoted text as content,
// so 'echo "$(sendmail a)"', 'X="$(sendmail a)"', 'git log --grep "$(sendmail a)"' and 'echo "`sendmail a`"' passed
// both gates while bash ran the send. This returns the bodies of every $(...) and `...` that sit inside double
// quotes (single quotes stay literal; heredoc bodies are cut first, like the tokenizer does). Mirrors
// _dquote_substitutions in outgoing-copy-gate.py.
// HEREDOCSUB1126 (PROGI msg 6028, BELA msg 6029): the body of an UNQUOTED heredoc ('<<EOF', not '<<'EOF'' /
// '<<"EOF"' / '<<\\EOF') is expanded for ANY command: 'cat <<EOF ... $(sendmail a) ... EOF' runs the send. Quotes
// inside such a body are plain characters, so only \\, `...` and $(...) count. Mirrors _heredoc_substitutions.
function heredocSubstitutions(cmd) {
  const out = []
  for (const m of cmd.matchAll(HEREDOC_RE)) {
    if (/^<<-?\s*['"\\]/.test(m[1])) continue
    const body = m[0].slice(m[1].length + 1, m[0].length - m[4].length)
    for (let i = 0; i < body.length; i++) {
      const ch = body[i]
      if (ch === '\\') { i++; continue }
      if (ch === '`') {
        let j = i + 1
        while (j < body.length && body[j] !== '`') { if (body[j] === '\\') j++; j++ }
        out.push(body.slice(i + 1, j))
        i = j
        continue
      }
      if (ch === '$' && body[i + 1] === '(') {
        let j = i + 2
        let level = 1
        let iq = null
        while (j < body.length && level > 0) {
          const c = body[j]
          if (iq) { if (c === '\\' && iq === '"') j++; else if (c === iq) iq = null }
          else if (c === "'" || c === '"') iq = c
          else if (c === '\\') j++
          else if (c === '(') level++
          else if (c === ')') level--
          j++
        }
        out.push(body.slice(i + 2, level === 0 ? j - 1 : j))
        i = j - 1
      }
    }
  }
  return out
}

function dquoteSubstitutions(cmd, anyContext = false) {
  const s = cmd.replace(HEREDOC_RE, '$1')
  const out = anyContext ? [] : heredocSubstitutions(cmd)
  let q = null
  for (let i = 0; i < s.length; i++) {
    const ch = s[i]
    if (q === "'") { if (ch === "'") q = null; continue }
    if (ch === '\\' && i + 1 < s.length) { i++; continue }
    if (q === null) { if (ch === "'" || ch === '"') { q = ch; continue } if (!anyContext) continue }
    // inside double quotes (or, with anyContext, unquoted too)
    if (ch === '"') { q = null; continue }
    if (ch === '`') {
      let j = i + 1
      while (j < s.length && s[j] !== '`') { if (s[j] === '\\') j++; j++ }
      out.push(s.slice(i + 1, j))
      i = j
      continue
    }
    if (ch === '$' && s[i + 1] === '(') {
      let j = i + 2
      let level = 1
      let iq = null
      while (j < s.length && level > 0) {
        const c = s[j]
        if (iq) { if (c === '\\' && iq === '"') j++; else if (c === iq) iq = null }
        else if (c === "'" || c === '"') iq = c
        else if (c === '\\') j++
        else if (c === '(') level++
        else if (c === ')') level--
        j++
      }
      out.push(s.slice(i + 2, level === 0 ? j - 1 : j))
      i = j - 1
      continue
    }
  }
  return out
}

// STDINSHELL1126 (PROGI msg 6001): a shell that reads its commands from STDIN runs text the segment rules never see:
// 'echo "sendmail a" | sh', 'printf ... | bash -s', 'git log --format=%s | sh' (the commit subject becomes a
// command), 'bash <<< "sendmail a"', 'source /dev/stdin <<< ...' and the body of 'bash <<EOF ... EOF'. Mirrors
// _stdin_shell_* in outgoing-copy-gate.py.
const STDIN_FILES = new Set(['/dev/stdin', '-', '/proc/self/fd/0', '/dev/fd/0'])
// Drop redirection words (and the separate target of a bare operator) so they are not read as a script operand.
function withoutRedirections(rest) {
  const out = []
  for (let i = 0; i < rest.length; i++) {
    const m = REDIR_RX.exec(rest[i])
    if (m) { if (m[1] === '') i++; continue }
    out.push(rest[i])
  }
  return out
}
// True when this head is a shell that takes its commands from stdin: no -c string, and no script operand (or -s).
function isStdinShellHead(h) {
  if (!h || !h.length) return false
  const prog = basename(h[0])
  const rest = withoutRedirections(h.slice(1))
  if (prog === 'source' || prog === '.') return STDIN_FILES.has(rest[0])
  if (FISH_SHELL.test(prog)) {
    if (fishCommandStrings(rest).length) return false
    return !rest.some((t) => !t.startsWith('-'))
  }
  if (!WRAPPER_SHELL.test(prog)) return false
  if (shellCommandString(rest, prog) !== null) return false
  const valueChars = KSH_FAMILY.test(prog) ? 'oOT' : 'oO'
  let sawS = false
  let i = 0
  while (i < rest.length) {
    const t = rest[i]
    if (t === '--' || t === '-') { i++; break }
    if (t === '+') { i++; continue }
    if (SHELL_LONG_WITH_VALUE.has(t)) { i += 2; continue }
    if (t.startsWith('--')) { i++; continue }
    if (t.length > 1 && (t[0] === '-' || t[0] === '+')) {
      if (t[0] === '-' && t.slice(1).includes('s')) sawS = true
      i += 1 + [...t.slice(1)].filter((ch) => valueChars.includes(ch)).length
      continue
    }
    break
  }
  return sawS || i >= rest.length || STDIN_FILES.has(rest[i])
}
// INTERPSTDIN1126 (PROGI msg 6034): python / node reading their PROGRAM from stdin ('python3 - <<EOF',
// 'python3 <<< "..."', 'node <<< "...execSync(...)"', 'echo ... | python3'): that text is code, judged by a code
// rule, never by the command-name rule (a heredoc script that mentions a sender is data).
// INTERPSTDIN2 (PROGI msg 6049, BELA msg 6050): the code rule is now whole-program and language-aware.
// - A language scanner (blankPython / blankJs) blanks string literals (comments are skipped, see SCANFIX1127), keeping
//   f-string {...} and template ${...} expressions as code. The old per-line regexes mis-read "'''" inside a plain string, a backtick
//   in a Python comment, an f-string call and a JS comment, and hid real code that way.
// - interpCodeSends: a mail call outside strings (CODE_SEND) sends; else a process starter or dynamic exec
//   outside strings (INTERP_EXECISH) anywhere in the program, plus a sender name or mail call ANYWHERE in it
//   (strings included), sends. The old same-line condition missed a call split over lines
//   ('subprocess.run(\n["sendmail", ...])') and a command passed in a variable ('c = "sendmail a"; os.system(c)').
//   Measured on the fleet's real interpreter-stdin texts (PROGI msg 6049: 2703): the only refusals are our own
//   gate-test harnesses (scripts that run subprocesses and name the sender); a script that only edits or prints
//   mail code stays allowed. A script that runs a subprocess AND names the sender for another purpose is refused:
//   the stated limit below.
// - stdinInterpreterLang: real option parsing, so '-X utf8', '-W ignore', '-Ximporttime', '-Wignore', '/dev/stdin'
//   and '/dev/fd/0' keep the stdin reading, and a -c / -e code that execs stdin ('exec(sys.stdin.read())',
//   'eval(require("fs").readFileSync(0))') makes stdin the program too.
// Mirrors _interp_code_sends / _blank_python / _blank_js / _stdin_interpreter_lang in outgoing-copy-gate.py.
const INTERP_EXECISH = /subprocess|\.system\s*\(|\bpopen\b|child_process|\bexec[A-Za-z]*\s*\(|_exec[A-Za-z]*\s*\(|\bspawn[A-Za-z]*\s*\(|\beval\s*\(|__import__|\bexecSync\b|\bexecFile\b|\bspawnSync\b/i
const isIdentChar = (c) => c !== undefined && /[A-Za-z0-9_$]/.test(c)
// f-string / template expressions nest by recursion; past this depth the scanners throw and interpCodeSends counts
// the program as a send (fail-closed, and the same verdict in both twins regardless of their stack sizes).
const SCAN_NEST_MAX = 100
// SCANFIX1127 (PROGI msg 6118 (a) A-E, BELA msg 6120): the first scanners read some code differently from python 3.14 /
// node 22, and a real call then looked like a comment, string or regex -- 10 sends production refused were allowed.
// A) Comments are SKIPPED but NOT blanked: a quote inside a comment still opens nothing, but code in it stays visible,
//    as production read it (a call that only sits in a comment is refused again; it had no measured benefit and the
//    blanking was the whole comment-desync class: '{255:#x}', '# x<CR>call', '// x<U+2028>call').
// B) Python f/t-string replacement field: at its top level a ':' or a '!' (not '!=') ends the expression; the
//    conversion / format spec up to the closing '}' is string text ('{1:'>5}', '{255:#x}'), nested {...} in it is code.
// C) Line ends: Python \n, \r (and \r\n); JS \n, \r, U+2028, U+2029 end a comment / one-line string / regex literal
//    (U+2028/9 are allowed inside JS string literals, so they do not end those).
// D) JS regex or division: the previous significant character is found across line ends ('4\n/ 2' divides), after a
//    postfix ++ / -- a '/' divides, after return/typeof/... it starts a regex; and JS is read TWICE, with that rule and
//    with every '/' a division -- a send seen in either reading counts, so one wrong guess cannot hide a call.
// E) String prefixes: r b u f t in any case and order (t/T/tr/rt are python 3.14 template strings, fields like f).
const PY_EOL = (c) => c === '\n' || c === '\r'
const JS_EOL = (c) => c === '\n' || c === '\r' || c === '\u2028' || c === '\u2029'
// JS whitespace, spelled out (the same set as the JS \s class) so the python twin can mirror it exactly.
const JS_WS = /[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]/
const JS_REGEX_AFTER_WORD = new Set(['return', 'typeof', 'instanceof', 'in', 'of', 'new', 'delete', 'void', 'throw',
  'case', 'do', 'else', 'yield', 'await'])
// Python: returns the code with string-literal text turned into spaces (newlines kept, same length). Comments are
// skipped, not blanked (A).
function blankPython(code) {
  const out = code.split('')
  const n = code.length
  const blank = (a, b) => { for (let k = a; k < b && k < n; k++) if (out[k] !== '\n' && out[k] !== '\r') out[k] = ' ' }
  let nest = 0
  // scan code from i; with a stop char, return at that char on bracket depth 0 (a field's closing '}'); inside a
  // replacement field (fexpr), also at a top-level ':' or '!' that starts the conversion / format spec (B).
  function scan(i, stop, fexpr) {
    let depth = 0
    while (i < n) {
      const c = code[i]
      if (c === '#') { while (i < n && !PY_EOL(code[i])) i++; continue }
      if (c === "'" || c === '"') { i = str(i); continue }
      if (stop) {
        if (c === '(' || c === '[' || c === '{') depth++
        else if (c === ')' || c === ']' || c === '}') { if (depth === 0 && c === stop) return i; depth-- }
        else if (fexpr && depth === 0 && (c === ':' || (c === '!' && code[i + 1] !== '='))) return i
      }
      i++
    }
    return n
  }
  // one replacement field starting at k (just after its '{'): the expression is code, the spec after a top-level
  // ':' / '!' is text except nested {...} fields. Returns the index of the closing '}' (or n).
  function field(k) {
    if (++nest > SCAN_NEST_MAX) throw new Error('nesting')
    k = scan(k, '}', true)
    let segStart = k
    while (k < n && code[k] !== '}') {
      if (code[k] === '{') { blank(segStart, k + 1); k = field(k + 1); segStart = k + 1; k++; continue }
      k++
    }
    blank(segStart, k)
    nest--
    return k
  }
  function str(i) {
    const q = code[i]
    let p = i
    while (p > 0 && /[rRbBuUfFtT]/.test(code[p - 1]) && i - p < 2) p--
    const isF = p < i && !isIdentChar(code[p - 1]) && /[fFtT]/.test(code.slice(p, i))
    const triple = code.startsWith(q + q + q, i)
    let k = i + (triple ? 3 : 1)
    let segStart = i
    while (k < n) {
      const ch = code[k]
      if (ch === '\\') { k += 2; continue }
      if (triple ? code.startsWith(q + q + q, k) : ch === q) { k += triple ? 3 : 1; break }
      if (!triple && PY_EOL(ch)) break
      if (isF && ch === '{') {
        if (code[k + 1] === '{') { k += 2; continue }
        blank(segStart, k + 1)
        k = field(k + 1)
        segStart = k
        k++
        continue
      }
      k++
    }
    blank(segStart, Math.min(k, n))
    return k
  }
  scan(0, null, false)
  return out.join('')
}
// JavaScript: strings, template literal text (keeping ${...}) and regex literals turned into spaces; comments skipped,
// not blanked (A). regexOn=false reads every '/' as a division (the second reading, D).
function blankJs(code, regexOn = true) {
  const out = code.split('')
  const n = code.length
  const blank = (a, b) => { for (let k = a; k < b && k < n; k++) if (!JS_EOL(out[k])) out[k] = ' ' }
  let nest = 0
  function regexStart(i) {
    if (!regexOn) return false
    let p = i - 1
    while (p >= 0 && JS_WS.test(code[p])) p--
    if (p < 0) return true
    const c = code[p]
    if ((c === '+' || c === '-') && code[p - 1] === c) return false
    if ('(,=:[!&|?{};+-*%<>~^'.includes(c)) return true
    if (isIdentChar(c)) {
      let s = p
      while (s > 0 && isIdentChar(code[s - 1])) s--
      return JS_REGEX_AFTER_WORD.has(code.slice(s, p + 1))
    }
    return false
  }
  function scan(i, stop) {
    let depth = 0
    while (i < n) {
      const c = code[i]
      if (c === '/' && code[i + 1] === '/') { while (i < n && !JS_EOL(code[i])) i++; continue }
      if (c === '/' && code[i + 1] === '*') { const e = code.indexOf('*/', i + 2); i = e < 0 ? n : e + 2; continue }
      if (c === '/' && regexStart(i)) {
        let k = i + 1
        let cls = false
        while (k < n && !JS_EOL(code[k])) {
          if (code[k] === '\\') { k += 2; continue }
          if (code[k] === '[') cls = true
          else if (code[k] === ']') cls = false
          else if (code[k] === '/' && !cls) break
          k++
        }
        blank(i, k + 1)
        i = k + 1
        continue
      }
      if (c === "'" || c === '"') {
        let k = i + 1
        while (k < n && code[k] !== c && code[k] !== '\n' && code[k] !== '\r') k += code[k] === '\\' ? 2 : 1
        blank(i, k + 1)
        i = k + 1
        continue
      }
      if (c === '`') {
        let k = i + 1
        let segStart = i
        while (k < n && code[k] !== '`') {
          if (code[k] === '\\') { k += 2; continue }
          if (code[k] === '$' && code[k + 1] === '{') {
            blank(segStart, k + 2)
            if (++nest > SCAN_NEST_MAX) throw new Error('nesting')
            k = scan(k + 2, '}')
            nest--
            segStart = k
            k++
            continue
          }
          k++
        }
        blank(segStart, k + 1)
        i = k + 1
        continue
      }
      if (stop) {
        if (c === '(' || c === '[' || c === '{') depth++
        else if (c === ')' || c === ']' || c === '}') { if (depth === 0 && c === stop) return i; depth-- }
      }
      i++
    }
    return n
  }
  scan(0, null)
  return out.join('')
}
function interpCodeSends(code, lang) {
  let bares
  try { bares = lang === 'js' ? [blankJs(code, true), blankJs(code, false)] : [blankPython(code)] } catch { return true }
  return bares.some((bare) => CODE_SEND.test(bare) ||
    (INTERP_EXECISH.test(bare) && (CODE_SENDER_LIT.test(code) || CODE_SEND.test(code))))
}
// A -c / -e program that executes what it reads from stdin makes stdin the program.
const STDIN_EXEC = /\b(?:exec|eval|compile|runpy|Function|runInThisContext|runInNewContext)\b/
const STDIN_READ = /stdin|\/dev\/fd\/0|readFileSync\s*\(\s*(?:0\b|['"]\/dev\/stdin)|\binput\s*\(/
const codeRunsStdin = (code) => typeof code === 'string' && STDIN_EXEC.test(code) && STDIN_READ.test(code)
const NODE_PROG = /^(node|nodejs)$/i
const NODE_WITH_VALUE = new Set(['-r', '--require', '--import', '--loader', '--experimental-loader', '-C', '--conditions',
  '--input-type', '--title', '--env-file', '--inspect-port', '--debug-port', '--redirect-warnings', '--disable-warning',
  '--watch-path', '--openssl-config', '--icu-data-dir', '--diagnostic-dir', '--report-dir', '--report-filename',
  '--secure-heap', '--secure-heap-min', '--stack-trace-limit', '--unhandled-rejections', '--trace-event-categories'])
// 'py' / 'js' when this head is python / node taking its PROGRAM from stdin; null otherwise.
function stdinInterpreterLang(h) {
  if (!h || !h.length) return null
  const prog = basename(h[0])
  const isPy = PYTHON.test(prog) || /^python3\.\d+$/i.test(prog)
  if (!isPy && !NODE_PROG.test(prog)) return null
  const lang = isPy ? 'py' : 'js'
  const rest = withoutRedirections(h.slice(1))
  for (let i = 0; i < rest.length; i++) {
    const t = rest[i]
    if (t === '--') return i + 1 >= rest.length || STDIN_FILES.has(rest[i + 1]) ? lang : null
    if (t === '-') return lang
    if (isPy) {
      if (t.startsWith('--')) {
        if (t.startsWith('--help') || t === '--version') return null
        if (t === '--check-hash-based-pycs') i++
        continue
      }
      if (t.startsWith('-')) {
        for (let k = 1; k < t.length; k++) {
          const ch = t[k]
          if (ch === 'c') return codeRunsStdin(k + 1 < t.length ? t.slice(k + 1) : rest[i + 1]) ? lang : null
          if (ch === 'm' || ch === 'h' || ch === 'V' || ch === '?') return null
          if (ch === 'X' || ch === 'W') { if (k + 1 >= t.length) i++; break }
        }
        continue
      }
    } else {
      if (t === '-e' || t === '--eval' || t === '-p' || t === '--print') return codeRunsStdin(rest[i + 1]) ? lang : null
      if (/^--(eval|print)=/.test(t)) return codeRunsStdin(t.slice(t.indexOf('=') + 1)) ? lang : null
      if (/^-[A-Za-z]{2,}$/.test(t) && /[ep]/.test(t)) return codeRunsStdin(rest[i + 1]) ? lang : null
      if (t === '-c' || t === '--check' || t === '-h' || t === '--help' || t === '-v' || t === '--version') return null
      if (NODE_WITH_VALUE.has(t)) { i++; continue }
      if (t.startsWith('-')) continue
    }
    return STDIN_FILES.has(t) ? lang : null
  }
  return lang
}
// The feeders of segment k through pipes: the pipeline to its left, and (GROUPPIPE1126) a '{ ...; }' group
// closing right before the pipe, back to the segment that opens it. Returns [left texts, feeder token lists].
function pipeFeeders(segs, k) {
  let j = k - 1
  const left = []
  const feeders = []
  const isPipe = (sep) => sep.endsWith('|') && !sep.endsWith('||')
  while (j >= 0 && isPipe(segs[j][1])) { left.unshift(segs[j][0].join(' ')); feeders.unshift(segs[j][0]); j-- }
  // GROUPPIPE1126 (PROGI msg 6034): '{ echo X; } | bash' -- the segment before the pipe is the closing '}', the
  // group's commands sit in the segments back to the one that opens with '{'. They all feed the shell.
  if (feeders.length && feeders[0].length === 1 && feeders[0][0] === '}') {
    while (j >= 0) {
      left.unshift(segs[j][0].join(' '))
      feeders.unshift(segs[j][0])
      if (segs[j][0][0] === '{') break
      j--
    }
  }
  return [left, feeders]
}
// echo / printf arguments as the text they print (the -n/-e/-E flags dropped); null for any other command.
function echoLiteral(toks) {
  const h = commandHeads(toks)[0]
  if (!h || !(basename(h[0]) === 'echo' || basename(h[0]) === 'printf')) return null
  return h.slice(1).filter((t) => !/^-[neE]+$/.test(t)).join(' ')
}

// [segment tokens, separator that ENDED it] -- the tokenizer above, keeping '|' apart from '||' and the rest.
function segmentsWithSeparators(cmd) {
  const s = maskSubshellMarkers(cmd.replace(HEREDOC_RE, '$1'))
  const out = []
  let cur = []
  let tok = ''
  let hasTok = false
  let q = null
  const pushTok = () => { if (hasTok) { cur.push(tok); tok = ''; hasTok = false } }
  // An empty segment (a run of operators, ')|') adds its separator to the previous one, as the python twin's shlex
  // returns such a run as one token.
  const pushSeg = (sep) => { pushTok(); if (!cur.length && out.length) out[out.length - 1][1] += sep; else out.push([cur, sep]); cur = [] }
  for (let i = 0; i < s.length; i++) {
    const ch = s[i]
    if (q) {
      if (ch === '\\' && q === '"' && i + 1 < s.length) { tok += s[++i]; continue }
      if (ch === q) { q = null; continue }
      tok += ch
      continue
    }
    if (ch === "'" || ch === '"') { q = ch; hasTok = true; continue }
    if (ch === '\\' && i + 1 < s.length) { tok += s[++i]; hasTok = true; continue }
    if (ch === ' ' || ch === '\t') { pushTok(); continue }
    if (ch === '|' && s[i + 1] === '|') { pushSeg('||'); i++; continue }
    if ('|&;()'.includes(ch)) { pushSeg(ch); continue }
    tok += ch
    hasTok = true
  }
  if (q) throw new Error('unbalanced quote')
  pushSeg('')
  return out
}
// The command texts a stdin shell would run, plus the texts it reads from a pipe (judged by name only: the output
// is made at run time). Returns { run: [...], piped: [...] }.
function stdinShellTexts(cmd) {
  const run = []
  const piped = []
  const code = []
  let segs
  try { segs = segmentsWithSeparators(cmd) } catch { return { run, piped, code } }
  for (let k = 0; k < segs.length; k++) {
    const [toks, sepK] = segs[k]
    if (!toks.length) continue
    // PROCSUB1126 (PROGI msg 6034): 'source <(...)', '. <(...)', 'bash <(...)', 'bash < <(...)' run the OUTPUT of
    // the process substitution as a script. The tokenizer leaves '<' as the last word and '(' as the separator.
    if (toks.length >= 2 && toks[toks.length - 1] === '<' && sepK === '(') {
      let base = toks.slice(0, -1)
      if (base[base.length - 1] === '<') base = base.slice(0, -1)
      const ph = commandHeads(base)[0]
      const pp = ph && ph.length ? basename(ph[0]) : ''
      const pl = stdinInterpreterLang(ph)
      if (pl || pp === 'source' || pp === '.' || ((WRAPPER_SHELL.test(pp) || FISH_SHELL.test(pp)) &&
          shellCommandString(withoutRedirections(ph.slice(1)), pp) === null)) {
        const body = []
        let depth = 1
        for (let m2 = k + 1; m2 < segs.length && depth > 0; m2++) {
          body.push(segs[m2][0])
          depth += (segs[m2][1].match(/\(/g) || []).length - (segs[m2][1].match(/\)/g) || []).length
        }
        if (pl) {
          // INTERPSTDIN2: 'python3 <(echo ...)', 'python3 < <(echo ...)': an echo/printf literal is the program
          // (code rule); any other command in it makes the program at run time (name rule on its text).
          for (const b of body) { const lit = echoLiteral(b); if (lit !== null) code.push([lit, pl]); else piped.push(b.join(' ')) }
        } else {
          piped.push(body.map((b) => b.join(' ')).join(' ; '))
          for (const b of body) { const lit = echoLiteral(b); if (lit !== null) run.push(lit) }
        }
      }
    }
    const head = commandHeads(toks)[0]
    const lang = stdinInterpreterLang(head)
    if (lang) {
      for (let i = 0; i < toks.length; i++) {
        if (toks[i] === '<<<' && i + 1 < toks.length) code.push([toks[i + 1], lang])
        else if (toks[i].startsWith('<<<') && toks[i].length > 3) code.push([toks[i].slice(3), lang])
      }
      // INTERPSTDIN2: the whole pipeline to its left feeds it ('echo ... | tee f | python3', '{ echo ...; } | python3'):
      // echo/printf literals are the program (code rule); another feeder makes it at run time (name rule on its
      // text, like a stdin shell's); a heredoc-fed 'cat' is read in the heredoc pass below.
      const [, feeders] = pipeFeeders(segs, k)
      for (const f of feeders) {
        const lit = echoLiteral(f)
        if (lit !== null) code.push([lit, lang])
        else piped.push(f.join(' '))
      }
      continue
    }
    if (!isStdinShellHead(head)) continue
    // here-string: '<<<' WORD or '<<<WORD'
    for (let i = 0; i < toks.length; i++) {
      if (toks[i] === '<<<' && i + 1 < toks.length) run.push(toks[i + 1])
      else if (toks[i].startsWith('<<<') && toks[i].length > 3) run.push(toks[i].slice(3))
    }
    // a pipe into it: the whole pipeline to its left
    const [left, feeders] = pipeFeeders(segs, k)
    if (left.length) {
      piped.push(left.join(' | '))
      for (const f of feeders) { const lit = echoLiteral(f); if (lit !== null) run.push(lit) }
    }
  }
  // a heredoc fed to a stdin shell: its body is the command text
  for (const m of cmd.matchAll(HEREDOC_RE)) {
    const lineStart = cmd.lastIndexOf('\n', m.index) + 1
    let headSegs
    try { headSegs = segmentsTokens(cmd.slice(lineStart, m.index)) } catch { continue }
    const last = headSegs[headSegs.length - 1]
    const body = m[0].slice(m[1].length + 1, m[0].length - m[4].length)
    if (last && isStdinShellHead(commandHeads(last)[0])) { run.push(body); continue }
    const lastLang = last ? stdinInterpreterLang(commandHeads(last)[0]) : null
    if (lastLang) { code.push([body, lastLang]); continue }
    // 'cat <<EOF | bash' / 'cat <<EOF | python3': the heredoc feeds a command whose output is piped into a stdin
    // shell or interpreter on the same line (INTERPSTDIN2: through any pipe chain, 'cat <<EOF | tee f | python3').
    let hs
    try { hs = segmentsWithSeparators(cmd.slice(lineStart, m.index + m[1].length)) } catch { continue }
    const isPipe = (sep) => sep.endsWith('|') && !sep.endsWith('||')
    let f = hs.length - 1
    while (f >= 0 && !hs[f][0].some((t) => t.startsWith('<<'))) f--
    if (f < 0 || !last) continue
    let g = f
    let sink = null
    while (g < hs.length - 1 && isPipe(hs[g][1])) {
      g++
      const gh = commandHeads(hs[g][0])[0]
      if (isStdinShellHead(gh)) { sink = 'sh'; break }
      const gl = stdinInterpreterLang(gh)
      if (gl) { sink = gl; break }
    }
    // INTERPSTDIN2: 'python3 < <(cat <<EOF ... EOF)', 'source <(cat <<EOF ... EOF)': the heredoc-fed command opens a
    // process substitution that a stdin shell / interpreter reads as its program.
    if (!sink && f > 0 && hs[f - 1][1] === '(' && hs[f - 1][0][hs[f - 1][0].length - 1] === '<') {
      let base = hs[f - 1][0].slice(0, -1)
      if (base[base.length - 1] === '<') base = base.slice(0, -1)
      const ph = commandHeads(base)[0]
      const pp = ph && ph.length ? basename(ph[0]) : ''
      sink = stdinInterpreterLang(ph) || ((pp === 'source' || pp === '.' || ((WRAPPER_SHELL.test(pp) || FISH_SHELL.test(pp)) &&
        shellCommandString(withoutRedirections(ph.slice(1)), pp) === null)) ? 'sh' : null)
    }
    if (!sink) continue
    const feeder = commandHeads(hs[f][0])[0]
    if (!(feeder && basename(feeder[0]) === 'cat')) piped.push(body)
    else if (sink === 'sh') run.push(body)
    else code.push([body, sink])
  }
  return { run, piped, code }
}

export function isSendInvocation(cmd, depth = 0) {
  const stdin = stdinShellTexts(cmd)
  if (stdin.piped.some((t) => CODE_SENDER_LIT.test(t))) return true
  if (stdin.code.some(([t, l]) => interpCodeSends(t, l))) return true
  for (const text of stdin.run) {
    if (depth >= 3) { if (text.trim()) return true } else if (isSendInvocation(text, depth + 1)) return true
  }
  for (const inner of dquoteSubstitutions(cmd)) {
    if (depth >= 3) { if (inner.trim()) return true } else if (isSendInvocation(inner, depth + 1)) return true
  }
  let segments
  try {
    segments = segmentsTokens(cmd)
  } catch {
    // Unparseable command: fall back to the LEGACY content patterns, so on
    // this path the gate is exactly as strict as before -- never weaker.
    return SEND_PATTERNS.some((re) => re.test(cmd))
  }
  if (configEnvSends(segments, depth)) return true
  return segments.some((toks) => segmentIsSend(toks, depth))
}

// Outbound-shaped operations of the multiplexed manage_email tool. Each of
// these sends for real unless the call explicitly asks for a draft.
const MANAGE_EMAIL_SEND_OPS = new Set(['send', 'reply', 'replyall', 'forward'])

// Pure decision: does this tool call send (or attempt to send) email?
// Returns { deny, kind? }. `kind` selects the deny wording at the hook
// entrypoint: 'draft-required' is the manage_email case (drafting is fine,
// only the actual send is refused), everything else is the sub-agent
// governance block.
// MAILCRED1127 (BELA msg 6120, PROGI msg 6118 (e)): this host has a CONFIGURED mail path the shell-form rules never
// knew: scripts/send-gmail.py (and the outreach scripts send_outreach.py / send_batch_daily.py) fetch a mail
// account's SMTP app password from the dashboard vault (GET /api/vault/<id>, authorized by the dashboard token every
// agent can read) and send through smtplib. 'python3 scripts/send-gmail.py --to ...' passed this gate directly.
// Sub-agent only (this gate is not wired on the main agent), and deliberately NOT in isSendInvocation: the Python
// twin of that contract decides when the MAIN agent's copy-QA gate runs, and treating these scripts as sends there
// would put the main agent's own file-bodied sends behind its fail-closed "unreadable body" block.
// (1) Running one of the mailer scripts, in any spelling the send-invocation rules understand (path, ./, env, sh -c,
//     interpreter code, heredoc, pipes...): the script name is read as a sender name for that check, so reading the
//     file ('cat', 'grep', 'sed -n') stays allowed while every way of running it is refused.
// (2) Touching the credential: either SMTP credential's vault id anywhere in the command (curl, python -c, a heredoc,
//     node importing vault.js), or the secret-value endpoint /api/vault/<id> used as an endpoint (after a host or
//     port, or followed by an id character). A mention like grep "/api/vault/" or "/api/vault/<id>" stays allowed.
// Stated limit: this is a name rule, not an access control. A copied/renamed script or another vault id read through
// vault.js is not seen; the real control is restricting the vault value route to the main agent (separate task).
// The bare name is enough: the replacement keeps any directory in front, and the send rules judge the basename.
const MAILER_SCRIPT_RX = /(?:send-gmail|send_outreach|send_batch_daily)\.py(?![\w.-])/g
// MAILCRED1127 fixup (PROGI msg 6134 (3), BELA msg 6135): this host holds TWO SMTP credentials in the vault -- the
// Gmail app password and the woowebsite.eu mailbox password ("info@woowebsite.eu jelszo" with an accented o, the
// default key of send_outreach.py). Both ids are matched in every spelling seen in commands: plain (the prefix up to
// "jelsz" also covers a ó escape), and URL-encoded (%40 for @, %20 or + for the space), as urllib.parse.quote
// and curl --data-urlencode write them.
const MAIL_CREDENTIAL_IDS_RX = /gmail_kiss_i_god_app_pw|info(?:@|%40)woowebsite\.eu(?: |%20|\+)jelsz/i
const MAIL_CREDENTIAL_RX = new RegExp(MAIL_CREDENTIAL_IDS_RX.source +
  /|(?:https?:\/\/|localhost|127\.0\.0\.1|\[::1\]|:\d{2,5})[^\s'"`]*\/api\/vault\/|\/api\/vault\/[A-Za-z0-9_%$.@{-]/.source, 'i')
// 'sed -n 1,40p FILE...' only prints: sed is not a never-exec program for the name layer (its e/w commands run and
// write), but this exact shape -- -n, one line-range print script, plain file operands -- cannot. It is how the fleet
// reads these scripts (measured on the real corpus), so for the mailer-script check alone it is taken out first.
const SED_PRINT_RX = /(^|[;&|(\n]|\s)sed\s+-n\s+(['"]?)[\d,$]+p\2((?:[ \t]+[^\s;&|<>()`$'"\\]+)+)/g
// A mailer-script name inside a command substitution ('$(echo "python3 send_batch_daily.py 1")', backticks): the
// substitution's OUTPUT may be run as a command (the documented open shell form "a substitution in command
// position"), and reading a script never needs this shape -- so for these scripts it is refused outright
// (PROGI msg 6122 probe: the only 2 of 22 forms that still passed). Mirrors nothing: MAILCRED is JS-gate only.
const MAILER_IN_SUBST_RX = /\$\([^)]*(?:send-gmail|send_outreach|send_batch_daily)\.py|`[^`]*(?:send-gmail|send_outreach|send_batch_daily)\.py/
export function runsMailerScript(cmd) {
  const s = String(cmd ?? '')
  if (!s.match(MAILER_SCRIPT_RX)) return false
  // A quoted heredoc body ('<<'EOF'', '<<"EOF"', '<<\\EOF') is data: markdown there names the scripts in backticks.
  // An unquoted body expands $(...) and backticks, so it stays in.
  const noQuotedBodies = s.replace(HEREDOC_RE, (m0, intro) => (/^<<-?[ \t]*['"\\]/.test(intro) ? intro : m0))
  if (MAILER_IN_SUBST_RX.test(noQuotedBodies)) return true
  return isSendInvocation(s.replace(SED_PRINT_RX, '$1true').replace(MAILER_SCRIPT_RX, 'sendmail'))
}
export function touchesMailCredential(cmd) {
  return MAIL_CREDENTIAL_RX.test(String(cmd ?? ''))
}

export function gateDecision(toolName, toolInput) {
  const name = String(toolName ?? '')
  // Any MCP send_email tool, name-agnostic (gmail or a differently-named
  // server in a customer install -> the matcher + this both key on send_email).
  if (/send_email/i.test(name)) return { deny: true }
  // @aaronsb/google-workspace-mcp multiplexes read, draft and send behind one
  // manage_email tool, so the tool NAME cannot decide this one -- the operation
  // plus the draft flag can. This is what replaces the server's own
  // draft-only-email safety policy, which blocks drafting too: that policy keys
  // on ctx.operation alone and never looks at draft:true, so with it enabled the
  // mailbox is effectively read-only (verified live, 2026-08-10).
  if (/(^|__)manage_email$/i.test(name)) {
    const op = String(toolInput?.operation ?? '').toLowerCase()
    if (!MANAGE_EMAIL_SEND_OPS.has(op)) return { deny: false }
    // Fail safe: only an explicit draft request passes. A missing/ambiguous
    // flag is treated as a real send, even though the server would itself
    // force a draft when attachments are present.
    const draft = toolInput?.draft
    if (draft === true || draft === 'true') return { deny: false }
    return { deny: true, kind: 'draft-required' }
  }
  if (name === 'Bash') {
    const cmd = String(toolInput?.command ?? '')
    if (isSendInvocation(cmd)) return { deny: true }
    if (touchesMailCredential(cmd)) return { deny: true, kind: 'mail-credential' }
    if (runsMailerScript(cmd)) return { deny: true }
  }
  return { deny: false }
}

// MAILCRED1127 (PROGI msg 6118 (d)): the hook entry point's decision. An unexpected exception inside the decision used
// to exit 1, which PreToolUse treats as NON-blocking, so the call ran unchecked. Any throw is a deny (fail-closed).
export function hookDecision(payload, decide = gateDecision) {
  try {
    return decide(payload?.tool_name, payload?.tool_input)
  } catch {
    return { deny: true }
  }
}

// MAILCRED1127: deny wording when a sub-agent reaches for the mail credential itself.
export function buildCredentialMsg(botName) {
  return (
    'A levelezo hitelesito adat (vault) es a levelkuldo szkriptek sub-agentbol tiltottak (governance hard-gate). ' +
    `Kimeno levelet csak ${botName} kuld: kuldd neki a tervezett levelet (CIMZETT + TARGY + TELJES SZOVEG) ` +
    'inter-agent uzenetben. Ha a vault-hozzaferes mas, nem levelezo celra kell, azt is neki jelezd.'
  )
}

// Deny wording for the manage_email case. Unlike the sub-agent governance
// block this is not about who the agent is: drafting stays open, so the fix is
// to re-issue the same call with draft: true and hand the draft to the owner.
export function buildDraftOnlyMsg(ownerName) {
  return (
    'Kimeno email kuldese tiltott (draft-kapu). ' +
    'Ird meg ugyanezt draft: true kapcsoloval, es jelezd a tulajdonosnak ' +
    `(${ownerName}), hogy a Gmail piszkozatok kozott varja a jovahagyasat. ` +
    'Csak VERIFIKALT cimre. A kuldes gombot ember nyomja meg.'
  )
}

// Pure builder for the deny message, so the brand/owner substitution is
// provable without spawning the hook. With the stock defaults (botName
// 'Marveen', ownerName 'Szabolcs') the wording is byte-identical to before.
export function buildGateMsg(botName, ownerName) {
  return (
    'Email-kuldes sub-agentkent tiltott (governance hard-gate). ' +
    `Kuldd a tervezett emailt (CIMZETT + TARGY + TELJES SZOVEG) ${botName}nek inter-agent uzenetben ` +
    `jovahagyasra; a kimeno emailt ${botName} kuldi. Csak VERIFIKALT cimre (soha nem nevbol talalt cim). ` +
    `Soha ne irj ala ${ownerName} nevevel, es soha ne kerj penzt senki neveben.`
  )
}

// Resolve the brand + owner display names for the deny message. The hook runs
// standalone (no config.ts), so read the install's .env directly, keyed off
// this file's own location (<root>/scripts/email-send-gate.mjs -> <root>/.env).
// Any failure falls back to the stock defaults, so the gate never breaks and a
// bare install keeps the original wording.
export function readBrandEnv(readFile = (p) => readFileSync(p, 'utf-8')) {
  const fallback = { botName: 'Marveen', ownerName: 'Szabolcs' }
  try {
    const envPath = join(dirname(fileURLToPath(import.meta.url)), '..', '.env')
    const raw = readFile(envPath)
    const pick = (key) => {
      const m = raw.match(new RegExp(`^\\s*${key}\\s*=\\s*(.*)$`, 'm'))
      if (!m) return ''
      return m[1].trim().replace(/^["']|["']$/g, '').trim()
    }
    return {
      botName: pick('BOT_NAME') || fallback.botName,
      ownerName: pick('OWNER_NAME') || fallback.ownerName,
    }
  } catch {
    return fallback
  }
}

function allow() { process.exit(0) }

function deny(reason) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: reason,
    },
  }))
  process.exit(0)
}

// Run as the hook entrypoint only when invoked directly (not when imported by a
// test). Reads the PreToolUse payload from stdin and emits a deny decision for
// email-send tool calls. realpath both sides so a symlinked install path (the
// hook command is an absolute path that may traverse a symlink, e.g. /tmp ->
// /private/tmp on macOS, or a symlinked /home on Linux) still matches -- a raw
// url-vs-argv compare would silently no-op the gate (a security bypass).
function isInvokedDirectly() {
  try {
    const self = realpathSync(fileURLToPath(import.meta.url))
    const entry = process.argv[1] ? realpathSync(process.argv[1]) : ''
    return self === entry
  } catch {
    return false
  }
}
if (isInvokedDirectly()) {
  let payload
  try {
    payload = JSON.parse(readFileSync(0, 'utf-8'))
  } catch {
    allow() // malformed/empty input must never break the agent's tool calls
  }
  // MAILCRED1127: hookDecision turns any throw into a deny; the brand lookup has its own fallbacks.
  const decision = hookDecision(payload)
  if (decision.deny) {
    let names = { botName: 'Marveen', ownerName: 'Szabolcs' }
    try { names = readBrandEnv() } catch { /* stock names */ }
    deny(decision.kind === 'draft-required' ? buildDraftOnlyMsg(names.ownerName)
      : decision.kind === 'mail-credential' ? buildCredentialMsg(names.botName)
        : buildGateMsg(names.botName, names.ownerName))
  }
  allow()
}

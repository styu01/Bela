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
const HEREDOC_RE = /(<<-?\s*'?(\w+)'?[^\n]*)\n[\s\S]*?\n\2(?=\s|$)/g
const ENV_ASSIGN = /^[A-Za-z_][A-Za-z_0-9]*=/
const SENDER_PROG = /^(sendmail|msmtp|swaks)$/i
const SENDPY = /^send\.py$/i
const PYTHON = /^python3?$/i
const NODEISH = /^(node|tsx|ts-node|deno|bun|npx)$/i
const GRAPHMAIL = /^graph-mail(\.ts|\.js)?$/i
const WRAPPER_SHELL = /^(sh|bash|zsh|dash)$/i
const CURLISH = /^(curl|wget|http)$/i
const RESEND_TARGET = /^(https?:\/\/)?([^/@\s]*\.)?api\.resend\.com(\/|$)/i
const CODE_SEND = /\bsmtplib\b|SMTP\s*\(|\bsendMail\s*\(|\bsendEmail\b|\bmail\.send\b/i
// Naive-shape exec heuristic (msg 14298): process-spawn AND a known mailer
// name together in one interpreter code string. Covers the accidental shapes;
// see the STATED LIMIT in the header for what it deliberately does not claim.
const CODE_EXECISH = /\bsubprocess\b|os\.system|\bpopen\b|child_process|\bexec[A-Za-z]*\s*\(|\bspawn[A-Za-z]*\s*\(/i
const CODE_SENDER_LIT = /sendmail|msmtp|swaks|send\.py/i
const codeStringSends = (code) => CODE_SEND.test(code) || (CODE_EXECISH.test(code) && CODE_SENDER_LIT.test(code))

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
  'false', 'cd', 'pwd', ':', 'id', 'whoami',
])
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

function segmentIsSend(toksIn, depth) {
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
  const progs = heads.filter((h) => h && h.length).map((h) => basename(h[0]))
    .filter((p) => p !== 'eval' && p !== 'find' && !WRAPPERS.has(p))
  return progs.length > 0 && !progs.every((p) => NON_EXEC_PROGS.has(p))
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
// cluster ('-lc', '-ec', '-xc'). '-o NAME' / '+o NAME' / '-O NAME' / '+O NAME' take a value, '--long' options
// stand alone, '--' ends the options. Mirrors _shell_command_string in outgoing-copy-gate.py.
function shellCommandString(rest) {
  let sawC = false
  let i = 0
  while (i < rest.length) {
    const t = rest[i]
    if (t === '--') { i++; break }
    if (t === '-o' || t === '+o' || t === '-O' || t === '+O') { i += 2; continue }
    if (t.startsWith('--')) { i++; continue }
    if (t.length > 1 && (t[0] === '-' || t[0] === '+')) {
      if (t[0] === '-' && t.slice(1).includes('c')) sawC = true
      i++
      continue
    }
    break
  }
  return sawC && i < rest.length ? rest[i] : null
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
    if (isSendInvocation(rest.join(' '), depth + 1)) return true
  }
  if (WRAPPER_SHELL.test(prog)) {
    // SHCLUSTER1126: the command string is located the way the shell does it (shellCommandString), so an
    // option cluster that carries c ('bash -lc ...', 'sh -ec ...') counts; it used to need a bare '-c' token.
    const cmdString = shellCommandString(rest)
    if (cmdString !== null) {
      // SHDEPTH1126: past the recursion bound the command string is out of sight. It used to be read as
      // "not a send", so four nested `bash -c` levels around sendmail passed both gates (measured: bash runs
      // the send). A non-empty command string at the bound is now refused (fail-closed), like a wrapper at the
      // head-depth bound and an eval at the same bound.
      if (depth >= 3) { if (cmdString.trim()) return true }
      else if (isSendInvocation(cmdString, depth + 1)) return true
    }
  }
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

export function isSendInvocation(cmd, depth = 0) {
  let segments
  try {
    segments = segmentsTokens(cmd)
  } catch {
    // Unparseable command: fall back to the LEGACY content patterns, so on
    // this path the gate is exactly as strict as before -- never weaker.
    return SEND_PATTERNS.some((re) => re.test(cmd))
  }
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
  }
  return { deny: false }
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
  const { deny: shouldDeny, kind } = gateDecision(payload?.tool_name, payload?.tool_input)
  if (shouldDeny) {
    const { botName, ownerName } = readBrandEnv()
    deny(kind === 'draft-required' ? buildDraftOnlyMsg(ownerName) : buildGateMsg(botName, ownerName))
  }
  allow()
}

// Skill scan: the review half of upstream's skill quarantine (MARVEENGAPS1020 (b)), ported for the agent package
// installer only (BELA 6404). The upstream module also holds the pending area, the approve / go-live paths and the
// SKILL_QUARANTINE switch; this fork has none of those, and the agent package path does not need them: a package
// agent's skills sit in its own .claude/skills and are loaded only when that agent runs, and a package agent does
// not run before its owner approval (agent-package/approval.ts). What is here, unchanged from upstream: the file
// walk, the text review, the scan patterns and scanSkillDir, the global root, and the audit line.
//
// What the scan is, and what it is not: no text classifier is a security boundary for prompt injection. The scan
// is a TRIAGE list for the human who approves, never an auto-verdict. An empty list does not mean safe.

import { createHash } from 'node:crypto'
import { appendFileSync, lstatSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, relative, sep } from 'node:path'
import { STORE_DIR } from '../config.js'
import { logger } from '../logger.js'

export interface ScanFile {
  path: string
  size: number
  kind: 'markdown' | 'script' | 'binary' | 'other'
  executable: boolean
  shebang?: string
  sha256: string
  // The FULL text of a non-binary file (#2088 review: the approver sees every
  // byte the hash binds). Absent for a binary file, which is shown by size
  // and sha256 only, and in the summary listing.
  text?: string
  // An image only: the readable runs between its bytes (#2088 delta 3). A
  // real PNG header with text behind it opens as a picture in the Read tool,
  // but `cat` hands that text to the model, so it is shown and scanned.
  strings?: string
}

export interface ScanFinding {
  file: string
  line: number
  pattern: string
  excerpt: string
}

export interface ScanReport {
  files: ScanFile[]
  findings: ScanFinding[]
  totalBytes: number
  // true when the bounds below stopped the scan: the approver is told the
  // list is incomplete instead of reading a short list as a clean one.
  truncated: boolean
  // true when the findings list hit its cap. Every file is still shown in
  // full, so this alone does not block the approval (#2088 review).
  findingsTruncated: boolean
  // Files the approver cannot read (a native executable, a PDF): their presence
  // makes the skill not approvable from the dashboard.
  unreviewable: string[]
}


const MAX_FILES = 300
const MAX_BYTES = 8 * 1024 * 1024
const MAX_FINDINGS = 200

export function globalSkillsRoot(): string {
  return join(homedir(), '.claude', 'skills')
}

// Every regular file under `root`, sorted, with its path relative to root.
function filesUnder(root: string): string[] {
  const out: string[] = []
  const rec = (dir: string) => {
    let names: string[]
    try { names = readdirSync(dir).sort() } catch { return }
    for (const n of names) {
      const abs = join(dir, n)
      let st
      try { st = lstatSync(abs) } catch { continue }
      if (st.isDirectory()) rec(abs)
      else out.push(relative(root, abs).split(sep).join('/'))
    }
  }
  rec(root)
  return out
}


const SCRIPT_EXT = new Set(['.sh', '.bash', '.zsh', '.py', '.js', '.mjs', '.cjs', '.ts', '.rb', '.pl', '.php', '.ps1', '.bat', '.cmd', '.command', '.applescript', '.scpt'])
const MARKDOWN_EXT = new Set(['.md', '.markdown', '.txt'])
// An image is shown by size and sha256 only: a model opening one gets the
// picture, not instructions hidden in the bytes. Decided by its FIRST BYTES,
// matching its extension, not by the name (#2088 delta 2): a text file named
// assets/logo.png was "binary", never shown nor scanned, yet approvable.
// Anything that is not a real image of its kind is text, shown and scanned.
const jpeg = (b: Buffer) => b.length >= 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff
const IMAGE_MAGIC: Record<string, (b: Buffer) => boolean> = {
  '.png': (b) => b.length >= 8 && b.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])),
  '.jpg': jpeg,
  '.jpeg': jpeg,
  '.gif': (b) => /^GIF8[79]a$/.test(b.toString('latin1', 0, 6)),
  '.webp': (b) => b.length >= 12 && b.toString('latin1', 0, 4) === 'RIFF' && b.toString('latin1', 8, 12) === 'WEBP',
  '.bmp': (b) => b.length >= 14 && b.toString('latin1', 0, 2) === 'BM',
  '.ico': (b) => b.length >= 6 && b[0] === 0 && b[1] === 0 && b[2] === 1 && b[3] === 0,
}

// The readable runs of a binary file, as `strings` would print them: at least
// MIN_RUN printable characters (ASCII or valid UTF-8, tab included), one run
// per line. Whatever text hides behind a real image header ends up here.
const MIN_RUN = 8
export function printableRuns(buf: Buffer): string {
  const runs: string[] = []
  let cur = ''
  let chars = 0
  const flush = () => {
    const run = cur.replace(/^\n+|\n+$/g, '')
    if (chars >= MIN_RUN && run) runs.push(run)
    cur = ''; chars = 0
  }
  let i = 0
  while (i < buf.length) {
    const b = buf[i]
    const len = b < 0x80 ? 1 : (b & 0xe0) === 0xc0 ? 2 : (b & 0xf0) === 0xe0 ? 3 : (b & 0xf8) === 0xf0 ? 4 : 0
    let cp = -1
    if (len === 1) cp = b
    else if (len > 1 && i + len <= buf.length) {
      cp = b & (0xff >> (len + 1))
      for (let k = 1; k < len; k++) {
        const c = buf[i + k]
        if ((c & 0xc0) !== 0x80) { cp = -1; break }
        cp = (cp << 6) | (c & 0x3f)
      }
      const min = [0, 0, 0x80, 0x800, 0x10000][len]
      if (cp !== -1 && (cp < min || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff))) cp = -1
    }
    // A line break stays a line break inside the run (\r\n, \r and \n alike,
    // one per break), so words on separate lines are not glued into one and
    // each line is scanned on its own (#2097 review).
    if (cp === 0x0a || cp === 0x0d) { if (cur && !cur.endsWith('\n')) cur += '\n'; i++; continue }
    // Any other C0 control (NUL, ...) does not end a run, and is not part of
    // it: text spelled out between NUL bytes (UTF-16LE, or "I\0g\0n...") still
    // reads as one run, the way a terminal shows it (#2088 delta 4).
    if (cp >= 0 && cp < 0x20 && cp !== 0x09) { i++; continue }
    const printable = cp === 0x09 || (cp >= 0x20 && cp !== 0x7f && !(cp >= 0x80 && cp <= 0x9f) && cp !== 0x2028 && cp !== 0x2029)
    if (!printable) { flush(); i++; continue }
    cur += String.fromCodePoint(cp)
    chars++
    i += len
  }
  flush()
  return runs.join('\n')
}

// A PDF is NOT an image here: the Read tool hands its text to the model, and
// that text cannot be reviewed in this view, so a PDF blocks the approval,
// whatever its name.
const isPdf = (b: Buffer) => b.toString('latin1', 0, 5) === '%PDF-'

function isNativeExecutable(buf: Buffer): boolean {
  if (buf.length < 4) return false
  if (buf[0] === 0x7f && buf.toString('latin1', 1, 4) === 'ELF') return true
  if (buf[0] === 0x4d && buf[1] === 0x5a) return true // MZ (PE)
  const be = buf.readUInt32BE(0)
  return [0xfeedface, 0xfeedfacf, 0xcefaedfe, 0xcffaedfe, 0xcafebabe].includes(be) // Mach-O / fat
}

// Lossless text for review (#2088 review): a NUL byte made a .md "binary",
// so it was neither shown nor scanned, yet a SKILL.md could tell the model to
// cat it. Every byte now reaches the approver: valid UTF-8 as text, every
// invalid byte as <0xNN>, and control characters (NUL included) as <0xNN>,
// so nothing is dropped or silently replaced with U+FFFD.
export function reviewText(buf: Buffer): { text: string; invalidUtf8: number; controlBytes: number } {
  let out = ''
  let invalidUtf8 = 0
  let controlBytes = 0
  const hex = (b: number) => `<0x${b.toString(16).toUpperCase().padStart(2, '0')}>`
  let i = 0
  while (i < buf.length) {
    const b = buf[i]
    let len = b < 0x80 ? 1 : (b & 0xe0) === 0xc0 ? 2 : (b & 0xf0) === 0xe0 ? 3 : (b & 0xf8) === 0xf0 ? 4 : 0
    let cp = -1
    if (len === 1) cp = b
    else if (len > 1 && i + len <= buf.length) {
      cp = b & (0xff >> (len + 1))
      for (let k = 1; k < len; k++) {
        const c = buf[i + k]
        if ((c & 0xc0) !== 0x80) { cp = -1; break }
        cp = (cp << 6) | (c & 0x3f)
      }
      const min = [0, 0, 0x80, 0x800, 0x10000][len]
      if (cp !== -1 && (cp < min || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff))) cp = -1
    }
    if (cp === -1) { out += hex(b); invalidUtf8++; i++; continue }
    if ((cp < 0x20 && cp !== 0x09 && cp !== 0x0a && cp !== 0x0d) || cp === 0x7f) { out += hex(cp); controlBytes++ }
    // C1 controls and the line/paragraph separators: valid UTF-8, but they
    // move or hide text the way a raw control byte does. <U+XXXX>, so they
    // do not read as an invalid byte.
    else if ((cp >= 0x80 && cp <= 0x9f) || cp === 0x2028 || cp === 0x2029) { out += `<U+${cp.toString(16).toUpperCase().padStart(4, '0')}>`; controlBytes++ }
    else out += String.fromCodePoint(cp)
    i += len
  }
  return { text: out, invalidUtf8, controlBytes }
}

// Invisible to a reader, read by the model: zero-width, bidi controls, the
// Unicode tag block. Escaped so this source file stays plain ASCII.
const INVISIBLE_RE = /[​-‏‪-‮⁠-⁤⁦-⁩﻿]|[\u{E0000}-\u{E007F}]/gu

// The same shapes scripts/hooks/browser-content-notice.py names, plus the
// install/exec and persistence shapes a skill has no reason to carry.
export const SKILL_SCAN_PATTERNS: ReadonlyArray<[string, RegExp]> = [
  ['security-tag-forgery', /<\s*\/?\s*(untrusted|trusted-peer|scheduled-task|channel)\b/i],
  ['system-reminder-forgery', /<\s*\/?\s*system-reminder\b/i],
  ['directive-header-forgery', /\[(SYSTEM-DIREKTIVA|CONTEXT-GUARD|CONTEXT-RESTART-GATE|SYSTEM:)/i],
  ['instruction-override', /\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|prior|above|all)\b[^.\n]{0,20}\b(instruction|prompt|rule|direction)/i],
  ['role-reassignment', /\byou are now\b|\bnew (system )?(instructions|prompt)\b|\bact as\b[^.\n]{0,30}\b(admin|root|developer mode)/i],
  ['credential-target', /(~\/\.ssh|id_rsa|\.env\b|CLAUDE_CODE_OAUTH_TOKEN|dashboard-token|\.credentials\.json|api[_-]?key\s*[:=])/i],
  ['exfil-shape', /\b(curl|wget|fetch)\b[^\n]{0,60}https?:\/\//i],
  ['pipe-to-shell', /\|\s*(ba|z)?sh\b|\bbash\s+<\s*\(\s*curl/i],
  ['decode-and-run', /\b(eval|exec)\s*\(\s*(base64|atob|codecs\.decode|bytes\.fromhex)|base64\s+(-d|--decode)[^\n]{0,40}\|\s*(ba|z)?sh/i],
  ['shell-exec', /\bos\.system\s*\(|subprocess\.[a-z_]+\([^)\n]{0,120}shell\s*=\s*True|child_process|\bexecSync\s*\(/],
  ['persistence-target', /(settings(\.local)?\.json|scheduled-tasks|crontab\b|LaunchAgents|\.bashrc|\.zshrc|\.profile\b)/i],
  ['install-hook', /"(pre|post)install"\s*:|\bnpm\s+(i|install)\b|\bpip3?\s+install\b|\bbrew\s+install\b/i],
  ['long-encoded-blob', /[A-Za-z0-9+/]{200,}={0,2}/],
  // SKILLSCANPI1118: prompt-injection shapes a skill has no honest reason to carry.
  // The idea of covering these shapes comes from skill-security-auditor
  // (alirezarezvani/claude-skills); the expressions below are our own. A line-level
  // "ignore this line" comment is deliberately NOT honoured: a file under review must
  // not be able to switch its own review off.
  // All rows here are audit lines only: none of them is in the set that holds a skill
  // with the quarantine switch off. exfil-instruction and cloud-credential-target fire on
  // honest how-to text now and then, so they stay out of that set on purpose.
  //
  // A reader is told to set its standing rules aside.
  ['override-directive', /\b(?:disregard|forget|override|abandon|set\s+aside)\s+(?:(?:all|any|every|your|of)\s+)+(?:(?:previous|prior|earlier|current|existing)\s+)?(?:instructions|rules|guidelines|constraints|safety)\b/i],
  // Either a role-play with the limits removed, or an order to switch a safety check off.
  ['safety-bypass', /\b(?:pretend|imagine|act\s+as\s+(?:if|though))\s+(?:that\s+)?you\s+(?:have\s+no|are\s+free\s+of|(?:do\s+not|don'?t)\s+have\s+any)\s+(?:restrictions|limits|limitations|rules|safety)\b|\b(?:skip|disable|bypass|ignore|(?:turn|switch)\s+off)\s+(?:safety|security|content)\s+(?:checks?|filters?|restrictions?|rules?|guards?)\b/i],
  // Whatever command, with the confirmation step taken out.
  ['unrestricted-exec', /\b(?:run|execute)\s+(?:any|every|all|arbitrary|whatever)\s+(?:shell\s+)?(?:commands?|code|scripts?)\b[^.\n]{0,30}?\b(?:without|with\s+no|no)\s+(?:asking|confirm(?:ation|ing)?|approval|restrictions?|limits?)\b/i],
  // Moving something out of the user's environment: the source is the workspace, or the
  // destination is somewhere external. A plain "send the data to the API" stays quiet.
  // The home folder, /etc, .ssh, .env and secret stores count as such sources too.
  ['exfil-instruction', /\b(?:send|upload|post|transmit|exfiltrate|forward)\s+(?:(?:the|all|every)\s+)?(?:contents?|data|files?|information)\s+(?:(?:of|from)\s+(?:the\s+|this\s+|your\s+|the\s+user'?s\s+)?(?:workspace|repo(?:sitory)?|project|codebase|home|machine|system|disk|environment|session|conversation|~\/|\$HOME|\/etc\/|\.ssh|\.env\b|credentials|secrets|keys)|to\s+(?:an?\s+|the\s+|my\s+)?(?:remote|external|attacker|third[-\s]party|webhook|pastebin|https?:\/\/))/i],
  // The AWS credential store itself, not the config folder a deployment guide names.
  ['cloud-credential-target', /\.aws\/(?:credentials|sso\/cache)\b/i],
  // An HTML comment, invisible when rendered, that speaks to the model: it carries a role
  // label (system:, developer:, assistant:) or tells "you" what you must or may now do.
  // A maintainer's note ("runbook: keep in sync") names no role and addresses no one.
  ['hidden-html-directive', /<!--[^>]{0,40}?(?:\b(?:system|developer|assistant)\s*(?:prompt\s*)?[:\]>]|\byou\s+(?:must|may|should|are\s+now)\b)/i],
  // A claim to whole-machine reach: an all-or-root grade of access, aimed within a few
  // words at the machine as a whole rather than at one folder.
  ['excess-permission', /\b(?:unrestricted|unlimited|full|complete|total|root|admin(?:istrator)?)[-\s]+(?:level\s+)?(?:access|control|rights|privileges)\b[^.\n]{0,30}?\b(?:file\s*system|disk|network|internet|shell|terminal|machine|computer|host|everything)\b/i],
  // Consent given before the question is asked: an unconditional consent verb over every
  // action of a kind, or consent to everything with the review left out. Accepting all
  // dialogs in a test browser names no action of the agent and stays quiet.
  ['blanket-approval', /\b(?:always|automatically|blindly|unconditionally|auto-?)\s*(?:approve|accept|allow|confirm|say\s+yes\s+to)\s+(?:all|any|every)\s+(?:\w+\s+)?(?:tool\s*calls?|requests?|prompts?|permissions?|actions?|commands?|operations?)\b|\b(?:approve|accept|allow)\s+(?:everything|all\s+of\s+(?:them|it))\s+without\b/i],
  // A soft hyphen between two letters of the same case renders as nothing and can split
  // a keyword the rows above look for. Only there: the same byte pair turns up in fonts
  // and images the scan reads as text. Kept apart from invisible-unicode so it never holds a skill.
  ['soft-hyphen-split', /\p{Ll}\u00AD\p{Ll}|\p{Lu}\u00AD\p{Lu}/u],
]

// SKILLHOLDSTRONG1117 (owner, 2026-11-17): with the switch OFF, a finding of one of these shapes still holds the
// skill for the owner's decision. They are the shapes of an attack on the agent itself (forged tags and headers,
// instruction override, hidden text, run-what-you-download), which an honest skill has no reason to carry. The
// other patterns (exfil-shape, credential-target, persistence-target, install-hook, shell-exec) stay in the audit
// only: measured on one install, 21 of 30 unquarantined go-lives carried at least one of them, all from the
// fleet's own operational skills (a curl to a LAN host, a settings.json path in a note), none of a strong shape.
// Holding on those would put two thirds of every skill fix in front of the owner, which trains a reflex click.
// Still a triage list, not a verdict: an empty strong set does not mean safe.
export const SKILL_HOLD_PATTERNS: ReadonlySet<string> = new Set([
  'security-tag-forgery',
  'system-reminder-forgery',
  'directive-header-forgery',
  'instruction-override',
  'role-reassignment',
  'pipe-to-shell',
  'decode-and-run',
  'long-encoded-blob',
  'invisible-unicode',
])

function excerpt(line: string, at: number): string {
  const start = Math.max(0, at - 40)
  return line.slice(start, start + 120)
    .replace(INVISIBLE_RE, (c) => `<U+${c.codePointAt(0)!.toString(16).toUpperCase()}>`)
    .replace(/\u00AD/g, '<U+AD>')
}

export function scanSkillDir(root: string): ScanReport {
  const files: ScanFile[] = []
  const findings: ScanFinding[] = []
  let totalBytes = 0
  let truncated = false
  let findingsTruncated = false
  const unreviewable: string[] = []
  const scanLines = (rel: string, lines: string[]) => {
    for (let i = 0; i < lines.length && findings.length < MAX_FINDINGS; i++) {
      const line = lines[i]
      INVISIBLE_RE.lastIndex = 0
      const inv = INVISIBLE_RE.exec(line)
      if (inv) findings.push({ file: rel, line: i + 1, pattern: 'invisible-unicode', excerpt: excerpt(line, inv.index) })
      for (const [name, rx] of SKILL_SCAN_PATTERNS) {
        const m = rx.exec(line)
        if (m) findings.push({ file: rel, line: i + 1, pattern: name, excerpt: excerpt(line, m.index) })
      }
    }
  }
  for (const rel of filesUnder(root)) {
    if (files.length >= MAX_FILES) { truncated = true; break }
    const abs = join(root, rel)
    let st
    try { st = statSync(abs) } catch { continue }
    totalBytes += st.size
    const dot = rel.lastIndexOf('.')
    const ext = dot > rel.lastIndexOf('/') ? rel.slice(dot).toLowerCase() : ''
    const executable = (st.mode & 0o111) !== 0
    if (totalBytes > MAX_BYTES) {
      truncated = true
      files.push({ path: rel, size: st.size, kind: 'other', executable, sha256: '' })
      break
    }
    let buf: Buffer
    try { buf = readFileSync(abs) } catch { continue }
    const native = isNativeExecutable(buf)
    const pdf = !native && isPdf(buf)
    const image = !native && !pdf && (IMAGE_MAGIC[ext]?.(buf) ?? false)
    const isBinary = native || pdf || image
    const reviewed = isBinary ? null : reviewText(buf)
    const text = reviewed ? reviewed.text : ''
    const nl = text.indexOf('\n')
    const shebang = !isBinary && text.startsWith('#!') ? text.slice(0, nl < 0 ? 80 : Math.min(nl, 80)) : undefined
    const kind: ScanFile['kind'] = isBinary ? 'binary'
      : (SCRIPT_EXT.has(ext) || shebang) ? 'script'
      : MARKDOWN_EXT.has(ext) ? 'markdown' : 'other'
    const sha256 = createHash('sha256').update(buf).digest('hex')
    const strings = image ? printableRuns(buf) : undefined
    files.push({ path: rel, size: st.size, kind, executable, ...(shebang ? { shebang } : {}), sha256, ...(isBinary ? {} : { text }), ...(strings ? { strings } : {}) })
    if (native || pdf) {
      unreviewable.push(rel)
      if (findings.length < MAX_FINDINGS) {
        findings.push(native
          ? { file: rel, line: 0, pattern: 'native-executable', excerpt: 'a compiled program: its content cannot be reviewed here' }
          : { file: rel, line: 0, pattern: 'pdf-document', excerpt: 'a PDF: the model reads its text, which cannot be reviewed here' })
      }
      continue
    }
    if (!reviewed) {
      if (strings) {
        // the readable runs of an image are scanned like text; a run is a line
        const runLines = strings.split('\n')
        if (findings.length < MAX_FINDINGS) findings.push({ file: rel, line: 0, pattern: 'image-text', excerpt: `${runLines.length} line(s) of readable text between the image bytes, shown below the file` })
        scanLines(rel, runLines)
      }
      if (findings.length >= MAX_FINDINGS) findingsTruncated = true
      continue
    }
    if (reviewed.controlBytes && findings.length < MAX_FINDINGS) {
      findings.push({ file: rel, line: 0, pattern: 'control-bytes', excerpt: `${reviewed.controlBytes} control byte(s), shown as <0xNN> or <U+XXXX>` })
    }
    if (reviewed.invalidUtf8 && findings.length < MAX_FINDINGS) {
      findings.push({ file: rel, line: 0, pattern: 'invalid-utf8', excerpt: `${reviewed.invalidUtf8} byte(s) that are not UTF-8, shown as <0xNN>` })
    }
    scanLines(rel, text.split('\n'))
    if (findings.length >= MAX_FINDINGS) findingsTruncated = true
  }
  return { files, findings, totalBytes, truncated, findingsTruncated, unreviewable }
}

// The files of a replacement proposal that should equal the live skill's but do not (see PendingSkill.liveDrift).
// Compared by sha256 against the live file, read without following a link; a live entry that is not a regular
// file counts as missing. Bounded like the scan: a file over MAX_BYTES is not read and counts as changed.

export const SKILL_AUDIT_FILE = 'skills-audit.jsonl'

/** This fork has no SKILL_QUARANTINE switch and no pending area: a package agent's skills wait behind the package
 *  approval instead (agent-package/approval.ts), so for the package installer the hold always applies. Kept as a
 *  function so the installer's upstream call sites read the same. */
export function quarantineOn(): boolean {
  return true
}

/** One audit line per scanned package skill (upstream's SkillAuditLine, narrowed to the agent package event). Counts
 *  only: the pattern name and how many hits; never an excerpt, never a file's text. */
export type SkillAuditLine = {
  at: string
  event: 'package_install_scanned'
  id: string
  name: string
  source: 'agent-package'
  target: { scope: 'agent'; agent: string }
  proposedBy: string | null
  hash: string | null
  replaces: boolean
  findings: Record<string, number>
  findingsTruncated: boolean
  why?: string
}

export function appendSkillAudit(line: SkillAuditLine, file = join(STORE_DIR, SKILL_AUDIT_FILE)): void {
  try {
    appendFileSync(file, JSON.stringify(line) + '\n', { mode: 0o600 })
  } catch (err) {
    // The audit line is part of the off path, so its loss is never silent.
    logger.error({ id: line.id, name: line.name, err: err instanceof Error ? err.message : String(err) }, 'skill audit: the line was NOT written')
  }
}


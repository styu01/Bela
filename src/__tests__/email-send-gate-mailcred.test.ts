// MAILCRED1127 (BELA msg 6120, PROGI msg 6118 (e)): the configured Gmail path on this host -- scripts/send-gmail.py
// and the outreach scripts, which take a mail app password from the dashboard vault and send through smtplib --
// passed the sub-agent hard gate directly. Gate-only: nothing here is executed.

import { describe, it, expect } from 'vitest'
// @ts-expect-error -- plain .mjs hook script, no types
import { gateDecision, runsMailerScript, touchesMailCredential, buildCredentialMsg } from '../../scripts/email-send-gate.mjs'

const KEY = 'gmail_kiss_i_god_app_pw'
const KEY2 = 'info@woowebsite.eu jelszó'
const BS = String.fromCharCode(92) // a backslash, built: no editor or tool turns an escape into its character
const bash = (command: string) => gateDecision('Bash', { command })

const RUNS: Array<[string, string]> = [
  ['the reported form', 'python3 scripts/send-gmail.py --to a@b.hu --subject s --body b'],
  ['absolute path', 'python3 /home/kisss/marveen/scripts/send-gmail.py --to a@b.hu --subject s --body b'],
  ['run directly', 'cd scripts && ./send-gmail.py --to a@b.hu --subject s --body b'],
  ['after cd, bare name', 'cd /home/kisss/marveen/scripts; python3 send-gmail.py --to a@b.hu'],
  ['through env', 'env python3 scripts/send-gmail.py --to a@b.hu'],
  ['in bash -c', "bash -c 'python3 scripts/send-gmail.py --to a@b.hu'"],
  ['in a loop', 'for a in x@y.hu z@w.hu; do python3 scripts/send-gmail.py --to "$a" --subject s --body b; done'],
  ['echo piped into bash', 'echo "python3 scripts/send-gmail.py --to a@b.hu" | bash'],
  ['python heredoc starting it', "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['python3', 'scripts/send-gmail.py', '--to', 'a@b.hu'])\nEOF"],
  ['the outreach sender', 'python3 send_outreach.py --to x@y.hu'],
  ['the daily batch', 'timeout 540 python3 send_batch_daily.py 40 /x/lista.xlsx 2>&1 | tail -5'],
  ['read then run', 'sed -n 1,40p scripts/send-gmail.py && python3 scripts/send-gmail.py --to a@b.hu'],
  // PROGI 6122 probe: a substitution in command position runs its output
  ['substitution in command position', '$(echo "python3 /x/work/send_batch_daily.py 1")'],
  ['backtick substitution', '`echo python3 send_outreach.py --to a@b.hu`'],
  ['substitution in an UNQUOTED heredoc body (it expands)', 'cat <<EOF\n$(python3 send_outreach.py --to a@b.hu)\nEOF'],
]
const CREDENTIAL: Array<[string, string]> = [
  ['curl the key', `curl -s -H "Authorization: Bearer $T" http://localhost:3420/api/vault/${KEY}`],
  ['curl a variable id', 'curl -s -H "Authorization: Bearer $(cat store/.dashboard-token)" "http://localhost:3420/api/vault/$K"'],
  ['python builds the URL', "python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:3420/api/vault/' + k)\""],
  ['heredoc builds the URL', "python3 - <<'EOF'\nurl = 'http://localhost:3420/api/vault/' + key\nEOF"],
  ['node reads vault.js by key', `node -e "import('/home/kisss/marveen/dist/web/vault.js').then(m => console.log(m.getSecret('${KEY}')))"`],
  ['wget without scheme', 'wget -qO- localhost:3420/api/vault/x'],
  // fixup (PROGI 6134 (3)): the second SMTP credential, the woowebsite.eu mailbox password, in its spellings
  ['node reads the mailbox password by key', `node -e "import('/home/kisss/marveen/dist/web/vault.js').then(v => v.getSecret('${KEY2}'))"`],
  ['the mailbox key with a \\u00f3 escape', `python3 -c "k = 'info@woowebsite.eu jelsz\\u00f3'; print(len(k))"`],
  ['the mailbox key url-encoded, %20', 'python3 -c "u = \'info%40woowebsite.eu%20jelsz%C3%B3\'"'],
  ['the mailbox key url-encoded, +', 'python3 -c "u = \'info%40woowebsite.eu+jelsz%C3%B3\'"'],
  // PROGI 6142 / BELA 6152: the spellings the plain pattern missed (each passed the gate before)
  ['shell-escaped space', String.raw`node vault-resolve.mjs <<< info@woowebsite.eu\ jelszó`],
  ['a literal tab for the space', 'node vault-resolve.mjs <<< "info@woowebsite.eu\tjelszó"'],
  ['a \\t escape for the space', String.raw`python3 -c "k = 'info@woowebsite.eu\tjelszó'"`],
  ['quote break: ""', `node vault-resolve.mjs <<< "info@woowebsite.eu"" jelszó"`],
  ["quote break: \"... \"'...'", `node vault-resolve.mjs <<< "info@woowebsite.eu "'jelszó'`],
  ['ANSI-C escapes', String.raw`node vault-resolve.mjs <<< $'info@woowebsite.eu\x20jelsz\xc3\xb3'`],
  ['python \\x40 for the @, the URL split', String.raw`python3 -c "import urllib.request as u; u.urlopen('http://localhost:3420/api/'+'vault'+'/'+u.quote('info\x40woowebsite.eu jelszó'))"`],
  // (the backslash is built, so no editor or tool can turn the escape into the character itself)
  ['backslash-u0040 for the @', `python3 -c "k = 'info${String.fromCharCode(92)}u0040woowebsite.eu jelszó'"`],
  ['backslash-u0020 for the space', `python3 -c "k = 'info@woowebsite.eu${String.fromCharCode(92)}u0020jelszó'"`],
  ['octal \\100 for the @', String.raw`printf 'info\100woowebsite.eu jelsz\303\263'`],
  ['a backslash before an ordinary letter (the shell drops it)', String.raw`node vault-resolve.mjs <<< info@woowebsite.eu\ \jelszó`],
  ['the Gmail id with a quote break', `node vault-resolve.mjs <<< gmail_kiss_"i_god"_app_pw`],
  ['the Gmail id with quote breaks inside a word', `node vault-resolve.mjs <<< "gm"'ail'_kiss_i_god_app_pw`],
  ['the Gmail id, one letter as \\x escape', String.raw`node vault-resolve.mjs <<< $'gmail_kiss_i_\x67od_app_pw'`],
  ['the Gmail id url-encoded underscores', 'curl -s "$U/api/v?k=gmail%5Fkiss%5Fi%5Fgod%5Fapp%5Fpw"'],
  // PROGI 6376 (B) / BELA 6377: the five spellings that still passed (each prints the real id); backslashes built
  ['a shell line continuation inside the word', `echo info@woo${BS}\nwebsite.eu${BS} jelszó`],
  ["an empty ANSI-C string inside the word ($'')", `node vault-resolve.mjs <<< info@woo$''website.eu${BS} jelszó`],
  ['an empty locale string inside the word ($"")', `node vault-resolve.mjs <<< info@woo$""website.eu${BS} jelszó`],
  ["the Gmail id with an empty $'' inside", `node vault-resolve.mjs <<< gmail_kiss_i$''_god_app_pw`],
  ['python adjacent literals with a space', `python3 -c "k = 'info@woo' 'website.eu jelszó'"`],
  ['python adjacent literals, the Gmail id', `python3 -c "k = 'gmail_kiss' '_i_god_app_pw'"`],
  ['a four-digit octal (printf %b)', `printf '%b' 'info${BS}0100woowebsite.eu jelsz${BS}0303${BS}0263'`],
  ['JS backslash-u{40}', `node -e "console.log('info${BS}u{40}woowebsite.eu jelszó')"`],
  ['Python backslash-U00000040', `python3 -c "print('info${BS}U00000040woowebsite.eu jelszó')"`],
  ['Python backslash-N{COMMERCIAL AT}', `python3 -c "print('info${BS}N{COMMERCIAL AT}woowebsite.eu jelszó')"`],
  ['Python backslash-N{SPACE} for the space', `python3 -c "print('info@woowebsite.eu${BS}N{SPACE}jelszó')"`],
]
const ALLOWED: Array<[string, string]> = [
  ['backticked names in a quoted heredoc body (markdown)', "cat > DREAM.md <<'EOF'\nrun `send_batch_daily.py 40` daily\nEOF"],
  ['the mailbox address alone', 'echo info@woowebsite.eu'],
  ['the word jelszo alone', 'grep -n jelszo notes.txt'],
  ['read the outreach script', 'cat docs/projects/otfelmero-megkereses/send_outreach.py'],
  ['grep in it', 'grep -n "SUBJECT =" send_outreach.py'],
  ['sed -n print', 'sed -n 1,40p scripts/send-gmail.py'],
  ['sed -n print piped', 'sed -n 1,120p send_outreach.py | cut -c1-220'],
  ['ls it', 'ls send_batch_daily.py && cp a.xlsx b.xlsx'],
  ['git diff it', 'git diff scripts/send-gmail.py'],
  ['grep for the route in code', 'grep -rn "/api/vault/" src/web/routes'],
  ['the route in a quoted pattern', "grep -n \"startsWith('/api/vault/')\" src/web/*.ts"],
  ['the route with a placeholder', 'echo "GET /api/vault/<id> returns the value"'],
  ['the id listing (no values)', 'curl -s -H "Authorization: Bearer $T" http://localhost:3420/api/vault'],
  ['unrelated python', 'python3 scripts/usage-check.py'],
  // PROGI 6142: the wider spelling must not catch ordinary text around the same words
  ['the address and the word in separate places', 'echo info@woowebsite.eu; grep -n jelszo notes.txt'],
  ['the address, other text, then the word', 'echo "info@woowebsite.eu: a jelszo modositva"'],
  ['the Gmail words apart', 'echo gmail kiss i god app pw'],
  ['a different Gmail key', 'echo gmail_kiss_i_god_app_token'],
  ['escapes and quotes, but not the id', String.raw`printf 'info\x40example.com jelsz\n'`],
  // PROGI 6376: the new joins must not glue ordinary text into the id
  ['python adjacent literals of other text', `python3 -c "k = 'info@woo' 'other.eu'"`],
  ["an empty $'' in ordinary text", `echo foo$''bar info@woowebsite.eu`],
  ['a line continuation in ordinary text', `echo info@woowebsite.eu ${BS}\n  && grep -n jelszo notes.txt`],
]

describe('MAILCRED1127: running a configured mailer script is refused for a sub-agent', () => {
  for (const [name, cmd] of RUNS) {
    it(name, () => {
      expect(bash(cmd).deny).toBe(true)
      expect(runsMailerScript(cmd)).toBe(true)
    })
  }
})

describe('MAILCRED1127: touching the mail credential is refused, with its own message', () => {
  for (const [name, cmd] of CREDENTIAL) {
    it(name, () => {
      const d = bash(cmd)
      expect(d.deny).toBe(true)
      expect(d.kind).toBe('mail-credential')
      expect(touchesMailCredential(cmd)).toBe(true)
    })
  }
  it('the message routes the mail to the main agent', () => {
    expect(buildCredentialMsg('BELA')).toContain('BELA kuld')
  })
})

describe('MAILCRED spellings: the matcher stays linear on hostile input (PROGI 6376)', () => {
  // An unbounded name in the N{...} form scanned to the end of the command at every position: 1.1 s on 96k.
  const cases: Array<[string, string]> = [
    ['a long unclosed N{', ('i' + BS + 'N{' + 'A'.repeat(20)).repeat(4000)],
    ['quote-space-quote soup', "' '".repeat(40000)],
    ['backslash-newline soup', (BS + '\n').repeat(50000)],
    ["$'' soup", "$''".repeat(33000)],
  ]
  for (const [name, input] of cases) {
    it(name, () => {
      const t0 = performance.now()
      expect(touchesMailCredential(input)).toBe(false)
      expect(performance.now() - t0).toBeLessThan(250)
    })
  }
})

describe('MAILCRED1127: reading the scripts or mentioning the route stays allowed', () => {
  for (const [name, cmd] of ALLOWED) {
    it(name, () => {
      expect(bash(cmd).deny).toBe(false)
    })
  }
})

describe('MAILCRED1127: the hook entry point fails closed', () => {
  it('a decision that throws is a deny', async () => {
    // @ts-expect-error -- plain .mjs hook script, no types
    const { hookDecision } = await import('../../scripts/email-send-gate.mjs')
    const d = hookDecision({ tool_name: 'Bash', tool_input: { command: 'ls' } }, () => { throw new Error('boom') })
    expect(d.deny).toBe(true)
  })
  it('the real hook process denies the reported command and the credential read, exit 0', async () => {
    const { spawnSync } = await import('node:child_process')
    const { fileURLToPath } = await import('node:url')
    const gate = fileURLToPath(new URL('../../scripts/email-send-gate.mjs', import.meta.url))
    const run = (command: string) => spawnSync(process.execPath, [gate], {
      input: JSON.stringify({ tool_name: 'Bash', tool_input: { command } }), encoding: 'utf-8',
    })
    const a = run('python3 scripts/send-gmail.py --to a@b.hu --subject s --body b')
    expect(a.status).toBe(0)
    expect(JSON.parse(a.stdout).hookSpecificOutput.permissionDecision).toBe('deny')
    const b = run(`curl -s http://localhost:3420/api/vault/${KEY}`)
    expect(b.status).toBe(0)
    expect(JSON.parse(b.stdout).hookSpecificOutput.permissionDecisionReason).toContain('vault')
    const c = run('sed -n 1,40p scripts/send-gmail.py')
    expect(c.status).toBe(0)
    expect(c.stdout).toBe('')
  })
})

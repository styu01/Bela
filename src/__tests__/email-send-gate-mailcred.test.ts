// MAILCRED1127 (BELA msg 6120, PROGI msg 6118 (e)): the configured Gmail path on this host -- scripts/send-gmail.py
// and the outreach scripts, which take a mail app password from the dashboard vault and send through smtplib --
// passed the sub-agent hard gate directly. Gate-only: nothing here is executed.

import { describe, it, expect } from 'vitest'
// @ts-expect-error -- plain .mjs hook script, no types
import { gateDecision, runsMailerScript, touchesMailCredential, buildCredentialMsg } from '../../scripts/email-send-gate.mjs'

const KEY = 'gmail_kiss_i_god_app_pw'
const KEY2 = 'info@woowebsite.eu jelszó'
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

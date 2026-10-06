// CHANNELSAUTHRACE923 + TOKENARGV929: replay the auth handoff on a real,
// isolated tmux socket. This is intentionally not a model of tmux's server
// environment: the start-server/new-session race is the bug boundary.
import { describe, it, expect, afterEach } from 'vitest'
import { execFileSync, spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, readFileSync, rmSync, existsSync, writeFileSync, chmodSync, statSync, lstatSync, symlinkSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'

const repo = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SH = readFileSync(join(repo, 'scripts', 'channels.sh'), 'utf-8')
const tmuxProbe = spawnSync('tmux', ['-V'])
const HAS_TMUX = tmuxProbe.status === 0 && !tmuxProbe.error
// Some restricted test sandboxes allow the version probe but reject child
// process creation. Keep the source-pin tests runnable there; the real-socket
// cases execute unchanged on a normal host/CI worker.
const bashProbe = spawnSync('bash', ['-c', ':'])
const CAN_SPAWN_ISOLATED_PROCESS = bashProbe.status === 0 && !bashProbe.error

function authBlock(): string {
  const start = SH.indexOf('_tmux_set_auth_globals() {')
  const end = SH.indexOf('unset _auth_file', start)
  if (start < 0 || end < 0) throw new Error('auth block not found in channels.sh')
  return SH.slice(start, end + 'unset _auth_file'.length)
}

const PRIMARY_LAUNCH = 'env -u "$STATE_ENV_VAR" $TMUX new-session -d -s "$SESSION" -c "$INSTALL_DIR"'

function primaryLaunch(): string {
  const start = SH.indexOf(PRIMARY_LAUNCH)
  const end = SH.indexOf('_tmux_set_auth_globals', start)
  if (start < 0 || end < 0) throw new Error('primary launch not found in channels.sh')
  return SH.slice(start, end + '_tmux_set_auth_globals'.length)
}

let dir = ''
let sock = ''
afterEach(() => {
  if (sock) spawnSync('tmux', ['-S', sock, 'kill-server'])
  if (dir) rmSync(dir, { recursive: true, force: true })
  dir = ''
  sock = ''
})

function bash(script: string, env: Record<string, string> = {}): string {
  return execFileSync('bash', ['-c', script], {
    encoding: 'utf-8',
    env: { PATH: process.env.PATH ?? '', HOME: process.env.HOME ?? '', ...env },
  })
}

describe.skipIf(!HAS_TMUX || !CAN_SPAWN_ISOLATED_PROCESS)('channels.sh tmux auth (real tmux, isolated socket)', () => {
  it('keeps the race protection when another worker created the server first', () => {
    dir = mkdtempSync(join(tmpdir(), 'tmuxrace-'))
    mkdirSync(join(dir, 'store'))
    sock = join(dir, 's')
    const out = join(dir, 'pane-env')
    const fake = join(dir, 'fake-claude')
    writeFileSync(fake, `#!/bin/sh\nprintenv CLAUDE_CODE_OAUTH_TOKEN > ${out}.tmp; mv ${out}.tmp ${out}; sleep 30\n`)
    chmodSync(fake, 0o755)
    const log = bash(`
      TMUX="tmux -S ${sock}"
      SESSION=channels INSTALL_DIR=${dir} CLAUDE=${fake} STATE_ENV_VAR=TELEGRAM_STATE_DIR
      MCP_BATCH_ENV= CFG_ENV= MODEL_FLAG= EFFORT_FLAG= DEBUG_FLAG= PLUGIN_ID=x EXTRA_CHANNELS=
      ${authBlock()}
      echo "auth=\${AUTH_PANE_ENV:+set}"
      env -u CLAUDE_CODE_OAUTH_TOKEN tmux -S ${sock} new-session -d -s worker "sleep 30"
      ${primaryLaunch()}
    `, { CLAUDE_CODE_OAUTH_TOKEN: 'test-oauth-race-value' })
    expect(log).toMatch(/auth=set$/m)
    expect(statSync(join(dir, 'store', '.channels-pane-auth')).mode & 0o777).toBe(0o600)
    for (let i = 0; i < 100 && !existsSync(out); i++) execFileSync('sleep', ['0.1'])
    expect(readFileSync(out, 'utf-8').trim()).toBe('test-oauth-race-value')
    const globalEnv = execFileSync('tmux', ['-S', sock, 'show-environment', '-g', 'CLAUDE_CODE_OAUTH_TOKEN'], { encoding: 'utf-8' })
    expect(globalEnv.trim()).toBe('CLAUDE_CODE_OAUTH_TOKEN=test-oauth-race-value')
  })

  it('uses a fresh 0600 auth file instead of an existing wider-mode file or symlink', () => {
    dir = mkdtempSync(join(tmpdir(), 'tmuxauthfile-'))
    mkdirSync(join(dir, 'store'))
    const file = join(dir, 'store', '.channels-pane-auth')
    writeFileSync(file, 'old\n')
    chmodSync(file, 0o644)
    const oldInode = statSync(file).ino
    bash(`TMUX="tmux -S ${join(dir, 's')}"; INSTALL_DIR=${dir}; ${authBlock()}`, { CLAUDE_CODE_OAUTH_TOKEN: 'test-oauth-file-value' })
    expect(statSync(file).mode & 0o777).toBe(0o600)
    expect(statSync(file).ino).not.toBe(oldInode)

    const target = join(dir, 'target')
    writeFileSync(target, 'untouched\n')
    rmSync(file)
    symlinkSync(target, file)
    bash(`TMUX="tmux -S ${join(dir, 's')}"; INSTALL_DIR=${dir}; ${authBlock()}`, { CLAUDE_CODE_OAUTH_TOKEN: 'test-oauth-symlink-value' })
    expect(readFileSync(target, 'utf-8')).toBe('untouched\n')
    expect(lstatSync(file).isSymbolicLink()).toBe(false)
    expect(statSync(file).mode & 0o777).toBe(0o600)
  })

  // The server process retains the new-session client argv when it creates the
  // server. Count only this isolated server tree, never print the probe value.
  const TOKEN = 'test-oauth-argv-probe'
  const countProbeInSocketTree = () => {
    const rows = execFileSync('ps', ['-axo', 'pid=,ppid=,command='], { encoding: 'utf-8' }).split('\n')
      .map((line) => { const m = /^\s*(\d+)\s+(\d+)\s(.*)$/.exec(line); return m ? { pid: +m[1], ppid: +m[2], cmd: m[3] } : null })
      .filter((row): row is { pid: number; ppid: number; cmd: string } => row !== null)
    const ours = new Set(rows.filter((row) => row.cmd.includes(sock)).map((row) => row.pid))
    expect(ours.size).toBeGreaterThan(0)
    for (let grew = true; grew;) {
      grew = false
      for (const row of rows) if (!ours.has(row.pid) && ours.has(row.ppid)) { ours.add(row.pid); grew = true }
    }
    return rows.filter((row) => ours.has(row.pid)).reduce((count, row) => count + row.cmd.split(TOKEN).length - 1, 0)
  }

  it('does not put the auth probe on any command line when channels creates the server', () => {
    dir = mkdtempSync(join(tmpdir(), 'tmuxargv-'))
    mkdirSync(join(dir, 'store'))
    sock = join(dir, 's')
    const out = join(dir, 'pane-env')
    const fake = join(dir, 'fake-claude')
    writeFileSync(fake, `#!/bin/sh\nprintenv CLAUDE_CODE_OAUTH_TOKEN > ${out}.tmp; mv ${out}.tmp ${out}; sleep 30\n`)
    chmodSync(fake, 0o755)
    bash(`
      TMUX="tmux -S ${sock}"
      SESSION=channels INSTALL_DIR=${dir} CLAUDE=${fake} STATE_ENV_VAR=TELEGRAM_STATE_DIR
      MCP_BATCH_ENV= CFG_ENV= MODEL_FLAG= EFFORT_FLAG= DEBUG_FLAG= PLUGIN_ID=x EXTRA_CHANNELS=
      ${authBlock()}
      ${primaryLaunch()}
    `, { CLAUDE_CODE_OAUTH_TOKEN: TOKEN })
    for (let i = 0; i < 100 && !existsSync(out); i++) execFileSync('sleep', ['0.1'])
    expect(readFileSync(out, 'utf-8').trim()).toBe(TOKEN)
    expect(countProbeInSocketTree()).toBe(0)
  })

  it('proves the old -e form would leak the probe through the tmux server argv', () => {
    dir = mkdtempSync(join(tmpdir(), 'tmuxargv-'))
    sock = join(dir, 's')
    bash(`tmux -S ${sock} new-session -d -s channels -e "CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN" "sleep 30"`, { CLAUDE_CODE_OAUTH_TOKEN: TOKEN })
    expect(countProbeInSocketTree()).toBeGreaterThan(0)
  })
})

describe('channels.sh tmux auth wiring (source pin)', () => {
  const launches = SH.split('\n').filter((line) => /^\s*(env -u "\$STATE_ENV_VAR" )?\$TMUX new-session -d -s "\$SESSION"/.test(line))

  it('keeps both launch paths state-dir-safe but passes no secret via new-session argv', () => {
    expect(launches).toHaveLength(2)
    for (const launch of launches) {
      expect(launch.trim().startsWith('env -u "$STATE_ENV_VAR" $TMUX new-session')).toBe(true)
      expect(launch).not.toContain('TMUX_AUTH_ENV')
      expect(launch).not.toMatch(/\s-e\s/)
    }
    expect(SH).not.toContain('TMUX_AUTH_ENV')
    expect(SH).toContain('$TMUX set-environment -g -u "$STATE_ENV_VAR"')
    const paneCommands = SH.split('\n').filter((line) => line.includes('${MCP_BATCH_ENV}${CFG_ENV}$CLAUDE'))
    expect(paneCommands).toHaveLength(2)
    for (const command of paneCommands) expect(command.trim().startsWith('"${AUTH_PANE_ENV}${MCP_BATCH_ENV}')).toBe(true)
  })

  it('keeps an auth-file setup failure non-fatal but records a durable warning', () => {
    expect(SH).toContain('WARN: channels auth file unavailable: mktemp failed')
    expect(SH).toContain('respawn_log "auth-file-unavailable: mktemp failed')
    expect(SH).toContain('[ -d "$_auth_file" ]')
    expect(SH).toContain('target is a directory; using tmux global auth fallback')
    // The directory guard must run before the rename: plain `mv -f tmp dir`
    // succeeds by placing tmp inside dir and would leave AUTH_PANE_ENV broken.
    expect(SH.indexOf('[ -d "$_auth_file" ]')).toBeLessThan(SH.indexOf('mv -f "$_auth_tmp" "$_auth_file"'))
  })

  it('retains remain-on-exit for both normal and /tmp fallback launches', () => {
    const first = SH.indexOf(PRIMARY_LAUNCH)
    const second = SH.indexOf('env -u "$STATE_ENV_VAR" $TMUX new-session', first + 1)
    expect(first).toBeGreaterThan(-1)
    expect(second).toBeGreaterThan(first)
    expect(SH.indexOf('$TMUX set-option -t "$SESSION" remain-on-exit on', first)).toBeGreaterThan(first)
    expect(SH.indexOf('$TMUX set-option -t "$SESSION" remain-on-exit on', second)).toBeGreaterThan(second)
  })
})

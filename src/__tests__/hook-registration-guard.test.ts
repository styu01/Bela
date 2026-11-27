import { describe, it, expect } from 'vitest'
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  isWorktreeRoot,
  isTemporaryRoot,
  shouldRegisterHooks,
  pruneStaleHookEntries,
  pruneStaleHooksFromSettingsFile,
  KNOWN_HOOK_SCRIPTS,
} from '../web/hook-registration-guard.js'

// 2026-07-11 incident: a WEB_ONLY smoke instance running from a git worktree
// registered UserPromptSubmit/SessionStart hooks into the user-global
// ~/.claude/settings.json with worktree-absolute paths. After the worktree was
// deleted, the hook exited 2 and BLOCKED every prompt (main agent deaf).
// These tests pin the guard (never register from a worktree / staging
// instance) and the self-heal (prune our stale entries, keep foreign ones).

const notAGitFile = () => false

describe('isWorktreeRoot', () => {
  it('detects a .claude/worktrees checkout by path', () => {
    expect(isWorktreeRoot('/home/user/app/.claude/worktrees/agent-abc123', { isGitFile: notAGitFile })).toBe(true)
  })
  it('detects a generic linked worktree via the .git-is-a-file signal', () => {
    expect(isWorktreeRoot('/tmp/some-linked-checkout', { isGitFile: () => true })).toBe(true)
  })
  it('treats a normal checkout (git dir, non-worktree path) as non-worktree', () => {
    expect(isWorktreeRoot('/opt/app', { isGitFile: notAGitFile })).toBe(false)
  })
})

describe('shouldRegisterHooks', () => {
  it('registers for a normal root in normal mode', () => {
    const d = shouldRegisterHooks({ projectRoot: '/opt/app', webOnly: false, isGitFile: notAGitFile })
    expect(d.register).toBe(true)
  })
  it('skips for a worktree root', () => {
    const d = shouldRegisterHooks({
      projectRoot: '/opt/app/.claude/worktrees/agent-xyz',
      webOnly: false,
      isGitFile: notAGitFile,
    })
    expect(d.register).toBe(false)
    expect(d.reason).toMatch(/worktree/)
  })
  it('skips in WEB_ONLY staging mode even from a normal root', () => {
    const d = shouldRegisterHooks({ projectRoot: '/opt/app', webOnly: true, isGitFile: notAGitFile })
    expect(d.register).toBe(false)
    expect(d.reason).toMatch(/WEB_ONLY/)
  })
  // 2026-07-13 canary incident: a plain `git clone` under /private/tmp is NOT a
  // worktree (.git is a dir) and not WEB_ONLY, yet it registered hooks into the
  // user-global settings.json -- the same deaf-agent trap, one class wider.
  it('skips a plain clone under /private/tmp (canary/second-instance)', () => {
    const d = shouldRegisterHooks({
      projectRoot: '/private/tmp/marveen-work',
      webOnly: false,
      isGitFile: notAGitFile,
    })
    expect(d.register).toBe(false)
    expect(d.reason).toMatch(/temp dir/)
  })
  it('skips a clone under /tmp', () => {
    const d = shouldRegisterHooks({ projectRoot: '/tmp/marveen-work', webOnly: false, isGitFile: notAGitFile })
    expect(d.register).toBe(false)
  })
  it('skips a clone under the injected OS tmpDir (e.g. macOS /var/folders/..)', () => {
    const d = shouldRegisterHooks({
      projectRoot: '/var/folders/xy/abc/T/marveen-clone',
      webOnly: false,
      isGitFile: notAGitFile,
      tmpDir: '/var/folders/xy/abc/T',
    })
    expect(d.register).toBe(false)
  })
  it('still registers for a real install path that merely contains "tmp" mid-path', () => {
    const d = shouldRegisterHooks({ projectRoot: '/home/user/mytmpapp', webOnly: false, isGitFile: notAGitFile })
    expect(d.register).toBe(true)
  })
})

describe('isTemporaryRoot', () => {
  it('true for /tmp and /private/tmp prefixes', () => {
    expect(isTemporaryRoot('/tmp/x')).toBe(true)
    expect(isTemporaryRoot('/private/tmp/x')).toBe(true)
    expect(isTemporaryRoot('/var/folders/a/b/T/x')).toBe(true)
  })
  it('false for a normal install root', () => {
    expect(isTemporaryRoot('/Users/marvin/ClaudeClaw')).toBe(false)
    expect(isTemporaryRoot('/opt/app')).toBe(false)
  })
  it('honours an injected OS tmpdir prefix (with or without trailing slash)', () => {
    expect(isTemporaryRoot('/custom/tmp/clone', { tmpDir: '/custom/tmp' })).toBe(true)
    expect(isTemporaryRoot('/custom/tmp/clone', { tmpDir: '/custom/tmp/' })).toBe(true)
  })
  it('does not match a path that merely contains a temp fragment mid-string', () => {
    expect(isTemporaryRoot('/home/tmpish/app')).toBe(false)
  })
})

function settingsFixture(): Record<string, unknown> {
  return {
    enabledPlugins: { 'telegram@claude-plugins-official': true },
    hooks: {
      UserPromptSubmit: [
        {
          hooks: [
            { type: 'command', command: 'python3 /opt/app/scripts/hooks/staleness-guard.py', timeout: 10 },
            { type: 'command', command: 'python3 /opt/app/.claude/worktrees/agent-dead/scripts/hooks/voice-reply-directive.py', timeout: 60 },
          ],
        },
        { hooks: [{ type: 'command', command: 'python3 /home/user/my-own-hook.py', timeout: 5 }] },
      ],
      SessionStart: [
        {
          matcher: 'compact|resume',
          hooks: [{ type: 'command', command: 'python3 /opt/app/.claude/worktrees/agent-dead/scripts/hooks/taskstate-replay.py', timeout: 15 }],
        },
      ],
      PreCompact: [
        { matcher: 'auto', hooks: [{ type: 'agent', prompt: 'save memories', timeout: 180 }] },
      ],
    },
  }
}

describe('pruneStaleHookEntries', () => {
  const liveFiles = new Set(['/opt/app/scripts/hooks/staleness-guard.py'])
  const fileExists = (p: string) => liveFiles.has(p)

  it('prunes stale worktree entries and keeps valid + foreign entries', () => {
    const settings = settingsFixture()
    const { changed, removed } = pruneStaleHookEntries(settings, { fileExists })
    expect(changed).toBe(true)
    expect(removed).toHaveLength(2)
    expect(removed.join(' ')).toContain('voice-reply-directive.py')
    expect(removed.join(' ')).toContain('taskstate-replay.py')

    const hooks = settings.hooks as Record<string, Array<{ hooks: Array<{ command?: string; prompt?: string }> }>>
    // Valid our-entry kept.
    expect(JSON.stringify(hooks.UserPromptSubmit)).toContain('staleness-guard.py')
    // Foreign entry kept even though its file does not exist.
    expect(JSON.stringify(hooks.UserPromptSubmit)).toContain('/home/user/my-own-hook.py')
    // SessionStart group emptied by pruning: the whole event key is dropped.
    expect(hooks.SessionStart).toBeUndefined()
    // Agent-type (non-command) hooks are never touched.
    expect(hooks.PreCompact[0].hooks[0].prompt).toBe('save memories')
  })

  it('prunes a missing-file NON-worktree entry when it matches our script names', () => {
    const settings = {
      hooks: {
        UserPromptSubmit: [
          { hooks: [{ type: 'command', command: 'python3 /old/install/scripts/hooks/voice-reply-directive.py', timeout: 60 }] },
        ],
      },
    }
    const { changed, removed } = pruneStaleHookEntries(settings, { fileExists: () => false })
    expect(changed).toBe(true)
    expect(removed).toEqual(['python3 /old/install/scripts/hooks/voice-reply-directive.py'])
    expect((settings.hooks as Record<string, unknown>).UserPromptSubmit).toBeUndefined()
  })

  it('keeps our entry when the script file exists', () => {
    const settings = {
      hooks: {
        UserPromptSubmit: [
          { hooks: [{ type: 'command', command: 'python3 /opt/app/scripts/hooks/staleness-guard.py', timeout: 10 }] },
        ],
      },
    }
    const before = JSON.stringify(settings)
    const { changed, removed } = pruneStaleHookEntries(settings, { fileExists })
    expect(changed).toBe(false)
    expect(removed).toEqual([])
    expect(JSON.stringify(settings)).toBe(before)
  })

  it('keeps a foreign hook whose file is missing (not ours, not worktree-pathed)', () => {
    const settings = {
      hooks: {
        PreToolUse: [
          { matcher: 'Bash', hooks: [{ type: 'command', command: 'node /somewhere/else/custom-gate.mjs', timeout: 10 }] },
        ],
      },
    }
    const before = JSON.stringify(settings)
    const { changed } = pruneStaleHookEntries(settings, { fileExists: () => false })
    expect(changed).toBe(false)
    expect(JSON.stringify(settings)).toBe(before)
  })

  it('prunes an unknown-named script when its path lies inside .claude/worktrees/', () => {
    const settings = {
      hooks: {
        SessionStart: [
          { hooks: [{ type: 'command', command: 'bash /x/.claude/worktrees/agent-1/scripts/some-future-hook.sh' }] },
        ],
      },
    }
    const { changed, removed } = pruneStaleHookEntries(settings, { fileExists: () => false })
    expect(changed).toBe(true)
    expect(removed).toHaveLength(1)
  })

  it('is a no-op on settings without a hooks block', () => {
    const settings: Record<string, unknown> = { permissions: { allow: [] } }
    const { changed, removed } = pruneStaleHookEntries(settings, { fileExists: () => false })
    expect(changed).toBe(false)
    expect(removed).toEqual([])
  })

  it('covers the incident hook script names in KNOWN_HOOK_SCRIPTS', () => {
    expect(KNOWN_HOOK_SCRIPTS).toContain('voice-reply-directive.py')
    expect(KNOWN_HOOK_SCRIPTS).toContain('taskstate-replay.py')
    expect(KNOWN_HOOK_SCRIPTS).toContain('staleness-guard.py')
    // channel-inbox-drain.py is app-registered (templates/settings.json.template),
    // so a missing-file entry must be prunable-as-ours, not treated as foreign.
    expect(KNOWN_HOOK_SCRIPTS).toContain('channel-inbox-drain.py')
  })
})

describe('pruneStaleHooksFromSettingsFile', () => {
  it('rewrites the file without stale entries and preserves the rest', () => {
    const dir = mkdtempSync(join(tmpdir(), 'hook-guard-test-'))
    try {
      // A real script file that must survive pruning.
      const liveScript = join(dir, 'staleness-guard.py')
      writeFileSync(liveScript, '# live')
      const settingsPath = join(dir, 'settings.json')
      writeFileSync(settingsPath, JSON.stringify({
        enabledPlugins: { x: true },
        hooks: {
          UserPromptSubmit: [
            {
              hooks: [
                { type: 'command', command: `python3 ${liveScript}`, timeout: 10 },
                { type: 'command', command: `python3 ${join(dir, '.claude', 'worktrees', 'agent-gone', 'voice-reply-directive.py')}`, timeout: 60 },
              ],
            },
          ],
        },
      }, null, 2))

      const removed = pruneStaleHooksFromSettingsFile(settingsPath)
      expect(removed).toHaveLength(1)
      const after = JSON.parse(readFileSync(settingsPath, 'utf-8'))
      expect(JSON.stringify(after.hooks)).toContain(liveScript)
      expect(JSON.stringify(after.hooks)).not.toContain('agent-gone')
      expect(after.enabledPlugins).toEqual({ x: true })
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('leaves a missing or unparseable file untouched', () => {
    const dir = mkdtempSync(join(tmpdir(), 'hook-guard-test-'))
    try {
      expect(pruneStaleHooksFromSettingsFile(join(dir, 'nope.json'))).toEqual([])
      const badPath = join(dir, 'settings.json')
      writeFileSync(badPath, '{ not json')
      expect(pruneStaleHooksFromSettingsFile(badPath)).toEqual([])
      expect(readFileSync(badPath, 'utf-8')).toBe('{ not json')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

// Upstream 42775042 (ported with 0e681c83): the fail-closed gate wrapper writes the script path as
// `"<path>"; else ...`, so a whitespace token ends in `";`. Without the strip the path was never read out
// of the new command form; with it, a fail-closed gate missing at the LIVE root is kept (it denies, which
// is its job) instead of being pruned into "no gate" (the injectors refuse to re-add a missing script).
describe('pruneStaleHookEntries on the fail-closed gate wrapper', () => {
  const MARK = 'governance-kapu (fail-closed)'
  // Shape of agent-scaffold failClosedGateRun() around a gate path (built by hand: this suite does not
  // load the scaffold; hook-fail-closed.test.ts pins FAIL_CLOSED_MARK to the scaffold's tag).
  const wrapped = (p: string) =>
    `test -x "/usr/bin/node" || { echo "x" >&2; exit 2; }; if command -v timeout >/dev/null 2>&1; then timeout -k 1 8 "/usr/bin/node" "${p}"; else "/usr/bin/node" "${p}"; fi; rc=$?; [ "$rc" = 0 ] || [ "$rc" = 2 ] || { echo "${MARK}: x: rc=$rc" >&2; exit 2; }; exit "$rc"`
  const ptu = (command: string) => ({ hooks: { PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command, timeout: 10 }] }] } })
  const LIVE = ['/opt/app/scripts', '/opt/app/scripts/hooks']

  it('reads the clean path out of the wrapper (no trailing "; on the checked path) and keeps a live gate', () => {
    const checked: string[] = []
    const s = ptu(wrapped('/opt/app/scripts/email-send-gate.mjs'))
    const before = JSON.stringify(s)
    const r = pruneStaleHookEntries(s, { fileExists: (p) => { checked.push(p); return p === '/opt/app/scripts/email-send-gate.mjs' }, expectedScriptDirs: LIVE })
    expect(checked).toContain('/opt/app/scripts/email-send-gate.mjs')
    expect(checked.every((p) => !p.endsWith(';') && !p.endsWith('"'))).toBe(true)
    expect(r.changed).toBe(false)
    expect(JSON.stringify(s)).toBe(before)
  })

  it('a worktree-pathed gate whose file EXISTS is kept (before the strip its `";` token never existed and was pruned)', () => {
    const p = '/x/.claude/worktrees/agent-1/scripts/self-pace-gate.mjs'
    const s = ptu(wrapped(p))
    const r = pruneStaleHookEntries(s, { fileExists: (q) => q === p })
    expect(r.changed).toBe(false)
    expect(r.removed).toEqual([])
  })

  it('a fail-closed gate MISSING at the live root stays (it denies until the file is back), and is reported', () => {
    const cmd = wrapped('/opt/app/scripts/email-send-gate.mjs')
    const s = ptu(cmd)
    const before = JSON.stringify(s)
    const r = pruneStaleHookEntries(s, { fileExists: () => false, expectedScriptDirs: LIVE })
    expect(r.changed).toBe(false)
    expect(r.keptFailClosed).toEqual([cmd])
    expect(JSON.stringify(s)).toBe(before)
  })

  it('the same missing gate on ANOTHER root (an old install, a gone worktree) is pruned as before', () => {
    for (const p of ['/old/install/scripts/email-send-gate.mjs', '/x/.claude/worktrees/agent-gone/scripts/self-pace-gate.mjs']) {
      const s = ptu(wrapped(p))
      const r = pruneStaleHookEntries(s, { fileExists: () => false, expectedScriptDirs: LIVE })
      expect(r.changed, p).toBe(true)
      expect(r.keptFailClosed).toEqual([])
      expect((s.hooks as Record<string, unknown>).PreToolUse).toBeUndefined()
    }
  })

  it('without the live root given, a missing fail-closed gate is pruned (the old contract for callers that pass none)', () => {
    const s = ptu(wrapped('/opt/app/scripts/email-send-gate.mjs'))
    expect(pruneStaleHookEntries(s, { fileExists: () => false }).changed).toBe(true)
  })

  it('a missing legacy (pre-wrapper) gate entry at the live root is still pruned: only the fail-closed form is kept', () => {
    const s = ptu('"/usr/bin/node" "/opt/app/scripts/email-send-gate.mjs"')
    const r = pruneStaleHookEntries(s, { fileExists: () => false, expectedScriptDirs: LIVE })
    expect(r.changed).toBe(true)
    expect(r.keptFailClosed).toEqual([])
  })

  it('a non-gate hook (no fail-closed mark) missing at the live root is pruned as before', () => {
    const s = { hooks: { UserPromptSubmit: [{ hooks: [{ type: 'command', command: 'python3 "/opt/app/scripts/hooks/voice-reply-directive.py"; true', timeout: 10 }] }] } }
    const r = pruneStaleHookEntries(s, { fileExists: () => false, expectedScriptDirs: LIVE })
    expect(r.changed).toBe(true)
  })

  it('file level: with liveRoot the missing live gate is kept and the file is not rewritten', () => {
    const dir = mkdtempSync(join(tmpdir(), 'hook-guard-fc-'))
    try {
      const settingsPath = join(dir, 'settings.json')
      const body = JSON.stringify(ptu(wrapped(join(dir, 'scripts', 'email-send-gate.mjs'))), null, 2)
      writeFileSync(settingsPath, body)
      expect(pruneStaleHooksFromSettingsFile(settingsPath, dir)).toEqual([])
      expect(readFileSync(settingsPath, 'utf-8')).toBe(body)
      // and without liveRoot the same entry goes (the pre-existing contract)
      expect(pruneStaleHooksFromSettingsFile(settingsPath)).toHaveLength(1)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})

describe('boot wiring', () => {
  it('web.ts passes the live PROJECT_ROOT to the boot prune, so a missing live fail-closed gate is kept', () => {
    const src = readFileSync(join(__dirname, '..', 'web.ts'), 'utf-8')
    expect(src).toContain('pruneStaleHooksFromSettingsFile(agentSettingsPath(agentName), PROJECT_ROOT)')
  })
})

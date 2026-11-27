import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { classifyWorkerPane, shouldSelfHeal } from '../web/agent-worker.js'

// Layer-2 regression tests for the worker stuck-modal self-heal (card
// e7c1bc7e): the classifier must single out parked dialog chrome without ever
// flagging a busy turn, a healthy prompt, or the auth chrome (those have their
// own handling). Fixtures reproduce real `tmux capture-pane -p` output.

const SEP = '─'.repeat(80)

// The actual CC 2.1.202 fullscreen-renderer upsell, captured live on the
// production worker session on 2026-07-08.
const FULLSCREEN_UPSELL = [
  ' ⚠ 1 setup issue: MCP · /doctor',
  '',
  SEP,
  '  Try the new fullscreen renderer?',
  '',
  '  · Flicker-free output, mouse support, clipboard auto-copy',
  '',
  '  ❯ 1. Yes, try it',
  '    2. Not now',
  '',
  '  Enter to confirm · Esc to cancel',
].join('\n')

// Trust-folder style first-run dialog (same family, option list).
const TRUST_DIALOG = [
  '  Do you trust the files in this folder?',
  '',
  '  ❯ 1. Yes, proceed',
  '    2. No, exit',
  '',
  '  Enter to confirm · Esc to cancel',
].join('\n')

const IDLE = ['', SEP, '❯ ', SEP, '  ⏵⏵ bypass permissions on (shift+tab to cycle)'].join('\n')

const BUSY = [
  '✻ Baking… (12s · 1.2k tokens · esc to interrupt)',
  SEP,
  '❯ ',
  SEP,
  '  ⏵⏵ bypass permissions on (shift+tab to cycle)',
].join('\n')

const AUTH_FAILURE = [
  '',
  '  Please run /login',
  '',
  '❯ ',
].join('\n')

// Unrecognised overlay: text, but no idle footer, no busy marker, no options.
const UNKNOWN_OVERLAY = [
  '  Something new the harness shipped',
  '  that we have never seen before',
].join('\n')

describe('classifyWorkerPane', () => {
  it('classifies the live-captured fullscreen upsell as modal', () => {
    expect(classifyWorkerPane(FULLSCREEN_UPSELL)).toBe('modal')
  })

  it('classifies the trust-dialog family as a DIALOG, which is answered, never Escaped (BELA 6227 item 6)', () => {
    // Escape on a trust / bypass panel is "No, exit": the worker would quit and respawn into the same panel.
    expect(classifyWorkerPane(TRUST_DIALOG)).toBe('dialog')
    expect(shouldSelfHeal('dialog')).toBe(false)
  })

  it('the 2.1.326 trust and bypass captures, the resume and the consent dialogs are dialogs too', () => {
    const fx = (n: string) => readFileSync(join(__dirname, 'fixtures', 'pane-2.1.326', n), 'utf-8')
    for (const n of ['first-run-trust-80x24.txt', 'first-run-bypass-80x24.txt']) expect(classifyWorkerPane(fx(n))).toBe('dialog')
    const resume = ['Resume this conversation?', '', '  Resume', '❯ Start a new conversation', '  Resume from summary', '', 'Enter to confirm · Esc to resume'].join('\n')
    expect(classifyWorkerPane(resume)).toBe('dialog')
    const consent = [' Fable 5 now uses usage credits', '', ' ❯ Switch to Sonnet 5 and continue', '   Continue with Fable 5', '', ' Enter to confirm · Esc to cancel'].join('\n')
    expect(classifyWorkerPane(consent)).toBe('dialog')
    // the generic upsell modal stays a self-heal target
    expect(classifyWorkerPane(FULLSCREEN_UPSELL)).toBe('modal')
  })

  it('selfHealWorkerOnce answers a dialog through the shared handlers and only Escapes the other classes', () => {
    const src = readFileSync(join(__dirname, '..', 'web', 'agent-worker.ts'), 'utf-8')
    const fn = src.slice(src.indexOf('async function selfHealWorkerOnce('), src.indexOf('/** Loud, rate-limited operator signal'))
    const dialog = fn.indexOf("if (cls === 'dialog') {")
    const escape = fn.indexOf("'Escape'")
    expect(dialog).toBeGreaterThan(0)
    expect(escape).toBeGreaterThan(dialog)
    const branch = fn.slice(dialog, fn.indexOf('if (!shouldSelfHeal(cls)) return false'))
    expect(branch).toContain('await answerFirstRunGates(ctx.session)')
    expect(branch).not.toContain("'Escape'")
    expect(src).toContain('try { await selfHealWorkerOnce(ctx) }')
  })

  it('never flags a healthy prompt or a live turn', () => {
    expect(classifyWorkerPane(IDLE)).toBe('idle')
    expect(classifyWorkerPane(BUSY)).toBe('busy')
  })

  it('routes the auth chrome to the auth recovery, not the self-heal', () => {
    expect(classifyWorkerPane(AUTH_FAILURE)).toBe('auth')
  })

  it('treats an empty capture as inconclusive (still booting)', () => {
    expect(classifyWorkerPane(null)).toBe('empty')
    expect(classifyWorkerPane('   \n  ')).toBe('empty')
  })

  it('flags unrecognised full-screen overlays as unknown', () => {
    expect(classifyWorkerPane(UNKNOWN_OVERLAY)).toBe('unknown')
  })
})

describe('shouldSelfHeal', () => {
  it('heals modal and unknown panes only', () => {
    expect(shouldSelfHeal('modal')).toBe(true)
    expect(shouldSelfHeal('unknown')).toBe(true)
    expect(shouldSelfHeal('idle')).toBe(false)
    expect(shouldSelfHeal('busy')).toBe(false)
    expect(shouldSelfHeal('auth')).toBe(false)
    expect(shouldSelfHeal('empty')).toBe(false)
    expect(shouldSelfHeal('dialog')).toBe(false)
  })
})

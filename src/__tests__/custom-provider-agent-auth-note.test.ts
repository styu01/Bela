// HOSTINGERRETEST1125 (upstream c40e1549, the provider part only; BELA 6404): a sub-agent on a custom provider runs on
// that provider's key, so the agent page does not offer it the Claude login choices (auth mode, Claude subscription);
// a note says why. The main agent and a Claude sub-agent are unchanged.
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'

const APP = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf-8')
const HTML = readFileSync(new URL('../../web/index.html', import.meta.url), 'utf-8')
const HU = readFileSync(new URL('../../web/lang/hu.js', import.meta.url), 'utf-8')
const EN = readFileSync(new URL('../../web/lang/en.js', import.meta.url), 'utf-8')
const fnBody = (name: string) => {
  const i = APP.indexOf(`function ${name}(`)
  return i < 0 ? '' : APP.slice(i, APP.indexOf('\n}\n', i))
}

describe('the agent page on a custom-provider sub-agent', () => {
  const RO = fnBody('applyMarveenReadonlyMode')

  it('decides "on a provider" only for an editable sub-agent with a customProvider', () => {
    expect(RO).toContain("const onProvider = !readOnly && !!(agent && agent.role !== 'main' && agent.customProvider)")
  })

  it('hides the auth mode group and shows the note with the provider named', () => {
    expect(RO).toContain('authModeGroup.hidden = readOnly || onProvider')
    expect(RO).toContain('providerNote.hidden = !onProvider')
    expect(RO).toContain("t('agents.plan_login.provider_note', { provider: agent.customProvider })")
    // textContent, never innerHTML: the provider id is data
    expect(RO).toMatch(/providerNoteText\.textContent = t\(/)
  })

  it('hides the Claude subscription group too', () => {
    expect(APP).toContain("planGroup.hidden = currentAgent.role === 'main' || !!currentAgent.customProvider")
  })

  it('the note element is in the page, hidden by default, next to the plan group', () => {
    const note = HTML.indexOf('id="agentProviderAuthNote" hidden')
    expect(note).toBeGreaterThan(HTML.indexOf('id="claudePlanGroup"'))
    expect(HTML).toContain('id="agentProviderAuthNoteText"')
  })

  it('the note text is in both languages, with the {provider} placeholder', () => {
    expect(HU).toMatch(/'agents\.plan_login\.provider_note': '[^']*\{provider\}/)
    expect(EN).toMatch(/'agents\.plan_login\.provider_note': '.*\{provider\}/)
  })
})

// Settings > Providers (custom Anthropic-compatible endpoints) answered a missing field and a failed save with
// alert(), in hard-coded Hungarian. These tests pin that the form points at the field and says what is missing,
// and that a failed save shows a translated toast.
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'

const APP = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf-8')
const HU = readFileSync(new URL('../../web/lang/hu.js', import.meta.url), 'utf-8')
const EN = readFileSync(new URL('../../web/lang/en.js', import.meta.url), 'utf-8')

const between = (src: string, from: string, to: string) => {
  const i = src.indexOf(from)
  const j = i < 0 ? -1 : src.indexOf(to, i)
  return i < 0 || j < 0 ? '' : src.slice(i, j)
}
// LOCAL ADAPTATION: this fork has no confirmDialog, so the section ends at the next function after the
// provider modal (buildSettingRow) instead of upstream's "The app's own confirmation dialog" comment.
const SECTION = between(APP, 'async function renderProvidersContent(', '\nfunction buildSettingRow(')
const SAVE = between(SECTION, "overlay.querySelector('#addProviderSaveBtn').addEventListener('click'", '\n}\n')
const hasKey = (lang: string, key: string) => lang.includes(`'${key}':`)

describe('the provider form says which field is missing', () => {
  it('the section and the save handler were found', () => {
    expect(SECTION).not.toBe('')
    expect(SAVE).not.toBe('')
  })

  it('no alert() is left in the section', () => {
    expect(SECTION).not.toMatch(/(^|[^.\w])alert\(/)
  })

  it('the first empty required field, in the form\'s order, gets the focus and its own toast, before any request', () => {
    const order = ["['#cpLabel', 'providers.form.label_required']", "['#cpId', 'providers.form.id_required']",
      "['#cpBaseUrl', 'providers.form.base_url_required']", "['#cpVaultKey', 'providers.form.vault_key_required']"]
    let last = -1
    for (const step of order) {
      const at = SAVE.indexOf(step)
      expect(at, step).toBeGreaterThan(last)
      last = at
    }
    expect(SAVE).toContain("(authHeader !== 'none' && !vaultKey) ? ['#cpVaultKey'")
    const stop = SAVE.indexOf('if (missing) { overlay.querySelector(missing[0]).focus(); showToast(t(missing[1])); return }')
    expect(stop).toBeGreaterThan(last)
    expect(SAVE.slice(0, stop)).not.toMatch(/\bfetch\(/)
  })

  it('a failed save shows the server\'s reason or a translated toast', () => {
    expect(SAVE).toContain("showToast(err.error || t('providers.toast.save_error'))")
    expect(SAVE).toContain("catch { showToast(t('providers.toast.save_error')) }")
  })

  it('the keys are in both languages', () => {
    for (const key of ['providers.form.label_required', 'providers.form.id_required', 'providers.form.base_url_required',
      'providers.form.vault_key_required', 'providers.toast.save_error']) {
      expect(hasKey(HU, key), key).toBe(true)
      expect(hasKey(EN, key), key).toBe(true)
    }
  })
})

describe('the Providers tab is translated', () => {
  it('the hard-coded Hungarian texts are gone', () => {
    expect(APP).not.toContain("'Provider-ok'")
    for (const gone of ['Betöltés...</p>', 'Hiba a provider lista', 'Tiszta OpenAI végponthoz proxy', '>Név</th>', '>Vault kulcs</th>',
      '>nincs</em>', '>Szerkesztés</button>', '>Törlés</button>', 'Nincs egyéni provider', "'+ Új provider'", 'Provider szerkesztése', 'Új egyéni provider',
      'Megjelenő név *', 'Provider azonosító', 'pl. DeepSeek', 'Ez a provider belső neve', '(https:// vagy http://localhost)</small>', 'Auth header *',
      'Ollama-szerű, token nélkül)</option>', 'Vault kulcs neve *', 'Vault tabon', 'fordítóproxy', '>Mégsem<', '>Mentés</button>']) {
      expect(SECTION, gone).not.toContain(gone)
    }
  })

  it('every providers.* key the page uses is in both languages', () => {
    const used = new Set([...APP.matchAll(/t\('(providers\.[a-z_.]+)'/g)].map(m => m[1]))
    for (const m of APP.matchAll(/'(providers\.modal\.(?:edit|add)_title)'/g)) used.add(m[1])
    expect(used.size).toBeGreaterThan(30)
    for (const key of used) {
      expect(hasKey(HU, key), `hu: ${key}`).toBe(true)
      expect(hasKey(EN, key), `en: ${key}`).toBe(true)
    }
  })

  it('the translated texts that go into innerHTML are escaped', () => {
    const raw = [...SECTION.matchAll(/\$\{t\('/g)]
    expect(raw.length, 'a ${t(...)} without escapeHtml').toBe(0)
  })

  it('the tab button and the agent editor\'s model group are named from a key', () => {
    expect(APP).toContain("btn.textContent = t('providers.tab')")
    expect(APP).toContain("g.label = '🔧 ' + t('providers.optgroup')")
  })

  it('the table has a "Used by" column from usedBy', () => {
    expect(SECTION).toContain("escapeHtml(t('providers.col.used_by'))")
    expect(SECTION).toContain("(p.usedBy || []).length ? escapeHtml(p.usedBy.join(', '))")
  })
})

// LOCAL ADAPTATION of upstream's confirm-dialog.test.ts "eighth block" (this fork has no confirmDialog): the
// delete asks with the native confirm(), but the question is the translated one and names the agents that use
// the provider, and a failed delete is a translated toast (or the server's reason), not an alert().
describe('the provider delete asks before it deletes, naming the agents that use the provider', () => {
  const DEL = between(SECTION, "tbody.querySelectorAll('[data-provider-delete]')", '\n  } else {')

  it('the delete handler was found', () => {
    expect(DEL).not.toBe('')
  })

  it('the translated question, with the users, is asked before the DELETE request', () => {
    expect(DEL).toContain("t('providers.confirm.delete', { name: (p && p.label) || id })")
    expect(DEL).toContain("t('providers.confirm.delete_used_by'")
    const ask = DEL.indexOf('if (!confirm(message)) return')
    expect(ask).toBeGreaterThan(-1)
    expect(ask).toBeLessThan(DEL.indexOf("method: 'DELETE'"))
    expect(DEL).not.toMatch(/Biztosan törlöd/)
  })

  it('a failed delete shows the server reason or a translated toast', () => {
    expect(DEL).toContain("showToast(err.error || t('providers.toast.delete_error'))")
    expect(DEL).toContain("catch { showToast(t('providers.toast.delete_error')) }")
  })

  it('the delete keys are in both languages', () => {
    for (const key of ['providers.confirm.delete', 'providers.confirm.delete_used_by', 'providers.toast.delete_error']) {
      expect(hasKey(HU, key), key).toBe(true)
      expect(hasKey(EN, key), key).toBe(true)
    }
  })
})

// PROGI review msg 6192 small items: after a 409 the edit form reloads the board (a reopen within the
// 30 s refresh fills from current data), and the parent dropdown goes back to the saved parent on any
// refusal (it must not show a parent that was never saved).
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const app = readFileSync(join(__dirname, '..', '..', 'web', 'app.js'), 'utf-8')

describe('web/app.js 409 handling', () => {
  it('the edit form calls loadKanban() in its 409 branch, and still keeps the modal open (return)', () => {
    const at = app.indexOf("showToast(t('kanban.toast.stale_version_form'), 8000)")
    expect(at).toBeGreaterThan(0)
    const tail = app.slice(at, at + 200)
    expect(tail).toMatch(/loadKanban\(\)\s*\n\s*return/)
  })
  it('the parent dropdown resets to the stored parent on every non-ok answer, before the toast', () => {
    const at = app.indexOf('parentSelect.onchange = async () => {')
    const body = app.slice(at, app.indexOf("parentMetaItem.style.display = ''", at))
    const elseIdx = body.indexOf('} else {')
    const resetIdx = body.indexOf("parentSelect.value = card.parent_id || ''")
    expect(elseIdx).toBeGreaterThan(0)
    expect(resetIdx).toBeGreaterThan(elseIdx)
    expect(body.indexOf("showToast(t('kanban.toast.stale_version'), 6000)")).toBeGreaterThan(resetIdx)
  })
})

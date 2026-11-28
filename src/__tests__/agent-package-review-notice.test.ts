// KISPROGI 6464 M1 (this fork): a package review request is a resolvable incident. Its escalation key names the agent,
// a newer request for the same agent supersedes the older, the owner approval clears it (so the stage-2 owner alert
// about an already approved package never goes), and the owner's text is a short, plain Hungarian sentence.
import { describe, it, expect, vi, beforeEach } from 'vitest'

const h = vi.hoisted(() => ({ escalated: [] as Array<{ key: string; belaText: string; ownerText: string }>, cleared: [] as string[] }))
vi.mock('../web/owner-escalation.js', () => ({
  escalateToOwner: (o: { key: string; belaText: string; ownerText: string }) => { h.escalated.push(o) },
  clearOwnerEscalation: (k: string) => { h.cleared.push(k) },
}))
const N = await import('../web/agent-package/notices.js')

beforeEach(() => {
  h.escalated.length = 0
  h.cleared.length = 0
  N.clearPackageReviewNotice('nora')
  N.clearPackageReviewNotice('ola')
  h.cleared.length = 0
})

describe('package review notices', () => {
  it('a review request: a per-agent key, BELA gets the details, the owner the plain sentence', async () => {
    await N.escalatePackageNotice('BELA details', { reviewOf: 'nora', ownerText: N.installReviewOwnerText('nora', '1.0.0') })
    expect(h.escalated).toHaveLength(1)
    expect(h.escalated[0].key).toMatch(/^agent-package-review:nora:[0-9a-f]{16}$/)
    expect(h.escalated[0].belaText).toBe('BELA details')
    expect(h.escalated[0].ownerText).toBe(N.installReviewOwnerText('nora', '1.0.0'))
    expect(N._packageReviewKeyForTest('nora')).toBe(h.escalated[0].key)
  })

  it('the approval clears the open request; a second clear finds nothing', async () => {
    await N.escalatePackageNotice('BELA details', { reviewOf: 'nora' })
    const key = h.escalated[0].key
    expect(N.clearPackageReviewNotice('nora')).toBe(true)
    expect(h.cleared).toEqual([key])
    expect(N.clearPackageReviewNotice('nora')).toBe(false)
    expect(N._packageReviewKeyForTest('nora')).toBeNull()
  })

  it('a newer request for the same agent supersedes the older (no stale owner alert about the old version)', async () => {
    await N.escalatePackageNotice('installed 1.0.0', { reviewOf: 'nora' })
    await N.escalatePackageNotice('updated to 1.1.0', { reviewOf: 'nora' })
    expect(h.escalated.map((e) => e.key)).toHaveLength(2)
    expect(h.escalated[0].key).not.toBe(h.escalated[1].key)
    expect(h.cleared).toEqual([h.escalated[0].key])
    expect(N._packageReviewKeyForTest('nora')).toBe(h.escalated[1].key)
  })

  it('the same request again keeps its key (the escalation only refreshes its text); other agents are separate', async () => {
    await N.escalatePackageNotice('same', { reviewOf: 'nora' })
    await N.escalatePackageNotice('same', { reviewOf: 'nora' })
    await N.escalatePackageNotice('same', { reviewOf: 'ola' })
    expect(h.cleared).toEqual([])
    expect(h.escalated[0].key).toBe(h.escalated[1].key)
    expect(h.escalated[2].key).toMatch(/^agent-package-review:ola:/)
    N.clearPackageReviewNotice('ola')
    expect(N._packageReviewKeyForTest('nora')).toBe(h.escalated[0].key)
  })

  it('any other package notice keeps its per-text key, is not tracked, and the owner gets the same text', async () => {
    await N.escalatePackageNotice('fallback refused for x')
    expect(h.escalated[0]).toMatchObject({ belaText: 'fallback refused for x', ownerText: 'fallback refused for x' })
    expect(h.escalated[0].key).toMatch(/^agent-package-notice:[0-9a-f]{16}$/)
    expect(N.clearPackageReviewNotice('x')).toBe(false)
  })

  it('the owner texts: plain Hungarian with accents, no endpoint, no em or en dash, no spaced double hyphen', () => {
    for (const t of [N.installReviewOwnerText('nora', '1.0.0'), N.updateReviewOwnerText('nora', '1.1.0')]) {
      expect(t).toContain('nora')
      expect(t).toMatch(/jóváhagy/)
      expect(t).toContain('Ügynök-katalógus')
      expect(t).not.toMatch(/\/api\/|POST|owner-munkamenet|[–—]| -- /)
      expect(t.length).toBeLessThan(220)
    }
    expect(N.updateReviewOwnerText('nora', '1.1.0')).toContain('1.1.0')
  })
})

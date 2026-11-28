// How the agent package installer's owner notices and review requests leave the machine (BELA 6404). Upstream sends
// them to the owner directly (notifyOwner). This fork's standing rule is that the system's own alerts go to BELA
// first and to the owner only if they stay unresolved, which owner-escalation.ts implements for every internal
// alert; these go the same way. Each text gets its own key, so two notices never fold into one.
//
// A review request (an installed or updated package waiting for the owner approval, KISPROGI 6464 M1) is a resolvable
// incident: the approval resolves it. Its key names the agent (and the text, so a later request is its own incident),
// a newer request for the same agent supersedes the older one, and the approval clears it, so the owner is not sent
// the stage-2 alert about a package already approved. Its owner text is a short, plain Hungarian sentence; the
// details (files, scan counts, how to review) stay in BELA's text.
import { createHash } from 'node:crypto'
import { clearOwnerEscalation, escalateToOwner } from '../owner-escalation.js'

export interface PackageNoticeOptions {
  /** The agent this review request is about (install or update). */
  reviewOf?: string
  /** The owner's stage-2 text; without it the owner gets BELA's text, as for every other package notice. */
  ownerText?: string
}

// name -> the key of its newest review request. Process-local, like the escalation state it points into.
const reviewKeys = new Map<string, string>()

const digestOf = (text: string): string => createHash('sha256').update(text).digest('hex').slice(0, 16)

export async function escalatePackageNotice(text: string, opts: PackageNoticeOptions = {}): Promise<boolean> {
  const ownerText = opts.ownerText ?? text
  if (opts.reviewOf !== undefined) {
    const key = `agent-package-review:${opts.reviewOf}:${digestOf(text)}`
    const older = reviewKeys.get(opts.reviewOf)
    if (older !== undefined && older !== key) clearOwnerEscalation(older)
    reviewKeys.set(opts.reviewOf, key)
    escalateToOwner({ key, belaText: text, ownerText })
    return true
  }
  escalateToOwner({ key: `agent-package-notice:${digestOf(text)}`, belaText: text, ownerText })
  return true
}

/** The owner approved `name`: its open review request is resolved (its stage-2 owner alert, if not sent yet, never
 *  goes). Returns whether there was one. */
export function clearPackageReviewNotice(name: string): boolean {
  const key = reviewKeys.get(name)
  if (key === undefined) return false
  clearOwnerEscalation(key)
  reviewKeys.delete(name)
  return true
}

/** Test seam: the key of `name`'s open review request, or null. */
export function _packageReviewKeyForTest(name: string): string | null {
  return reviewKeys.get(name) ?? null
}

/** The owner texts of the two review requests: what happened, that it does not start, where to approve. */
export function installReviewOwnerText(name: string, version: string): string {
  return `Új ügynök-csomag vár a jóváhagyásodra: ${name} (${version}). Amíg nem hagyod jóvá, nem indul el. Jóváhagyni a dashboard Ügynök-katalógus oldalán tudod.`
}
export function updateReviewOwnerText(name: string, version: string): string {
  return `Frissült egy ügynök-csomag, újra jóváhagyásra vár: ${name} (új verzió: ${version}). Amíg nem hagyod jóvá, nem indul el. Jóváhagyni a dashboard Ügynök-katalógus oldalán tudod.`
}

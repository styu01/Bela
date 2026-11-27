// How the agent package installer's owner notices and review requests leave the machine (BELA 6404). Upstream sends
// them to the owner directly (notifyOwner). This fork's standing rule is that the system's own alerts go to BELA
// first and to the owner only if they stay unresolved, which owner-escalation.ts implements for every internal
// alert; these go the same way. Each text gets its own key, so two notices never fold into one.
import { createHash } from 'node:crypto'
import { escalateToOwner } from '../owner-escalation.js'

export async function escalatePackageNotice(text: string): Promise<boolean> {
  escalateToOwner({ key: `agent-package-notice:${createHash('sha256').update(text).digest('hex').slice(0, 16)}`, belaText: text, ownerText: text })
  return true
}

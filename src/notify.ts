import { CHANNEL_PROVIDER, CHANNEL_TOKEN, CHANNEL_CHAT_ID } from './config.js'
import { getProvider } from './channel-provider.js'
import { logger } from './logger.js'
import { markIfTestRun } from './test-run-marker.js'
import { normalizeChatId } from './owner-chat.js'

export async function notifyChannel(text: string): Promise<void> {
  // CHATID0 (2026-09-08): "0" is the installer's placeholder for an
  // un-paired chat -- normalizeChatId is what treats it as "not set" instead
  // of a truthy, deliverable-looking value (the whole reason
  // ALLOWED_CHAT_ID=0 survived: `!"0"` is false). normalizeChatId, NOT
  // resolveOwnerChatId, on purpose: this function is provider-agnostic
  // (telegram/slack/discord/googlechat/teams), and resolveOwnerChatId's
  // access.json fallback is a Telegram-specific concept that would be wrong
  // to apply to e.g. a Slack-configured install's CHANNEL_CHAT_ID.
  const chatId = normalizeChatId(CHANNEL_CHAT_ID)
  if (!CHANNEL_TOKEN || !chatId) {
    logger.warn('Channel ertesites kihagyva: token vagy chat ID hianyzik')
    return
  }

  // Marked here at the funnel, NOT at call sites -- a new caller must not be
  // able to leak an unmarked message from a test run.
  const outbound = markIfTestRun(text)
  const provider = getProvider(CHANNEL_PROVIDER)
  const formatted = provider.formatMessage(outbound)
  const chunks = provider.splitMessage(formatted)

  const parseMode = CHANNEL_PROVIDER === 'telegram' ? 'HTML' : undefined
  for (let i = 0; i < chunks.length; i++) {
    const chunk = chunks[i]
    try {
      await provider.sendMessage(CHANNEL_TOKEN, chatId, chunk, parseMode)
    } catch {
      // #1878 (9): the fallback is THIS chunk without formatting. It used to be
      // outbound.slice(0, 4096), so a failure on chunk N sent the start of the
      // whole message again and chunk N was never delivered.
      try {
        await provider.sendMessage(CHANNEL_TOKEN, chatId, parseMode === 'HTML' ? telegramHtmlToPlain(chunk) : chunk)
      } catch (err2) {
        // Last resort failed too: say so (index only, never the text).
        logger.warn({ err: err2 instanceof Error ? err2.message : String(err2), chunk: i + 1, of: chunks.length }, 'Channel ertesites: egy resz nem ment ki (formazva sem, sima szovegkent sem)')
      }
    }
  }
}

// The plain-text form of a chunk formatForTelegram produced: its own tags
// dropped (only those, so a literal "<5 perc>" in the text survives) and the
// three entities its escapeHtml writes turned back into characters.
export function telegramHtmlToPlain(html: string): string {
  return html
    .replace(/<\/?(?:b|i|s|code|pre)>|<code class="language-[^"]*">|<a href="[^"]*">|<\/a>/g, '')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
}

// Backward-compatible alias
export const notifyTelegram = notifyChannel

// Kanban cf12a93a (2026-09-02, Codex review round 2): notifyChannel swallows
// every send failure internally (the inner catch/retry/give-up above never
// rejects the outer promise) -- fine for a fire-and-forget notice, but WRONG
// for a caller that needs to know whether delivery actually succeeded before
// marking its own state "sent" (owner-escalation.ts's stage-2 alert: if the
// send silently failed and the caller stamped "sent" anyway, it would never
// retry, and Istvan would never actually receive the escalation). This
// throws on failure instead of swallowing -- same chunking/formatting/
// provider-agnostic behavior as notifyChannel, just without the silent
// give-up. A missing token/chat ID also throws here (notifyChannel treats
// that as an expected, silently-skippable state; a caller using THIS
// function explicitly wants to know delivery didn't happen).
export async function notifyChannelOrThrow(text: string): Promise<void> {
  // CHATID0: same reasoning as notifyChannel above -- this is the ONLY
  // delivery path for owner-escalation.ts's stage-2 alert (the "Istvan gets
  // notified directly" fallback), so a placeholder "0" silently passing this
  // guard would mean the most severe escalation tier never actually reaches
  // anyone while looking like it succeeded (sendMessage would 400, caught by
  // the caller as "delivery failed" -- but only AFTER already committing to
  // this being the notification path of last resort).
  const chatId = normalizeChatId(CHANNEL_CHAT_ID)
  if (!CHANNEL_TOKEN || !chatId) {
    throw new Error('Channel ertesites nem lehetseges: token vagy chat ID hianyzik')
  }
  const outbound = markIfTestRun(text)
  const provider = getProvider(CHANNEL_PROVIDER)
  const formatted = provider.formatMessage(outbound)
  const chunks = provider.splitMessage(formatted)
  const parseMode = CHANNEL_PROVIDER === 'telegram' ? 'HTML' : undefined
  for (const chunk of chunks) {
    await provider.sendMessage(CHANNEL_TOKEN, chatId, chunk, parseMode)
  }
}

// Security-event notification (break-glass password reset, security:reset).
// Unlike notifyChannel, a missing channel config is an EXPECTED state here
// (fresh installs, channel-less deployments), so it stays fully silent -- the
// recovery path must never depend on, or be noisy about, Telegram being wired.
export async function notifySecurityEvent(text: string): Promise<void> {
  if (!CHANNEL_TOKEN || !normalizeChatId(CHANNEL_CHAT_ID)) return
  try {
    await notifyChannel(text)
  } catch {
    /* never let a notification failure break the recovery action itself */
  }
}

import { describe, it, expect, vi, beforeEach } from 'vitest'

const mockSendMessage = vi.fn((..._a: unknown[]) => Promise.resolve())
const mockGetProvider = vi.fn((..._a: unknown[]) => ({
  formatMessage: (t: string) => t,
  splitMessage: (t: string) => [t],
  sendMessage: (...a: unknown[]) => mockSendMessage(...a),
}))

vi.mock('../logger.js', () => ({
  logger: { info: vi.fn(), warn: vi.fn(), debug: vi.fn(), error: vi.fn() },
}))

vi.mock('../config.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../config.js')>()),
  CHANNEL_PROVIDER: 'telegram',
  CHANNEL_TOKEN: 'test-token',
  CHANNEL_CHAT_ID: 'test-chat',
}))

vi.mock('../channel-provider.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../channel-provider.js')>()),
  getProvider: (...a: unknown[]) => mockGetProvider(...a),
}))

vi.mock('../test-run-marker.js', () => ({
  markIfTestRun: (t: string) => t,
}))

import { notifyChannelOrThrow } from '../notify.js'

describe('notifyChannelOrThrow', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockSendMessage.mockResolvedValue(undefined)
  })

  it('resolves when the provider send succeeds', async () => {
    await expect(notifyChannelOrThrow('hello')).resolves.toBeUndefined()
    expect(mockSendMessage).toHaveBeenCalledWith('test-token', 'test-chat', 'hello', 'HTML')
  })

  it('THROWS when the provider send fails (unlike notifyChannel, which swallows)', async () => {
    mockSendMessage.mockRejectedValueOnce(new Error('Telegram API 500: boom'))
    await expect(notifyChannelOrThrow('hello')).rejects.toThrow('Telegram API 500')
  })

  it('does not retry with a truncated fallback on failure -- one attempt, throw', async () => {
    mockSendMessage.mockRejectedValueOnce(new Error('Telegram API 400: bad request'))
    await expect(notifyChannelOrThrow('hello')).rejects.toThrow()
    expect(mockSendMessage).toHaveBeenCalledTimes(1)
  })
})

// CHATID0 (2026-09-08): "0" is the installer's placeholder for an un-paired
// chat. notifyChannelOrThrow is the ONLY delivery path for owner-escalation's
// stage-2 alert -- if a placeholder "0" passed as a truthy, deliverable-
// looking chat id, the send would 400 at the API instead of being recognised
// up front as "not configured", and the most severe escalation tier would
// silently never reach anyone. A separate module-mock scope is needed here
// (vi.resetModules + vi.doMock) since the describe block above pins
// CHANNEL_CHAT_ID to a real-looking value at module load time.
describe('notifyChannelOrThrow with CHANNEL_CHAT_ID="0" (placeholder, not a real chat)', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.clearAllMocks()
    mockSendMessage.mockResolvedValue(undefined)
  })

  it('throws "not possible" (not a provider 400) and never calls sendMessage', async () => {
    vi.doMock('../config.js', async (importOriginal) => ({
      ...(await importOriginal<typeof import('../config.js')>()),
      CHANNEL_PROVIDER: 'telegram',
      CHANNEL_TOKEN: 'test-token',
      CHANNEL_CHAT_ID: '0',
    }))
    const { notifyChannelOrThrow: notifyWithZero } = await import('../notify.js')
    await expect(notifyWithZero('hello')).rejects.toThrow('Channel ertesites nem lehetseges')
    expect(mockSendMessage).not.toHaveBeenCalled()
  })
})

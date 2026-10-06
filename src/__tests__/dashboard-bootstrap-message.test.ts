import { describe, expect, it } from 'vitest'
import { dashboardBootstrapMessage } from '../web/dashboard-bootstrap-message.js'

describe('dashboard bootstrap stderr message', () => {
  const sentinel = 'not-a-real-' + 'dashboard-token'

  it('shows the bootstrap URL only on an interactive terminal', () => {
    expect(dashboardBootstrapMessage(3420, sentinel, true)).toContain(`?token=${sentinel}`)
  })

  it('never writes the token when stderr is redirected', () => {
    const message = dashboardBootstrapMessage(3420, sentinel, false)
    expect(message).not.toContain(sentinel)
    expect(message).toContain('store/.dashboard-token')
  })
})

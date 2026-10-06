// Best-effort, cached measurement used by the model-picker compatibility gate.
import { execFile } from 'node:child_process'
import { tryResolveFromPath } from '../platform.js'
import { parseClaudeVersion } from '../claude-cli-support.js'

export interface ClaudeCliVersionResult {
  version: string | null
  measuredAt: number
  error: string | null
  source: 'probe' | 'env'
}

export const CLI_VERSION_OVERRIDE_ENV = 'MARVEEN_CLAUDE_CLI_VERSION'
const CACHE_TTL_MS = 10 * 60 * 1000
const PROBE_TIMEOUT_MS = 10_000
let cache: ClaudeCliVersionResult | null = null

function fromEnv(): ClaudeCliVersionResult | null {
  const raw = process.env[CLI_VERSION_OVERRIDE_ENV]
  if (raw === undefined) return null
  const version = parseClaudeVersion(raw)
  return {
    version,
    measuredAt: Date.now(),
    error: version ? null : `override ${CLI_VERSION_OVERRIDE_ENV} carries no version`,
    source: 'env',
  }
}

export function measureClaudeCliVersion(opts: { fresh?: boolean } = {}): Promise<ClaudeCliVersionResult> {
  const override = fromEnv()
  if (override) return Promise.resolve(override)
  if (!opts.fresh && cache && Date.now() - cache.measuredAt < CACHE_TTL_MS) return Promise.resolve(cache)
  return new Promise((resolve) => {
    const binary = tryResolveFromPath('claude')
    if (!binary) {
      cache = { version: null, measuredAt: Date.now(), error: 'claude binary not found on PATH', source: 'probe' }
      resolve(cache)
      return
    }
    execFile(binary, ['--version'], { encoding: 'utf8', timeout: PROBE_TIMEOUT_MS }, (err, stdout) => {
      const version = parseClaudeVersion(String(stdout ?? ''))
      const error = err
        ? `claude --version failed: ${err.message.slice(0, 120)}`
        : version ? null : `unparseable output: ${String(stdout ?? '').slice(0, 60)}`
      cache = { version: err ? null : version, measuredAt: Date.now(), error, source: 'probe' }
      resolve(cache)
    })
  })
}

export function resetClaudeCliVersionCache(): void { cache = null }

// Reads the quota snapshot scripts/usage-collect.py writes to
// store/usage-latest.json and hands it to the pure gate in quota-gate.ts.
//
// Everything here degrades to null: a missing file, a half-written file, a
// shape we do not recognise. Null means "we do not know", and the gate treats
// not-knowing as permission to run (see quota-gate.ts, FAIL OPEN).

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { STORE_DIR } from './config.js'
import type { QuotaSnapshot } from './quota-gate.js'

export const USAGE_SNAPSHOT_PATH = join(STORE_DIR, 'usage-latest.json')

/**
 * Map the collector's on-disk JSON onto the gate's input shape.
 * Exported separately from the file read so the mapping is unit-testable.
 */
export function parseQuotaSnapshot(raw: unknown): QuotaSnapshot | null {
  if (!raw || typeof raw !== 'object') return null
  const root = raw as Record<string, unknown>
  const claude = root.claude
  if (!claude || typeof claude !== 'object') return null
  const c = claude as Record<string, unknown>

  // generated_at is an ISO-8601 string on disk; the gate wants epoch ms.
  let generatedAtMs: number | null = null
  if (typeof root.generated_at === 'string') {
    const parsed = Date.parse(root.generated_at)
    if (!Number.isNaN(parsed)) generatedAtMs = parsed
  }

  // The collector rewrites generated_at on every run. Prefer the time the
  // authoritative quota data was actually acquired, but retain generated_at
  // for the first migration run / old snapshots that do not have it.
  const collected = c.authoritative_collected_at_unix
  let authoritativeCollectedAtMs: number | null = null
  if (collected !== undefined) {
    // A present but malformed upstream timestamp is not equivalent to an old
    // snapshot that lacks the field: fail open instead of silently trusting
    // the freshly rewritten generated_at file timestamp.
    if (typeof collected !== 'number' || !Number.isFinite(collected)) return null
    authoritativeCollectedAtMs = collected * 1000
  }

  const windows = c.windows && typeof c.windows === 'object'
    ? (c.windows as QuotaSnapshot['windows'])
    : null

  return {
    source: typeof c.source === 'string' ? c.source : null,
    authoritativeCollectedAtMs,
    generatedAtMs,
    windows,
  }
}

export function readQuotaSnapshot(path: string = USAGE_SNAPSHOT_PATH): QuotaSnapshot | null {
  try {
    return parseQuotaSnapshot(JSON.parse(readFileSync(path, 'utf-8')))
  } catch {
    return null
  }
}

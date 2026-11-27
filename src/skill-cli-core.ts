// The canonical JSON of upstream's skill-cli-core.ts (MIOCLISKILL831), ported alone with the agent package update
// (upstream 68f5c43a uses it for the update plan id; BELA 6404). The rest of that module (the HMAC attestation of the
// `marveen skill` CLI) is not in this fork.
//
// Sorted keys, no white space, arrays in their order: the same bytes as python's
// json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False) for the shapes it is used on.
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']'
  if (typeof value === 'object' && value !== null) {
    const o = value as Record<string, unknown>
    return (
      '{' +
      Object.keys(o)
        .sort()
        .map((k) => JSON.stringify(k) + ':' + canonicalJson(o[k]))
        .join(',') +
      '}'
    )
  }
  return JSON.stringify(value)
}

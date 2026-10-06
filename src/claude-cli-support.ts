// Which Claude model ids the installed Claude Code CLI can actually launch.
// Pure module: probing `claude --version` lives in web/claude-cli-version.ts.

export interface ClaudeModelCliRequirement {
  minCli: string
  measured: string
}

// Minimums are the lowest versions measured to launch a model.  They are
// intentionally conservative only for the affected model; an unmeasured CLI
// remains fail-open so a temporary probe problem does not hide the picker.
export const CLAUDE_MODEL_MIN_CLI: Readonly<Record<string, ClaudeModelCliRequirement>> = {
  'claude-fable-5-1': {
    minCli: '2.1.278',
    measured: '2.1.110 -> unrecognized_model; 2.1.278 and 2.1.280 -> OK (2026-09-22/23)',
  },
  'claude-sonnet-5-5': {
    // Upstream measured 2.1.283.  Our fleet measured a successful real launch
    // on 2.1.220 on 2026-10-06, so retain the lowest local proven minimum.
    minCli: '2.1.220',
    measured: 'upstream: 2.1.283 -> OK (plugin-free claude -p, 2026-09-28); local fleet: 2.1.220 -> OK (real --model launch, 2026-10-06)',
  },
  'claude-opus-5-5': {
    minCli: '2.1.280',
    measured: '2.1.110 and 2.1.278 -> unrecognized_model; 2.1.280 -> OK (2026-09-22)',
  },
}

export function parseClaudeVersion(output: string | null | undefined): string | null {
  if (!output) return null
  const m = /(\d+)\.(\d+)\.(\d+)/.exec(output)
  return m ? `${m[1]}.${m[2]}.${m[3]}` : null
}

/** Numeric dotted compare: negative when a < b, zero when equal. */
export function compareVersions(a: string, b: string): number {
  const pa = a.split('.').map((n) => parseInt(n, 10) || 0)
  const pb = b.split('.').map((n) => parseInt(n, 10) || 0)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const delta = (pa[i] ?? 0) - (pb[i] ?? 0)
    if (delta !== 0) return delta
  }
  return 0
}

/** The table is keyed without context suffixes. */
export function baseModelId(model: string): string {
  return model.replace(/\[[^\]]*\]$/, '').trim()
}

export function isModelUnsupportedByCli(model: string, installedVersion: string | null): boolean {
  if (!installedVersion) return false
  const requirement = CLAUDE_MODEL_MIN_CLI[baseModelId(model)]
  return requirement ? compareVersions(installedVersion, requirement.minCli) < 0 : false
}

export interface ClaudeSupportSummary {
  measured: boolean
  installedVersion: string | null
  /** Base ids: variants share the base model's compatibility rule. */
  unsupported: Array<{ id: string; minCli: string }>
}

export function claudeSupportForCli(installedVersion: string | null): ClaudeSupportSummary {
  if (!installedVersion) return { measured: false, installedVersion: null, unsupported: [] }
  return {
    measured: true,
    installedVersion,
    unsupported: Object.entries(CLAUDE_MODEL_MIN_CLI)
      .filter(([id]) => isModelUnsupportedByCli(id, installedVersion))
      .map(([id, requirement]) => ({ id, minCli: requirement.minCli })),
  }
}

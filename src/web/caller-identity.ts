// VAULTMAINONLY (KISPROGI 6431 plan, BELA 6432 decisions, BELA 6533): who is on the other end of a local HTTP
// request, told by the kernel, not by a token. Every agent holds the same dashboard token (and can read it from
// store/.dashboard-token), so the token cannot say which agent is calling. The kernel can: every tmux pane of this
// fleet runs in its own systemd scope (tmux 3.6 with systemd --user: .../app.slice/tmux-spawn-<uuid>.scope), a child
// inherits it, and setsid plus a double fork does not leave it (measured 2026-11-27, KISPROGI 6431). A loopback
// client's socket is found in /proc/net/tcp(6) by its port pair, the socket's inode leads to the processes holding
// it (/proc/<pid>/fd), and their cgroup names the scope, which names the tmux pane.
//
// Classes: 'main' (the main agent's pane: the first pane of <MAIN_AGENT_ID>-channels whose process is claude with
// the project root as its cwd), 'agent' (an agent-* pane), 'worker' (bela-worker / bela-worker-fast and any other
// tmux pane), 'service' (a systemd service, e.g. the webchat units), 'external' (not loopback, or a socket of
// another OS user: the Windows browser and interop tools arrive through the WSL relay as uid 0), 'unknown'
// (anything that cannot be proven). Fail closed: every read error ends in 'unknown'.
//
// A scope decides alone only when it is a per-pane tmux scope (tmux-spawn-*.scope) that no other pane shares; in a
// shared or broader cgroup (tmux without systemd) the caller must descend from the main pane's process.
import { readFileSync, readdirSync, readlinkSync, realpathSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { join } from 'node:path'
import { resolveFromPath } from '../platform.js'

export type CallerClass = 'main' | 'agent' | 'worker' | 'service' | 'external' | 'unknown'

export interface CallerIdentity {
  cls: CallerClass
  /** tmux session of the caller's pane, when one was found */
  session?: string
  pid?: number
  comm?: string
  /** last part of the caller's cgroup path (e.g. tmux-spawn-<uuid>.scope, bela-webchat.service) */
  scope?: string
  reason?: string
}

export interface PaneInfo { session: string; window: number; pane: number; pid: number }

export interface CallerIdentityDeps {
  procRoot: string
  readFile(path: string): string
  readdir(path: string): string[]
  readlink(path: string): string
  realpath(path: string): string
  /** every tmux pane of the fleet's tmux server, or null when tmux cannot be read */
  listPanes(): PaneInfo[] | null
  mainSession: string
  projectRoot: string
  ownUid: number
}

const PANE_SCOPE_RX = /(^|\/)tmux-spawn-[0-9a-f-]+\.scope$/

export function isLoopbackAddress(addr: string | undefined): boolean {
  if (!addr) return false
  const a = addr.toLowerCase()
  return a === '::1' || /^127\.\d+\.\d+\.\d+$/.test(a) || /^::ffff:127\.\d+\.\d+\.\d+$/.test(a)
}

/** The kernel's hex form of a loopback address in /proc/net/tcp (IPv4, little-endian) or tcp6. */
function hexIsLoopback(hex: string): boolean {
  if (hex.length === 8) return hex.toUpperCase().endsWith('7F') // 127.x.x.x, byte order reversed
  if (hex.length === 32) {
    const h = hex.toUpperCase()
    return h === '00000000000000000000000001000000' // ::1
      || (h.startsWith('0000000000000000FFFF0000') && h.endsWith('7F')) // ::ffff:127.x.x.x
  }
  return false
}

/** The client side of a loopback connection: its socket inode and owner uid, or null. */
export function findClientSocket(clientPort: number, serverPort: number, deps: CallerIdentityDeps): { inode: string; uid: number } | null {
  for (const table of ['tcp', 'tcp6']) {
    let text: string
    try {
      text = deps.readFile(join(deps.procRoot, 'net', table))
    } catch {
      continue
    }
    for (const line of text.split('\n').slice(1)) {
      const f = line.trim().split(/\s+/)
      if (f.length < 10) continue
      const [lAddr, lPort] = f[1].split(':')
      const [rAddr, rPort] = f[2].split(':')
      if (parseInt(lPort, 16) !== clientPort || parseInt(rPort, 16) !== serverPort) continue
      if (!hexIsLoopback(lAddr) || !hexIsLoopback(rAddr)) continue
      if (f[9] === '0') continue
      return { inode: f[9], uid: Number(f[7]) }
    }
  }
  return null
}

/** Every process holding the socket (a forked child shares it). */
export function pidsHoldingSocket(inode: string, deps: CallerIdentityDeps): number[] {
  const target = `socket:[${inode}]`
  const out: number[] = []
  let entries: string[]
  try {
    entries = deps.readdir(deps.procRoot)
  } catch {
    return out
  }
  for (const p of entries) {
    if (!/^\d+$/.test(p)) continue
    let fds: string[]
    try {
      fds = deps.readdir(join(deps.procRoot, p, 'fd'))
    } catch {
      continue
    }
    for (const fd of fds) {
      try {
        if (deps.readlink(join(deps.procRoot, p, 'fd', fd)) === target) {
          out.push(Number(p))
          break
        }
      } catch { /* gone or not ours */ }
    }
  }
  return out
}

function cgroupOf(pid: number, deps: CallerIdentityDeps): string | null {
  try {
    const text = deps.readFile(join(deps.procRoot, String(pid), 'cgroup'))
    const v2 = text.split('\n').find((l) => l.startsWith('0::'))
    return v2 ? v2.slice(3).trim() : null
  } catch {
    return null
  }
}

function parentOf(pid: number, deps: CallerIdentityDeps): number | null {
  try {
    const s = deps.readFile(join(deps.procRoot, String(pid), 'stat'))
    const ppid = Number(s.slice(s.lastIndexOf(')') + 2).split(' ')[1])
    return Number.isFinite(ppid) ? ppid : null
  } catch {
    return null
  }
}

function commOf(pid: number, deps: CallerIdentityDeps): string | undefined {
  try {
    return deps.readFile(join(deps.procRoot, String(pid), 'comm')).trim()
  } catch {
    return undefined
  }
}

/** The main agent's pane pid: the first pane of the main channels session, running claude in the project root. */
export function mainPanePid(panes: PaneInfo[], deps: CallerIdentityDeps): number | null {
  const mine = panes.filter((p) => p.session === deps.mainSession).sort((a, b) => a.window - b.window || a.pane - b.pane)
  const first = mine[0]
  if (!first) return null
  if (commOf(first.pid, deps) !== 'claude') return null
  try {
    if (deps.realpath(deps.readlink(join(deps.procRoot, String(first.pid), 'cwd'))) !== deps.realpath(deps.projectRoot)) return null
  } catch {
    return null
  }
  return first.pid
}

function lastPart(cg: string | null): string | undefined {
  return cg ? cg.split('/').filter(Boolean).pop() : undefined
}

/** One process: its class by its scope (or, in a shared scope, by its ancestry). */
function classifyPid(pid: number, panes: PaneInfo[], mainPid: number | null, deps: CallerIdentityDeps): CallerIdentity {
  const cg = cgroupOf(pid, deps)
  const base: CallerIdentity = { cls: 'unknown', pid, comm: commOf(pid, deps), scope: lastPart(cg) }
  if (!cg) return { ...base, reason: 'no cgroup' }
  const paneScopes = panes.map((p) => ({ ...p, cg: cgroupOf(p.pid, deps) }))
  const sameScope = paneScopes.filter((p) => p.cg === cg)
  const perPane = PANE_SCOPE_RX.test(cg) && sameScope.length === 1
  if (perPane) {
    const pane = sameScope[0]
    if (mainPid !== null && pane.pid === mainPid) return { ...base, cls: 'main', session: pane.session }
    return paneClass(base, pane.session)
  }
  // a shared or broad cgroup: the nearest pane process among the caller's ancestors decides
  const panePids = new Map(panes.map((p) => [p.pid, p.session]))
  let cur: number | null = pid
  for (let hops = 0; cur !== null && cur > 1 && hops < 64; hops++) {
    const session = panePids.get(cur)
    if (session !== undefined) {
      if (mainPid !== null && cur === mainPid) return { ...base, cls: 'main', session }
      return paneClass(base, session)
    }
    cur = parentOf(cur, deps)
  }
  if (/\.service$/.test(cg)) return { ...base, cls: 'service', reason: 'systemd service' }
  return { ...base, reason: sameScope.length ? 'shared scope, not below a pane' : 'no pane' }
}

function paneClass(base: CallerIdentity, session: string): CallerIdentity {
  return { ...base, cls: session.startsWith('agent-') ? 'agent' : 'worker', session }
}

const RANK: Record<CallerClass, number> = { unknown: 0, external: 1, service: 2, agent: 3, worker: 3, main: 9 }

/** Who made a request that arrived from remoteAddress:remotePort on our localPort. */
export function identifyLocalCaller(remoteAddress: string | undefined, remotePort: number | undefined, localPort: number | undefined,
                                    deps: CallerIdentityDeps): CallerIdentity {
  if (!isLoopbackAddress(remoteAddress)) return { cls: 'external', reason: 'not loopback' }
  if (!remotePort || !localPort) return { cls: 'unknown', reason: 'no port' }
  const sock = findClientSocket(remotePort, localPort, deps)
  if (!sock) return { cls: 'unknown', reason: 'socket not found' }
  if (sock.uid !== deps.ownUid) return { cls: 'external', reason: `socket of uid ${sock.uid}` }
  const pids = pidsHoldingSocket(sock.inode, deps)
  if (!pids.length) return { cls: 'unknown', reason: 'no process holds the socket' }
  const panes = deps.listPanes()
  if (!panes) return { cls: 'unknown', reason: 'tmux not readable', pid: pids[0], comm: commOf(pids[0], deps) }
  const mainPid = mainPanePid(panes, deps)
  // every holder must be the main agent for the call to be the main agent's; otherwise the weakest holder counts
  let result: CallerIdentity | null = null
  for (const pid of pids) {
    const one = classifyPid(pid, panes, mainPid, deps)
    if (result === null || RANK[one.cls] < RANK[result.cls]) result = one
  }
  return result!
}

/** The live dependencies: /proc, the fleet's tmux server, this process's uid. */
export function liveCallerIdentityDeps(mainSession: string, projectRoot: string): CallerIdentityDeps {
  return {
    procRoot: '/proc',
    readFile: (p) => readFileSync(p, 'utf-8'),
    readdir: (p) => readdirSync(p),
    readlink: (p) => readlinkSync(p),
    realpath: (p) => realpathSync(p),
    listPanes: () => {
      try {
        const out = execFileSync(resolveFromPath('tmux'), ['list-panes', '-a', '-F', '#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_pid}'],
          { timeout: 3000, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'] })
        const panes: PaneInfo[] = []
        for (const line of out.split('\n')) {
          const [session, w, p, pid] = line.split('\t')
          if (session && /^\d+$/.test(pid ?? '')) panes.push({ session, window: Number(w), pane: Number(p), pid: Number(pid) })
        }
        return panes
      } catch {
        return null
      }
    },
    mainSession,
    projectRoot,
    ownUid: typeof process.getuid === 'function' ? process.getuid() : -1,
  }
}

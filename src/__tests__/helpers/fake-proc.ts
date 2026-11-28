// In-memory /proc and tmux panes for the caller-identity tests (VAULTMAINONLY): a socket table, processes with their
// cgroup, parent, comm, cwd and socket fds.
import type { CallerIdentityDeps, PaneInfo } from '../../web/caller-identity.js'

export interface FakeProc {
  pid: number
  ppid: number
  comm: string
  cgroup: string
  cwd?: string
  sockets?: string[]
}
export interface FakeSock { clientPort: number; serverPort: number; uid: number; inode: string; v6?: boolean; laddr?: string }

export const SCOPE = (u: string) => `/user.slice/user-1000.slice/user@1000.service/app.slice/tmux-spawn-${u}.scope`
export const ROOT = '/home/u/marveen'

function hexPort(p: number): string { return p.toString(16).toUpperCase().padStart(4, '0') }

export function fakeDeps(procs: FakeProc[], socks: FakeSock[], panes: PaneInfo[] | null, ownUid = 1000): CallerIdentityDeps {
  const files = new Map<string, string>()
  const links = new Map<string, string>()
  const dirs = new Map<string, string[]>()
  const v4 = socks.filter((s) => !s.v6)
  const v6 = socks.filter((s) => s.v6)
  const head = '  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode'
  files.set('/proc/net/tcp', [head, ...v4.map((s, i) => `   ${i}: ${s.laddr ?? '0100007F'}:${hexPort(s.clientPort)} 0100007F:${hexPort(s.serverPort)} 01 00000000:00000000 00:00000000 00000000  ${s.uid}        0 ${s.inode} 1 0000000000000000 20 4 30 10 -1`)].join('\n'))
  files.set('/proc/net/tcp6', [head, ...v6.map((s, i) => `   ${i}: 0000000000000000FFFF00000100007F:${hexPort(s.clientPort)} 0000000000000000FFFF00000100007F:${hexPort(s.serverPort)} 01 00000000:00000000 00:00000000 00000000  ${s.uid}        0 ${s.inode} 1 0000000000000000 20 4 30 10 -1`)].join('\n'))
  dirs.set('/proc', [...procs.map((p) => String(p.pid)), 'net', 'self'])
  for (const p of procs) {
    files.set(`/proc/${p.pid}/cgroup`, `0::${p.cgroup}\n`)
    files.set(`/proc/${p.pid}/comm`, `${p.comm}\n`)
    files.set(`/proc/${p.pid}/stat`, `${p.pid} (${p.comm}) S ${p.ppid} ${p.pid} ${p.pid} 0 -1`)
    links.set(`/proc/${p.pid}/cwd`, p.cwd ?? '/tmp')
    const fds = (p.sockets ?? []).map((_, i) => String(i + 3))
    dirs.set(`/proc/${p.pid}/fd`, ['0', '1', '2', ...fds])
    for (const fd of ['0', '1', '2']) links.set(`/proc/${p.pid}/fd/${fd}`, '/dev/pts/1')
    ;(p.sockets ?? []).forEach((ino, i) => links.set(`/proc/${p.pid}/fd/${i + 3}`, `socket:[${ino}]`))
  }
  const need = <T>(m: Map<string, T>, k: string): T => {
    if (!m.has(k)) throw Object.assign(new Error(`ENOENT ${k}`), { code: 'ENOENT' })
    return m.get(k)!
  }
  return {
    procRoot: '/proc',
    readFile: (p) => need(files, p),
    readdir: (p) => need(dirs, p),
    readlink: (p) => need(links, p),
    realpath: (p) => p.replace(/\/+$/, ''),
    listPanes: () => panes,
    mainSession: 'bela-channels',
    projectRoot: ROOT,
    ownUid,
  }
}

// a standard fleet: main pane 775 (claude in ROOT), kisprogi pane 2725, a worker pane 1799 (a shell) with claude 1825
export const MAIN = { pid: 775, ppid: 700, comm: 'claude', cgroup: SCOPE('aaaa'), cwd: ROOT }
export const KISPROGI = { pid: 2725, ppid: 700, comm: 'claude', cgroup: SCOPE('bbbb'), cwd: `${ROOT}/agents/kisprogi` }
export const WORKER_SHELL = { pid: 1799, ppid: 700, comm: 'bash', cgroup: SCOPE('cccc'), cwd: '/home/u/.bela-worker' }
export const PANES: PaneInfo[] = [
  { session: 'agent-kisprogi', window: 0, pane: 0, pid: 2725 },
  { session: 'bela-channels', window: 0, pane: 0, pid: 775 },
  { session: 'bela-worker', window: 0, pane: 0, pid: 1799 },
]

import { describe, it, expect } from 'vitest'
import { identifyLocalCaller, isLoopbackAddress, type PaneInfo } from '../web/caller-identity.js'
import { fakeDeps, MAIN, KISPROGI, WORKER_SHELL, PANES, ROOT, SCOPE, type FakeProc, type FakeSock } from './helpers/fake-proc.js'

// VAULTMAINONLY (KISPROGI 6431 point 5, unit): the caller is told by the kernel (socket -> process -> cgroup scope ->
// tmux pane), on an in-memory /proc. The live proof (real sockets, real tmux panes and scopes) is in the report.

const SERVER = 3420

function who(procs: FakeProc[], socks: FakeSock[], panes: PaneInfo[] | null = PANES, addr = '127.0.0.1', port = 50000, ownUid = 1000) {
  return identifyLocalCaller(addr, port, SERVER, fakeDeps([MAIN, KISPROGI, WORKER_SHELL, ...procs], socks, panes, ownUid))
}

describe('caller-identity: the kernel names the caller (KISPROGI 6431)', () => {
  it('a curl from the main pane is the main agent; from a sub-agent pane it is that agent', () => {
    const curlMain = { pid: 9001, ppid: 775, comm: 'curl', cgroup: MAIN.cgroup, sockets: ['111'] }
    expect(who([curlMain], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '111' }])).toMatchObject({ cls: 'main', session: 'bela-channels', pid: 9001, comm: 'curl' })
    const curlAgent = { pid: 9002, ppid: 2725, comm: 'curl', cgroup: KISPROGI.cgroup, sockets: ['112'] }
    expect(who([curlAgent], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '112' }])).toMatchObject({ cls: 'agent', session: 'agent-kisprogi' })
  })

  it('a detached child (setsid, double fork: its parent is init) stays in its scope, so it stays the sub-agent', () => {
    const detached = { pid: 9003, ppid: 1, comm: 'python3', cgroup: KISPROGI.cgroup, sockets: ['113'] }
    expect(who([detached], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '113' }]).cls).toBe('agent')
    // and a detached child of the main agent stays the main agent (the scope decides, not the parent chain)
    const mainDetached = { pid: 9004, ppid: 1, comm: 'node', cgroup: MAIN.cgroup, sockets: ['114'] }
    expect(who([mainDetached], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '114' }]).cls).toBe('main')
  })

  it('a worker pane is a worker, a systemd service (the webchat units) is a service', () => {
    const w = { pid: 1825, ppid: 1799, comm: 'claude', cgroup: WORKER_SHELL.cgroup, sockets: ['115'] }
    expect(who([w], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '115' }])).toMatchObject({ cls: 'worker', session: 'bela-worker' })
    const web = { pid: 9005, ppid: 1, comm: 'python3', cgroup: '/user.slice/user-1000.slice/user@1000.service/app.slice/bela-webchat.service', sockets: ['116'] }
    expect(who([web], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '116' }])).toMatchObject({ cls: 'service', scope: 'bela-webchat.service' })
  })

  it('a socket of uid 0 (the WSL relay of the Windows browser, interop tools) and a non-loopback peer are external', () => {
    expect(who([], [{ clientPort: 50000, serverPort: SERVER, uid: 0, inode: '117' }])).toMatchObject({ cls: 'external' })
    expect(who([], [], PANES, '192.168.1.5').cls).toBe('external')
    expect(isLoopbackAddress('::ffff:127.0.0.1')).toBe(true)
    expect(isLoopbackAddress('::1')).toBe(true)
    expect(isLoopbackAddress('10.0.0.1')).toBe(false)
  })

  it('an IPv4-mapped socket in tcp6 is found too', () => {
    const curlMain = { pid: 9006, ppid: 775, comm: 'curl', cgroup: MAIN.cgroup, sockets: ['118'] }
    expect(identifyLocalCaller('::ffff:127.0.0.1', 50000, SERVER, fakeDeps([MAIN, KISPROGI, WORKER_SHELL, curlMain],
      [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '118', v6: true }], PANES)).cls).toBe('main')
  })

  it('fail closed: no socket line, no holder, tmux not readable, a missing cgroup are all unknown', () => {
    expect(who([], []).cls).toBe('unknown')
    expect(who([], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '119' }]).cls).toBe('unknown')
    const curlMain = { pid: 9007, ppid: 775, comm: 'curl', cgroup: MAIN.cgroup, sockets: ['120'] }
    expect(who([curlMain], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '120' }], null).cls).toBe('unknown')
    expect(identifyLocalCaller('127.0.0.1', undefined, SERVER, fakeDeps([], [], PANES)).cls).toBe('unknown')
    // a socket line whose ports match but whose address is not loopback is not taken
    expect(who([curlMain], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '120', laddr: '0501A8C0' }]).cls).toBe('unknown')
  })

  it('the main pane must run claude in the project root, read fresh: a respawned pane counts by its new scope', () => {
    const fake = { ...MAIN, comm: 'bash' }
    const curl = { pid: 9008, ppid: 775, comm: 'curl', cgroup: MAIN.cgroup, sockets: ['121'] }
    expect(identifyLocalCaller('127.0.0.1', 50000, SERVER, fakeDeps([fake, KISPROGI, curl], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '121' }], PANES)).cls).not.toBe('main')
    const wrongCwd = { ...MAIN, cwd: '/tmp' }
    expect(identifyLocalCaller('127.0.0.1', 50000, SERVER, fakeDeps([wrongCwd, KISPROGI, curl], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '121' }], PANES)).cls).not.toBe('main')
    // respawn: the main pane is now pid 7000 in a new scope; a caller in the OLD scope is no longer main
    const respawned = { pid: 7000, ppid: 700, comm: 'claude', cgroup: SCOPE('dddd'), cwd: ROOT }
    const panes2 = PANES.map((p) => (p.session === 'bela-channels' ? { ...p, pid: 7000 } : p))
    const oldScopeCurl = { pid: 9009, ppid: 1, comm: 'curl', cgroup: MAIN.cgroup, sockets: ['122'] }
    expect(identifyLocalCaller('127.0.0.1', 50000, SERVER, fakeDeps([respawned, KISPROGI, oldScopeCurl], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '122' }], panes2)).cls).not.toBe('main')
    const newScopeCurl = { pid: 9010, ppid: 7000, comm: 'curl', cgroup: respawned.cgroup, sockets: ['123'] }
    expect(identifyLocalCaller('127.0.0.1', 50000, SERVER, fakeDeps([respawned, KISPROGI, newScopeCurl], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '123' }], panes2)).cls).toBe('main')
  })

  it('a shared scope (tmux without per-pane scopes): the parent chain decides, up to the nearest pane process', () => {
    const shared = '/user.slice/user-1000.slice/user@1000.service/tmux.service'
    const m = { ...MAIN, cgroup: shared }
    const k = { ...KISPROGI, cgroup: shared }
    const childOfAgent = { pid: 9011, ppid: 2725, comm: 'curl', cgroup: shared, sockets: ['124'] }
    const childOfMain = { pid: 9012, ppid: 775, comm: 'curl', cgroup: shared, sockets: ['125'] }
    const orphan = { pid: 9013, ppid: 1, comm: 'curl', cgroup: shared, sockets: ['126'] }
    const deps = (p: FakeProc, ino: string) => fakeDeps([m, k, p], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: ino }], PANES.filter((x) => x.session !== 'bela-worker'))
    expect(identifyLocalCaller('127.0.0.1', 50000, SERVER, deps(childOfAgent, '124')).cls).toBe('agent')
    expect(identifyLocalCaller('127.0.0.1', 50000, SERVER, deps(childOfMain, '125')).cls).toBe('main')
    // an orphan in the shared scope cannot be placed: not main (a .service cgroup without a pane above it)
    expect(identifyLocalCaller('127.0.0.1', 50000, SERVER, deps(orphan, '126')).cls).not.toBe('main')
    // a per-pane scope two panes share is not trusted alone either
    const twin = { ...KISPROGI, cgroup: MAIN.cgroup }
    const agentChild = { pid: 9014, ppid: 2725, comm: 'curl', cgroup: MAIN.cgroup, sockets: ['127'] }
    expect(identifyLocalCaller('127.0.0.1', 50000, SERVER, fakeDeps([MAIN, twin, agentChild], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '127' }], PANES)).cls).toBe('agent')
  })

  it('a shared per-pane scope never makes the caller main by pane order, and a non-tmux cgroup never decides alone', () => {
    // the main pane listed FIRST, a sub-agent pane in the same scope after it: the sub-agent's child is not main
    const twin = { ...KISPROGI, cgroup: MAIN.cgroup }
    const agentChild = { pid: 9017, ppid: 2725, comm: 'curl', cgroup: MAIN.cgroup, sockets: ['129'] }
    const mainFirst = [PANES[1], PANES[0], PANES[2]]
    expect(identifyLocalCaller('127.0.0.1', 50000, SERVER, fakeDeps([MAIN, twin, WORKER_SHELL, agentChild],
      [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '129' }], mainFirst)).cls).toBe('agent')
    // the main pane alone in a cgroup that is not a tmux pane scope (a login session scope): an orphan there is not main
    const sessionScope = '/user.slice/user-1000.slice/session-3.scope'
    const m = { ...MAIN, cgroup: sessionScope }
    const orphan = { pid: 9018, ppid: 1, comm: 'curl', cgroup: sessionScope, sockets: ['130'] }
    expect(identifyLocalCaller('127.0.0.1', 50000, SERVER, fakeDeps([m, KISPROGI, WORKER_SHELL, orphan],
      [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '130' }], PANES)).cls).not.toBe('main')
  })

  it('a socket held by the main agent AND by another process counts as the weaker one', () => {
    const a = { pid: 9015, ppid: 775, comm: 'curl', cgroup: MAIN.cgroup, sockets: ['128'] }
    const b = { pid: 9016, ppid: 2725, comm: 'python3', cgroup: KISPROGI.cgroup, sockets: ['128'] }
    expect(who([a, b], [{ clientPort: 50000, serverPort: SERVER, uid: 1000, inode: '128' }]).cls).toBe('agent')
  })
})

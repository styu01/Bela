# Public HTTPS access to the dashboard (optional, OFF by default)

Port of upstream DOCKERHTTPS1119 (139ac6db, 3a8b912b) to this systemd install (BELA 6404). The code is installed;
nothing is enabled. Turning it on is the owner's decision: it puts the dashboard login on the internet.

## What is there and what is not, by default

- The dashboard listens on **127.0.0.1:3420 only**, as before. No other port is opened.
- The public listener opens **only** when `WEB_PUBLIC_PORT` is set in the install's `.env` (or the process
  environment). There is no default port (upstream's 3421 is taken on this machine by another local service).
- Even then it stays closed unless **all** of these hold, checked at every dashboard start
  (`src/web/public-access.ts`, `publicListenerDecision`):
  - `WEB_PUBLIC_URL` is the `https://` address the proxy serves (its own key: see below why not
    `DASHBOARD_PUBLIC_URL`);
  - at least one enabled dashboard login exists, and **every** enabled login has the two-step sign-in (TOTP) on;
  - it is the install that runs the services (never a git worktree, a temp clone or a `WEB_ONLY` instance). A
    worktree started with `MARVEEN_ALLOW_SECOND_INSTANCE_SERVICES=1` AND `WEB_PUBLIC_PORT` would open it: that is the
    same explicit opt-in as for the background services, never a default.
  A refused start is logged with its reason (`public listener NOT opened ...`), also for a `WEB_PUBLIC_PORT` that is
  set but is not a port number; the dashboard keeps running.

`DASHBOARD_PUBLIC_URL` must stay as it is (empty on this install): it is the address the **agents** reach the API on
(the curl examples in every agent's CLAUDE.md, the heartbeat's `CLAW_DASHBOARD_ORIGIN`). Pointed at the public address
it would send the fleet's bearer-token calls out to the internet, where the public listener refuses them (401) and the
token would leave the loopback. The public listener reads only `WEB_PUBLIC_URL`.
- When open, the public listener binds **127.0.0.1** only (`PUBLIC_LISTENER_HOST`). Only a reverse proxy on this
  machine reaches it.

## What the public listener accepts

- Only a **browser login session** whose user has the two-step sign-in on. The bearer token, device keys, the SSE
  `?token=` and federation tokens are never looked at there.
- Without a session: the static page (index, app.js, style.css, lang, manifest), `GET /api/auth/status`,
  `POST /api/auth/login` and `POST /api/auth/login/totp`. A path that is open on 3420 (for example the avatars)
  answers 404 there; gated paths answer 401 as everywhere.
- The password login of a login **without** the two-step sign-in gets **403 `public_totp_required`** and no session.
  Turning the second factor off later shuts that user out of the public address at once (checked per request).
- The session cookie is always `Secure` there.

Not ported from upstream: the one-time setup code and the setup session (first login over the internet on a fresh
Docker install). This install already has its login; a first login must be created on the internal address or with
`npm run dashboard-user -- add <name>`.

## Turning it on (owner's decision, in this order)

1. Two-step sign-in on for every dashboard login: Settings, Security, "Two-step sign-in" (on 127.0.0.1:3420 or an SSH
   tunnel). Keep the backup codes.
2. A public host name pointing at this machine, and ports 80 and 443 reachable from the internet (router / firewall).
3. Caddy on this machine with `deploy/caddy/Caddyfile.example` (host name and port filled in); it gets the
   certificate itself.
4. In the install's `.env`: `WEB_PUBLIC_PORT=<a free local port>` and `WEB_PUBLIC_URL=https://<host name>`. Do NOT
   set `DASHBOARD_PUBLIC_URL` for this (see above).
5. Restart the dashboard. The log says `Web dashboard: PUBLIC listener open ...`, or why it did not open.

## Turning it off

Remove `WEB_PUBLIC_PORT` (and `WEB_PUBLIC_URL`) from `.env` and restart the dashboard (and stop Caddy). Lost phone and backup codes:
`npm run dashboard-user -- totp:off <username>` on the machine (docs/dashboard-auth-recovery.md).

## The limit

The second factor protects against a remote attacker who knows the password. It does not protect against a process
of the same OS user on this machine. The agents are fenced by a narrow PreToolUse gate (`scripts/hooks/auth-store-gate.py`,
every agent, BELA 6410): `store/.dashboard-totp.json` in any agent tool call (reading too), an SQL write on the
`dashboard_users` table in text a program runs, and the `store/claudeclaw.db` file as a write target. It is a text
brake, not an OS boundary: a file name or an SQL statement assembled at run time (variables, encoding), a script
written first and run in a later call, or any process outside an agent tool call is not stopped by it.

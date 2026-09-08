#!/usr/bin/env python3
"""
One-time OAuth setup helper for BELA's own Calendar (and, once separately
approved, Gmail) read access, reusing the existing "bela-home" WEB-type
OAuth client (fixed, already registered redirect URI -- no local callback
server needed).

IMPORTANT: as of 2026-09-08 only Calendar read access (DEFAULT_SCOPES) is
approved for this client. Gmail read access is a SEPARATE, still-open
decision (file-based custom reader vs. the native Claude.ai MCP Gmail
connector) -- do not add gmail.readonly to DEFAULT_SCOPES until that is
explicitly resolved.

This is a manual, two-step "copy the redirected URL out of the browser" flow,
because the client's only registered redirect URI is
http://localhost:3422/api/auth/google/callback and nothing listens on that
port outside the Bela Home app itself. After Google redirects there the
browser will show a connection-refused page, but the `code=`/`state=` query
parameters are present in the address bar regardless -- paste the WHOLE URL
back into auth-exchange when it prompts (not just the code).

Usage:
  bela-google-oauth.py auth-url
  bela-google-oauth.py auth-exchange [--force]

auth-url always requests exactly DEFAULT_SCOPES -- there is deliberately no
--scope CLI override, so the "only Calendar is approved" decision above can't
be bypassed by whoever runs this later.

auth-exchange prompts interactively (echo hidden, via getpass) for the
redirected URL -- it is never a CLI argument or plain input(), so it can't
leak into shell history, `ps` output, or the terminal scrollback.

Security notes (2026-09-08 Codex review rounds 1-3, all addressed):
  - No --scope CLI flag exists at all -- auth-url only ever requests the
    hardcoded DEFAULT_SCOPES, so the "Calendar-only, Gmail deferred" decision
    can't be silently widened by a future invocation.
  - PKCE (S256) + a random `state` are used and verified.
  - Each auth-url run gets its OWN pending-state file, named by a hash of its
    `state` value, under ~/.config/bela-google-oauth/oauth-pending/ (0700) -- concurrent
    auth-url runs no longer clobber each other or race an in-flight exchange.
    Pending files older than 10 minutes are pruned opportunistically.
  - The redirect URI is a hardcoded constant (EXPECTED_REDIRECT_URI), never
    taken from array position or any CLI input; load_client() only confirms
    it is one of the client's registered URIs, it never picks a URI *for* us.
  - Both output files (client creds + token) are existence-checked BEFORE any
    network call or prompt, and both are gated by the SAME --force flag --
    this is a one-time-setup tool, re-running it against an already-working
    setup requires an explicit --force, not a silent overwrite of one file
    while leaving the other's guard inconsistent.
  - Output directories are chmod 700, output files chmod 600, writes are
    atomic (temp file + os.replace), refuse to follow a symlink at the
    target path, AND refuse if any parent directory in the path is a
    symlink.
  - The authorization code is read via getpass.getpass() (echo hidden). Both
    a plain stdin.isatty() check AND turning getpass.GetPassWarning into a
    hard error are used, so a terminal that can't suppress echo makes the
    tool abort instead of silently falling back to visible input.
  - A response missing `refresh_token` does not get written as a token file
    outright; the tool MAY fall back to a previously stored refresh_token,
    but only if that prior token was issued to the SAME client_id and its
    recorded scope already covers everything currently requested -- never a
    blind reuse across a different client or a narrower old grant.
  - The granted `scope` in the token response is checked against what was
    requested; a partial grant (any requested scope silently dropped) is
    treated as an error, not written as if it fully succeeded.
  - HTTP/network errors print only the parsed `error`/`error_description`
    fields (or a short non-JSON marker), never the raw response body, and
    URLError/timeout/JSON-decode failures are caught explicitly instead of
    crashing with a raw traceback.
  - Nothing secret (client_secret, code, tokens) is ever printed or logged;
    only file paths and pass/fail status.
  - PENDING_DIR itself (and every parent up to $HOME) is symlink-checked
    BEFORE it is ever listed, opened into, or pruned -- not just before the
    final target write -- and pruning only ever touches regular (non-symlink)
    *.json files it finds there.
  - The two output files are prepared (written to temp) before either is
    committed, and if a pre-existing file is about to be replaced (--force
    path) it is snapshotted first: if the SECOND os.replace() fails after the
    first one already succeeded, the first file is rolled back from that
    snapshot on a best-effort basis. This narrows, but -- being two separate
    filesystem operations -- cannot make the pair fully atomic against e.g. a
    process kill landing exactly between the two renames; the docstrings
    below describe this honestly rather than promising a two-file
    transaction guarantee this script can't actually provide.
  - CLIENT_JSON (the raw downloaded client secret) is defensively chmod'd to
    0600 on every read, regardless of how it arrived on disk.

Reads the client secret from CLIENT_JSON (Google's raw downloaded "web"-type
JSON, key "web": {client_id, client_secret, redirect_uris, token_uri, ...}).

On successful exchange, writes TWO files so marveen's own src/google-api.ts
can use them as-is:
  - CLIENT_OUT (~/.config/bela-google-oauth/gcp-oauth.keys.json): the client id/secret
    RESHAPED into the `{"installed": {...}}` wrapper google-api.ts expects
    (ClientCredentials interface reads `client.installed.*`). This is a
    purely local representation choice -- the client is still a "web" type
    OAuth client on Google's side; the token endpoint doesn't care which
    wrapper key we store it under locally, only client_id/secret/redirect_uri
    at exchange time and the refresh_token afterwards. No new Google Cloud
    Console change needed for this.
  - TOKEN_OUT (~/.config/google-calendar-mcp/tokens.json): the refresh/access
    token pair wrapped in `{"normal": {...}}`, matching TokenData + the
    on-disk shape loadTokens() in google-api.ts expects.
"""
import sys, os, json, argparse, time, stat, hashlib, base64, secrets, hmac, getpass, warnings, re
import urllib.request
import urllib.parse
import urllib.error

CLIENT_JSON = os.environ.get(
    "BELA_GOOGLE_OAUTH_CLIENT",
    os.path.join(os.path.dirname(__file__), "..", "store", ".bela-home-google-oauth-client.json"),
)
# Moved off ~/.gmail-mcp/ on 2026-09-08: that directory is the hardcoded
# working dir of the (separately installed) @artymclabin/gmail-mcp npm
# package, which writes its OWN gcp-oauth.keys.json there for a DIFFERENT
# (Desktop-app-type) OAuth client. Sharing the path would have let either
# tool's setup silently clobber the other's Calendar/Gmail credentials.
CLIENT_OUT = os.path.expanduser("~/.config/bela-google-oauth/gcp-oauth.keys.json")
TOKEN_OUT = os.path.expanduser("~/.config/google-calendar-mcp/tokens.json")
PENDING_DIR = os.path.expanduser("~/.config/bela-google-oauth/oauth-pending")
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"

# Hardcoded on purpose -- never read positionally from the client JSON's
# redirect_uris array (JSON key order is not something to trust), never
# overridable from argv/env. load_client() only verifies this value is
# present among the client's registered URIs.
EXPECTED_REDIRECT_URI = "http://localhost:3422/api/auth/google/callback"

# Only Calendar read access is approved so far (2026-09-08). Gmail read is a
# separate, still-open decision -- see module docstring. Do not extend this
# list until that is resolved.
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events.readonly",
]

PENDING_MAX_AGE_S = 600


def load_client():
    try:
        os.chmod(CLIENT_JSON, stat.S_IRUSR | stat.S_IWUSR)  # 0600, defensive, best-effort
    except OSError:
        pass
    with open(CLIENT_JSON, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise SystemExit(f"{CLIENT_JSON}: expected a JSON object at the root, got {type(raw).__name__}")
    if "web" not in raw:
        raise SystemExit(f"{CLIENT_JSON}: expected a Google 'web' client JSON with a top-level \"web\" key, got: {list(raw.keys())}")
    web = raw["web"]
    for k in ("client_id", "client_secret", "redirect_uris"):
        if k not in web:
            raise SystemExit(f"{CLIENT_JSON}: missing '{k}' under \"web\"")
    if not (isinstance(web["client_id"], str) and web["client_id"]):
        raise SystemExit(f"{CLIENT_JSON}: 'client_id' must be a non-empty string")
    if not (isinstance(web["client_secret"], str) and web["client_secret"]):
        raise SystemExit(f"{CLIENT_JSON}: 'client_secret' must be a non-empty string")
    if not (isinstance(web["redirect_uris"], list) and all(isinstance(u, str) for u in web["redirect_uris"])):
        raise SystemExit(f"{CLIENT_JSON}: 'redirect_uris' must be a list of strings")
    # membership check is now safe -- redirect_uris is confirmed to be an
    # actual list of strings above, so this can't false-positive on a
    # substring match the way `EXPECTED in web["redirect_uris"]` could if
    # that field were ever a bare string instead of a list.
    if EXPECTED_REDIRECT_URI not in web["redirect_uris"]:
        raise SystemExit(
            f"{CLIENT_JSON}: expected redirect URI {EXPECTED_REDIRECT_URI!r} is not "
            f"among the client's registered redirect_uris {web['redirect_uris']!r} -- "
            "refusing to guess a different one."
        )
    return web


def _assert_no_symlink_in_path(path):
    """Refuse if the target itself, or any parent directory up to $HOME, is a symlink."""
    home = os.path.realpath(os.path.expanduser("~"))
    cur = os.path.abspath(path)
    while True:
        if os.path.islink(cur):
            raise SystemExit(f"{cur} is a symlink -- refusing to write through it")
        parent = os.path.dirname(cur)
        if parent == cur or os.path.realpath(parent) == home or parent == "/":
            break
        cur = parent


def _mkdir_secure(path):
    """Create every directory component under $HOME needed for `path`, chmod
    0700 each one (not just the final leaf -- os.makedirs can silently create
    intermediate dirs, e.g. ~/.config/bela-google-oauth itself, at the process umask)."""
    home = os.path.realpath(os.path.expanduser("~"))
    d = os.path.dirname(os.path.abspath(path))
    to_chmod = []
    cur = d
    while cur != home and cur != "/" and not os.path.isdir(cur):
        to_chmod.append(cur)
        cur = os.path.dirname(cur)
    os.makedirs(d, exist_ok=True)
    for p in to_chmod + [d]:
        os.chmod(p, stat.S_IRWXU)  # 0700


def write_secure(path, data: dict):
    """Caller is responsible for the existence/--force precheck; this always
    writes (atomically, symlink-safe, 0600) once called. Single-file helper,
    kept for the pending-state file (where partial-pair atomicity doesn't
    apply). For the CLIENT_OUT+TOKEN_OUT pair, use _atomic_prepare/_commit
    below instead so a failure on the second file never leaves the first
    already-committed."""
    tmp = _atomic_prepare(path, data)
    _atomic_commit(tmp, path)


def _atomic_prepare(path, data: dict) -> str:
    """Write `data` to a sibling temp file (symlink-safe, 0600) and return its
    path WITHOUT committing it over `path` yet."""
    _assert_no_symlink_in_path(path)
    _mkdir_secure(path)
    tmp = path + f".tmp.{os.getpid()}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp


def _log_best_effort(msg: str):
    """A status print must never look like it caused the operation it's
    reporting on to fail (e.g. BrokenPipeError on stdout) -- the rename it
    describes already happened."""
    try:
        print(msg)
    except OSError:
        pass


def _atomic_commit(tmp: str, path: str):
    os.replace(tmp, path)
    _log_best_effort(f"wrote {path} (chmod 600)")


def _copy_secure(src: str, dst: str):
    """Snapshot `src` to a brand-new `dst`, O_EXCL|O_NOFOLLOW -- refuses to
    follow/overwrite anything already at `dst` (a same-UID concurrent process
    racing a symlink into place at the backup path gets refused, not
    followed), unlike shutil.copy2 which offers neither guarantee."""
    with open(src, "rb") as fsrc:
        data = fsrc.read()
    fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "wb") as fdst:
            fdst.write(data)
    except BaseException:
        os.unlink(dst)
        raise


def _commit_pair(tmp_client: str, tmp_token: str):
    """Commit both prepared temp files, tracking exactly which rename actually
    happened so a failure on the SECOND one can be recovered from either way:
      - if the target pre-existed (--force path), it was snapshotted first and
        gets rolled back from that snapshot;
      - if the target did NOT pre-exist, there's nothing to roll back to, so
        the just-created file is removed instead -- restoring the "neither
        output exists yet" state rather than leaving an orphaned new
        credential with no matching token (or vice versa).
    Every backup and temp file that isn't needed anymore is deleted on every
    code path (success, commit failure, or even a failure while taking the
    backups themselves) -- the only thing ever left behind on disk is a
    backup whose OWN rollback attempt failed, which gets an explicit WARNING
    naming the file so it can be recovered by hand.
    This is NOT a real two-file transaction (a process kill landing exactly
    between the two renames still leaves a partial state) -- it only narrows
    that window and gives a recovery path for the common failure case (the
    second replace erroring out, e.g. ENOSPC/EACCES), not a hard guarantee."""
    targets = [(CLIENT_OUT, tmp_client), (TOKEN_OUT, tmp_token)]

    backups = {}
    try:
        for path, _ in targets:
            if os.path.lexists(path):
                bak = path + f".bak.{secrets.token_hex(4)}"
                _copy_secure(path, bak)
                backups[path] = bak
    except BaseException:
        # Taking the snapshots themselves failed partway through -- nothing
        # has been committed yet, so just clean up whatever we managed to
        # snapshot plus both still-secret-bearing prepared temp files, and
        # bail out before ever attempting a rename.
        for bak in backups.values():
            try:
                os.unlink(bak)
            except OSError:
                pass
        for _, tmp in targets:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise

    committed = []
    try:
        for path, tmp in targets:
            os.replace(tmp, path)
            # Bookkeeping FIRST, status print second (best-effort) -- if the
            # rename succeeds but print() itself raises (e.g. BrokenPipeError
            # on a closed stdout), `path` must still count as committed, or
            # the except-branch below wouldn't know to roll it back.
            committed.append(path)
            _log_best_effort(f"wrote {path} (chmod 600)")
    except BaseException:
        for path in reversed(committed):
            if path in backups:
                try:
                    os.replace(backups[path], path)
                    backups.pop(path, None)  # consumed by the rollback rename, nothing left to clean up
                    _log_best_effort(f"rolled back {path} from pre-run snapshot after a commit failure")
                except OSError:
                    _log_best_effort(f"WARNING: could not roll back {path} -- check {backups[path]} manually")
                    # deliberately left in `backups` -- it's the one thing kept below
            else:
                try:
                    os.unlink(path)
                    _log_best_effort(f"removed newly-created {path} after a commit failure (no prior version to restore)")
                except OSError:
                    _log_best_effort(f"WARNING: could not remove orphaned {path} -- clean up manually")
        # Everything still in `backups` at this point is either (a) for a
        # target that was never touched this run (nothing to roll back,
        # redundant snapshot) or (b) a rollback that just failed above (kept
        # on purpose, already warned about). Delete every (a) so a
        # secret-bearing .bak.* file never lingers unexplained.
        for path, bak in list(backups.items()):
            if path in committed:
                continue  # this is a failed-rollback case -- keep it, already warned
            try:
                os.unlink(bak)
            except OSError:
                pass
        # any temp file that never got consumed by a successful replace above
        # (the one that failed, and anything after it) still exists -- it can
        # hold a secret, so don't leave it lying around.
        for path, tmp in targets:
            if path not in committed and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        raise
    else:
        for bak in backups.values():
            try:
                os.unlink(bak)
            except OSError:
                pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pending_path_for_state(state: str) -> str:
    digest = hashlib.sha256(state.encode("ascii")).hexdigest()
    return os.path.join(PENDING_DIR, f"{digest}.json")


def _assert_pending_dir_safe():
    """PENDING_DIR (and every parent up to $HOME) must not be a symlink --
    checked before it is ever listed into, opened into, or pruned, not just
    before a final target write."""
    if os.path.lexists(PENDING_DIR):
        _assert_no_symlink_in_path(PENDING_DIR)


def _prune_stale_pending():
    _assert_pending_dir_safe()
    if not os.path.isdir(PENDING_DIR):
        return
    now = time.time()
    for name in os.listdir(PENDING_DIR):
        if not name.endswith(".json"):
            continue
        p = os.path.join(PENDING_DIR, name)
        if os.path.islink(p) or not os.path.isfile(p):
            continue  # never follow/remove anything but a regular file we expect here
        try:
            with open(p, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            created_at = loaded.get("created_at", 0) if isinstance(loaded, dict) else 0
            if not isinstance(created_at, (int, float)):
                created_at = 0
            age = now - created_at
            if age > PENDING_MAX_AGE_S:
                os.unlink(p)
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            # corrupt/unexpected-shape pending file -- treat it as stale and
            # discard it rather than letting a raw traceback surface here
            try:
                os.unlink(p)
            except OSError:
                pass


def cmd_auth_url(args):
    web = load_client()
    _prune_stale_pending()
    scopes = list(DEFAULT_SCOPES)  # no CLI override -- see module docstring

    code_verifier = _b64url(secrets.token_bytes(64))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(24))

    pending_path = _pending_path_for_state(state)
    write_secure(pending_path, {
        "state": state,
        "code_verifier": code_verifier,
        "redirect_uri": EXPECTED_REDIRECT_URI,
        "client_id": web["client_id"],
        "requested_scopes": sorted(scopes),
        "created_at": int(time.time()),
    })

    params = {
        "client_id": web["client_id"],
        "redirect_uri": EXPECTED_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)
    print("Open this URL, sign in, approve. Google will redirect to:")
    print(f"  {EXPECTED_REDIRECT_URI}?code=...&state=...")
    print("Nothing listens on that port right now, so the browser will show a")
    print("connection-refused / can't-reach-this-page error -- that's expected.")
    print("Copy the WHOLE resulting address-bar URL (not just the code) and run:")
    print("  bela-google-oauth.py auth-exchange")
    print("(it will prompt you to paste the URL with echo hidden -- never pass it as a CLI arg)")
    print()
    print(url)


def _load_pending_for_state(state: str):
    _assert_pending_dir_safe()
    p = _pending_path_for_state(state)
    if os.path.islink(p):
        raise SystemExit(f"{p} is a symlink -- refusing to read a pending-state through it")
    try:
        with open(p, "r", encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        raise SystemExit(
            "no matching pending auth-url run found for this state (expired >10min, "
            "already used, or this state didn't come from a run of this tool) -- run `auth-url` again"
        )
    except (OSError, ValueError, json.JSONDecodeError):
        raise SystemExit(f"{p}: could not read/parse pending-state file -- run `auth-url` again")
    if not isinstance(st, dict):
        os.unlink(p)
        raise SystemExit(f"{p}: pending-state file's JSON root is not an object -- discarded, run `auth-url` again")
    for k in ("state", "code_verifier", "redirect_uri", "client_id", "requested_scopes", "created_at"):
        if k not in st:
            raise SystemExit(f"{p}: pending-state file is missing '{k}' -- corrupt, run `auth-url` again")
    if not (isinstance(st["state"], str) and isinstance(st["code_verifier"], str)
             and isinstance(st["redirect_uri"], str) and isinstance(st["client_id"], str)
             and isinstance(st["requested_scopes"], list)
             and all(isinstance(s, str) for s in st["requested_scopes"])
             and isinstance(st["created_at"], (int, float))):
        os.unlink(p)
        raise SystemExit(f"{p}: pending-state file has an unexpected shape -- discarded, run `auth-url` again")
    if st["redirect_uri"] != EXPECTED_REDIRECT_URI:
        os.unlink(p)
        raise SystemExit(f"{p}: pending redirect_uri {st['redirect_uri']!r} != expected {EXPECTED_REDIRECT_URI!r} -- discarded, run `auth-url` again")
    age = time.time() - st["created_at"]
    if not (0 <= age <= PENDING_MAX_AGE_S):
        os.unlink(p)
        raise SystemExit(f"pending auth-url state is stale ({int(age)}s old) -- run `auth-url` again")
    return st, p


# What _b64url() can actually produce (base64url alphabet, no padding). A
# pasted `state` outside this shape is either not ours or corrupted -- reject
# it before it's ever used as a hash-input/filename component, rather than
# letting e.g. a non-ASCII value blow up with a raw UnicodeEncodeError deep
# inside _pending_path_for_state.
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _extract_code_and_state(pasted: str):
    pasted = pasted.strip()
    if "code=" not in pasted and "state=" not in pasted:
        # allow a bare code too, but then state can't be verified
        return pasted, None
    if "?" in pasted:
        pasted = pasted.split("?", 1)[1]
    qs = urllib.parse.parse_qs(pasted)
    code = qs.get("code", [None])[0]
    state = qs.get("state", [None])[0]
    if not code:
        raise SystemExit("could not find a `code` parameter in the pasted value")
    if state is not None and not _STATE_RE.match(state):
        raise SystemExit("the `state` value in what you pasted doesn't look like one this tool generates -- refusing to use it")
    return code, state


def _safe_exists_not_symlinked(path) -> bool:
    """Like os.path.exists(), but also requires no symlink anywhere in the
    path (target or parents). Used by the read-only refresh_token-fallback
    path -- treat a symlinked location as "nothing usable here" rather than
    crashing a fallback helper outright."""
    if not os.path.exists(path):
        return False
    try:
        _assert_no_symlink_in_path(path)
    except SystemExit:
        return False
    return True


def _existing_client_id():
    if not _safe_exists_not_symlinked(CLIENT_OUT):
        return None
    try:
        with open(CLIENT_OUT, "r", encoding="utf-8") as f:
            return json.load(f).get("installed", {}).get("client_id")
    except (OSError, ValueError):
        return None


def _existing_refresh_token_if_reusable(current_client_id: str, requested_scopes: set):
    """Only ever reuse a stored refresh_token if it belongs to the SAME client
    and its recorded scope already covers everything we're requesting now."""
    if not _safe_exists_not_symlinked(TOKEN_OUT):
        return None
    if _existing_client_id() != current_client_id:
        return None
    try:
        with open(TOKEN_OUT, "r", encoding="utf-8") as f:
            normal = json.load(f).get("normal", {})
    except (OSError, ValueError):
        return None
    existing_scopes = set(normal.get("scope", "").split())
    if not requested_scopes.issubset(existing_scopes):
        return None
    return normal.get("refresh_token")


def _print_token_error(prefix: str, exc):
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            detail = {k: body[k] for k in ("error", "error_description") if k in body}
        except (ValueError, json.JSONDecodeError):
            detail = "(non-JSON error body, not printed)"
        raise SystemExit(f"{prefix}: HTTP {exc.code}: {detail}")
    if isinstance(exc, urllib.error.URLError):
        raise SystemExit(f"{prefix}: network error: {exc.reason}")
    raise SystemExit(f"{prefix}: {type(exc).__name__}")


def _read_hidden(prompt: str) -> str:
    """getpass.getpass(), but a failure to actually suppress echo (or EOF) is
    a hard failure -- never silently falls back to visible input()."""
    if not sys.stdin.isatty():
        raise SystemExit("auth-exchange must be run from an interactive terminal (stdin is not a TTY) -- refusing to fall back to echoed input")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise SystemExit("terminal echo could not be suppressed -- refusing to fall back to visible input")
        except EOFError:
            raise SystemExit("no input received (EOF) -- aborting")
        except OSError as e:
            raise SystemExit(f"could not read input from the terminal: {e}")


def cmd_auth_exchange(args):
    # Preflight BEFORE prompting/network: don't burn a one-time auth code on
    # a run that's going to refuse to write anyway. Both outputs share one
    # --force gate -- this is a one-time-setup tool, not an auto-refresher.
    # Full parent-chain symlink check (not just the direct target) up front --
    # if only the direct-target check ran (or it ran later inside
    # _atomic_prepare), a symlinked PARENT directory would only surface as an
    # error AFTER the one-time Google auth code was already spent.
    for p in (CLIENT_OUT, TOKEN_OUT):
        _assert_no_symlink_in_path(p)
    if not args.force:
        existing = [p for p in (CLIENT_OUT, TOKEN_OUT) if os.path.lexists(p)]
        if existing:
            raise SystemExit(
                "refusing to run: already exists -> " + ", ".join(existing) +
                "\nPass --force to intentionally replace them."
            )

    pasted = _read_hidden("Paste the full redirected URL (input hidden): ")
    code, returned_state = _extract_code_and_state(pasted)
    if returned_state is None:
        raise SystemExit(
            "no `state` parameter found in what you pasted -- paste the FULL "
            "address-bar URL (including ?code=...&state=...), not just the code."
        )

    pending, pending_path = _load_pending_for_state(returned_state)
    if not hmac.compare_digest(returned_state, pending["state"]):
        raise SystemExit("state mismatch -- aborting.")  # defense in depth; lookup above already keyed on this state

    web = load_client()
    if web["client_id"] != pending["client_id"]:
        raise SystemExit("client_id changed since auth-url was run (store/.bela-home-google-oauth-client.json was edited mid-flow?) -- aborting, run auth-url again")

    requested_scopes = set(pending["requested_scopes"])

    body = urllib.parse.urlencode({
        "code": code,
        "client_id": web["client_id"],
        "client_secret": web["client_secret"],
        "redirect_uri": pending["redirect_uri"],
        "grant_type": "authorization_code",
        "code_verifier": pending["code_verifier"],
    }).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_ENDPOINT, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        _print_token_error("token exchange failed", e)
        return  # unreachable, _print_token_error always raises
    except (ValueError, json.JSONDecodeError):
        raise SystemExit("token exchange failed: response was not valid JSON")

    if not (isinstance(payload.get("access_token"), str) and payload.get("access_token")
             and isinstance(payload.get("scope", ""), str)
             and isinstance(payload.get("expires_in", 3600), (int, float)) and payload.get("expires_in", 3600) > 0
             and (payload.get("refresh_token") is None or isinstance(payload.get("refresh_token"), str))
             and isinstance(payload.get("token_type", "Bearer"), str)):
        raise SystemExit("token exchange succeeded but the response had unexpected/invalid field values -- refusing to trust it")

    granted_scopes = set(payload.get("scope", "").split())
    missing = requested_scopes - granted_scopes
    if missing:
        raise SystemExit(
            "token exchange succeeded but the grant is PARTIAL -- missing scope(s): "
            + ", ".join(sorted(missing))
            + "\nRefusing to write a token file that looks complete but isn't. "
              "Re-run auth-url and make sure every requested permission is approved."
        )

    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        refresh_token = _existing_refresh_token_if_reusable(web["client_id"], requested_scopes)
        if not refresh_token:
            raise SystemExit(
                "response had no refresh_token, and there's no existing one for this "
                "same client_id that already covers the requested scope(s) to fall "
                "back to. Revoke prior grants at https://myaccount.google.com/permissions "
                "and retry from auth-url (this tool always sends "
                "access_type=offline&prompt=consent, which should force a fresh one)."
            )
        print("note: no new refresh_token in this response -- keeping the previously stored one (same client, scope already covered)")

    client_data = {
        "installed": {
            "client_id": web["client_id"],
            "client_secret": web["client_secret"],
            "token_uri": TOKEN_ENDPOINT,
        }
    }
    token_data = {
        "normal": {
            "access_token": payload["access_token"],
            "refresh_token": refresh_token,
            "expiry_date": int(time.time() * 1000) + int(payload.get("expires_in", 3600)) * 1000,
            "token_type": payload.get("token_type", "Bearer"),
            "scope": payload.get("scope", " ".join(sorted(requested_scopes))),
        }
    }

    # Prepare BOTH temp files first; only start committing once both writes
    # succeeded. _commit_pair additionally snapshots any pre-existing target
    # and rolls back on a second-commit failure (best-effort, see its
    # docstring for what this does and doesn't guarantee).
    tmp_client = _atomic_prepare(CLIENT_OUT, client_data)
    try:
        tmp_token = _atomic_prepare(TOKEN_OUT, token_data)
    except BaseException:
        os.unlink(tmp_client)
        raise
    _commit_pair(tmp_client, tmp_token)

    try:
        os.unlink(pending_path)
    except FileNotFoundError:
        pass
    print("Done. src/google-api.ts can now read both files as-is.")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth-url")  # deliberately no flags -- see module docstring on why --scope was removed

    p_ex = sub.add_parser("auth-exchange")
    p_ex.add_argument("--force", action="store_true", help="allow replacing already-existing output files")

    args = ap.parse_args()

    if args.cmd == "auth-url":
        cmd_auth_url(args)
    elif args.cmd == "auth-exchange":
        cmd_auth_exchange(args)


if __name__ == "__main__":
    main()

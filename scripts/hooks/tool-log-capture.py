#!/usr/bin/env python3
"""PostToolUse hook: log every tool call to /api/tool-log for the activity dashboard."""
import sys
import os
import json
import re
import random
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_lib  # noqa: E402


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _web_port() -> str:
    # Config-driven: WEB_PORT env, else .env file, default 3420.
    port = os.environ.get("WEB_PORT")
    if not port:
        try:
            with open(os.path.join(_project_root(), ".env")) as f:
                for line in f:
                    if line.startswith("WEB_PORT="):
                        port = line.split("=", 1)[1].strip().strip('"')
                        break
        except Exception:
            pass
    return port or "3420"


def _dashboard_token() -> str:
    try:
        with open(os.path.join(_project_root(), "store", ".dashboard-token")) as f:
            return f.read().strip()
    except OSError:
        return ''


# Patterns that could reveal secrets if stored verbatim.
#
# TOOLLOGREDACT924: shell input arrives in several equivalent spellings.  Keep
# a label (or flag) visible for diagnostics, but never its literal value.  A
# shell-variable reference is deliberately retained: it is a reference, not
# the secret itself.  `-p X` is not a generic password flag because it is also
# commonly a port flag; the mysql and sshpass spellings are handled narrowly.
_SECRET_PATTERNS = [
    # Bearer / Basic authorization values
    re.compile(r'(?i)(\b(?:bearer|basic)\s+)[A-Za-z0-9+/=_\-\.]{8,}'),
    # Credentials embedded in a URL: any RFC 3986 scheme.  The password runs
    # to the final @ before the authority so an unescaped @ in it stays hidden.
    re.compile(r'(?i)(\b[a-z][a-z0-9+.\-]*://)(?!\$)[^/\s:@]*:(?!\$)[^/\s]+(?=@[^/\s@]*(?:[/\s?#]|$))'),
    re.compile(r'(?i)(\b[a-z][a-z0-9+.\-]*://)[A-Za-z0-9_\-]{20,}(?=@)'),
    # Spaced or = flags: --token X, --password 'X', --api-key=X ...
    re.compile(r'(?i)(--(?:token|password|passwd|api-key|apikey|access-token|auth-token|secret)(?:\s+|=)[\'\"]?)(?!\$)[^\s\'\"]{6,}'),
    # key=value / key: value, with a quoted or unquoted value.  This includes
    # arbitrary *_KEY, *_TOKEN, *_SECRET and *_PASSWORD names.
    re.compile(r'(?i)(\b\w*(?:token|secret|passw(?:or)?d|api[_\-]?key|apikey|auth|credential|_key)[\'\"]?\s*[=:]\s*[\'\"]?)(?!\$)[^\s,\'";&|]{6,}'),
    # Known token prefixes.  Preserve only the non-sensitive prefix.
    re.compile(r'\b(ghp_|gho_|ghs_|ghu_|ghr_|github_pat_|sbp_|sk-ant-|sk-|xoxb-|xoxp-|xapp-|sk_live_|sk_test_|rk_live_|rk_test_|whsec_)[A-Za-z0-9_\-]{10,}'),
    # Telegram bot token and AWS access-key id.
    re.compile(r'(\b(?:bot)?\d{6,12}:)[A-Za-z0-9_\-]{30,}'),
    re.compile(r'\b(AKIA|ASIA)[A-Z0-9]{16}\b'),
    # Explicit password-taking tools.  A bare mysql -p prompts for a password
    # and has no secret to redact.
    re.compile(r'(\bsshpass\s+-p\s*)(?:\'[^\']*\'|"(?!\$)[^"]*"|(?!\$)[^\s\'\"]+)'),
    re.compile(r'(\b(?:mysql|mysqldump|mariadb)\b[^|;&\n]*?\s-p[\'\"]?)(?!\$)[^\s\'\"]{4,}'),
    # A JWT anywhere (header.payload.signature).
    re.compile(r'\beyJ[\w\-]{8,}\.eyJ[\w\-]{8,}\.[\w\-]{8,}'),
    # Raw hex blobs >= 32 chars (likely hashed secrets) -- no capture group.
    re.compile(r'\b[0-9a-fA-F]{32,}\b'),
]


def _redact(text: str) -> str:
    """Replace potential secret values with [REDACTED]."""
    for pat in _SECRET_PATTERNS:
        # Keep any leading label group (group 1), replace the secret part
        if pat.groups:
            text = pat.sub(lambda m: (m.group(1) if m.lastindex and m.lastindex >= 1 else '') + '[REDACTED]', text)
        else:
            text = pat.sub('[REDACTED]', text)
    return text


def _input_summary(tool_input: dict, tool_name: str) -> str:
    """Build a short human-readable summary of the tool input, secrets redacted."""
    if not tool_input:
        return ''
    if tool_name in ('Bash', 'bash'):
        return _redact(str(tool_input.get('command', ''))[:400])[:200]
    if tool_name in ('Read', 'Write', 'Edit'):
        return str(tool_input.get('file_path', ''))[:200]
    if tool_name in ('WebFetch', 'WebSearch'):
        return _redact(str(tool_input.get('url', tool_input.get('query', '')))[:400])[:200]
    # Generic fallback: first string value found
    for v in tool_input.values():
        if isinstance(v, str):
            return _redact(v[:400])[:200]
    return ''


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = payload.get('session_id') or ''
    tool_name = payload.get('tool_name') or ''
    tool_input = payload.get('tool_input') or {}
    cwd = payload.get('cwd') or ''
    # CC provides tool_use_id (stable per-call ID shared with PreToolUse) and
    # duration_ms (native wall-clock measurement, more accurate than hook-side
    # timestamps because it excludes hook overhead).
    tool_use_id = payload.get('tool_use_id') or None
    duration_ms = payload.get('duration_ms')
    if not isinstance(duration_ms, int):
        duration_ms = None
    success = not bool(payload.get('tool_response', {}).get('is_error') if isinstance(payload.get('tool_response'), dict) else False)

    if not session_id or not tool_name:
        sys.exit(0)

    token = _dashboard_token()
    if not token:
        sys.exit(0)

    port = _web_port()
    base_url = f'http://localhost:{port}/api'

    body = json.dumps({
        'session_id': session_id,
        'tool_name': tool_name,
        'input_summary': _input_summary(tool_input, tool_name),
        'success': success,
        'agent_id': ledger_lib.agent_id_from_cwd(cwd),
        # trace_id holds the CC-native tool_use_id: stable, unique per call,
        # present in both Pre and PostToolUse payloads (empirically verified).
        # No PreToolUse hook needed -- CC already gives us the correlation key
        # and the latency measurement in one place.
        'trace_id': tool_use_id,
        'duration_ms': duration_ms,
    }).encode()

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}',
    }

    try:
        urllib.request.urlopen(
            urllib.request.Request(f'{base_url}/tool-log', data=body, headers=headers, method='POST'),
            timeout=3,
        )
    except Exception:
        pass  # never block the agent

    # Prune old entries with ~1% probability to keep the table from growing indefinitely.
    if random.random() < 0.01:
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f'{base_url}/tool-log/prune',
                    data=b'{}',
                    headers=headers,
                    method='POST',
                ),
                timeout=3,
            )
        except Exception:
            pass

    sys.exit(0)


if __name__ == '__main__':
    main()

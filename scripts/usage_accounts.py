"""The Claude accounts' quota rules: ONE table for every reader (BELA 6587, KISPROGI 6585).

usage-collect.py, usage-check.py, usage-fleet-monitor.py and usage-check-info.py
all load this file by path (each has a small _load_usage_accounts()), so the windows an account
must report, its warning / STOP (fleet pause) thresholds and its release rule
cannot drift apart.

Owner decision 2026-11-28 (Telegram 8728, BELA 6580, 6586):
  styu01      both windows required; 5-hour warning 90, STOP 98; weekly warning
              80, STOP 90. A missing 5-hour window is ISMERETLEN / STALE.
  info        (Ultra, Moho) no 5-hour limit: only the weekly window is required
              and judged (a 5-hour one is ignored even if a response carries
              it); weekly warning 90, STOP 96.
  ismeretlen  a token that matches neither account (or a snapshot without a
              valid account): the stricter fleet rule, both windows required.
Release (a paused fleet may resume): every judged window below its warning
value (styu01: 5-hour < 90 AND weekly < 80; info: weekly < 90).

Stdlib only, no side effects. Adding an account or changing a threshold is one
edit here.
"""

ACCOUNT_RULES = {
    "styu01": {"five_hour": {"warn": 90, "stop": 98}, "seven_day": {"warn": 80, "stop": 90}},
    "info": {"seven_day": {"warn": 90, "stop": 96}},
    "ismeretlen": {"five_hour": {"warn": 90, "stop": 98}, "seven_day": {"warn": 80, "stop": 90}},
}
UNKNOWN_ACCOUNT = "ismeretlen"
ACCOUNTS = tuple(ACCOUNT_RULES)
WINDOWS = ("five_hour", "seven_day")


def account_or_unknown(value):
    """A valid account name, else "ismeretlen"."""
    return value if isinstance(value, str) and value in ACCOUNT_RULES else UNKNOWN_ACCOUNT


def required_windows(account):
    """The windows the account must report (and the only ones judged), in WINDOWS order."""
    rules = ACCOUNT_RULES[account_or_unknown(account)]
    return tuple(w for w in WINDOWS if w in rules)


def warn_values(account):
    rules = ACCOUNT_RULES[account_or_unknown(account)]
    return {w: rules[w]["warn"] for w in required_windows(account)}


def stop_values(account):
    rules = ACCOUNT_RULES[account_or_unknown(account)]
    return {w: rules[w]["stop"] for w in required_windows(account)}


def accounts_without(window):
    """The accounts that have no limit on (and need not report) a window."""
    return tuple(a for a, rules in ACCOUNT_RULES.items() if window not in rules)


def verdict(account, values):
    """'STOP' / 'FIGYELMEZTETÉS' / 'OK' from {window: used percent} of the required windows."""
    stops, warns = stop_values(account), warn_values(account)
    if any(values[w] >= stops[w] for w in stops):
        return "STOP"
    if any(values[w] >= warns[w] for w in warns):
        return "FIGYELMEZTETÉS"
    return "OK"


def released(account, values):
    """Whether a paused fleet may resume: every judged window below its warning value."""
    warns = warn_values(account)
    return all(values[w] < warns[w] for w in warns)


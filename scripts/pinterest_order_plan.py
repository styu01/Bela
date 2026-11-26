"""Optional ordering of approved Pinterest drafts; never creates approvals."""
import json
import os
import re
import stat
from functools import lru_cache
from pathlib import Path
from pinterest_schedule import Error

PLAN = 'pinterest-order-plan.json'
MAX_BYTES = 1024 * 1024
MAX_KEYS = 10000


class Plan(list):
    days = None


def identity(value):
    if not isinstance(value, str):
        raise Error('order_plan_invalid')
    match = re.fullmatch(r'(D|V)([1-9][0-9]{0,3})_P([1-9][0-9]{0,2})', value)
    if not match:
        raise Error('order_plan_invalid')
    kind, day, pin = match.groups()
    return int(day), int(pin), kind


def label(key):
    day, pin, kind = key
    return f'{kind}{day}_P{pin}'


def validate(value):
    if not isinstance(value, dict) or set(value) not in ({'order'}, {'order', 'days'}):
        raise Error('order_plan_invalid')
    order = value['order']
    if not isinstance(order, list) or len(order) > MAX_KEYS:
        raise Error('order_plan_invalid')
    keys = [identity(item) for item in order]
    if len(set(keys)) != len(keys):
        raise Error('order_plan_invalid')
    result = Plan(keys)
    if 'days' in value:
        days = value['days']
        if (not isinstance(days, list) or len(days) > 56
                or any(not isinstance(row, list) or not 1 <= len(row) <= 7 for row in days)
                or [key for row in days for key in row] != order):
            raise Error('order_plan_invalid')
        result.days = [[identity(key) for key in row] for row in days]
        for row in result.days:
            if any((day + 4, pin, 'V') in row for day, pin, kind in row if kind == 'D'):
                raise Error('order_plan_invalid')
            if any(a[2] == b[2] for a, b in zip(row, row[1:])):
                raise Error('order_plan_invalid')
    return result


def unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise Error('order_plan_invalid')
        out[key] = value
    return out


def load_plan(store):
    path = Path(store) / PLAN
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise Error('order_plan_read_failed') from None
    try:
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise Error('order_plan_read_failed')
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise Error('order_plan_invalid')
        return validate(json.loads(raw, object_pairs_hook=unique_object))
    except (ValueError, UnicodeError, RecursionError):
        raise Error('order_plan_invalid') from None
    except OSError:
        raise Error('order_plan_read_failed') from None


def generate(image_days, video_days, available=None):
    """Four ordered source days per medium, with video day = image day + 4.

    Rotating the video sources by two days separates every matching pair
    by at least three publishing days. Each source preserves P1..P7.
    """
    for days in (image_days, video_days):
        if (not isinstance(days, list) or len(days) != 4
                or any(type(day) is not int or not 1 <= day <= 9999 for day in days)
                or days != sorted(set(days))):
            raise Error('order_plan_days_invalid')
    if video_days != [day + 4 for day in image_days] or set(image_days) & set(video_days):
        raise Error('order_plan_days_invalid')
    images = [f'D{day}_P{pin}' for day in image_days for pin in range(1, 8)]
    rotated = video_days[2:] + video_days[:2]
    videos = [f'V{day}_P{pin}' for day in rotated for pin in range(1, 8)]
    order = []
    indexes = {'D': 0, 'V': 0}
    for day in range(8):
        kinds = 'VDVDVDV' if day % 2 == 0 else 'DVDVDVD'
        for kind in kinds:
            source = images if kind == 'D' else videos
            order.append(source[indexes[kind]])
            indexes[kind] += 1
    result = {'order': order}
    validate(result)
    if available is not None:
        validate({'order': available})
        if not set(available) <= set(order):
            raise Error('order_plan_invalid')
        return generate_available(images, videos, set(available))
    return result


def generate_available(images, videos, available):
    """Globally minimize day count, then size variance and parity mismatch.

    Fixed per-medium queues preserve each source's P sequence. A day is
    strictly alternating and cannot contain a D/V(+4) pair. If stock makes
    seven-slot days impossible, produce shorter days rather than 7/0 days.
    """
    images = [key for key in images if key in available]
    videos = [key for key in videos if key in available]
    n, m = len(images), len(videos)
    total = n + m
    if not total:
        return {'order': [], 'days': []}
    for count in range(max((total + 6) // 7, abs(n - m), 1), total + 1):
        @lru_cache(None)
        def solve(i, j, day):
            left = count - day
            remaining = total - i - j
            if left == 0:
                return (0, ()) if remaining == 0 else None
            if not left <= remaining <= 7 * left or abs((n-i)-(m-j)) > left:
                return None
            best = None
            for d in range(min(4, n-i) + 1):
                for v in range(min(4, m-j) + 1):
                    size = d + v
                    if not 1 <= size <= 7 or abs(d-v) > 1:
                        continue
                    chosen_d, chosen_v = images[i:i+d], videos[j:j+v]
                    if any(f'V{identity(key)[0]+4}_P{identity(key)[1]}' in chosen_v for key in chosen_d):
                        continue
                    tail = solve(i+d, j+v, day+1)
                    if tail is None:
                        continue
                    # Size variance dominates the alternating 3/4 preference.
                    cost = tail[0] + 100 * (size*count-total)**2 + abs(d-(3 if day%2 == 0 else 4))
                    candidate = (cost, ((d,v),) + tail[1])
                    if best is None or candidate < best:
                        best = candidate
            return best
        answer = solve(0, 0, 0)
        if answer is None:
            continue
        rows = []
        i = j = 0
        for day, (d, v) in enumerate(answer[1]):
            start = 'D' if d > v else 'V' if v > d else ('V' if day%2 == 0 else 'D')
            row = []
            for slot in range(d+v):
                kind = start if slot%2 == 0 else ('V' if start == 'D' else 'D')
                if kind == 'D':
                    row.append(images[i]);i += 1
                else:
                    row.append(videos[j]);j += 1
            rows.append(row)
        result = {'order': [key for row in rows for key in row], 'days': rows}
        validate(result)
        return result
    raise Error('order_plan_invalid')  # single-pin days always provide a solution


def read_available(path):
    """Same bounded, strict reader as a plan; availability file is a JSON list."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise Error('order_plan_read_failed')
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise Error('order_plan_invalid')
        value = json.loads(raw)
        validate({'order': value})
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise Error('order_plan_invalid') from None
    except OSError:
        raise Error('order_plan_read_failed') from None

#!/usr/bin/env python3
"""Generate an eight-day mixed Pinterest order plan, offline only."""
import sys
sys.dont_write_bytecode = True
import argparse
import json
from pathlib import Path
from pinterest_schedule import ROOT, SLOTS_ET, Error, common, lock
from pinterest_order_plan import PLAN, generate, read_available


def days(value):
    try:
        if '-' in value:
            first, last = value.split('-')
            if not first.isascii() or not first.isdecimal() or not last.isascii() or not last.isdecimal():
                raise ValueError()
            start, end = int(first), int(last)
            if end - start != 3:
                raise ValueError()
            return list(range(start, end + 1))
        parts = value.split(',')
        if len(parts) != 4 or any(not part.isascii() or not part.isdecimal() for part in parts):
            raise ValueError()
        return [int(part) for part in parts]
    except ValueError:
        raise argparse.ArgumentTypeError('order_plan_days_invalid') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-days', required=True, type=days)
    parser.add_argument('--video-days', required=True, type=days)
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'store')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--available-keys', type=Path, help='JSON list of approved canonical keys')
    args = parser.parse_args(argv)
    try:
        available = read_available(args.available_keys) if args.available_keys is not None else None
        plan = generate(args.image_days, args.video_days, available)
        rows = plan.get('days', [plan['order'][day*7:(day+1)*7] for day in range(8)])
        if available is not None:
            full = generate(args.image_days, args.video_days)['order']
            print(json.dumps({'missing_keys': [key for key in full if key not in set(available)],
                              'days': [{'day': index+1, 'images': sum(key.startswith('D') for key in row),
                                        'videos': sum(key.startswith('V') for key in row), 'unused_slots': 7-len(row)}
                                       for index, row in enumerate(rows)]}))
        if args.dry_run:
            print('Day  D/V  ' + '  '.join(f'{hour:02d} ET' for hour in SLOTS_ET))
            for day, pins in enumerate(rows):
                counts = f"{sum(key.startswith('D') for key in pins)}/{sum(key.startswith('V') for key in pins)}"
                print(f'{day + 1:>3}  {counts}  ' + '  '.join(pins + ['--']*(7-len(pins))))
        else:
            with lock(args.state_dir):
                common.atomic_state(args.state_dir / PLAN, plan)
            print(f"order_plan_written: {len(plan['order'])} pins, {len(rows)} days")
        return 0
    except Error as exc:
        code = str(exc)
        if code not in {'order_plan_days_invalid', 'order_plan_invalid', 'order_plan_read_failed', 'busy', 'lock_failed', 'state_write_failed'}:
            code = 'order_plan_error'
        print(code, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

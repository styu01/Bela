#!/usr/bin/env python3
"""Offline subset, day-boundary, cursor and notification stress tests."""
import importlib.util
import io
import json
import random
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import pinterest_order_plan as o
import pinterest_schedule as s


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


b = module('holes_base', SCRIPTS/'__tests__/pinterest-order-plan.test.py')
p = b.p
cli = b.cli
IMAGES = [19, 20, 21, 22]
VIDEOS = [23, 24, 25, 26]
FULL = o.generate(IMAGES, VIDEOS)['order']


class SubsetTests(unittest.TestCase):
    def check(self, stock):
        plan = o.generate(IMAGES, VIDEOS, stock)
        o.validate(plan)
        self.assertEqual(set(plan['order']), set(stock))
        self.assertEqual(len(plan['order']), len(stock))
        self.assertEqual(plan['order'], sum(plan['days'], []))
        for row in plan['days']:
            self.assertLessEqual(len(row), 7)
            self.assertTrue(all(a[0] != c[0] for a, c in zip(row, row[1:])))
            for key in row:
                day, pin, kind = o.identity(key)
                if kind == 'D':
                    self.assertNotIn(f'V{day+4}_P{pin}', row)
        for kind, days in [('D', IMAGES), ('V', VIDEOS)]:
            for day in days:
                actual = [o.identity(key)[1] for key in plan['order'] if key.startswith(f'{kind}{day}_')]
                self.assertEqual(actual, sorted(actual))
        return plan

    def test_every_single_rejection(self):
        for rejected in FULL:
            with self.subTest(rejected=rejected):
                plan = self.check([key for key in FULL if key != rejected])
                self.assertEqual(Counter(map(len, plan['days'])), {7: 7, 6: 1})

    def test_full_available_same_as_original_order(self):
        self.assertEqual(self.check(FULL)['order'], FULL)

    def test_300_random_subsets_deterministic(self):
        rng = random.Random(5418)
        for _ in range(300):
            stock = rng.sample(FULL, rng.randrange(57))
            plan = self.check(stock)
            rng.shuffle(stock)
            self.assertEqual(plan, o.generate(IMAGES, VIDEOS, stock))

    def test_empty_single_media_and_pair_only(self):
        self.assertEqual(self.check([]), {'order': [], 'days': []})
        for kind in ['D', 'V']:
            plan = self.check([key for key in FULL if key.startswith(kind)])
            self.assertEqual(len(plan['days']), 28)
            self.assertTrue(all(len(row) == 1 for row in plan['days']))
        self.assertEqual(len(self.check(['D19_P1', 'V23_P1'])['days']), 2)

    def test_invalid_stock_never_writes(self):
        for stock in [['D19_P1', 'D19_P1'], ['D19_P8'], ['D18_P1'], ['V23_P1_S1'], 'D19_P1', None]:
            if stock is None:
                continue  # None explicitly requests the legacy complete plan
            with self.subTest(stock=stock), self.assertRaises(s.Error):
                o.generate(IMAGES, VIDEOS, stock)

    def test_day_schema_fail_closed(self):
        for value in [
            {'order': ['D19_P1'], 'days': []},
            {'order': [], 'days': [[]]},
            {'order': ['D19_P1'], 'days': 'bad'},
            {'order': ['D19_P1', 'V23_P1'], 'days': [['D19_P1', 'V23_P1']]},
            {'order': ['D19_P1', 'D19_P2'], 'days': [['D19_P1', 'D19_P2']]},
            {'order': FULL, 'days': [FULL]},
            {'order': [], 'days': [], 'extra': True}]:
            with self.subTest(value=value), self.assertRaises(s.Error):
                o.validate(value)

    def test_cli_subset_diagnostics_dry_and_write(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'available.json'
            path.write_text(json.dumps(FULL[1:]))
            store = Path(root)/'store'
            args = ['--image-days', '19-22', '--video-days', '23-26', '--available-keys', str(path), '--state-dir', str(store)]
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(args+['--dry-run']), 0)
            report = json.loads(out.getvalue().splitlines()[0])
            self.assertEqual(report['missing_keys'], [FULL[0]])
            self.assertEqual(sum(row['unused_slots'] for row in report['days']), 1)
            self.assertFalse(store.exists())
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(args), 0)
            self.assertIn('55 pins, 8 days', out.getvalue())
            self.assertEqual(len(o.load_plan(store).days), 8)

    def test_available_file_missing_corrupt_and_no_store_write(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'available.json'
            args = ['--image-days', '19-22', '--video-days', '23-26', '--available-keys', str(path), '--state-dir', str(Path(root)/'store')]
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(args), 1)
            self.assertEqual(err.getvalue(), 'order_plan_read_failed\n')
            for raw in ['broken', '{}', '["D19_P1","D19_P1"]', '["D19_P8"]']:
                path.write_text(raw)
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(args), 1)
            self.assertFalse((Path(root)/'store').exists())


class HoleNotificationTests(unittest.TestCase):
    setUp = b.IntegrationTests.setUp
    drafts = b.IntegrationTests.drafts
    write_plan = b.IntegrationTests.write_plan
    run_topup = b.IntegrationTests.run_topup
    scheduled = b.IntegrationTests.scheduled

    def hole_inventory(self):
        self.drafts(21)
        self.write_plan(['D1_P1', 'D1_P2', 'D1_P3', 'D1_P4'])
        self.buffer.posts['draft'] = [post for post in self.buffer.posts['draft'] if post['id'] != '1']
        self.scheduled(10)

    def test_true_hole_not_prefix_or_suffix(self):
        self.drafts(3)
        self.write_plan(['D99_P1', 'D1_P1', 'V23_P1', 'D1_P3', 'D99_P2'])
        result = self.run_topup()
        self.assertEqual(result['order_plan_holes'], ['V23_P1'])
        self.assertEqual(result['order_plan_missing'], ['D99_P1', 'V23_P1', 'D99_P2'])
        self.assertEqual([edit['id'] for edit in self.buffer.edits], ['0', '2', '1'])
        self.assertEqual(len(self.notify.sent), 1)
        self.assertIn('order_plan_holes', self.notify.sent[0])

    def test_only_published_prefix_is_silent(self):
        self.drafts(21)
        self.write_plan(['V19_P1', 'D1_P1', 'D1_P2'])
        self.scheduled(10)
        result = self.run_topup()
        self.assertEqual(result['order_plan_holes'], [])
        self.assertEqual(self.notify.sent, [])

    def test_past_scheduled_not_an_unpublished_anchor(self):
        self.drafts(21)
        self.write_plan(['D99_P1', 'V23_P1', 'D1_P1'])
        self.scheduled(10)
        self.buffer.posts['scheduled'][0].update(metadata={'url': 'https://e.test/?utm_content=D99_P1'}, dueAt=(b.base.NOW-timedelta(hours=1)).isoformat())
        self.assertEqual(self.run_topup()['order_plan_holes'], [])

    def test_future_scheduled_is_an_anchor(self):
        self.drafts(21)
        self.write_plan(['D99_P1', 'V23_P1', 'D1_P1'])
        self.scheduled(10)
        self.buffer.posts['scheduled'][0]['metadata'] = {'url': 'https://e.test/?utm_content=D99_P1'}
        self.assertEqual(self.run_topup()['order_plan_holes'], ['V23_P1'])

    def test_daily_gate_survives_new_instances(self):
        self.hole_inventory()
        for hour in range(6):
            result = self.run_topup(now=b.base.NOW+timedelta(hours=hour))
            self.assertEqual(result['status'], 'queue_full')
        self.assertEqual(len(self.notify.sent), 1)
        self.assertIn('order_plan_holes', json.loads((self.store/p.STATE).read_text())['reported'])
        self.run_topup(now=b.base.NOW+timedelta(days=1))
        self.assertEqual(len(self.notify.sent), 2)

    def test_dry_run_suppresses_alert_and_state(self):
        self.hole_inventory()
        result = self.run_topup(dry=True)
        self.assertEqual(result['order_plan_holes'], ['D1_P2'])
        self.assertEqual(self.notify.sent, [])
        self.assertFalse((self.store/p.STATE).exists())

    def test_failed_delivery_retries(self):
        self.hole_inventory()
        self.notify.fail = True
        self.assertEqual(self.run_topup()['status'], 'error')
        self.notify.fail = False
        self.run_topup()
        self.assertEqual(len(self.notify.sent), 1)

    def test_budapest_midnight_resets_hole_gate(self):
        self.hole_inventory()
        before = datetime(2026, 11, 5, 22, 59, tzinfo=timezone.utc)
        self.run_topup(now=before)
        self.run_topup(now=before+timedelta(minutes=2))
        self.assertEqual(len(self.notify.sent), 2)

    def test_future_gate_stamp_does_not_silence_hole(self):
        self.hole_inventory()
        (self.store/p.STATE).write_text(json.dumps({'reported': {'order_plan_holes': '2099-01-01'}}))
        self.run_topup()
        self.assertEqual(len(self.notify.sent), 1)

    def test_no_plan_bytewise_result_payload_state_and_notices(self):
        self.drafts(21)
        from copy import deepcopy
        original = deepcopy(self.buffer.posts['draft'])
        rng = random.Random(541899)
        for trial in range(40):
            inventory = rng.sample(original, rng.randrange(22))
            captured = []
            for impl in [b.legacy, p]:
                buffer = b.base.FakeBuffer()
                buffer.posts['draft'] = deepcopy(inventory)
                notify = b.base.Notify()
                with tempfile.TemporaryDirectory() as root:
                    store = Path(root)
                    runner = impl.TopUp(store, b.base.NOW, False, buffer, notify, self.pins, max_add=1 if trial%2 else None)
                    result = runner.run()
                    state = (store/p.STATE).read_bytes() if (store/p.STATE).exists() else None
                    captured.append((json.dumps(result).encode(), json.dumps(buffer.edits).encode(), state, notify.sent))
            self.assertEqual(captured[0], captured[1])

    def test_corrupt_plan_still_fails_before_fetch(self):
        self.hole_inventory()
        (self.store/o.PLAN).write_text('{"order":[],"days":[[]]}')
        self.assertEqual(self.run_topup()['error'], 'order_plan_invalid')
        self.assertEqual(self.buffer.fetches, [])


class DayBoundaryTests(unittest.TestCase):
    setUp = b.IntegrationTests.setUp
    make_complete_inventory = b.IntegrationTests.make_complete_inventory
    run_topup = b.IntegrationTests.run_topup

    def install(self, stock):
        self.make_complete_inventory()
        self.buffer.posts['draft'] = [post for post in self.buffer.posts['draft'] if post['id'] in stock]
        plan = o.generate(IMAGES, VIDEOS, stock)
        (self.store/o.PLAN).write_text(json.dumps(plan))
        return plan

    def test_55_pins_repeated_queue_topups_preserve_six_pin_day(self):
        plan = self.install([key for key in FULL if key != 'D19_P4'])
        now = datetime(2026, 11, 14, 11, tzinfo=timezone.utc)
        with patch.object(p, 'video_thumbnail_offset', return_value=5850):
            for tick in range(8*24):
                self.buffer.posts['scheduled'] = [post for post in self.buffer.posts['scheduled'] if p.parse_due(post['dueAt']) > now]
                random.Random(tick).shuffle(self.buffer.posts['draft'])
                result = self.run_topup(now=now)
                self.assertNotEqual(result['status'], 'error', result)
                self.assertLessEqual(len(self.buffer.posts['scheduled']), 10)
                now += timedelta(hours=1)
        actual = {}
        for edit in self.buffer.edits:
            day = p.parse_due(edit['dueAt']).astimezone(s.ET).date()
            actual.setdefault(day, []).append(edit['id'])
        self.assertEqual(list(actual.values()), plan['days'])
        self.assertEqual(len(self.buffer.edits), 55)

    def test_published_last_row_cursor_prevents_same_day_next_row(self):
        plan = self.install(['D19_P1', 'V23_P1'])
        now = datetime(2026, 11, 14, 11, tzinfo=timezone.utc)
        with patch.object(p, 'video_thumbnail_offset', return_value=5850):
            first = p.TopUp(self.store, now, False, self.buffer, self.notify, self.pins, max_add=1).run()
            self.assertEqual(first['added'], 1)
            first_due = p.parse_due(self.buffer.edits[0]['dueAt'])
            self.buffer.posts['scheduled'] = []  # first pin already published
            result = self.run_topup(now=first_due+timedelta(hours=1))
        self.assertEqual(result['added'], 1)
        second_due = p.parse_due(self.buffer.edits[1]['dueAt'])
        self.assertEqual((second_due.astimezone(s.ET).date()-first_due.astimezone(s.ET).date()).days, 1)

    def test_cursor_does_not_skip_extra_day_after_long_pause(self):
        self.install(['D19_P1', 'V23_P1'])
        now = datetime(2026, 11, 14, 11, tzinfo=timezone.utc)
        with patch.object(p, 'video_thumbnail_offset', return_value=5850):
            p.TopUp(self.store, now, False, self.buffer, self.notify, self.pins, max_add=1).run()
            self.buffer.posts['scheduled'] = []
            self.run_topup(now=now+timedelta(days=3))
        self.assertEqual(p.parse_due(self.buffer.edits[1]['dueAt']).astimezone(s.ET).date(), (now+timedelta(days=3)).astimezone(s.ET).date())

    def test_subset_dry_run_has_no_cursor_write(self):
        self.install(FULL[1:])
        with patch.object(p, 'video_thumbnail_offset', return_value=5850):
            self.assertEqual(self.run_topup(dry=True)['added'], 10)
        self.assertFalse((self.store/p.STATE).exists())
        self.assertEqual(self.buffer.edits, [])

    def test_bad_matching_cursor_fails_closed(self):
        self.install(FULL)
        with patch.object(p, 'video_thumbnail_offset', return_value=5850):
            self.run_topup()
        state = json.loads((self.store/p.STATE).read_text())
        state['order_plan_cursor']['row'] = -1
        (self.store/p.STATE).write_text(json.dumps(state))
        result = self.run_topup()
        self.assertEqual(result['error'], 'state_read_failed')

    def test_cursor_write_failure_counts_accepted_pin_and_recovers(self):
        self.install(FULL[1:])
        now = datetime(2026, 11, 14, 11, tzinfo=timezone.utc)
        with patch.object(p, 'video_thumbnail_offset', return_value=5850):
            with patch.object(p.common, 'atomic_state', side_effect=s.Error('state_write_failed')):
                result = self.run_topup(now=now)
            self.assertEqual(result['added'], 1)
            self.assertEqual(result['error'], 'state_write_failed')
            self.assertEqual(len(self.buffer.edits), 1)
            second = self.run_topup(now=now)
            self.assertNotEqual(second['status'], 'error', second)
            self.assertEqual(len(set(edit['id'] for edit in self.buffer.edits)), 10)

    def test_short_day_boundary_with_max_add_one_each_run(self):
        plan = self.install(FULL[1:])
        now = datetime(2026, 11, 14, 11, tzinfo=timezone.utc)
        with patch.object(p, 'video_thumbnail_offset', return_value=5850):
            for _ in range(8):
                result = p.TopUp(self.store, now, False, self.buffer, self.notify, self.pins, max_add=1).run()
                self.assertNotEqual(result['status'], 'error', result)
        dates = [p.parse_due(edit['dueAt']).astimezone(s.ET).date() for edit in self.buffer.edits]
        first_len = len(plan['days'][0])
        self.assertEqual(len(set(dates[:first_len])), 1)
        self.assertNotEqual(dates[first_len-1], dates[first_len])

    def test_unlisted_drafts_continue_together_after_short_plan(self):
        self.make_complete_inventory()
        plan = o.generate(IMAGES, VIDEOS, ['D19_P1'])
        (self.store/o.PLAN).write_text(json.dumps(plan))
        now = datetime(2026, 11, 14, 11, tzinfo=timezone.utc)
        with patch.object(p, 'video_thumbnail_offset', return_value=5850):
            result = self.run_topup(now=now)
        self.assertEqual(result['added'], 10)
        dates = [p.parse_due(edit['dueAt']).astimezone(s.ET).date() for edit in self.buffer.edits]
        self.assertNotEqual(dates[0], dates[1])
        self.assertEqual(len(set(dates[1:8])), 1)


if __name__ == '__main__':
    unittest.main()

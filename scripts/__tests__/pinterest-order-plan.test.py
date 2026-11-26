#!/usr/bin/env python3
"""Offline ordering stress tests. Every network connection is prohibited."""
import importlib.util
import io
import json
import os
import random
import socket
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
sys.dont_write_bytecode = True
import pinterest_order_plan as order
import pinterest_schedule as s


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


base = module('order_test_fixtures', SCRIPTS / '__tests__/pinterest-buffer-topup.test.py')
p = base.p
cli = module('order_cli', SCRIPTS / 'pinterest-order-plan.py')
legacy = module('order_baseline', SCRIPTS / '__tests__/fixtures/pinterest-buffer-topup-before-order.py')


def setUpModule():
    global guards
    guards = [patch.object(socket.socket, 'connect', side_effect=AssertionError('NETWORK_DISABLED')),
              patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('NETWORK_DISABLED')),
              patch.object(socket, 'getaddrinfo', side_effect=AssertionError('NETWORK_DISABLED'))]
    for guard in guards:
        guard.start()


def tearDownModule():
    for guard in reversed(guards):
        guard.stop()


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)
        self.path = self.store / order.PLAN

    def test_absent_and_empty_order_are_distinct(self):
        self.assertIsNone(order.load_plan(self.store))
        self.path.write_text('{"order":[]}')
        self.assertEqual(order.load_plan(self.store), [])

    def test_json_and_schema_adversaries(self):
        values = ['', 'broken', 'null', '[]', '{}', '{"order":null}', '{"order":"D19_P1"}',
                  '{"order":[],"order":[]}', '{"order":[],"ignored":true}',
                  '{"order":["D19_P1","D19_P1"]}', '{"order":[[19,1,"D"]]}',
                  '{"order":[true]}', '{"order":[1]}', '{"order":[null]}',
                  '{"order":[{"D19_P1":1}]}', '{"order":[],"ORDER":[]}',
                  '[[' * 2000 + ']]' * 2000, '{"order":[NaN]}', '{"order":[]} trailing']
        for raw in values:
            with self.subTest(raw=raw[:70]):
                self.path.write_text(raw)
                with self.assertRaisesRegex(s.Error, '^order_plan_invalid$'):
                    order.load_plan(self.store)

    def test_key_boundaries_canonical_form_and_suffixes(self):
        for value in ['D0_P1', 'V1_P0', 'D01_P1', 'D1_P01', 'D10000_P1', 'V1_P1000',
                      'D1_P1_S1', 'd1_P1', 'D1_P1\n', ' D1_P1', 'D１_P1', 'Q1_P1', '']:
            with self.subTest(value=value), self.assertRaises(s.Error):
                order.identity(value)
        for value in ['D1_P1', 'V9999_P999', 'D19_P7']:
            self.assertEqual(order.label(order.identity(value)), value)

    def test_invalid_utf8_and_size_limits(self):
        for raw in [b'\xff', b' ' * (order.MAX_BYTES + 1)]:
            self.path.write_bytes(raw)
            with self.assertRaisesRegex(s.Error, '^order_plan_invalid$'):
                order.load_plan(self.store)
        keys = [f'D{day}_P{pin}' for day in range(1, 102) for pin in range(1, 101)]
        with self.assertRaises(s.Error):
            order.validate({'order': keys})
        self.assertEqual(len(order.validate({'order': keys[:10000]})), 10000)

    def test_directory_fifo_symlink_and_broken_symlink_fail_closed(self):
        self.path.mkdir()
        with self.assertRaisesRegex(s.Error, '^order_plan_read_failed$'):
            order.load_plan(self.store)
        self.path.rmdir()
        os.mkfifo(self.path)
        with self.assertRaisesRegex(s.Error, '^order_plan_read_failed$'):
            order.load_plan(self.store)
        self.path.unlink()
        target = self.store / 'target'
        target.write_text('{"order":[]}')
        self.path.symlink_to(target)
        for _ in range(2):
            with self.assertRaisesRegex(s.Error, '^order_plan_read_failed$'):
                order.load_plan(self.store)
            if target.exists():
                target.unlink()

    def test_permission_failure_fixed_code(self):
        with patch.object(order.os, 'open', side_effect=PermissionError('SECRET')):
            with self.assertRaisesRegex(s.Error, '^order_plan_read_failed$'):
                order.load_plan(self.store)


class GeneratorTests(unittest.TestCase):
    def check_invariants(self, images, videos):
        plan = order.generate(images, videos)
        keys = plan['order']
        self.assertEqual(len(keys), 56)
        self.assertEqual(len(set(keys)), 56)
        positions = {key: index // 7 for index, key in enumerate(keys)}
        self.assertEqual(Counter(key[0] for key in keys), {'D': 28, 'V': 28})
        for day in range(8):
            row = keys[day * 7:(day + 1) * 7]
            self.assertEqual(Counter(key[0] for key in row), {'D': 3 + day % 2, 'V': 4 - day % 2})
            self.assertTrue(all(a[0] != b[0] for a, b in zip(row, row[1:])))
        self.assertTrue(all(a[0] != b[0] for a, b in zip(keys, keys[1:])))
        for image_day, video_day in zip(images, videos):
            for pin in range(1, 8):
                self.assertIn(abs(positions[f'D{image_day}_P{pin}'] - positions[f'V{video_day}_P{pin}']), (3, 4, 5))
            for kind, source_day in [('D', image_day), ('V', video_day)]:
                self.assertEqual([order.identity(key)[1] for key in keys if key.startswith(f'{kind}{source_day}_')], list(range(1, 8)))
        self.assertEqual(plan, order.generate(images, videos))

    def test_requested_plan(self):
        self.check_invariants([19, 20, 21, 22], [23, 24, 25, 26])

    def test_thousand_random_source_blocks(self):
        rng = random.Random(9185)
        for _ in range(1000):
            first = rng.randint(1, 9992)
            self.check_invariants(list(range(first, first + 4)), list(range(first + 4, first + 8)))

    def test_invalid_days(self):
        for images, videos in [([], []), ([19, 20, 21], [23, 24, 25]),
                               ([19, 19, 21, 22], [23, 24, 25, 26]),
                               ([20, 19, 21, 22], [23, 24, 25, 26]),
                               ([True, 2, 3, 4], [5, 6, 7, 8]),
                               ([1.0, 2, 3, 4], [5, 6, 7, 8]),
                               ([0, 1, 2, 3], [4, 5, 6, 7]),
                               ([19, 20, 21, 22], [24, 25, 26, 27]),
                               ([9993, 9994, 9995, 9996], [9997, 9998, 9999, 10000]),
                               ([1, 5, 9, 13], [5, 9, 13, 17]),
                               (None, [23, 24, 25, 26])]:
            with self.subTest(images=images), self.assertRaises(s.Error):
                order.generate(images, videos)

    def test_cli_day_parser(self):
        self.assertEqual(cli.days('19-22'), [19, 20, 21, 22])
        self.assertEqual(cli.days('19,20,21,22'), [19, 20, 21, 22])
        for value in ['19-21', '19-99999999999999999', '22-19', '19-22-23', '19,,21,22',
                      '19,20,21', '١٩-٢٢', '19.0-22', '1-4\n']:
            with self.subTest(value=value), self.assertRaises(Exception):
                cli.days(value)

    def test_cli_dry_run_does_not_create_state_dir(self):
        with tempfile.TemporaryDirectory() as root:
            store = Path(root) / 'absent'
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(['--image-days', '19-22', '--video-days', '23-26', '--state-dir', str(store), '--dry-run'])
            self.assertEqual(code, 0)
            self.assertEqual(len(output.getvalue().splitlines()), 9)
            self.assertIn('21 ET', output.getvalue())
            self.assertFalse(store.exists())

    def test_cli_atomic_write_and_shared_lock(self):
        with tempfile.TemporaryDirectory() as root:
            store = Path(root)
            args = ['--image-days', '19-22', '--video-days', '23-26', '--state-dir', root]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args), 0)
            self.assertEqual(order.load_plan(store), order.validate(order.generate([19, 20, 21, 22], [23, 24, 25, 26])))
            before = (store / order.PLAN).read_bytes()
            with s.lock(store), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(args), 1)
            self.assertEqual((store / order.PLAN).read_bytes(), before)
            self.assertEqual(list(store.glob('*.tmp')), [])

    def test_cli_failed_replace_preserves_previous_plan(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / order.PLAN
            path.write_text('{"order":[]}')
            args = ['--image-days', '19-22', '--video-days', '23-26', '--state-dir', root]
            with patch.object(s.common.os, 'replace', side_effect=OSError('SECRET')), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(args), 1)
            self.assertEqual(path.read_text(), '{"order":[]}')
            self.assertEqual(err.getvalue(), 'state_write_failed\n')
            self.assertEqual(list(Path(root).glob('*.tmp')), [])


class IntegrationTests(unittest.TestCase):
    setUp = base.TopUpTests.setUp
    drafts = base.TopUpTests.drafts
    scheduled = base.TopUpTests.scheduled
    run_topup = base.TopUpTests.run_topup

    def write_plan(self, keys):
        (self.store / order.PLAN).write_text(json.dumps({'order': keys}))

    def test_plan_priority_then_numeric_legacy_order(self):
        self.drafts(21)
        self.write_plan(['D3_P7', 'D1_P3', 'D2_P2'])
        random.Random(18).shuffle(self.buffer.posts['draft'])
        result = self.run_topup()
        self.assertEqual(result['added'], 10)
        self.assertEqual([edit['id'] for edit in self.buffer.edits], ['20', '2', '8', '0', '1', '3', '4', '5', '6', '7'])
        self.assertEqual(result['order_plan_missing'], [])

    def test_empty_plan_preserves_all_drafts_and_order(self):
        self.drafts(21)
        self.write_plan([])
        random.Random(1).shuffle(self.buffer.posts['draft'])
        result = self.run_topup()
        self.assertEqual([edit['id'] for edit in self.buffer.edits], list(map(str, range(10))))
        self.assertEqual(result['drafts_left'], 11)
        self.assertEqual(result['order_plan_missing'], [])

    def test_missing_plan_pin_is_explicit_and_drafts_are_not_discarded(self):
        self.drafts(2)
        self.write_plan(['V23_P1', 'D1_P2', 'D99_P7'])
        result = self.run_topup()
        self.assertEqual(result['order_plan_missing'], ['V23_P1', 'D99_P7'])
        self.assertEqual([edit['id'] for edit in self.buffer.edits], ['1', '0'])

    def test_scheduled_keys_are_present(self):
        self.drafts(2)
        self.write_plan(['D1_P1', 'D1_P2'])
        first = self.buffer.posts['draft'].pop(0)
        first.update(status='scheduled', dueAt=(base.NOW + timedelta(hours=1)).isoformat())
        self.buffer.posts['scheduled'].append(first)
        self.assertEqual(self.run_topup()['order_plan_missing'], [])
        self.assertEqual([edit['id'] for edit in self.buffer.edits], ['1'])

    def test_exhausted_plan_reports_missing_and_allows_new_inventory(self):
        self.drafts(2)
        self.write_plan(['D19_P1', 'V23_P1'])
        result = self.run_topup()
        self.assertEqual(result['order_plan_missing'], ['D19_P1', 'V23_P1'])
        self.assertEqual([edit['id'] for edit in self.buffer.edits], ['0', '1'])

    def test_dry_run_cli_uses_fake_buffer_only(self):
        self.drafts(3)
        self.write_plan(['D1_P3', 'V99_P1'])
        original_input = p.pin_input
        with patch.object(p, 'Buffer', return_value=self.buffer), patch.object(p.common, 'Dashboard', return_value=self.notify), patch.object(p, 'pin_input', side_effect=lambda post, root: original_input(post, self.pins)), redirect_stdout(io.StringIO()) as out:
            code = p.main(['--state-dir', str(self.store), '--now', base.NOW.isoformat(), '--dry-run'])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result['added'], 3)
        self.assertEqual(result['order_plan_missing'], ['V99_P1'])
        self.assertEqual(self.buffer.edits, [])
        self.assertEqual(self.notify.sent, [])
        self.assertFalse((self.store / p.STATE).exists())

    def test_partial_remote_failure_restarts_in_remaining_plan_order(self):
        self.drafts(21)
        self.write_plan(['D3_P7', 'D1_P3', 'D2_P2'])
        self.buffer.fail_after = 2
        result = self.run_topup()
        self.assertEqual(result['added'], 2)
        self.assertEqual(result['error'], 'buffer_unavailable')
        self.assertEqual([edit['id'] for edit in self.buffer.edits], ['20', '2'])
        self.buffer.fail_after = None
        result = self.run_topup()
        self.assertEqual(result['added'], 8)
        self.assertEqual(self.buffer.edits[2]['id'], '8')
        self.assertEqual(len({edit['id'] for edit in self.buffer.edits}), 10)

    def test_new_drafts_arrive_out_of_order_behind_remaining_plan(self):
        self.drafts(21)
        self.write_plan(['D3_P7', 'D3_P6', 'D3_P5', 'D3_P4'])
        first = p.TopUp(self.store, base.NOW, False, self.buffer, self.notify, self.pins, max_add=1).run()
        self.assertEqual(first['added'], 1)
        template = deepcopy(self.buffer.posts['draft'][0])
        template['id'] = 'new-early'
        template['metadata']['url'] = 'https://example.test/?utm_content=D1_P8'
        folder = self.pins / 'nap_1/D01_P8_pin'
        folder.mkdir()
        (folder / 'pin.txt').write_text((self.pins / 'nap_1/D01_P1_pin/pin.txt').read_text().replace('D1_P1', 'D1_P8'))
        self.buffer.posts['draft'].insert(0, template)
        random.Random(43).shuffle(self.buffer.posts['draft'])
        second = self.run_topup()
        self.assertEqual(second['added'], 9)
        self.assertEqual([edit['id'] for edit in self.buffer.edits[:4]], ['20', '19', '18', '17'])
        self.assertEqual([edit['id'] for edit in self.buffer.edits[4:]], ['0', '1', '2', '3', '4', '5'])

    def test_bad_plan_fails_before_fetch_and_any_edit(self):
        self.drafts(10)
        for raw in ['broken', '{"order":["D1_P1","D1_P1"]}', '{"order":[],"typo":[]}']:
            (self.store / order.PLAN).write_text(raw)
            self.assertEqual(self.run_topup()['error'], 'order_plan_invalid')
            self.assertEqual(self.buffer.fetches, [])
            self.assertEqual(self.buffer.edits, [])

    def test_malformed_duplicate_draft_identity_still_fails(self):
        self.drafts(2)
        self.write_plan(['D1_P2'])
        duplicate = deepcopy(self.buffer.posts['draft'][1])
        duplicate['id'] = 'duplicate'
        duplicate['metadata']['url'] += '_STRATEGY'
        self.buffer.posts['draft'].append(duplicate)
        self.assertEqual(self.run_topup()['error'], 'pin_metadata_invalid')
        self.assertEqual(self.buffer.edits, [])

    def test_missing_pin_file_whole_batch_preflight(self):
        self.drafts(10)
        self.write_plan(['D2_P3', 'D1_P1'])
        next(self.pins.glob('nap_1/D01_P1_*/pin.txt')).unlink()
        self.assertEqual(self.run_topup()['error'], 'pin_files_invalid')
        self.assertEqual(self.buffer.edits, [])

    def test_plan_dry_run_has_no_side_effects(self):
        self.drafts(21)
        self.write_plan(['D3_P7', 'D1_P1'])
        before = {str(path): path.read_bytes() for path in self.store.rglob('*') if path.is_file()}
        self.assertEqual(self.run_topup(dry=True)['added'], 10)
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.store.rglob('*') if path.is_file()})
        self.assertEqual(self.buffer.edits, [])
        self.assertEqual(self.notify.sent, [])

    def test_limits_zero_through_eleven_existing(self):
        self.drafts(30)
        self.write_plan(['D4_P7', 'D1_P1'])
        original = deepcopy(self.buffer.posts['draft'])
        for count in range(12):
            with self.subTest(existing=count):
                self.buffer.posts['draft'] = deepcopy(original)
                self.buffer.edits.clear()
                self.scheduled(count)
                result = self.run_topup(dry=False)
                self.assertEqual(result['added'], max(0, 10 - count))
                self.assertEqual(result['scheduled_count'], max(10, count))
                if count < 10:
                    self.assertEqual(self.buffer.edits[0]['id'], '27')

    def test_max_add_preflight_and_limit_remain_unchanged(self):
        self.drafts(21)
        self.write_plan(['D3_P7', 'D1_P1'])
        result = p.TopUp(self.store, base.NOW, False, self.buffer, self.notify, self.pins, max_add=1).run()
        self.assertEqual(result['added'], 1)
        self.assertTrue(result['max_add_limited'])
        self.assertEqual(self.buffer.edits[0]['id'], '20')

    def make_complete_inventory(self):
        for day in range(19, 27):
            for pin in range(1, 8):
                folder = self.pins / f'nap_{day}' / f'D{day:02d}_P{pin}_pin'
                folder.mkdir(parents=True)
                kind = 'D' if day < 23 else 'V'
                key = f'{kind}{day}_P{pin}'
                link = f'https://example.test/?utm_content={key}_S1'
                (folder / 'pin.txt').write_text(f'BOARD: AI Nutrition and Meal Tracking\nDESCRIPTION: Approved text\nALT TEXT: Alt\nTITLE: Title\nLINK: {link}\n')
                self.buffer.posts['draft'].append({'id': key, 'status': 'draft', 'metadata': {'url': link},
                                                   'assets': [{'source': 'https://media.example/asset', 'type': 'video' if kind == 'V' else 'image'}]})

    def test_all_eight_calendar_days_through_ten_slot_queue(self):
        self.make_complete_inventory()
        plan = order.generate([19, 20, 21, 22], [23, 24, 25, 26])
        self.write_plan(plan['order'])
        now = datetime(2026, 11, 14, 11, tzinfo=timezone.utc)  # 06 ET, before first slot
        rng = random.Random(85)
        with patch.object(p, 'video_thumbnail_offset', return_value=5850):
            for tick in range(8 * 24):
                self.buffer.posts['scheduled'] = [post for post in self.buffer.posts['scheduled'] if p.parse_due(post['dueAt']) > now]
                rng.shuffle(self.buffer.posts['draft'])
                result = self.run_topup(now=now)
                self.assertNotEqual(result['status'], 'error', result)
                self.assertLessEqual(len(self.buffer.posts['scheduled']), 10)
                now += timedelta(hours=1)
        self.assertEqual([edit['id'] for edit in self.buffer.edits], plan['order'])
        by_day = {}
        for edit in self.buffer.edits:
            due = p.parse_due(edit['dueAt']).astimezone(s.ET)
            by_day.setdefault(due.date(), []).append(edit)
        self.assertEqual(len(by_day), 8)
        locations = {}
        for day, edits in by_day.items():
            self.assertEqual([p.parse_due(edit['dueAt']).astimezone(s.ET).hour for edit in edits], list(s.SLOTS_ET))
            self.assertEqual(Counter(edit['id'][0] for edit in edits), {'D': 3 + ((day - min(by_day)).days % 2), 'V': 4 - ((day - min(by_day)).days % 2)})
            locations.update({edit['id']: day for edit in edits})
        for day in range(19, 23):
            for pin in range(1, 8):
                self.assertNotEqual(locations[f'D{day}_P{pin}'], locations[f'V{day + 4}_P{pin}'])

    def test_150_shuffles_suffix_normalization_and_unlisted_fallback(self):
        self.drafts(35)
        rng = random.Random(191)
        original = deepcopy(self.buffer.posts['draft'])
        keys = [order.label(p.pin_key(post)) for post in original]
        for _ in range(150):
            selected = rng.sample(keys, rng.randrange(36))
            self.write_plan(selected)
            self.buffer.posts['draft'] = deepcopy(original)
            rng.shuffle(self.buffer.posts['draft'])
            self.buffer.edits.clear()
            self.buffer.posts['scheduled'] = []
            for post in self.buffer.posts['draft']:
                post['metadata']['url'] += '_S1_T06'
            expected = selected + [key for key in keys if key not in selected]
            result = self.run_topup(dry=False)
            self.assertEqual(result['added'], 10)
            self.assertEqual([order.label(p.pin_key({'metadata': {'url': edit['metadata']['pinterest']['url']}})) for edit in self.buffer.edits], expected[:10])

    def test_100_absent_plan_differential_regressions(self):
        self.drafts(35)
        original = deepcopy(self.buffer.posts['draft'])
        rng = random.Random(2026)
        for trial in range(100):
            rng.shuffle(original)
            inventory = deepcopy(original[:rng.randrange(36)])
            self.scheduled(rng.randrange(12))
            scheduled = deepcopy(self.buffer.posts['scheduled'])
            now = base.NOW + timedelta(minutes=trial)
            max_add = rng.choice([None, 1, 3, 10, 20])
            results = []
            for impl in [legacy, p]:
                buffer = base.FakeBuffer()
                buffer.posts = {'draft': deepcopy(inventory), 'scheduled': deepcopy(scheduled)}
                with tempfile.TemporaryDirectory() as root:
                    runner = impl.TopUp(Path(root), now, False, buffer, base.Notify(), self.pins, max_add=max_add)
                    results.append((runner.run(), buffer.edits))
            self.assertEqual(results[0], results[1])


if __name__ == '__main__':
    unittest.main()

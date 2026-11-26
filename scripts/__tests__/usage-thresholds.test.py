#!/usr/bin/env python3
"""Synthetic quota snapshots and mock deliveries only. No live collector or API."""
import importlib.util
import io
import json
import random
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parent.parent
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result

base=module('fleet_boundary_fixtures',SCRIPTS/'__tests__/usage-fleet-monitor.test.py')
um,NOW=base.um,base.NOW
checker=module('claude_boundary_check',SCRIPTS/'usage-check.py')
codex=module('codex_boundary_check',SCRIPTS/'codex-usage-check.py')
gemini=module('gemini_boundary_check',SCRIPTS/'gemini-usage-check.py')

def setUpModule():
    global guards
    guards=[patch.object(socket.socket,'connect',side_effect=AssertionError('NETWORK_DISABLED')),
            patch.object(socket.socket,'connect_ex',side_effect=AssertionError('NETWORK_DISABLED')),
            patch.object(socket,'getaddrinfo',side_effect=AssertionError('NETWORK_DISABLED'))]
    for guard in guards:guard.start()
def tearDownModule():
    for guard in reversed(guards):guard.stop()


class ThresholdTests(unittest.TestCase):
    setUp=base.MonitorTests.setUp
    usage=base.MonitorTests.usage
    write=base.MonitorTests.write
    read=base.MonitorTests.read
    run_monitor=base.MonitorTests.run_monitor

    def safe_text(self,text):
        self.assertIn('Claude',text)
        self.assertTrue('5 órás' in text or 'heti' in text)
        for forbidden in ('\u2013','\u2014',' -- ','FAKE_SECRET','five_hour','seven_day'):
            self.assertNotIn(forbidden,text)

    def test_initial_threshold_cartesian_and_double_run_dedup(self):
        original=self.store
        for five in (0,89,89.999999,90,95,97,97.999999,98,99,100):
            for seven in (0,79,79.999999,80,85,89,89.999999,90,91,100):
                with self.subTest(five=five,seven=seven),tempfile.TemporaryDirectory() as directory:
                    self.store=Path(directory);self.transport=base.FakeTransport()
                    self.usage(five,seven);self.run_monitor()
                    level=90 if five>=98 or seven>=90 else 80 if five>=90 or seven>=80 else 0
                    self.assertEqual(self.read()['level'],level)
                    self.assertEqual(um.pause_is_active(self.store/um.PAUSE),level==90)
                    self.assertEqual(len(self.transport.sent),5 if level else 0)
                    for _,text in self.transport.sent:self.safe_text(text)
                    count=len(self.transport.sent);self.run_monitor()
                    self.assertEqual(len(self.transport.sent),count)
        self.store=original

    def test_five_hour_up_down_hysteresis_and_caution_continues(self):
        for value,level in ((89,0),(90,80),(97,80),(98,90),(99,90),(97,90),(90,90),(89.999,0)):
            self.usage(value,20);self.run_monitor()
            self.assertEqual(self.read()['level'],level)
            self.assertEqual(um.pause_is_active(self.store/um.PAUSE),level==90)
        warnings=[text for _,text in self.transport.sent if 'figyelmeztetés' in text]
        self.assertTrue(warnings);self.assertIn('munka folytatható',warnings[0])
        for _,text in self.transport.sent:self.safe_text(text)
        self.assertIn('feloldva',self.transport.sent[-1][1])
        self.assertIn('heti',self.transport.sent[-1][1])

    def test_weekly_unchanged_and_mixed_hold_requires_both_below(self):
        for five,seven,level in ((20,79,0),(20,80,80),(20,89,80),(20,90,90),
                                (20,91,90),(95,85,90),(89,80,90),(90,79,90),(89,79,0)):
            self.usage(five,seven);self.run_monitor();self.assertEqual(self.read()['level'],level)

    def test_mixed_95_85_from_idle_is_warning_not_pause(self):
        self.usage(95,85);self.run_monitor()
        self.assertEqual(self.read()['level'],80);self.assertFalse((self.store/um.PAUSE).exists())
        for _,text in self.transport.sent:
            self.assertIn('Claude 5 órás keret 95%',text);self.assertIn('Claude heti keret 85%',text)

    def test_legacy_level_90_is_a_phase_not_new_98_percent(self):
        self.write(um.STATE,{'level':90,'metric':'five_hour','percent':91,'source':'authoritative','since':42,
                             'notified_90':{'five_hour':91,'seven_day':90}})
        self.usage(95,79);self.run_monitor()
        self.assertEqual(self.read()['level'],90);self.assertTrue(um.pause_is_active(self.store/um.PAUSE))
        self.assertEqual(self.transport.sent,[])
        self.usage(98,79);self.run_monitor();self.assertEqual(len(self.transport.sent),5)
        self.assertEqual(self.read()['notified_90']['five_hour'],98)
        self.run_monitor();self.assertEqual(len(self.transport.sent),5)
        self.usage(89,79);self.run_monitor();self.assertIn('feloldva',self.transport.sent[-1][1])

    def test_legacy_warning_below_new_threshold_clears_silently(self):
        self.write(um.STATE,{'level':80,'metric':'five_hour','percent':85,'source':'authoritative','since':42})
        self.usage(85,79);self.run_monitor()
        self.assertEqual(self.read()['level'],0);self.assertEqual(self.transport.sent,[])

    def legacy_pending(self,kind='pause',attempts=2):
        level={'pause':90,'warning':80,'resume':0}[kind]
        event={'kind':kind,'target':{'level':level,'metric':'five_hour','percent':91,
               'source':'authoritative','since':42,'notified_90':{'five_hour':91}},
               'agent_text':'five_hour 91% old','telegram_text':'five_hour 91% old',
               'delivered':{'progi':'sent'}}
        self.write(um.STATE,{**um.empty_state(),'pending':event,
                            'telegram_pending':{'kind':kind,'text':'five_hour 91% old','attempts':attempts}})

    def test_legacy_pending_text_and_outbox_migrate_without_receipt_or_budget_reset(self):
        self.legacy_pending();self.usage(95,79);self.transport.fail={'telegram'}
        self.run_monitor();self.assertEqual(self.read()['level'],90)
        self.assertFalse(any(recipient=='progi' for recipient,_ in self.transport.attempted))
        self.assertNotIn('telegram_pending',self.read())
        self.assertEqual(len([r for r,_ in self.transport.attempted if r=='telegram']),1)
        for _,text in self.transport.attempted:self.safe_text(text)
        self.run_monitor();self.assertEqual(len(self.transport.attempted),4)

    def test_legacy_pending_pause_recovery_stale_then_safe_release(self):
        self.legacy_pending();self.usage(95,79,source='estimate')
        self.assertEqual(self.run_monitor(),'STALE silent')
        self.assertTrue(um.pause_is_active(self.store/um.PAUSE));self.assertEqual(self.transport.attempted,[])
        self.assertEqual(self.read()['telegram_pending']['attempts'],2)
        self.usage(89,79);self.run_monitor()
        self.assertEqual(self.read()['level'],0);self.assertFalse((self.store/um.PAUSE).exists())
        for _,text in self.transport.attempted:self.safe_text(text)

    def test_legacy_completed_resume_outbox_is_named_and_budget_preserved(self):
        self.write(um.STATE,{**um.empty_state(),'telegram_pending':{'kind':'resume','text':'old','attempts':2}})
        self.usage(89,79);self.transport.fail={'telegram'};self.run_monitor()
        self.assertEqual(len(self.transport.attempted),1);self.safe_text(self.transport.attempted[0][1])
        self.assertNotIn('telegram_pending',self.read())

    def test_weekly_five_point_refire_and_five_hour_cannot_refire_above_100(self):
        for five,seven,count in ((98,40,5),(100,40,5),(95,90,10),(95,94.999,10),
                                 (95,95,15),(95,91,15),(98,99.99,15),(99,100,20)):
            self.usage(five,seven);self.run_monitor();self.assertEqual(len(self.transport.sent),count)

    def test_partial_resume_reassert_at_new_hysteresis_boundary(self):
        self.usage(98,50);self.run_monitor();self.usage(89,79)
        self.transport.fail={'iris'};self.run_monitor();self.transport.fail.clear()
        self.usage(90,79);self.run_monitor()
        self.assertTrue(um.pause_is_active(self.store/um.PAUSE));self.assertEqual(self.read()['level'],90)
        self.assertIn('továbbra is',self.transport.sent[-1][1])
        for _,text in self.transport.sent:self.safe_text(text)

    def test_dry_run_legacy_text_migration_is_bytewise_read_only(self):
        self.legacy_pending();self.usage(95,79)
        before={p.name:p.read_bytes() for p in self.store.iterdir()}
        preview=self.run_monitor(dry=True)
        self.assertIn('Claude',preview);self.assertIn('5 órás',preview)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.store.iterdir()})
        self.assertEqual(self.transport.attempted,[])

    def test_weekly_differential_1000_steps_with_failures_against_original(self):
        old=module('weekly_legacy_reference',SCRIPTS/'__tests__/fixtures/usage-fleet-monitor-before-thresholds.py')
        original_store,original_transport=self.store,self.transport
        rng=random.Random(20261107)
        try:
            with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b:
                pairs=((old,Path(a),base.FakeTransport()),(um,Path(b),base.FakeTransport()))
                for i in range(1000):
                    weekly=rng.choice((0,79,79.999,80,85,89.999,90,90.001,94.999,95,99.999,100))
                    outcomes=[]
                    for implementation,store,transport in pairs:
                        self.store,self.transport=store,transport
                        transport.fail=({'iris'} if i%13==0 else set())|({'telegram'} if i%11==0 else set())
                        self.usage(20,weekly,source=um.SOURCES[i%3])
                        result=implementation.Monitor(store,NOW,False,transport).run()
                        state=self.read()
                        outcomes.append((result,state['level'],state.get('notified_90',{}),
                            implementation.pause_is_active(store/um.PAUSE),
                            [recipient for recipient,_ in transport.sent],
                            state.get('telegram_pending',{}).get('attempts')))
                    self.assertEqual(*outcomes,msg=f'weekly step {i}')
        finally:
            self.store,self.transport=original_store,original_transport


class ProviderCheckTests(unittest.TestCase):
    def claude(self,five,seven,raw=None):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'usage.json';now=datetime.now(timezone.utc)
            data=raw if raw is not None else {'generated_at':now.isoformat(),'claude':{'windows':{
                'five_hour':{'used_percent':five,'resets_at':now.timestamp()+3600},
                'seven_day':{'used_percent':seven,'resets_at':now.timestamp()+86400}}}}
            path.write_text(json.dumps(data))
            with patch.object(checker,'LATEST_PATH',str(path)),patch('sys.stdout',io.StringIO()) as out:
                code=checker.main()
            self.assertEqual(len(out.getvalue().splitlines()),1)
            self.assertIn('Claude',out.getvalue())
            return code,out.getvalue()

    def test_claude_checker_thresholds_and_mixed_statuses(self):
        for five,seven,status in ((89,79,'OK'),(90,79,'FIGYELMEZTETÉS'),(97,79,'FIGYELMEZTETÉS'),(98,79,'STOP'),
                                  (99,79,'STOP'),(0,79,'OK'),(0,80,'FIGYELMEZTETÉS'),(0,89,'FIGYELMEZTETÉS'),
                                  (0,90,'STOP'),(0,91,'STOP'),(95,85,'FIGYELMEZTETÉS')):
            code,text=self.claude(five,seven)
            self.assertTrue(text.rstrip().endswith(status));self.assertEqual(code,2 if status=='STOP' else 0)
            self.assertIn('5 órás',text);self.assertIn('heti',text)

    def test_claude_invalid_shapes_and_large_numbers_are_unknown_without_leak(self):
        for raw in ([],{'claude':['FAKE_SECRET']},{'claude':{'windows':['FAKE_SECRET']}},
                    {'generated_at':[], 'claude':{}}, {'generated_at':'FAKE_SECRET'}):
            code,text=self.claude(0,0,raw);self.assertEqual(code,3);self.assertNotIn('FAKE_SECRET',text)
        for value in (True,'FAKE_SECRET',{},[],10**500,float('nan'),float('inf'),-1,101):
            code,text=self.claude(value,0);self.assertEqual(code,3);self.assertNotIn('FAKE_SECRET',text)

    def test_codex_thresholds_unchanged_named_both_windows(self):
        for used,status in ((79,'OK'),(80,'FIGYELMEZTETÉS'),(89,'FIGYELMEZTETÉS'),(90,'STOP'),(98,'STOP')):
            data={'source':'codex-session-log','freshness':'fresh','observed_at':NOW.isoformat(),
                  'windows':{m:{'used_percent':used,'window_minutes':minutes,'resets_at':NOW.timestamp()+86400}
                             for m,minutes in (('five_hour',300),('seven_day',10080))}}
            line=codex.format_snapshot(data,NOW)
            self.assertIn(f'Codex 5 órás keret {used}% {status}',line)
            self.assertIn(f'Codex heti keret {used}% {status}',line)

    def test_gemini_only_weekly_and_other_group_is_not_claude_subscription(self):
        for used,status in ((79,'OK'),(80,'FIGYELMEZTETÉS'),(89,'FIGYELMEZTETÉS'),(90,'STOP'),(98,'STOP')):
            group={'used_pct':used,'remaining_pct':100-used,'resets_at':'2026-11-10T10:00:49Z'}
            line=gemini.format_snapshot({'collectedAt':gemini.collector.utc(NOW),'groups':{
                'gemini':group,'claude_gpt':group}},NOW)
            self.assertIn(f'Gemini heti keret {used}% {status}',line)
            self.assertIn('Gemini szolgáltatói Claude/GPT heti keret',line)
            self.assertNotIn('5 órás',line)


if __name__=='__main__':unittest.main()

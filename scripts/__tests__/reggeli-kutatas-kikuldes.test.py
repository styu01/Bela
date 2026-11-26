#!/usr/bin/env python3
"""Isolated stdlib tests. No live HTTP, agents, tokens or systemd activation."""
import configparser
import importlib.util
import io
import json
import multiprocessing
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock,patch

PATH=Path(__file__).resolve().parent.parent/'reggeli-kutatas-kikuldes.py'
spec=importlib.util.spec_from_file_location('morning_tested',PATH)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
NOW=m.common.parse_now('2026-11-05T05:30:00+01:00')
EXPECTED='''FELADAT: harom kutatasi szekcio a mai reggeli napindito Telegram-uzenetehez. A valasz TOMOR, strukturalt, harom kulon szekcio, egyertelmuen elvalasztva, forrasokkal (URL). Csak nyilvanos webes keresest hasznalj, ne irj fajlt, ne csinalj semmi mast.

1) AI HIREK: a tegnapi (2026-11-04) legfontosabb AI-hirek, roviden osszefoglalva, forrassal. Max 5 tetel.
2) WEBDESIGN UJDONSAGOK: keress a web design tools animations 2026 es beautiful website templates CSS animations temaban. Max 2-3 valodi, friss ujdonsag/eszkoz, ami vizualisan szep, animalt weboldalak gyartasat segiti. Mindegyikhez: nev, mit tud, link.
3) AI TIPPEK: keress a Claude Code tips tricks 2026 es ChatGPT productivity features 2026 temaban. Max 2-3 konkret, actionable tipp/trukk/funkcio.

Ha valamelyik szekciohoz nem talalsz friss, megbizhato anyagot, ird oda hogy nincs friss talalat, ne talalj ki semmit. Magyarul valaszolj.'''


class FakeClient:
    def __init__(self):
        self.sent=[]
        self.fail=False
        self.ident=123

    def send(self,text):
        if self.fail:
            raise m.Error('dashboard_unavailable')
        self.sent.append(text)
        return self.ident


def hold(path,ready,release):
    import fcntl
    with open(path,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        ready.set();release.wait(5)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Path(self.tmp.name)
        self.client=FakeClient()

    def run_dispatch(self,now=NOW,**kwargs):
        return m.Dispatcher(self.store,now,client=self.client,**kwargs).run()

    def state(self):
        return json.loads((self.store/m.STATE).read_text())

    def test_normal_send_exact_verbatim_template_and_receipt_state(self):
        result=self.run_dispatch()
        self.assertTrue(result['sent'])
        self.assertEqual(self.client.sent,[EXPECTED])
        self.assertEqual(self.state(),{'last_sent_date':'2026-11-05','message_id':123})
        self.assertEqual((self.store/m.STATE).stat().st_mode & 0o777,0o600)

    def test_same_budapest_day_second_run_is_quiet(self):
        self.run_dispatch()
        before=(self.store/m.STATE).read_bytes()
        result=self.run_dispatch(now=NOW+120)
        self.assertEqual(result['status'],'already_sent')
        self.assertEqual(self.client.sent,[EXPECTED])
        self.assertEqual((self.store/m.STATE).read_bytes(),before)

    def test_force_resends_and_updates_receipt(self):
        self.run_dispatch()
        self.client.ident=124
        self.assertTrue(self.run_dispatch(force=True)['sent'])
        self.assertEqual(self.client.sent,[EXPECTED,EXPECTED])
        self.assertEqual(self.state()['message_id'],124)

    def test_next_day_sends_new_date(self):
        self.run_dispatch()
        result=self.run_dispatch(now=m.common.parse_now('2026-11-06T05:30:00+01:00'))
        self.assertEqual(result['yesterday'],'2026-11-05')
        self.assertEqual(len(self.client.sent),2)
        self.assertIn('(2026-11-05)',self.client.sent[-1])

    def test_dashboard_failure_keeps_state_for_retry(self):
        (self.store/m.STATE).write_text(json.dumps({'last_sent_date':'2026-11-04','message_id':11}))
        before=(self.store/m.STATE).read_bytes()
        self.client.fail=True
        with self.assertRaisesRegex(m.Error,'dashboard_unavailable'):self.run_dispatch()
        self.assertEqual((self.store/m.STATE).read_bytes(),before)
        self.client.fail=False
        self.assertTrue(self.run_dispatch()['sent'])

    def test_dashboard_failure_first_run_creates_no_state(self):
        self.client.fail=True
        with self.assertRaises(m.Error):self.run_dispatch()
        self.assertFalse((self.store/m.STATE).exists())

    def test_missing_or_corrupt_state_is_recoverable(self):
        for text in ('broken','null','[]','{"last_sent_date":"2026-11-05"}',
                     '{"last_sent_date":"2026-02-30","message_id":1}',
                     '{"last_sent_date":"20261105","message_id":1}',
                     '{"last_sent_date":"2026-11-05","message_id":true}'):
            (self.store/m.STATE).write_text(text)
            self.assertTrue(self.run_dispatch()['sent'])
        self.assertEqual(len(self.client.sent),7)

    def test_dry_run_no_files_and_no_network(self):
        before={p.name:p.read_bytes() for p in self.store.iterdir()}
        result=self.run_dispatch(dry=True)
        self.assertEqual(result['status'],'dry_run')
        self.assertTrue(result['would_send'])
        self.assertEqual(result['content'],EXPECTED)
        self.assertEqual(result['to'],'codex/programozo')
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.store.iterdir()})
        self.assertEqual(self.client.sent,[])

    def test_dry_force_does_not_mutate_existing_state(self):
        self.run_dispatch()
        before={p.name:p.read_bytes() for p in self.store.iterdir()}
        result=self.run_dispatch(dry=True,force=True)
        self.assertTrue(result['would_send'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.store.iterdir()})
        self.assertEqual(len(self.client.sent),1)

    def test_template_contains_no_forbidden_dash_or_accents(self):
        self.assertTrue(EXPECTED.isascii())
        self.assertNotIn(' -- ',EXPECTED)
        self.assertNotIn('\u2014',EXPECTED)
        self.assertNotIn('\u2013',EXPECTED)
        self.assertEqual(m.TEMPLATE.count('{YESTERDAY}'),1)

    def test_year_end_and_leap_day(self):
        for local,yesterday in (('2027-01-01T05:30:00+01:00','2026-12-31'),
                                ('2028-03-01T05:30:00+01:00','2028-02-29'),
                                ('2028-02-29T05:30:00+01:00','2028-02-28'),
                                ('2027-03-01T05:30:00+01:00','2027-02-28')):
            now=m.common.parse_now(local)
            self.assertEqual(m.dates(now)[1],yesterday)

    def test_budapest_date_differs_from_utc_date_at_midnight(self):
        local,yesterday=m.dates(m.common.parse_now('2026-12-31T23:30:00Z'))
        self.assertEqual(local.date().isoformat(),'2027-01-01')
        self.assertEqual(yesterday,'2026-12-31')
        local,yesterday=m.dates(m.common.parse_now('2026-06-30T22:30:00Z'))
        self.assertEqual(local.date().isoformat(),'2026-07-01')
        self.assertEqual(yesterday,'2026-06-30')

    def test_dst_uses_calendar_day_not_subtraction_of_86400_seconds(self):
        for local,expected in (('2026-03-30T00:30:00+02:00','2026-03-29'),
                               ('2026-10-25T02:30:00+02:00','2026-10-24'),
                               ('2026-10-25T02:30:00+01:00','2026-10-24'),
                               ('2026-10-26T00:30:00+01:00','2026-10-25')):
            self.assertEqual(m.dates(m.common.parse_now(local))[1],expected)

    def test_six_am_deadline_even_with_force_and_dry_run(self):
        late=m.common.parse_now('2026-11-05T06:00:00+01:00')
        for kwargs in ({},{'force':True},{'dry':True},{'dry':True,'force':True}):
            result=self.run_dispatch(now=late,**kwargs)
            self.assertEqual(result['status'],'expired')
        self.assertFalse((self.store/m.STATE).exists())
        self.assertEqual(self.client.sent,[])

    def test_before_six_am_is_allowed(self):
        now=m.common.parse_now('2026-11-05T05:59:59+01:00')
        self.assertTrue(self.run_dispatch(now=now)['sent'])

    def test_future_state_fails_closed_even_with_force(self):
        state={'last_sent_date':'2026-11-06','message_id':111}
        (self.store/m.STATE).write_text(json.dumps(state))
        for force in (False,True):
            with self.assertRaisesRegex(m.Error,'clock_rollback'):self.run_dispatch(force=force)
        self.assertEqual(self.state(),state)
        self.assertEqual(self.client.sent,[])

    def test_noninteger_or_boolean_send_receipt_does_not_commit(self):
        for ident in (True,False,0,-1,'123',1.5,None,{},m.MAX_ID+1):
            self.client.ident=ident
            with self.assertRaisesRegex(m.Error,'dashboard_invalid_response'):self.run_dispatch()
            self.assertFalse((self.store/m.STATE).exists())

    def test_atomic_replace_failure_preserves_old_state_and_removes_temporary_file(self):
        old={'last_sent_date':'2026-11-04','message_id':111}
        (self.store/m.STATE).write_text(json.dumps(old))
        with patch.object(m.common.os,'replace',side_effect=OSError('SECRET DISK FULL')):
            with self.assertRaisesRegex(m.Error,'state_write_failed'):self.run_dispatch()
        self.assertEqual(self.state(),old)
        self.assertEqual(list(self.store.glob('*.tmp')),[])
        self.assertEqual(len(self.client.sent),1)
        # No server-side idempotency: acceptance before failed local save can repeat.
        self.run_dispatch()
        self.assertEqual(len(self.client.sent),2)

    def test_state_read_permission_error_is_not_corrupt_state(self):
        with patch.object(m.Path,'read_text',side_effect=PermissionError('SECRET')):
            with self.assertRaisesRegex(m.Error,'state_read_failed'):self.run_dispatch()
        self.assertEqual(self.client.sent,[])

    def test_lock_with_real_other_process(self):
        ctx=multiprocessing.get_context('fork');ready,release=ctx.Event(),ctx.Event()
        child=ctx.Process(target=hold,args=(self.store/'.reggeli-kutatas.lock',ready,release))
        child.start()
        try:
            self.assertTrue(ready.wait(2))
            self.assertEqual(self.run_dispatch()['status'],'busy')
            self.assertEqual(self.client.sent,[])
        finally:
            release.set();child.join(3)
            if child.is_alive():child.terminate();child.join()
        self.assertEqual(child.exitcode,0)
        self.assertTrue(self.run_dispatch()['sent'])

    def test_two_actual_dispatch_processes_send_once(self):
        ctx=multiprocessing.get_context('fork');barrier=ctx.Barrier(3);queue=ctx.Queue();counter=ctx.Value('i',0)
        def worker():
            class Counting(FakeClient):
                def send(self,text):
                    with counter.get_lock():counter.value+=1
                    time.sleep(0.05)
                    return 123
            barrier.wait(3)
            queue.put(m.Dispatcher(self.store,NOW,client=Counting()).run())
        children=[ctx.Process(target=worker) for _ in range(2)]
        for child in children:child.start()
        try:
            barrier.wait(3)
            for child in children:child.join(5)
            self.assertTrue(all(child.exitcode==0 for child in children))
            results=[queue.get(timeout=2) for child in children]
            self.assertEqual(sum(bool(r['sent']) for r in results),1)
            self.assertEqual(counter.value,1)
        finally:
            for child in children:
                if child.is_alive():child.terminate();child.join()

    def test_symlink_lock_is_rejected_without_modifying_target(self):
        target=self.store/'target';target.write_text('untouched')
        (self.store/'.reggeli-kutatas.lock').symlink_to(target)
        with self.assertRaisesRegex(m.Error,'lock_failed'):self.run_dispatch()
        self.assertEqual(target.read_text(),'untouched')
        self.assertEqual(self.client.sent,[])

    def test_cli_error_is_one_json_line_plus_fixed_secret_free_stderr(self):
        self.client.fail=True
        with patch.object(m,'Dashboard',return_value=self.client),patch('sys.stdout',io.StringIO()) as out, \
             patch('sys.stderr',io.StringIO()) as err:
            status=m.main(['--state-dir',str(self.store),'--now',str(NOW)])
        self.assertEqual(status,1)
        self.assertEqual(len(out.getvalue().splitlines()),1)
        self.assertEqual(len(err.getvalue().splitlines()),1)
        self.assertEqual(json.loads(out.getvalue())['error'],'dashboard_unavailable')
        self.assertFalse((self.store/m.STATE).exists())

    def test_raw_exception_cannot_leak_secret(self):
        client=Mock();client.send.side_effect=RuntimeError('Authorization Bearer SECRET https://private')
        with patch.object(m,'Dashboard',return_value=client),patch('sys.stdout',io.StringIO()) as out, \
             patch('sys.stderr',io.StringIO()) as err:
            self.assertEqual(m.main(['--state-dir',str(self.store),'--now',str(NOW)]),1)
        text=out.getvalue()+err.getvalue()
        self.assertIn('dispatch_failed',text)
        for secret in ('SECRET','Authorization','https://private'):self.assertNotIn(secret,text)

    def test_invalid_now_cli_has_no_side_effects(self):
        for invalid in ('nan','inf','-1','2026-11-05T05:30:00','nonsense'):
            with patch('sys.stdout',io.StringIO()) as out,patch('sys.stderr',io.StringIO()):
                status=m.main(['--state-dir',str(self.store),'--now',invalid])
            self.assertEqual(status,1)
            self.assertEqual(json.loads(out.getvalue())['error'],'invalid_now')
        self.assertEqual(list(self.store.iterdir()),[])

    def test_default_paths_do_not_depend_on_cwd_or_path(self):
        repo=self.store/'repo';scripts=repo/'scripts';scripts.mkdir(parents=True)
        shutil.copyfile(PATH,scripts/PATH.name)
        shutil.copyfile(PATH.parent/'kanban-audit.py',scripts/'kanban-audit.py')
        for cwd in (repo,self.store):
            result=subprocess.run(['/usr/bin/python3',str(scripts/PATH.name),'--now',str(NOW),'--dry-run'],
                cwd=cwd,env={**os.environ,'PATH':''},capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['content'],EXPECTED)
        self.assertFalse((repo/'store').exists())
        self.assertFalse((scripts/'__pycache__').exists())

    def test_real_clock_is_sampled_inside_run_after_lock(self):
        dispatcher=m.Dispatcher(self.store,client=self.client)
        with patch.object(m.common,'parse_now',return_value=m.common.parse_now('2026-11-05T06:01:00+01:00')) as clock:
            self.assertEqual(dispatcher.run()['status'],'expired')
        clock.assert_called_once_with(None)
        self.assertEqual(self.client.sent,[])

    def test_timer_and_service_policy(self):
        units=PATH.parent/'systemd'
        parser=configparser.ConfigParser(interpolation=None)
        parser.read(units/'reggeli-kutatas-kikuldes.service')
        self.assertEqual(parser['Service']['Type'],'oneshot')
        self.assertEqual(parser['Service']['Restart'],'on-failure')
        self.assertEqual(parser['Service'].getint('RestartSec'),120)
        self.assertEqual(parser['Unit'].getint('StartLimitIntervalSec'),900)
        self.assertEqual(parser['Unit'].getint('StartLimitBurst'),3)
        self.assertTrue(parser['Service']['ExecStart'].startswith('/usr/bin/python3 '))
        timer=configparser.ConfigParser(interpolation=None);timer.read(units/'reggeli-kutatas-kikuldes.timer')
        self.assertFalse(timer['Timer'].getboolean('Persistent'))
        self.assertEqual(timer['Timer']['OnCalendar'],'*-*-* 05:30:00 Europe/Budapest')

    def test_stress_live_clock_is_rechecked_before_post_after_slow_state_io(self):
        dispatcher=m.Dispatcher(self.store,client=self.client)
        late=m.common.parse_now('2026-11-05T06:01:00+01:00')
        with patch.object(m.common,'parse_now',side_effect=[NOW,late]) as clock:
            result=dispatcher.run()
        self.assertEqual(clock.call_count,2)
        self.assertEqual(result['status'],'expired')
        self.assertEqual(self.client.sent,[])
        self.assertFalse((self.store/m.STATE).exists())

    def test_stress_suspension_to_next_day_does_not_send_stale_prompt(self):
        dispatcher=m.Dispatcher(self.store,client=self.client)
        next_day=m.common.parse_now('2026-11-06T05:30:00+01:00')
        with patch.object(m.common,'parse_now',side_effect=[NOW,next_day]):
            result=dispatcher.run()
        self.assertEqual(result['status'],'expired')
        self.assertEqual(result['date'],'2026-11-06')
        self.assertEqual(self.client.sent,[])

    def test_stress_live_clock_valid_window_still_sends_once(self):
        dispatcher=m.Dispatcher(self.store,client=self.client)
        with patch.object(m.common,'parse_now',side_effect=[NOW,NOW+1]):
            self.assertTrue(dispatcher.run()['sent'])
        self.assertEqual(self.client.sent,[EXPECTED])

    def test_stress_deeply_nested_state_is_treated_as_corrupt(self):
        (self.store/m.STATE).write_text('['*2000+']'*2000)
        self.assertTrue(self.run_dispatch()['sent'])
        self.assertEqual(self.state()['message_id'],123)

    def test_stress_fifo_lock_does_not_block_or_send(self):
        os.mkfifo(self.store/'.reggeli-kutatas.lock')
        ctx=multiprocessing.get_context('fork');queue=ctx.Queue()
        def run():
            try:self.run_dispatch();queue.put('unexpected')
            except m.Error as exc:queue.put(str(exc))
        child=ctx.Process(target=run);child.start();child.join(3)
        if child.is_alive():child.terminate();child.join();self.fail('FIFO blocked')
        self.assertEqual(child.exitcode,0)
        self.assertEqual(queue.get(timeout=2),'lock_failed')
        self.assertEqual(self.client.sent,[])

    def test_stress_final_directory_fsync_failure_leaves_valid_state_and_no_extra_send(self):
        original=m.common.os.fsync;calls=[]
        def fail_second(fd):
            calls.append(1)
            if len(calls)==2:raise OSError('SECRET disk')
            return original(fd)
        with patch.object(m.common.os,'fsync',side_effect=fail_second):
            with self.assertRaisesRegex(m.Error,'state_write_failed'):self.run_dispatch()
        self.assertEqual(self.state()['message_id'],123)
        self.assertEqual(self.run_dispatch()['status'],'already_sent')
        self.assertEqual(len(self.client.sent),1)

    def test_stress_hourly_repeated_runs_in_morning_do_not_repeat(self):
        for n in range(100):
            self.run_dispatch(now=NOW+n)
        self.assertEqual(self.client.sent,[EXPECTED])

    def test_stress_wrong_date_types_and_receipts_do_not_forge_dedup(self):
        for value in ({'last_sent_date':123,'message_id':1},
                      {'last_sent_date':'2026-11-05','message_id':0},
                      {'last_sent_date':'2026-11-05','message_id':'123'},
                      {'last_sent_date':'2026-11-05','message_id':m.MAX_ID+1}):
            (self.store/m.STATE).write_text(json.dumps(value))
            self.assertTrue(self.run_dispatch()['sent'])
        self.assertEqual(len(self.client.sent),4)

    def test_stress_late_cli_does_not_read_credentials_or_retry(self):
        with patch.object(m.Dashboard,'send',side_effect=AssertionError('no send')), \
             patch('sys.stdout',io.StringIO()) as out,patch('sys.stderr',io.StringIO()) as err:
            status=m.main(['--state-dir',str(self.store),'--now','2026-11-05T06:00:00+01:00','--force'])
        self.assertEqual(status,0)
        self.assertEqual(json.loads(out.getvalue())['status'],'expired')
        self.assertEqual(err.getvalue(),'')
        self.assertFalse((self.store/m.STATE).exists())

    def test_stress_service_rate_limit_contains_all_three_timed_attempts(self):
        parser=configparser.ConfigParser(interpolation=None)
        parser.read(PATH.parent/'systemd/reggeli-kutatas-kikuldes.service')
        service,unit=parser['Service'],parser['Unit']
        self.assertGreater(unit.getint('StartLimitIntervalSec'),
                           3*(service.getint('RestartSec')+service.getint('TimeoutStartSec')))



class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Path(self.tmp.name);(self.store/'.dashboard-token').write_text('isolated-secret-token')
        self.client=m.Dashboard(self.store,'http://localhost:3420')

    def response(self,body=b'{"id":123}',status=200):
        response=Mock();response.status=status;response.read.return_value=body
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        return response

    def test_single_post_has_correct_recipient_exact_content_and_bearer(self):
        self.client.opener.open=Mock(return_value=self.response())
        self.assertEqual(self.client.send(EXPECTED),123)
        request=self.client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(),'POST')
        self.assertEqual(request.full_url,'http://localhost:3420/api/messages')
        self.assertEqual(json.loads(request.data),{'from':'bela','to':'codex/programozo','content':EXPECTED})
        self.assertEqual(request.get_header('Authorization'),'Bearer isolated-secret-token')
        self.assertEqual(self.client.opener.open.call_count,1)
        self.assertEqual(self.client.opener.open.call_args.kwargs['timeout'],10)

    def test_http_error_and_network_exception_are_fixed_codes(self):
        self.client.opener.open=Mock(return_value=self.response(status=503))
        with self.assertRaisesRegex(m.Error,'dashboard_http_error'):self.client.send(EXPECTED)
        self.client.opener.open=Mock(side_effect=OSError('SECRET'))
        with self.assertRaisesRegex(m.Error,'dashboard_unavailable'):self.client.send(EXPECTED)

    def test_only_positive_integer_receipt_is_accepted(self):
        for payload in ({'id':True},{'id':0},{'id':-1},{'id':'123'},{'id':1.0},{'id':{}},[],{},None):
            self.client.opener.open=Mock(return_value=self.response(json.dumps(payload).encode()))
            with self.assertRaisesRegex(m.Error,'dashboard_invalid_response'):self.client.send(EXPECTED)

    def test_invalid_json_and_oversize_response(self):
        for raw in (b'not JSON',b'\xff',b'x'*65537,b'['*2000+b']'*2000):
            self.client.opener.open=Mock(return_value=self.response(raw))
            with self.assertRaisesRegex(m.Error,'dashboard_invalid_response'):self.client.send(EXPECTED)

    def test_bad_or_missing_credentials_never_contact_dashboard(self):
        for token in ('','secret\ninjected','áé','secret\x00'):
            (self.store/'.dashboard-token').write_text(token)
            self.client.opener.open=Mock()
            with self.assertRaisesRegex(m.Error,'dashboard_credentials_unavailable'):self.client.send(EXPECTED)
            self.client.opener.open.assert_not_called()
        (self.store/'.dashboard-token').unlink()
        with self.assertRaises(m.Error):self.client.send(EXPECTED)

    def test_redirect_and_external_dashboard_rejected(self):
        self.assertIsNone(m.common.NoRedirect().redirect_request(None,None,302,'Found',{},'http://evil'))
        for url in ('http://evil.example','http://localhost.evil','http://user:secret@localhost:3420',
                    'http://localhost:3420?token=secret'):
            with self.assertRaises(m.Error):m.Dashboard(self.store,url)

    def test_proxy_environment_does_not_enable_proxy(self):
        with patch.dict(os.environ,{'http_proxy':'http://evil.example'}):
            client=m.Dashboard(self.store,'http://localhost:3420')
        self.assertFalse(any(isinstance(h,m.urllib.request.ProxyHandler) and h.proxies for h in client.opener.handlers))


if __name__=='__main__':unittest.main()

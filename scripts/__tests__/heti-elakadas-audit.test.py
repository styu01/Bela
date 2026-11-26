#!/usr/bin/env python3
"""Temporary SQLite/log/state and mocked HTTP only. Never touches the live fleet."""
import importlib.util
import io
import json
import multiprocessing
import os
import random
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock,patch

PATH = Path(__file__).resolve().parent.parent/'heti-elakadas-audit.py'
spec = importlib.util.spec_from_file_location('weekly_audit_tested',PATH)
wa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wa)
NOW = 1793854800
SCHEMA = '''
CREATE TABLE pending_task_retries(id INTEGER PRIMARY KEY AUTOINCREMENT,
 task_name TEXT NOT NULL,agent_name TEXT NOT NULL,first_attempt INTEGER NOT NULL,
 last_attempt INTEGER NOT NULL,attempt_count INTEGER NOT NULL DEFAULT 1,last_reason TEXT,
 alert_sent_at INTEGER,owner_alert_sent_at INTEGER,UNIQUE(task_name,agent_name));
CREATE TABLE kanban_cards(id TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT,status TEXT NOT NULL DEFAULT 'planned'
 CHECK(status IN ('planned','in_progress','testing','waiting','done')),assignee TEXT,priority TEXT NOT NULL DEFAULT 'normal',
 project TEXT,due_date INTEGER,sort_order REAL NOT NULL DEFAULT 0,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
 archived_at INTEGER,parent_id TEXT,dispatched_at INTEGER,is_recurring_template INTEGER NOT NULL DEFAULT 0);
'''


def agents():
    return [{'name':name,'running':True,'needsReauth':False} for name in wa.DEFAULT_ALWAYS_RUNNING]


class FakeClient:
    def __init__(self):
        self.rows=agents()
        self.sent=[]
        self.agent_calls=0
        self.fail_get=False
        self.fail_send=False

    def agents(self):
        self.agent_calls+=1
        if self.fail_get:
            raise wa.AuditError('agents_unavailable')
        return self.rows

    def send(self,text):
        if self.fail_send:
            raise RuntimeError('SECRET dashboard-token https://botSECRET')
        self.sent.append(text)
        return True


def hold(path,ready,release):
    import fcntl
    with open(path,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        ready.set()
        release.wait(5)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Path(self.tmp.name)
        self.db=self.store/'claudeclaw.db'
        self.log=self.store/'dashboard.log'
        self.log.write_bytes(b'')
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executescript(SCHEMA)
        self.client=FakeClient()

    def sql(self,query,args=()):
        with closing(sqlite3.connect(self.db)) as connection,connection:
            return connection.execute(query,args).fetchall()

    def retry(self,count=51,age=0,name='task'):
        self.sql('INSERT INTO pending_task_retries(task_name,agent_name,first_attempt,last_attempt,attempt_count) VALUES(?,?,?,?,?)',
                 (name,'progi',(NOW-wa.DAY)*1000,(NOW-age)*1000,count))

    def card(self,ident='alpha',title='WP admin felület beállítás jóváhagyás',description='',assignee=None,status='waiting',archived=None):
        self.sql('INSERT INTO kanban_cards(id,title,description,assignee,status,created_at,updated_at,archived_at) VALUES(?,?,?,?,?,?,?,?)',
                 (ident,title,description,assignee,status,NOW-wa.DAY,NOW-wa.DAY,archived))

    def audit(self,now=NOW,dry=False,**kwargs):
        return wa.Audit(self.store,now,dry=dry,client=self.client,**kwargs).run()

    def state(self):
        return json.loads((self.store/wa.STATE).read_text())

    def append(self,data):
        with self.log.open('ab') as stream:
            stream.write(data)

    def test_clean_first_and_next_run_are_silent(self):
        result=self.audit()
        self.assertFalse(result['notification_needed'])
        self.assertEqual(result['log_change'],'initialized')
        self.assertEqual(self.client.sent,[])
        self.assertEqual(self.audit(now=NOW+7*wa.DAY)['status'],'complete')
        self.assertEqual(self.client.sent,[])

    def test_attempt_threshold_strictly_above_fifty(self):
        self.retry(count=50,name='boundary')
        self.retry(count=51,name='over')
        result=self.audit()
        self.assertEqual(result['retry_count'],1)
        self.assertEqual(len(self.client.sent),1)
        self.assertIn('probak=51',self.client.sent[0])

    def test_retry_age_strictly_more_than_two_hours(self):
        self.retry(count=1,age=7200,name='boundary')
        self.retry(count=1,age=7201,name='older')
        self.assertEqual(self.audit()['retry_count'],1)

    def test_unowned_alone_nineteen_is_quiet(self):
        for n in range(19):
            self.card(str(n),title='Folyamatos projekt')
        self.assertEqual(self.audit()['hygiene_count'],0)
        self.assertEqual(self.client.sent,[])

    def test_unassigned_title_and_description_keyword_detection(self):
        self.card('one')
        self.card('two',title='Projekt',description='Belépés engedély szükséges')
        self.card('three',title='Folyamatos projekt')
        self.assertEqual(self.audit()['hygiene_count'],2)
        self.assertIn('emberi ertelemmel',self.client.sent[0])
        self.assertEqual(self.sql('SELECT count(*) FROM kanban_cards WHERE assignee IS NULL')[0][0],3)

    def test_admin_keyword_title_only_for_configured_subagent(self):
        self.card('one',title='WP beállítás',assignee='progi')
        self.card('two',title='Jóváhagyás',assignee='istvan')
        self.card('three',title='Projekt',description='jóváhagyás',assignee='iris')
        self.card('four',title='Admin-felület',assignee='bela')
        self.assertEqual(self.audit()['hygiene_count'],1)

    def test_done_and_archived_hygiene_excluded(self):
        self.card('done',status='done')
        self.card('archived',archived=NOW-1)
        self.assertEqual(self.audit()['hygiene_count'],0)

    def test_hygiene_twenty_eight_day_gate_exact_boundary(self):
        self.card()
        self.audit()
        self.assertEqual(len(self.client.sent),1)
        for delta in (7*wa.DAY,28*wa.DAY-1):
            self.assertEqual(self.audit(now=NOW+delta)['hygiene_count'],0)
        self.assertEqual(len(self.client.sent),1)
        self.assertEqual(self.audit(now=NOW+28*wa.DAY)['hygiene_count'],1)
        self.assertEqual(len(self.client.sent),2)

    def test_first_log_scan_only_baselines_old_large_count(self):
        payload=b'[01:02:03.456] Stuck-input\n'*4000
        self.log.write_bytes(payload)
        result=self.audit()
        self.assertEqual(result['log_delta'],0)
        self.assertEqual(result['log_bytes_read'],0)
        self.assertEqual(self.state()['log']['offset'],len(payload))
        self.assertEqual(self.client.sent,[])

    def test_log_growth_threshold_and_cumulative_count(self):
        self.audit()
        self.append(b'Stuck-input\n'*30000)
        result=self.audit(now=NOW+1)
        self.assertEqual(result['log_delta'],30000)
        self.assertEqual(self.client.sent,[])
        self.append(b'Schedule target session busy\n'*30001)
        result=self.audit(now=NOW+2)
        self.assertEqual(result['log_delta'],30001)
        self.assertEqual(result['log_total'],60001)
        self.assertEqual(len(self.client.sent),1)

    def test_log_ansi_and_two_markers_count_once_per_line(self):
        self.audit()
        self.append(b'\x1b[31mStuck-\x1b[0minput and Schedule target session busy\x1b[0m\n')
        result=self.audit(now=NOW+1)
        self.assertEqual(result['log_delta'],1)

    def test_log_rotation_reads_new_file_from_start(self):
        self.audit()
        old=self.state()['log']['inode']
        self.log.rename(self.store/'dashboard.log.1')
        self.log.write_bytes(b'Stuck-input\n')
        result=self.audit(now=NOW+1)
        self.assertEqual(result['log_change'],'rotated')
        self.assertEqual(result['log_delta'],1)
        self.assertNotEqual(self.state()['log']['inode'],old)
        self.assertEqual(self.state()['log']['rotations'],1)

    def test_log_truncation_reads_from_start(self):
        self.log.write_bytes(b'old data\n'*100)
        self.audit()
        self.log.write_bytes(b'Stuck-input\n')
        result=self.audit(now=NOW+1)
        self.assertEqual(result['log_change'],'truncated')
        self.assertEqual(result['log_delta'],1)

    def test_copytruncate_regrown_beyond_offset_is_detected_by_anchor(self):
        self.log.write_bytes(b'old data\n'*10)
        self.audit()
        self.log.write_bytes(b'Stuck-input\n'*100)
        result=self.audit(now=NOW+1)
        self.assertEqual(result['log_change'],'rewritten')
        self.assertEqual(result['log_delta'],100)

    def test_partial_line_and_keyword_span_runs_without_raw_text_state(self):
        self.audit()
        for n,payload in enumerate((b'Stu',b'ck-input SECRET-TOKEN',b'\n')):
            self.append(payload)
            result=self.audit(now=NOW+n+1)
            self.assertEqual(result['log_delta'],1 if n==2 else 0)
            self.assertNotIn('SECRET',json.dumps(self.state()))
        self.assertEqual(self.state()['log']['total_matches'],1)

    def test_streaming_ansi_sequences_span_chunks_and_runs(self):
        for pieces in ((b'Stuck-\x1b[',b'31minput\n'),
                       (b'\x1b]0;SECRET\x1b',b'\\Stuck-input\n'),
                       (b'\x1b]SECRET',b'\x07Schedule target session busy\n')):
            carry=wa.stream_state()
            self.assertEqual(sum(wa.consume(piece,carry) for piece in pieces),1)
            self.assertNotIn('SECRET',json.dumps(carry))

    def test_invalid_utf8_bytes_do_not_break_log_scanning(self):
        self.audit()
        self.append(b'\xff\xfeStuck-input\x80\nnormal\xff\n')
        self.assertEqual(self.audit(now=NOW+1)['log_delta'],1)

    def test_needs_reauth_any_agent_but_bela(self):
        next(r for r in self.client.rows if r['name']=='vizsla').update(running=False,needsReauth=True)
        self.client.rows += [{'name':'bela','running':False,'needsReauth':True}]
        result=self.audit()
        self.assertEqual(result['agent_issue_count'],2)
        self.assertIn('vizsla: ujra belepes kell',self.client.sent[0])

    def test_running_false_required_vs_on_demand(self):
        self.client.rows[0]['running']=False
        for name in ('autobot','usage-tracker'):
            self.client.rows.append({'name':name,'running':False,'needsReauth':False})
        result=self.audit()
        self.assertEqual(result['agent_issue_count'],1)
        self.assertIn('progi: nem fut',self.client.sent[0])

    def test_missing_required_agent_is_observable(self):
        self.client.rows=self.client.rows[1:]
        self.assertEqual(self.audit()['agent_issue_count'],1)
        self.assertIn('hianyzik',self.client.sent[0])

    def test_api_failure_is_error_and_best_effort_fixed_notification(self):
        self.client.fail_get=True
        result=self.audit()
        self.assertEqual(result['error'],'agents_unavailable')
        self.assertTrue(result['error_notification_sent'])
        self.assertEqual(self.client.sent,['[HETI-AUDIT] a szkript hibaval allt le: agents_unavailable'])
        self.assertFalse((self.store/wa.STATE).exists())

    def test_database_failure_is_error_with_best_effort_notification(self):
        self.db.unlink()
        result=self.audit()
        self.assertEqual(result['error'],'database_read_failed')
        self.assertTrue(result['error_notification_sent'])
        self.assertFalse(self.db.exists())

    def test_missing_log_is_not_a_clean_audit(self):
        self.log.unlink()
        result=self.audit()
        self.assertEqual(result['error'],'log_read_failed')
        self.assertTrue(result['error_notification_sent'])
        self.assertFalse((self.store/wa.STATE).exists())

    def test_failed_notification_preserves_every_cursor_and_report_for_retry(self):
        self.audit()
        before=(self.store/wa.STATE).read_bytes()
        self.card()
        self.append(b'Stuck-input\n')
        self.client.fail_send=True
        result=self.audit(now=NOW+1)
        self.assertEqual(result['status'],'error')
        self.assertEqual((self.store/wa.STATE).read_bytes(),before)
        self.client.fail_send=False
        result=self.audit(now=NOW+1)
        self.assertEqual(result['hygiene_count'],1)
        self.assertEqual(result['log_delta'],1)
        self.assertTrue(result['sent'])

    def test_dry_run_reads_agents_but_never_posts_or_writes(self):
        self.card()
        self.client.rows[0]['running']=False
        before={p.name:p.read_bytes() for p in self.store.iterdir()}
        result=self.audit(dry=True)
        self.assertEqual(result['status'],'dry_run')
        self.assertTrue(result['notification_needed'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.store.iterdir()})
        self.assertEqual(self.client.sent,[])
        self.assertEqual(self.client.agent_calls,1)

    def test_dry_run_error_does_not_post_best_effort(self):
        self.log.unlink()
        result=self.audit(dry=True)
        self.assertEqual(result['status'],'error')
        self.assertEqual(self.client.sent,[])
        self.assertFalse((self.store/'.heti-elakadas-audit.lock').exists())

    def test_outgoing_message_is_ascii_without_forbidden_dashes(self):
        self.card(title='István — jóváhagyás – valami --\n\x1b')
        self.retry(name='áé --\nvalami')
        self.audit()
        text=self.client.sent[0]
        self.assertTrue(text.isascii())
        self.assertTrue(text.startswith('[HETI-AUDIT]'))
        for forbidden in ('—','–',' -- ','\x1b'):
            self.assertNotIn(forbidden,text)

    def test_whole_database_bytes_unchanged(self):
        self.card()
        self.retry()
        before=self.db.read_bytes()
        self.audit()
        self.assertEqual(self.db.read_bytes(),before)
        self.assertIsNone(self.sql('SELECT assignee FROM kanban_cards')[0][0])

    def test_duplicate_run_and_clock_rollback(self):
        self.retry()
        self.audit()
        self.assertEqual(self.audit()['status'],'already_audited')
        self.assertEqual(len(self.client.sent),1)
        self.assertEqual(self.audit(now=NOW-1)['error'],'clock_rollback')

    def test_corrupt_state_starts_new_log_baseline(self):
        (self.store/wa.STATE).write_text('corrupt')
        self.log.write_bytes(b'Stuck-input\n'*4000)
        self.assertEqual(self.audit()['log_delta'],0)
        self.assertEqual(self.client.sent,[])

    def test_corrupt_offset_fails_visibly_instead_of_skipping_log(self):
        self.audit()
        state=self.state()
        state['log']['offset']=-1
        (self.store/wa.STATE).write_text(json.dumps(state))
        self.assertEqual(self.audit(now=NOW+1)['error'],'state_read_failed')

    def test_real_process_lock_prevents_double_audit(self):
        self.retry()
        ctx=multiprocessing.get_context('fork')
        ready,release=ctx.Event(),ctx.Event()
        child=ctx.Process(target=hold,args=(self.store/'.heti-elakadas-audit.lock',ready,release))
        child.start()
        try:
            self.assertTrue(ready.wait(2))
            self.assertEqual(self.audit()['status'],'busy')
            self.assertEqual(self.audit(seed_reported=True)['status'],'busy')
            self.assertEqual(self.client.sent,[])
        finally:
            release.set();child.join(3)
            if child.is_alive():child.terminate();child.join()
        self.assertEqual(child.exitcode,0)
        self.assertTrue(self.audit()['sent'])

    def test_state_write_failure_keeps_previous_state_and_cleans_temp(self):
        self.audit()
        before=(self.store/wa.STATE).read_bytes()
        self.card()
        with patch.object(wa.common.os,'replace',side_effect=OSError('SECRET disk full')):
            result=self.audit(now=NOW+1)
        self.assertEqual(result['error'],'state_write_failed')
        self.assertEqual((self.store/wa.STATE).read_bytes(),before)
        self.assertEqual(list(self.store.glob('*.tmp')),[])
        self.assertNotIn('SECRET',json.dumps(result))

    def test_cli_failure_stdout_stderr_and_redaction(self):
        with patch.object(wa,'Dashboard',return_value=self.client),patch.object(wa,'database',
                side_effect=RuntimeError('Bearer SECRET-TOKEN')),patch('sys.stdout',io.StringIO()) as out, \
                patch('sys.stderr',io.StringIO()) as err:
            result=wa.main(['--state-dir',str(self.store),'--now',str(NOW)])
        self.assertEqual(result,1)
        self.assertEqual(len(out.getvalue().splitlines()),1)
        self.assertEqual(len(err.getvalue().splitlines()),1)
        self.assertNotIn('SECRET',out.getvalue()+err.getvalue()+'\n'.join(self.client.sent))

    def test_large_log_incremental_read_performance_and_no_full_reread(self):
        self.log.write_bytes(b'x'*16*1024*1024)
        started=time.monotonic()
        self.assertEqual(self.audit()['log_bytes_read'],0)
        self.append((b'normal data\n'*100+b'Stuck-input\n')*1000)
        result=self.audit(now=NOW+1)
        self.assertEqual(result['log_delta'],1000)
        self.assertLess(result['log_bytes_read'],2*1024*1024)
        self.assertLess(time.monotonic()-started,10)

    def test_random_chunk_boundaries_match_reference_line_counts(self):
        rng=random.Random(501)
        data=(b'Stu\x1b[31mck-input\x1b[0m\xff\nnormal\nSchedule target session busy\n'
              b'Stuck-input Schedule target session busy\n')*50
        for attempt in range(20):
            state=wa.stream_state();offset=count=0
            while offset<len(data):
                size=rng.randint(1,97)
                count+=wa.consume(data[offset:offset+size],state)
                offset+=size
            self.assertEqual(count,150)

    def test_dst_epoch_conversion_is_unambiguous(self):
        self.assertEqual(wa.common.parse_now('2026-10-25T02:30:00+02:00')+3600,
                         wa.common.parse_now('2026-10-25T02:30:00+01:00'))
        with self.assertRaises(wa.AuditError):wa.common.parse_now('2026-11-05T09:00:00')

    def test_log_fifo_rejected_without_blocking(self):
        self.log.unlink();os.mkfifo(self.log)
        ctx=multiprocessing.get_context('fork');queue=ctx.Queue()
        child=ctx.Process(target=lambda:queue.put(self.audit()))
        child.start();child.join(3)
        if child.is_alive():child.terminate();child.join();self.fail('FIFO blocked')
        self.assertEqual(queue.get(timeout=2)['error'],'log_read_failed')

    def test_malformed_agents_never_claim_clean(self):
        for rows in (None,{},[{'name':'progi','running':'false','needsReauth':False}],agents()+[agents()[0]]):
            self.client.rows=rows
            self.assertEqual(self.audit()['error'],'agents_invalid_response')
        self.assertFalse((self.store/wa.STATE).exists())

    def test_invalid_retry_row_fails_closed(self):
        self.retry()
        self.sql("UPDATE pending_task_retries SET last_attempt='garbage'")
        self.assertEqual(self.audit()['error'],'invalid_database_data')

    def test_stress_partial_line_survives_rotation_without_cross_file_marker(self):
        self.audit()
        self.append(b'Stuck-')
        self.assertEqual(self.audit(now=NOW+1)['log_delta'],0)
        self.log.rename(self.store/'old.log')
        self.log.write_bytes(b'input\nSchedule target session busy\n')
        self.assertEqual(self.audit(now=NOW+2)['log_delta'],1)

    def test_stress_rotation_during_scan_is_an_error_and_cursor_stays(self):
        self.audit()
        before=(self.store/wa.STATE).read_bytes()
        self.append(b'Stuck-input\n')
        original=wa.consume
        changed=[]
        def rotate(chunk,carry):
            result=original(chunk,carry)
            if not changed:
                changed.append(True)
                self.log.rename(self.store/'previous.log')
                self.log.write_bytes(b'new log\n')
            return result
        with patch.object(wa,'consume',side_effect=rotate):
            result=self.audit(now=NOW+1)
        self.assertEqual(result['error'],'log_changed')
        self.assertEqual((self.store/wa.STATE).read_bytes(),before)
        self.assertTrue(result['error_notification_sent'])

    def test_stress_append_during_scan_is_left_for_next_run(self):
        self.audit()
        self.append(b'Stuck-input\n')
        initial_size=self.log.stat().st_size
        original=wa.consume
        changed=[]
        def append(chunk,carry):
            result=original(chunk,carry)
            if not changed:
                changed.append(True)
                self.append(b'Stuck-input\n')
            return result
        with patch.object(wa,'consume',side_effect=append):
            result=self.audit(now=NOW+1)
        self.assertEqual(result['log_delta'],1)
        self.assertEqual(self.state()['log']['offset'],initial_size)
        self.assertEqual(self.audit(now=NOW+2)['log_delta'],1)

    def test_stress_truncate_during_scan_cannot_commit_clean_state(self):
        self.audit()
        self.append(b'x'*100+b'Stuck-input\n')
        before=(self.store/wa.STATE).read_bytes()
        original=wa.consume
        def truncate(chunk,carry):
            result=original(chunk,carry)
            self.log.write_bytes(b'')
            return result
        with patch.object(wa,'consume',side_effect=truncate):
            result=self.audit(now=NOW+1)
        self.assertEqual(result['error'],'log_changed')
        self.assertEqual((self.store/wa.STATE).read_bytes(),before)

    def test_stress_invalid_stream_carry_rejected(self):
        self.audit()
        original=self.state()
        for value in ([99,0],[True,0],[-1,0]):
            state=json.loads(json.dumps(original))
            state['log']['stream']['prefixes']=value
            (self.store/wa.STATE).write_text(json.dumps(state))
            self.assertEqual(self.audit(now=NOW+1)['error'],'state_read_failed')

    def test_stress_ansi_broken_on_one_line_does_not_hide_following_lines(self):
        carry=wa.stream_state()
        data=b'\x1b]unterminated secret\nStuck-input\n'
        self.assertEqual(wa.consume(data,carry),1)
        self.assertNotIn('secret',json.dumps(carry))

    def test_stress_admin_assignment_for_agent_discovered_from_api(self):
        self.client.rows.append({'name':'new-agent','running':True,'needsReauth':False})
        self.card(title='Jovahagyas kell',assignee='new-agent')
        self.assertEqual(self.audit()['hygiene_count'],1)

    def test_stress_keyword_boundaries_and_mixed_case_hungarian(self):
        self.card('notmatch',title='progitermek integracio')
        self.card('match',title='JÓVÁHAGYÁSRA VÁR')
        self.assertEqual(self.audit()['hygiene_count'],1)
        self.assertNotIn('notmatch',self.client.sent[0])

    def test_stress_sql_injection_text_has_no_side_effects(self):
        self.card(ident="x'); DROP TABLE kanban_cards; --",title='Jovahagyas $(touch /tmp/never-written)')
        self.audit()
        self.assertEqual(self.sql('SELECT count(*) FROM kanban_cards')[0][0],1)
        self.assertIn('ide zett'.replace(' ',''),self.client.sent[0])

    def test_stress_log_anchor_state_never_contains_log_or_token_text(self):
        self.log.write_bytes(b'Authorization: Bearer SECRET-TOKEN\n')
        self.audit()
        self.append(b'\x1b]SECRET-TOKEN')
        self.audit(now=NOW+1)
        text=(self.store/wa.STATE).read_text()
        for forbidden in ('SECRET','Authorization','Bearer'):
            self.assertNotIn(forbidden,text)

    def test_stress_database_connection_refuses_real_write(self):
        original=wa.sqlite3.connect
        uris=[]
        def verify(database_uri,**kwargs):
            connection=original(database_uri,**kwargs)
            uris.append(database_uri)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError,'readonly'):
                    connection.execute('DELETE FROM pending_task_retries')
            except BaseException:
                connection.close();raise
            return connection
        with patch.object(wa.sqlite3,'connect',side_effect=verify):
            self.assertEqual(self.audit()['status'],'complete')
        self.assertIn('mode=ro',uris[0])

    def test_stress_first_existing_partial_line_does_not_count_historical_marker(self):
        self.log.write_bytes(b'Stuck-')
        self.audit()
        self.append(b'input\nStuck-input\n')
        self.assertEqual(self.audit(now=NOW+1)['log_delta'],1)

    def test_stress_large_appended_log_bounds_memory_and_processes_all_bytes(self):
        self.audit()
        size=16*1024*1024
        self.log.write_bytes(b'x'*(size-12)+b'Stuck-input\n')
        started=time.monotonic()
        result=self.audit(now=NOW+1)
        elapsed=time.monotonic()-started
        self.assertEqual(result['log_bytes_read'],size)
        self.assertEqual(result['log_delta'],1)
        self.assertLess(elapsed,10)
        self.assertLess(len(json.dumps(self.state())),2048)

    def test_stress_missing_dependency_and_path_regression_not_hidden_by_cwd(self):
        # Standalone copies prove file-relative imports and defaults, with no HTTP.
        repo=self.store/'repo'
        scripts=repo/'scripts';scripts.mkdir(parents=True)
        store=repo/'store';store.mkdir()
        shutil.copyfile(PATH,scripts/PATH.name)
        shutil.copyfile(PATH.parent/'kanban-audit.py',scripts/'kanban-audit.py')
        shutil.copyfile(self.db,store/'claudeclaw.db')
        (store/'dashboard.log').write_bytes(b'')
        # Same timestamp makes a completed audit a no-op, avoiding a live GET.
        state=wa.fresh();state['last_audit_at']=NOW
        (store/wa.STATE).write_text(json.dumps(state))
        result=subprocess.run(['/usr/bin/python3',str(scripts/PATH.name),'--now',str(NOW),'--dry-run'],
            cwd=self.store,env={**os.environ,'PATH':''},capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'],'already_audited')
        self.assertFalse((scripts/'__pycache__').exists())


    def test_stress_two_real_audits_send_only_once(self):
        self.retry()
        ctx=multiprocessing.get_context('fork')
        barrier=ctx.Barrier(3);counter=ctx.Value('i',0);queue=ctx.Queue()
        def worker():
            class CountingClient(FakeClient):
                def send(self,text):
                    with counter.get_lock():counter.value+=1
                    time.sleep(0.05)
                    return True
            barrier.wait(3)
            queue.put(wa.Audit(self.store,NOW,client=CountingClient()).run())
        children=[ctx.Process(target=worker) for _ in range(2)]
        for child in children:child.start()
        try:
            barrier.wait(3)
            for child in children:child.join(5)
            self.assertTrue(all(child.exitcode==0 for child in children))
            results=[queue.get(timeout=2) for _ in children]
            self.assertEqual(sum(r['status']=='complete' for r in results),1)
            self.assertTrue(all(r['status'] in ('complete','busy','already_audited') for r in results))
            self.assertEqual(counter.value,1)
        finally:
            for child in children:
                if child.is_alive():child.terminate();child.join()

    def test_stress_configurable_threshold_and_keyword_lists(self):
        self.audit()
        self.card(title='Kulonleges tulajdonosi varakozas')
        with patch.object(wa,'ADMIN_KEYWORDS',('kulonleges',)),patch.object(wa,'LOG_THRESHOLD',1):
            self.append(b'Stuck-input\n'*2)
            result=self.audit(now=NOW+1)
        self.assertEqual(result['hygiene_count'],1)
        self.assertEqual(result['log_delta'],2)
        self.assertTrue(result['notification_needed'])

    def test_stress_log_symlink_rejected_without_following_it(self):
        target=self.store/'private'
        target.write_bytes(b'SECRET')
        self.log.unlink();self.log.symlink_to(target)
        result=self.audit()
        self.assertEqual(result['error'],'log_read_failed')
        self.assertNotIn('SECRET',json.dumps(result)+' '.join(self.client.sent))
        self.assertEqual(target.read_bytes(),b'SECRET')


    def test_review_real_titles_without_admin_keywords_never_match(self):
        titles=('Kiscsillag Egyesulet heti 2 Facebook poszt',
                'context-restart-gate: pending-outbound-messages meg mindig teves blokk',
                'Claude + Codex egyforma kihasznalas: feladatok ujragondolasa',
                'AI Shop Booster v2.3.2 stabilizacios javitas',
                'ASB: licence-atalakitas -- tier-struktura frissites + Freemius integracio')
        for n,title in enumerate(titles):
            self.card(str(n),title=title,description='Istvanra var. Progi es okoska korabban atnezte.')
        result=self.audit()
        self.assertEqual(result['hygiene_count'],0)
        self.assertFalse(result['notification_needed'])
        self.assertEqual(result['log_change'],'initialized')
        self.assertEqual(self.client.sent,[])

    def test_review_seventeen_generic_unassigned_cards_are_quiet_on_first_run(self):
        for n in range(17):
            self.card(str(n),title='Folyamatos projekt '+str(n),description='Istvan es progi feladataira varunk.')
        self.log.write_bytes(b'Stuck-input\n'*4000)
        result=self.audit()
        self.assertEqual(result['hygiene_count'],0)
        self.assertEqual(result['log_delta'],0)
        self.assertEqual(result['log_change'],'initialized')
        self.assertFalse(result['notification_needed'])
        self.assertEqual(self.client.sent,[])

    def test_review_synthetic_admin_title_is_a_real_first_run_exception(self):
        self.card(title='WP admin felulet beallitas jovahagyas')
        result=self.audit()
        self.assertEqual(result['log_change'],'initialized')
        self.assertEqual(result['hygiene_count'],1)
        self.assertTrue(result['sent'])
        self.assertEqual(len(self.client.sent),1)

    def test_review_each_admin_keyword_accent_and_case_in_title_or_description(self):
        variants=('admin-felulet','ADMIN-FELÜLET','jovahagyas','JÓVÁHAGYÁS',
                  'jovahagy','JÓVÁHAGY','WP beallitas','WP BEÁLLÍTÁS',
                  'engedely','ENGEDÉLY','hozzaferes','HOZZÁFÉRÉS','belepes','BELÉPÉS')
        for n,word in enumerate(variants):
            for field in ('title','description'):
                self.card(str(n)+field,title=word if field=='title' else 'Projekt',
                          description=word if field=='description' else '',assignee=None)
        self.assertEqual(self.audit()['hygiene_count'],2*len(variants))

    def test_review_subagent_admin_branch_has_exact_28_day_gate(self):
        self.card(title='Hozzaferes engedely kell',assignee='progi')
        self.assertEqual(self.audit()['hygiene_count'],1)
        self.assertEqual(self.audit(now=NOW+7*wa.DAY)['hygiene_count'],0)
        self.assertEqual(self.audit(now=NOW+28*wa.DAY-1)['hygiene_count'],0)
        self.assertEqual(len(self.client.sent),1)
        self.assertEqual(self.audit(now=NOW+28*wa.DAY)['hygiene_count'],1)
        self.assertEqual(len(self.client.sent),2)

    def test_seed_both_branches_silently_without_log_initialization(self):
        self.card('unassigned')
        self.card('assigned',assignee='iris')
        self.card('generic',title='Istvanra varo folyamatos projekt')
        self.log.unlink()  # Seed must not require or fabricate any log observation.
        with patch.object(wa,'scan_log',side_effect=AssertionError('no scan')):
            result=self.audit(seed_reported=True)
        self.assertEqual(result['seed_count'],2)
        self.assertEqual(self.state(),{'last_audit_at':None,'reported':{'unassigned':NOW,'assigned':NOW},'log':None})
        self.assertEqual(self.client.sent,[])
        self.assertFalse(self.log.exists())

    def test_seed_twice_does_not_extend_gate_or_rewrite_state(self):
        self.card()
        self.audit(seed_reported=True)
        before=(self.store/wa.STATE).read_bytes()
        for delta in (0,1,7*wa.DAY):
            with patch.object(wa.common,'atomic_state',wraps=wa.common.atomic_state) as save:
                result=self.audit(now=NOW+delta,seed_reported=True)
            self.assertEqual(result['seed_count'],0)
            save.assert_not_called()
            self.assertEqual((self.store/wa.STATE).read_bytes(),before)
        self.assertEqual(self.client.sent,[])

    def test_seed_preserves_existing_log_cursor_partial_state_and_audit_time(self):
        self.audit()
        self.append(b'Stuck-')
        self.audit(now=NOW+1)
        before=self.state()
        self.append(b'input\n')
        self.card()
        with patch.object(wa,'scan_log',side_effect=AssertionError('no scan')):
            self.audit(now=NOW+2,seed_reported=True)
        self.assertEqual(self.state()['log'],before['log'])
        self.assertEqual(self.state()['last_audit_at'],before['last_audit_at'])
        result=self.audit(now=NOW+3)
        self.assertEqual(result['hygiene_count'],0)
        self.assertEqual(result['log_delta'],1)
        self.assertEqual(self.client.sent,[])

    def test_seeded_first_normal_audit_is_quiet_and_keeps_seeded_reports(self):
        self.card()
        self.log.write_bytes(b'Stuck-input\n'*4000)
        self.audit(seed_reported=True)
        result=self.audit()
        self.assertEqual(result['hygiene_count'],0)
        self.assertEqual(result['log_change'],'initialized')
        self.assertEqual(result['log_delta'],0)
        self.assertFalse(result['notification_needed'])
        self.assertEqual(self.state()['reported'],{'alpha':NOW})
        self.assertEqual(self.client.sent,[])

    def test_seed_dry_run_no_files_no_post_no_log_scan(self):
        self.card()
        before={p.name:p.read_bytes() for p in self.store.iterdir()}
        with patch.object(wa,'scan_log',side_effect=AssertionError('no scan')):
            result=self.audit(dry=True,seed_reported=True)
        self.assertEqual(result['seed_count'],1)
        self.assertEqual(result['status'],'dry_run')
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.store.iterdir()})
        self.assertEqual(self.client.sent,[])
        self.assertEqual(self.client.agent_calls,1)

    def test_seed_failed_api_does_not_send_even_best_effort_error_message(self):
        self.card()
        self.client.fail_get=True
        result=self.audit(seed_reported=True)
        self.assertEqual(result['error'],'agents_unavailable')
        self.assertFalse(result['error_notification_sent'])
        self.assertFalse((self.store/wa.STATE).exists())
        self.assertEqual(self.client.sent,[])

    def test_seed_failed_db_never_posts(self):
        self.db.unlink()
        result=self.audit(seed_reported=True)
        self.assertEqual(result['error'],'database_read_failed')
        self.assertFalse(result['error_notification_sent'])
        self.assertEqual(self.client.sent,[])

    def test_seed_ignores_other_alerts_but_does_not_claim_full_audit(self):
        self.retry()
        self.card()
        self.client.rows[0]['running']=False
        result=self.audit(seed_reported=True)
        self.assertEqual(result['status'],'seeded')
        self.assertEqual(self.client.sent,[])
        self.assertIsNone(self.state()['last_audit_at'])
        result=self.audit()
        self.assertEqual(result['retry_count'],1)
        self.assertEqual(result['agent_issue_count'],1)
        self.assertTrue(result['sent'])

    def test_seed_same_timestamp_as_previous_audit_is_not_skipped(self):
        self.audit()
        self.card()
        result=self.audit(seed_reported=True)
        self.assertEqual(result['seed_count'],1)
        self.assertEqual(self.state()['last_audit_at'],NOW)
        self.assertEqual(self.client.sent,[])

    def test_seed_atomic_write_failure_keeps_reports_and_log_retryable(self):
        self.audit()
        before=(self.store/wa.STATE).read_bytes()
        self.card()
        with patch.object(wa.common.os,'replace',side_effect=OSError('SECRET full disk')):
            result=self.audit(now=NOW+1,seed_reported=True)
        self.assertEqual(result['error'],'state_write_failed')
        self.assertEqual((self.store/wa.STATE).read_bytes(),before)
        self.assertEqual(list(self.store.glob('*.tmp')),[])
        self.assertEqual(self.client.sent,[])
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertEqual(self.audit(now=NOW+1,seed_reported=True)['seed_count'],1)

    def test_seed_cli_one_json_line_and_no_post(self):
        self.card()
        with patch.object(wa,'Dashboard',return_value=self.client),patch('sys.stdout',io.StringIO()) as out:
            status=wa.main(['--state-dir',str(self.store),'--now',str(NOW),'--seed-reported'])
        self.assertEqual(status,0)
        self.assertEqual(len(out.getvalue().splitlines()),1)
        self.assertEqual(json.loads(out.getvalue())['seed_count'],1)
        self.assertEqual(self.client.sent,[])


    def test_review_stress_keyword_must_not_be_manufactured_across_fields(self):
        self.card('admin',title='admin',description='felulet fejlesztes')
        self.card('wordpress',title='WP',description='beallitas majd kesobb')
        result=self.audit()
        self.assertEqual(result['hygiene_count'],0)
        self.assertEqual(self.client.sent,[])

    def test_review_stress_empty_keyword_cannot_flag_every_unassigned_card(self):
        self.card(title='Folyamatos projekt')
        for keywords in (('',),('   ',),('jovahagyas',None)):
            with patch.object(wa,'ADMIN_KEYWORDS',keywords):
                result=self.audit(seed_reported=True)
            self.assertEqual(result['error'],'invalid_hygiene_keywords')
            self.assertFalse(result['error_notification_sent'])
            self.assertFalse((self.store/wa.STATE).exists())
        self.assertEqual(self.client.sent,[])

    def test_review_stress_seed_preserves_unrelated_reports_and_refreshes_only_expired_matches(self):
        self.audit()
        self.card('recent')
        self.card('expired',assignee='progi')
        state=self.state()
        state['reported']={'recent':NOW-wa.DAY,'expired':NOW-29*wa.DAY,'unrelated':NOW-wa.DAY}
        (self.store/wa.STATE).write_text(json.dumps(state))
        result=self.audit(now=NOW+1,seed_reported=True)
        self.assertEqual(result['eligible_count'],2)
        self.assertEqual(result['seed_count'],1)
        self.assertEqual(self.state()['reported'],{'recent':NOW-wa.DAY,'expired':NOW+1,'unrelated':NOW-wa.DAY})
        self.assertEqual(self.state()['log'],state['log'])
        self.assertEqual(self.state()['last_audit_at'],NOW)
        self.assertEqual(self.client.sent,[])

    def test_review_stress_seed_preserves_null_log_even_for_old_log_over_threshold(self):
        self.card()
        self.log.write_bytes(b'Stuck-input\n'*4000)
        self.audit(seed_reported=True)
        self.assertIsNone(self.state()['log'])
        self.assertIsNone(self.state()['last_audit_at'])
        self.assertEqual(self.client.sent,[])
        # A genuinely new unseeded exception is still reported on first audit.
        self.card('new',title='Hozzaferes kell')
        result=self.audit(now=NOW+1)
        self.assertEqual(result['log_change'],'initialized')
        self.assertEqual(result['log_delta'],0)
        self.assertEqual(result['hygiene_count'],1)
        self.assertTrue(result['sent'])
        self.assertIn('id="new"',self.client.sent[0])
        self.assertNotIn('id="alpha"',self.client.sent[0])

    def test_review_stress_new_hygiene_after_seed_and_old_after_28_days(self):
        self.card('old')
        self.audit(seed_reported=True)
        self.audit()
        self.card('new',title='Belepes szukseges',assignee='iris')
        self.assertEqual(self.audit(now=NOW+1)['hygiene_count'],1)
        self.assertEqual(self.audit(now=NOW+28*wa.DAY)['hygiene_count'],1)
        self.assertIn('id="old"',self.client.sent[-1])
        self.assertNotIn('id="new"',self.client.sent[-1])



class DesiredAndServiceTests(unittest.TestCase):
    setUp=AuditTests.setUp
    audit=AuditTests.audit
    append=AuditTests.append
    card=AuditTests.card
    sql=AuditTests.sql

    def desired(self,value):
        (self.store/'agents-desired.json').write_text(json.dumps(value))

    def test_missing_desired_file_uses_fallback_with_vizsla(self):
        names,source=wa.desired_agents(self.store)
        self.assertEqual(source,'fallback')
        self.assertEqual(set(names),{'progi','okoska','kisprogi','iris','deepseek','vizsla'})
        result=self.audit()
        self.assertFalse(result['notification_needed'])
        self.assertEqual(result['desired_source'],'fallback')
        self.assertEqual(result['desired_count'],6)

    def test_corrupt_empty_or_invalid_utf8_desired_file_uses_fallback(self):
        path=self.store/'agents-desired.json'
        for payload in (b'not json',b'',b'  ',b'\xff',b'[]'):
            path.write_bytes(payload)
            names,source=wa.desired_agents(self.store)
            self.assertEqual(source,'fallback')
            self.assertIn('vizsla',names)

    def test_deeply_nested_invalid_desired_json_is_fallback_not_script_failure(self):
        (self.store/'agents-desired.json').write_bytes(b'['*2000+b']'*2000)
        self.assertEqual(wa.desired_agents(self.store),(wa.DEFAULT_ALWAYS_RUNNING,'fallback'))
        self.assertFalse(self.audit()['notification_needed'])

    def test_non_array_desired_values_use_fallback(self):
        for value in ({'agents':['progi']},'progi',17,False,None):
            self.desired(value)
            self.assertEqual(wa.desired_agents(self.store),(wa.DEFAULT_ALWAYS_RUNNING,'fallback'))

    def test_new_desired_agent_not_running_is_reported(self):
        self.desired(['new-agent'])
        self.client.rows.append({'name':'new-agent','running':False,'needsReauth':False})
        result=self.audit()
        self.assertEqual(result['desired_source'],'file')
        self.assertEqual(result['desired_count'],1)
        self.assertEqual(result['agent_issue_count'],1)
        self.assertIn('Agens new-agent: nem fut.',self.client.sent[0])

    def test_new_desired_agent_absent_from_api_is_reported(self):
        self.desired(['new-agent'])
        self.assertEqual(self.audit()['agent_issue_count'],1)
        self.assertIn('Agens new-agent: hianyzik az API listabol.',self.client.sent[0])

    def test_valid_file_replaces_default_required_names(self):
        self.desired(['progi'])
        for row in self.client.rows:
            row['running']=row['name']=='progi'
        self.assertEqual(self.audit()['agent_issue_count'],0)
        self.assertEqual(self.client.sent,[])

    def test_on_demand_stopped_is_normal_unless_explicitly_desired(self):
        self.desired(['progi'])
        self.client.rows += [{'name':name,'running':False,'needsReauth':False}
                            for name in ('autobot','usage-tracker')]
        self.assertFalse(self.audit()['notification_needed'])
        self.desired(['progi','autobot','usage-tracker'])
        result=self.audit(now=NOW+1)
        self.assertEqual(result['agent_issue_count'],2)
        self.assertEqual(result['desired_source'],'file')

    def test_vizsla_stopped_is_reported_by_fallback(self):
        next(row for row in self.client.rows if row['name']=='vizsla')['running']=False
        self.assertEqual(self.audit()['agent_issue_count'],1)
        self.assertIn('Agens vizsla: nem fut.',self.client.sent[0])

    def test_reauth_is_reported_even_for_non_desired_agent(self):
        self.desired(['progi'])
        next(row for row in self.client.rows if row['name']=='iris')['needsReauth']=True
        self.assertEqual(self.audit()['agent_issue_count'],1)
        self.assertIn('Agens iris: ujra belepes kell.',self.client.sent[0])

    def test_duplicates_are_deduplicated_and_owner_names_excluded(self):
        self.desired(['progi','progi','bela','istvan'])
        self.assertEqual(wa.desired_agents(self.store),(('progi',),'file'))
        self.client.rows.append({'name':'bela','running':False,'needsReauth':True})
        self.assertFalse(self.audit()['notification_needed'])

    def test_invalid_elements_and_owner_only_list_fall_back(self):
        for value in (['progi',17],[''],['../private'],['SECRET\n'],['bela','istvan']):
            self.desired(value)
            self.assertEqual(wa.desired_agents(self.store),(wa.DEFAULT_ALWAYS_RUNNING,'fallback'))

    def test_unreadable_file_uses_fallback_without_exception_or_secret(self):
        with patch.object(wa.Path,'read_text',side_effect=PermissionError('SECRET')):
            result=wa.desired_agents(self.store)
        self.assertEqual(result,(wa.DEFAULT_ALWAYS_RUNNING,'fallback'))
        self.assertNotIn('SECRET',repr(result))

    def test_desired_file_read_does_not_write_in_dry_run(self):
        self.desired(['progi'])
        before={p.name:p.read_bytes() for p in self.store.iterdir()}
        result=self.audit(dry=True)
        self.assertEqual(result['desired_source'],'file')
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.store.iterdir()})
        self.assertEqual(self.client.sent,[])

    def test_seed_uses_desired_file_without_changing_audit_state_semantics(self):
        self.desired(['progi'])
        self.card()
        result=self.audit(seed_reported=True)
        self.assertEqual(result['desired_source'],'file')
        self.assertEqual(result['seed_count'],1)
        state=json.loads((self.store/wa.STATE).read_text())
        self.assertIsNone(state['log'])
        self.assertIsNone(state['last_audit_at'])
        self.assertEqual(self.client.sent,[])

    def test_measured_baseline_eight_to_fourteen_thousand_does_not_alert(self):
        self.audit()
        self.append(b'Stuck-input\n'*8000)
        self.assertFalse(self.audit(now=NOW+1)['notification_needed'])
        self.append(b'Schedule target session busy\n'*14000)
        self.assertFalse(self.audit(now=NOW+2)['notification_needed'])
        self.assertEqual(self.client.sent,[])

    def test_service_has_valid_restart_policy_and_effective_rate_limit_window(self):
        import configparser
        parser=configparser.ConfigParser(interpolation=None)
        path=PATH.parent/'systemd/heti-elakadas-audit.service'
        parser.read(path)
        service=parser['Service'];unit=parser['Unit']
        self.assertEqual(service['Type'],'oneshot')
        self.assertEqual(service['Restart'],'on-failure')
        self.assertEqual(service.getint('RestartSec'),300)
        self.assertEqual(unit.getint('StartLimitBurst'),3)
        interval=unit.getint('StartLimitIntervalSec')
        # Include the initial run, each possible 180s timeout and restart delay.
        self.assertGreater(interval,3*(service.getint('RestartSec')+service.getint('TimeoutStartSec')))
        self.assertNotIn('OnFailure',unit)
        self.assertIn('# OnFailure=',path.read_text())
        self.assertTrue(service['ExecStart'].startswith('/usr/bin/python3 '))



class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Path(self.tmp.name)
        (self.store/'.dashboard-token').write_text('private-test-token')

    def response(self,body):
        response=Mock();response.status=200;response.read.return_value=body
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        return response

    def test_get_uses_local_authenticated_agents_endpoint(self):
        client=wa.Dashboard(self.store,'http://127.0.0.1:9999')
        client.opener.open=Mock(return_value=self.response(json.dumps(agents()).encode()))
        self.assertEqual(client.agents(),agents())
        request=client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'http://127.0.0.1:9999/api/agents')
        self.assertEqual(request.get_method(),'GET')
        self.assertEqual(request.get_header('Authorization'),'Bearer private-test-token')

    def test_shared_post_uses_fixed_bela_identity(self):
        client=wa.Dashboard(self.store,'http://localhost:3420')
        client.opener.open=Mock(return_value=self.response(b'{"id":123}'))
        self.assertTrue(client.send('[HETI-AUDIT] test'))
        self.assertEqual(json.loads(client.opener.open.call_args.args[0].data),
                         {'from':'bela','to':'bela','content':'[HETI-AUDIT] test'})

    def test_network_exceptions_and_invalid_json_are_redacted(self):
        client=wa.Dashboard(self.store,'http://localhost:3420')
        client.opener.open=Mock(side_effect=RuntimeError('SECRET URL'))
        with self.assertRaisesRegex(wa.AuditError,'^agents_unavailable$'):client.agents()
        client.opener.open=Mock(return_value=self.response(b'SECRET not JSON'))
        with self.assertRaisesRegex(wa.AuditError,'^agents_unavailable$'):client.agents()

    def test_credential_failure_does_not_get(self):
        (self.store/'.dashboard-token').unlink()
        client=wa.Dashboard(self.store,'http://localhost:3420');client.opener.open=Mock()
        with self.assertRaisesRegex(wa.AuditError,'dashboard_credentials_unavailable'):client.agents()
        client.opener.open.assert_not_called()

    def test_oversize_response_is_rejected(self):
        client=wa.Dashboard(self.store,'http://localhost:3420')
        client.opener.open=Mock(return_value=self.response(b'x'*(1024*1024+1)))
        with self.assertRaises(wa.AuditError):client.agents()

    def test_external_endpoint_is_rejected_and_redirects_disabled(self):
        with self.assertRaises(wa.AuditError):wa.Dashboard(self.store,'http://evil.example')
        self.assertIsNone(wa.common.NoRedirect().redirect_request(None,None,302,'',{},'http://evil.example'))



class RetryMillisecondsTests(unittest.TestCase):
    setUp=AuditTests.setUp
    sql=AuditTests.sql
    audit=AuditTests.audit
    retry=AuditTests.retry

    def real_retry(self):
        self.sql('INSERT INTO pending_task_retries(id,task_name,agent_name,first_attempt,last_attempt,attempt_count) VALUES(?,?,?,?,?,?)',
                 (3623,'stuck-menu-scan','progi',1794211206925,1794211238432,3))

    def test_exact_production_ms_row_is_valid_and_fresh(self):
        self.real_retry()
        before={p.name:p.read_bytes() for p in self.store.iterdir()}
        retries,cards=wa.database(self.db,dry=True)
        self.assertEqual(retries[0]['first_attempt'],1794211206925)
        self.assertEqual(retries[0]['last_attempt'],1794211238432)
        self.assertFalse(wa.common.epoch(retries[0]['last_attempt']))
        result=self.audit(now=1794211239,dry=True)
        self.assertEqual(result['status'],'dry_run');self.assertEqual(result['retry_count'],0)
        self.assertFalse(result['notification_needed']);self.assertEqual(self.client.sent,[])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.store.iterdir()})

    def test_real_ms_row_is_reported_after_two_hours(self):
        self.real_retry()
        result=self.audit(now=1794211239+7200)
        self.assertEqual(result['retry_count'],1)
        self.assertIn('Retry id=3623; task="stuck-menu-scan";',self.client.sent[0])
        self.assertIn('probak=3',self.client.sent[0])
        self.assertNotIn('1794211',self.client.sent[0])  # No timestamp rendering to rescale.

    def test_two_hour_threshold_at_one_millisecond_resolution(self):
        self.retry(count=1)
        for age_ms in (0,7199999,7200000,7200001,-1,-7200001):
            with self.subTest(age_ms=age_ms):
                self.sql('UPDATE pending_task_retries SET last_attempt=?',(NOW*1000-age_ms,))
                result=self.audit(dry=True)
                self.assertEqual(result['status'],'dry_run')
                self.assertEqual(result['retry_count'],int(age_ms>7200000))
        self.assertEqual(self.client.sent,[])

    def test_first_attempt_is_validation_only_last_attempt_controls_age(self):
        self.retry(count=1)
        self.sql('UPDATE pending_task_retries SET first_attempt=?, last_attempt=?',((NOW-90*wa.DAY)*1000,NOW*1000))
        self.assertEqual(self.audit(dry=True)['retry_count'],0)

    def test_attempt_count_threshold_is_unchanged_for_fresh_ms_rows(self):
        self.retry(count=50,name='boundary');self.retry(count=51,name='over')
        self.assertEqual(self.audit(dry=True)['retry_count'],1)

    def test_ms_validator_range_and_invalid_types(self):
        maximum=(wa.common.MAX_EPOCH+1)*1000-1
        for value in (0,1,NOW,NOW*1000,1794211206925,maximum):
            self.assertTrue(wa.retry_epoch_ms(value),value)
        for value in (-1,maximum+1,2**63-1,10**10000,None,True,False,1.0,1.5,float('nan'),float('inf'),'1794211206925',{},[]):
            self.assertFalse(wa.retry_epoch_ms(value))
        self.assertTrue(wa.common.epoch(NOW));self.assertFalse(wa.common.epoch(NOW*1000))

    def test_each_retry_timestamp_is_validated_without_unit_guessing(self):
        self.retry(count=1);maximum=(wa.common.MAX_EPOCH+1)*1000-1
        for column in ('first_attempt','last_attempt'):
            for value in (0,maximum):
                self.sql('UPDATE pending_task_retries SET first_attempt=?,last_attempt=?',(NOW*1000,NOW*1000))
                self.sql('UPDATE pending_task_retries SET '+column+'=?',(value,))
                retries,_=wa.database(self.db,dry=True)
                self.assertEqual(retries[0][column],value)
            for value in (-1,maximum+1,'FAKE_SECRET',1.5):
                self.sql('UPDATE pending_task_retries SET first_attempt=?,last_attempt=?',(NOW*1000,NOW*1000))
                self.sql('UPDATE pending_task_retries SET '+column+'=?',(value,))
                with self.assertRaisesRegex(wa.AuditError,'^invalid_database_data$'):wa.database(self.db,dry=True)

    def test_state_timestamps_and_summary_remain_seconds(self):
        self.retry()
        self.audit()
        state=json.loads((self.store/wa.STATE).read_text())
        self.assertEqual(state['last_audit_at'],NOW)
        self.assertEqual(wa.read_state(self.store/wa.STATE,NOW)['last_audit_at'],NOW)
        self.assertEqual(self.sql('SELECT first_attempt,last_attempt FROM pending_task_retries'),[((NOW-wa.DAY)*1000,NOW*1000)])

    def test_stress_age_and_attempt_matrix_1000_cases(self):
        self.retry(count=1);rng=random.Random(5188)
        for index in range(1000):
            age_ms=rng.choice((7199999,7200000,7200001,rng.randrange(-wa.DAY*1000,7*wa.DAY*1000)))
            attempts=rng.choice((0,1,49,50,51,100))
            self.sql('UPDATE pending_task_retries SET last_attempt=?,attempt_count=?',(NOW*1000-age_ms,attempts))
            result=self.audit(dry=True)
            self.assertEqual(result['status'],'dry_run')
            self.assertEqual(result['retry_count'],int(age_ms>7200000 or attempts>50),index)
        self.assertEqual(self.client.sent,[])

    def test_dry_cli_with_ms_row_is_immutable_and_does_not_send_even_on_error(self):
        self.real_retry();before={p.name:p.read_bytes() for p in self.store.iterdir()}
        connect=wa.sqlite3.connect
        with patch.object(wa,'Dashboard',return_value=self.client),patch.object(wa.sqlite3,'connect',wraps=connect) as calls, \
             patch.object(wa.common,'atomic_state',side_effect=AssertionError('NO_STATE_WRITE')) as save, \
             patch.object(self.client,'send',side_effect=AssertionError('NO_SEND')) as send, \
             patch('sys.stdout',io.StringIO()) as out,patch('sys.stderr',io.StringIO()) as err:
            status=wa.main(['--dry-run','--state-dir',str(self.store),'--db',str(self.db),'--now','1794211239'])
        self.assertEqual(status,0);self.assertEqual(err.getvalue(),'')
        self.assertFalse(json.loads(out.getvalue())['sent'])
        self.assertIn('mode=ro&immutable=1',calls.call_args.args[0]);save.assert_not_called();send.assert_not_called()
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.store.iterdir()})
        self.sql("UPDATE pending_task_retries SET last_attempt='FAKE_SECRET'")
        with patch.object(wa,'Dashboard',return_value=self.client),patch('sys.stdout',io.StringIO()) as out,patch('sys.stderr',io.StringIO()) as err:
            status=wa.main(['--dry-run','--state-dir',str(self.store),'--now','1794211239'])
        self.assertEqual(status,1);self.assertEqual(json.loads(out.getvalue())['error'],'invalid_database_data')
        self.assertNotIn('FAKE_SECRET',out.getvalue()+err.getvalue());self.assertEqual(self.client.sent,[])

    def test_nonempty_wal_dry_run_refuses_before_sqlite_or_send(self):
        self.real_retry();wal=Path(str(self.db)+'-wal');wal.write_bytes(b'synthetic-uncheckpointed-wal')
        before={p.name:p.read_bytes() for p in self.store.iterdir()}
        with patch.object(wa.sqlite3,'connect',side_effect=AssertionError('NO_CONNECTION')) as connect:
            result=self.audit(dry=True)
        self.assertEqual(result['error'],'dry_run_requires_checkpointed_db')
        self.assertFalse(result['error_notification_sent']);connect.assert_not_called()
        self.assertEqual(self.client.sent,[])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.store.iterdir()})


if __name__=='__main__':unittest.main()

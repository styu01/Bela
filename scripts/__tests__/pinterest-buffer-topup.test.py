#!/usr/bin/env python3
"""Temporary pin files/config/state and fake Buffer/dashboard only."""
import importlib.util,io,json,multiprocessing,os,sys,tempfile,unittest
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock,patch
SCRIPTS=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(SCRIPTS))
import pinterest_schedule as s
spec=importlib.util.spec_from_file_location('pinterest_buffer_tested',SCRIPTS/'pinterest-buffer-topup.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
NOW=datetime(2026,11,5,12,tzinfo=timezone.utc)


class FakeBuffer:
    def __init__(self):self.posts={'scheduled':[],'draft':[]};self.edits=[];self.fetches=[];self.fail=False;self.fail_after=None
    def fetch(self,status):
        self.fetches.append(status)
        if self.fail:raise s.Error('buffer_unavailable')
        return deepcopy(self.posts[status])
    def edit(self,inp):
        if self.fail_after is not None and len(self.edits)>=self.fail_after:raise s.Error('buffer_unavailable')
        self.edits.append(deepcopy(inp))
        post=next(x for x in self.posts['draft'] if x['id']==inp['id'])
        self.posts['draft'].remove(post);post.update(status='scheduled',dueAt=inp['dueAt'])
        self.posts['scheduled'].append(post)


class Notify:
    def __init__(self):self.sent=[];self.fail=False
    def send(self,text):
        if self.fail:raise RuntimeError('SECRET dashboard token')
        self.sent.append(text);return True


class RulesTests(unittest.TestCase):
    def test_pin_keys_accept_image_video_and_strategy_suffixes(self):
        for content,expected in (('D12_P7_C1_T18',(12,7,'D')),('V12_P1_S1_T06',(12,1,'V')),
                                 ('V09_P1',(9,1,'V')),('D1_P1',(1,1,'D')),
                                 ('V9999_P999_aZ09',(9999,999,'V'))):
            with self.subTest(content=content):
                self.assertEqual(p.pin_key({'metadata':{'url':'https://example.test/?utm_content='+content}}),expected)

    def test_pin_keys_remain_strict_and_positive(self):
        for content in ('D0_P1','V1_P0','D12345_P1','V1_P1234','V1_P1_',
                        'V1_P1__S1','V1_P1_bad-tag','v1_P1','D1_P1_tail!',
                        'V1_P1&utm_content=V1_P1','unrelated'):
            with self.subTest(content=content):
                self.assertIsNone(p.pin_key({'metadata':{'url':'https://example.test/?utm_content='+content}}))
        for metadata in (None,[],{'url':42},{'url':b'https://example.test/?utm_content=V1_P1'}):
            self.assertIsNone(p.pin_key({'metadata':metadata}))

    def test_defaults_and_missing_config(self):
        with tempfile.TemporaryDirectory() as directory:self.assertEqual(s.load(Path(directory)),s.defaults())
        self.assertTrue(all(hours==[8,10,12,14,16,19,21] for hours in s.defaults()['slots_et_by_weekday'].values()))

    def test_weekday_slots_off_skip_and_pause_inclusive(self):
        config=s.defaults();config['slots_et_by_weekday']['thu']=[]
        self.assertEqual(s.next_slot(NOW,config).astimezone(s.ET).date().isoformat(),'2026-11-06')
        config['skip_dates_et']=['2026-11-06']
        self.assertEqual(s.next_slot(NOW,config).astimezone(s.ET).date().isoformat(),'2026-11-07')
        config['pause_until_et']='2026-11-07'
        self.assertEqual(s.next_slot(NOW,config).astimezone(s.ET).date().isoformat(),'2026-11-08')

    def test_custom_hours_and_strict_next_slot(self):
        config=s.defaults();config['slots_et_by_weekday']['thu']=[9,17]
        self.assertEqual(s.next_slot(NOW,config).astimezone(s.ET).hour,9)
        at=s.next_slot(NOW,config)
        self.assertEqual(s.next_slot(at,config).astimezone(s.ET).hour,17)

    def test_cap_existing_posts_and_fourteen_day_horizon(self):
        config=s.defaults();config['max_pins_per_day']=1
        self.assertEqual(s.next_slot(NOW,config,{'2026-11-05':1}).astimezone(s.ET).date().isoformat(),'2026-11-06')
        config['slots_et_by_weekday']={d:[] for d in s.DAYS}
        with self.assertRaisesRegex(s.Error,'no_slot_within_14_days'):s.next_slot(NOW,config)
        config=s.defaults();config['skip_dates_et']=[(NOW.astimezone(s.ET).date()+timedelta(days=i)).isoformat() for i in range(14)]
        with self.assertRaisesRegex(s.Error,'no_slot_within_14_days'):s.next_slot(NOW,config)

    def test_exact_horizon_last_day_is_allowed_but_day_fourteen_not(self):
        config=s.defaults();day=NOW.astimezone(s.ET).date()
        config['skip_dates_et']=[(day+timedelta(days=i)).isoformat() for i in range(13)]
        self.assertEqual(s.next_slot(NOW,config).astimezone(s.ET).date(),day+timedelta(days=13))
        with self.assertRaises(s.Error):s.next_slot(NOW+timedelta(days=14),config,start_day=day)

    def test_spring_nonexistent_hour_skipped_and_fall_fold_once(self):
        config=s.defaults();config['slots_et_by_weekday']['sun']=[1,2,3]
        spring=s.instants(config,datetime(2026,3,8).date())
        self.assertEqual([x.astimezone(s.ET).hour for x in spring],[1,3])
        self.assertEqual([x.hour for x in spring],[6,7])
        fall=s.instants(config,datetime(2026,11,1).date())
        self.assertEqual([x.hour for x in fall],[5,7,8])
        self.assertEqual(len(fall),3)

    def test_invalid_config_never_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/s.CONFIG
            for raw in ('broken','null','[]','{}','{"max_pins_per_day":7,"max_pins_per_day":0}'):
                path.write_text(raw)
                with self.assertRaisesRegex(s.Error,'schedule_invalid'):s.load(Path(directory))

    def test_validation_bad_hours_dates_and_types(self):
        for hours in ([8,8],[10,8],[-1],[24],[True],[8.0],['8']):
            config=s.defaults();config['slots_et_by_weekday']['mon']=hours
            with self.assertRaises(s.Error):s.validate(config)
        for value in ('2026-02-29','20261105','2026-13-01',None):
            config=s.defaults();config['skip_dates_et']=[value]
            with self.assertRaises(s.Error):s.validate(config)
        for cap in (True,-1,25,7.0):
            config=s.defaults();config['max_pins_per_day']=cap
            with self.assertRaises(s.Error):s.validate(config)

    def test_estimate_respects_off_days_and_cap(self):
        config=s.defaults();self.assertEqual(s.estimate(14,config,NOW.date()),2)
        config['slots_et_by_weekday']['sat']=[];config['slots_et_by_weekday']['sun']=[]
        self.assertEqual(s.estimate(14,config,NOW.date()),2.8)
        config['max_pins_per_day']=1
        self.assertEqual(s.estimate(10,config,NOW.date()),14)
        config['max_pins_per_day']=0
        self.assertIsNone(s.estimate(10,config,NOW.date()))


class TopUpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Path(self.tmp.name);self.pins=self.store/'pins';self.pins.mkdir()
        self.buffer=FakeBuffer();self.notify=Notify()
        probe=patch.object(p.subprocess,'run',return_value=Mock(stdout=json.dumps({'streams':[{'codec_type':'video','duration':'6.000000'}]})))
        self.probe=probe.start();self.addCleanup(probe.stop)

    def drafts(self,count):
        for i in range(count):
            day,pin=i//7+1,i%7+1
            folder=self.pins/f'nap_{day}'/f'D{day:02d}_P{pin}_pin';folder.mkdir(parents=True)
            link=f'https://example.test/?utm_content=D{day}_P{pin}'
            (folder/'clip.mp4').write_bytes(b'mock-local-video')
            (folder/'pin.txt').write_text(f'BOARD: AI Nutrition and Meal Tracking\nDESCRIPTION: Approved text\nALT TEXT: Image alt\nTITLE: Approved title\nLINK: {link}\n')
            self.buffer.posts['draft'].append({'id':str(i),'status':'draft','dueAt':None,'assets':[{'source':'https://images.example/a.png'}],
                'metadata':{'url':link}})

    def scheduled(self,count):
        self.buffer.posts['scheduled']=[{'id':'s'+str(i),'status':'scheduled','dueAt':(NOW+timedelta(hours=i+1)).isoformat()} for i in range(count)]

    def run_topup(self,now=NOW,dry=False):
        return p.TopUp(self.store,now,dry,self.buffer,self.notify,self.pins).run()

    def config(self,config): (self.store/s.CONFIG).write_text(json.dumps(config))

    def video(self,content='V01_P1_S1_T06'):
        post=deepcopy(self.buffer.posts['draft'][0]);post['id']='video'
        post['metadata']['url']='https://example.test/watch?utm_source=pinterest&utm_content='+content+'&keep=%2F%20#clip'
        post['assets']=[{'source':'https://media.example/clip.mp4?signature=mock-only','type':'video'}]
        return post

    def test_long_image_code_keeps_existing_image_input(self):
        self.drafts(1);post=self.buffer.posts['draft'][0]
        post['metadata']['url']='https://example.test/?utm_content=D1_P1_C1_T18'
        inp=p.pin_input(post,self.pins)
        self.assertEqual(inp['assets'],[{'image':{'url':'https://images.example/a.png','metadata':{'altText':'Image alt'}}}])
        self.assertEqual(inp['metadata']['pinterest']['url'],'https://example.test/?utm_content=D1_P1')
        self.assertEqual(self.run_topup()['added'],1)

    def test_video_uses_own_source_and_preserves_exact_link(self):
        self.drafts(1);video=self.video();self.buffer.posts['draft']=[video]
        result=self.run_topup();self.assertEqual(result['added'],1)
        inp=self.buffer.edits[0]
        self.assertEqual(inp['assets'],[{'video':{'url':video['assets'][0]['source'],'metadata':{'thumbnailOffset':5850}}}])
        self.assertEqual(inp['metadata']['pinterest'],{'title':'Approved title',
            'url':video['metadata']['url'],'boardServiceId':p.BOARDS['AI Nutrition and Meal Tracking']})
        self.assertEqual(inp['text'],'Approved text')
        self.assertEqual(inp['mode'],'customScheduled');self.assertFalse(inp['saveToDraft'])

    def test_video_ignores_pin_file_link_even_if_missing_or_invalid(self):
        self.drafts(1);video=self.video();path=next(self.pins.glob('nap_*/*/pin.txt'))
        original=path.read_text()
        for replacement in ('LINK: ftp://invalid.test/',''):
            path.write_text('\n'.join(replacement if line.startswith('LINK:') else line for line in original.splitlines()))
            self.assertEqual(p.pin_input(video,self.pins)['metadata']['pinterest']['url'],video['metadata']['url'])

    def test_legacy_video_code_uses_image_folder(self):
        self.drafts(1);folder=next(self.pins.glob('nap_*/*'));folder.rename(folder.with_name('D09_P1_pin'))
        self.buffer.posts['draft']=[self.video('V09_P1')]
        self.assertEqual(self.run_topup()['added'],1)
        self.assertIn('video',self.buffer.edits[0]['assets'][0])

    def test_image_and_video_same_day_pin_are_distinct_and_sorted(self):
        self.drafts(2);first,second=self.buffer.posts['draft'];video=self.video()
        self.buffer.posts['draft']=[second,video,first]
        result=self.run_topup();self.assertEqual(result['added'],3)
        self.assertEqual([x['id'] for x in self.buffer.edits],['0','video','1'])
        self.assertEqual(result['drafts_left'],0)

    def test_duplicate_keys_ignore_strategy_suffix_for_each_media_type(self):
        self.drafts(1)
        for kind in ('D','V'):
            with self.subTest(kind=kind):
                one=self.video(kind+'1_P1_S1');two=self.video(kind+'01_P1_T99');two['id']='other'
                self.buffer.posts['draft']=[one,two]
                self.assertEqual(self.run_topup()['error'],'pin_metadata_invalid')
                self.assertEqual(self.buffer.edits,[])

    def test_malformed_video_utm_is_ignored_without_blocking_image(self):
        self.drafts(1);image=deepcopy(self.buffer.posts['draft'][0]);template=self.video()
        for content in ('V1_P1_','V1_P0','V1_P1_bad-tag','V1_P1&utm_content=D1_P1'):
            with self.subTest(content=content):
                video=deepcopy(template);video['metadata']['url']='https://example.test/?utm_content='+content
                remote=FakeBuffer();remote.posts['draft']=[deepcopy(image),video]
                result=p.TopUp(self.store,NOW,buffer=remote,notifier=Notify(),pin_root=self.pins).run()
                self.assertEqual(result['added'],1);self.assertNotIn('error',result)
                self.assertEqual([x['id'] for x in remote.edits],['0'])
                self.assertIsNone(p.pin_input(video,self.pins))

    def test_video_requires_http_url_without_credentials(self):
        self.drafts(1)
        for prefix in ('ftp://example.test/','https://user:SECRET@example.test/','/relative'):
            video=self.video();video['metadata']['url']=prefix+'?utm_content=V1_P1'
            self.buffer.posts['draft']=[video]
            result=self.run_topup();self.assertEqual(result['error'],'pin_metadata_invalid')
            self.assertEqual(self.buffer.edits,[])
            self.assertNotIn('SECRET',json.dumps(result)+' '.join(self.notify.sent))

    def test_missing_or_changed_video_source_is_fixed_error_before_any_edit(self):
        self.drafts(1);image=self.buffer.posts['draft'][0]
        for assets in (None,[],[{}],[{}, {'source':'https://media.example/clip.mp4'}],[{'source':None}],[{'source':''}],
                       [{'source':{'url':'https://media.example/clip.mp4'}}],
                       [{'video':{'url':'https://media.example/clip.mp4'}}],
                       [{'source':'ftp://media.example/clip.mp4'}],[{'source':'https://user:SECRET@media.example/clip.mp4'}]):
            with self.subTest(assets=assets):
                video=self.video();video['assets']=assets;self.buffer.posts['draft']=[image,video]
                result=self.run_topup();self.assertEqual(result['error'],'pin_metadata_invalid')
                self.assertEqual(result['added'],0);self.assertEqual(self.buffer.edits,[])
                self.assertNotIn('SECRET',json.dumps(result)+' '.join(self.notify.sent))
        video=self.video();del video['assets'];self.buffer.posts['draft']=[video]
        self.assertEqual(self.run_topup()['error'],'pin_metadata_invalid')

    def test_video_dry_run_preserves_all_local_files_and_remote_posts(self):
        self.drafts(1);self.buffer.posts['draft']=[self.video()]
        before={str(x.relative_to(self.store)):x.read_bytes() for x in self.store.rglob('*') if x.is_file()}
        posts=deepcopy(self.buffer.posts)
        result=self.run_topup(dry=True);self.assertEqual(result['added'],1)
        self.assertEqual(self.buffer.posts,posts);self.assertEqual(self.buffer.edits,[])
        self.assertEqual(self.notify.sent,[])
        self.assertEqual(before,{str(x.relative_to(self.store)):x.read_bytes() for x in self.store.rglob('*') if x.is_file()})

    def test_mixed_media_obeys_ten_post_limit_and_inventory_estimate(self):
        self.drafts(10);video=self.video();self.buffer.posts['draft'].append(video)
        result=self.run_topup();self.assertEqual(result['added'],10)
        self.assertEqual(result['scheduled_count'],10);self.assertEqual(result['drafts_left'],1)
        self.assertEqual(result['days_of_drafts_left'],round(1/7,3))
        self.assertEqual([x['id'] for x in self.buffer.edits[:2]],['0','video'])

    def test_default_fills_ten_and_normal_is_silent(self):
        self.drafts(30);result=self.run_topup()
        self.assertEqual(result['status'],'scheduled');self.assertEqual(result['added'],10)
        self.assertEqual(result['scheduled_count'],10);self.assertEqual(result['drafts_left'],20)
        self.assertEqual(self.notify.sent,[])
        self.assertFalse((self.store/p.STATE).exists())

    def test_queue_full_does_not_edit_but_low_inventory_notifies(self):
        self.scheduled(10);result=self.run_topup()
        self.assertEqual(result['status'],'queue_full');self.assertEqual(self.buffer.edits,[])
        self.assertEqual(len(self.notify.sent),1)
        self.assertIn('2 napnal kevesebb',self.notify.sent[0])

    def test_empty_drafts_notifies_once_same_reason_per_budapest_day(self):
        self.assertEqual(self.run_topup()['status'],'no_drafts_left')
        self.run_topup(now=NOW+timedelta(hours=1));self.assertEqual(len(self.notify.sent),1)
        self.run_topup(now=NOW+timedelta(days=1));self.assertEqual(len(self.notify.sent),2)

    def test_less_than_two_days_notifies_but_exactly_two_does_not(self):
        self.drafts(24);self.run_topup();self.assertEqual(self.notify.sent,[])
        self.buffer.posts['draft'].pop();self.run_topup(now=NOW+timedelta(hours=1))
        self.assertEqual(len(self.notify.sent),1)

    def test_no_drafts_after_partial_fill_status_and_single_notice(self):
        self.drafts(2);result=self.run_topup()
        self.assertEqual(result['status'],'no_drafts_left');self.assertEqual(result['added'],2)
        self.assertEqual(len(self.notify.sent),1)
        self.assertIn('Elfogytak',self.notify.sent[0])

    def test_paused_does_not_call_buffer_or_notify(self):
        config=s.defaults();config['pause_until_et']='2026-11-05';self.config(config)
        self.assertEqual(self.run_topup()['status'],'paused')
        self.assertEqual(self.buffer.fetches,[]);self.assertEqual(self.notify.sent,[])
        self.assertEqual(self.run_topup(now=NOW+timedelta(days=1))['status'],'no_drafts_left')

    def test_all_off_is_paused(self):
        config=s.defaults();config['slots_et_by_weekday']={day:[] for day in s.DAYS};self.config(config)
        self.assertEqual(self.run_topup()['status'],'paused');self.assertEqual(self.buffer.fetches,[])

    def test_corrupt_config_does_not_fetch_or_schedule_and_error_once(self):
        (self.store/s.CONFIG).write_text('broken')
        for i in range(3):self.assertEqual(self.run_topup()['error'],'schedule_invalid')
        self.assertEqual(self.buffer.fetches,[]);self.assertEqual(self.buffer.edits,[])
        self.assertEqual(len(self.notify.sent),1)

    def test_sparse_schedule_uses_available_slots_without_filling_whole_queue(self):
        self.drafts(10);config=s.defaults()
        config['slots_et_by_weekday']={day:[] for day in s.DAYS};config['slots_et_by_weekday']['thu']=[8]
        config['max_pins_per_day']=1;self.config(config)
        result=self.run_topup()
        self.assertEqual(result['status'],'scheduled');self.assertEqual(result['reason'],'no_slot_within_14_days')
        self.assertEqual(len(self.buffer.edits),2);self.assertEqual(result['drafts_left'],8)

    def test_per_day_cap_in_full_plan(self):
        self.drafts(10);config=s.defaults();config['max_pins_per_day']=1;self.config(config)
        result=self.run_topup();self.assertEqual(result['added'],10)
        dates=[p.parse_due(x['dueAt']).astimezone(s.ET).date() for x in self.buffer.edits]
        self.assertEqual(len(dates),len(set(dates)))

    def test_existing_scheduled_day_count_respected(self):
        self.drafts(9);self.scheduled(1);config=s.defaults();config['max_pins_per_day']=1;self.config(config)
        self.run_topup()
        self.assertNotEqual(p.parse_due(self.buffer.edits[0]['dueAt']).astimezone(s.ET).date(),NOW.astimezone(s.ET).date())

    def test_bad_pin_file_preflight_before_first_edit(self):
        self.drafts(10)
        last=sorted(self.pins.glob('nap_*/D02_P3_*/pin.txt'))[0];last.unlink()
        result=self.run_topup();self.assertEqual(result['error'],'pin_files_invalid');self.assertEqual(self.buffer.edits,[])

    def test_unknown_board_error_does_not_log_value(self):
        self.drafts(1);path=next(self.pins.glob('nap_*/*/pin.txt'));path.write_text(path.read_text().replace('AI Nutrition and Meal Tracking','SECRET'))
        result=self.run_topup();self.assertEqual(result['error'],'pin_metadata_invalid')
        self.assertNotIn('SECRET',json.dumps(result)+' '.join(self.notify.sent))

    def test_dry_run_predicts_but_no_edits_state_or_notices(self):
        self.drafts(10)
        before={str(x.relative_to(self.store)):x.read_bytes() for x in self.store.rglob('*') if x.is_file()}
        result=self.run_topup(dry=True);self.assertEqual(result['added'],10)
        self.assertEqual(self.buffer.edits,[]);self.assertEqual(self.notify.sent,[])
        self.assertEqual(before,{str(x.relative_to(self.store)):x.read_bytes() for x in self.store.rglob('*') if x.is_file()})
        self.assertFalse((self.store/'.pinterest-buffer.lock').exists())

    def test_buffer_failure_fixed_error_and_daily_gate(self):
        self.buffer.fail=True
        self.assertEqual(self.run_topup()['error'],'buffer_unavailable')
        self.run_topup(now=NOW+timedelta(minutes=5));self.assertEqual(len(self.notify.sent),1)

    def test_failed_notice_retries_without_swallowing_gate(self):
        self.notify.fail=True;result=self.run_topup();self.assertEqual(result['status'],'error')
        self.assertFalse((self.store/p.STATE).exists())
        self.notify.fail=False;self.run_topup();self.assertEqual(len(self.notify.sent),1)

    def test_message_is_ascii_and_no_forbidden_dash(self):
        self.run_topup()
        for text in self.notify.sent:
            self.assertTrue(text.startswith('[PINTEREST]'));self.assertTrue(text.isascii())
            for bad in (' -- ','\u2014','\u2013'):self.assertNotIn(bad,text)

    def test_lock_blocked_by_real_other_process(self):
        ctx=multiprocessing.get_context('fork');ready,release=ctx.Event(),ctx.Event()
        def holder():
            with s.lock(self.store):ready.set();release.wait(5)
        child=ctx.Process(target=holder);child.start()
        try:
            self.assertTrue(ready.wait(2));result=self.run_topup()
            self.assertEqual(result['reason'],'busy');self.assertEqual(self.buffer.fetches,[])
        finally:
            release.set();child.join(3)
            if child.is_alive():child.terminate();child.join()

    def test_import_and_missing_api_key_are_safe_no_traceback(self):
        client=p.Buffer(self.store);client.opener.open=Mock()
        with self.assertRaisesRegex(s.Error,'buffer_credentials_unavailable'):client.gql('query{}')
        client.opener.open.assert_not_called()

    def test_stress_no_slot_at_all_errors_without_edit(self):
        self.drafts(10);config=s.defaults();day=NOW.astimezone(s.ET).date()
        config['skip_dates_et']=[(day+timedelta(days=i)).isoformat() for i in range(14)];self.config(config)
        result=self.run_topup();self.assertEqual(result['error'],'no_slot_within_14_days')
        self.assertEqual(self.buffer.edits,[])

    def test_stress_partial_remote_failure_reports_confirmed_progress_and_retry_fetches_fresh(self):
        self.drafts(30);self.buffer.fail_after=2
        result=self.run_topup();self.assertEqual(result['status'],'error')
        self.assertEqual(result['added'],2);self.assertEqual(result['scheduled_count'],2)
        self.assertEqual(result['drafts_left'],28);self.assertIsNotNone(result['next_due_utc'])
        self.assertEqual(result['days_of_drafts_left'],4)
        self.buffer.fail_after=None
        result=self.run_topup();self.assertEqual(result['added'],8)
        self.assertEqual(len({x['id'] for x in self.buffer.edits}),10)
        self.assertEqual(len(self.notify.sent),1)

    def test_stress_error_reasons_share_one_daily_error_gate(self):
        self.buffer.fail=True;self.run_topup();self.buffer.fail=False
        (self.store/s.CONFIG).write_text('bad config')
        self.run_topup(now=NOW+timedelta(minutes=5));self.assertEqual(len(self.notify.sent),1)

    def test_stress_notification_state_replace_failure_is_retryable_and_redacted(self):
        with patch.object(s.common.os,'replace',side_effect=OSError('SECRET')):
            result=self.run_topup()
        self.assertEqual(result['status'],'error');self.assertNotIn('SECRET',json.dumps(result))
        self.assertFalse((self.store/p.STATE).exists());self.assertEqual(list(self.store.glob('*.tmp')),[])
        self.run_topup();self.assertEqual(len(self.notify.sent),2)

    def test_stress_unrecognized_draft_key_is_not_counted_or_edited(self):
        self.buffer.posts['draft']=[{'id':'x','status':'draft','metadata':{'url':'https://x.test/?bad=utm_content=D1_P1'}}]
        result=self.run_topup();self.assertEqual(result['drafts_left'],0)
        self.assertEqual(result['status'],'no_drafts_left');self.assertEqual(self.buffer.edits,[])

    def test_stress_duplicate_pin_keys_stop_all_remote_mutations(self):
        self.drafts(1);other=deepcopy(self.buffer.posts['draft'][0]);other['id']='other';self.buffer.posts['draft'].append(other)
        self.assertEqual(self.run_topup()['error'],'pin_metadata_invalid');self.assertEqual(self.buffer.edits,[])

    def test_stress_duplicate_json_keys_cannot_override_skip_silently(self):
        raw=json.dumps(s.defaults()).replace('"skip_dates_et": []','"skip_dates_et": [], "skip_dates_et": ["2026-11-05"]')
        (self.store/s.CONFIG).write_text(raw)
        self.assertEqual(self.run_topup()['error'],'schedule_invalid');self.assertEqual(self.buffer.fetches,[])

    def test_stress_two_real_topups_share_one_lock_and_no_duplicate_edits(self):
        import time
        self.drafts(30);remote=self.store/'fake-remote.json';remote.write_text(json.dumps(self.buffer.posts))
        ctx=multiprocessing.get_context('fork');barrier=ctx.Barrier(3);queue=ctx.Queue();counter=ctx.Value('i',0)
        root=self.pins;store=self.store
        class SharedRemote:
            def fetch(self,status):return json.loads(remote.read_text())[status]
            def edit(self,inp):
                with counter.get_lock():counter.value+=1
                time.sleep(0.01)
                data=json.loads(remote.read_text());post=next(x for x in data['draft'] if x['id']==inp['id'])
                data['draft'].remove(post);post.update(status='scheduled',dueAt=inp['dueAt']);data['scheduled'].append(post)
                remote.write_text(json.dumps(data))
        def worker():
            barrier.wait(3);queue.put(p.TopUp(store,NOW,buffer=SharedRemote(),notifier=Notify(),pin_root=root).run())
        children=[ctx.Process(target=worker) for _ in range(2)]
        for child in children:child.start()
        try:
            barrier.wait(3)
            for child in children:child.join(5)
            self.assertTrue(all(child.exitcode==0 for child in children))
            results=[queue.get(timeout=2) for child in children]
            self.assertEqual(counter.value,10)
            self.assertEqual(sum(x['added'] for x in results),10)
        finally:
            for child in children:
                if child.is_alive():child.terminate();child.join()

    def test_stress_naive_time_rejected(self):
        with self.assertRaisesRegex(s.Error,'invalid_now'):p.TopUp(self.store,datetime(2026,11,5),buffer=self.buffer,notifier=self.notify)


    def test_stress_future_notification_stamp_cannot_mute_error_forever(self):
        (self.store/p.STATE).write_text(json.dumps({'reported':{'error':'2099-01-01'}}))
        self.buffer.fail=True;self.run_topup();self.assertEqual(len(self.notify.sent),1)

    def test_cli_missing_key_returns_one_json_line_and_fixed_error_without_network(self):
        with patch.object(p.common,'Dashboard',return_value=self.notify), \
             patch.object(p.urllib.request.OpenerDirector,'open',side_effect=AssertionError('NO LIVE API')) as http, \
             patch('sys.stdout',io.StringIO()) as out,patch('sys.stderr',io.StringIO()) as err:
            code=p.main(['--state-dir',str(self.store),'--now',NOW.isoformat()])
        self.assertEqual(code,1);self.assertEqual(len(out.getvalue().splitlines()),1)
        self.assertEqual(json.loads(out.getvalue())['error'],'buffer_credentials_unavailable')
        self.assertIn('buffer_credentials_unavailable',err.getvalue());http.assert_not_called()



class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=Path(self.tmp.name)
        (self.store/'.buffer-api-key').write_text('test-private-key');self.client=p.Buffer(self.store)
        probe=patch.object(p.subprocess,'run',return_value=Mock(stdout=json.dumps({'streams':[{'codec_type':'video','duration':'6.000000'}]})))
        self.probe=probe.start();self.addCleanup(probe.stop)

    def response(self,body,status=200):
        result=Mock();result.status=status;result.read.return_value=json.dumps(body).encode()
        result.__enter__=Mock(return_value=result);result.__exit__=Mock(return_value=False);return result

    def test_mock_graphql_video_source_reaches_video_input(self):
        post={'id':'video','status':'draft','assets':[{'source':'https://media.example/movie.mp4','type':'video',
              'mimeType':'video/mp4','thumbnail':'https://media.example/poster.jpg?sig=mock'}],
              'metadata':{'title':'Old title','url':'https://example.test/?utm_content=V12_P1_S1_T06'}}
        body={'data':{'posts':{'edges':[{'node':post}],'pageInfo':{'hasNextPage':False,'endCursor':None}}}}
        self.client.opener.open=Mock(return_value=self.response(body))
        fetched=self.client.fetch('draft')
        request=self.client.opener.open.call_args.args[0];query=json.loads(request.data)['query']
        self.assertIn('assets{source thumbnail type mimeType}',query)
        self.assertIn('PinterestPostMetadata{title url}',query)
        folder=self.store/'nap_12'/'D12_P1_pin';folder.mkdir(parents=True)
        (folder/'pin.txt').write_text('BOARD: AI Nutrition and Meal Tracking\nDESCRIPTION: Approved\nALT TEXT: Alt\nTITLE: Title\nLINK: https://example.test/?utm_content=D12_P1\n')
        (folder/'movie.mp4').write_bytes(b'mock-local-video')
        inp=p.pin_input(fetched[0],self.store)
        self.assertEqual(inp['assets'],[{'video':{'url':post['assets'][0]['source'],'metadata':{'thumbnailOffset':5850}}}])
        self.assertEqual(inp['metadata']['pinterest']['url'],post['metadata']['url'])

    def test_mock_graphql_image_legacy_and_new_optional_asset_fields(self):
        folder=self.store/'nap_1'/'D01_P1_pin';folder.mkdir(parents=True)
        link='https://example.test/?utm_content=D1_P1'
        (folder/'pin.txt').write_text('BOARD: AI Nutrition and Meal Tracking\nDESCRIPTION: Approved\nALT TEXT: Alt\nTITLE: Title\nLINK: '+link+'\n')
        for fields in ({},{'thumbnail':None,'type':None,'mimeType':None},
                       {'thumbnail':'https://cdn.test/poster.jpg','type':'image','mimeType':'image/png'}):
            post={'id':'image','status':'draft','assets':[{'source':'https://cdn.test/image.png',**fields}],
                  'metadata':{'url':link}}
            body={'data':{'posts':{'edges':[{'node':post}],'pageInfo':{'hasNextPage':False}}}}
            self.client.opener.open=Mock(return_value=self.response(body))
            inp=p.pin_input(self.client.fetch('draft')[0],self.store)
            self.assertEqual(inp['assets'],[{'image':{'url':'https://cdn.test/image.png','metadata':{'altText':'Alt'}}}])
            self.assertEqual(inp['metadata']['pinterest']['url'],link)

    def test_mock_graphql_video_mime_without_type_or_thumbnail(self):
        folder=self.store/'nap_1'/'D01_P1_pin';folder.mkdir(parents=True)
        (folder/'pin.txt').write_text('BOARD: AI Nutrition and Meal Tracking\nDESCRIPTION: Approved\nALT TEXT: Alt\nTITLE: Title\n')
        post={'id':'video','status':'draft','assets':[{'source':'https://cdn.test/video.mp4','mimeType':'video/mp4'}],
              'metadata':{'url':'https://example.test/?utm_content=V1_P1'}}
        body={'data':{'posts':{'edges':[{'node':post}],'pageInfo':{'hasNextPage':False}}}}
        self.client.opener.open=Mock(return_value=self.response(body))
        (folder/'video.mp4').write_bytes(b'mock-local-video')
        inp=p.pin_input(self.client.fetch('draft')[0],self.store)
        self.assertEqual(inp['assets'],[{'video':{'url':'https://cdn.test/video.mp4','metadata':{'thumbnailOffset':5850}}}])

    def test_http_and_graphql_errors_fixed_and_private(self):
        for body,status,code in (({'errors':[{'message':'SECRET'}]},200,'buffer_graphql_error'),({},503,'buffer_http_error'),
                                 ({'data':None},200,'buffer_invalid_response')):
            self.client.opener.open=Mock(return_value=self.response(body,status))
            with self.assertRaisesRegex(s.Error,'^'+code+'$'):self.client.gql('query{}')

    def test_network_exception_redacted(self):
        self.client.opener.open=Mock(side_effect=RuntimeError('SECRET'))
        with self.assertRaisesRegex(s.Error,'buffer_unavailable'):self.client.gql('query{}')

    def test_pagination_repeat_is_bounded(self):
        page={'posts':{'edges':[],'pageInfo':{'hasNextPage':True,'endCursor':'repeat'}}}
        with patch.object(self.client,'gql',return_value=page) as calls:
            with self.assertRaisesRegex(s.Error,'buffer_pagination_error'):self.client.fetch('draft')
        self.assertEqual(calls.call_count,2)

    def test_bad_edit_receipt_is_not_success(self):
        with patch.object(self.client,'gql',return_value={'editPost':{'__typename':'MutationError','message':'SECRET'}}):
            with self.assertRaisesRegex(s.Error,'buffer_graphql_error'):self.client.edit({'id':'1'})

    def test_request_uses_fixed_https_endpoint_and_no_proxy(self):
        self.client.opener.open=Mock(return_value=self.response({'data':{}}))
        self.client.gql('query{}');req=self.client.opener.open.call_args.args[0]
        self.assertEqual(req.full_url,'https://api.buffer.com');self.assertEqual(req.get_header('Authorization'),'Bearer test-private-key')
        self.assertIsNone(s.common.NoRedirect().redirect_request(None,None,302,'',{},'https://evil'))


if __name__=='__main__':unittest.main()

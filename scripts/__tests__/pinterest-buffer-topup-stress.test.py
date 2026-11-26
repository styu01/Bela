#!/usr/bin/env python3
"""Offline adversarial tests, fault injection and differential image regression."""
import importlib.util
import io
import json
import random
import socket
import subprocess
from decimal import Decimal
import sys
import tempfile
import unittest
import urllib.error
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(SCRIPTS))

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result

base=module('topup_stress_fixtures',SCRIPTS/'__tests__/pinterest-buffer-topup.test.py')
p,s,NOW=base.p,base.s,base.NOW
old=module('topup_image_baseline',SCRIPTS/'__tests__/fixtures/pinterest-buffer-topup-before-video.py')

def setUpModule():
    global network_guards
    network_guards=[patch.object(socket.socket,'connect',side_effect=AssertionError('NETWORK_DISABLED')),
                    patch.object(socket.socket,'connect_ex',side_effect=AssertionError('NETWORK_DISABLED')),
                    patch.object(socket,'getaddrinfo',side_effect=AssertionError('NETWORK_DISABLED'))]
    for guard in network_guards:guard.start()

def tearDownModule():
    for guard in reversed(network_guards):guard.stop()


class StressTests(unittest.TestCase):
    setUp=base.TopUpTests.setUp
    drafts=base.TopUpTests.drafts
    video=base.TopUpTests.video
    run_topup=base.TopUpTests.run_topup
    scheduled=base.TopUpTests.scheduled

    def test_numeric_boundaries_both_media_types(self):
        for kind in ('D','V'):
            for day in (0,1,9999,10000):
                for pin in (0,1,999,1000):
                    with self.subTest(kind=kind,day=day,pin=pin):
                        post={'metadata':{'url':f'https://example.test/?utm_content={kind}{day}_P{pin}'}}
                        expected=(day,pin,kind) if 1<=day<=9999 and 1<=pin<=999 else None
                        self.assertEqual(p.pin_key(post),expected)

    def test_suffix_empty_huge_repeated_and_lowercase_prefixes(self):
        for kind in ('D','V'):
            for suffix in ('','_S1_S1','_'+'a9'*100000,'_S1'*10000):
                self.assertEqual(p.pin_key({'metadata':{'url':f'https://example.test/?utm_content={kind}1_P1'+suffix}}),(1,1,kind))
            for suffix in ('_','__S1','_S1_','_á','_A-B','_A.B','_S1\n'):
                self.assertIsNone(p.pin_key({'metadata':{'url':f'https://example.test/?utm_content={kind}1_P1'+p.urllib.parse.quote(suffix)}}))
        for kind in ('d','v'):
            self.assertIsNone(p.pin_key({'metadata':{'url':f'https://example.test/?utm_content={kind}1_P1'}}))

    def test_duplicate_video_utm_including_blank_and_encoded_keys(self):
        self.drafts(1);template=self.video()
        for query in ('utm_content=V1_P1&utm_content=', 'utm_content=&utm_content=V1_P1',
                      'utm_content=V1_P1&utm_content', 'utm_content=V1_P1&utm_content=V1_P1',
                      'utm_content=D1_P1&utm_content=V1_P1',
                      'utm_content=V1_P1&%75tm_content=D1_P1'):
            with self.subTest(query=query):
                video=deepcopy(template);video['metadata']['url']='https://example.test/?'+query
                self.buffer.posts['draft']=[video]
                result=self.run_topup()
                self.assertEqual(result['status'],'no_drafts_left');self.assertNotIn('error',result)
                self.assertEqual(result['drafts_left'],0)
                self.assertEqual(self.buffer.edits,[])

    def test_duplicate_suffixes_are_allowed_but_same_video_identity_is_not(self):
        self.drafts(1);one=self.video('V1_P1_S1_S1');two=self.video('V0001_P001_OTHER');two['id']='other'
        self.buffer.posts['draft']=[one,two]
        self.assertEqual(self.run_topup()['error'],'pin_metadata_invalid')
        self.assertEqual(self.buffer.edits,[])

    def test_unparseable_url_is_ignored_but_recognized_video_url_is_validated(self):
        self.drafts(1);template=self.video()
        for url in ('https://[broken/?utm_content=V1_P1',
                    'https://[broken/?utm_content=V1_P1&utm_content=',
                    'https://cdn.test/\n?utm_content=V1_P1'):
            with self.subTest(url=url):
                video=deepcopy(template);video['metadata']['url']=url;self.buffer.posts['draft']=[video]
                result=self.run_topup()
                if '\n' in url:self.assertEqual(result.get('error'),'pin_metadata_invalid')
                else:self.assertEqual(result['status'],'no_drafts_left');self.assertNotIn('error',result)
                self.assertEqual(self.buffer.edits,[])

    def test_mixed_limit_at_nine_ten_and_eleven_existing_posts(self):
        self.drafts(1);image=self.buffer.posts['draft'][0];video=self.video()
        for count in (9,10,11):
            with self.subTest(count=count):
                remote=base.FakeBuffer();remote.posts['draft']=[deepcopy(video),deepcopy(image)]
                remote.posts['scheduled']=[{'id':'s'+str(i),'status':'scheduled','dueAt':(NOW+timedelta(hours=i+1)).isoformat()} for i in range(count)]
                result=p.TopUp(self.store,NOW,buffer=remote,notifier=base.Notify(),pin_root=self.pins).run()
                self.assertEqual(result['added'],max(0,10-count))
                self.assertEqual(result['scheduled_count'],max(count,10))
                self.assertEqual(result['drafts_left'],2-max(0,10-count))
                self.assertEqual([x['id'] for x in remote.edits],['0'] if count==9 else [])

    def test_many_mixed_drafts_numeric_order_daily_cap_and_limit(self):
        self.drafts(100);images=deepcopy(self.buffer.posts['draft'])
        for image in images:
            video=deepcopy(image);video['id']='v'+image['id']
            video['metadata']['url']=video['metadata']['url'].replace('=D','=V')+'_S1_T06'
            video['assets']=[{'source':'https://media.example/movie.mp4','mimeType':'video/mp4'}]
            self.buffer.posts['draft'].append(video)
        self.buffer.posts['draft'].reverse()
        result=self.run_topup();self.assertEqual(result['added'],10)
        self.assertEqual(result['drafts_left'],190)
        self.assertEqual([x['id'] for x in self.buffer.edits],['0','v0','1','v1','2','v2','3','v3','4','v4'])
        days={}
        for inp in self.buffer.edits:
            day=p.parse_due(inp['dueAt']).astimezone(s.ET).date();days[day]=days.get(day,0)+1
        self.assertLessEqual(max(days.values()),7)

    def test_source_shapes_and_unsafe_schemes_are_rejected(self):
        self.drafts(1)
        for source in (None,{},[],True,7,'','/clip.mp4','//cdn.example/clip.mp4',
                       'data:video/mp4;base64,AAAA','javascript:alert(1)','file:///clip.mp4',
                       'https://[broken/clip.mp4','https://user:FAKE_TOKEN@cdn.example/clip.mp4',
                       'https://cdn.example/clip.mp4\r\nLocation:https://elsewhere.test/'):
            with self.subTest(source=source):
                video=self.video();video['assets']=[{'source':source,'type':'video'}];self.buffer.posts['draft']=[video]
                result=self.run_topup();self.assertEqual(result.get('error'),'pin_metadata_invalid')
                self.assertEqual(self.buffer.edits,[])
                self.assertNotIn('FAKE_TOKEN',json.dumps(result)+' '.join(self.notify.sent))

    def test_redirect_shaped_source_is_preserved_without_fetching(self):
        self.drafts(1);video=self.video()
        url='https://cdn.example/redirect?url=https%3A%2F%2Felsewhere.test%2Fclip.mp4'
        video['assets']=[{'source':url,'type':'video'}]
        with patch.object(p.urllib.request.OpenerDirector,'open',side_effect=AssertionError('NO_FETCH')) as network:
            inp=p.pin_input(video,self.pins)
        network.assert_not_called();self.assertEqual(inp['assets'],[{'video':{'url':url,'metadata':{'thumbnailOffset':5850}}}])

    def test_late_selected_video_failure_prevents_all_edits(self):
        self.drafts(10);video=deepcopy(self.buffer.posts['draft'][-1]);video['id']='late-video'
        video['metadata']['url']=video['metadata']['url'].replace('=D','=V');video['assets']=[]
        self.buffer.posts['draft'][-1]=video
        result=self.run_topup();self.assertEqual(result['error'],'pin_metadata_invalid')
        self.assertEqual(result['added'],0);self.assertEqual(self.buffer.edits,[])

    def test_unselected_video_asset_is_not_preflighted_past_limit(self):
        self.drafts(10);video=self.video('V9999_P999');video['assets']=[]
        self.buffer.posts['draft'].append(video)
        result=self.run_topup();self.assertEqual(result['added'],10)
        self.assertEqual(result['drafts_left'],1)

    def test_d_code_with_video_type_is_rejected(self):
        self.drafts(1);video=self.video('D1_P1')
        with self.assertRaisesRegex(s.Error,'^pin_metadata_invalid$'):p.pin_input(video,self.pins)

    def compare_image_runs(self,posts,config,dry=False,fail_after=None):
        outcomes=[]
        for implementation in (old,p):
            with tempfile.TemporaryDirectory(dir=self.store) as directory:
                store=Path(directory);(store/s.CONFIG).write_text(json.dumps(config))
                remote=base.FakeBuffer();remote.posts=deepcopy(posts);remote.fail_after=fail_after
                notify=base.Notify()
                result=implementation.TopUp(store,NOW,dry,remote,notify,self.pins).run()
                files={x.name:x.read_bytes() for x in store.iterdir() if x.is_file()}
                outcomes.append((result,remote.edits,remote.posts,notify.sent,files))
        self.assertEqual(*outcomes)

    def test_differential_image_regression_160_queue_config_dry_run_cases(self):
        self.drafts(30);drafts=deepcopy(self.buffer.posts['draft']);configs=[]
        for mode in ('default','cap1','sparse','pause'):
            config=s.defaults()
            if mode=='cap1':config['max_pins_per_day']=1
            if mode=='sparse':config['slots_et_by_weekday']={d:([8] if d=='thu' else []) for d in s.DAYS}
            if mode=='pause':config['pause_until_et']='2026-11-05'
            configs.append(config)
        for count in (0,1,9,10,30):
            for scheduled in (0,1,9,10):
                for config in configs:
                    for dry in (False,True):
                        with self.subTest(count=count,scheduled=scheduled,config=config,dry=dry):
                            posts={'draft':deepcopy(drafts[:count]),'scheduled':[
                                {'id':'s'+str(i),'status':'scheduled','dueAt':(NOW+timedelta(hours=i+1)).isoformat()} for i in range(scheduled)]}
                            self.compare_image_runs(posts,config,dry)

    def test_differential_image_partial_failures(self):
        self.drafts(30)
        for fail_after in (0,1,9):
            self.compare_image_runs(self.buffer.posts,s.defaults(),fail_after=fail_after)

    def test_differential_image_legacy_blank_duplicate_query_behavior(self):
        self.drafts(1);image=self.buffer.posts['draft'][0]
        for query in ('utm_content=D1_P1&utm_content=','utm_content=&utm_content=D1_P1',
                      'utm_content=D1_P1&utm_content=D1_P1','utm_content=d1_P1'):
            image['metadata']['url']='https://example.test/?'+query
            self.compare_image_runs(self.buffer.posts,s.defaults())
        image['metadata']['url']='https://[broken/?utm_content=D1_P1'
        self.compare_image_runs(self.buffer.posts,s.defaults())

    def test_differential_image_malformed_sources_keep_original_error_codes(self):
        self.drafts(1);image=self.buffer.posts['draft'][0]
        for source in (None,{},[],7,'ftp://invalid.test/','https://user:FAKE_TOKEN@cdn.test/x'):
            image['assets']=[{'source':source}]
            self.compare_image_runs(self.buffer.posts,s.defaults())

    def test_differential_image_pin_fields_and_folder_failures(self):
        self.drafts(1);path=next(self.pins.glob('nap_*/*/pin.txt'));original=path.read_text()
        for field in ('BOARD','TITLE','DESCRIPTION','ALT TEXT','LINK'):
            for replacement in ('',field+':'):
                path.write_text('\n'.join(replacement if line.startswith(field+':') else line for line in original.splitlines()))
                self.compare_image_runs(self.buffer.posts,s.defaults())
        for text in (original+'TITLE: duplicate\n',original.replace('AI Nutrition and Meal Tracking','Unknown'),
                     original.replace('https://example.test/','javascript:bad/'),
                     original.replace('AI Nutrition and Meal Tracking','Healthy Lifestyle and Food Habits')):
            path.write_text(text);self.compare_image_runs(self.buffer.posts,s.defaults())
        path.unlink();self.compare_image_runs(self.buffer.posts,s.defaults())
        outside=self.store/'outside-pin.txt';outside.write_text(original);path.symlink_to(outside)
        self.compare_image_runs(self.buffer.posts,s.defaults())


class ReviewTests(unittest.TestCase):
    setUp=base.TopUpTests.setUp
    drafts=base.TopUpTests.drafts
    video=base.TopUpTests.video

    def run_case(self,posts,max_add=None,dry=False,config=None,fail_after=None):
        with tempfile.TemporaryDirectory(dir=self.store) as directory:
            store=Path(directory)
            if config is not None:(store/s.CONFIG).write_text(json.dumps(config))
            before={x.name:x.read_bytes() for x in store.iterdir()}
            remote=base.FakeBuffer();remote.posts=deepcopy(posts);remote.fail_after=fail_after
            notify=base.Notify()
            result=p.TopUp(store,NOW,dry,remote,notify,self.pins,max_add=max_add).run()
            after={x.name:x.read_bytes() for x in store.iterdir() if x.is_file()}
            return result,remote,notify,before,after

    def test_max_add_one_of_seven_preserves_drafts_and_low_notice(self):
        self.drafts(7)
        result,remote,notify,_,_=self.run_case(self.buffer.posts,max_add=1)
        self.assertEqual(result['added'],1);self.assertEqual(result['drafts_left'],6)
        self.assertEqual(result['status'],'scheduled');self.assertEqual(result['max_add'],1)
        self.assertTrue(result['max_add_limited']);self.assertEqual(len(remote.edits),1)
        self.assertEqual(len(remote.posts['draft']),6);self.assertEqual(len(notify.sent),1)
        self.assertIn('2 napnal kevesebb',notify.sent[0]);self.assertNotIn('Elfogytak',notify.sent[0])

    def test_max_add_large_or_exact_does_not_limit_and_empty_notice_is_correct(self):
        self.drafts(7)
        for cap in (7,8,100000):
            result,remote,notify,_,_=self.run_case(self.buffer.posts,max_add=cap)
            self.assertEqual(result['added'],7);self.assertEqual(result['drafts_left'],0)
            self.assertFalse(result['max_add_limited']);self.assertEqual(result['max_add'],cap)
            self.assertEqual(result['status'],'no_drafts_left');self.assertIn('Elfogytak',notify.sent[0])

    def test_max_add_leaves_enough_inventory_for_silence(self):
        self.drafts(24)
        result,remote,notify,_,_=self.run_case(self.buffer.posts,max_add=1)
        self.assertEqual(result['drafts_left'],23);self.assertEqual(notify.sent,[])
        self.assertEqual(result['days_of_drafts_left'],round(23/7,3))

    def test_max_add_validates_last_selected_draft_before_first_edit(self):
        self.drafts(7);last=sorted(self.pins.glob('nap_*/*/pin.txt'))[-1];last.unlink()
        result,remote,_,_,_=self.run_case(self.buffer.posts,max_add=1)
        self.assertEqual(result['error'],'pin_files_invalid');self.assertEqual(result['added'],0)
        self.assertEqual(remote.edits,[])

    def test_max_add_dry_run_projects_only_capped_prefix_without_writes(self):
        self.drafts(7)
        result,remote,notify,before,after=self.run_case(self.buffer.posts,max_add=1,dry=True)
        self.assertEqual(result['added'],1);self.assertEqual(result['drafts_left'],6)
        self.assertTrue(result['dry_run']);self.assertTrue(result['max_add_limited'])
        self.assertEqual(remote.posts,self.buffer.posts);self.assertEqual(remote.edits,[])
        self.assertEqual(notify.sent,[]);self.assertEqual(before,after)

    def test_mixed_media_queue_limit_and_max_add_boundaries(self):
        self.drafts(7);posts=deepcopy(self.buffer.posts);posts['draft'].append(self.video())
        for queued in (0,8,9,10):
            posts['scheduled']=[{'id':'s'+str(i),'status':'scheduled','dueAt':(NOW+timedelta(hours=i+1)).isoformat()} for i in range(queued)]
            for cap in (1,2,20):
                with self.subTest(queued=queued,cap=cap):
                    result,remote,_,_,_=self.run_case(posts,max_add=cap)
                    plan_count=min(8,10-queued);expected=min(cap,plan_count)
                    self.assertEqual(result['added'],expected);self.assertEqual(len(remote.edits),expected)
                    self.assertEqual(result['drafts_left'],8-expected)
                    self.assertEqual(result['max_add_limited'],plan_count>cap)
                    self.assertLessEqual(result['scheduled_count'],10)
                    self.assertEqual([x['id'] for x in remote.edits[:2]],['0','video'][:expected])

    def test_max_add_keeps_full_sparse_plan_and_reports_confirmed_partial_failure(self):
        self.drafts(10);config=s.defaults()
        config['slots_et_by_weekday']={day:([8] if day=='thu' else []) for day in s.DAYS}
        result,remote,_,_,_=self.run_case(self.buffer.posts,max_add=1,config=config)
        self.assertEqual(result['added'],1);self.assertEqual(result['drafts_left'],9)
        self.assertTrue(result['max_add_limited']);self.assertEqual(result['reason'],'no_slot_within_14_days')
        result,remote,_,_,_=self.run_case(self.buffer.posts,max_add=2,fail_after=1)
        self.assertEqual(result['error'],'buffer_unavailable');self.assertEqual(result['added'],1)
        self.assertEqual(result['drafts_left'],9);self.assertEqual(len(remote.edits),1)

    def test_max_add_retries_do_not_reschedule_already_confirmed_posts(self):
        self.drafts(7)
        for expected_left in (6,5):
            result=p.TopUp(self.store,NOW,buffer=self.buffer,notifier=self.notify,pin_root=self.pins,max_add=1).run()
            self.assertEqual(result['added'],1);self.assertEqual(result['drafts_left'],expected_left)
        self.assertEqual([x['id'] for x in self.buffer.edits],['0','1'])
        self.assertEqual(len(self.notify.sent),1)

    def test_max_add_paused_and_busy_outputs_keep_option(self):
        config=s.defaults();config['max_pins_per_day']=0
        result,remote,_,_,_=self.run_case(self.buffer.posts,max_add=1,config=config)
        self.assertEqual(result['status'],'paused');self.assertEqual(result['max_add'],1)
        self.assertFalse(result['max_add_limited']);self.assertEqual(remote.fetches,[])
        with s.lock(self.store):
            result=p.TopUp(self.store,NOW,buffer=self.buffer,notifier=self.notify,pin_root=self.pins,max_add=1).run()
        self.assertEqual(result['reason'],'busy');self.assertEqual(result['max_add'],1)

    def test_cli_max_add_invalid_values_are_fixed_and_do_not_touch_dependencies(self):
        for args in (['--max-add',value] for value in ('0','-1','1.5','FAKE_SECRET','1e3','9'*5000)):
            with patch.object(p,'Buffer') as buffer,patch.object(p.common,'Dashboard') as dashboard, \
                 patch('sys.stdout',io.StringIO()) as stdout,patch('sys.stderr',io.StringIO()) as stderr:
                code=p.main(args)
            self.assertEqual(code,1);self.assertEqual(json.loads(stdout.getvalue())['error'],'schedule_invalid')
            self.assertEqual(stderr.getvalue(),'[PINTEREST] schedule_invalid\n')
            self.assertNotIn('FAKE_SECRET',stdout.getvalue()+stderr.getvalue())
            buffer.assert_not_called();dashboard.assert_not_called()
        with patch('sys.stdout',io.StringIO()) as stdout,patch('sys.stderr',io.StringIO()) as stderr:
            self.assertEqual(p.main(['--max-add']),1)
        self.assertEqual(json.loads(stdout.getvalue())['error'],'schedule_invalid')
        for value in (0,-1,True,1.5,'1'):
            with self.assertRaisesRegex(s.Error,'^schedule_invalid$'):
                p.TopUp(self.store,NOW,buffer=self.buffer,notifier=self.notify,max_add=value)

    def test_cli_max_add_valid_value_reaches_real_planner(self):
        self.drafts(7);pin_input=p.pin_input
        with patch.object(p,'Buffer',return_value=self.buffer),patch.object(p.common,'Dashboard',return_value=self.notify), \
             patch.object(p,'pin_input',side_effect=lambda post,root:pin_input(post,self.pins)), \
             patch('sys.stdout',io.StringIO()) as stdout,patch('sys.stderr',io.StringIO()) as stderr:
            code=p.main(['--state-dir',str(self.store),'--now',NOW.isoformat(),'--max-add','1'])
        result=json.loads(stdout.getvalue());self.assertEqual(code,0);self.assertEqual(stderr.getvalue(),'')
        self.assertEqual(result['added'],1);self.assertEqual(result['max_add'],1)
        self.assertEqual(len(self.buffer.edits),1)

    def test_remote_thumbnail_ignored_and_image_unchanged(self):
        self.drafts(1);image=deepcopy(self.buffer.posts['draft'][0]);video=self.video()
        expected_image=old.pin_input(image,self.pins)
        for thumbnail in (None,'',{},[],True,7,b'https://cdn.test/poster.jpg','/poster.jpg',
                          'ftp://cdn.test/poster.jpg','https://user:FAKE_SECRET@cdn.test/x',
                          'https://[bad/x','https://cdn.test/a\nb',
                          'https://cdn.test/poster.jpg?sig=%2F%20#frame','http://cdn.test/poster.jpg'):
            with self.subTest(thumbnail=thumbnail):
                video['assets'][0]['thumbnail']=thumbnail;image['assets'][0]['thumbnail']=thumbnail
                inp=p.pin_input(video,self.pins);media=inp['assets'][0]['video']
                self.assertEqual(media,{'url':video['assets'][0]['source'],'metadata':{'thumbnailOffset':5850}})
                self.assertEqual(p.pin_input(image,self.pins),expected_image)
        del video['assets'][0]['thumbnail'];self.assertNotIn('thumbnailUrl',p.pin_input(video,self.pins)['assets'][0]['video'])

    def test_type_and_mime_cartesian_matrix_for_d_and_v(self):
        self.drafts(1);template=self.video();missing=object()
        for kind in ('D','V'):
            for media_type in (missing,None,'video','image','VIDEO','',{},7):
                for mime in (missing,None,'video/mp4','video/webm','image/png','VIDEO/mp4','video/','',[],7):
                    with self.subTest(kind=kind,media_type=media_type,mime=mime):
                        post=deepcopy(template);post['metadata']['url']=f'https://example.test/?utm_content={kind}1_P1'
                        media=post['assets'][0];media.pop('type',None)
                        if media_type is not missing:media['type']=media_type
                        if mime is not missing:media['mimeType']=mime
                        video=media_type=='video' or (isinstance(mime,str) and mime in ('video/mp4','video/webm'))
                        if (kind=='V')==video:
                            self.assertIn('video' if video else 'image',p.pin_input(post,self.pins)['assets'][0])
                        else:
                            with self.assertRaisesRegex(s.Error,'^pin_metadata_invalid$'):p.pin_input(post,self.pins)

    def test_wrong_type_after_first_max_add_draft_stops_entire_plan(self):
        self.drafts(2);posts=deepcopy(self.buffer.posts)
        posts['draft'][1]['assets'][0]['mimeType']='video/mp4'
        result,remote,_,_,_=self.run_case(posts,max_add=1)
        self.assertEqual(result['error'],'pin_metadata_invalid');self.assertEqual(remote.edits,[])
        self.assertEqual(result['added'],0)

    def test_v_without_type_and_mime_is_error_d_missing_fields_is_legacy(self):
        self.drafts(1);image=self.buffer.posts['draft'][0];video=self.video()
        del video['assets'][0]['type']
        with self.assertRaisesRegex(s.Error,'^pin_metadata_invalid$'):p.pin_input(video,self.pins)
        self.assertEqual(p.pin_input(image,self.pins),old.pin_input(image,self.pins))

    def test_vip_unrelated_utm_does_not_block_long_d_code(self):
        self.drafts(1);folder=next(self.pins.glob('nap_*/*'));folder.rename(folder.with_name('D12_P7_pin'))
        image=self.buffer.posts['draft'][0];image['metadata']['url']='https://example.test/?utm_content=D12_P7_C1_T18'
        for content in ('VIP_launch','V_x','V1_P1_','V1_P0','vegyes','D12_P7_C1_T18&utm_content=VIP_launch'):
            posts=deepcopy(self.buffer.posts);unrelated=deepcopy(image);unrelated['id']='unrelated'
            unrelated['metadata']['url']='https://example.test/?utm_content='+content
            unrelated['assets']=[];posts['draft'].insert(0,unrelated)
            result,remote,_,_,_=self.run_case(posts)
            self.assertEqual(result['added'],1);self.assertNotIn('error',result)
            self.assertEqual([x['id'] for x in remote.edits],['0'])

    def test_d_v_pair_occupies_two_consecutive_slots(self):
        self.drafts(1);self.buffer.posts['draft'].insert(0,self.video())
        result,remote,_,_,_=self.run_case(self.buffer.posts)
        self.assertEqual(result['added'],2);self.assertEqual([x['id'] for x in remote.edits],['0','video'])
        config=s.defaults();first=p.parse_due(remote.edits[0]['dueAt'])
        self.assertEqual(p.parse_due(remote.edits[1]['dueAt']),s.next_slot(first,config))

    def test_legacy_pin_key_fuzz_12000_cases(self):
        rng=random.Random(5136);checked=0;i=-1
        while checked<12000:
            i+=1
            if i%3==0:content=f'D{rng.randrange(0,12001)}_P{rng.randrange(0,1201)}'+rng.choice(('','','_','-bad','__'))
            elif i%3==1:content=rng.choice(('VIP_launch','V_x','v1_P1','d1_P1','D1_P1_','V1_P1_bad-tag'))
            else:content=''.join(rng.choices('DVdvP_0123456789-! xyz',k=rng.randrange(0,35)))
            # New valid V/suffixed-D codes are intentional extensions, checked separately.
            if p.re.fullmatch(r'(D|V)[0-9]{1,4}_P[0-9]{1,3}(?:_[A-Za-z0-9]+)+',content):continue
            if p.re.fullmatch(r'V[0-9]{1,4}_P[0-9]{1,3}',content):continue
            query=p.urllib.parse.urlencode({'utm_content':content})
            if i%7==0:query+='&utm_content='
            if i%11==0:query+='&utm_content=unrelated'
            post={'metadata':{'url':rng.choice(('https://example.test/?','/relative?','https://[bad/?'))+query}}
            legacy=old.pin_key(post);expected=(*legacy,'D') if legacy is not None else None
            self.assertEqual(p.pin_key(post),expected,msg=f'fuzz case {i}')
            checked+=1


class ErrorTests(unittest.TestCase):
    setUp=base.TopUpTests.setUp

    def cli(self,*extra):
        with patch('sys.stdout',io.StringIO()) as stdout,patch('sys.stderr',io.StringIO()) as stderr:
            code=p.main(['--state-dir',str(self.store),'--now',NOW.isoformat(),*extra])
        out,err=stdout.getvalue(),stderr.getvalue()
        self.assertEqual(len(out.splitlines()),1)
        self.assertNotIn('FAKE_SECRET',out+err);self.assertNotIn('Traceback',out+err)
        return code,json.loads(out),err

    def test_all_fixed_codes_cli_and_unknown_exception_redaction(self):
        self.assertEqual(p.CODES,old.CODES|{'order_plan_invalid','order_plan_read_failed'})
        for code in sorted(p.CODES):
            with self.subTest(code=code),patch.object(p.TopUp,'run',side_effect=s.Error(code)):
                status,result,err=self.cli()
                self.assertEqual(status,1);self.assertEqual(result['error'],code)
                self.assertEqual(err,'[PINTEREST] '+code+'\n')
        for exc in (s.Error('FAKE_SECRET'),RuntimeError('FAKE_SECRET'),RuntimeError('pin_metadata_invalid')):
            with patch.object(p.TopUp,'run',side_effect=exc):
                self.assertEqual(self.cli()[1]['error'],'buffer_error')

    def test_actual_local_io_and_invalid_now_error_paths(self):
        original=Path.read_text
        for filename,expected in ((s.CONFIG,'schedule_read_failed'),(p.STATE,'state_read_failed')):
            def fail(path,*args,**kwargs):
                if path.name==filename:raise PermissionError('FAKE_SECRET')
                return original(path,*args,**kwargs)
            with patch.object(Path,'read_text',fail),patch.object(p,'Buffer',return_value=self.buffer),patch.object(p.common,'Dashboard',return_value=self.notify):
                self.assertEqual(self.cli()[1]['error'],expected)
        with patch.object(s.os,'open',side_effect=PermissionError('FAKE_SECRET')):
            self.assertEqual(self.cli()[1]['error'],'lock_failed')
        self.assertEqual(self.cli('--now','FAKE_SECRET')[1]['error'],'invalid_now')
        self.assertEqual(self.cli('--dashboard-url','https://FAKE_SECRET.example/')[1]['error'],'invalid_dashboard_url')

    def test_notification_error_redaction_for_every_fixed_code(self):
        for expected in sorted(p.CODES):
            notify=base.Notify();notify.send=Mock(side_effect=s.Error(expected))
            result=p.TopUp(self.store,NOW,buffer=self.buffer,notifier=notify,pin_root=self.pins).run()
            self.assertEqual(result['notification_error'],expected)
            self.assertFalse((self.store/p.STATE).exists())
        notify.send=Mock(return_value=False)
        result=p.TopUp(self.store,NOW,buffer=self.buffer,notifier=notify,pin_root=self.pins).run()
        self.assertEqual(result['notification_error'],'dashboard_invalid_response')

    def test_buffer_and_dashboard_real_transport_branches_with_synthetic_secrets(self):
        (self.store/'.buffer-api-key').write_text('FAKE_SECRET_BUFFER')
        (self.store/'.dashboard-token').write_text('FAKE_SECRET_DASHBOARD')
        for client,invoke,http_code,network_code,invalid_code in (
                (p.Buffer(self.store),lambda c:c.gql('query{}'),'buffer_http_error','buffer_unavailable','buffer_invalid_response'),
                (p.common.Dashboard(self.store,'http://localhost:3420'),lambda c:c.send('mock'),'dashboard_http_error','dashboard_unavailable','dashboard_invalid_response')):
            for status in (301,302,307,308,401,403,429,500,503):
                response=base.APITests.response(self,{'error':'FAKE_SECRET'},status)
                client.opener.open=Mock(return_value=response)
                with self.assertRaisesRegex(s.Error,'^'+http_code+'$'):invoke(client)
                self.assertEqual(client.opener.open.call_count,1)
            for exc in (RuntimeError('FAKE_SECRET'),urllib.error.HTTPError('https://FAKE_SECRET',302,'FAKE_SECRET',{},io.BytesIO(b'FAKE_SECRET'))):
                client.opener.open=Mock(side_effect=exc)
                expected=http_code if isinstance(exc,urllib.error.HTTPError) else network_code
                with self.assertRaisesRegex(s.Error,'^'+expected+'$'):invoke(client)
            for raw in (b'FAKE_SECRET',b'[]',b'null',b'{"data":null}',b'x'*(4*1024*1024+1)):
                response=base.APITests.response(self,{});response.read.return_value=raw
                client.opener.open=Mock(return_value=response)
                with self.assertRaisesRegex(s.Error,'^'+invalid_code+'$'):invoke(client)
        for status in (301,302,303,307,308):
            self.assertIsNone(s.common.NoRedirect().redirect_request(None,None,status,'',{},'https://FAKE_SECRET'))

    def test_buffer_pagination_100_page_bound_and_invalid_node_shapes(self):
        client=p.Buffer(self.store)
        pages=[{'posts':{'edges':[],'pageInfo':{'hasNextPage':True,'endCursor':str(i)}}} for i in range(100)]
        with patch.object(client,'gql',side_effect=pages) as calls:
            with self.assertRaisesRegex(s.Error,'^buffer_pagination_error$'):client.fetch('draft')
        self.assertEqual(calls.call_count,100)
        for node in ({},None,{'id':'','status':'draft'},{'id':'x','status':'scheduled'}):
            page={'posts':{'edges':[{'node':node}],'pageInfo':{'hasNextPage':False}}}
            with patch.object(client,'gql',return_value=page):
                with self.assertRaisesRegex(s.Error,'^buffer_invalid_response$'):client.fetch('draft')



class ThumbnailOffsetTests(unittest.TestCase):
    setUp=base.TopUpTests.setUp
    drafts=base.TopUpTests.drafts
    video=base.TopUpTests.video
    run_topup=base.TopUpTests.run_topup

    def prepare(self):
        self.drafts(1)
        return self.video(),next(self.pins.glob('nap_*/*'))

    def test_offset_integer_floor_margin_and_graphql_limits(self):
        video,folder=self.prepare()
        for duration in ('6.000000','6.000999','1','0.151','0.150001','2147483.647'):
            with self.subTest(duration=duration):
                self.probe.return_value=Mock(stdout=json.dumps({'streams':[{'codec_type':'video','duration':duration}]}))
                inp=p.pin_input(video,self.pins);media=inp['assets'][0]['video']
                offset=media['metadata']['thumbnailOffset'];ms=Decimal(duration)*1000
                self.assertIs(type(offset),int);self.assertEqual(offset,int(ms)-150)
                self.assertGreaterEqual(offset,0);self.assertLess(offset,ms)
                self.assertGreaterEqual(ms-offset,150);self.assertLess(ms-offset,151)
                self.assertLessEqual(offset,2147483647)
                self.assertEqual(set(media),{'url','metadata'})
                self.assertEqual(set(inp['metadata']['pinterest']),{'title','url','boardServiceId'})
                self.assertNotIn('thumbnailUrl',json.dumps(inp))
        args=self.probe.call_args.args[0];kwargs=self.probe.call_args.kwargs
        self.assertEqual(args[0],'ffprobe');self.assertEqual(args[-1],str((folder/'clip.mp4').resolve()))
        self.assertIn('file',args);self.assertNotIn(video['assets'][0]['source'],args)
        self.assertNotIn('shell',kwargs);self.assertTrue(kwargs['check']);self.assertEqual(kwargs['timeout'],30)
        self.assertEqual(kwargs['stdin'],subprocess.DEVNULL)

    def test_invalid_or_zero_duration_is_fixed_metadata_error(self):
        video,_=self.prepare()
        for duration in ('0','-1','0.00001','0.149999','0.150','NaN','sNaN','Infinity','-Infinity','N/A','',
                         '2147483.648','1e9999999','1e-9999999',None,True,6,{},[], 'FAKE_SECRET'):
            with self.subTest(duration=duration):
                self.probe.return_value=Mock(stdout=json.dumps({'streams':[{'codec_type':'video','duration':duration}]}))
                with self.assertRaisesRegex(s.Error,'^pin_metadata_invalid$'):p.pin_input(video,self.pins)

    def test_duration_fuzz_never_crosses_video_end(self):
        video,_=self.prepare();rng=random.Random(5160)
        for _ in range(2000):
            duration=Decimal(rng.randrange(151000,86400000000))/1000000
            self.probe.return_value=Mock(stdout=json.dumps({'streams':[{'codec_type':'video','duration':str(duration)}]}))
            offset=p.pin_input(video,self.pins)['assets'][0]['video']['metadata']['thumbnailOffset']
            self.assertTrue(0<=offset<duration*1000)
            self.assertTrue(150<=duration*1000-offset<151)

    def test_invalid_probe_output_including_format_only_is_not_trusted(self):
        video,_=self.prepare()
        for output in ('', 'FAKE_SECRET', '[]', 'null', '{}', '{"streams":null}', '{"streams":{}}',
                       '{"streams":[]}', '{"streams":[null]}','{"streams":[{}]}',
                       '{"streams":[{"codec_type":"audio","duration":"6"}]}',
                       '{"format":{"duration":"6"}}',
                       '{"streams":[{"codec_type":"video"}],"format":{"duration":"6"}}',
                       '{"streams":[{},{}]}','x'*65537):
            with self.subTest(output=output[:80]):
                self.probe.return_value=Mock(stdout=output)
                with self.assertRaisesRegex(s.Error,'^pin_metadata_invalid$'):p.pin_input(video,self.pins)

    def test_missing_probe_timeout_and_process_errors_are_redacted_before_all_edits(self):
        video,_=self.prepare();image=self.buffer.posts['draft'][0]
        for error in (FileNotFoundError('FAKE_SECRET'),PermissionError('FAKE_SECRET'),
                      subprocess.TimeoutExpired('FAKE_SECRET',30,stderr='FAKE_SECRET'),
                      subprocess.CalledProcessError(1,'FAKE_SECRET',stderr='FAKE_SECRET'),UnicodeError('FAKE_SECRET')):
            with self.subTest(error=type(error).__name__):
                self.probe.side_effect=error
                remote=base.FakeBuffer();remote.posts['draft']=[deepcopy(image),deepcopy(video)]
                notify=base.Notify()
                result=p.TopUp(self.store,NOW,buffer=remote,notifier=notify,pin_root=self.pins,max_add=1).run()
                self.assertEqual(result['error'],'pin_metadata_invalid');self.assertEqual(result['added'],0)
                self.assertEqual(remote.edits,[])
                self.assertNotIn('FAKE_SECRET',json.dumps(result)+' '.join(notify.sent))

    def test_missing_or_empty_local_files_fail_before_probe(self):
        video,folder=self.prepare();path=folder/'clip.mp4';path.unlink()
        for mode in ('missing','empty'):
            with self.subTest(mode=mode):
                if mode=='empty':path.touch()
                self.probe.reset_mock()
                with self.assertRaisesRegex(s.Error,'^pin_files_invalid$'):p.pin_input(video,self.pins)
                self.probe.assert_not_called()

    def test_actual_renamed_url_two_six_second_videos_returns_5850(self):
        video,folder=self.prepare();(folder/'clip.mp4').rename(folder/'video.mp4')
        (folder/'video_anim.mp4').write_bytes(b'animated')
        video['assets'][0]['source']='https://nutritionwmp.com/wp-content/uploads/2026/11/nwmp-v09-p1.mp4'
        self.probe.reset_mock()
        inp=p.pin_input(video,self.pins)
        self.assertEqual(inp['assets'][0]['video'],{'url':video['assets'][0]['source'],'metadata':{'thumbnailOffset':5850}})
        self.assertEqual(self.probe.call_count,2)
        self.assertEqual({Path(call.args[0][-1]).name for call in self.probe.call_args_list},{'video.mp4','video_anim.mp4'})

    def test_multiple_videos_common_offset_is_independent_of_name_and_url(self):
        video,folder=self.prepare();(folder/'video_anim.mp4').write_bytes(b'animated')
        (folder/'third.MP4').write_bytes(b'third')
        for source in ('https://cdn.test/clip.mp4','https://cdn.test/opaque.mp4?sig=FAKE_SECRET',
                       'https://cdn.test/VIDEO_ANIM.MP4','https://cdn.test/redirect?url=video_anim.mp4'):
            self.probe.reset_mock();video['assets'][0]['source']=source
            inp=p.pin_input(video,self.pins)
            self.assertEqual(inp['assets'][0]['video']['metadata'],{'thumbnailOffset':5850})
            self.assertEqual(self.probe.call_count,3)

    def test_different_lengths_fail_even_if_remote_filename_matches_one(self):
        video,folder=self.prepare();(folder/'other.mp4').write_bytes(b'other')
        video['assets'][0]['source']='https://cdn.test/clip.mp4'
        for durations in (('6.000000','5.000000'),('6.000000','6.001000')):
            with self.subTest(durations=durations):
                self.probe.reset_mock()
                self.probe.side_effect=[Mock(stdout=json.dumps({'streams':[{'codec_type':'video','duration':d}]})) for d in durations]
                with self.assertRaisesRegex(s.Error,'^pin_files_invalid$'):p.pin_input(video,self.pins)
                self.assertEqual(self.probe.call_count,2)

    def test_different_fractional_lengths_with_same_offset_are_allowed(self):
        video,folder=self.prepare();(folder/'other.mp4').write_bytes(b'other')
        self.probe.side_effect=[Mock(stdout=json.dumps({'streams':[{'codec_type':'video','duration':d}]})) for d in ('6.000001','6.000999')]
        self.assertEqual(p.pin_input(video,self.pins)['assets'][0]['video']['metadata'],{'thumbnailOffset':5850})
        self.assertEqual(self.probe.call_count,2)

    def test_common_offset_agreement_fuzz_1000_pairs(self):
        video,folder=self.prepare();(folder/'other.mp4').write_bytes(b'other')
        rng=random.Random(5162)
        for _ in range(1000):
            micros=rng.randrange(151000,86400000000)
            durations=[Decimal(micros)/1000000,Decimal(micros+rng.randrange(-999,1000))/1000000]
            # Keep both candidates above the 150 ms minimum.
            if min(durations)<=Decimal('0.150'):continue
            rng.shuffle(durations)
            expected={int(d*1000)-150 for d in durations}
            self.probe.side_effect=[Mock(stdout=json.dumps({'streams':[{'codec_type':'video','duration':str(d)}]})) for d in durations]
            if len(expected)==1:
                self.assertEqual(p.pin_input(video,self.pins)['assets'][0]['video']['metadata']['thumbnailOffset'],expected.pop())
            else:
                with self.assertRaisesRegex(s.Error,'^pin_files_invalid$'):p.pin_input(video,self.pins)

    def test_bad_second_candidate_is_not_skipped_when_first_one_is_valid(self):
        video,folder=self.prepare();(folder/'other.mp4').write_bytes(b'other')
        video['assets'][0]['source']='https://cdn.test/clip.mp4'
        good=Mock(stdout=json.dumps({'streams':[{'codec_type':'video','duration':'6'}]}))
        for bad in (Mock(stdout=json.dumps({'streams':[{'codec_type':'video','duration':'0'}]})),
                    Mock(stdout='invalid'),FileNotFoundError('FAKE_SECRET'),subprocess.TimeoutExpired('FAKE_SECRET',30)):
            with self.subTest(bad=type(bad).__name__):
                self.probe.reset_mock();self.probe.side_effect=[good,bad]
                with self.assertRaisesRegex(s.Error,'^pin_metadata_invalid$'):p.pin_input(video,self.pins)
                self.assertEqual(self.probe.call_count,2)

    def test_invalid_mp4_candidates_are_not_silently_filtered(self):
        video,folder=self.prepare();bad=folder/'other.mp4'
        for mode in ('empty','directory','broken_symlink','outside_symlink'):
            with self.subTest(mode=mode):
                if mode=='empty':bad.touch()
                elif mode=='directory':bad.mkdir()
                elif mode=='broken_symlink':bad.symlink_to(folder/'missing')
                else:
                    outside=self.store/'outside.mp4';outside.write_bytes(b'outside');bad.symlink_to(outside)
                self.probe.reset_mock()
                with self.assertRaisesRegex(s.Error,'^pin_files_invalid$'):p.pin_input(video,self.pins)
                self.probe.assert_not_called()
                if mode=='directory':bad.rmdir()
                else:bad.unlink()

    def test_three_candidates_all_measured_before_offset_disagreement_fails(self):
        video,folder=self.prepare();(folder/'second.mp4').write_bytes(b'two');(folder/'third.mp4').write_bytes(b'three')
        self.probe.side_effect=[Mock(stdout=json.dumps({'streams':[{'codec_type':'video','duration':d}]})) for d in ('6','5','6')]
        with self.assertRaisesRegex(s.Error,'^pin_files_invalid$'):p.pin_input(video,self.pins)
        self.assertEqual(self.probe.call_count,3)

    def test_multiple_candidate_disagreement_prevents_all_edits(self):
        video,folder=self.prepare();(folder/'other.mp4').write_bytes(b'other')
        self.buffer.posts['draft'].append(video)
        self.probe.side_effect=[Mock(stdout=json.dumps({'streams':[{'codec_type':'video','duration':d}]})) for d in ('6','7')]
        result=self.run_topup()
        self.assertEqual(result['error'],'pin_files_invalid');self.assertEqual(result['added'],0)
        self.assertEqual(self.buffer.edits,[])

    def test_symlink_escape_is_rejected_without_probe(self):
        video,folder=self.prepare();path=folder/'clip.mp4';path.unlink()
        outside=self.store/'outside.mp4';outside.write_bytes(b'not-in-pin-root');path.symlink_to(outside)
        self.probe.reset_mock()
        with self.assertRaisesRegex(s.Error,'^pin_files_invalid$'):p.pin_input(video,self.pins)
        self.probe.assert_not_called()

    def test_file_io_errors_are_fixed(self):
        video,_=self.prepare()
        with patch.object(Path,'iterdir',side_effect=PermissionError('FAKE_SECRET')):
            with self.assertRaisesRegex(s.Error,'^pin_files_invalid$'):p.pin_input(video,self.pins)

    def test_buffer_duration_and_thumbnail_cannot_override_local_measurement(self):
        video,_=self.prepare()
        video['assets'][0].update(thumbnail='https://cdn.test/remote.jpg',video={'durationMs':999999,'thumbnailOffset':999999})
        self.assertEqual(p.pin_input(video,self.pins)['assets'][0]['video'],{
            'url':video['assets'][0]['source'],'metadata':{'thumbnailOffset':5850}})

    def test_image_does_not_require_any_video_or_probe(self):
        self.drafts(1);image=self.buffer.posts['draft'][0]
        for path in self.pins.rglob('*.mp4'):path.unlink()
        self.probe.side_effect=AssertionError('IMAGE_MUST_NOT_PROBE')
        self.assertEqual(p.pin_input(image,self.pins),old.pin_input(image,self.pins))
        result=self.run_topup();self.assertEqual(result['added'],1);self.probe.assert_not_called()

    def test_offset_failure_in_later_video_prevents_first_image_mutation(self):
        video,folder=self.prepare();image=self.buffer.posts['draft'][0];(folder/'clip.mp4').unlink()
        self.buffer.posts['draft']=[image,video]
        result=self.run_topup();self.assertEqual(result['error'],'pin_files_invalid')
        self.assertEqual(result['added'],0);self.assertEqual(self.buffer.edits,[])


class RealMediaProbeTests(unittest.TestCase):
    def test_real_ffprobe_and_frame_decode_at_computed_offset(self):
        # Local generated video only; no Buffer credentials, API or source media.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);video=root/'test.mp4'
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i',
                'color=c=blue:s=64x64:r=25:d=1','-vf',"drawbox=color=red:t=fill:enable='gte(t,0.8)'",
                '-c:v','libx264','-pix_fmt','yuv420p',str(video)],capture_output=True,check=True,timeout=30)
            offset=p.video_thumbnail_offset(root,root)
            self.assertEqual(offset,850)
            (root/'video_anim.mp4').write_bytes(video.read_bytes())
            self.assertEqual(p.video_thumbnail_offset(root,root),850)
            frame=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-ss',str(offset/1000),'-i',str(video),
                '-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],capture_output=True,check=True,timeout=30).stdout
            self.assertEqual(len(frame),64*64*3)
            # The last 200 ms is red, whereas the earlier segment was blue.
            self.assertGreater(sum(frame[0::3])/4096,240)
            self.assertLess(sum(frame[2::3])/4096,10)

if __name__=='__main__':unittest.main()

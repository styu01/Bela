#!/usr/bin/env python3
"""Schedule only existing approved Buffer drafts. No live calls during tests."""
import sys
sys.dont_write_bytecode=True
import argparse
import glob
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime,timedelta,timezone
from pathlib import Path
from pinterest_schedule import (ROOT,ET,BUDAPEST,Error,common,load,lock,next_slot,estimate,paused,now_utc,unique_object)

ORG='6ae23eba37ad80a492ba63fa'
CHANNEL='6ae2402e2f1f5ed6480015d6'
PIN_ROOT=Path('/home/kisss/Kepek/Pinterest_NutritionWMP/V2_7naponta')
LIMIT=10
STATE='pinterest-buffer-state.json'
BOARDS={'AI Nutrition and Meal Tracking':'914934549238973707','Healthy Lifestyle and Food Habits':'914934549238973709'}
CODES={'schedule_invalid','schedule_read_failed','buffer_credentials_unavailable','buffer_http_error','buffer_unavailable',
       'buffer_graphql_error','buffer_invalid_response','buffer_pagination_error','pin_files_invalid','pin_metadata_invalid',
       'no_slot_within_14_days','state_read_failed','state_write_failed','lock_failed','invalid_now',
       'dashboard_credentials_unavailable','dashboard_http_error','dashboard_unavailable','dashboard_invalid_response',
       'invalid_dashboard_url','buffer_error'}
QUERY='''query($a:String){posts(first:50,after:$a,input:{organizationId:"%s",filter:{channelIds:["%s"],status:[%s]}}){edges{node{id status dueAt text assets{source} metadata{... on PinterestPostMetadata{title url}}}} pageInfo{hasNextPage endCursor}}}'''
EDIT='''mutation($i:EditPostInput!){editPost(input:$i){__typename ... on PostActionSuccess{post{id status dueAt}} ... on MutationError{message}}}'''


def safe(exc):
    return str(exc) if isinstance(exc,Error) and str(exc) in CODES else 'buffer_error'


def parse_due(value):
    try:
        if not isinstance(value,str):raise ValueError()
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo is None:raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError,OverflowError):raise Error('buffer_invalid_response') from None


class Buffer:
    def __init__(self,store):
        self.store=Path(store)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),common.NoRedirect())

    def gql(self,query,variables=None):
        try:
            key=(self.store/'.buffer-api-key').read_text(encoding='utf-8').strip()
            if not key or any(ord(c)<33 or ord(c)>126 for c in key):raise ValueError()
        except (OSError,ValueError,UnicodeError):raise Error('buffer_credentials_unavailable') from None
        request=urllib.request.Request('https://api.buffer.com',
            json.dumps({'query':query,'variables':variables or {}}).encode(),
            {'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
        try:
            with self.opener.open(request,timeout=30) as response:
                raw=response.read(4*1024*1024+1)
                if response.status!=200:raise Error('buffer_http_error')
        except urllib.error.HTTPError as exc:
            exc.close();raise Error('buffer_http_error') from None
        except Error:raise
        except Exception:raise Error('buffer_unavailable') from None
        try:
            result=json.loads(raw)
            if len(raw)>4*1024*1024 or not isinstance(result,dict):raise ValueError()
            if result.get('errors'):raise Error('buffer_graphql_error')
            if not isinstance(result.get('data'),dict):raise ValueError()
            return result['data']
        except (ValueError,UnicodeError,RecursionError):raise Error('buffer_invalid_response') from None

    def fetch(self,status):
        out=[];after=None;seen=set();cursors=set()
        for _ in range(100):
            data=self.gql(QUERY%(ORG,CHANNEL,status),{'a':after})
            try:
                posts=data['posts'];edges=posts['edges'];page=posts['pageInfo']
                if not isinstance(edges,list) or type(page['hasNextPage']) is not bool:raise ValueError()
                for edge in edges:
                    post=edge['node']
                    if (not isinstance(post,dict) or not isinstance(post.get('id'),str) or not post['id']
                            or post['id'] in seen or post.get('status')!=status):raise ValueError()
                    seen.add(post['id']);out.append(post)
                if not page['hasNextPage']:return out
                after=page.get('endCursor')
                if not isinstance(after,str) or not after or after in cursors:raise Error('buffer_pagination_error')
                cursors.add(after)
            except Error:raise
            except (KeyError,TypeError,ValueError):raise Error('buffer_invalid_response') from None
        raise Error('buffer_pagination_error')

    def edit(self,inp):
        data=self.gql(EDIT,{'i':inp})
        try:
            result=data['editPost'];post=result.get('post')
            if result.get('__typename')!='PostActionSuccess':raise Error('buffer_graphql_error')
            if (not isinstance(post,dict) or post.get('id')!=inp['id'] or post.get('status')!='scheduled'
                    or parse_due(post.get('dueAt'))!=parse_due(inp['dueAt'])):raise ValueError()
        except Error:raise
        except (KeyError,ValueError,TypeError,AttributeError):raise Error('buffer_invalid_response') from None


def pin_key(post):
    try:
        url=(post.get('metadata') or {}).get('url') or ''
        query=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        values=query.get('utm_content',[])
        match=re.fullmatch(r'D([0-9]{1,4})_P([0-9]{1,3})',values[0]) if len(values)==1 else None
        if not match:return None
        key=tuple(map(int,match.groups()))
        return key if all(n>0 for n in key) else None
    except (TypeError,ValueError,AttributeError):return None


def web_url(value):
    try:
        url=urllib.parse.urlsplit(value)
        return (isinstance(value,str) and url.scheme in ('http','https') and bool(url.hostname)
                and url.username is None and url.password is None and not any(ord(c)<32 for c in value))
    except (TypeError,ValueError):return False


def pin_input(post,root):
    key=pin_key(post)
    if key is None:return None
    directories=[Path(d) for d in glob.glob(str(root/f'nap_*/D{key[0]:02d}_P{key[1]}_*'))
                 if re.fullmatch(r'nap_[0-9]+',Path(d).parent.name)]
    if len(directories)!=1:raise Error('pin_files_invalid')
    path=directories[0]/'pin.txt'
    try:
        if not path.resolve().is_relative_to(Path(root).resolve()):raise ValueError()
        data={}
        for line in path.read_text(encoding='utf-8').splitlines():
            match=re.match(r'^([A-Z_ ()/0-9,a-z]+?):\s*(.*)$',line)
            if match:
                name=match[1].split(' (')[0]
                if name in data:raise ValueError()
                data[name]=match[2]
    except (OSError,ValueError,UnicodeError):raise Error('pin_files_invalid') from None
    try:
        if any(not data.get(k) for k in ('BOARD','DESCRIPTION','ALT TEXT','TITLE','LINK')):raise ValueError()
        if data['BOARD'] not in BOARDS or not web_url(data['LINK']):raise ValueError()
        src=post['assets'][0]['source']
        if not web_url(src):raise ValueError()
        return {'id':post['id'],'text':data['DESCRIPTION'],'saveToDraft':False,'mode':'customScheduled',
                'schedulingType':'automatic','aiAssisted':True,
                'assets':[{'image':{'url':src,'metadata':{'altText':data['ALT TEXT']}}}],
                'metadata':{'pinterest':{'title':data['TITLE'],'url':data['LINK'],'boardServiceId':BOARDS[data['BOARD']]}}}
    except (KeyError,IndexError,TypeError,ValueError):raise Error('pin_metadata_invalid') from None


def read_state(store,today):
    try:value=json.loads((store/STATE).read_text(encoding='utf-8'))
    except (FileNotFoundError,ValueError,UnicodeError,RecursionError):return {'reported':{}}
    except OSError:raise Error('state_read_failed') from None
    if not isinstance(value,dict) or not isinstance(value.get('reported'),dict):return {'reported':{}}
    reported={}
    for key,val in value['reported'].items():
        if key not in ('error','no_drafts_left','low_drafts') or not isinstance(val,str):continue
        try:
            if datetime.strptime(val,'%Y-%m-%d').date().isoformat()==val and val<=today:reported[key]=val
        except ValueError:pass
    return {'reported':reported}


def empty_result(status='error'):
    return {'status':status,'scheduled_count':None,'added':0,'next_due_utc':None,
            'drafts_left':None,'days_of_drafts_left':None}


class TopUp:
    def __init__(self,store,now=None,dry=False,buffer=None,notifier=None,pin_root=PIN_ROOT,dashboard_url='http://localhost:3420'):
        self.store=Path(store);self.now=now or now_utc();self.dry=dry;self.pin_root=Path(pin_root)
        if not isinstance(self.now,datetime) or self.now.tzinfo is None:raise Error('invalid_now')
        self.buffer=buffer or Buffer(self.store)
        self.notifier=notifier or common.Dashboard(self.store,dashboard_url)
        self.state=None;self.result=empty_result();self.reason=None

    def evaluate(self):
        config=load(self.store);day=self.now.astimezone(ET).date()
        if paused(config,day):
            self.result=empty_result('paused');self.result['reason']='schedule_paused'
            return
        scheduled=self.buffer.fetch('scheduled')
        drafts=self.buffer.fetch('draft')
        due=[parse_due(p.get('dueAt')) for p in scheduled]
        counts={}
        for instant in due:
            key=instant.astimezone(ET).date().isoformat();counts[key]=counts.get(key,0)+1
        ids=[p['id'] for p in scheduled+drafts]
        if len(ids)!=len(set(ids)):raise Error('buffer_invalid_response')
        approved=[];keys=set()
        for post in sorted(drafts,key=lambda p:pin_key(p) or (99999,99999)):
            key=pin_key(post)
            if key is None:continue
            if key in keys:raise Error('pin_metadata_invalid')
            keys.add(key);approved.append(post)
        n=len(scheduled);remaining=len(approved)
        self.result.update(scheduled_count=n,drafts_left=remaining,
            next_due_utc=min((d for d in due if d>=self.now),default=None),
            days_of_drafts_left=estimate(remaining,config,day))
        if n>=LIMIT:
            self.result['status']='queue_full'
        else:
            last=max([self.now+timedelta(minutes=15),*due])
            plan=[]
            for post in approved[:LIMIT-n]:
                inp=pin_input(post,self.pin_root)
                try:when=next_slot(last,config,counts,start_day=day)
                except Error as exc:
                    if str(exc)!='no_slot_within_14_days' or not plan:raise
                    self.result['reason']='no_slot_within_14_days';break
                inp['dueAt']=when.strftime('%Y-%m-%dT%H:%M:%S.000Z')
                plan.append((inp,when));last=when
                key=when.astimezone(ET).date().isoformat();counts[key]=counts.get(key,0)+1
            # Whole local plan is validated before the first remote mutation.
            for inp,when in plan:
                if not self.dry:self.buffer.edit(inp)
                due.append(when);n+=1;remaining-=1
                self.result.update(scheduled_count=n,added=self.result['added']+1,drafts_left=remaining,
                    next_due_utc=min(d for d in due if d>=self.now).isoformat(),
                    days_of_drafts_left=estimate(remaining,config,day))
            self.result['status']='no_drafts_left' if n<LIMIT and remaining==0 else 'scheduled'
        self.result['days_of_drafts_left']=estimate(remaining,config,day)
        next_due=min((d for d in due if d>=self.now),default=None)
        self.result['next_due_utc']=next_due.isoformat() if next_due is not None else None
        if self.result['status']=='no_drafts_left':self.reason='no_drafts_left'
        elif self.result['days_of_drafts_left'] is not None and self.result['days_of_drafts_left']<2:
            self.reason='low_drafts'

    def notify(self):
        if self.dry or self.reason is None:return
        today=self.now.astimezone(BUDAPEST).date().isoformat()
        if self.state['reported'].get(self.reason,'')>=today:return
        texts={'error':'[PINTEREST] Hiba a Buffer feltoltesben. Ellenorizd a naplo fix hibakodjat.',
               'no_drafts_left':'[PINTEREST] Elfogytak a feltoltheto jovahagyott vazlatok. Uj nap jovahagyasa szukseges.',
               'low_drafts':'[PINTEREST] A jovahagyott vazlatok becsult keszlete 2 napnal kevesebb. Kerj uj napot jovahagyasra.'}
        if self.notifier.send(texts[self.reason]) is not True:raise Error('dashboard_invalid_response')
        self.state['reported'][self.reason]=today
        common.atomic_state(self.store/STATE,self.state)

    def execute(self):
        self.state=read_state(self.store,self.now.astimezone(BUDAPEST).date().isoformat())
        try:self.evaluate()
        except Exception as exc:
            self.result.update(status='error',error=safe(exc));self.reason='error'
        try:self.notify()
        except Exception as exc:self.result.update(status='error',notification_error=safe(exc))
        self.result['dry_run']=self.dry
        # Partial failure may have left an internal datetime in the initial result.
        if isinstance(self.result.get('next_due_utc'),datetime):self.result['next_due_utc']=self.result['next_due_utc'].isoformat()
        return self.result

    def run(self):
        if self.dry:return self.execute()
        try:
            with lock(self.store):return self.execute()
        except Error as exc:
            if str(exc)=='busy':
                result=empty_result('paused');result['reason']='busy';return result
            raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--state-dir',type=Path,default=ROOT/'store')
    parser.add_argument('--now')
    parser.add_argument('--dashboard-url',default='http://localhost:3420')
    args=parser.parse_args(argv)
    try:result=TopUp(args.state_dir,now_utc(args.now),args.dry_run,dashboard_url=args.dashboard_url).run()
    except Exception as exc:result=empty_result();result['error']=safe(exc)
    print(json.dumps(result,ensure_ascii=True,allow_nan=False))
    if result['status']=='error':
        print('[PINTEREST] '+result.get('error',result.get('notification_error','buffer_error')),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':sys.exit(main())

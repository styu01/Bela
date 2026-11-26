#!/usr/bin/env python3
"""Send the fixed morning research request, with Budapest-day deduplication."""
import sys
sys.dont_write_bytecode = True
import argparse
import fcntl
import importlib.util
import json
import os
import re
import stat
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location('morning_kanban_common', ROOT/'scripts/kanban-audit.py')
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)
Error = common.AuditError
STATE = 'reggeli-kutatas-state.json'
BUDAPEST = ZoneInfo('Europe/Budapest')
RECIPIENT = 'codex/programozo'
MAX_ID = 2**63-1
CUTOFF_HOUR = 6
CODES = {'invalid_now','invalid_dashboard_url','state_read_failed','state_write_failed','lock_failed',
         'clock_rollback','dashboard_credentials_unavailable','dashboard_http_error',
         'dashboard_unavailable','dashboard_invalid_response','dispatch_failed'}
TEMPLATE = (
    'FELADAT: harom kutatasi szekcio a mai reggeli napindito Telegram-uzenetehez. A valasz TOMOR, '
    'strukturalt, harom kulon szekcio, egyertelmuen elvalasztva, forrasokkal (URL). Csak nyilvanos '
    'webes keresest hasznalj, ne irj fajlt, ne csinalj semmi mast.\n\n'
    '1) AI HIREK: a tegnapi ({YESTERDAY}) legfontosabb AI-hirek, roviden osszefoglalva, forrassal. Max 5 tetel.\n'
    '2) WEBDESIGN UJDONSAGOK: keress a web design tools animations 2026 es beautiful website templates CSS animations '
    'temaban. Max 2-3 valodi, friss ujdonsag/eszkoz, ami vizualisan szep, animalt weboldalak gyartasat segiti. '
    'Mindegyikhez: nev, mit tud, link.\n'
    '3) AI TIPPEK: keress a Claude Code tips tricks 2026 es ChatGPT productivity features 2026 temaban. '
    'Max 2-3 konkret, actionable tipp/trukk/funkcio.\n\n'
    'Ha valamelyik szekciohoz nem talalsz friss, megbizhato anyagot, ird oda hogy nincs friss talalat, '
    'ne talalj ki semmit. Magyarul valaszolj.'
)


def receipt(value):
    return type(value) is int and 0 < value <= MAX_ID


def dates(now):
    try:
        if not common.epoch(now):
            raise ValueError()
        local = datetime.fromtimestamp(now,BUDAPEST)
        return local,(local.date()-timedelta(days=1)).isoformat()
    except (ValueError,OverflowError,OSError):
        raise Error('invalid_now') from None


def read_state(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError,ValueError,UnicodeError,RecursionError):
        return None
    except OSError:
        raise Error('state_read_failed') from None
    if not isinstance(value,dict) or not receipt(value.get('message_id')):
        return None
    last = value.get('last_sent_date')
    if not isinstance(last,str) or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',last) is None:
        return None
    try:
        if date.fromisoformat(last).isoformat() != last:
            return None
    except ValueError:
        return None
    return {'last_sent_date':last,'message_id':value['message_id']}


class Dashboard(common.Dashboard):
    def send(self,content):
        try:
            token = (self.store/'.dashboard-token').read_text(encoding='utf-8').strip()
            if not token or any(ord(c)<33 or ord(c)>126 for c in token):
                raise ValueError()
        except (OSError,ValueError,UnicodeError):
            raise Error('dashboard_credentials_unavailable') from None
        request = urllib.request.Request(self.endpoint,
            json.dumps({'from':'bela','to':RECIPIENT,'content':content}).encode('utf-8'),
            {'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
        try:
            with self.opener.open(request,timeout=10) as response:
                raw = response.read(65537)
                if response.status != 200:
                    raise Error('dashboard_http_error')
        except urllib.error.HTTPError as exc:
            exc.close()
            raise Error('dashboard_http_error') from None
        except Error:
            raise
        except Exception:
            raise Error('dashboard_unavailable') from None
        try:
            body = json.loads(raw)
            ident = body.get('id') if isinstance(body,dict) else None
            if len(raw)>65536 or not receipt(ident):
                raise ValueError()
        except (ValueError,TypeError,UnicodeError,RecursionError):
            raise Error('dashboard_invalid_response') from None
        return ident  # Queued message id, not proof of downstream consumption.


class Dispatcher:
    def __init__(self,store,now=None,dry=False,force=False,dashboard_url='http://localhost:3420',client=None):
        self.store,self.now,self.dry,self.force = Path(store),now,dry,force
        if now is not None:
            dates(now)
        common.dashboard_endpoint(dashboard_url)
        self.client = client if client is not None else Dashboard(self.store,dashboard_url)

    def evaluate(self):
        # Read the real clock under the lock. An explicit --now is a test clock.
        local,yesterday = dates(self.now if self.now is not None else common.parse_now(None))
        today = local.date().isoformat()
        state = read_state(self.store/STATE)
        if state is not None and state['last_sent_date']>today:
            raise Error('clock_rollback')
        result = {'status':'dry_run' if self.dry else 'sent','date':today,'yesterday':yesterday,'sent':False}
        if state is not None and state['last_sent_date']==today and not self.force:
            result.update(status='already_sent',message_id=state['message_id'])
            return result
        # Persistent=false does not protect a manually late/suspended service.
        # --force overrides only day deduplication, never the morning deadline.
        if local.hour>=CUTOFF_HOUR:
            result['status']='expired'
            return result
        content = TEMPLATE.format(YESTERDAY=yesterday)
        if self.dry:
            result.update(would_send=True,to=RECIPIENT,content=content)
            return result
        if self.now is None:
            # State I/O or a suspended process can outlive the morning window.
            latest,latest_yesterday = dates(common.parse_now(None))
            if latest.date()!=local.date() or latest.hour>=CUTOFF_HOUR:
                result.update(status='expired',date=latest.date().isoformat(),yesterday=latest_yesterday)
                return result
        ident = self.client.send(content)
        if not receipt(ident):
            raise Error('dashboard_invalid_response')
        common.atomic_state(self.store/STATE,{'last_sent_date':today,'message_id':ident})
        result.update(sent=True,message_id=ident)
        return result

    def run(self):
        if self.dry:
            return self.evaluate()
        try:
            self.store.mkdir(parents=True,exist_ok=True)
            fd = os.open(self.store/'.reggeli-kutatas.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
            with os.fdopen(fd,'r+') as lock:
                if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                    raise Error('lock_failed')
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    return {'status':'busy','sent':False}
                return self.evaluate()
        except OSError:
            raise Error('lock_failed') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--force',action='store_true')
    parser.add_argument('--now',help='Unix seconds or timezone-qualified ISO time')
    parser.add_argument('--state-dir',type=Path,default=ROOT/'store')
    parser.add_argument('--dashboard-url',default='http://localhost:3420')
    args = parser.parse_args(argv)
    try:
        now = common.parse_now(args.now) if args.now is not None else None
        result = Dispatcher(args.state_dir,now,args.dry_run,args.force,args.dashboard_url).run()
    except Exception as exc:
        code = str(exc) if isinstance(exc,Error) and str(exc) in CODES else 'dispatch_failed'
        result = {'status':'error','error':code,'sent':False}
    print(json.dumps(result,ensure_ascii=True,allow_nan=False))
    if result['status']=='error':
        print('[REGGELI-KUTATAS] '+result['error'],file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    sys.exit(main())

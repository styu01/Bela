#!/usr/bin/env python3
"""Operator acknowledgements of inspected, immutable inventory parse errors."""
import sys
sys.dont_write_bytecode=True
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import stat
from datetime import datetime,timezone
from pathlib import Path

_spec=importlib.util.spec_from_file_location('supply_ack_scan',Path(__file__).with_name('supply-chain-scan.py'))
scan=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(scan)
WARNING='Parse hiba utan a fajl tovabbi resze nem lett kiertekelve; a hiba elotti rekordok igen. Az ack nem igazolja a fajl biztonsagat.'


def execute(args):
    store=args.state_dir
    if args.command=='list':
        items=scan.read_ack(store/scan.ACK)
        # Notes may hold operator text; never echo them as commands or log content.
        return {'status':'ok','acknowledged':[{'path':scan.clean(v['path'],160),'kind':v['kind'],
                'size':v['size'],'sha256':v['sha256'],'acked_at':scan.clean(v['acked_at'],64)} for v in items]}
    prepared=None
    if args.command=='ack':
        path,kind,raw,info=scan.inspect_issue(args.path)
        if len(args.note)>1000:raise scan.Error('ack_invalid')
        prepared={'path':str(path),'kind':kind,'size':info.st_size,'sha256':hashlib.sha256(raw).hexdigest(),
                  'note':args.note,'acked_at':datetime.now(timezone.utc).isoformat()}
    store.mkdir(parents=True,exist_ok=True)
    checked,_=scan.open_dir(store);os.close(checked)
    fd=os.open(store/'.supply-chain-scan.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    with os.fdopen(fd,'r+') as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):raise scan.Error('lock_failed')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'busy'}
        if args.command=='clear-pending':
            parent,_=scan.open_dir(store)
            try:
                try:raw,_=scan.named_bytes(parent,scan.STATE)
                except FileNotFoundError:return {'status':'unchanged','cleared_finding_ids':[]}
                try:data=json.loads(raw.decode('utf-8-sig'))
                except (ValueError,UnicodeError,RecursionError):raise scan.Error('state_read_failed') from None
            finally:os.close(parent)
            if not isinstance(data,dict):raise scan.Error('state_read_failed')
            pending=data.pop('pending_notification',None)
            if pending is None:return {'status':'unchanged','cleared_finding_ids':[]}
            keys=pending.get('finding_keys',[]) if isinstance(pending,dict) else []
            ids=[k for k in keys if isinstance(k,str) and re.fullmatch('[0-9a-f]{64}',k)] if isinstance(keys,list) else []
            scan.common.atomic_state(store/scan.STATE,data)
            return {'status':'ok','cleared_finding_ids':ids[:20],'additional_ids':max(0,len(ids)-20)}
        items=scan.read_ack(store/scan.ACK)
        path=Path(os.path.abspath(os.path.expanduser(str(args.path))))
        if args.command=='ack':
            item=prepared;kind=item['kind']
            # Parsing happens before flock; only a bounded byte/hash recheck is inside it.
            parent,_=scan.open_dir(path.parent)
            try:current,current_info=scan.named_bytes(parent,path.name)
            finally:os.close(parent)
            if current_info.st_size!=item['size'] or hashlib.sha256(current).hexdigest()!=item['sha256']:
                raise scan.Error('ack_path_changed')
            same=next((v for v in items if all(v[k]==item[k] for k in ('path','kind','size','sha256'))),None)
            if same is not None:return {'status':'unchanged','kind':kind,'warning':WARNING}
            items=[v for v in items if v['path']!=str(path)]+[item]
            if len(items)>10000:raise scan.Error('ack_invalid')
        else:items=[v for v in items if v['path']!=str(path)]
        scan.common.atomic_state(store/scan.ACK,{'acknowledged':items})
        return {'status':'ok','count':len(items),**({'warning':WARNING} if args.command=='ack' else {})}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,default=scan.ROOT/'store')
    commands=parser.add_subparsers(dest='command',required=True)
    ack=commands.add_parser('ack');ack.add_argument('--path',required=True,type=Path)
    ack.add_argument('--note',default='Operator altal ellenorzott ismert serult vagy ertektelen cache fajl.')
    commands.add_parser('list')
    commands.add_parser('clear-pending')
    remove=commands.add_parser('remove');remove.add_argument('--path',required=True,type=Path)
    args=parser.parse_args(argv)
    try:result=execute(args)
    except Exception as exc:
        code=str(exc) if isinstance(exc,scan.Error) and str(exc) in scan.CODES else 'ack_operation_failed'
        print(json.dumps({'status':'error','error':code}));print('[SUPPLY-CHAIN-ACK] '+code,file=sys.stderr);return 1
    print(json.dumps(result,ensure_ascii=True));return 75 if result['status']=='busy' else 0


if __name__=='__main__':raise SystemExit(main())

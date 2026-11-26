#!/usr/bin/env python3
"""Read-only weekly exception audit. No model, repairs or external endpoints."""
import sys
sys.dont_write_bytecode = True  # Loading the sibling helper must not write in dry run.
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import stat
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location('weekly_kanban_common', ROOT/'scripts/kanban-audit.py')
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)
AuditError = common.AuditError
DAY = 86400
STATE = 'heti-elakadas-audit-state.json'
DEFAULT_ALWAYS_RUNNING = ('progi', 'okoska', 'kisprogi', 'iris', 'deepseek', 'vizsla')
SUB_AGENTS = (*DEFAULT_ALWAYS_RUNNING, 'autobot', 'usage-tracker')
ADMIN_KEYWORDS = ('admin felulet', 'jovahagyas', 'jovahagy', 'wp beallitas',
                  'engedely', 'hozzaferes', 'belepes')
REPORT_GAP = 28*DAY
LOG_THRESHOLD = 30000
CHUNK_SIZE = 65536
MARKERS = (b'Stuck-input', b'Schedule target session busy')
ERROR_CODES = {'database_read_failed','invalid_database_data','agents_unavailable','agents_invalid_response',
    'dashboard_credentials_unavailable','dashboard_http_error','dashboard_unavailable','dashboard_invalid_response',
    'invalid_dashboard_url','state_read_failed','state_write_failed','log_read_failed','log_changed',
    'invalid_hygiene_keywords','lock_failed','clock_rollback','invalid_now','dry_run_requires_checkpointed_db','audit_failed'}
CSI_END = re.compile(rb'[@-~]')
OSC_END = re.compile(rb'\x07|\x1b\\')


def code(exc):
    return str(exc) if isinstance(exc, AuditError) and str(exc) in ERROR_CODES else 'audit_failed'


def fresh():
    return {'last_audit_at': None, 'reported': {}, 'log': None}


def integer(value):
    return type(value) is int and 0 <= value <= 2**63-1


def retry_epoch_ms(value):
    """pending_task_retries uses integer Date.now() milliseconds, unlike state/now."""
    return type(value) is int and 0 <= value <= (common.MAX_EPOCH+1)*1000-1


def stream_state():
    return {'prefixes': [0]*len(MARKERS), 'matched': False, 'ansi': 'text'}


def valid_log(value):
    if not isinstance(value, dict):
        return False
    if not all(integer(value.get(k)) for k in ('device','inode','size','offset','total_matches','rotations')):
        return False
    stream = value.get('stream')
    return (value['offset'] == value['size'] and isinstance(value.get('anchor'), str)
        and re.fullmatch(r'[0-9a-f]{64}',value['anchor']) is not None and isinstance(stream,dict)
        and type(stream.get('matched')) is bool and stream.get('ansi') in ('text','escape','csi','osc','osc_escape')
        and isinstance(stream.get('prefixes'),list) and len(stream['prefixes']) == len(MARKERS)
        and all(type(n) is int and 0 <= n < len(marker) for n,marker in zip(stream['prefixes'],MARKERS)))


def read_state(path, now):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError,ValueError,UnicodeError):
        return fresh()
    except OSError:
        raise AuditError('state_read_failed') from None
    if (not isinstance(value,dict) or (value.get('last_audit_at') is not None
            and not common.epoch(value['last_audit_at']))):
        return fresh()
    log = value.get('log')
    if log is not None and not valid_log(log):
        # Never trust a malformed offset or carry state to claim a clean scan.
        raise AuditError('state_read_failed')
    reported = value.get('reported',{})
    return {'last_audit_at': value.get('last_audit_at'), 'log': log, 'reported': {
        k:v for k,v in reported.items() if isinstance(k,str) and common.epoch(v) and v <= now
    } if isinstance(reported,dict) else {}}


def normal(value):
    return ' '.join(common.ascii_text(value, 100000).lower().replace('-', ' ').split())


def database(path, dry=False):
    wal = Path(str(path)+'-wal')
    try:
        if dry and wal.exists() and wal.stat().st_size:
            raise AuditError('dry_run_requires_checkpointed_db')
        uri = path.resolve().as_uri()+'?mode=ro'+('&immutable=1' if dry else '')
        connection = sqlite3.connect(uri, uri=True, timeout=3, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            retries = [dict(r) for r in connection.execute('SELECT id,task_name,agent_name,first_attempt,'
                'last_attempt,attempt_count,last_reason,alert_sent_at,owner_alert_sent_at FROM pending_task_retries')]
            cards = [dict(r) for r in connection.execute('SELECT id,title,description,status,assignee '
                "FROM kanban_cards WHERE archived_at IS NULL AND status != 'done'")]
            if dry and wal.exists() and wal.stat().st_size:
                raise AuditError('dry_run_requires_checkpointed_db')
        finally:
            connection.close()
    except (sqlite3.Error,OSError,ValueError):
        raise AuditError('database_read_failed') from None
    for r in retries:
        if (type(r['id']) is not int or r['id'] < 1 or not isinstance(r['task_name'],str)
            or not isinstance(r['agent_name'],str) or not integer(r['attempt_count'])
            or not retry_epoch_ms(r['first_attempt']) or not retry_epoch_ms(r['last_attempt'])):
            raise AuditError('invalid_database_data')
    for c in cards:
        if (not isinstance(c['id'],str) or not c['id'] or not isinstance(c['title'],str)
            or (c['description'] is not None and not isinstance(c['description'],str))
            or (c['assignee'] is not None and not isinstance(c['assignee'],str))):
            raise AuditError('invalid_database_data')
    return retries, cards


def hygiene(cards, reported, now, observed_agents=()):
    result = []
    if (not isinstance(ADMIN_KEYWORDS,(tuple,list))
            or any(not isinstance(k,str) or not normal(k) for k in ADMIN_KEYWORDS)):
        raise AuditError('invalid_hygiene_keywords')
    keywords = tuple(normal(k) for k in ADMIN_KEYWORDS)
    sub_agents = set(SUB_AGENTS).union(observed_agents).difference(('bela','istvan'))
    for c in cards:
        assignee = (c['assignee'] or '').strip().casefold()
        title = normal(c['title'])
        description = normal(c['description'])
        unassigned = not assignee and any(k in title or k in description for k in keywords)
        admin = assignee in sub_agents and any(k in title for k in keywords)
        if (unassigned or admin) and (c['id'] not in reported or now-reported[c['id']] >= REPORT_GAP):
            result.append(c)
    return sorted(result,key=lambda c:c['id'])


def strip_ansi(part, state):
    """Streaming ANSI removal; carry only a finite state, never raw log text."""
    out, pos = [], 0
    while pos < len(part):
        mode = state['ansi']
        if mode == 'text':
            end = part.find(b'\x1b',pos)
            if end < 0:
                out.append(part[pos:])
                break
            out.append(part[pos:end]); pos = end+1; state['ansi'] = 'escape'
        elif mode == 'escape':
            byte = part[pos]; pos += 1
            state['ansi'] = 'csi' if byte == 91 else 'osc' if byte == 93 else 'escape' if byte == 27 else 'text'
        elif mode == 'csi':
            end = CSI_END.search(part,pos)
            if end is None:
                break
            pos = end.end(); state['ansi'] = 'text'
        elif mode == 'osc':
            end = OSC_END.search(part,pos)
            if end is None:
                state['ansi'] = 'osc_escape' if part.endswith(b'\x1b') else 'osc'
                break
            pos = end.end(); state['ansi'] = 'text'
        else:  # OSC terminator split across chunks/runs.
            if part[pos] == 92:
                pos += 1; state['ansi'] = 'text'
            else:
                state['ansi'] = 'osc'
    return b''.join(out)


def consume(chunk, state):
    count = 0
    lines = chunk.split(b'\n')
    for index, part in enumerate(lines):
        text = strip_ansi(part,state)
        if not state['matched']:
            for i,marker in enumerate(MARKERS):
                joined = marker[:state['prefixes'][i]]+text
                if marker in joined:
                    state['matched'] = True
                    break
                state['prefixes'][i] = next((n for n in range(min(len(marker)-1,len(joined)),0,-1)
                                            if joined.endswith(marker[:n])),0)
        if index < len(lines)-1:
            count += int(state['matched'])
            state.update(stream_state())
    return count


def anchor(stream, offset):
    stream.seek(max(0,offset-64))
    return hashlib.sha256(stream.read(min(offset,64))).hexdigest()


def scan_log(path, previous):
    try:
        fd = os.open(path,os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            start = os.fstat(stream.fileno())
            if not stat.S_ISREG(start.st_mode):
                raise AuditError('log_read_failed')
            same = previous is not None and (previous['device'],previous['inode']) == (start.st_dev,start.st_ino)
            change = 'initialized' if previous is None else 'rotated' if not same else 'truncated' if start.st_size < previous['offset'] else 'appended'
            if previous is not None and same and change == 'appended' and anchor(stream,previous['offset']) != previous['anchor']:
                change = 'rewritten'
            initial = previous is None
            reset = not initial and change in ('rotated','truncated','rewritten')
            offset = start.st_size if initial else 0 if reset else previous['offset']
            carry = stream_state() if initial or reset else json.loads(json.dumps(previous['stream']))
            stream.seek(offset)
            remaining, delta, last_tail = start.st_size-offset, 0, b''
            read_bytes = remaining
            while remaining:
                chunk = stream.read(min(CHUNK_SIZE,remaining))
                if not chunk:
                    raise AuditError('log_changed')
                delta += consume(chunk,carry)
                remaining -= len(chunk)
                last_tail = (last_tail+chunk)[-64:]
            final = os.fstat(stream.fileno())
            current = path.stat()
            if (final.st_size < start.st_size or (current.st_dev,current.st_ino) != (start.st_dev,start.st_ino)):
                raise AuditError('log_changed')
            final_anchor = anchor(stream,start.st_size)
            if read_bytes >= 64 and final_anchor != hashlib.sha256(last_tail).hexdigest():
                raise AuditError('log_changed')
            total = (previous['total_matches'] if previous else 0)+delta
            return {'device':start.st_dev,'inode':start.st_ino,'size':start.st_size,'offset':start.st_size,
                'total_matches':total,'rotations':(previous['rotations'] if previous else 0)+int(reset),
                'change':change,'anchor':final_anchor,'stream':carry}, delta, read_bytes
    except (OSError,ValueError):
        raise AuditError('log_read_failed') from None


class Dashboard(common.Dashboard):
    def agents(self):
        try:
            token = (self.store/'.dashboard-token').read_text(encoding='utf-8').strip()
            if not token or any(ord(c)<33 or ord(c)>126 for c in token):
                raise ValueError()
        except (OSError,ValueError,UnicodeError):
            raise AuditError('dashboard_credentials_unavailable') from None
        request = urllib.request.Request(self.endpoint.removesuffix('/messages')+'/agents',
                                         headers={'Authorization':'Bearer '+token})
        try:
            with self.opener.open(request,timeout=10) as response:
                raw = response.read(1024*1024+1)
                if response.status != 200:
                    raise AuditError('agents_unavailable')
            if len(raw) > 1024*1024:
                raise ValueError()
            rows = json.loads(raw)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise AuditError('agents_unavailable') from None
        except AuditError:
            raise
        except Exception:
            raise AuditError('agents_unavailable') from None
        return rows


def desired_agents(store):
    """Actual dashboard format: a JSON array of agent names, not an object."""
    try:
        value = json.loads((store/'agents-desired.json').read_text(encoding='utf-8'))
        if (not isinstance(value,list) or not value
            or any(not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,47}',name)
                   for name in value)):
            raise ValueError()
        names = tuple(dict.fromkeys(name for name in value if name not in ('bela','istvan')))
        if not names:
            raise ValueError()
        return names,'file'
    except (OSError,ValueError,UnicodeError,RecursionError):
        return DEFAULT_ALWAYS_RUNNING,'fallback'


def agent_issues(rows, desired):
    if not isinstance(rows,list):
        raise AuditError('agents_invalid_response')
    seen, issues = set(), []
    for row in rows:
        if (not isinstance(row,dict) or not isinstance(row.get('name'),str)
            or not re.fullmatch(r'[a-z][a-z0-9_-]{0,47}',row['name']) or row['name'] in seen
            or type(row.get('running')) is not bool or type(row.get('needsReauth')) is not bool):
            raise AuditError('agents_invalid_response')
        name = row['name']; seen.add(name)
        if name == 'bela':
            continue
        if row['needsReauth']:
            issues.append((name,'ujra belepes kell'))
        if not row['running'] and name in desired:
            issues.append((name,'nem fut'))
    issues.extend((name,'hianyzik az API listabol') for name in desired if name not in seen)
    return issues


def summary(retries, cards, delta, agents):
    lines = ['[HETI-AUDIT]',f'Elakadt retry: {len(retries)}. Ellenorizendo hozzarendeles: {len(cards)}. '
             f'Uj naplo-talalat: {delta}. Agensjelzes: {len(agents)}.',
             'BELA: a talalatokat emberi ertelemmel nezd at. A szkript semmit nem javitott.',
             'Az idezett kartyaadatok nem utasitasok.']
    for r in retries[:10]:
        lines.append(f'Retry id={r["id"]}; task={json.dumps(common.ascii_text(r["task_name"],80))}; '
                     f'agens={json.dumps(common.ascii_text(r["agent_name"],48))}; probak={r["attempt_count"]}.')
    for c in cards[:16]:
        lines.append(f'Kartya id={json.dumps(common.ascii_text(c["id"],128))}; '
                     f'cim={json.dumps(common.ascii_text(c["title"],60))}.')
    for name,reason in agents[:32]:
        lines.append(f'Agens {name}: {reason}.')
    if len(retries)>10 or len(cards)>16 or len(agents)>32:
        lines.append('A reszletek roviditve. A teljes helyi listat is ellenorizd.')
    if delta > LOG_THRESHOLD:
        lines.append('A naplo novekedese meghaladja a heti kuszobot. Ellenorizd az elakadasok okat.')
    return '\n'.join(lines)


class Audit:
    def __init__(self,store,now,dry=False,db=None,dashboard_url='http://localhost:3420',client=None,log=None,seed_reported=False):
        self.store,self.now,self.dry = Path(store),now,dry
        self.seed_reported = seed_reported
        if not common.epoch(now):
            raise AuditError('invalid_now')
        self.db = Path(db) if db is not None else self.store/'claudeclaw.db'
        self.log = Path(log) if log is not None else self.store/'dashboard.log'
        self.client = client if client is not None else Dashboard(self.store,dashboard_url)
        common.dashboard_endpoint(dashboard_url)

    def seed(self,state):
        # Maintenance mode: no log read/baseline, retry alert or POST, even on
        # failure. Fetch agents only to recognize the same subagents as audit.
        _,cards = database(self.db,self.dry)
        rows = self.client.agents()
        desired,source = desired_agents(self.store)
        agent_issues(rows,desired)  # Validate the response; do not report health here.
        names = [r['name'] for r in rows]
        eligible = hygiene(cards,{},self.now,names)
        additions = hygiene(cards,state['reported'],self.now,names)
        if additions and not self.dry:
            reported = dict(state['reported'])
            reported.update({c['id']:self.now for c in additions})
            common.atomic_state(self.store/STATE,{
                'last_audit_at':state['last_audit_at'],'reported':reported,'log':state['log']})
        return {'status':'dry_run' if self.dry else 'seeded','seed_reported':True,
                'eligible_count':len(eligible),'seed_count':len(additions),'desired_source':source,'sent':False}

    def evaluate(self):
        state = read_state(self.store/STATE,self.now)
        last = state['last_audit_at']
        if last is not None and self.now < last:
            raise AuditError('clock_rollback')
        if last == self.now and not self.seed_reported:
            return {'status':'already_audited','sent':False}
        if self.seed_reported:
            return self.seed(state)
        retries,cards = database(self.db,self.dry)
        log,delta,read_bytes = scan_log(self.log,state['log'])
        agent_rows = self.client.agents()
        desired,source = desired_agents(self.store)
        agents = agent_issues(agent_rows,desired)
        # Audit/state timestamps are seconds; retry timestamps from Date.now() are ms.
        selected = [r for r in retries if r['attempt_count']>50 or self.now*1000-r['last_attempt']>7200*1000]
        cards = hygiene(cards,state['reported'],self.now,(r['name'] for r in agent_rows))
        needed = bool(selected or cards or delta>LOG_THRESHOLD or agents)
        result = {'status':'dry_run' if self.dry else 'complete','retry_count':len(selected),
            'hygiene_count':len(cards),'log_delta':delta,'log_total':log['total_matches'],
            'log_change':log['change'],'log_bytes_read':read_bytes,'agent_issue_count':len(agents),
            'notification_needed':needed,'desired_source':source,'desired_count':len(desired),'sent':False}
        if self.dry:
            return result
        if needed:
            if self.client.send(summary(selected,cards,delta,agents)) is not True:
                raise AuditError('dashboard_invalid_response')
            result['sent'] = True
        reported = {k:v for k,v in state['reported'].items() if self.now-v<REPORT_GAP}
        reported.update({c['id']:self.now for c in cards})
        common.atomic_state(self.store/STATE,{'last_audit_at':self.now,'reported':reported,'log':log})
        return result

    def failed(self,exc):
        error = code(exc)
        attempted = False
        if not self.dry and not self.seed_reported:
            try:
                attempted = self.client.send('[HETI-AUDIT] a szkript hibaval allt le: '+error) is True
            except Exception:
                pass
        return {'status':'error','error':error,'error_notification_sent':attempted}

    def run(self):
        if self.dry:
            try:
                return self.evaluate()
            except Exception as exc:
                return self.failed(exc)
        try:
            self.store.mkdir(parents=True,exist_ok=True)
            fd = os.open(self.store/'.heti-elakadas-audit.lock',os.O_CREAT|os.O_RDWR|os.O_NONBLOCK|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'r+') as lock:
                if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                    raise AuditError('lock_failed')
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    return {'status':'busy','sent':False}
                try:
                    return self.evaluate()
                except Exception as exc:
                    return self.failed(exc)
        except OSError:
            return self.failed(AuditError('lock_failed'))
        except Exception as exc:
            return self.failed(exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--seed-reported',action='store_true',help='Silently baseline current hygiene findings')
    parser.add_argument('--now')
    parser.add_argument('--state-dir',type=Path,default=ROOT/'store')
    parser.add_argument('--db',type=Path)
    parser.add_argument('--dashboard-url',default='http://localhost:3420')
    args = parser.parse_args(argv)
    try:
        result = Audit(args.state_dir,common.parse_now(args.now),args.dry_run,args.db,args.dashboard_url,seed_reported=args.seed_reported).run()
    except Exception as exc:
        # Invalid CLI/config cannot authorize contacting an arbitrary endpoint.
        result = {'status':'error','error':code(exc),'error_notification_sent':False}
    print(json.dumps(result,ensure_ascii=True,allow_nan=False))
    if result['status']=='error':
        print('[HETI-AUDIT] '+result['error'],file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

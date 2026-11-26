"""Shared, fail-closed Pinterest scheduling rules. Python stdlib only."""
import fcntl
import importlib.util
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location('pinterest_kanban_common',ROOT/'scripts/kanban-audit.py')
common = importlib.util.module_from_spec(_spec);_spec.loader.exec_module(common)
Error = common.AuditError
ET = ZoneInfo('America/New_York')
BUDAPEST = ZoneInfo('Europe/Budapest')
DAYS = ('mon','tue','wed','thu','fri','sat','sun')
SLOTS_ET = (8,10,12,14,16,19,21)
HORIZON = 14
CONFIG = 'pinterest-schedule.json'


def defaults():
    return {'slots_et_by_weekday':{d:list(SLOTS_ET) for d in DAYS},'skip_dates_et':[],
            'pause_until_et':None,'max_pins_per_day':7}


def valid_date(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',value):
        raise Error('schedule_invalid')
    try:
        if date.fromisoformat(value).isoformat()!=value:raise ValueError()
    except ValueError:raise Error('schedule_invalid') from None
    return value


def validate(value):
    if not isinstance(value,dict) or set(value)!=set(defaults()):raise Error('schedule_invalid')
    days=value['slots_et_by_weekday']
    if not isinstance(days,dict) or set(days)!=set(DAYS):raise Error('schedule_invalid')
    for hours in days.values():
        if (not isinstance(hours,list) or any(type(h) is not int or not 0<=h<=23 for h in hours)
                or hours!=sorted(set(hours))):raise Error('schedule_invalid')
    skips=value['skip_dates_et']
    if not isinstance(skips,list):raise Error('schedule_invalid')
    for item in skips:valid_date(item)
    if len(skips)!=len(set(skips)):raise Error('schedule_invalid')
    if value['pause_until_et'] is not None:valid_date(value['pause_until_et'])
    if type(value['max_pins_per_day']) is not int or not 0<=value['max_pins_per_day']<=24:
        raise Error('schedule_invalid')
    return value


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise Error('schedule_invalid')
        result[key]=value
    return result


def load(store):
    try:
        raw=(Path(store)/CONFIG).read_text(encoding='utf-8')
        return validate(json.loads(raw,object_pairs_hook=unique_object))
    except FileNotFoundError:return defaults()
    except (ValueError,UnicodeError,RecursionError):raise Error('schedule_invalid') from None
    except OSError:raise Error('schedule_read_failed') from None


@contextmanager
def lock(store):
    try:
        Path(store).mkdir(parents=True,exist_ok=True)
        fd=os.open(Path(store)/'.pinterest-buffer.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        stream=os.fdopen(fd,'r+')
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            stream.close();raise Error('lock_failed')
    except OSError:raise Error('lock_failed') from None
    with stream:
        try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Error('busy') from None
        yield


def hours_for(config,day):
    key=day.isoformat()
    if key in config['skip_dates_et'] or (config['pause_until_et'] is not None and key<=config['pause_until_et']):
        return []
    return config['slots_et_by_weekday'][DAYS[day.weekday()]]


def instants(config,day):
    result=[]
    for hour in hours_for(config,day):
        local=datetime.combine(day,time(hour),ET)
        utc=local.astimezone(timezone.utc)
        # A nonexistent spring-forward hour must not silently move to 03:00.
        if utc.astimezone(ET).replace(tzinfo=None)!=local.replace(tzinfo=None):continue
        # The repeated fall-back hour appears once, at its first occurrence.
        result.append(utc)
    return result


def capacity(config,day):
    return min(config['max_pins_per_day'],len(instants(config,day)))


def next_slot(after_utc,config,counts=None,start_day=None):
    if after_utc.tzinfo is None:raise Error('invalid_now')
    start=start_day or after_utc.astimezone(ET).date()
    counts=counts or {}
    for offset in range(HORIZON):
        day=start+timedelta(days=offset)
        if counts.get(day.isoformat(),0)>=config['max_pins_per_day']:continue
        for instant in instants(config,day):
            if instant>after_utc:return instant
    raise Error('no_slot_within_14_days')


def estimate(drafts,config,day):
    total=sum(capacity(config,day+timedelta(days=n)) for n in range(HORIZON))
    return round(drafts*HORIZON/total,3) if total else None


def paused(config,day):
    return (config['max_pins_per_day']==0 or not any(config['slots_et_by_weekday'].values())
            or (config['pause_until_et'] is not None and day.isoformat()<=config['pause_until_et']))


def now_utc(value=None):
    try:return datetime.fromtimestamp(common.parse_now(value),timezone.utc)
    except (ValueError,OverflowError,OSError):raise Error('invalid_now') from None

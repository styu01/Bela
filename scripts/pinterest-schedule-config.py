#!/usr/bin/env python3
"""Edit the Pinterest ET schedule; show never writes files."""
import sys
sys.dont_write_bytecode=True
import argparse
import json
from datetime import timedelta
from pathlib import Path
from pinterest_schedule import ROOT,DAYS,CONFIG,HORIZON,Error,common,defaults,validate,valid_date,load,lock,now_utc,ET,capacity,instants


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,default=ROOT/'store')
    parser.add_argument('--now')
    parser.add_argument('--dry-run',action='store_true')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('show');sub.add_parser('reset')
    day=sub.add_parser('set-day');day.add_argument('day',choices=DAYS);day.add_argument('hours')
    for name in ('skip','unskip','pause-until'):
        command=sub.add_parser(name);command.add_argument('date')
    args=parser.parse_args(argv)
    try:
        now=now_utc(args.now)
        def apply():
            config=defaults() if args.command=='reset' else load(args.state_dir)
            if args.command=='set-day':
                try:hours=[] if args.hours=='off' else [int(h) for h in args.hours.split(',')]
                except ValueError:raise Error('schedule_invalid') from None
                config['slots_et_by_weekday'][args.day]=hours
            elif args.command in ('skip','unskip'):
                value=valid_date(args.date);dates=set(config['skip_dates_et'])
                if args.command=='skip':dates.add(value)
                else:dates.discard(value)
                config['skip_dates_et']=sorted(dates)
            elif args.command=='pause-until':
                config['pause_until_et']=None if args.date=='off' else valid_date(args.date)
            validate(config)
            if args.command=='show':
                print('Pinterest utemezes, America/New_York ido, kovetkezo 14 nap:')
                for n in range(HORIZON):
                    day=now.astimezone(ET).date()+timedelta(days=n)
                    times=[t.astimezone(ET).strftime('%H:%M') for t in instants(config,day)]
                    print(f'{day.isoformat()} {DAYS[day.weekday()]}: {capacity(config,day)} pin; '
                          + (', '.join(times) if times else 'kihagyas')
                          + f'; napi maximum {config["max_pins_per_day"]}')
            elif not args.dry_run:
                common.atomic_state(args.state_dir/CONFIG,config)
            if args.command!='show':print(json.dumps({'status':'dry_run' if args.dry_run else 'saved'}))
        if args.command=='show' or args.dry_run:apply()
        else:
            with lock(args.state_dir):apply()
        return 0
    except Error as exc:
        code=str(exc) if str(exc) in {'schedule_invalid','schedule_read_failed','state_write_failed','lock_failed','busy','invalid_now'} else 'schedule_error'
    except Exception:code='schedule_error'
    print(json.dumps({'status':'error','error':code}));print('[PINTEREST] '+code,file=sys.stderr)
    return 1


if __name__=='__main__':sys.exit(main())

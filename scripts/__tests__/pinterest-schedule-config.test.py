#!/usr/bin/env python3
import importlib.util,io,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(SCRIPTS))
import pinterest_schedule as s
spec=importlib.util.spec_from_file_location('pinterest_cli_tested',SCRIPTS/'pinterest-schedule-config.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)


class CLITests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=Path(self.tmp.name)
    def run_cli(self,*args,dry=False):
        argv=['--state-dir',str(self.store),'--now','2026-11-05T12:00:00Z']+(['--dry-run'] if dry else [])+list(args)
        with patch('sys.stdout',io.StringIO()) as out,patch('sys.stderr',io.StringIO()) as err:code=c.main(argv)
        return code,out.getvalue(),err.getvalue()
    def config(self):return s.load(self.store)
    def test_show_fourteen_days_no_writes(self):
        code,out,err=self.run_cli('show');self.assertEqual(code,0);self.assertEqual(len(out.splitlines()),15)
        self.assertIn('2026-11-05 thu: 7 pin',out);self.assertEqual(list(self.store.iterdir()),[])
    def test_set_day_and_off(self):
        self.assertEqual(self.run_cli('set-day','mon','9,17')[0],0)
        self.assertEqual(self.config()['slots_et_by_weekday']['mon'],[9,17])
        self.assertEqual(self.run_cli('set-day','sat','off')[0],0)
        self.assertEqual(self.config()['slots_et_by_weekday']['sat'],[])
    def test_skip_unskip_idempotent(self):
        for _ in range(2):self.assertEqual(self.run_cli('skip','2026-11-27')[0],0)
        self.assertEqual(self.config()['skip_dates_et'],['2026-11-27'])
        for _ in range(2):self.assertEqual(self.run_cli('unskip','2026-11-27')[0],0)
        self.assertEqual(self.config()['skip_dates_et'],[])
    def test_pause_and_resume(self):
        self.run_cli('pause-until','2026-11-12');self.assertEqual(self.config()['pause_until_et'],'2026-11-12')
        self.run_cli('pause-until','off');self.assertIsNone(self.config()['pause_until_et'])
    def test_reset_repairs_corrupt_config_explicitly(self):
        (self.store/s.CONFIG).write_text('corrupt');self.assertEqual(self.run_cli('show')[0],1)
        self.assertEqual(self.run_cli('set-day','mon','off')[0],1)
        self.assertEqual(self.run_cli('reset')[0],0);self.assertEqual(self.config(),s.defaults())
    def test_invalid_values_leave_file_unchanged(self):
        self.run_cli('reset');before=(self.store/s.CONFIG).read_bytes()
        for args in (('set-day','mon','9,8'),('set-day','mon','8,8'),('set-day','mon','24'),('skip','2026-02-29')):
            self.assertEqual(self.run_cli(*args)[0],1);self.assertEqual((self.store/s.CONFIG).read_bytes(),before)
    def test_dry_run_no_writes(self):
        self.assertEqual(self.run_cli('set-day','mon','off',dry=True)[0],0)
        self.assertEqual(list(self.store.iterdir()),[])
    def test_atomic_replace_failure_keeps_old_config(self):
        self.run_cli('reset');before=(self.store/s.CONFIG).read_bytes()
        with patch.object(s.common.os,'replace',side_effect=OSError('SECRET')):code,out,err=self.run_cli('set-day','mon','off')
        self.assertEqual(code,1);self.assertNotIn('SECRET',out+err);self.assertEqual((self.store/s.CONFIG).read_bytes(),before)
        self.assertEqual(list(self.store.glob('*.tmp')),[])
    def test_shared_lock_blocks_configuration_change(self):
        with s.lock(self.store):self.assertEqual(self.run_cli('set-day','mon','off')[0],1)
        self.assertFalse((self.store/s.CONFIG).exists())
    def test_show_skip_and_off(self):
        self.run_cli('set-day','sat','off');self.run_cli('skip','2026-11-06')
        code,out,_=self.run_cli('show');self.assertEqual(code,0)
        self.assertIn('2026-11-06 fri: 0 pin; kihagyas',out);self.assertIn('2026-11-07 sat: 0 pin; kihagyas',out)


if __name__=='__main__':unittest.main()

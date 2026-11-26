#!/usr/bin/env python3
"""Run the isolated review suites using installed dependencies only."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
COMMANDS=[['python3','-B','scripts/__tests__/'+name] for name in (
    'usage-fleet-monitor.test.py','usage-check.test.py','usage-thresholds.test.py',
    'test_codex_usage.py','gemini-usage-collect.test.py','usage-collect.test.py',
    'usage-monitor-shortcircuit.test.py','statusline-usage-export.test.py')]
VITEST=ROOT/'node_modules/vitest/vitest.mjs'
if not VITEST.is_file():VITEST=ROOT.parents[1]/'node_modules/vitest/vitest.mjs'
COMMANDS.append(['node',os.path.relpath(VITEST,ROOT),'run',
    'src/__tests__/quota-gate.test.ts','src/__tests__/quota-gate-wiring.test.ts',
    'src/__tests__/usage-fleet-pause.test.ts','src/__tests__/message-router-usage-pause.test.ts',
    '--maxWorkers=1','--minWorkers=1'])

def main():
    if any((ROOT/'store'/name).exists() for name in ('.dashboard-token','claudeclaw.db','.claude-oauth-token')):
        print('REFUSING: live runtime markers present; use an isolated copy.',file=sys.stderr)
        return 2
    environment={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','NO_COLOR':'1','FORCE_COLOR':'0'}
    failed=False
    with (ROOT/'test-results.txt').open('w',encoding='utf-8') as log:
        log.write('Working directory: '+str(ROOT)+'\n')
        for command in COMMANDS:
            result=subprocess.run(command,cwd=ROOT,env=environment,text=True,
                                  stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=120)
            log.write('\n$ '+shlex.join(command)+'\n'+result.stdout+'\nExit code: '+str(result.returncode)+'\n')
            log.flush()
            print(shlex.join(command)+': exit '+str(result.returncode),flush=True)
            failed=failed or result.returncode!=0
    return int(failed)

if __name__=='__main__':sys.exit(main())

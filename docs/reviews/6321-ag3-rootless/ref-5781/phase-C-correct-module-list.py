"""Correct only the rejected test-list syntax; run the real SQL/doctor checks.

The first part rejected the list before importing any test. A/B and the
successful negative permission checks are never repeated.
"""
import datetime,hashlib,importlib.util,json,os,re,stat,time
from pathlib import Path
BASE=Path('/root/ameesh-operator-5781-AG1-20261104')
os.umask(0o077)
assert os.geteuid()==0 and os.uname().nodename=='ameesh-agents-1'
p=BASE/'phase-C2-authority.json';st=p.lstat();assert stat.S_ISREG(st.st_mode) and st.st_uid==0 and stat.S_IMODE(st.st_mode)==0o600
authority=json.loads(p.read_text());assert 0<=time.time()-authority['verified_epoch']<600
assert (authority.get('native_AG1_drain_active') is True or authority.get('AG1_lost_gate_closed_drain_intent') is True) and authority['no_active_turns'] is True
assert time.time()<1793867400 and not (BASE/'phase-C2-progress.json').exists()
helper=BASE/'phase-A-engine-and-PG.py';assert hashlib.sha256(helper.read_bytes()).hexdigest()=='d5b505109dc05a8820e28225354863c16444e0bd5d33e472b97a6a4b56811821'
spec=importlib.util.spec_from_file_location('reviewed_phase_A',helper);h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
record=json.loads((BASE/'phase-C-progress.json').read_text())
assert record['phase']=='qualified release source extracted as UID1001 for one real SQL module'
assert record['no_sudo_allow_rule'] and record['protected_bind_read_denied']
assert json.loads((BASE/'phase-B-progress.json').read_text())['phase'].startswith('phase B completed;')
assert h.inspect(h.RL,'ameesh-pg')['State']['Health']['Status']=='healthy'
assert record['test_source_sha']=='d5a7ba45284fbc73b272d9b430afd0c37e5a11bc'
workdir=record['test_workdir'];assert re.fullmatch(r'/home/ameesh/\.local/state/ameesh/operators/5781-sql-[A-Za-z0-9_-]+',workdir)
assert (BASE/'phase-C-SQL.private.log').read_text()=='1 base(s) prête(s) ; journaux dans /tmp/ameesh-parts-G8EPIQ\n'
assert Path('/tmp/ameesh-parts-G8EPIQ/psql-1.log').read_text()==workdir+'/.operator-5781-modules.txt : module(s) inconnu(s) : tests.test_work\n'
old=h.command(['systemctl','show','ameesh-operator-5781-phase-C-20261105.service','-p','ActiveState','--value']).stdout.strip()
assert old==b'failed'
live=h.user(['systemctl','--user','show','ameesh-operator-5781-real-sql.service','-p','MainPID','--value']);assert live in ('','0')
def save(phase,**fields):
 record.update(fields,phase=phase,at=h.stamp());h.write_private(BASE/'phase-C2-progress.json',(json.dumps(record,indent=2)+'\n').encode())
save('correcting rejected bare module-list entry; no test previously imported',first_attempt_tests_executed=0)
fix="import pathlib,sys,stat; p=pathlib.Path(sys.argv[1])/'.operator-5781-modules.txt'; assert p.lstat().st_uid==1001 and stat.S_ISREG(p.lstat().st_mode); assert p.read_text()=='tests.test_work\\n'; p.write_text('test_work\\n'); p.chmod(0o600)"
h.user(['/usr/bin/python3','-I','-c',fix,workdir])
def unit(suffix,args,timeout):
 return h.command(h.user_args(['systemd-run','--user','--pipe','--wait','--collect','--quiet','--unit=ameesh-operator-5781-'+suffix,'--property=WorkingDirectory='+workdir,'--property=EnvironmentFile=/home/ameesh/.config/ameesh/env','--setenv=PATH=/home/ameesh/.local/share/ameesh/venv/bin:/usr/local/bin:/usr/bin:/bin',*args]),timeout=timeout,allow_error=True)
sql=unit('real-sql-correct-list',['/bin/bash',workdir+'/scripts/test-parallele.sh','-n','1','-p','psql','--liste',workdir+'/.operator-5781-modules.txt'],1800)
h.write_private(BASE/'phase-C2-SQL.private.log',sql.stdout+sql.stderr)
assert sql.returncode==0 and b'base(s)' in sql.stdout and b'OK' in sql.stdout,'Actual SQL part failed; inspect existing part logs, never replay blindly'
save('real SQL module passed under effective rootless user-unit environment',real_SQL_psql_passed=True,SQL_log='phase-C2-SQL.private.log')
binary='/home/ameesh/.local/share/ameesh/venv/bin/ameesh'
doctor=unit('doctor-after-sql',[binary,'doctor'],180);h.write_private(BASE/'phase-C2-doctor.private.log',doctor.stdout+doctor.stderr)
assert doctor.returncode==0,'Native doctor failed: drainage remains active'
housekeeping=unit('menage-readonly-after-sql',[binary,'menage','--dry-run','--json'],600);h.write_private(BASE/'phase-C2-menage.private.json',housekeeping.stdout)
assert housekeeping.returncode==0,'Native housekeeping failed: drainage remains active'
json.loads(housekeeping.stdout)
save('local checks passed; native mail-turn, timer restoration and independent attestation pending',doctor_passed=True,housekeeping_dryrun_passed=True,rootless_PG_healthy=True)
print(json.dumps(record))

"""Finish the already active native rootless engine, then the reviewed PG tail.

No symlink, no installer replay, no CLI context creation or switch.
"""
import ast,hashlib,importlib.util,json,os,stat,time
from pathlib import Path
BASE=Path('/root/ameesh-operator-5781-AG1-20261104')
ORIGINAL=BASE/'phase-A-engine-and-PG.py'
EXPECTED='d5b505109dc05a8820e28225354863c16444e0bd5d33e472b97a6a4b56811821'
os.umask(0o077)
assert os.geteuid()==0 and os.uname().nodename=='ameesh-agents-1'
assert not BASE.is_symlink() and BASE.stat().st_uid==0 and stat.S_IMODE(BASE.stat().st_mode)==0o700
assert not ORIGINAL.is_symlink() and ORIGINAL.stat().st_uid==0 and hashlib.sha256(ORIGINAL.read_bytes()).hexdigest()==EXPECTED
p=BASE/'phase-A-resume2-authority.json';st=p.lstat()
assert stat.S_ISREG(st.st_mode) and st.st_uid==0 and stat.S_IMODE(st.st_mode)==0o600
proof=json.loads(p.read_text())
for k in ['native_AG1_drain_active','no_active_turns','roles_have_other_candidates','no_unapproved_personal_session','bounded_resume_reviewed']:assert proof.get(k) is True,k
assert 0<=time.time()-proof['verified_epoch']<600 and time.time()<1793867400
assert not (BASE/'phase-A-resume2-started.json').exists()
prior=json.loads((BASE/'phase-A-progress.json').read_text())
assert prior['phase']=='failed bounded continuation; preserve drainage and reconcile actual phase' and prior['error']=='runuser rc=127'
assert not prior.get('source_stopped') and not prior.get('target_container')
assert (BASE/'last-command-error.private').read_text().endswith('/usr/local/bin/dockerd-rootless-setuptool.sh: 377: /usr/local/bin/docker: not found\n')
assert Path('/run/user/1001/docker.sock').exists() and not Path('/home/ameesh-staging/.ssh/id_ed25519').exists()
assert not Path('/usr/local/bin/docker').exists() and not Path('/usr/local/bin/docker').is_symlink()
spec=importlib.util.spec_from_file_location('reviewed_phase_A',ORIGINAL);h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
source=h.inspect(h.RF,'ameesh-pg')
assert source['Id']==prior['source_container'] and source['Image']==prior['source_image']
assert source['State']['Running'] and source['State']['Health']['Status']=='healthy'
assert source['HostConfig']['PortBindings']=={'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':'55432'}]}
mounts=[m for m in source['Mounts'] if m['Destination']=='/var/lib/postgresql/data']
assert len(source['Mounts'])==len(mounts)==1 and mounts[0]['Type']=='volume' and mounts[0]['Name']==prior['source_volume']
env=dict(v.split('=',1) for v in source['Config']['Env'] if '=' in v)
assert source['Config']['Entrypoint']==['docker-entrypoint.sh'] and env.get('PGDATA','/var/lib/postgresql/data')=='/var/lib/postgresql/data'
assert env.get('POSTGRES_USER','postgres')==prior['role']
assert 'masked' in h.user(['systemctl','--user','show','ameesh-paquet-horaire.timer','-p','UnitFileState'])
assert 'ActiveState=inactive' in h.user(['systemctl','--user','show','ameesh-paquet-horaire.service','-p','ActiveState'])
assert h.user(['systemctl','--user','show','docker.service','-p','ActiveState','--value'])=='active'
assert h.user(['systemctl','--user','show','docker.service','-p','UnitFileState','--value'])=='disabled'
assert h.user(['systemctl','--user','show','docker.service','-p','MainPID','--value'])=='593660'
info=json.loads(h.docker(h.RL,'info','--format','{{json .}}').stdout)
assert any('rootless' in x for x in info['SecurityOptions']) and info['Driver']!='vfs'
assert info['DockerRootDir']=='/home/ameesh/.local/share/docker' and info['Containers']==info['Images']==0
manifest=json.loads((BASE/'verified-public-inputs/L2-rootless-review-20261104/apt/offline-verification.json').read_text())
for name in ['/usr/bin/docker','/usr/bin/dockerd','/usr/bin/containerd','/usr/bin/runc','/usr/bin/rootlesskit']:
 p=Path(name);st=p.lstat();assert stat.S_ISREG(st.st_mode) and st.st_uid==st.st_gid==0 and not st.st_mode&0o022
 assert hashlib.sha256(p.read_bytes()).hexdigest()==manifest['expected_programs'][name]['sha256']
for n,digest in {'dockerd-rootless-setuptool.sh':'e68ef6550fea1b5c01b5cc7a400a6b44ee28159d7714758c3845759e03e079dc','dockerd-rootless.sh':'8eb00d61b5e78ffc0dcd628f056389b5859cc61d6fe3ce17b4a3a4b08143c8a5'}.items():
 p=Path('/usr/local/bin')/n;st=p.lstat();assert stat.S_ISREG(st.st_mode) and st.st_uid==st.st_gid==0 and st.st_nlink==1 and stat.S_IMODE(st.st_mode)==0o755 and hashlib.sha256(p.read_bytes()).hexdigest()==digest
for n in ['home-config-before.private.json','user-units-before-enabled.private.json','user-units-before-active.private.json','docker-context-before.json']:
 st=(BASE/n).lstat();assert stat.S_ISREG(st.st_mode) and st.st_uid==0 and stat.S_IMODE(st.st_mode)==0o600
assert h.user(['/usr/bin/docker','context','show'])==(BASE/'docker-context-before.json').read_text()
h.write_private(BASE/'phase-A-failed-missing-client.private.json',json.dumps(prior,indent=2).encode())
h.write_private(BASE/'phase-A-resume2-started.json',json.dumps({'at':h.stamp(),'review':proof.get('reviews'),'action':'native enable existing engine only; installer isolated context deliberately unused','original_sha':EXPECTED}).encode())
h.record.update(prior)
for k in ['error','error_type','no_blind_replay']:h.record.pop(k,None)
try:
 h.user(['systemctl','--user','enable','docker.service'])
 assert h.user(['systemctl','--user','show','docker.service','-p','UnitFileState','--value'])=='enabled'
 assert h.user(['systemctl','--user','show','docker.service','-p','MainPID','--value'])=='593660'
 assert h.user(['/usr/bin/docker','context','show'])==(BASE/'docker-context-before.json').read_text()
 h.progress('existing native rootless engine enabled, ordinary context unchanged; reviewed PG tail next')
 tree=ast.parse(ORIGINAL.read_text(),filename=str(ORIGINAL));main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
 starts=[i for i,n in enumerate(main.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='info' for t in n.targets)];assert len(starts)==1
 tail=ast.Module(body=main.body[starts[0]:],type_ignores=[]);ast.fix_missing_locations(tail)
 exec(compile(tail,str(ORIGINAL)+'[reviewed-tail]','exec'),dict(h.__dict__,source=source,mounts=mounts,source_volume=prior['source_volume'],role=prior['role']))
except BaseException as exc:
 h.progress('failed continuation2; preserve drainage and reconcile actual phase',error_type=type(exc).__name__,error=str(exc),no_blind_replay=True)
 raise

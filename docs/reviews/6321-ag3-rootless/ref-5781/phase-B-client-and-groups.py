"""AG1 reviewed phase B; root controller, all home writes performed as UID1001.

Not a quiescence bypass. Requires fresh independently collected authority and a
successful phase A. Never generates a deployment key or activates L2.
"""
import argparse
import datetime
import grp
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import stat
import time

ROOT = Path('/root/ameesh-operator-5781-AG1-20261104')


def load_helpers():
    path = ROOT / 'phase-A-engine-and-PG.py'
    assert path.stat().st_uid == 0 and not path.is_symlink()
    spec = importlib.util.spec_from_file_location('ag1_phase_A', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def processes():
    rows = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            status = dict(line.split(':', 1) for line in (entry / 'status').read_text().splitlines() if ':' in line)
            if int(status['Uid'].split()[0]) != 1001:
                continue
            rows.append({'pid': int(entry.name), 'ppid': int(status['PPid']),
                         'name': status['Name'].strip(),
                         'groups': [int(x) for x in status.get('Groups', '').split()],
                         'cgroup': (entry / 'cgroup').read_text().strip()})
        except (FileNotFoundError, ProcessLookupError):
            continue
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', required=True)
    parser.parse_args()
    assert os.geteuid() == 0 and os.uname().nodename == 'ameesh-agents-1'
    assert not ROOT.is_symlink() and ROOT.stat().st_uid == 0 and stat.S_IMODE(ROOT.stat().st_mode) == 0o700
    h = load_helpers()
    receipt_path = ROOT / 'phase-B-progress.json'
    assert not receipt_path.exists(), 'Prior attempt; reconcile actual manager/PG before retry'
    a = json.loads((ROOT / 'phase-A-progress.json').read_text())
    assert a['phase'] == 'completed phase A; group/client/manager still unchanged'
    assert h.inspect(h.RL, 'ameesh-pg')['State']['Health']['Status'] == 'healthy'
    proof_path = ROOT / 'phase-B-authority.json'
    assert not proof_path.is_symlink() and proof_path.stat().st_uid == 0 and stat.S_IMODE(proof_path.stat().st_mode) == 0o600
    proof = json.loads(proof_path.read_text())
    for key in ['native_AG1_drain_active', 'no_active_turns', 'composer_timer_masked',
                'composer_inactive', 'roles_have_other_candidates', 'no_unapproved_personal_session']:
        assert proof.get(key) is True, 'Missing fresh phase-B prerequisite: ' + key
    assert 0 <= time.time() - proof['verified_epoch'] < 600
    now = datetime.datetime.now(datetime.timezone.utc)
    assert datetime.datetime(2026, 11, 4, 22, tzinfo=datetime.timezone.utc) < now < datetime.datetime(2026, 11, 5, 8, 30, tzinfo=datetime.timezone.utc)
    assert not Path('/home/ameesh-staging/.ssh/id_ed25519').exists()
    assert 'masked' in h.user(['systemctl', '--user', 'show', 'ameesh-paquet-horaire.timer', '-p', 'UnitFileState'])
    assert 'ActiveState=inactive' in h.user(['systemctl', '--user', 'show', 'ameesh-paquet-horaire.service', '-p', 'ActiveState'])
    assert grp.getgrnam('docker').gr_gid == 112 and 'ameesh' in grp.getgrnam('docker').gr_mem
    account = pwd.getpwnam('ameesh')
    assert account.pw_uid == account.pw_gid == 1001
    before = processes()
    outside = [x for x in before if 'user@1001.service/' not in x['cgroup']]
    # The only reviewed outside-manager process is the existing Podman pause.
    assert all(x['pid'] == 3039609 and x['name'] == 'podman pause' for x in outside), 'Unknown outside-manager UID1001 process'
    approved = set(proof['approved_active_units'])
    for row in before:
        if row in outside or '/init.scope' in row['cgroup']:
            continue
        names = re.findall(r'/([^/]+\.(?:service|scope))', row['cgroup'])
        assert any(name in approved for name in names), 'Unapproved UID1001 service/session'
    receipt = {'lot': 5781, 'phase': 'verified phase B prerequisites', 'before_processes': before,
               'docker_group_removed': False, 'manager_restart_requested': False, 'L2_key_created': False}

    def save(phase, **fields):
        receipt.update(fields, phase=phase, at=h.stamp())
        h.write_private(receipt_path, (json.dumps(receipt, indent=2) + '\n').encode())

    save('verified phase B prerequisites')
    # Values are retained only inside this host; the root-only phase-A snapshot
    # holds the exact original bytes. User paths never receive root writes.
    configure = r'''import json,os,pathlib,tempfile
h=pathlib.Path.home();env=h/'.config/ameesh/env';directory=h/'.config/environment.d';directory.mkdir(parents=True,exist_ok=True)
target=directory/'90-ameesh-docker-rootless.conf'
assert not target.exists() and not target.is_symlink(),'New env file exists; reconcile rather than overwrite'
def replace(path):
 raw=path.read_bytes();text=raw.decode();lines=text.splitlines(keepends=True);out=[];found=False
 for line in lines:
  if line.partition('=')[0].strip()=='DOCKER_HOST':
   out.append('DOCKER_HOST=unix:///run/user/1001/docker.sock\n');found=True
  else:out.append(line)
 if not found:
  if out and not out[-1].endswith('\n'):out[-1]+='\n'
  out.append('DOCKER_HOST=unix:///run/user/1001/docker.sock\n')
 fd,tmp=tempfile.mkstemp(prefix='.5781-',dir=path.parent)
 try:
  with os.fdopen(fd,'w') as f:f.write(''.join(out));f.flush();os.fsync(f.fileno())
  os.chmod(tmp,0o600);os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
replace(env)
for path in directory.iterdir():
 if path.is_file() and any(line.partition('=')[0].strip()=='DOCKER_HOST' for line in path.read_text().splitlines()):replace(path)
fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:f.write('DOCKER_HOST=unix:///run/user/1001/docker.sock\n');f.flush();os.fsync(f.fileno())
print(json.dumps({'new_files':['.config/environment.d/90-ameesh-docker-rootless.conf']}))'''
    changed = json.loads(h.user(['/usr/bin/python3', '-I', '-c', configure]))
    save('client environment updated under UID1001', **changed)

    def podman_unit(suffix, args):
        return h.user(['systemd-run', '--user', '--pipe', '--wait', '--collect', '--quiet',
                       '--unit=ameesh-operator-5781-podman-' + suffix,
                       '--property=WorkingDirectory=/home/ameesh',
                       '--setenv=XDG_RUNTIME_DIR=/run/user/1001', '/usr/bin/podman', *args], timeout=120)

    inventory = podman_unit('inventory', ['ps', '-a', '--format', '{{.ID}}'])
    assert not inventory.strip(), 'Podman workload appeared; leave drain and reconcile'
    save('Podman inventory empty, native migrate next')
    podman_unit('migrate', ['system', 'migrate'])
    # Never invoke Podman again before the group change and manager restart.
    after_migrate = processes()
    assert not any(x['pid'] == 3039609 for x in after_migrate)
    assert all('user@1001.service/' in x['cgroup'] for x in after_migrate), 'Outside-manager process remains'
    save('Podman outside pause released; no more Podman before restart')
    h.command(['gpasswd', '--delete', 'ameesh', 'docker'])
    assert 'ameesh' not in grp.getgrnam('docker').gr_mem
    save('docker group removed, approved user manager restart next', docker_group_removed=True)
    save('manager restart requested; reconcile systemd job if observation expires', manager_restart_requested=True)
    h.command(['systemctl', 'restart', 'user@1001.service'], timeout=180)
    assert h.command(['systemctl', 'is-active', 'user@1001.service']).stdout.strip() == b'active'
    for unit in sorted(proof['restore_active_native_units']):
        assert re.fullmatch(r'ameesh-[A-Za-z0-9_.@:-]+\.(?:service|timer)', unit)
        assert unit != 'ameesh-paquet-horaire.timer', 'Composer remains masked during verification'
        current = h.user(['systemctl', '--user', 'show', unit, '-p', 'ActiveState', '--value'])
        if current != 'active':
            h.user(['systemctl', '--user', 'start', unit], timeout=120)
    after = processes()
    assert not any(112 in x['groups'] for x in after), 'Surviving UID1001 process retains docker'
    forbidden = {'sudo', 'wheel', 'admin', 'adm', 'docker', 'lxd', 'libvirt', 'systemd-journal'}
    gids = os.getgrouplist('ameesh', 1001)
    groups = [grp.getgrgid(g).gr_name for g in gids]
    assert not forbidden.intersection(groups)
    deny = r'''import errno,socket,json
s=socket.socket(socket.AF_UNIX)
try:s.connect('/run/docker.sock')
except OSError as e:assert e.errno in (errno.EACCES,errno.EPERM);print(json.dumps({'rootful_socket_denied':True}))
else:raise RuntimeError('Rootful Docker still accessible')
finally:s.close()'''
    assert json.loads(h.user(['/usr/bin/python3', '-I', '-c', deny]))['rootful_socket_denied']
    assert h.inspect(h.RL, 'ameesh-pg')['State']['Health']['Status'] == 'healthy'
    context = h.user(['docker', 'context', 'show'])
    assert context == (ROOT / 'docker-context-before.json').read_text()
    save('phase B completed; meaningful SQL and native mail/doctor/audit still pending',
         after_processes=after, fresh_groups=groups, rootful_socket_denied=True,
         rootless_PG_healthy=True, ordinary_context_unchanged=True,
         timer_restoration_pending=True, native_undrain_pending=True)
    print(json.dumps({k: v for k, v in receipt.items() if k not in ('before_processes', 'after_processes')}))


if __name__ == '__main__':
    main()

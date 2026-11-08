"""Reviewed pre-key rollback, requiring fresh operator reconciliation of writes.

Preserves source and target volumes. Never restores rootful authority while an
AG1 deployment key exists, and never reverse-copies a running PostgreSQL volume.
"""
import argparse
import grp
import importlib.util
import json
import os
from pathlib import Path
import stat
import time

ROOT = Path('/root/ameesh-operator-5781-AG1-20261104')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true', required=True)
    p.parse_args()
    assert os.geteuid() == 0 and os.uname().nodename == 'ameesh-agents-1'
    assert not ROOT.is_symlink() and ROOT.stat().st_uid == 0
    assert not Path('/home/ameesh-staging/.ssh/id_ed25519').exists(), 'Revoke target public lines before any privilege restoration'
    auth = ROOT / 'rollback-authority.json'
    assert not auth.is_symlink() and auth.stat().st_uid == 0 and stat.S_IMODE(auth.stat().st_mode) == 0o600
    authority = json.loads(auth.read_text())
    assert 0 <= time.time() - authority['verified_epoch'] < 600
    for k in ['native_AG1_drain_active', 'no_active_turns', 'composer_timer_masked',
              'no_unapproved_personal_session', 'no_AG1_deployment_key', 'target_writes_reconciled',
              'preserved_rootful_source_is_valid_rollback_base']:
        assert authority.get(k) is True, 'Rollback prerequisite missing: ' + k
    path = ROOT / 'phase-A-engine-and-PG.py'
    assert path.stat().st_uid == 0 and not path.is_symlink()
    spec = importlib.util.spec_from_file_location('phase_a', path)
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    a = json.loads((ROOT / 'phase-A-progress.json').read_text())
    bpath = ROOT / 'phase-B-progress.json'
    b = json.loads(bpath.read_text()) if bpath.exists() else {}
    source = h.inspect(h.RF, 'ameesh-pg')
    assert source['Id'] == a['source_container'] and source['Image'] == a['source_image']
    assert source['HostConfig']['RestartPolicy']['Name'] == a['source_restart_policy'] == 'no'
    assert not (ROOT / 'rollback-progress.json').exists(), 'Prior rollback; reconcile, do not replay'
    record = {'lot': 5781, 'kind': 'pre-key rollback', 'no_key_created': True}

    def save(phase, **fields):
        record.update(fields, phase=phase, at=h.stamp())
        h.write_private(ROOT / 'rollback-progress.json', (json.dumps(record, indent=2) + '\n').encode())

    save('fresh authority and preserved source reconciled')
    # Fail closed if the rootless engine cannot be inspected; an observation
    # failure is never evidence that a container has stopped.
    rl_live = Path('/run/user/1001/docker.sock').exists()
    if rl_live:
        names = h.docker(h.RL, 'ps', '-a', '--format', '{{.Names}}').stdout.decode().splitlines()
        assert set(names) <= {'ameesh-pg'}, 'Unexpected rootless workload; reconcile before rollback'
        if 'ameesh-pg' in names:
            target = h.inspect(h.RL, 'ameesh-pg')
            assert target['Id'] == a.get('target_container'), 'Unrecognized target container'
            if target['State']['Running']:
                h.docker(h.RL, 'stop', '--time=60', 'ameesh-pg', timeout=90)
            assert not h.inspect(h.RL, 'ameesh-pg')['State']['Running']
    state = h.user(['systemctl', '--user', 'show', 'docker.service', '-p', 'LoadState', '--value'])
    if state != 'not-found':
        h.user(['systemctl', '--user', 'disable', '--now', 'docker.service'], timeout=120)
        assert h.user(['systemctl', '--user', 'show', 'docker.service', '-p', 'ActiveState', '--value']) == 'inactive'
    save('rootless PG stopped and rootless engine disabled; volumes retained')
    snapshot_path = ROOT / 'home-config-before.private.json'
    if snapshot_path.exists():
        snapshot = json.loads(snapshot_path.read_text())
        new_files = list(b.get('new_files', []))
        extra = '.config/environment.d/90-ameesh-docker-rootless.conf'
        # Reconcile a crash after the user wrote the new file but before the
        # controller could record its successful return. Original files win.
        if b and extra not in snapshot and extra not in new_files:
            new_files.append(extra)
        restore = r'''import base64,json,os,pathlib,tempfile,stat,sys
p=json.load(sys.stdin);h=pathlib.Path.home()
def remove(path):
 if path.is_symlink() or path.is_file():path.unlink()
 elif path.exists():raise RuntimeError('Unexpected directory during file restore')
for name in p['new_files']:
 assert name=='.config/environment.d/90-ameesh-docker-rootless.conf'
 path=h/name
 if path.exists():assert path.read_bytes()==b'DOCKER_HOST=unix:///run/user/1001/docker.sock\n'
 remove(path)
for name,row in p['snapshot'].items():
 assert not pathlib.PurePosixPath(name).is_absolute() and '..' not in pathlib.PurePosixPath(name).parts
 path=h/name
 if row['type']=='absent':remove(path);continue
 if row['type']=='symlink':remove(path);os.symlink(row['target'],path);continue
 assert row['type']=='file';path.parent.mkdir(parents=True,exist_ok=True)
 fd,tmp=tempfile.mkstemp(prefix='.5781-restore-',dir=path.parent)
 try:
  with os.fdopen(fd,'wb') as out:out.write(base64.b64decode(row['data']));out.flush();os.fsync(out.fileno())
  os.chmod(tmp,row['mode']);os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
print(json.dumps({'original_home_config_restored':True}))'''
        assert json.loads(h.user_json(restore, {'snapshot': snapshot, 'new_files': new_files}))['original_home_config_restored']
    if b.get('docker_group_removed'):
        assert 'ameesh' not in grp.getgrnam('docker').gr_mem
        h.command(['gpasswd', '--add', 'ameesh', 'docker'])
    assert 'ameesh' in grp.getgrnam('docker').gr_mem
    save('original client configuration and docker membership restored; approved manager restart next')
    h.command(['systemctl', 'restart', 'user@1001.service'], timeout=180)
    assert h.command(['systemctl', 'is-active', 'user@1001.service']).stdout.strip() == b'active'
    if not source['State']['Running']:
        h.docker(h.RF, 'start', 'ameesh-pg')
    deadline = time.monotonic() + 150
    while h.inspect(h.RF, 'ameesh-pg')['State'].get('Health', {}).get('Status') != 'healthy':
        assert time.monotonic() < deadline, 'Preserved source health pending; observe same source'
        time.sleep(2)
    baseline = ROOT / 'database-baseline.private.json'
    if baseline.exists():
        role = a['role']
        assert h.database_baseline(h.RF, 'ameesh-pg', role) == json.loads(baseline.read_text())
    for unit in authority['restore_active_native_units']:
        assert __import__('re').fullmatch(r'ameesh-[A-Za-z0-9_.@:-]+\.(?:service|timer)', unit)
        assert unit != 'ameesh-paquet-horaire.timer'
        if h.user(['systemctl', '--user', 'show', unit, '-p', 'ActiveState', '--value']) != 'active':
            h.user(['systemctl', '--user', 'start', unit], timeout=120)
    save('pre-key rollback PG/client/manager restored; final timer+drain restoration still required',
         source_PG_healthy=True, source_volume_retained=True, target_volume_retained=True,
         original_restart_policy='no', timer_restoration_pending=True, native_undrain_pending=True)
    print(json.dumps(record))


if __name__ == '__main__':
    main()

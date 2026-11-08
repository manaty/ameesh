"""Real rootless SQL/user-unit and rights checks; does not lift native drainage.

Mail-turn proof, exact timer restoration and independent attestation remain
explicit subsequent steps. No broad completion is inferred from these checks.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import time

ROOT = Path('/root/ameesh-operator-5781-AG1-20261104')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', required=True)
    parser.parse_args()
    assert os.geteuid() == 0 and os.uname().nodename == 'ameesh-agents-1'
    assert not ROOT.is_symlink() and ROOT.stat().st_uid == 0
    path = ROOT / 'phase-A-engine-and-PG.py'
    assert path.stat().st_uid == 0 and not path.is_symlink()
    spec = importlib.util.spec_from_file_location('phase_a', path)
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    assert json.loads((ROOT / 'phase-B-progress.json').read_text())['phase'].startswith('phase B completed;')
    authority_path = ROOT / 'phase-C-authority.json'
    assert not authority_path.is_symlink() and authority_path.stat().st_uid == 0 and stat.S_IMODE(authority_path.stat().st_mode) == 0o600
    authority = json.loads(authority_path.read_text())
    assert 0 <= time.time() - authority['verified_epoch'] < 600
    for key in ['native_AG1_drain_active', 'composer_timer_masked', 'no_unapproved_personal_session', 'release_tag_and_test_source_sha_verified']:
        assert authority.get(key) is True
    sha = authority['test_source_sha']
    assert re.fullmatch(r'[0-9a-f]{40}', sha)
    assert not (ROOT / 'phase-C-progress.json').exists(), 'Reconcile previous verification; no blind duplicate'
    record = {'lot': 5781, 'test_source_sha': sha, 'drain_lifted': False,
              'real_mail_turn_proved': False, 'independent_attestation': False,
              'composer_timer_restored': False, 'L2_key_created': False}

    def save(phase, **fields):
        record.update(fields, phase=phase, at=h.stamp())
        h.write_private(ROOT / 'phase-C-progress.json', (json.dumps(record, indent=2) + '\n').encode())

    save('local verification starting, drain remains active')
    sudo = h.command(['env', 'LC_ALL=C', 'sudo', '-n', '-l', '-U', 'ameesh'], allow_error=True)
    message = (sudo.stdout + sudo.stderr).decode()
    assert 'not allowed' in message and 'NOPASSWD:' not in message
    own = h.command(h.user_args(['sudo', '-n', '-l']), allow_error=True)
    assert own.returncode != 0
    fixture = ROOT / 'protected-read-fixture'
    fd = os.open(fixture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(b'harmless operator-owned access-check fixture\n')
    image = json.loads((ROOT / 'phase-A-progress.json').read_text())['source_image']
    probe = h.docker(h.RL, 'run', '--rm', '--network=none', '--mount',
                     'type=bind,src=' + str(fixture) + ',dst=/protected,readonly',
                     '--entrypoint=/bin/cat', image, '/protected', allow_error=True)
    # Never print even the harmless content if the negative check unexpectedly succeeds.
    assert probe.returncode != 0 and not probe.stdout, 'Rootless container could read root-protected fixture'
    assert h.inspect(h.RL, 'ameesh-pg')['State']['Health']['Status'] == 'healthy'
    save('sudo denied and protected bind read refused', no_sudo_allow_rule=True, protected_bind_read_denied=True)
    prepare = "import pathlib,tempfile;d=pathlib.Path.home()/'.local/state/ameesh/operators';d.mkdir(parents=True,exist_ok=True);print(tempfile.mkdtemp(prefix='5781-sql-',dir=d))"
    workdir = h.user(['/usr/bin/python3', '-I', '-c', prepare])
    assert workdir.startswith('/home/ameesh/.local/state/ameesh/operators/5781-sql-')
    archive = h.command(h.user_args(['git', '--no-replace-objects', '-C', '/home/ameesh/work/ameesh-release',
                                   '-c', 'core.hooksPath=/dev/null', 'archive', '--format=tar', sha]), timeout=90).stdout
    extract = r'''import io,tarfile,pathlib,sys
p=pathlib.Path(sys.argv[1]);assert p.is_dir()
with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())) as t:t.extractall(p,filter='data')
assert (p/'tests/test_work.py').is_file() and (p/'scripts/test-parallele.sh').is_file()
(p/'.operator-5781-modules.txt').write_text('tests.test_work\n')'''
    h.command(h.user_args(['/usr/bin/python3', '-I', '-c', extract, workdir]), data=archive, timeout=120)
    save('qualified release source extracted as UID1001 for one real SQL module', test_workdir=workdir)

    def unit(name, args, timeout=600):
        return h.command(h.user_args(['systemd-run', '--user', '--pipe', '--wait', '--collect', '--quiet',
                                     '--unit=ameesh-operator-5781-' + name,
                                     '--property=WorkingDirectory=' + workdir,
                                     '--property=EnvironmentFile=/home/ameesh/.config/ameesh/env',
                                     '--setenv=PATH=/home/ameesh/.local/share/ameesh/venv/bin:/usr/local/bin:/usr/bin:/bin',
                                     *args]), timeout=timeout, allow_error=True)

    sql = unit('real-sql', ['/bin/bash', workdir + '/scripts/test-parallele.sh', '-n', '1', '-p', 'psql',
                          '--liste', workdir + '/.operator-5781-modules.txt'], timeout=1800)
    h.write_private(ROOT / 'phase-C-SQL.private.log', sql.stdout + sql.stderr)
    assert sql.returncode == 0, 'Actual SQL module failed; inspect existing job/log before any rerun'
    assert b'base(s)' in sql.stdout, 'No actual PostgreSQL-backed part evidence'
    save('real SQL module passed under effective user-unit environment', real_SQL_psql_passed=True)
    binary = '/home/ameesh/.local/share/ameesh/venv/bin/ameesh'
    doctor = unit('doctor', [binary, 'doctor'], timeout=180)
    h.write_private(ROOT / 'phase-C-doctor.private.log', doctor.stdout + doctor.stderr)
    assert doctor.returncode == 0, 'Native doctor reports a problem; do not undrain yet'
    housekeeping = unit('menage-readonly', [binary, 'menage', '--dry-run', '--json'], timeout=600)
    h.write_private(ROOT / 'phase-C-menage.private.json', housekeeping.stdout)
    assert housekeeping.returncode == 0, 'Native housekeeping dry-run failed'
    json.loads(housekeeping.stdout)
    save('local checks passed; native mail-turn, timer restoration and independent attestation pending',
         doctor_passed=True, housekeeping_dryrun_passed=True, rootless_PG_healthy=True)
    print(json.dumps(record))


if __name__ == '__main__':
    main()

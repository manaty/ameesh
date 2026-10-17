#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Root-owned stdin dispatcher for enrolled restart/recovery-restart actions.

No caller-selected file path, shell command, unit, DSN or manifest is accepted.
Enrollment/bootstrap and the independent controller service remain separate.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys

ENROLLMENT = Path('/etc/ameesh-release/enrollment.json')
OPERATIONS = Path('/etc/ameesh-release/operations')
DATABASE = Path('/etc/ameesh-release/database.json')
JOURNAL = Path('/var/lib/ameesh-release/helper')
CONTROLLER = Path('/var/lib/ameesh-release/controller')
RUNTIME = Path('/opt/ameesh-release/runtime/bin/python')


class HostError(ValueError):
    pass


def trusted(path, *, private=False, owner=0):
    path = Path(path).absolute()
    for ancestor in (path,*path.parents):
        if ancestor.is_symlink():
            raise HostError('untrusted enrolled file')
        info = ancestor.stat()
        if info.st_uid != owner or info.st_mode & 0o022:
            raise HostError('untrusted enrolled file')
    if private and (not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077):
        raise HostError('private enrolled file required')


def private_json(path):
    trusted(path,private=True)
    fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != 0 or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
            raise HostError('private enrolled file required')
        data = stream.read(65537)
    if len(data)>65536:
        raise HostError('enrolled document exceeds limit')
    return json.loads(data)


def module(name):
    path = Path(__file__).with_name(name+'.py')
    trusted(path)
    spec = importlib.util.spec_from_file_location('host_'+name.replace('-','_'),path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def execute(request):
    # Called only from the root-owned immutable installed runtime with -I -B.
    if os.geteuid()!=0 or Path(sys.executable)!=RUNTIME or not sys.flags.isolated or not sys.flags.dont_write_bytecode:
        raise HostError('independent root runtime required')
    trusted(RUNTIME)
    trusted(Path(__file__))
    if (not isinstance(request,dict) or set(request)!={'operation','step','action_id','manifest_digest'}
            or not isinstance(request['operation'],str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',request['operation'])
            or request['step'] not in ('restart','recovery_restart')
            or request['action_id']!=request['operation']+':'+request['step']
            or not isinstance(request['manifest_digest'],str)
            or not re.fullmatch(r'[0-9a-f]{64}',request['manifest_digest'])):
        raise HostError('typed host request required')
    enrollment = private_json(ENROLLMENT)
    if not isinstance(enrollment,dict) or set(enrollment)!={'host','machine_id','policy','database'}:
        raise HostError('invalid host enrollment')
    trusted('/etc/machine-id')
    if Path('/etc/machine-id').read_text().strip()!=enrollment['machine_id']:
        raise HostError('enrolled host identity mismatch')
    operation = private_json(OPERATIONS/(request['operation']+'.json'))
    if (not isinstance(operation,dict) or set(operation)!={'operation','decision_id','manifest'}
            or operation['operation']!=request['operation'] or type(operation['decision_id']) is not int
            or operation['decision_id']<=0 or operation['manifest'].get('host')!=enrollment['host']
            or operation['manifest'].get('database')!=enrollment['database']):
        raise HostError('invalid enrolled operation')
    controller = private_json(CONTROLLER/(request['operation']+'.json'))
    if (controller.get('manifest')!=operation['manifest'] or controller.get('digest')!=request['manifest_digest']
            or controller.get('intent')!=request['step'] or controller.get('status') in ('accepted','rolled_back')):
        raise HostError('independent controller intent required')
    # Verify protected credentials before asking config.load to consume them.
    database = private_json(DATABASE)
    if (not isinstance(database,dict) or database.get('backend')!='postgres'
            or not isinstance(database.get('dsn'),str) or not database['dsn']):
        raise HostError('enrolled direct database configuration required')
    restart = module('helper-redemarrage')
    authority = module('autorite-release')
    fence = module('fence-release')
    # Do not inherit caller/runner libpq, tracing, account or config variables.
    os.environ.clear()
    os.environ.update({'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C'})
    from ameesh import config, db as db_mod
    cfg = config.load(env={'AMEESH_CONFIG':str(DATABASE)})
    db = db_mod.connect(cfg,driver='psycopg')
    try:
        signed = authority.SignedDecisionAuthority(db,operation['decision_id'],request['operation'],operation['manifest'])
        gate = fence.SignedReleaseFence(db,signed)
        engine = restart.RestartHelper(JOURNAL,enrollment['policy'],authority=signed,fence=gate,system=restart.Systemd())
        return engine.perform(request['step'],dict(request,manifest=operation['manifest']))
    finally:
        db.close()


def main():
    def expired(signum,frame):
        raise HostError('host request deadline exceeded')
    try:
        if len(sys.argv)!=1:
            raise HostError('host arguments refused')
        signal.signal(signal.SIGALRM,expired)
        signal.alarm(30)
        data = sys.stdin.buffer.read(65537)
        signal.alarm(0)
        if len(data)>65536:
            raise HostError('host request exceeds limit')
        receipt = execute(json.loads(data))
        print(json.dumps(receipt,sort_keys=True))
        return 0
    except Exception:
        # Never echo invalid stdin, signed documents, filenames, DSNs, provider
        # exceptions or systemd output. The caller retains its exact request.
        signal.alarm(0)
        print('{"error":"host_action_unavailable"}')
        return 1


if __name__=='__main__':
    sys.exit(main())

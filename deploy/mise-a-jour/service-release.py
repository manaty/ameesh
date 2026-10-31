#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Independent service coordinator; only protected, signed host jobs are run.

The pinned host factory is deliberately not supplied by transport or enrollment
JSON. Until that complete factory is installed, startup fails without readiness.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time


def module(name):
    spec=importlib.util.spec_from_file_location('service_'+name.replace('-','_'),Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


protocol=module('controleur-release')
runtime=module('runtime-release')
restart=module('helper-redemarrage')
REQUIRED=frozenset(protocol.STEPS+protocol.RECOVERY)
ROOT=Path('/var/lib/ameesh-release/controller')
OPERATIONS=Path('/etc/ameesh-release/operations')
RUNTIME=Path('/opt/ameesh-release/runtime')
SOURCES=Path('/opt/ameesh-release/lib')


class ServiceError(ValueError):
    pass


class Documents:
    def load(self,path):
        runtime.trusted(path)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                    or info.st_mode & 0o077 or info.st_size>65536 or info.st_nlink!=1):
                raise ServiceError('private service document required')
            data=stream.read(65537)
        if len(data)>65536:
            raise ServiceError('service document exceeds limit')
        return json.loads(data)


class System:
    def identity(self):
        text=restart.Systemd.run(['/usr/bin/systemctl','show','ameesh-release-controller.service',
            '--property=LoadState,ActiveState,Slice,MainPID,InvocationID'])
        fields=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
        if (fields.get('LoadState')!='loaded' or fields.get('ActiveState')!='active'
                or fields.get('Slice')!='system.slice' or fields.get('MainPID')!=str(os.getpid())
                or re.fullmatch(r'[0-9a-f]{32}',fields.get('InvocationID','')) is None):
            raise ServiceError('independent service instance required')
        return dict(machine_id=Path('/etc/machine-id').read_text().strip(),
                    boot_id=restart.Systemd().boot(),invocation_id=fields['InvocationID'])

    def verify_trees(self,pins):
        for path,key in ((RUNTIME,'runtime_tree_sha256'),(SOURCES,'source_tree_sha256')):
            observed=runtime.inventory(path)
            if observed['digest']!=pins[key]:
                return False
            if any((path/name).stat().st_mode & 0o222 for name in observed['dirs']+list(observed['files'])):
                return False
        return True


class IndependentService:
    def __init__(self,root,operations,enrollment,pins, *, documents,system,factory,authority_factory,clock=time.monotonic):
        if (not isinstance(enrollment,dict) or set(enrollment)!={'host','machine_id','policy','database'}
                or not isinstance(pins,dict) or set(pins)!={'runtime_tree_sha256','source_tree_sha256'}):
            raise ServiceError('exact service enrollment required')
        if (not callable(getattr(factory,'admit',None)) or not callable(getattr(factory,'for_operation',None))
                or not callable(authority_factory)):
            raise ServiceError('enrolled service factory required')
        restart.validate_policy(enrollment['policy'])
        if (enrollment['host']!=enrollment['policy']['host']
                or not isinstance(enrollment['machine_id'],str) or not re.fullmatch(r'[0-9a-f]{32}',enrollment['machine_id'])
                or not isinstance(enrollment['database'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',enrollment['database'])
                or any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value) for value in pins.values())):
            raise ServiceError('invalid service host bindings')
        self.root,self.operations=Path(root),Path(operations)
        self.enrollment=json.loads(json.dumps(enrollment)); self.pins=dict(pins)
        self.documents,self.system,self.factory,self.authority_factory=documents,system,factory,authority_factory
        self.clock=clock; self.retry={}; self.admitted=False; self.receipt=None

    def admit(self):
        self.admitted=False
        identity=self.system.identity()
        # Remove a previous readiness before any slow/failed admission. A stale
        # current-instance receipt must never outlive drift or missing backends.
        with protocol.locked(self.root,self.enrollment['host']):
            (self.root/'readiness.json').unlink(missing_ok=True)
        if identity['machine_id']!=self.enrollment['machine_id'] or self.system.verify_trees(self.pins) is not True:
            raise ServiceError('service host or runtime drift')
        capabilities=self.factory.admit(self.enrollment)
        if (not isinstance(capabilities,(tuple,list,set,frozenset))
                or any(not isinstance(value,str) for value in capabilities)
                or set(capabilities)!=REQUIRED):
            raise ServiceError('complete host release adapter required')
        # Recheck the identity and immutable trees after factory DB/config reads.
        if self.system.identity()!=identity or self.system.verify_trees(self.pins) is not True:
            raise ServiceError('service admission changed')
        receipt=dict(host=self.enrollment['host'],machine_id=identity['machine_id'],
            boot_id=identity['boot_id'],invocation_id=identity['invocation_id'],adapter_ready=True,**self.pins)
        with protocol.locked(self.root,self.enrollment['host']):
            protocol.save(self.root/'readiness.json',receipt)
        self.admitted=True
        self.receipt=receipt
        return receipt

    def withdraw(self):
        self.admitted=False
        if self.receipt is None:
            return
        with protocol.locked(self.root,self.enrollment['host']):
            path=self.root/'readiness.json'
            if path.exists() or path.is_symlink():
                if protocol.identity(self.documents.load(path))==protocol.identity(self.receipt):
                    path.unlink(); runtime.fsync_directory(self.root)

    def job(self,operation):
        if not isinstance(operation,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',operation):
            raise ServiceError('typed service operation required')
        value=self.documents.load(self.operations/(operation+'.json'))
        if (not isinstance(value,dict) or set(value)!={'operation','decision_id','manifest'}
                or value['operation']!=operation or type(value['decision_id']) is not int or value['decision_id']<=0):
            raise ServiceError('invalid protected operation')
        protocol.validate(value['manifest'])
        manifest=value['manifest']
        if (manifest['host']!=self.enrollment['host'] or manifest['database']!=self.enrollment['database']
                or manifest['policy']!=restart.policy_id(self.enrollment['policy'])
                or any(unit not in self.enrollment['policy']['units'] for unit in manifest['units'])):
            raise ServiceError('service operation scope mismatch')
        return value

    def run_one(self,operation):
        if not self.admitted:
            raise ServiceError('service admission required')
        job=self.job(operation)
        authority=self.authority_factory(job)
        adapter=self.factory.for_operation(job,authority)
        try:
            outcome=protocol.run(self.root,operation,job['manifest'],authority=authority,adapter=adapter)
        except Exception:
            outcome='held'
        result=dict(operation=operation,manifest_digest=protocol.identity(job['manifest']),outcome=outcome)
        with protocol.locked(self.root,self.enrollment['host']):
            protocol.save(self.root/(operation+'.result.json'),result)
        self.retry[operation]=self.clock()+30 if outcome=='held' else float('inf')
        return result

    def tick(self):
        self.admit()
        active=self.root/(self.enrollment['host']+'.active.json')
        if active.exists() or active.is_symlink():
            reservation=self.documents.load(active)
            if not isinstance(reservation,dict) or set(reservation)!={'operation','digest'}:
                raise ServiceError('service reservation unavailable')
            operation=reservation['operation']
            names=[operation]
        else:
            names=[path.stem for path in self.operations.glob('*.json')]
            if len(names)>128:
                raise ServiceError('service queue admission exceeded')
        if any(not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',name) for name in names):
            raise ServiceError('typed service queue required')
        results=[]
        for operation in sorted(names):
            if self.retry.get(operation,0)>self.clock():
                continue
            try:
                result=self.run_one(operation)
                results.append(result)
                if result['outcome']=='held' and (active.exists() or active.is_symlink()):
                    break  # A newly reserved host blocks the rest of this batch.
            except Exception:
                # Do not echo document bodies, proof/credential/provider errors.
                self.retry[operation]=self.clock()+30
                result=dict(operation=operation,outcome='unavailable')
                with protocol.locked(self.root,self.enrollment['host']):
                    protocol.save(self.root/(operation+'.result.json'),result)
                results.append(result)
        return results


def main():
    db=None; service=None
    try:
        if (len(sys.argv)!=1 or os.geteuid()!=0 or Path(sys.executable)!=RUNTIME/'bin/python'
                or not sys.flags.isolated or not sys.flags.dont_write_bytecode):
            raise ServiceError('fixed independent runtime required')
        runtime.trusted(Path(__file__)); runtime.trusted(RUNTIME/'bin/python')
        os.environ.clear(); os.environ.update(PATH='/usr/bin:/bin',LANG='C',LC_ALL='C')
        documents=Documents()
        enrollment=documents.load(Path('/etc/ameesh-release/enrollment.json'))
        pins=documents.load(Path('/etc/ameesh-release/service.json'))
        if (not isinstance(pins,dict) or set(pins)!={'runtime_tree_sha256','source_tree_sha256'}
                or any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value) for value in pins.values())
                or System().verify_trees(pins) is not True):
            raise ServiceError('pinned service source/runtime required')
        # Fixed pinned source module, never a name or path from a job/config.
        factory_path=Path(__file__).with_name('adaptateur-hote-release.py')
        runtime.trusted(factory_path)
        factory_module=module('adaptateur-hote-release')
        signed=module('autorite-release')
        database=Path('/etc/ameesh-release/database.json')
        cfg_document=documents.load(database)
        if (cfg_document.get('backend')!='postgres' or not isinstance(cfg_document.get('dsn'),str)
                or not cfg_document['dsn']):
            raise ServiceError('independent database enrollment required')
        from ameesh import config,db as db_mod,migrations
        db=db_mod.connect(config.load(env={'AMEESH_CONFIG':str(database)}),driver='psycopg')
        factory=factory_module.HostFactory(db,enrollment)
        service=IndependentService(ROOT,OPERATIONS,enrollment,pins,documents=documents,system=System(),factory=factory,
            authority_factory=lambda job:signed.SignedDecisionAuthority(db,job['decision_id'],job['operation'],job['manifest']))
        stopping=[False]
        def stop(signum,frame): stopping[0]=True
        signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
        while not stopping[0]:
            migrations.require_compatible_writer(db)
            for result in service.tick():
                print(json.dumps(result,sort_keys=True),flush=True)
            for _ in range(20):
                if stopping[0]: break
                time.sleep(0.1)
        return 0
    except Exception:
        print('{"error":"independent_service_unavailable"}',flush=True)
        return 1
    finally:
        try:
            if service is not None:
                service.withdraw()
        except Exception:
            pass  # Inactive/changed systemd identity invalidates stale readiness.
        try:
            if db is not None: db.close()
        except Exception:
            pass  # Never expose credential-bearing driver cleanup errors.


if __name__=='__main__':
    sys.exit(main())

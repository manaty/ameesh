#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Exact admin bootstrap files with private snapshots and crash reconciliation.

No CLI. Host-bound bootstrap authority is required separately for every effect.
The controller key is installed only through enable after readiness validation.
"""
import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import stat
import uuid


def module(name):
    spec=importlib.util.spec_from_file_location('bootstrap_file_'+name.replace('-','_'),
        Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


delegation=module('delegation-release')
runtime=module('runtime-release')
protocol=delegation.restart.protocol
KEY='/etc/ameesh-release/authorized_keys'


class FileError(ValueError):
    pass


class BootstrapFiles:
    def __init__(self,journal, *, authority,root='/',readiness=None):
        # root is only an administrator/test fixture mapping. There is no CLI
        # or SSH route accepting it, and the default is the fixed host root.
        self.journal=Path(journal)
        self.authority=authority
        self.readiness=readiness
        self.root=Path(root).absolute()
        runtime.trusted(self.root)

    def path(self,logical):
        return self.root/logical.lstrip('/')

    def snapshot(self,path):
        runtime.trusted(path.parent)
        if not path.exists() and not path.is_symlink():
            return None
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid()
                    or before.st_mode & 0o022 or before.st_nlink!=1 or before.st_size>65536
                    or before.st_mode & 0o7000):
                raise FileError('untrusted bootstrap destination')
            data=stream.read(65537)
            if len(data)>65536 or runtime.stamp(before)!=runtime.stamp(os.fstat(stream.fileno())):
                raise FileError('bootstrap destination changed')
        return dict(content=base64.b64encode(data).decode(),mode=stat.S_IMODE(before.st_mode),
                    sha256=hashlib.sha256(data).hexdigest())

    def approved(self,request):
        try:
            machine=self.path('/etc/machine-id')
            runtime.trusted(machine)
            ready=(request['step']!='enable' or (callable(self.readiness) and self.readiness(request) is True))
            allowed=(ready and machine.read_text().strip()==request['bundle']['machine_id']
                     and self.authority.validate(request['operation'],request['scope_digest'],request['step']) is True)
        except Exception:
            allowed=False
        if not allowed:
            raise FileError('bootstrap file authority unavailable')

    @staticmethod
    def expected(info):
        data=info['content'].encode()
        return dict(content=base64.b64encode(data).decode(),mode=info['mode'],sha256=info['sha256'])

    def write(self,path,expected, *, request,prior):
        runtime.trusted(path.parent)
        self.approved(request)
        if self.snapshot(path)!=prior:
            raise FileError('bootstrap destination changed before publication')
        temporary=path.with_name('.'+path.name+'-'+uuid.uuid4().hex)
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(base64.b64decode(expected['content'],validate=True))
                stream.flush(); os.fsync(stream.fileno()); os.fchmod(stream.fileno(),expected['mode'])
            if self.snapshot(path)!=prior:
                raise FileError('bootstrap destination changed before publication')
            self.approved(request)
            os.replace(temporary,path)
            runtime.fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def checked(self,step,request):
        if (step not in ('configuration','enable','revoke') or not isinstance(request,dict)
                or set(request)!={'operation','step','action_id','scope_digest','bundle'}
                or not isinstance(request['operation'],str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',request['operation'])
                or request['step']!=step or request['action_id']!=request['operation']+':'+step):
            raise FileError('typed bootstrap file request required')
        # Validate the complete canonical inventory and scope before mapping any
        # path. No caller content or path outside the renderer is accepted.
        delegation.question(request['bundle'])
        expected_scope=protocol.identity(dict(operation=request['operation'],bundle_digest=request['bundle']['digest'],step=step))
        if request['scope_digest']!=expected_scope:
            raise FileError('bootstrap file scope mismatch')
        self.approved(request)

    def perform(self,step,request):
        self.checked(step,request)
        if step=='revoke':
            return self.revoke(request)
        files={path:info for path,info in request['bundle']['files'].items() if (path==KEY)==(step=='enable')}
        # Directory creation/layout is a preceding bootstrap effect, never an
        # implicit repair using account-owned or unknown destination ancestors.
        for logical in files:
            runtime.trusted(self.path(logical).parent)
        action=protocol.identity(dict(operation=request['operation'],step=step))
        with protocol.locked(self.journal,'bootstrap-files') as root:
            self.approved(request)
            path=root/(action+'.json')
            if path.exists() or path.is_symlink():
                record=protocol.load(path)
                if (set(record)!={'request','before','done','receipt'}
                        or protocol.identity(record['request'])!=protocol.identity(request)
                        or set(record['before'])!=set(files)
                        or not isinstance(record['done'],list) or not set(record['done'])<=set(files)):
                    raise FileError('bootstrap file journal mismatch')
            else:
                before={logical:self.snapshot(self.path(logical)) for logical in files}
                for logical,snapshot in before.items():
                    declared=request['bundle']['previous_files'][logical]
                    if (None if snapshot is None else {key:snapshot[key] for key in ('sha256','mode')})!=declared:
                        raise FileError('bootstrap prior file was not reviewed')
                self.approved(request)
                record=dict(request=request,before=before,done=[],receipt=None)
                # Full private snapshots become durable before first replace.
                protocol.save(path,record)
            for logical,info in sorted(files.items()):
                actual=self.snapshot(self.path(logical)); wanted=self.expected(info)
                if logical in record['done']:
                    if actual!=wanted:
                        raise FileError('installed bootstrap file changed')
                    continue
                if actual!=wanted:
                    if actual!=record['before'][logical]:
                        raise FileError('bootstrap file changed outside operation')
                    self.write(self.path(logical),wanted,request=request,prior=actual)
                self.approved(request)
                record['done'].append(logical)
                protocol.save(path,record)
            receipt=dict(request,ok=True)
            if record['receipt'] is not None and protocol.identity(record['receipt'])!=protocol.identity(receipt):
                raise FileError('bootstrap file receipt mismatch')
            self.approved(request)
            if record['receipt'] is None:
                record['receipt']=receipt; protocol.save(path,record)
            return receipt

    def revoke(self,request):
        self.checked('revoke',request)
        files=request['bundle']['files']
        action=protocol.identity(dict(operation=request['operation'],step='revoke'))
        with protocol.locked(self.journal,'bootstrap-files') as root:
            path=root/(action+'.json')
            if path.exists() or path.is_symlink():
                record=protocol.load(path)
                if (set(record)!={'request','done','receipt'}
                        or protocol.identity(record['request'])!=protocol.identity(request)
                        or not isinstance(record['done'],list) or not set(record['done'])<=set(files)):
                    raise FileError('bootstrap revoke journal mismatch')
            else:
                record=dict(request=request,done=[],receipt=None)
                self.approved(request); protocol.save(path,record)
            snapshots={}
            for step in ('configuration','enable'):
                source_action=protocol.identity(dict(operation=request['operation'],step=step))
                source_path=root/(source_action+'.json')
                if not source_path.exists() and not source_path.is_symlink():
                    continue
                source=protocol.load(source_path)
                expected=dict(request,step=step,action_id=request['operation']+':'+step,
                    scope_digest=protocol.identity(dict(operation=request['operation'],bundle_digest=request['bundle']['digest'],step=step)))
                expected_paths={logical for logical in files if (logical==KEY)==(step=='enable')}
                if (set(source)!={'request','before','done','receipt'}
                        or set(source['before'])!=expected_paths
                        or protocol.identity(source.get('request'))!=protocol.identity(expected)):
                    raise FileError('bootstrap revoke source intent mismatch')
                for logical,before in source['before'].items():
                    if logical not in files:
                        raise FileError('bootstrap revoke source path mismatch')
                    declared=request['bundle']['previous_files'][logical]
                    if (None if before is None else {key:before[key] for key in ('sha256','mode')})!=declared:
                        raise FileError('bootstrap prior snapshot mismatch')
                    if before is not None and hashlib.sha256(base64.b64decode(before['content'],validate=True)).hexdigest()!=before['sha256']:
                        raise FileError('bootstrap prior snapshot corrupt')
                    snapshots[logical]=before
            # Revoke access first; keep runtime, account, credentials, journals
            # and live fences untouched even after failed/partial bootstrap.
            for logical in sorted(snapshots,key=lambda name:(name!=KEY,name)):
                before=snapshots[logical]; target=self.path(logical)
                actual=self.snapshot(target)
                self.approved(request)
                if actual!=before:
                    if actual!=self.expected(files[logical]):
                        raise FileError('bootstrap revoke destination changed')
                    if before is None:
                        if self.snapshot(target)!=actual:
                            raise FileError('bootstrap revoke destination changed')
                        self.approved(request)
                        target.unlink(); runtime.fsync_directory(target.parent)
                    else:
                        self.write(target,before,request=request,prior=actual)
                if logical not in record['done']:
                    record['done'].append(logical); protocol.save(path,record)
            receipt=dict(request,ok=True)
            if record['receipt'] is not None and protocol.identity(record['receipt'])!=protocol.identity(receipt):
                raise FileError('bootstrap revoke receipt mismatch')
            self.approved(request)
            if record['receipt'] is None:
                record['receipt']=receipt; protocol.save(path,record)
            return receipt

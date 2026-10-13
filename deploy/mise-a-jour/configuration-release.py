#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Operation-private file recovery, before a host adapter reloads configuration.

No CLI, systemd command or service admission. Destination names come exclusively
from protected administrator enrollment. Target bytes belong to the signed
versioned-release identity. Callers must validate syntax and reconcile reloads.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid


def module(name):
    spec=importlib.util.spec_from_file_location('configuration_'+name.replace('-','_'),
        Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


runtime=module('runtime-release')
protocol=runtime.protocol
fences=module('fence-release')


class ConfigurationError(ValueError):
    pass


def validate_destinations(destinations):
    """Exact protected destination map; the host factory also admits it alone."""
    if not isinstance(destinations,dict) or not 1<=len(destinations)<=32:
        raise ConfigurationError('bounded enrolled configuration required')
    value=dict(destinations)
    for name,logical in value.items():
        if (not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',name)
                or not isinstance(logical,str) or '..' in Path(logical).parts
                or str(Path(logical))!=logical
                or not (logical.startswith('/etc/systemd/system/') or logical.startswith('/etc/ameesh/'))):
            raise ConfigurationError('protected configuration destination required')
    if len(set(value.values()))!=len(value):
        raise ConfigurationError('distinct configuration destinations required')
    return value


class ConfigurationFiles:
    def __init__(self,journal,destinations, *, authority,gate,root='/',clock=time.time):
        # root is administrator/test mapping only; neither a job nor transport
        # can select it. User-writable units require separate enrollment policy.
        self.journal=Path(journal); self.root=Path(root).absolute()
        runtime.trusted(self.root)
        self.destinations=validate_destinations(destinations)
        self.authority,self.gate,self.clock=authority,gate,clock

    def path(self,name):
        return self.root/self.destinations[name].lstrip('/')

    def observed(self,path):
        runtime.trusted(path.parent)
        if not path.exists() and not path.is_symlink():
            return None
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid()
                    or before.st_nlink!=1 or before.st_mode & 0o7022 or before.st_size>65536):
                raise ConfigurationError('untrusted configuration file')
            data=stream.read(65537)
            if len(data)>65536 or runtime.stamp(before)!=runtime.stamp(os.fstat(stream.fileno())):
                raise ConfigurationError('configuration read changed')
        return dict(content=base64.b64encode(data).decode(),mode=stat.S_IMODE(before.st_mode),
                    sha256=hashlib.sha256(data).hexdigest())

    def checked(self,request,release):
        fences.validate(request)
        if request['step'] not in ('pin','configure','rollback'):
            raise ConfigurationError('configuration step required')
        if (not isinstance(release,dict) or release.get('commit')!=request['manifest']['commit']
                or request['manifest']['release']!=release['commit']+'-'+hashlib.sha256(
                    json.dumps(release,sort_keys=True).encode()).hexdigest()):
            raise ConfigurationError('signed release configuration identity mismatch')
        entries=release.get('configuration')
        if not isinstance(entries,dict) or set(entries)!=set(self.destinations):
            raise ConfigurationError('exact enrolled configuration inventory required')
        wanted={}; total=0
        for name,entry in entries.items():
            if (not isinstance(entry,dict) or set(entry)!={'content','mode'}
                    or not isinstance(entry['content'],str) or type(entry['mode']) is not int
                    or not 0<=entry['mode']<=0o777 or entry['mode'] & 0o022):
                raise ConfigurationError('configuration bytes and safe mode required')
            data=entry['content'].encode(); total+=len(data)
            if len(data)>65536 or total>1048576:
                raise ConfigurationError('configuration inventory exceeds limit')
            wanted[name]=dict(content=base64.b64encode(data).decode(),mode=entry['mode'],
                              sha256=hashlib.sha256(data).hexdigest())
        return wanted

    def approved(self,request):
        try:
            granted=self.authority.validate(request['operation'],request['manifest_digest'],request['step'])
        except Exception:
            granted=False
        if granted is not True or (request['step']=='rollback' and self.clock()>request['manifest']['rollback_until']):
            raise ConfigurationError('configuration authority unavailable')

    def replace(self,path,wanted,prior,request):
        if self.observed(path)!=prior:
            raise ConfigurationError('configuration changed outside operation')
        if wanted is None:
            self.approved(request)
            if self.observed(path)!=prior:
                raise ConfigurationError('configuration changed before removal')
            path.unlink(); runtime.fsync_directory(path.parent)
            return
        candidate=path.with_name('.'+path.name+'-'+uuid.uuid4().hex)
        fd=os.open(candidate,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(base64.b64decode(wanted['content'],validate=True))
                stream.flush(); os.fsync(stream.fileno()); os.fchmod(stream.fileno(),wanted['mode'])
            if self.observed(path)!=prior:
                raise ConfigurationError('configuration changed before publication')
            self.approved(request)  # authority may expire during the slow copy
            if self.observed(path)!=prior:
                raise ConfigurationError('configuration changed during authorization')
            os.replace(candidate,path); runtime.fsync_directory(path.parent)
        finally:
            candidate.unlink(missing_ok=True)

    def perform(self,request,release):
        request=json.loads(json.dumps(request)); release=json.loads(json.dumps(release))
        wanted=self.checked(request,release); step=request['step']
        with protocol.locked(self.journal,request['manifest']['host']):
            self.approved(request)
            path=self.journal/(request['operation']+'.configuration.json')
            binding=dict(operation=request['operation'],manifest_digest=request['manifest_digest'],
                         manifest=request['manifest'],destinations=self.destinations,wanted=wanted)
            if path.exists() or path.is_symlink():
                runtime.trusted(path)
                if path.stat().st_mode & 0o077 or path.stat().st_nlink!=1 or path.stat().st_size>8388608:
                    raise ConfigurationError('private configuration journal required')
                record=json.loads(path.read_text())
                if (not isinstance(record,dict) or set(record)!={'binding','before','phase','done'}
                        or protocol.identity(record['binding'])!=protocol.identity(binding)
                        or set(record['before'])!=set(wanted) or record['phase'] not in ('pinned','configure','rollback')
                        or not isinstance(record['done'],list) or not set(record['done'])<=set(wanted)):
                    raise ConfigurationError('configuration operation journal mismatch')
            elif step=='pin':
                before={name:self.observed(self.path(name)) for name in wanted}
                self.approved(request)
                record=dict(binding=binding,before=before,phase='pinned',done=[])
                protocol.save(path,record)  # all prior bytes durable before first write
            else:
                raise ConfigurationError('configuration pin required before effects')
            if step=='pin':
                return dict(action_id=request['action_id'],manifest_digest=request['manifest_digest'],files_pinned=True)
            if step=='configure' and record['phase']=='rollback':
                raise ConfigurationError('configuration already recovering')
            # The DB gate must hold the live drained fence and recheck signed
            # authority after waits, throughout publication and its receipt.
            with self.gate.guard(request):
                self.approved(request)
                if record['phase']!=step:
                    record['phase']=step; record['done']=[]; protocol.save(path,record)
                for name in sorted(wanted):
                    actual=self.observed(self.path(name))
                    target=wanted[name] if step=='configure' else record['before'][name]
                    alternative=record['before'][name] if step=='configure' else wanted[name]
                    if name in record['done'] and actual!=target:
                        raise ConfigurationError('completed configuration file changed')
                    if actual!=target:
                        if actual!=alternative:
                            raise ConfigurationError('configuration changed outside operation')
                        self.replace(self.path(name),target,actual,request)
                    self.approved(request)
                    if name not in record['done']:
                        record['done'].append(name); protocol.save(path,record)
                return dict(action_id=request['action_id'],manifest_digest=request['manifest_digest'],files_reconciled=True)

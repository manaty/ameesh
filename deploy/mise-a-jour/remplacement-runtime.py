#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Signed replacement of an already sealed root runtime (#5166).

`runtime-release.publish` never overwrites a tree and the H8 measurement admits
only runtime/lib/store under /opt/ameesh-release: the former trees and pin move
to a retained sibling on the same filesystem, the new trees are published by the
existing primitive, and the pin is replaced last. The permanent lock is only
acquired, and a bootstrap held before `enable` keeps its reservation and journal.
Any interruption leaves a state H8 refuses; a resume reconciles from observation.
Rollback is its own signed decision. No CLI: `backend-administrateur.py` wires it,
running from the staged trees because the enrolled backend predates this entry.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid


def module(name):
    spec=importlib.util.spec_from_file_location('replacement_'+name.replace('-','_'),
        Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


bootstrap=module('bootstrap-release')
runtime=module('runtime-release')
seal=module('scellement-runtime')
protocol=bootstrap.protocol
BASE='/opt/ameesh-release'
RETAINED='/opt/ameesh-release-retained'
PIN=bootstrap.delegation.SEAL_DOCUMENT
LOCK=bootstrap.delegation.SEAL_LOCK
ENTRY=bootstrap.delegation.SEAL_ENTRY
STEPS=('preflight','retire','runtime','sources','seal','verify')
ACTIONS=STEPS+('rollback',)
TREES={'runtime':'runtime','sources':'lib'}
OPTIONS=[dict(key='replace',label='Replace this exact sealed runtime'),
         dict(key='leave',label='Leave this host unchanged')]
PREFIX='Approve this exact sealed runtime replacement:\n'
ROLLBACK_OPTIONS=[dict(key='rollback',label='Restore the retained sealed runtime'),
                  dict(key='leave',label='Leave this host unchanged')]
ROLLBACK_PREFIX='Approve restoring the retained sealed runtime below:\n'
INPUT_FIELDS={'version','sha','expires_at','held_operation'}
BUNDLE_FIELDS={'version','kind','operation','host','machine_id','host_key_sha256','code_sha256',
               'held_operation','previous','next','retained','entry_sha256','seal','digest'}


class ReplacementError(ValueError):
    pass


def scopes(operation,bundle):
    if not isinstance(bundle,dict) or set(bundle)!=BUNDLE_FIELDS or bundle['operation']!=operation:
        raise ReplacementError('exact replacement bundle required')
    if protocol.identity({key:value for key,value in bundle.items() if key!='digest'})!=bundle['digest']:
        raise ReplacementError('replacement bundle changed')
    return {step:protocol.identity(dict(operation=operation,bundle_digest=bundle['digest'],step=step))
            for step in ACTIONS}


def _text(prefix,value):
    text=prefix+json.dumps(value,sort_keys=True,separators=(',',':'))
    if len(text.encode())>bootstrap.signed.decisions.MAX_QUESTION:
        raise ReplacementError('replacement question exceeds the signed limit')
    return text


def question(operation,bundle, *, expires_at):
    if type(expires_at) is not int or expires_at<=0:
        raise ReplacementError('signed replacement deadline required')
    value={key:item for key,item in bundle.items() if key!='seal'}
    return _text(PREFIX,dict(value,scopes={step:scope for step,scope in scopes(operation,bundle).items()
                                           if step in STEPS},expires_at=expires_at))


def rollback_question(operation,bundle, *, expires_at):
    if type(expires_at) is not int or expires_at<=0:
        raise ReplacementError('signed rollback deadline required')
    return _text(ROLLBACK_PREFIX,dict(version=1,operation=operation,host=bundle['host'],
        machine_id=bundle['machine_id'],bundle_digest=bundle['digest'],previous=bundle['previous'],
        next=bundle['next'],retained=bundle['retained'],scope=scopes(operation,bundle)['rollback'],
        step='rollback',expires_at=expires_at))


class SignedReplacementAuthority(bootstrap.SignedBootstrapAuthority):
    steps,option,options,prefix=STEPS,'replace',OPTIONS,PREFIX

    def __init__(self,db,decision_id,operation,bundle,**kwargs):
        if type(decision_id) is not int or decision_id<=0:
            raise ReplacementError('replacement decision required')
        clock=kwargs.get('clock',bootstrap.time.time)
        self.db,self.decision_id,self.operation,self.clock=db,decision_id,operation,clock
        self.bundle=json.loads(json.dumps(bundle))
        self.scopes=scopes(operation,self.bundle)

    def render(self,operation,expires_at):
        return question(operation,self.bundle,expires_at=expires_at)


class SignedRollbackAuthority(SignedReplacementAuthority):
    steps,option,options,prefix=('rollback',),'rollback',ROLLBACK_OPTIONS,ROLLBACK_PREFIX

    def render(self,operation,expires_at):
        return rollback_question(operation,self.bundle,expires_at=expires_at)


class Replacement:
    """One replacement on the backend's fixed paths; `backend` maps tests only."""

    def __init__(self,backend,operation):
        self.backend,self.operation=backend,operation
        if len(operation)>70:
            raise ReplacementError('replacement operation too long')
        self.stage=backend.stage(operation)
        self.base=backend.path(BASE)
        self.retained=backend.path(RETAINED)/operation
        self.pin=backend.path(PIN)
        self.lock=backend.path(LOCK)
        self.effects=backend.admin/'effects'
        self.bundle_path=self.stage/'replacement-bundle.json'
        self.question_path=self.stage/'replacement-question.txt'
        self.uid=os.geteuid()

    # ---- preparation: reads the host, publishes nothing
    def measure_stage(self):
        """Digests recomputed from the root copy this command runs from, never read.

        The stage must be private before measuring: nothing but root can change
        it between this measure and the execution.
        """
        runtime.trusted(self.stage)
        info=self.stage.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=self.uid or stat.S_IMODE(info.st_mode)!=0o700
                or stat.S_IMODE(self.stage.parent.lstat().st_mode)!=0o700):
            raise ReplacementError('private root stage required')
        return {name:runtime.inventory(self.stage/stage) for stage,name in TREES.items()}

    def check_measured(self):
        """Refuse before any effect when the stage is not the signed one."""
        fresh=self.measure_stage()
        if self.bundle_path.exists() or self.bundle_path.is_symlink():
            bundle=json.loads(self.backend.private(self.bundle_path,1<<24))
            if any(fresh[name]['digest']!=bundle['next'][name] for name in ('runtime','lib')):
                raise ReplacementError('staged trees differ from the signed replacement')
        return fresh

    def current_pin(self):
        data=seal.read(self.pin,seal.MAX_DOCUMENT,uid=self.uid)
        document=json.loads(data,object_pairs_hook=seal.unique)
        seal.validate(document)
        if seal.canonical(document)!=data or stat.S_IMODE(self.pin.stat().st_mode)!=0o600:
            raise ReplacementError('canonical private pin required')
        return document,hashlib.sha256(data).hexdigest()

    def reservation(self):
        path=self.backend.journal()/(self.backend.enrollment['host']+'.active.json')
        if not path.exists() and not path.is_symlink():
            return None
        return protocol.load(path)

    def check_held(self,held):
        active=self.reservation()
        if held is None:
            if active is not None:
                raise ReplacementError('host has another unresolved bootstrap')
            return
        record=self.backend.record(held)
        if (active is None or active.get('operation')!=held or record.get('complete') is not False
                or record.get('intent') not in (None,'validate') or 'enable' in record.get('receipts',{})):
            raise ReplacementError('named bootstrap must stay held before enable')

    def prepare(self):
        backend=self.backend
        fresh=self.check_measured()
        try:
            value=json.loads(backend.private(self.stage/'replacement.json',4096))
        except (OSError,ValueError):
            raise ReplacementError('staged replacement unavailable') from None
        if (not isinstance(value,dict) or set(value)!=INPUT_FIELDS or value['version']!=1
                or not isinstance(value['sha'],str) or re.fullmatch('[0-9a-f]{40}',value['sha']) is None
                or (value['held_operation'] is not None and (not isinstance(value['held_operation'],str)
                    or value['held_operation']==self.operation))):
            raise ReplacementError('exact staged replacement required')
        self.check_held(value['held_operation'])
        old,old_sha=self.current_pin()
        machine=backend.machine()
        if old['machine_id']!=machine or old['sha']==value['sha']:
            raise ReplacementError('replacement must change the sealed commit of this host')
        if fresh['runtime']['digest']==old['trees']['runtime']['digest'] \
                and fresh['lib']['digest']==old['trees']['lib']['digest']:
            raise ReplacementError('replacement must change the sealed trees')
        document=dict(version=1,sha=value['sha'],machine_id=machine,
                      trees=dict(runtime=fresh['runtime'],lib=fresh['lib'],store=old['trees']['store']))
        seal.validate(document)
        text=seal.canonical(document).decode()
        def summary(item,digest):
            return dict(sha=item['sha'],seal_sha256=digest,
                        **{name:item['trees'][name]['digest'] for name in ('runtime','lib','store')})
        body=dict(version=1,kind='runtime-replacement',operation=self.operation,
            host=backend.enrollment['host'],machine_id=machine,host_key_sha256=backend.host_key(),
            code_sha256=backend.gestes.code,held_operation=value['held_operation'],
            previous=summary(old,old_sha),next=summary(document,hashlib.sha256(text.encode()).hexdigest()),
            retained=RETAINED+'/'+self.operation,
            entry_sha256=hashlib.sha256(backend.path(ENTRY).read_bytes()).hexdigest(),seal=text)
        bundle=dict(body,digest=protocol.identity(body))
        rendered=question(self.operation,bundle,expires_at=value['expires_at'])
        if self.bundle_path.exists() or self.bundle_path.is_symlink():
            if protocol.identity(json.loads(backend.private(self.bundle_path,1<<24)))!=protocol.identity(bundle):
                raise ReplacementError('another replacement is already prepared for this operation')
            backend.replace(self.question_path,rendered)
        else:
            self.question_path.unlink(missing_ok=True)
            backend.write(self.question_path,rendered)
            backend.write(self.bundle_path,json.dumps(bundle,sort_keys=True,separators=(',',':')))
        backend.gestes.append(command='replacement-prepare',operation=self.operation,phase='prepared',
                              bundle_digest=bundle['digest'],
                              question_sha256=hashlib.sha256(rendered.encode()).hexdigest())
        return dict(operation=self.operation,bundle_digest=bundle['digest'],question=rendered,options=OPTIONS)

    def prepared(self,*,same_code=True):
        try:
            bundle=json.loads(self.backend.private(self.bundle_path,1<<24))
            text=self.backend.private(self.question_path,bootstrap.signed.decisions.MAX_QUESTION).decode()
            expires=json.loads(text[len(PREFIX):])['expires_at']
            exact=text==question(self.operation,bundle,expires_at=expires)
        except (OSError,KeyError,TypeError,ValueError):
            raise ReplacementError('prepared replacement unavailable') from None
        if not exact or bundle['host']!=self.backend.enrollment['host'] or bundle['machine_id']!=self.backend.machine():
            raise ReplacementError('prepared replacement does not match this host')
        # The signature stands for the code enrollment of this one operation.
        if same_code:
            if bundle['code_sha256']!=self.backend.gestes.code:
                raise ReplacementError('replacement prepared by other code')
            self.check_measured()
        return bundle,text

    def rollback_text(self,expires_at):
        bundle,_=self.prepared()
        return dict(operation=self.operation,question=rollback_question(self.operation,bundle,expires_at=expires_at),
                    options=ROLLBACK_OPTIONS)

    # ---- observation
    def digest(self,path):
        if not path.exists() and not path.is_symlink():
            return None
        return runtime.inventory(path,max_bytes=seal.MAX_BYTES,max_files=seal.MAX_ENTRIES)['digest']

    def observed(self,bundle):
        pin=hashlib.sha256(self.pin.read_bytes()).hexdigest() if self.pin.exists() else None
        return dict(pin={bundle['previous']['seal_sha256']:'previous',bundle['next']['seal_sha256']:'next'}.get(pin,'unknown'),
                    trees={name:self.digest(self.base/name) for name in ('runtime','lib')},
                    retained_trees={name:self.digest(self.retained/name) for name in ('runtime','lib')},
                    stopped=self.stopped())

    def stopped(self):
        try:
            return self.backend.system.handoff_stopped() is True
        except Exception:
            return False

    def plan(self,decision):
        bundle,_=self.prepared()
        authority=self.backend.replacement_authority(decision,self.operation,bundle)
        action=scopes(self.operation,bundle)
        allowed={}
        for step in STEPS:
            try:
                allowed[step]=authority.validate(self.operation,action[step],step) is True
            except Exception:
                allowed[step]=False
        record=self.load_record()
        return dict(dry_run=True,operation=self.operation,bundle_digest=bundle['digest'],decision=decision,
                    authorized=all(allowed.values()),steps=allowed,
                    done=sorted((record or {}).get('receipts',{})),observed=self.observed(bundle),
                    interpreter_outside_base=self.interpreter_outside())

    def interpreter_outside(self):
        # The running interpreter must survive the rename of the former tree.
        return self.base not in Path(sys.prefix).resolve().parents and Path(sys.prefix).resolve()!=self.base

    # ---- effects
    def record_path(self,kind='replace'):
        return self.effects/'replacement'/(self.operation+'.'+kind+'.json')

    def load_record(self,kind='replace'):
        path=self.record_path(kind)
        if not path.exists() and not path.is_symlink():
            return None
        return protocol.load(path)

    def run(self,decision):
        bundle,_=self.prepared()
        authority=self.backend.replacement_authority(decision,self.operation,bundle)
        action=scopes(self.operation,bundle)
        def approved(step):
            try:
                valid=authority.validate(self.operation,action[step],step) is True
            except Exception:
                valid=False
            if not valid or self.backend.machine()!=bundle['machine_id'] or not self.stopped():
                raise ReplacementError(getattr(authority,'denial_reason',None)
                                       or 'replacement authority or stopped H8 unavailable')
            return True
        self.backend.gestes.append(command='replace',operation=self.operation,phase='start',
                                   bundle_digest=bundle['digest'],decision=decision)
        approved('preflight')
        if not self.interpreter_outside():
            raise ReplacementError('run replacement from the staged runtime')
        if self.load_record('rollback') is not None:
            raise ReplacementError('this replacement was rolled back; prepare another operation')
        with seal.locked(self.lock,exclusive=True,uid=self.uid):
            inode=self.lock.stat().st_ino
            with protocol.locked(self.backend.journal(),bundle['host']):
                record=self.load_record()
                self.record_path().parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                if record is None:
                    record=dict(bundle_digest=bundle['digest'],receipts={},complete=False,lock_inode=inode)
                    protocol.save(self.record_path(),record)
                if record['bundle_digest']!=bundle['digest'] or record['lock_inode']!=inode:
                    raise ReplacementError('replacement journal or permanent lock changed')
                if record['complete']:
                    return dict(operation=self.operation,bundle_digest=bundle['digest'],outcome='replaced')
                self.check_held(bundle['held_operation'])
                for step in STEPS:
                    if step in record['receipts']:
                        continue
                    approved(step)
                    base=dict(command='replace',operation=self.operation,step=step,bundle_digest=bundle['digest'])
                    self.backend.gestes.append(phase='intent',**base)
                    try:
                        getattr(self,'do_'+step)(bundle,lambda step=step:approved(step))
                    except Exception as error:
                        self.backend.gestes.append(phase='uncertain',category=type(error).__name__,**base)
                        raise
                    approved(step)
                    self.backend.gestes.append(phase='receipt',ok=True,**base)
                    record['receipts'][step]=action[step]
                    protocol.save(self.record_path(),record)
                record['complete']=True
                protocol.save(self.record_path(),record)
        return dict(operation=self.operation,bundle_digest=bundle['digest'],outcome='replaced')

    def do_preflight(self,bundle,approved):
        if self.backend.host_key()!=bundle['host_key_sha256']:
            raise ReplacementError('pinned host key mismatch')
        old,digest=self.current_pin()
        if digest!=bundle['previous']['seal_sha256']:
            raise ReplacementError('installed pin is not the reviewed previous pin')
        seal.verify(old,self.base,uid=self.uid)
        if hashlib.sha256(self.backend.path(ENTRY).read_bytes()).hexdigest()!=bundle['entry_sha256']:
            raise ReplacementError('installed seal verifier changed')
        for stage,name in TREES.items():
            if runtime.inventory(self.stage/stage)['digest']!=bundle['next'][name]:
                raise ReplacementError('staged tree mismatch')
        if self.retained.exists() or self.retained.is_symlink():
            raise ReplacementError('retained destination exists')
        parent=self.retained.parent
        anchor=parent if parent.exists() else parent.parent
        if anchor.stat().st_dev!=self.base.stat().st_dev:
            raise ReplacementError('retained trees must share the runtime filesystem')

    @staticmethod
    def write_private(path,data):
        temporary=path.with_name('.'+path.name+'-'+uuid.uuid4().hex)
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,path)
            runtime.fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def move(self,source,target,digest):
        """Atomic rename once, reconciled by observation on a resume."""
        if target.exists() or target.is_symlink():
            if self.digest(target)!=digest or source.exists() and self.digest(source)==digest:
                raise ReplacementError('tree move unresolved; inspect')
            return
        if self.digest(source)!=digest:
            raise ReplacementError('tree to move is not the reviewed one')
        os.rename(source,target)
        runtime.fsync_directory(source.parent); runtime.fsync_directory(target.parent)

    def do_retire(self,bundle,approved):
        parent=self.retained.parent
        for directory in (parent,self.retained):
            if not directory.exists():
                directory.mkdir(mode=0o700); os.chmod(directory,0o700)
                runtime.fsync_directory(directory.parent)
            runtime.trusted(directory)
        kept=self.retained/'runtime-seal.json'
        data=self.pin.read_bytes()
        if not kept.exists():
            if hashlib.sha256(data).hexdigest()!=bundle['previous']['seal_sha256']:
                raise ReplacementError('installed pin is not the reviewed previous pin')
            self.write_private(kept,data)
        elif hashlib.sha256(kept.read_bytes()).hexdigest()!=bundle['previous']['seal_sha256']:
            raise ReplacementError('retained pin changed')
        for name in ('runtime','lib'):
            approved()
            self.move(self.base/name,self.retained/name,bundle['previous'][name])

    def publish(self,step,bundle,approved):
        name=TREES[step]
        runtime.publish(self.stage/step,self.base/name,self.effects/'trees',self.operation+'-'+step,
                        bundle['next'][name],authorize=approved)

    def do_runtime(self,bundle,approved):
        self.publish('runtime',bundle,approved)

    def do_sources(self,bundle,approved):
        self.publish('sources',bundle,approved)

    def do_seal(self,bundle,approved):
        data=bundle['seal'].encode()
        current=hashlib.sha256(self.pin.read_bytes()).hexdigest()
        if current==bundle['next']['seal_sha256']:
            return
        if current!=bundle['previous']['seal_sha256']:
            raise ReplacementError('installed pin changed outside the replacement')
        for name in ('runtime','lib'):
            if self.digest(self.base/name)!=bundle['next'][name]:
                raise ReplacementError('new tree missing before the new pin')
        approved()
        self.write_private(self.pin,data)

    def do_verify(self,bundle,approved):
        new,digest=self.current_pin()
        if digest!=bundle['next']['seal_sha256']:
            raise ReplacementError('new pin unavailable')
        seal.verify(new,self.base,uid=self.uid)
        self.check_retained(bundle)

    def check_retained(self,bundle):
        kept=self.retained/'runtime-seal.json'
        if hashlib.sha256(kept.read_bytes()).hexdigest()!=bundle['previous']['seal_sha256']:
            raise ReplacementError('retained pin changed')
        for name in ('runtime','lib'):
            if self.digest(self.retained/name)!=bundle['previous'][name]:
                raise ReplacementError('retained tree changed')

    def rollback(self,decision):
        bundle,_=self.prepared()
        authority=self.backend.rollback_authority(decision,self.operation,bundle)
        scope=scopes(self.operation,bundle)['rollback']
        def approved():
            try:
                valid=authority.validate(self.operation,scope,'rollback') is True
            except Exception:
                valid=False
            if not valid or self.backend.machine()!=bundle['machine_id'] or not self.stopped():
                raise ReplacementError(getattr(authority,'denial_reason',None)
                                       or 'rollback authority or stopped H8 unavailable')
        approved()
        if not self.interpreter_outside():
            raise ReplacementError('run rollback from the staged runtime')
        with seal.locked(self.lock,exclusive=True,uid=self.uid):
            with protocol.locked(self.backend.journal(),bundle['host']):
                self.check_held(bundle['held_operation'])
                base=dict(command='replacement-rollback',operation=self.operation,bundle_digest=bundle['digest'])
                self.backend.gestes.append(phase='intent',**base)
                kept=(self.retained/'runtime-seal.json').read_bytes()
                if hashlib.sha256(kept).hexdigest()!=bundle['previous']['seal_sha256']:
                    raise ReplacementError('retained pin changed')
                self.record_path().parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                protocol.save(self.record_path('rollback'),dict(bundle_digest=bundle['digest'],complete=False))
                for name in ('runtime','lib'):
                    approved()
                    current,back=self.base/name,self.retained/name
                    if self.digest(current)==bundle['previous'][name]:
                        continue
                    if current.exists():
                        self.move(current,self.retained/('replaced-'+name),bundle['next'][name])
                    self.move(back,current,bundle['previous'][name])
                approved()
                self.write_private(self.pin,kept)
                old,_=self.current_pin()
                seal.verify(old,self.base,uid=self.uid)
                protocol.save(self.record_path('rollback'),dict(bundle_digest=bundle['digest'],complete=True))
                self.backend.gestes.append(phase='rolled-back',**base)
        return dict(operation=self.operation,bundle_digest=bundle['digest'],outcome='rolled-back')


def receipt_for(backend,previous):
    """The one completed, not rolled back replacement naming this held bootstrap."""
    directory=backend.admin/'effects'/'replacement'
    found=[]
    if directory.is_dir():
        for path in sorted(directory.glob('*.replace.json')):
            operation=path.name[:-len('.replace.json')]
            item=Replacement(backend,operation)
            bundle,text=item.prepared(same_code=False)
            record=item.load_record()
            if (bundle['held_operation']==previous and record.get('complete') is True
                    and item.load_record('rollback') is None):
                found.append((item,bundle,text,record))
    if len(found)>1:
        raise ReplacementError('several replacements name this bootstrap')
    return found[0] if found else None

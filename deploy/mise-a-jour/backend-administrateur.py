#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Enrolled administrator backend of the signed bootstrap (L232b, #4323).

The one root entry the bureau runs for `entree-operateur.py`: it only wires
the existing reviewed pieces (`SignedBootstrapAuthority`, `LinuxHost` with
`BootstrapHostAdapter`, `bootstrap.run`) to fixed paths. The caller chooses
an operation name and a decision id, never a program, root, destination,
journal or credential:

  inventory OPERATION              read-only host inventory of the reviewed
                                   destinations of the staged plan;
  prepare OPERATION                measures the staged trees, observes the
                                   inventory itself and writes the bundle and
                                   its compact question beside the stage;
  plan OPERATION --decision N      dry run: the exact ordered plan, per-step
                                   authority and observed state, no effect;
  install OPERATION --decision N   executes the signed bootstrap;
  continuation-question NEW --previous OLD --decision N
                                   freezes the previous receipt reference and
                                   prints the separate replacement question;
  continue-install NEW --decision N
                                   resumes through reciprocal signed
                                   supersession, keeping previous receipts;
  revoke-question OPERATION --expires T
                                   prints the separate rollback question to
                                   sign (option `revoke`); writes nothing;
  revoke OPERATION --decision N    signed rollback under its own decision:
                                   key first, approved snapshots restored,
                                   exact new files removed;
  replacement-prepare OPERATION    measures the staged trees and the installed
                                   pin, writes the replacement bundle/question;
  replacement-plan OPERATION --decision N
                                   dry run of the replacement; no effect;
  replace OPERATION --decision N   replaces a sealed runtime (#5166): former
                                   trees and pin retained, permanent lock kept;
  replacement-rollback-question OPERATION --expires T
                                   prints the separate question to sign;
  replacement-rollback OPERATION --decision N
                                   restores the retained trees and pin.

The staged `plan.json` may name `"delegation": "canaux"` (#4327): the same
commands then prepare and execute the separately signed bundle of the
`ameesh-handoff` and `ameesh-secrets` channel accounts (`bootstrap-canaux.py`,
`installateur-canaux.py`), which publishes no tree and requires the release
bootstrap already installed. Its rollback is again its own signed decision.

Every effect gesture is appended to a private hash-chained journal
(`gestes.jsonl`): code digest, bundle digest, step, intent, receipt digest or
a fixed failure category, never a document body, secret or provider text.
The administrator stages `runtime/`, `sources/` and `plan.json` under the
fixed `/var/lib/ameesh-release-bootstrap/OPERATION/`. The database used to
revalidate the signed decision is the separately enrolled controller
connection named by `/etc/ameesh-admin/config.json`, never runner
credentials; `/etc/ameesh-admin/backend.json` pins the host, machine-id and
the digest of the installed backend code, which must live outside any agent
checkout (root-owned, not writable by group or others). Tests map a disposable root and simulated accounts/services;
no real root, account, SSH or systemd effect is executed by them.
"""
import base64
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid


def module(name):
    spec=importlib.util.spec_from_file_location('admin_backend_'+name.replace('-','_'),
        Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


entry=module('entree-operateur')
installer=module('installateur-bootstrap')
channels=module('bootstrap-canaux')
channel_installer=module('installateur-canaux')
bootstrap=entry.bootstrap
delegation=bootstrap.delegation
protocol=bootstrap.protocol
runtime=installer.runtime
files_mod=installer.file_mod

STAGE='/var/lib/ameesh-release-bootstrap'
#: Operation names never start with a dot: no stage can collide with this.
ADMIN=STAGE+'/.admin'
ENROLLMENT='/etc/ameesh-admin/backend.json'
CONFIG='/etc/ameesh-admin/config.json'
HOST_KEY='/etc/ssh/ssh_host_ed25519_key.pub'
PLAN_FIELDS={'plan','account_ids','expires_at'}
PLAN_OPTIONAL={'probe_turn','runtime_seal','delegation'}
#: Signed bundle kinds a staged plan may name; release is the default.
DELEGATIONS=('release','canaux')
REPLACEMENT=('replacement-prepare','replacement-plan','replace','replacement-rollback-question','replacement-rollback')
COMMANDS=('inventory','prepare','plan','install','revoke-question','revoke','continuation-question','continue-install',*REPLACEMENT)
SIGNED=('plan','install','revoke','continue-install','replacement-plan','replace','replacement-rollback')
GENESIS='0'*64


class BackendError(ValueError):
    pass


class Release:
    """The release bootstrap bundle (#4323): ameesh-release and its trees."""
    name='release'
    steps,options,revoke_options=bootstrap.STEPS,bootstrap.OPTIONS,bootstrap.REVOKE_OPTIONS
    prefix,revoke_prefix=bootstrap.PREFIX,bootstrap.REVOKE_PREFIX
    scopes,question,revoke_question=bootstrap.scopes,bootstrap.question,bootstrap.revoke_question
    authority,revoke_authority=bootstrap.SignedBootstrapAuthority,bootstrap.SignedBootstrapRevokeAuthority
    directories=installer.DIRECTORIES
    private=('/etc/ameesh-release','/var/lib/ameesh-release','/opt/ameesh-release/store')
    keys=frozenset({files_mod.KEY})
    account=installer.account
    errors=(entry.OperatorError,bootstrap.BootstrapError)


class Channels:
    """The channel accounts bundle (#4327): ameesh-handoff and ameesh-secrets."""
    name='canaux'
    steps,options,revoke_options=channels.STEPS,channels.OPTIONS,channels.REVOKE_OPTIONS
    prefix,revoke_prefix=channels.PREFIX,channels.REVOKE_PREFIX
    scopes,question,revoke_question=channels.scopes,channels.question,channels.revoke_question
    authority,revoke_authority=channels.SignedChannelAuthority,channels.SignedChannelRevokeAuthority
    directories=channels.delegation.DIRECTORIES
    private=channels.delegation.PRIVATE
    keys=frozenset(channels.delegation.KEYS)
    account=channels.account
    errors=(channels.BootstrapError,channels.delegation.DelegationError)


def kind_of(bundle):
    """The kind a prepared bundle declares; its question is then rechecked."""
    return Channels if isinstance(bundle,dict) and bundle.get('kind')==channels.delegation.KIND else Release


def code_digest(directory=None):
    """Digest of the installed backend and companion bytes it can load."""
    return runtime.code_digest(directory or Path(__file__).parent)


class Gestes:
    """Append-only, hash-chained gesture journal (one JSON object per line).

    Each line carries the digest of the previous one: a removed, reordered or
    edited line breaks the chain and every later append refuses. Opened with
    O_APPEND|O_NOFOLLOW, private to its owner, fsynced line by line.
    """
    def __init__(self,path,code):
        self.path=Path(path); self.code=code

    def _open(self,flags):
        fd=os.open(self.path,flags|os.O_NOFOLLOW,0o600)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077 or info.st_nlink!=1:
            os.close(fd); raise BackendError('unsafe gesture journal')
        return fd

    @staticmethod
    def _chain(text):
        last=GENESIS; entries=[]
        for number,line in enumerate(text.splitlines(),1):
            try:
                item=json.loads(line)
            except ValueError:
                raise BackendError('gesture journal corrupt') from None
            if (not isinstance(item,dict) or item.get('seq')!=number or item.get('prev')!=last
                    or item.get('sha')!=protocol.identity({k:v for k,v in item.items() if k!='sha'})):
                raise BackendError('gesture journal chain broken')
            last=item['sha']; entries.append(item)
        return entries

    def entries(self):
        if not self.path.exists() and not self.path.is_symlink():
            return []
        fd=self._open(os.O_RDONLY)
        with os.fdopen(fd,'r') as stream:
            return self._chain(stream.read())

    def append(self,**fields):
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd=self._open(os.O_RDWR|os.O_CREAT|os.O_APPEND)
        with os.fdopen(fd,'r+') as stream:
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
            stream.seek(0)
            entries=self._chain(stream.read())
            item=dict(fields,seq=len(entries)+1,prev=entries[-1]['sha'] if entries else GENESIS,
                      ts=int(time.time()),code_sha256=self.code)
            item['sha']=protocol.identity(item)
            stream.write(json.dumps(item,sort_keys=True,separators=(',',':'))+'\n')
            stream.flush(); os.fsync(stream.fileno())
        return item


class JournaledAdapter:
    """Record intent before and receipt after every adapter effect."""
    def __init__(self,adapter,gestes,command):
        self.adapter,self.gestes,self.command=adapter,gestes,command

    def perform(self,step,request):
        base=dict(command=self.command,operation=request.get('operation'),step=step,
                  bundle_digest=(request.get('bundle') or {}).get('digest'),
                  scope_digest=request.get('scope_digest'))
        self.gestes.append(phase='intent',**base)
        try:
            receipt=self.adapter.perform(step,request)
        except Exception as error:
            # A fixed category: provider/command text may name private paths.
            self.gestes.append(phase='uncertain',category=type(error).__name__,**base)
            raise
        self.gestes.append(phase='receipt',ok=isinstance(receipt,dict) and receipt.get('ok') is True,
                           receipt_sha256=protocol.identity(receipt),**base)
        return receipt


class Backend:
    def __init__(self, *, system, enrollment, db=None, root='/', code=None):
        # `root` and a simulated `system` exist only for tests: `main()` uses
        # the fixed host root and `LinuxHost`, and no argument selects them.
        self.system,self.db=system,db
        self.root=Path(root).absolute(); runtime.trusted(self.root)
        self.gestes=Gestes(self.path(ADMIN)/'gestes.jsonl',code or code_digest())
        if (not isinstance(enrollment,dict) or set(enrollment)!={'version','host','machine_id','code_sha256'}
                or enrollment['version']!=1 or not isinstance(enrollment['host'],str)
                or not isinstance(enrollment['machine_id'],str)
                or re.fullmatch(r'[0-9a-f]{32}',enrollment['machine_id']) is None):
            raise BackendError('enrolled administrator backend required')
        if enrollment['code_sha256']!=self.gestes.code:
            # Only the enrolled bytes run as root: a changed backend or
            # companion refuses before any host access.
            raise BackendError('backend code differs from its enrollment')
        self.enrollment=enrollment
        self.admin=self.path(ADMIN)
        # Read-only observer: the constructor stores, never creates.
        self.files=files_mod.BootstrapFiles(self.admin/'effects/files',authority=None,root=self.root)

    def path(self,logical):
        return self.root/logical.lstrip('/')

    def stage(self,operation):
        if not isinstance(operation,str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',operation) is None:
            raise BackendError('bootstrap operation required')
        return self.path(STAGE)/operation

    def machine(self):
        try:
            observed=self.system.machine()
        except Exception:
            raise BackendError('host identity unavailable') from None
        if observed!=self.enrollment['machine_id']:
            raise BackendError('backend enrolled on another host')
        return observed

    def host_key(self):
        try:
            key=self.path(HOST_KEY); runtime.trusted(key)
            parts=key.read_text().split()
            if len(parts)<2 or parts[0]!='ssh-ed25519':
                return None
            return hashlib.sha256(base64.b64decode(parts[1],validate=True)).hexdigest()
        except Exception:
            return None

    @staticmethod
    def private(path,limit):
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077
                    or info.st_size>limit):
                raise BackendError('private administrator document required')
            return stream.read(limit+1)

    def staged(self,operation):
        stage=self.stage(operation); runtime.trusted(stage)
        try:
            value=json.loads(self.private(stage/'plan.json',1<<23))
        except (OSError,ValueError):
            raise BackendError('staged plan unavailable') from None
        if (not isinstance(value,dict) or not PLAN_FIELDS<=set(value)
                or not set(value)<=PLAN_FIELDS|PLAN_OPTIONAL or not isinstance(value['plan'],dict)
                or value.get('delegation','release') not in DELEGATIONS
                or (value.get('delegation')=='canaux' and set(value)&{'probe_turn','runtime_seal'})):
            raise BackendError('exact staged plan required')
        if value['plan'].get('host')!=self.enrollment['host'] or value['plan'].get('machine_id')!=self.machine():
            raise BackendError('staged plan names another host')
        return stage,value

    def observe(self,logical):
        target=self.path(logical)
        if not target.exists() and not target.is_symlink():
            return None
        try:
            # Same admission rules as the installer's own prior snapshot.
            snapshot=self.files.snapshot(target)
        except Exception:
            raise BackendError('untrusted reviewed destination') from None
        return dict(sha256=snapshot['sha256'],mode=snapshot['mode'])

    def inventory(self,operation):
        stage,value=self.staged(operation)
        if value.get('delegation')=='canaux':
            expected=channels.destinations(plan=value['plan'],account_ids=value['account_ids'])
        else:
            expected=entry.destinations(plan=value['plan'],account_ids=value['account_ids'],
                probe_turn=value.get('probe_turn'),runtime_seal=value.get('runtime_seal'))
        return dict(operation=operation,host=self.enrollment['host'],machine_id=self.machine(),
                    host_key_sha256=self.host_key(),files={path:self.observe(path) for path in expected})

    def prepare(self,operation):
        stage,value=self.staged(operation)
        observed=self.inventory(operation)['files']
        if value.get('delegation')=='canaux':
            # No staged tree: the channels run the installed release trees.
            prepared=entry.Prepared(operation,*channels.prepare(operation=operation,plan=value['plan'],
                account_ids=value['account_ids'],observed=observed,expires_at=value['expires_at']))
        else:
            prepared=entry.prepare(operation=operation,plan=value['plan'],account_ids=value['account_ids'],
                trees={'runtime':str(stage/'runtime'),'sources':str(stage/'sources')},observed=observed,
                probe_turn=value.get('probe_turn'),expires_at=value['expires_at'],db=self.db,
                runtime_seal=value.get('runtime_seal'))
        bundle=stage/'bundle.json'
        question=stage/'question.txt'
        if bundle.exists() or bundle.is_symlink():
            # Never re-render a reviewed bundle: a resumed install keeps it.
            if protocol.identity(json.loads(self.private(bundle,1<<24)))!=protocol.identity(prepared.bundle):
                raise BackendError('another bundle is already prepared for this operation')
            # The question is derived from that frozen bundle and the staged
            # deadline: a lost or altered one is repaired, never re-rendered.
            try:
                current=self.private(question,entry.MAX_QUESTION).decode()
            except (OSError,ValueError):
                current=None
            if current!=prepared.question:
                self.replace(question,prepared.question)
        else:
            # The bundle is written last: it alone marks a completed prepare,
            # so a question left by an interrupted run is replaced.
            question.unlink(missing_ok=True)
            self.write(question,prepared.question)
            self.write(bundle,json.dumps(prepared.bundle,sort_keys=True,separators=(',',':')))
        self.gestes.append(command='prepare',operation=operation,phase='prepared',
                           bundle_digest=prepared.bundle['digest'],
                           question_sha256=hashlib.sha256(prepared.question.encode()).hexdigest())
        return dict(operation=operation,bundle_digest=prepared.bundle['digest'],question=prepared.question)

    @staticmethod
    def write(path,text):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        runtime.fsync_directory(path.parent)

    @staticmethod
    def replace(path,text):
        temporary=path.with_name('.'+path.name+'-'+uuid.uuid4().hex)
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as stream:
                stream.write(text); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,path)
            runtime.fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def prepared(self,operation):
        stage=self.stage(operation); runtime.trusted(stage)
        try:
            bundle=json.loads(self.private(stage/'bundle.json',1<<24))
            question=self.private(stage/'question.txt',entry.MAX_QUESTION).decode()
        except (OSError,ValueError):
            raise BackendError('prepared bundle unavailable') from None
        if (not isinstance(bundle,dict) or bundle.get('host')!=self.enrollment['host']
                or bundle.get('machine_id')!=self.machine()):
            raise BackendError('prepared bundle names another host')
        kind=kind_of(bundle)
        try:
            expires=json.loads(question[len(kind.prefix):])['expires_at']
            exact=(question.startswith(kind.prefix)
                   and question==kind.question(operation,bundle,expires_at=expires))
        except (KeyError,TypeError,ValueError,*kind.errors):
            exact=False
        if not exact:
            raise BackendError('prepared question does not match its bundle')
        return entry.Prepared(operation,bundle,question)

    def authority(self,decision,operation,bundle,revoke=False):
        if type(decision) is not int or decision<=0:
            raise BackendError('signed decision required')
        if self.db is None:
            raise BackendError('enrolled decision registry required')
        kind=kind_of(bundle)
        return (kind.revoke_authority if revoke else kind.authority)(self.db,decision,operation,bundle)

    def adapter(self,authority,bundle):
        # One effects journal for both kinds: operations are distinct stages.
        factory=(channel_installer.ChannelHostAdapter if kind_of(bundle) is Channels
                 else installer.BootstrapHostAdapter)
        return factory(self.admin/'effects',authority=authority,system=self.system,root=self.root)

    def journal(self):
        return self.admin/'controller'

    def plan(self,operation,decision,authority=None,revoke_authority=None):
        """Dry run: the exact plan this decision would execute; no effect.

        Nothing is created, locked, journaled or reloaded: only reads of the
        host state, the staged trees, the controller journal and the signed
        decision. `authorized` is false while any step lacks its authority.
        """
        prepared=self.prepared(operation); bundle=prepared.bundle; kind=kind_of(bundle)
        authority=authority or self.authority(decision,operation,bundle)
        scopes=kind.scopes(operation,bundle)
        record=self.record(operation)
        steps=[]
        for step in kind.steps:
            try:
                allowed=authority.validate(operation,scopes[step],step) is True
            except Exception:
                allowed=False
            steps.append(dict(step=step,scope_digest=scopes[step],authorized=allowed,
                              done=step in record.get('receipts',{}),effects=self.effects(step,operation,bundle)))
        revoke_authority=revoke_authority or self.authority(decision,operation,bundle,revoke=True)
        try:
            revocable=revoke_authority.validate(operation,scopes['revoke'],'revoke') is True
        except Exception:
            revocable=False
        return dict(dry_run=True,delegation=kind.name,operation=operation,host=bundle['host'],machine_id=bundle['machine_id'],
                    bundle_digest=bundle['digest'],decision=decision,
                    authorized=all(item['authorized'] for item in steps),
                    complete=record.get('complete') is True,intent=record.get('intent'),
                    steps=steps,revoke=dict(scope_digest=scopes['revoke'],authorized=revocable,
                                            files=self.revocation(bundle)))

    def record(self,operation):
        path=self.journal()/(operation+'.json')
        if not path.exists() and not path.is_symlink():
            return {}
        return protocol.load(path)

    def account_intent(self,path,request):
        """Durable account intent `compte-release.ensure` would resume, or None.

        Read-only: the dry run must know whether a pre-existing group/account
        is the one a previous run created (resume) or an unowned identity the
        install refuses. Bad intent records refuse instead of rendering a plan.
        """
        path=Path(path)
        if not path.exists() and not path.is_symlink():
            return None
        try:
            runtime.trusted(path.parent)
            record=protocol.load(path)
        except Exception:
            raise BackendError('account intent unavailable') from None
        if (not isinstance(record,dict) or set(record)!={'request','phase','receipt'}
                or record['request']!=request
                or record['phase'] not in ('group','user_ready','user','complete')
                or (record['phase']!='complete' and record['receipt'] is not None)):
            raise BackendError('account intent unavailable')
        return record

    def tree(self,path):
        try:
            return runtime.inventory(path)['digest']
        except FileNotFoundError:
            return None
        except Exception:
            return 'untrusted'

    def effects(self,step,operation,bundle):
        stage=self.stage(operation); kind=kind_of(bundle)
        if step=='preflight' and kind is Channels:
            return dict(machine_id=self.system.machine()==bundle['machine_id'],
                        host_key=self.host_key()==bundle['plan']['host_key_sha256'],
                        **{logical:self.tree(self.path(logical))==bundle['plan'][field]
                           for logical,field in channel_installer.TREES.items()},
                        dispatchers={name:self.path(path).is_file() for name,path in bundle['dispatchers'].items()})
        if step=='preflight':
            return dict(machine_id=self.system.machine()==bundle['machine_id'],
                        host_key=self.host_key()==bundle['plan']['host_key_sha256'],
                        runtime=self.tree(stage/'runtime')==bundle['plan']['runtime_tree_sha256'],
                        sources=self.tree(stage/'sources')==bundle['plan']['source_tree_sha256'])
        if step=='layout':
            result={}
            for logical,mode in kind.directories.items():
                target=self.path(logical)
                if not target.exists() and not target.is_symlink():
                    # The real layout trusts the nearest existing ancestor
                    # before creating the missing chain; a linked or writable
                    # ancestor is refused, never repaired.
                    parent=target
                    while not parent.exists() and not parent.is_symlink():
                        parent=parent.parent
                    try:
                        runtime.trusted(parent)
                    except Exception:
                        result[logical]='refuse untrusted'; continue
                    result[logical]='create %04o'%mode
                    continue
                try:
                    runtime.trusted(target)
                except Exception:
                    result[logical]='refuse untrusted'; continue
                info=target.lstat()
                ok=(stat.S_ISDIR(info.st_mode) and info.st_uid==os.geteuid()
                    and (stat.S_IMODE(info.st_mode)==mode or not logical.startswith(tuple(kind.private))))
                result[logical]='present' if ok else 'mismatch'
            return result
        if step in ('runtime','sources'):
            logical='/opt/ameesh-release/'+('runtime' if step=='runtime' else 'lib')
            pinned=bundle['plan']['runtime_tree_sha256' if step=='runtime' else 'source_tree_sha256']
            current=self.tree(self.path(logical))
            return {logical:'publish '+pinned if current is None else 'present' if current==pinned else 'conflict'}
        if step=='account' or step in channels.ACCOUNT_STEPS and kind is Channels:
            if kind is Channels:
                ids=bundle['accounts'][channels.ACCOUNT_STEPS[step]]
                account_name,account_home=ids['name'],ids['home']
                observe=kind.account.Named(self.system,account_name,account_home)
            else:
                ids=bundle['account']; observe=self.system
                account_name,account_home=kind.account.NAME,kind.account.HOME
            # `compte-release.ensure` names one intent per account.
            suffix='.json' if account_name==kind.account.NAME else '.'+account_name+'.json'
            request=dict(operation=operation,machine_id=bundle['machine_id'],uid=ids['uid'],
                         gid=ids['gid'],scope_digest=kind.account.scope_digest(
                             operation,bundle['machine_id'],ids['uid'],ids['gid'],
                             name=account_name,home=account_home))
            try:
                group,user=observe.group(),observe.account()
                free=observe.free(ids['uid'],ids['gid'])
            except Exception:
                return dict(account='unavailable')
            record=self.account_intent(self.admin/'effects/account'/(operation+suffix),request)
            reviewed=kind.account.expected(ids['uid'],ids['gid'],name=account_name,home=account_home)
            if record is None:
                # Without its durable intent, ensure() refuses any pre-existing
                # group/account or taken numeric identity before creating.
                reasons=[]
                if group is not None: reasons.append('existing group')
                if user is not None: reasons.append('existing account')
                if not free: reasons.append('taken uid/gid')
                if reasons:
                    return dict(group='conflict',account='conflict',name=ids['name'],
                                uid=ids['uid'],gid=ids['gid'],refused=', '.join(reasons))
                return dict(group='create',account='create',name=ids['name'],
                            uid=ids['uid'],gid=ids['gid'])
            group_state='present' if group==reviewed['group'] else 'conflict'
            if group_state=='conflict':
                account_state='conflict'
            elif record['phase'] in ('group','user_ready'):
                account_state='create' if user is None else 'conflict'
            else:
                account_state='present' if user==reviewed['account'] else 'conflict'
            return dict(group=group_state,account=account_state,name=ids['name'],uid=ids['uid'],
                        gid=ids['gid'],phase=record['phase'])
        if step in ('configuration','enable'):
            result={}
            for logical,info in sorted(bundle['files'].items()):
                if (logical in kind.keys)!=(step=='enable'):
                    continue
                try:
                    current=self.observe(logical)
                except BackendError:
                    result[logical]='refuse untrusted'; continue
                wanted=dict(sha256=info['sha256'],mode=info['mode'])
                prior=bundle['previous_files'].get(logical)
                result[logical]=('unchanged' if current==wanted else
                                 'write %04o'%info['mode'] if current==prior else 'refuse not reviewed')
            if step=='enable':
                result['reload']=bundle['services']['ssh_reload']
                result['ready']=self.ready(bundle)
            return result
        if step=='validate' and kind is Channels:
            return dict(commands=['visudo -c -f /etc/sudoers.d/'+channel['account']
                                  for channel in channels.delegation.CHANNELS.values()]
                                 +['sshd -T (effective delegation per channel account)'],
                        ready=self.ready(bundle))
        if step=='validate':
            return dict(commands=['visudo -c -f /etc/sudoers.d/ameesh-release','sshd -T (effective delegation)'],
                        ready=self.ready(bundle),
                        seal=delegation.SEAL_DOCUMENT in bundle['files'])
        return {}

    def ready(self,bundle):
        try:
            if kind_of(bundle) is Channels:
                return self.system.trees_ready(bundle) is True
            return self.system.ready(bundle) is True
        except Exception:
            return False

    def revocation(self,bundle):
        result={}
        for logical,info in sorted(bundle['files'].items()):
            try:
                current=self.observe(logical)
            except BackendError:
                result[logical]='refuse untrusted'; continue
            wanted=dict(sha256=info['sha256'],mode=info['mode'])
            prior=bundle['previous_files'].get(logical)
            if current==prior:
                result[logical]='unchanged'
            elif current==wanted:
                result[logical]='remove' if prior is None else 'restore'
            else:
                result[logical]='refuse changed'
        return result

    def revoke_question(self,operation,expires_at):
        bundle=self.prepared(operation).bundle; kind=kind_of(bundle)
        question=kind.revoke_question(operation,bundle,expires_at=expires_at)
        if len(question.encode())>entry.MAX_QUESTION:
            raise BackendError('revoke question exceeds the signed limit')
        return dict(operation=operation,bundle_digest=bundle['digest'],question=question,
                    options=kind.revoke_options)

    def continuation_question(self,operation,previous,decision):
        continuation=module('continuation-bootstrap')
        prepared=self.prepared(operation)
        ref=continuation.reference(self,operation,previous,decision)
        expires=json.loads(prepared.question[len(bootstrap.PREFIX):])['expires_at']
        text=continuation.question(bootstrap,operation,prepared.bundle,ref,expires)
        path=self.stage(operation)/'continuation.json'
        if path.exists() or path.is_symlink():
            if json.loads(self.private(path,65536))!=ref:
                raise BackendError('frozen continuation changed')
        else:
            self.write(path,json.dumps(ref,sort_keys=True,separators=(',',':')))
        return dict(question=text,options=bootstrap.OPTIONS,previous=ref)

    def continue_install(self,operation,decision):
        base=dict(command='continue-install',operation=operation,decision=decision,
                  bundle_digest=self.prepared(operation).bundle['digest'])
        self.gestes.append(phase='start',**base)
        try:
            outcome=module('continuation-bootstrap').run(bootstrap,self,operation,decision,
                wrap=lambda adapter:JournaledAdapter(adapter,self.gestes,'continue-install'))
        except Exception as error:
            self.gestes.append(phase='held',category=type(error).__name__,**base)
            raise
        self.gestes.append(phase=outcome,**base)
        return dict(operation=operation,outcome=outcome)

    def install(self,operation,decision,authority=None):
        prepared=self.prepared(operation)
        authority=authority or self.authority(decision,operation,prepared.bundle)
        adapter=JournaledAdapter(self.adapter(authority,prepared.bundle),self.gestes,'install')
        base=dict(command='install',operation=operation,bundle_digest=prepared.bundle['digest'],decision=decision)
        self.gestes.append(phase='start',**base)
        try:
            if kind_of(prepared.bundle) is Channels:
                outcome=channels.run(self.journal(),operation,prepared.bundle,authority=authority,adapter=adapter)
            else:
                outcome=entry.install(prepared,authority=authority,adapter=adapter,journal=self.journal())
        except Exception as error:
            self.gestes.append(phase='held',category=type(error).__name__,**base)
            raise
        self.gestes.append(phase=outcome,**base)
        return dict(operation=operation,bundle_digest=prepared.bundle['digest'],outcome=outcome)

    def replacement(self,operation):
        return module('remplacement-runtime').Replacement(self,operation)

    def replacement_authority(self,decision,operation,bundle):
        if self.db is None:
            raise BackendError('enrolled decision registry required')
        return module('remplacement-runtime').SignedReplacementAuthority(self.db,decision,operation,bundle)

    def rollback_authority(self,decision,operation,bundle):
        if self.db is None:
            raise BackendError('enrolled decision registry required')
        return module('remplacement-runtime').SignedRollbackAuthority(self.db,decision,operation,bundle)

    def revoke(self,operation,decision,authority=None):
        """Signed rollback under the bootstrap's own host lock.

        `fichiers-bootstrap` removes the controller key first, restores only
        approved snapshots still matching the bundle and removes exact new
        files; runtime, account, journals and fences are kept.
        """
        prepared=self.prepared(operation); bundle=prepared.bundle
        authority=authority or self.authority(decision,operation,bundle,revoke=True)
        scopes=kind_of(bundle).scopes(operation,bundle)
        request=dict(operation=operation,step='revoke',action_id=operation+':revoke',
                     scope_digest=scopes['revoke'],bundle=bundle)
        adapter=JournaledAdapter(self.adapter(authority,bundle),self.gestes,'revoke')
        def run():
            with protocol.locked(self.journal(),bundle['host']):
                return adapter.perform('revoke',request)
        if delegation.SEAL_DOCUMENT in bundle['files']:
            with module('scellement-runtime').locked(exclusive=True):
                receipt=run()
        else:
            receipt=run()
        if not isinstance(receipt,dict) or receipt.get('ok') is not True:
            raise BackendError('bootstrap revoke held')
        return dict(operation=operation,bundle_digest=bundle['digest'],outcome='revoked')


def replacement_enrollment(enrollment,operation,code):
    """Host enrollment of a replacement, bound to its own staged code (#5166).

    The enrolled backend predates this entry and its re-enrollment needs the
    runtime being replaced: this one case runs from the staged trees of the
    operation, and its signed question binds the digest of exactly that code.
    """
    stage=Path(STAGE)/operation
    if (Path(__file__).resolve().parent!=stage/'sources' or Path(sys.prefix).resolve()!=stage/'runtime'
            or Path(sys.executable).parent.resolve()!=stage/'runtime/bin'):
        raise BackendError('replacement runs from its staged runtime and sources')
    return dict(enrollment,code_sha256=code)


def connect(config=CONFIG):
    """The separately enrolled controller connection; never runner settings."""
    path=Path(config); runtime.trusted(path)
    Backend.private(path,65536)
    from ameesh import config as settings, db
    return db.connect(settings.load({'AMEESH_CONFIG':str(path),'HOME':'/root','PATH':'/usr/bin:/bin'}))


def parse(argv):
    if len(argv)<2 or argv[0] not in COMMANDS:
        raise BackendError('usage: '+'|'.join(COMMANDS)+' OPERATION [--decision N]')
    command,operation,rest=argv[0],argv[1],argv[2:]
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',operation) is None:
        # Refused before any path is built from it.
        raise BackendError('bootstrap operation required')
    decision=None
    if command=='continuation-question':
        if (len(rest)!=4 or rest[0]!='--previous' or rest[2]!='--decision'
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',rest[1]) is None
                or not rest[3].isdigit() or int(rest[3])<=0):
            raise BackendError('continuation-question requires --previous OPERATION --decision N')
        decision=(rest[1],int(rest[3]))
    elif command in ('revoke-question','replacement-rollback-question'):
        if len(rest)!=2 or rest[0]!='--expires' or not rest[1].isdigit() or int(rest[1])<=0:
            raise BackendError(command+' requires --expires EPOCH')
        decision=int(rest[1])
    elif command in SIGNED:
        if len(rest)!=2 or rest[0]!='--decision' or not rest[1].isdigit() or int(rest[1])<=0:
            raise BackendError(command+' requires --decision N')
        decision=int(rest[1])
    elif rest:
        raise BackendError(command+' takes no option')
    return command,operation,decision


def dispatch(backend,command,operation,decision):
    if command=='continuation-question':
        return backend.continuation_question(operation,*decision)
    if command=='continue-install':
        return backend.continue_install(operation,decision)
    if command in ('inventory','prepare'):
        return getattr(backend,command)(operation)
    if command=='revoke-question':
        return backend.revoke_question(operation,decision)
    if command in REPLACEMENT:
        item=backend.replacement(operation)
        return {'replacement-prepare':item.prepare,'replacement-plan':item.plan,'replace':item.run,
                'replacement-rollback-question':item.rollback_text,'replacement-rollback':item.rollback}[command](
                    *(() if decision is None else (decision,)))
    return getattr(backend,command)(operation,decision)


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    try:
        command,operation,decision=parse(argv)
        if os.geteuid()!=0:
            raise BackendError('administrator backend runs as root')
        os.umask(0o077)
        try:
            # Installed code only: an agent checkout is not root-owned.
            runtime.trusted(Path(__file__).resolve().parent)
        except Exception:
            raise BackendError('backend code must be installed root-owned') from None
        enrollment=json.loads(Backend.private(runtime.trusted(ENROLLMENT),4096))
        if command in REPLACEMENT:
            enrollment=replacement_enrollment(enrollment,operation,code_digest())
        # A replacement measures its stage before importing the database stack.
        db=connect() if command not in ('inventory',*REPLACEMENT) else None
        # LinuxChannels extends LinuxHost: one fixed system for both kinds.
        backend=Backend(system=channel_installer.LinuxChannels(),enrollment=enrollment,db=db)
        if command in REPLACEMENT:
            backend.replacement(operation).check_measured()
            backend.db=connect()
        result=dispatch(backend,command,operation,decision)
    except (BackendError,module('remplacement-runtime').ReplacementError,*Release.errors,*Channels.errors) as error:
        print(json.dumps(dict(ok=False,error=str(error))),file=sys.stderr)
        return 2
    except Exception as error:
        # Never print provider text: a fixed category only.
        print(json.dumps(dict(ok=False,error='uncertain',category=type(error).__name__)),file=sys.stderr)
        return 1
    print(json.dumps(result,sort_keys=True,indent=1))
    if command=='plan' and not result['authorized'] and not result['revoke']['authorized']:
        return 3
    if command=='replacement-plan' and not result['authorized']:
        return 3
    return 0


if __name__=='__main__':
    sys.exit(main())

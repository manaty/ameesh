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
  revoke OPERATION --decision N    signed rollback: key first, approved
                                   snapshots restored, exact new files removed.

Every effect gesture is appended to a private hash-chained journal
(`gestes.jsonl`): code digest, bundle digest, step, intent, receipt digest or
a fixed failure category, never a document body, secret or provider text.
The administrator stages `runtime/`, `sources/` and `plan.json` under the
fixed `/var/lib/ameesh-release-bootstrap/OPERATION/`. The database used to
revalidate the signed decision is the separately enrolled controller
connection named by `/etc/ameesh-admin/backend.json`, never runner
credentials. Tests map a disposable root and simulated accounts/services;
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


def module(name):
    spec=importlib.util.spec_from_file_location('admin_backend_'+name.replace('-','_'),
        Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


entry=module('entree-operateur')
installer=module('installateur-bootstrap')
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
PLAN_OPTIONAL={'probe_turn','runtime_seal'}
COMMANDS=('inventory','prepare','plan','install','revoke')
SIGNED=('plan','install','revoke')
GENESIS='0'*64


class BackendError(ValueError):
    pass


def code_digest(directory=None):
    """Digest of the installed backend and companion bytes it can load."""
    directory=Path(directory or Path(__file__).parent)
    items={}
    for path in sorted(directory.iterdir()):
        if path.suffix=='.py' and path.is_file() and not path.is_symlink():
            items[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
    return protocol.identity(items)


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
        if (not isinstance(enrollment,dict) or set(enrollment)!={'version','host','machine_id'}
                or enrollment['version']!=1 or not isinstance(enrollment['host'],str)
                or not isinstance(enrollment['machine_id'],str)
                or re.fullmatch(r'[0-9a-f]{32}',enrollment['machine_id']) is None):
            raise BackendError('enrolled administrator backend required')
        self.enrollment=enrollment
        self.admin=self.path(ADMIN)
        # Read-only observer: the constructor stores, never creates.
        self.files=files_mod.BootstrapFiles(self.admin/'effects/files',authority=None,root=self.root)
        self.gestes=Gestes(self.admin/'gestes.jsonl',code or code_digest())

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
                or not set(value)<=PLAN_FIELDS|PLAN_OPTIONAL or not isinstance(value['plan'],dict)):
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
        expected=entry.destinations(plan=value['plan'],account_ids=value['account_ids'],
            probe_turn=value.get('probe_turn'),runtime_seal=value.get('runtime_seal'))
        return dict(operation=operation,host=self.enrollment['host'],machine_id=self.machine(),
                    host_key_sha256=self.host_key(),files={path:self.observe(path) for path in expected})

    def prepare(self,operation):
        stage,value=self.staged(operation)
        observed=self.inventory(operation)['files']
        prepared=entry.prepare(operation=operation,plan=value['plan'],account_ids=value['account_ids'],
            trees={'runtime':str(stage/'runtime'),'sources':str(stage/'sources')},observed=observed,
            probe_turn=value.get('probe_turn'),expires_at=value['expires_at'],db=self.db,
            runtime_seal=value.get('runtime_seal'))
        bundle=stage/'bundle.json'
        if bundle.exists() or bundle.is_symlink():
            # Never re-render a reviewed bundle: a resumed install keeps it.
            if protocol.identity(json.loads(self.private(bundle,1<<24)))!=protocol.identity(prepared.bundle):
                raise BackendError('another bundle is already prepared for this operation')
        else:
            # The bundle is written last: it alone marks a completed prepare,
            # so a question left by an interrupted run is replaced.
            (stage/'question.txt').unlink(missing_ok=True)
            self.write(stage/'question.txt',prepared.question)
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
        try:
            expires=json.loads(question[len(bootstrap.PREFIX):])['expires_at']
            exact=(question.startswith(bootstrap.PREFIX)
                   and question==bootstrap.question(operation,bundle,expires_at=expires))
        except (KeyError,TypeError,ValueError):
            exact=False
        if not exact:
            raise BackendError('prepared question does not match its bundle')
        return entry.Prepared(operation,bundle,question)

    def authority(self,decision,operation,bundle):
        if type(decision) is not int or decision<=0:
            raise BackendError('signed decision required')
        if self.db is None:
            raise BackendError('enrolled decision registry required')
        return bootstrap.SignedBootstrapAuthority(self.db,decision,operation,bundle)

    def adapter(self,authority):
        return installer.BootstrapHostAdapter(self.admin/'effects',authority=authority,
                                              system=self.system,root=self.root)

    def journal(self):
        return self.admin/'controller'

    def plan(self,operation,decision,authority=None):
        """Dry run: the exact plan this decision would execute; no effect.

        Nothing is created, locked, journaled or reloaded: only reads of the
        host state, the staged trees, the controller journal and the signed
        decision. `authorized` is false while any step lacks its authority.
        """
        prepared=self.prepared(operation); bundle=prepared.bundle
        authority=authority or self.authority(decision,operation,bundle)
        scopes=bootstrap.scopes(operation,bundle)
        record=self.record(operation)
        steps=[]
        for step in bootstrap.STEPS:
            try:
                allowed=authority.validate(operation,scopes[step],step) is True
            except Exception:
                allowed=False
            steps.append(dict(step=step,scope_digest=scopes[step],authorized=allowed,
                              done=step in record.get('receipts',{}),effects=self.effects(step,operation,bundle)))
        try:
            revocable=authority.validate(operation,scopes['revoke'],'revoke') is True
        except Exception:
            revocable=False
        return dict(dry_run=True,operation=operation,host=bundle['host'],machine_id=bundle['machine_id'],
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

    def tree(self,path):
        try:
            return runtime.inventory(path)['digest']
        except FileNotFoundError:
            return None
        except Exception:
            return 'untrusted'

    def effects(self,step,operation,bundle):
        stage=self.stage(operation)
        if step=='preflight':
            return dict(machine_id=self.system.machine()==bundle['machine_id'],
                        host_key=self.host_key()==bundle['plan']['host_key_sha256'],
                        runtime=self.tree(stage/'runtime')==bundle['plan']['runtime_tree_sha256'],
                        sources=self.tree(stage/'sources')==bundle['plan']['source_tree_sha256'])
        if step=='layout':
            result={}
            for logical,mode in installer.DIRECTORIES.items():
                target=self.path(logical)
                if not target.exists() and not target.is_symlink():
                    result[logical]='create %04o'%mode
                    continue
                info=target.lstat()
                ok=(stat.S_ISDIR(info.st_mode) and info.st_uid==os.geteuid()
                    and (stat.S_IMODE(info.st_mode)==mode or not logical.startswith(
                        ('/etc/ameesh-release','/var/lib/ameesh-release','/opt/ameesh-release/store'))))
                result[logical]='present' if ok else 'mismatch'
            return result
        if step in ('runtime','sources'):
            logical='/opt/ameesh-release/'+('runtime' if step=='runtime' else 'lib')
            pinned=bundle['plan']['runtime_tree_sha256' if step=='runtime' else 'source_tree_sha256']
            current=self.tree(self.path(logical))
            return {logical:'publish '+pinned if current is None else 'present' if current==pinned else 'conflict'}
        if step=='account':
            ids=bundle['account']
            try:
                group,user=self.system.group(),self.system.account()
            except Exception:
                return dict(account='unavailable')
            def state(observed,wanted):
                if observed is None:
                    return 'create'
                return 'present' if all(observed.get(k)==v for k,v in wanted.items()) else 'conflict'
            return dict(group=state(group,dict(gid=ids['gid'])),account=state(user,dict(uid=ids['uid'],gid=ids['gid'])),
                        uid=ids['uid'],gid=ids['gid'])
        if step in ('configuration','enable'):
            result={}
            for logical,info in sorted(bundle['files'].items()):
                if (logical==files_mod.KEY)!=(step=='enable'):
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
        if step=='validate':
            return dict(commands=['visudo -c -f /etc/sudoers.d/ameesh-release','sshd -T (effective delegation)'],
                        ready=self.ready(bundle),
                        seal=delegation.SEAL_DOCUMENT in bundle['files'])
        return {}

    def ready(self,bundle):
        try:
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

    def install(self,operation,decision,authority=None):
        prepared=self.prepared(operation)
        authority=authority or self.authority(decision,operation,prepared.bundle)
        adapter=JournaledAdapter(self.adapter(authority),self.gestes,'install')
        base=dict(command='install',operation=operation,bundle_digest=prepared.bundle['digest'],decision=decision)
        self.gestes.append(phase='start',**base)
        try:
            outcome=entry.install(prepared,authority=authority,adapter=adapter,journal=self.journal())
        except Exception as error:
            self.gestes.append(phase='held',category=type(error).__name__,**base)
            raise
        self.gestes.append(phase=outcome,**base)
        return dict(operation=operation,bundle_digest=prepared.bundle['digest'],outcome=outcome)

    def revoke(self,operation,decision,authority=None):
        """Signed rollback under the bootstrap's own host lock.

        `fichiers-bootstrap` removes the controller key first, restores only
        approved snapshots still matching the bundle and removes exact new
        files; runtime, account, journals and fences are kept.
        """
        prepared=self.prepared(operation); bundle=prepared.bundle
        authority=authority or self.authority(decision,operation,bundle)
        scopes=bootstrap.scopes(operation,bundle)
        request=dict(operation=operation,step='revoke',action_id=operation+':revoke',
                     scope_digest=scopes['revoke'],bundle=bundle)
        adapter=JournaledAdapter(self.adapter(authority),self.gestes,'revoke')
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
    decision=None
    if command in SIGNED:
        if len(rest)!=2 or rest[0]!='--decision' or not rest[1].isdigit() or int(rest[1])<=0:
            raise BackendError(command+' requires --decision N')
        decision=int(rest[1])
    elif rest:
        raise BackendError(command+' takes no option')
    return command,operation,decision


def dispatch(backend,command,operation,decision):
    if command in ('inventory','prepare'):
        return getattr(backend,command)(operation)
    return getattr(backend,command)(operation,decision)


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    try:
        command,operation,decision=parse(argv)
        if os.geteuid()!=0:
            raise BackendError('administrator backend runs as root')
        os.umask(0o077)
        enrollment=json.loads(Backend.private(runtime.trusted(ENROLLMENT),4096))
        db=connect() if command!='inventory' else None
        backend=Backend(system=installer.LinuxHost(),enrollment=enrollment,db=db)
        result=dispatch(backend,command,operation,decision)
    except (BackendError,entry.OperatorError,bootstrap.BootstrapError) as error:
        print(json.dumps(dict(ok=False,error=str(error))),file=sys.stderr)
        return 2
    except Exception as error:
        # Never print provider text: a fixed category only.
        print(json.dumps(dict(ok=False,error='uncertain',category=type(error).__name__)),file=sys.stderr)
        return 1
    print(json.dumps(result,sort_keys=True,indent=1))
    if command=='plan' and not result['authorized']:
        return 3
    return 0


if __name__=='__main__':
    sys.exit(main())

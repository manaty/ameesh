#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Administrator-only dedicated account creation with durable reconciliation.

No executable CLI or transport entry point; authority is independently enrolled.
Revocation/undo removes delegation, never guesses that account deletion is safe.
"""
import grp
import importlib.util
import os
from pathlib import Path
import pwd
import re
import subprocess

_spec = importlib.util.spec_from_file_location('account_protocol',
        Path(__file__).with_name('controleur-release.py'))
protocol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(protocol)

NAME = 'ameesh-release'
HOME = '/var/lib/ameesh-release/ssh'


def scope_digest(operation,machine_id,uid,gid, *, name=NAME,home=HOME):
    return protocol.identity(dict(version=1,operation=operation,machine_id=machine_id,
        account=dict(name=name,uid=uid,gid=gid,home=home,shell='/bin/sh',
                     password_disabled=True,supplementary_groups=[])))


def expected(uid,gid, *, name=NAME,home=HOME):
    """Exact reviewed identity `ensure()` admits for this uid/gid.

    One definition shared by the install and its dry run: a plan can only
    announce `present` when every field below already matches.
    """
    return dict(group=dict(name=name,gid=gid,members=[]),
                account=dict(name=name,uid=uid,gid=gid,home=home,shell='/bin/sh',
                             password_disabled=True,supplementary_groups=[]))


class AccountError(ValueError):
    pass


class LinuxAccounts:
    """Fixed system commands; never called by the restart SSH dispatcher."""
    def __init__(self):
        if os.geteuid()!=0:
            raise AccountError('administrator account backend required')

    def machine(self):
        return Path('/etc/machine-id').read_text().strip()

    @staticmethod
    def command(args):
        try:
            return subprocess.run(args,check=True,capture_output=True,text=True,timeout=30,
                    env={'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C'}).stdout
        except Exception:
            raise AccountError('account command outcome unavailable') from None

    def group(self,name=NAME):
        try:
            group = grp.getgrnam(name)
        except KeyError:
            return None
        return dict(name=group.gr_name,gid=group.gr_gid,members=list(group.gr_mem))

    def account(self,name=NAME):
        try:
            user = pwd.getpwnam(name)
        except KeyError:
            return None
        # The shadow value is never persisted, returned or logged. '*' is the
        # exact non-password sentinel installed by this backend, not a secret.
        shadow = self.command(['/usr/bin/getent','shadow',name]).strip().split(':')
        disabled = len(shadow)>=2 and shadow[0]==name and shadow[1]=='*'
        return dict(name=user.pw_name,uid=user.pw_uid,gid=user.pw_gid,
                    home=user.pw_dir,shell=user.pw_shell,password_disabled=disabled,
                    supplementary_groups=sorted(gid for gid in os.getgrouplist(name,user.pw_gid) if gid!=user.pw_gid))

    def free(self,uid,gid):
        try:
            pwd.getpwuid(uid)
            return False
        except KeyError:
            pass
        try:
            grp.getgrgid(gid)
            return False
        except KeyError:
            return True

    def create_group(self,gid,name=NAME):
        self.command(['/usr/sbin/groupadd','--system','--gid',str(gid),name])

    def create_account(self,uid,gid,name=NAME,home=HOME):
        self.command(['/usr/sbin/useradd','--system','--uid',str(uid),'--gid',str(gid),
                      '--home-dir',home,'--no-create-home','--no-log-init',
                      '--shell','/bin/sh','--password','*',name])


class Named:
    """One fixed account of a multi-account backend, seen as `ensure` expects.

    The name and home come from the reviewed renderer, never from a request.
    """
    def __init__(self,system,name,home):
        self.system,self.name,self.home=system,name,home

    def machine(self):
        return self.system.machine()

    def free(self,uid,gid):
        return self.system.free(uid,gid)

    def group(self):
        return self.system.group(self.name)

    def account(self):
        return self.system.account(self.name)

    def create_group(self,gid):
        self.system.create_group(gid,self.name)

    def create_account(self,uid,gid):
        self.system.create_account(uid,gid,self.name,self.home)


def ensure(journal,request, *, authority,system,name=NAME,home=HOME,step='account'):
    """Create exact enrolled uid/gid; reject pre-existing/unowned identities.

    request's scope_digest must be the account scope pinned by the signed
    bootstrap decision; it is recomputed over the exact uid/gid/host/operation.
    No false-success or blind command retry after an ambiguous
    disappearance; the durable intent remains for administrator reconciliation.
    """
    if (not isinstance(request,dict) or set(request)!={'operation','machine_id','uid','gid','scope_digest'}
            or not isinstance(request['operation'],str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',request['operation'])
            or not isinstance(request['machine_id'],str) or not re.fullmatch(r'[0-9a-f]{32}',request['machine_id'])
            or not isinstance(request['scope_digest'],str) or not re.fullmatch(r'[0-9a-f]{64}',request['scope_digest'])
            or any(type(request[name]) is not int or not 100<=request[name]<=60000 for name in ('uid','gid'))):
        raise AccountError('exact account enrollment required')
    if request['scope_digest']!=scope_digest(request['operation'],request['machine_id'],request['uid'],request['gid'],
                                             name=name,home=home):
        raise AccountError('account scope digest mismatch')
    request = dict(request)
    reviewed = expected(request['uid'],request['gid'],name=name,home=home)
    expected_group = reviewed['group']
    expected_account = reviewed['account']
    def approved():
        try:
            valid = (system.machine()==request['machine_id']
                     and authority.validate(request['operation'],request['scope_digest'],step) is True)
        except Exception:
            valid = False
        if not valid:
            raise AccountError('account host authority unavailable')
    approved()
    with protocol.locked(journal,'bootstrap-account') as directory:
        approved()
        # Operation names never contain a dot: one intent per account.
        path = directory/(request['operation']+('.json' if name==NAME else '.'+name+'.json'))
        if path.exists() or path.is_symlink():
            record = protocol.load(path)
            if (set(record)!={'request','phase','receipt'} or record['request']!=request
                    or record['phase'] not in ('group','user_ready','user','complete')
                    or (record['phase']!='complete' and record['receipt'] is not None)):
                raise AccountError('account intent mismatch')
        else:
            if system.group() is not None or system.account() is not None or not system.free(request['uid'],request['gid']):
                raise AccountError('unowned account or numeric identity exists')
            approved()
            record = dict(request=request,phase='group',receipt=None)
            protocol.save(path,record)
            # From this point, an exception is an uncertain effect. Never
            # delete a group/account or resubmit an existing ambiguous intent.
            system.create_group(request['gid'])
        if system.group()!=expected_group:
            raise AccountError('account group unresolved or changed')
        if record['phase']=='group':
            if system.account() is not None:
                raise AccountError('unowned account appeared before user intent')
            approved()
            record['phase']='user_ready'
            protocol.save(path,record)
        if record['phase']=='user_ready':
            if system.account() is not None:
                raise AccountError('unowned account appeared before user intent')
            approved()
            record['phase']='user'
            protocol.save(path,record)
            approved()
            system.create_account(request['uid'],request['gid'])
        if system.account()!=expected_account:
            raise AccountError('account creation unresolved or identity changed')
        approved()
        receipt = dict(request,account=expected_account,group=expected_group,ok=True)
        if record['phase']=='complete':
            if protocol.identity(record['receipt'])!=protocol.identity(receipt):
                raise AccountError('account receipt mismatch')
            return receipt
        protocol.save(path,dict(request=request,phase='complete',receipt=receipt))
        return receipt

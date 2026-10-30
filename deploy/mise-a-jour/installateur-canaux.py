#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Host effects of the signed channel bootstrap (#4327): accounts and SSH channels.

No CLI: `backend-administrateur.py` is the one root entry. Each effect
revalidates the scoped signed decision and the host identity first. Files,
snapshots and rollback are `fichiers-bootstrap.BootstrapFiles` bound to the
channel renderer; accounts are `compte-release.ensure` bound to each fixed
account name and home. Both controller keys are written last, after the
effective sudoers/sshd settings are loaded and validated.
"""
import base64
import hashlib
import importlib.util
import ipaddress
from pathlib import Path


def module(name):
    spec=importlib.util.spec_from_file_location('channels_install_'+name.replace('-','_'),
        Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


installer=module('installateur-bootstrap')
bootstrap=module('bootstrap-canaux')
delegation=bootstrap.delegation
account=bootstrap.account
runtime=installer.runtime
file_mod=installer.file_mod
InstallError=installer.InstallError
#: Installed release trees the channel dispatchers run from (never published here).
TREES={'/opt/ameesh-release/runtime':'runtime_tree_sha256','/opt/ameesh-release/lib':'source_tree_sha256'}


class LinuxChannels(installer.LinuxHost):
    """Fixed system commands of the channel bootstrap; root only."""

    def preflight_channels(self,bundle):
        if self.machine()!=bundle['machine_id']:
            raise InstallError('bootstrap host mismatch')
        key=Path('/etc/ssh/ssh_host_ed25519_key.pub')
        runtime.trusted(key)
        parts=key.read_text().split()
        if (len(parts)<2 or parts[0]!='ssh-ed25519'
                or hashlib.sha256(base64.b64decode(parts[1],validate=True)).hexdigest()!=bundle['plan']['host_key_sha256']):
            raise InstallError('pinned host key mismatch')
        return self.trees_ready(bundle)

    def trees_ready(self,bundle):
        # The release bootstrap installed these trees; a different or writable
        # tree means the dispatchers would not be the reviewed ones.
        try:
            measured={}
            for logical,field in TREES.items():
                observed=runtime.inventory(logical)
                if observed['digest']!=bundle['plan'][field]:
                    return False
                for relative in observed['dirs']+list(observed['files']):
                    if (Path(logical)/relative).stat().st_mode & 0o222:
                        return False
                measured[logical]=observed
            # Presence is not enough: each dispatcher must be a file of the
            # measured source tree, so its bytes are bound by the pinned digest.
            lib='/opt/ameesh-release/lib'
            for dispatcher in bundle['dispatchers'].values():
                relative=str(Path(dispatcher).relative_to(lib))
                if relative not in measured[lib]['files']:
                    return False
                runtime.trusted(dispatcher)
        except Exception:
            return False
        return True

    def layout_channels(self,bundle):
        return self.create_layout(delegation.DIRECTORIES,delegation.PRIVATE)

    def validate_channels(self,bundle):
        sshd=next((path for path in ('/usr/bin/sshd','/usr/sbin/sshd') if Path(path).is_file()),None)
        if sshd is None:
            raise InstallError('enrolled SSH daemon unavailable')
        runtime.trusted(sshd)
        for name,channel in sorted(delegation.CHANNELS.items()):
            runtime.trusted('/usr/sbin/visudo')
            self.command(['/usr/sbin/visudo','-c','-f','/etc/sudoers.d/'+channel['account']])
            source=bundle['plan']['channels'][name]['controller_source']
            address=str(ipaddress.ip_network(source).network_address)
            args=[sshd,'-T','-C','user='+channel['account']+',host='+bundle['host']+',addr='+address]
            text=self.command(args)
            if not text:
                text=self.command([sshd,'-T','-G',*args[2:]])
            lines=text.lower().splitlines()
            expected=['forcecommand '+channel['wrapper'],'authorizedkeysfile '+channel['keys'],
                'authorizedkeyscommand none','authenticationmethods publickey','passwordauthentication no',
                'kbdinteractiveauthentication no','disableforwarding yes','permittty no',
                'permituserrc no','maxsessions 1']
            if any(item not in lines for item in expected):
                raise InstallError('effective SSH channel delegation mismatch')
        return True


class ChannelHostAdapter:
    def __init__(self,journal, *, authority,system,root='/'):
        # Test-only root mapping is not exposed by any CLI argument.
        self.journal=Path(journal); self.authority,self.system=authority,system
        self.root=Path(root).absolute(); runtime.trusted(self.root)
        self.files=file_mod.BootstrapFiles(self.journal/'files',authority=authority,root=self.root,
            readiness=lambda request:system.trees_ready(request['bundle']) is True,
            renderer=delegation,keys=delegation.KEYS)

    def approved(self,request):
        if (self.system.machine()!=request['bundle']['machine_id']
                or self.authority.validate(request['operation'],request['scope_digest'],request['step']) is not True):
            raise InstallError('bootstrap effect authority unavailable')

    def perform(self,step,request):
        if (not isinstance(request,dict) or set(request)!={'operation','step','action_id','scope_digest','bundle'}
                or step not in bootstrap.ACTIONS or request['step']!=step
                or request['action_id']!=request['operation']+':'+step
                or bootstrap.scopes(request['operation'],request['bundle'])[step]!=request['scope_digest']):
            raise InstallError('bootstrap effect scope mismatch')
        self.approved(request)
        operation=request['operation']; bundle=request['bundle']
        if step=='preflight':
            if self.system.preflight_channels(bundle) is not True:
                raise InstallError('channel preflight unavailable')
        elif step=='layout':
            if self.system.layout_channels(bundle) is not True:
                raise InstallError('channel layout unavailable')
        elif step in bootstrap.ACCOUNT_STEPS:
            ids=bundle['accounts'][bootstrap.ACCOUNT_STEPS[step]]
            value=dict(operation=operation,machine_id=bundle['machine_id'],uid=ids['uid'],gid=ids['gid'],
                       scope_digest=request['scope_digest'])
            account.ensure(self.journal/'account',value,authority=self.authority,
                           system=account.Named(self.system,ids['name'],ids['home']),
                           name=ids['name'],home=ids['home'],step=step)
        elif step=='revoke':
            receipt=self.files.perform(step,request)
            if self.system.validate_restored_configuration(bundle) is not True:
                raise InstallError('restored SSH/sudo configuration unavailable')
            self.approved(request)
            if self.system.activate_ssh_configuration(bundle) is not True:
                raise InstallError('SSH revocation activation unavailable')
            self.approved(request)
            return receipt
        elif step=='configuration':
            return self.files.perform(step,request)
        elif step=='validate':
            if self.system.validate_channels(bundle) is not True or self.system.trees_ready(bundle) is not True:
                raise InstallError('channel delegation not valid')
        elif step=='enable':
            if self.system.trees_ready(bundle) is not True:
                raise InstallError('release trees not ready for the channels')
            self.approved(request)
            # Keys stay absent until the fixed forced settings are loaded and
            # validated again; reload is idempotent after a lost reply.
            if self.system.activate_ssh_configuration(bundle) is not True:
                raise InstallError('SSH configuration activation unavailable')
            if self.system.validate_channels(bundle) is not True:
                raise InstallError('SSH configuration changed during activation')
            self.approved(request)
            return self.files.perform(step,request)
        self.approved(request)
        return dict(request,ok=True)

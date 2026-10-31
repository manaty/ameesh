#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Connect signed bootstrap ordering to fixed administrator host effects.

No CLI, enabled service, credential delivery or live operation is provided.
The real host backend refuses access until independent service readiness exists.
"""
import base64
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess


def module(name):
    spec=importlib.util.spec_from_file_location('bootstrap_install_'+name.replace('-','_'),
        Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


bootstrap=module('bootstrap-release')
runtime=module('runtime-release')
file_mod=module('fichiers-bootstrap')
account=bootstrap.account
protocol=bootstrap.protocol
DIRECTORIES=bootstrap.delegation.DIRECTORIES


class InstallError(ValueError):
    pass


class LinuxHost(account.LinuxAccounts):
    @staticmethod
    def private_json(path):
        runtime.trusted(path)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o077
                    or info.st_size>65536):
                raise InstallError('private host enrollment required')
            return json.loads(stream.read(65537))

    def preflight(self,bundle):
        if self.machine()!=bundle['machine_id']:
            raise InstallError('bootstrap host mismatch')
        key=Path('/etc/ssh/ssh_host_ed25519_key.pub')
        runtime.trusted(key)
        parts=key.read_text().split()
        if (len(parts)<2 or parts[0]!='ssh-ed25519'
                or hashlib.sha256(base64.b64decode(parts[1],validate=True)).hexdigest()!=bundle['plan']['host_key_sha256']):
            raise InstallError('pinned host key mismatch')
        return True

    def layout(self,bundle):
        return self.create_layout(DIRECTORIES,('/etc/ameesh-release','/var/lib/ameesh-release',
                                               '/opt/ameesh-release/store'))

    def create_layout(self,directories,private):
        """Create the reviewed directories; `private` roots keep their exact mode."""
        for logical,mode in directories.items():
            path=Path(logical)
            # Trusted fixed system ancestors only. Never repair ownership or
            # permissions of an existing unrelated directory as a side effect.
            missing=[]; parent=path
            while not parent.exists() and not parent.is_symlink():
                missing.append(parent); parent=parent.parent
            runtime.trusted(parent)
            for item in reversed(missing):
                wanted=mode if item==path else 0o755
                item.mkdir(mode=wanted)
                # L1631: mkdir(2) applies the caller's umask, so a directory
                # this call just created gets its reviewed mode exactly; an
                # existing directory is never repaired (comment above).
                os.chmod(item,wanted)
                runtime.fsync_directory(item.parent)
            runtime.trusted(path)
            info=path.stat()
            if info.st_uid!=0 or not stat.S_ISDIR(info.st_mode):
                raise InstallError('bootstrap directory not root-owned')
            # Existing generic system directories may be more restrictive; the
            # private roots below keep their reviewed mode exactly.
            if logical.startswith(tuple(private)) and stat.S_IMODE(info.st_mode)!=mode:
                raise InstallError('bootstrap private layout mismatch')
        return True

    def validate_configuration(self,bundle):
        runtime.trusted('/usr/sbin/visudo')
        self.command(['/usr/sbin/visudo','-c','-f','/etc/sudoers.d/ameesh-release'])
        sshd=next((path for path in ('/usr/bin/sshd','/usr/sbin/sshd') if Path(path).is_file()),None)
        if sshd is None:
            raise InstallError('enrolled SSH daemon unavailable')
        runtime.trusted(sshd)
        for entry in bootstrap.delegation.transport_entries(bundle['plan']):
            address=str(ipaddress.ip_network(entry['controller_source']).network_address)
            args=[sshd,'-T','-C','user=ameesh-release,host='+bundle['host']+',addr='+address]
            text=self.command(args)
            if not text:
                # Some enrolled OpenSSH builds require -G as well to print config.
                text=self.command([sshd,'-T','-G',*args[2:]])
            lines=text.lower().splitlines()
            expected=['forcecommand '+bootstrap.delegation.WRAPPER,
                'authorizedkeysfile /etc/ameesh-release/authorized_keys','authorizedkeyscommand none',
                'authenticationmethods publickey','passwordauthentication no','kbdinteractiveauthentication no',
                'disableforwarding yes','permittty no','permituserrc no','maxsessions 1']
            if any(item not in lines for item in expected):
                raise InstallError('effective SSH delegation mismatch')
        # Never reload a daemon or enable a service from this validation call.
        return True

    def ready(self,bundle):
        # A daemon merely being active is insufficient. The separately enrolled
        # complete release adapter must publish its private readiness bound to
        # this immutable runtime/source closure and its current systemd instance.
        try:
            for logical,field in (('/opt/ameesh-release/runtime','runtime_tree_sha256'),
                                   ('/opt/ameesh-release/lib','source_tree_sha256')):
                observed=runtime.inventory(logical)
                if observed['digest']!=bundle['plan'][field]:
                    return False
                for relative in observed['dirs']+list(observed['files']):
                    if (Path(logical)/relative).stat().st_mode & 0o222:
                        return False
            enrollment=self.private_json('/etc/ameesh-release/enrollment.json')
            if (enrollment['host']!=bundle['host'] or enrollment['machine_id']!=bundle['machine_id']
                    or enrollment['policy']!=bundle['plan']['policy']):
                return False
            receipt=self.private_json('/var/lib/ameesh-release/controller/readiness.json')
            text=self.command(['/usr/bin/systemctl','show','ameesh-release-controller.service',
                '--property=LoadState,ActiveState,Slice,InvocationID'])
            fields=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
            expected=dict(host=bundle['host'],machine_id=bundle['machine_id'],
                runtime_tree_sha256=bundle['plan']['runtime_tree_sha256'],
                source_tree_sha256=bundle['plan']['source_tree_sha256'],
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                invocation_id=fields.get('InvocationID'),adapter_ready=True)
            return (fields.get('LoadState')=='loaded' and fields.get('ActiveState')=='active'
                and fields.get('Slice')=='system.slice'
                and re.fullmatch(r'[0-9a-f]{32}',fields.get('InvocationID','')) is not None
                and protocol.identity(receipt)==protocol.identity(expected))
        except Exception:
            return False

    def activate_ssh_configuration(self,bundle):
        self.command(['/usr/bin/systemctl','reload',bundle['services']['ssh_reload']])
        return True

    def validate_restored_configuration(self,bundle):
        runtime.trusted('/usr/sbin/visudo')
        self.command(['/usr/sbin/visudo','-c'])
        sshd=next((path for path in ('/usr/bin/sshd','/usr/sbin/sshd') if Path(path).is_file()),None)
        if sshd is None:
            raise InstallError('enrolled SSH daemon unavailable')
        runtime.trusted(sshd)
        self.command([sshd,'-t'])
        return True


class BootstrapHostAdapter:
    def __init__(self,journal, *, authority,system,root='/'):
        # Test-only root mapping is not exposed by SSH or any CLI. Production
        # uses the fixed root and a separately enrolled administrator backend.
        self.journal=Path(journal); self.authority,self.system=authority,system
        self.root=Path(root).absolute(); runtime.trusted(self.root)
        self.files=file_mod.BootstrapFiles(self.journal/'files',authority=authority,root=self.root,
            readiness=lambda request:system.ready(request['bundle']))

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
        stage=self.root/('var/lib/ameesh-release-bootstrap/'+operation)
        if step=='preflight':
            if self.system.preflight(bundle) is not True:
                raise InstallError('bootstrap preflight unavailable')
            for name,field in (('runtime','runtime_tree_sha256'),('sources','source_tree_sha256')):
                if runtime.inventory(stage/name)['digest']!=bundle['plan'][field]:
                    raise InstallError('bootstrap staged tree mismatch')
        elif step in ('runtime','sources'):
            logical='/opt/ameesh-release/'+('runtime' if step=='runtime' else 'lib')
            field='runtime_tree_sha256' if step=='runtime' else 'source_tree_sha256'
            runtime.publish(stage/step,self.root/logical.lstrip('/'),self.journal/'trees',operation+'-'+step,
                bundle['plan'][field],authorize=lambda:self._approved(request))
        elif step=='layout':
            if self.system.layout(bundle) is not True:
                raise InstallError('bootstrap layout unavailable')
        elif step=='account':
            value=dict(operation=operation,machine_id=bundle['machine_id'],uid=bundle['account']['uid'],
                       gid=bundle['account']['gid'],scope_digest=request['scope_digest'])
            account.ensure(self.journal/'account',value,authority=self.authority,system=self.system)
        elif step in ('configuration','enable','revoke'):
            if step=='revoke':
                receipt=self.files.perform(step,request)
                if self.system.validate_restored_configuration(bundle) is not True:
                    raise InstallError('restored SSH/sudo configuration unavailable')
                self.approved(request)
                if self.system.activate_ssh_configuration(bundle) is not True:
                    raise InstallError('SSH revocation activation unavailable')
                self.approved(request)
                return receipt
            if step=='enable':
                if self.system.ready(bundle) is not True:
                    raise InstallError('independent release adapter not ready')
                self.approved(request)
                # Reload is idempotent and validated again after a lost reply.
                # The controller key remains absent until fixed forced settings
                # are loaded and independent readiness is still current.
                if self.system.activate_ssh_configuration(bundle) is not True:
                    raise InstallError('SSH configuration activation unavailable')
                if self.system.validate_configuration(bundle) is not True:
                    raise InstallError('SSH configuration changed during activation')
                self.approved(request)
            return self.files.perform(step,request)
        elif step=='validate':
            seal_info=bundle['files'].get(bootstrap.delegation.SEAL_DOCUMENT)
            if seal_info is not None:
                seal=module('scellement-runtime')
                seal.verify(json.loads(seal_info['content']),
                            self.root/'opt/ameesh-release')
            if self.system.validate_configuration(bundle) is not True or self.system.ready(bundle) is not True:
                raise InstallError('independent release adapter not ready')
        self.approved(request)
        return dict(request,ok=True)

    def _approved(self,request):
        self.approved(request)
        return True

#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare host-bound SSH/sudo review artifacts, never install or request root.

The runtime/source tree digests and host-key proof are enrollment prerequisites,
not assertions established by this renderer. No credential value is accepted.
"""
import base64
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import struct

_spec = importlib.util.spec_from_file_location('delegation_restart',
        Path(__file__).with_name('helper-redemarrage.py'))
restart = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(restart)
_unit_spec=importlib.util.spec_from_file_location('delegation_unit',Path(__file__).with_name('unite-controleur.py'))
unit=importlib.util.module_from_spec(_unit_spec)
_unit_spec.loader.exec_module(unit)

WRAPPER = '/usr/local/libexec/ameesh-release-ssh'
DISPATCHER = '/opt/ameesh-release/lib/hote-release.py'
RUNTIME = '/opt/ameesh-release/runtime/bin/python'
DIRECTORIES={
    '/opt/ameesh-release':0o755,'/etc/ameesh-release':0o755,
    '/etc/ameesh-release/operations':0o700,'/usr/local/libexec':0o755,
    '/etc/sudoers.d':0o750,'/etc/ssh/sshd_config.d':0o755,
    '/var/lib/ameesh-release':0o755,'/var/lib/ameesh-release/controller':0o700,
    '/var/lib/ameesh-release/helper':0o700,'/var/lib/ameesh-release/bootstrap':0o700,
    '/var/lib/ameesh-release/ssh':0o755,
}


class DelegationError(ValueError):
    pass


def validate(plan):
    fields = {'host', 'machine_id', 'controller_source', 'controller_key',
              'host_key_sha256', 'runtime_tree_sha256', 'source_tree_sha256', 'policy'}
    if not isinstance(plan, dict) or set(plan) != fields:
        raise DelegationError('invalid delegation plan')
    try:
        restart.validate_policy(plan['policy'])
        if plan['host'] != plan['policy']['host']:
            raise DelegationError('delegation host mismatch')
        if not isinstance(plan['machine_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', plan['machine_id']):
            raise DelegationError('exact machine identity required')
        for name in ('host_key_sha256','runtime_tree_sha256','source_tree_sha256'):
            if not isinstance(plan[name], str) or not re.fullmatch(r'[0-9a-f]{64}',plan[name]):
                raise DelegationError('pinned artifact digest required')
        if (not isinstance(plan['controller_source'],str)
                or str(ipaddress.ip_network(plan['controller_source'],strict=True)) != plan['controller_source']):
            raise DelegationError('canonical controller source network required')
        # Accept one bare public Ed25519 key, with no authorized_keys options or
        # comment. Parse the SSH wire format, rather than merely checking base64.
        key = plan['controller_key']
        if not isinstance(key,str) or not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,2}',key):
            raise DelegationError('one controller public key required')
        data = base64.b64decode(key.split(' ')[1],validate=True)
        expected = struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)
        if len(data) != 51 or not data.startswith(expected):
            raise DelegationError('invalid controller public key')
    except DelegationError:
        raise
    except Exception:
        raise DelegationError('invalid delegation enrollment') from None


def render(plan, *, account_ids=None,previous_files=None):
    """Return exact install/revoke inventory; this function has no host effects."""
    plan = json.loads(json.dumps(plan))
    validate(plan)
    if account_ids is not None and (not isinstance(account_ids,dict) or set(account_ids)!={'uid','gid'}
            or any(type(account_ids[name]) is not int or not 100<=account_ids[name]<=60000 for name in ('uid','gid'))):
        raise DelegationError('exact account numeric identity required')
    forced = '''#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
set -eu
if [ "$#" -ne 0 ] || [ -n "${SSH_ORIGINAL_COMMAND:-}" ]; then
    printf '%s\\n' '{"error":"host_action_unavailable"}'
    exit 1
fi
exec /usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C /usr/bin/sudo -n -- '''+RUNTIME+' -I -B '+DISPATCHER+'\n'
    sudo = ('# SPDX-License-Identifier: AGPL-3.0-only\n'
            'Defaults:ameesh-release env_reset,!setenv\n'
            'Defaults:ameesh-release secure_path="/usr/bin:/bin"\n'
            'ameesh-release ALL=(root) NOPASSWD: '+RUNTIME+' -I -B '+DISPATCHER+'\n')
    keys = ('restrict,from="'+plan['controller_source']+'",command="'+WRAPPER+'" '
            +plan['controller_key']+'\n')
    sshd = ('# SPDX-License-Identifier: AGPL-3.0-only\n'
            'Match User ameesh-release\n'
            '    AuthorizedKeysFile /etc/ameesh-release/authorized_keys\n'
            '    AuthorizedKeysCommand none\n'
            '    AuthenticationMethods publickey\n'
            '    PasswordAuthentication no\n'
            '    KbdInteractiveAuthentication no\n'
            '    ForceCommand '+WRAPPER+'\n'
            '    DisableForwarding yes\n'
            '    PermitTTY no\n'
            '    PermitUserRC no\n'
            '    MaxSessions 1\n'
            'Match all\n')
    files = {}
    for path, mode, content in (
            (WRAPPER,0o755,forced),
            ('/etc/sudoers.d/ameesh-release',0o440,sudo),
            # sshd reads keys with the account's privileges. Public key data
            # is readable; root ownership prevents the account changing it.
            ('/etc/ameesh-release/authorized_keys',0o644,keys),
            ('/etc/ssh/sshd_config.d/60-ameesh-release.conf',0o600,sshd)):
        files[path] = dict(owner='root',group='root',mode=mode,content=content,
                          sha256=hashlib.sha256(content.encode()).hexdigest())
    files['/etc/systemd/system/ameesh-release-controller.service']=dict(owner='root',group='root',mode=0o644,
        content=unit.UNIT,sha256=hashlib.sha256(unit.UNIT.encode()).hexdigest())
    if previous_files is None:
        previous_files={path:None for path in files}
    if not isinstance(previous_files,dict) or set(previous_files)!=set(files):
        raise DelegationError('exact previous file inventory required')
    for info in previous_files.values():
        if info is not None and (not isinstance(info,dict) or set(info)!={'sha256','mode'}
                or not isinstance(info['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',info['sha256'])
                or type(info['mode']) is not int or not 0<=info['mode']<=0o777 or info['mode'] & 0o022):
            raise DelegationError('exact protected previous file required')
    review = dict(version=1,host=plan['host'],machine_id=plan['machine_id'],
            plan=plan,policy_id=restart.policy_id(plan['policy']),files=files,previous_files=previous_files,
            directories=dict(DIRECTORIES),services=dict(ssh_reload='sshd.service',controller='ameesh-release-controller.service'),
            account=dict(name='ameesh-release',system=True,shell='/bin/sh',
                         supplementary_groups=[],password_login=False),
            prerequisites=[
                'Owner/admin verifies exact machine-id and pinned SSH host key.',
                'Dedicated account has no alternate credentials or supplementary groups.',
                'Root-owned /etc/ameesh-release is searchable by the SSH account; credential and operation files remain root-private.',
                'Complete isolated runtime and helper sources match enrolled tree digests, root-owned with no writable/linked ancestors.',
                'Python toolchain standard library/shared libraries are immutable and independently pinned.',
                'Root-private enrollment, database credentials, operations and independent controller journal exist.',
                'No existing destination file is replaced without its explicit diff and undo snapshot.',
                'Administrator validates sudoers and effective sshd Match settings before enabling the controller key.',
                'Administrator enables only after independent controller service and complete adapter are enrolled.'
            ],
            revoke=dict(remove_only_matching_sha256={path:item['sha256'] for path,item in files.items()},
                        preserve=['runtime','helper sources','database','enrollment','operations',
                                  'controller journal','helper journal','live release fences'],
                        action='Administrator restores only signed previous-file snapshots, or removes newly created files, while current contents match this exact delegation. Revoke the controller key first and verify effective SSH configuration. Changed files require a separate reviewed diff.'))
    if account_ids is not None:
        review['account'].update(account_ids)
    return dict(review, digest=restart.protocol.identity(review))


def question(bundle):
    """Prepare signed-decide text; do not send it or claim it was approved."""
    if not isinstance(bundle,dict) or 'digest' not in bundle:
        raise DelegationError('review bundle required')
    numeric = {key:bundle['account'][key] for key in ('uid','gid') if key in bundle['account']}
    expected = render(bundle['plan'],account_ids=numeric or None,previous_files=bundle.get('previous_files'))
    if restart.protocol.identity(bundle) != restart.protocol.identity(expected):
        raise DelegationError('review bundle changed')
    return ('Approve prepared root SSH/sudo delegation on '+bundle['host']+
            ', machine-id '+bundle['machine_id']+', bundle '+bundle['digest']+
            '? Review exact files/account/prerequisites and digest-bound revoke plan:\n'+
            json.dumps(bundle,sort_keys=True,separators=(',',':'))+
            '\nThis delegates only the enrolled restart/recovery-restart dispatcher; '
            'each host action still requires its scoped signed release authority. '
            'No installation is authorized by preparing this question.')

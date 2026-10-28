#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Signed review bundle of the handoff and secret channel accounts (#4327).

The release bundle (`delegation-release.py`) enrolls only `ameesh-release`
and its paths. This renderer prepares the second, separately signed bundle
the canary pair needs: the dedicated `ameesh-handoff` (H2) and `ameesh-secrets`
(L232c) accounts, their fixed roots `/etc/ameesh-handoff`, `/etc/ameesh-secrets`,
`/var/lib/ameesh-handoff` and `/var/lib/ameesh-secrets`, and one forced-command
SSH channel per account, each with its own controller key and source.

Like the release renderer it never installs anything or requests root: the
administrator backend executes the bundle on a scoped signed decision. The
runtime and helper sources are those of the release bootstrap, which must
already be installed at the pinned digests; this bundle publishes no tree.
No enrollment document (`execution.json`, `policy.json`, `helper.json`...),
credential value or destination list is accepted here.
"""
import importlib.util
import json
from pathlib import Path


def module(name):
    spec=importlib.util.spec_from_file_location('channels_'+name.replace('-','_'),
        Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


release=module('delegation-release')
secrets=module('delegation-secrets')
DelegationError=release.DelegationError
KIND='channels'
#: Fixed per-channel layout. Handoff follows docs/ops/handoff-staging-enrollment.md;
#: secrets is exactly `delegation-secrets.py`. The account home is the channel's
#: root-owned /etc root: searchable, never writable by the account.
CHANNELS={
    'handoff':dict(account='ameesh-handoff',home='/etc/ameesh-handoff',
                   wrapper='/usr/local/libexec/ameesh-handoff-ssh',
                   dispatcher='/opt/ameesh-release/lib/hote-handoff.py',
                   keys='/etc/ameesh-handoff/authorized_keys',
                   sshd='/etc/ssh/sshd_config.d/62-ameesh-handoff.conf'),
    'secrets':dict(account=secrets.ACCOUNT,home='/etc/ameesh-secrets',
                   wrapper=secrets.WRAPPER,dispatcher=secrets.DISPATCHER,
                   keys=secrets.KEYS,sshd=secrets.SSHD),
}
KEYS=tuple(channel['keys'] for channel in CHANNELS.values())
DIRECTORIES={
    '/usr/local/libexec':0o755,
    '/etc/sudoers.d':0o750,
    '/etc/ssh/sshd_config.d':0o755,
    # Searchable for sshd's key lookup as the account; private documents
    # enrolled later inside stay root-only (0600).
    '/etc/ameesh-handoff':0o755,
    '/etc/ameesh-secrets':0o755,
    # Host-side journals of each channel executor: root-private.
    '/var/lib/ameesh-handoff':0o700,
    '/var/lib/ameesh-secrets':0o700,
}
#: Roots this bundle owns: an existing one must already have its exact mode.
PRIVATE=('/etc/ameesh-handoff','/etc/ameesh-secrets','/var/lib/ameesh-handoff','/var/lib/ameesh-secrets')
HOST=frozenset({'host','machine_id','host_key_sha256','runtime_tree_sha256','source_tree_sha256'})
ACCESS=frozenset({'controller_source','controller_key'})


def validate(plan):
    if (not isinstance(plan,dict) or set(plan)!=HOST|{'channels'}
            or not isinstance(plan['channels'],dict) or set(plan['channels'])!=set(CHANNELS)):
        raise DelegationError('invalid channel delegation plan')
    for name,access in plan['channels'].items():
        if not isinstance(access,dict) or set(access)!=ACCESS:
            raise DelegationError('invalid channel delegation plan')
        release.validate_access(dict({key:plan[key] for key in HOST},**access))
    if len({access['controller_key'] for access in plan['channels'].values()})!=len(CHANNELS):
        # No key sharing between channels (L232c): one key per account.
        raise DelegationError('one distinct controller key per channel required')


def validate_account_ids(account_ids):
    if not isinstance(account_ids,dict) or set(account_ids)!=set(CHANNELS):
        raise DelegationError('exact channel account numeric identities required')
    for ids in account_ids.values():
        if ids is None:
            raise DelegationError('exact channel account numeric identities required')
        release.validate_account_ids(ids)
    uids=[ids['uid'] for ids in account_ids.values()]; gids=[ids['gid'] for ids in account_ids.values()]
    if len(set(uids))!=len(uids) or len(set(gids))!=len(gids):
        raise DelegationError('distinct channel account identities required')


def render(plan, *, account_ids,previous_files=None):
    """Return the exact install/revoke inventory; this function has no host effects."""
    plan=json.loads(json.dumps(plan))
    validate(plan)
    validate_account_ids(account_ids)
    files={}
    for name,channel in sorted(CHANNELS.items()):
        access=dict({key:plan[key] for key in HOST},**plan['channels'][name])
        for path,(mode,content) in release.access_files(access,account=channel['account'],
                wrapper=channel['wrapper'],dispatcher=channel['dispatcher'],
                keys_file=channel['keys'],sshd_file=channel['sshd']).items():
            files[path]=release.root_file(mode,content)
    previous_files=release.validate_previous(previous_files,files)
    accounts={name:dict(name=channel['account'],system=True,shell='/bin/sh',home=channel['home'],
                        supplementary_groups=[],password_login=False,**account_ids[name])
              for name,channel in CHANNELS.items()}
    review=dict(version=1,kind=KIND,host=plan['host'],machine_id=plan['machine_id'],
            plan=plan,files=files,previous_files=previous_files,
            directories=dict(DIRECTORIES),services=dict(ssh_reload='sshd.service'),
            accounts=accounts,
            dispatchers={name:channel['dispatcher'] for name,channel in CHANNELS.items()},
            prerequisites=[
                'The release bootstrap is installed: runtime and helper sources match the pinned tree digests of this plan.',
                'Owner/admin verifies exact machine-id and pinned SSH host key.',
                'ameesh-handoff and ameesh-secrets are distinct from ameesh-release and from each other, with no alternate credentials or supplementary groups.',
                'Each channel has its own controller key and source; no key is shared with another channel or the restart delegation.',
                'Root-owned /etc/ameesh-handoff and /etc/ameesh-secrets are searchable by their account; enrolled documents inside remain root-private.',
                'Enrollment documents (execution.json, policy.json, helper.json, transports) are enrolled by their own signed decision; until then each dispatcher refuses.',
                'No existing destination file is replaced without its explicit diff and undo snapshot.',
                'Administrator validates sudoers and effective sshd Match settings before enabling the controller keys.'
            ],
            revoke=dict(remove_only_matching_sha256={path:item['sha256'] for path,item in files.items()},
                        preserve=['accounts','runtime','helper sources','channel roots','channel journals',
                                  'enrollment documents','installed destinations'],
                        action='Administrator revokes both controller keys first, then restores only signed previous-file snapshots, or removes newly created files, while current contents match this exact delegation. Accounts, roots and journals are kept; installed credentials are revoked per destination by their helper.'))
    return dict(review,digest=release.restart.protocol.identity(review))


def numeric(bundle):
    try:
        return {name:{key:bundle['accounts'][name][key] for key in ('uid','gid')} for name in CHANNELS}
    except (KeyError,TypeError):
        raise DelegationError('review bundle changed') from None


def question(bundle):
    """Prepare signed-decide text; do not send it or claim it was approved."""
    if not isinstance(bundle,dict) or 'digest' not in bundle or bundle.get('kind')!=KIND:
        raise DelegationError('review bundle required')
    expected=render(bundle['plan'],account_ids=numeric(bundle),previous_files=bundle.get('previous_files'))
    if release.restart.protocol.identity(bundle)!=release.restart.protocol.identity(expected):
        raise DelegationError('review bundle changed')
    return ('Approve prepared root SSH/sudo delegation of the handoff and secret channels on '+bundle['host']+
            ', machine-id '+bundle['machine_id']+', bundle '+bundle['digest']+
            '? Review exact files/accounts/prerequisites and digest-bound revoke plan:\n'+
            json.dumps(bundle,sort_keys=True,separators=(',',':'))+
            '\nThis delegates only the enrolled handoff executor (hote-handoff.py) and secret '
            'installation dispatcher (hote-secrets.py); it does not widen the restart delegation. '
            'No installation is authorized by preparing this question.')

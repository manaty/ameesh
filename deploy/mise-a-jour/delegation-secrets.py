#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare host-bound SSH/sudo review artifacts of the secret channel (L232c).

A separate account (`ameesh-secrets`), forced command, sudoers line and sshd
Match reach only `hote-secrets.py`; L232b's restart delegation is not widened.
Like `delegation-release.py`, this never installs anything or requests root:
installing the bundle is a distinct gesture on a scoped signed decision. No
credential value, policy or destination list is accepted here: those stay in
the root-private enrolled documents.
"""
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location('secrets_delegation_release',
        Path(__file__).with_name('delegation-release.py'))
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

DelegationError = release.DelegationError
ACCOUNT = 'ameesh-secrets'
WRAPPER = '/usr/local/libexec/ameesh-secrets-ssh'
DISPATCHER = '/opt/ameesh-release/lib/hote-secrets.py'
KEYS = '/etc/ameesh-secrets/authorized_keys'
SSHD = '/etc/ssh/sshd_config.d/61-ameesh-secrets.conf'
DIRECTORIES = {
    '/opt/ameesh-release':0o755,
    # Searchable for sshd's key lookup as the account; policy.json and
    # helper.json inside stay root-private (0600).
    '/etc/ameesh-secrets':0o755,
    '/usr/local/libexec':0o755,
    '/etc/sudoers.d':0o750,
    '/etc/ssh/sshd_config.d':0o755,
    '/var/lib/ameesh-secrets':0o700,
}


def validate(plan):
    if not isinstance(plan, dict) or set(plan) != release.ACCESS:
        raise DelegationError('invalid delegation plan')
    release.validate_access(plan)


def render(plan, *, account_ids=None, previous_files=None):
    """Return exact install/revoke inventory; this function has no host effects."""
    plan = json.loads(json.dumps(plan))
    validate(plan)
    release.validate_account_ids(account_ids)
    files = {path:release.root_file(mode, content) for path, (mode, content)
             in release.access_files(plan, account=ACCOUNT, wrapper=WRAPPER,
                                     dispatcher=DISPATCHER, keys_file=KEYS,
                                     sshd_file=SSHD).items()}
    previous_files = release.validate_previous(previous_files, files)
    review = dict(version=1, channel='secrets', host=plan['host'], machine_id=plan['machine_id'],
            plan=plan, files=files, previous_files=previous_files,
            directories=dict(DIRECTORIES), services=dict(ssh_reload='sshd.service'),
            account=dict(name=ACCOUNT, system=True, shell='/bin/sh',
                         supplementary_groups=[], password_login=False),
            prerequisites=[
                'Owner/admin verifies exact machine-id and pinned SSH host key.',
                'Dedicated account is distinct from ameesh-release, with no alternate credentials or supplementary groups.',
                'Root-owned /etc/ameesh-secrets is searchable by the SSH account; policy.json and helper.json remain root-private 0600.',
                'helper.json lists only destinations approved by a scoped owner decision (study L232c); a new reference or destination needs its own decision.',
                'Isolated runtime and helper sources (hote-secrets.py, hote-release.py, coffre-release.py) match enrolled tree digests, root-owned with no writable/linked ancestors.',
                'No existing destination file is replaced without its explicit diff and undo snapshot.',
                'Administrator validates sudoers and effective sshd Match settings before enabling the controller key.',
                'A canary delivery on the real host shows no value in stdout, stderr, journals, mail or Git before the first real reference.'
            ],
            revoke=dict(remove_only_matching_sha256={path:item['sha256'] for path, item in files.items()},
                        preserve=['runtime', 'helper sources', 'policy', 'helper enrollment',
                                  'rotation journal', 'installed destinations'],
                        action='Administrator revokes the controller key first, then restores only signed previous-file snapshots, or removes newly created files, while current contents match this exact delegation. Installed credentials are revoked per destination by the helper, never by removing this delegation.'))
    if account_ids is not None:
        review['account'].update(account_ids)
    return dict(review, digest=release.restart.protocol.identity(review))


def question(bundle):
    """Prepare signed-decide text; do not send it or claim it was approved."""
    if not isinstance(bundle, dict) or 'digest' not in bundle:
        raise DelegationError('review bundle required')
    numeric = {key:bundle['account'][key] for key in ('uid', 'gid') if key in bundle['account']}
    expected = render(bundle['plan'], account_ids=numeric or None,
                      previous_files=bundle.get('previous_files'))
    if release.restart.protocol.identity(bundle) != release.restart.protocol.identity(expected):
        raise DelegationError('review bundle changed')
    return ('Approve prepared root SSH/sudo delegation of the secret channel on '+bundle['host']+
            ', machine-id '+bundle['machine_id']+', bundle '+bundle['digest']+
            '? Review exact files/account/prerequisites and digest-bound revoke plan:\n'+
            json.dumps(bundle, sort_keys=True, separators=(',', ':'))+
            '\nThis delegates only the enrolled secret installation dispatcher (hote-secrets.py); '
            'it does not widen the restart delegation, and each delivery stays bound to the '
            'destinations of the root-private helper.json. '
            'No installation is authorized by preparing this question.')

#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""H4: pinned L232c composition; reads enrollment only when invoked.

No paths or transport endpoints come from the operation. H7 must enroll the
protected policy and a transport.json {"hosts": {host: SSH enrollment}} under
/etc/ameesh-secrets. The dedicated ameesh-secrets channel remains separate
from restart. No enrollment is installed by this module.
"""
import importlib.util
import json
from pathlib import Path


def _module(name):
    spec = importlib.util.spec_from_file_location(
        'handoff_' + name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


coffre = _module('coffre-release')
transport = _module('transport-secrets')


class DeliveryBuilder:
    def __init__(self, *, controller_host, root=Path('/etc/ameesh-secrets'),
                 journal=Path('/var/lib/ameesh-secrets')):
        self.root, self.journal = Path(root), Path(journal)
        self.controller_host = controller_host

    def __call__(self, required, *, boundary):
        # L232c's root ownership, private modes, single-link checks and trusted
        # ancestors apply to both documents, not only the value store.
        coffre._trusted_store(self.root, uid=0)

        def document(name):
            data = coffre._read_private(self.root / name, owners=(0,),
                                       modes=(0o600, 0o640), limit=65536)
            return json.loads(data)

        policy = coffre.Politique(document('policy.json'))
        if policy.host != self.controller_host:
            raise ValueError('secret_policy_identity')
        endpoints = document('transport.json')
        if (not isinstance(endpoints, dict) or set(endpoints) != {'hosts'}
                or not isinstance(endpoints['hosts'], dict)
                or required.host not in endpoints['hosts']):
            raise ValueError('secret_transport_enrollment')
        endpoint = endpoints['hosts'][required.host]
        # validate_enrollment checks the exact SSH host identity as well as
        # the dedicated user; the host map cannot redirect an enrolled host.
        if not isinstance(endpoint, dict) or endpoint.get('host') != required.host:
            raise ValueError('secret_transport_identity')
        channel = transport.SSHSecretTransport(endpoint)
        return coffre.Livraison(policy, channel, coffre.Journal(self.journal),
                                boundary=boundary)

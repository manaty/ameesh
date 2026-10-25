# SPDX-License-Identifier: AGPL-3.0-only
"""H4: reference delivery and fresh account health for an enrolled handoff.

No login, account substitution or transport selection occurs here. The pinned
factory supplies identities, the controller supplies a held effect guard and
an enrolled L232c Livraison builder. The guard must fence delayed requests,
validate both epochs and hold the exact account's turn boundary until exit.
An absent capability refuses; constructing a bridge has no host effect.
"""
from collections.abc import Mapping
from copy import deepcopy

from . import handoff_contract, host_preflight, storage


class CredentialRefused(ValueError):
    """Stable category only; upstream exceptions may contain private material."""


class CredentialUncertain(CredentialRefused):
    """A delivery may have happened; reconcile using L232c's durable intent.

    Also covers a later reference failing after an earlier one was installed.
    This exception is never evidence that an account or host was restored.
    """


class CredentialBridge:
    def __init__(self, factory, *, delivery_for=None, guard=None):
        self.factory = factory
        self.delivery_for = delivery_for
        self.guard = guard

    def check(self, agent, host, *, now=None):
        """Read authenticated inventory and credential_health anew each time."""
        try:
            required = self.factory.requirements_for(agent, host)
            inventory = self.factory.adapter.inventory(host)
            rows = storage.of(self.factory.db).credentials.listing(host)
            return host_preflight.check(required, inventory, rows, now=now)
        except Exception:
            raise CredentialRefused('credential_health_unavailable') from None

    def deliver(self, operation, agent, host, *, operation_kind='install'):
        """Deliver policy references while the controller/account guard is held.

        ``delivery_for(required, boundary=...)`` must construct the existing
        L232c Livraison with its protected policy, enrolled secret transport,
        durable intents and journal. It must not choose policy from operation
        data. ``guard(operation, required, rotation=...)`` is a context manager
        yielding True only after acquiring durable epoch/account exclusion.
        H2/H3 supply this controller capability; no guard means no effect.
        Health is always reread after delivery, including replay. An installed
        file alone cannot manufacture a healthy login or credential report.
        """
        try:
            op = deepcopy(operation)
            handoff_contract.validate_request(op, 'prepare')
            required = self.factory.requirements_for(agent, host)
            if (operation_kind not in ('install', 'rotate')
                    or agent.get('name') != op['agent'] or host != op['target']
                    or required.harness != op['plan'].get('harness')
                    or required.account != op['plan'].get('account')):
                raise CredentialRefused('credential_identity')
            references = tuple(dict.fromkeys(self.factory.references_for(agent, host)))
        except CredentialRefused:
            raise
        except Exception:
            raise CredentialRefused('credential_identity') from None
        if self.guard is None:
            raise CredentialRefused('credential_guard_unavailable')
        if references and self.delivery_for is None:
            raise CredentialRefused('secret_delivery_unavailable')
        attempted = False
        try:
            # The guard encloses all references AND the final fresh health read.
            # A boolean check before a remote effect would leave a rotation race.
            with self.guard(op, required, rotation=operation_kind == 'rotate') as held:
                if held is not True:
                    raise CredentialRefused('credential_guard')
                active = True

                def boundary(target, account):
                    return active and target == required.host and account == required.account

                try:
                    delivery = (self.delivery_for(required, boundary=boundary)
                                if references else None)
                    # Resolve every destination before the first read/send, so an
                    # unknown later reference cannot cause a partial admission.
                    destinations = [delivery.politique.destination(ref, host, required.account)
                                    for ref in references]
                    for ref, destination in zip(references, destinations):
                        attempted = True
                        receipt = delivery.deliver(ref, host, required.account,
                                                   operation=operation_kind)
                        if (not isinstance(receipt, Mapping) or receipt.get('ok') is not True
                                or receipt.get('reference') != ref
                                or receipt.get('operation') != operation_kind
                                or receipt.get('destination') != destination.document()):
                            raise CredentialRefused('secret_delivery_refused')
                    health = self.check(agent, host)
                    return {'credentials_ready': health['ready'],
                            'host': host, 'missing': health['missing']}
                finally:
                    active = False
        except Exception:
            # After entering Livraison, even a local exception may follow a
            # completed reference. Conservatively keep the handoff held.
            if attempted:
                raise CredentialUncertain('secret_delivery_reconcile') from None
            raise CredentialRefused('secret_delivery_unavailable') from None

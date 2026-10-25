#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""H1: pinned handoff companion loaded by ``fabrique-handoff.py``.

H1 binds the contract, the enrolled identities and the factory; it performs
no host effect. Every capability below therefore refuses by name:

- H2 replaces the preparation/Git methods (``inventory``, ``revalidate``,
  ``prepare``, ``stop_source``, ``publish``, ``transfer``, ``session``);
- H3 replaces the activation/health methods (``admit``, ``enable``,
  ``verify``, ``restore_source``) and the recovery methods
  (``recover_source``, ``reconcile_effect``).

A missing method is refused by the factory at admission. A present method
that is not implemented yet refuses at call time and never returns a
simulated success or an invented proof.
"""


class Refused(ValueError):
    """A named capability that is absent; the controller holds the operation."""


class HostAdapter:
    def __init__(self, *, db=None, enrollment=None, hosts=None, references=None):
        # H2/H3 companions keep the injected controllers/enrollment; H1 needs
        # none of it and must not read credentials, hosts or secrets.
        self.db = db
        self.enrollment = enrollment
        self.hosts = hosts
        self.references = references

    def _unbound(self, capability):
        raise Refused('capability:' + capability)

    def inventory(self, host):
        self._unbound('inventory')

    def revalidate(self, operation):
        self._unbound('revalidate')

    def prepare(self, operation):
        self._unbound('prepare')

    def stop_source(self, operation):
        self._unbound('stop_source')

    def publish(self, operation):
        self._unbound('publish')

    def transfer(self, operation):
        self._unbound('transfer')

    def session(self, operation):
        self._unbound('session')

    def admit(self, operation):
        self._unbound('admit')

    def enable(self, operation):
        self._unbound('enable')

    def verify(self, operation):
        self._unbound('verify')

    def restore_source(self, operation):
        self._unbound('restore_source')

    def recover_source(self, operation):
        self._unbound('recover_source')

    def reconcile_effect(self, operation):
        self._unbound('reconcile_effect')

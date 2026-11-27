# SPDX-License-Identifier: AGPL-3.0-only
"""Offline example: copied outside the repository, never shipped by ameesh.

It imports only the public connector surface (contract 1.8, #8342): a real
external distribution never imports ``ameesh.connectors`` or ``tests``.
"""
from pathlib import Path
from ameesh.public.connectors import Outcome

NAME = 'bench-actions'
TYPES = ()


def descriptor(options=None):
    if options:
        raise ValueError('bench-actions has no module-specific options')
    return dict(types=[], schemas={}, rules=[])


class JournalConnector:
    name = 'bench-journal'
    dedupe = 'guaranteed'

    def classify(self, operation, args):
        raise AssertionError('the canon must classify module operations')

    def execute(self, action, idempotency_key):
        path = Path(action.args['directory']) / idempotency_key
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(action.operation, encoding='utf-8')
        # Simulate a lost response, so the gate must reconcile by reading.
        return Outcome.unknown('read the journal to confirm')

    def reconcile(self, action):
        path = Path(action.args['directory']) / action.action_id
        return Outcome.confirmed(str(path)) if path.exists() else None


ACTION_CONNECTORS = {'bench-journal': JournalConnector}

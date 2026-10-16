# SPDX-License-Identifier: AGPL-3.0-only
"""Independent-controller storage for host release fences and exact receipts.

Not exposed through the mediated executor API. Its enrolled controller caller
must validate signed authority before writes; these methods do not grant it.
"""
import json
import re
from contextlib import contextmanager

from ... import db as db_mod
from .hosts import Hosts

#: Enrolled probe identity, same shape as the runner's agent names.
PROBE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')


class FenceError(db_mod.DbError):
    pass


class ReleaseFences:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _lock(tx, host):
        Hosts._lock_gate(tx, host)
        # Serialize existing claim/begin_turn writers before publishing closure.
        # The VOLATILE gate predicate is read again after their row-lock wait.
        tx.query('SELECT name FROM agent_registry WHERE host=%s ORDER BY name FOR UPDATE', (host,))

    @staticmethod
    def _quiet(tx, host):
        # An expired lease is not proof that a harness/process group stopped.
        # Unfinished and orphan resources require reconciliation, not expiry.
        return tx.query(
            "SELECT NOT EXISTS (SELECT 1 FROM agent_registry r WHERE host=%s "
            "AND (status='running' OR current_prompt IS NOT NULL OR EXISTS "
            "(SELECT 1 FROM spend_pending s WHERE s.agent=r.name))) "
            "AND NOT EXISTS (SELECT 1 FROM turn_resources WHERE host=%s "
            "AND (ended_at IS NULL OR status IN ('running','orphan'))) AS quiet", (host, host))[0]['quiet']

    @staticmethod
    def _scope(tx, request):
        host = request['manifest']['host']
        tx.execute('INSERT INTO host_release_operations(operation,host,manifest_digest) VALUES (%s,%s,%s) '
                   'ON CONFLICT(operation) DO NOTHING', (request['operation'], host, request['manifest_digest']))
        row = tx.query('SELECT host,manifest_digest FROM host_release_operations WHERE operation=%s FOR UPDATE',
                       (request['operation'],))[0]
        if row != {'host': host, 'manifest_digest': request['manifest_digest']}:
            raise FenceError('release fence operation scope changed')

    @staticmethod
    def _receipt(tx, request):
        rows = tx.query('SELECT receipt FROM host_release_fence_receipts WHERE action_id=%s',
                        (request['action_id'],))
        if not rows:
            return None
        expected = dict(request, ok=True)
        receipt = rows[0]['receipt']
        if receipt != expected:
            raise FenceError('release fence receipt mismatch')
        return receipt

    @staticmethod
    def _save(tx, request):
        receipt = dict(request, ok=True)
        tx.execute('INSERT INTO host_release_fence_receipts(action_id,operation,step,receipt) VALUES (%s,%s,%s,%s::jsonb)',
                   (request['action_id'], request['operation'], request['step'], json.dumps(receipt)))
        return receipt

    def close(self, request, *, authorize, probe):
        """Commit closure immediately; receipt only when registered turns drain.

        `probe` is the host's enrolled probe agent: the fence records it so the
        controller's own one-turn health check stays admissible while every
        other claim of the host is closed. None means still draining. No turn,
        lease, prompt, placement or session is interrupted or mutated. Retry
        with the same request after a deadline.
        """
        if not isinstance(probe, str) or PROBE.fullmatch(probe) is None:
            raise FenceError('enrolled probe agent required')
        host = request['manifest']['host']
        with self.db.transaction() as tx:
            self._lock(tx, host)
            if authorize() is not True:
                raise FenceError('release fence authority denied')
            self._scope(tx, request)
            if authorize() is not True:
                raise FenceError('release fence authority denied')
            old = self._receipt(tx, request)
            if old is not None:
                return old
            rows = tx.query('SELECT operation,probe FROM host_release_fences WHERE host=%s', (host,))
            if rows and rows[0] != {'operation': request['operation'], 'probe': probe}:
                raise FenceError('host has another release fence')
            tx.execute('INSERT INTO host_release_fences(host,operation,probe) VALUES (%s,%s,%s)'
                       ' ON CONFLICT(host) DO NOTHING',
                       (host, request['operation'], probe))
            if not self._quiet(tx, host):
                return None
            return self._save(tx, request)

    def drained(self, request, *, authorize):
        """Read the live owned fence and quiescence under the same gate lock."""
        host = request['manifest']['host']
        with self.db.transaction() as tx:
            self._lock(tx, host)
            if authorize() is not True:
                raise FenceError('release fence authority denied')
            rows = tx.query('SELECT f.operation,o.manifest_digest FROM host_release_fences f '
                            'JOIN host_release_operations o USING(operation) WHERE f.host=%s', (host,))
            return bool(rows and rows[0] == {'operation': request['operation'],
                                             'manifest_digest': request['manifest_digest']}
                        and self._quiet(tx, host))

    @contextmanager
    def guard(self, request, *, authorize):
        """Keep owned closure serialized through the synchronous host submit."""
        host = request['manifest']['host']
        with self.db.transaction() as tx:
            self._lock(tx,host)
            if authorize() is not True:
                raise FenceError('release fence authority denied')
            rows = tx.query('SELECT f.operation,o.manifest_digest FROM host_release_fences f '
                            'JOIN host_release_operations o USING(operation) WHERE f.host=%s',(host,))
            if (not rows or rows[0] != {'operation':request['operation'],'manifest_digest':request['manifest_digest']}
                    or not self._quiet(tx,host)):
                raise FenceError('owned drained release fence required')
            yield

    def open(self, request, *, authorize):
        """Atomically remove only this fence and persist completion for replay."""
        host = request['manifest']['host']
        with self.db.transaction() as tx:
            self._lock(tx, host)
            if authorize() is not True:
                raise FenceError('release fence authority denied')
            self._scope(tx, request)
            if authorize() is not True:
                raise FenceError('release fence authority denied')
            old = self._receipt(tx, request)
            if old is not None:
                return old
            rows = tx.query('SELECT operation FROM host_release_fences WHERE host=%s', (host,))
            if not rows or rows[0]['operation'] != request['operation']:
                raise FenceError('owned release fence missing')
            if not self._quiet(tx, host):
                raise FenceError('release fence still has active resources')
            tx.execute('DELETE FROM host_release_fences WHERE host=%s AND operation=%s', (host, request['operation']))
            return self._save(tx, request)

# SPDX-License-Identifier: AGPL-3.0-only
"""Dormant sandbox reconciler (#2202 S4b).

The runner supplies a fresh durable S1 row at each step and consumes events.
This module never holds a lease or writes a database. In particular, a bind
must appear in the next snapshot before result reading, and a cancellation
fence must appear before deletion. A restart discards only local observations.
No real provider, cluster credentials, runner hook or activation ships here.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .sandbox_provider import (
    Job, JobAlreadyExists, JobNotFound, JobProvider, JobSpec, JobUIDConflict,
    LABEL_TURN_ID, PHASE_FAILED, PHASE_RUNNING, PHASE_SUCCEEDED, ProviderError,
)
from .storage.postgres.sandbox import (
    MAX_RESULT_BYTES, TERMINAL, Refusal, _canonical, _validate,
)


@dataclass(frozen=True)
class Observation:
    """An event for the runner; state is a proposal, never a database write.

    bound: call ameesh_sandbox_bind, then refresh the row.
    cancel-requested: fence via ameesh_sandbox_cancel, then refresh the row.
    cancelled/orphaned: call cancel(observed=True)/cancel(orphaned=True).
    lost/expired: trusted recovery cancellation, never close a new lease.
    result: submit through ameesh_sandbox_complete; replay is S1's authority.
    failed: the Job is terminated without an accepted result; retain cost
    uncertainty and let the runner's settlement boundary close the turn.
    """

    kind: str
    turn_id: str
    state: str
    job_uid: str | None = None
    job_phase: str | None = None
    refusal: str | None = None
    result: dict | None = None

    def as_dict(self):
        return {key: value for key, value in vars(self).items() if value is not None}


def _deadline(value):
    # Both storage drivers and the private JSON handoff return ISO timestamps.
    if isinstance(value, str):
        value = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    if isinstance(value, datetime) and value.tzinfo is None:
        raise ValueError('sandbox timestamps must carry a timezone')
    return value.timestamp() if isinstance(value, datetime) else float(value)


def write_result(path: str | Path, result: Mapping) -> None:
    """Atomically publish a validated, canonical result with private mode."""
    raw, _ = _canonical(dict(result))
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.sandbox-result-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SandboxSupervisor:
    """One reconciliation step, with no sleep and no hidden retry loop.

    The caller serializes steps for a turn. Provider operations must have
    transport timeouts. An unknown create outcome is retried by GET on the
    next step; it never causes a second name or a replacement of a bound UID.
    ``emit`` must deliver to the runner (or raise); failures are not swallowed.
    ``orphan_after`` is the admitted cancellation observation budget, in
    seconds, measured from the durable cancel timestamp across restarts.
    """

    def __init__(self, provider: JobProvider, emit: Callable[[dict], None], *,
                 orphan_after: float, clock: Callable[[], float] = time.time,
                 result_path: str | Path | None = None):
        if not callable(emit) or orphan_after <= 0:
            raise ValueError('emit and a positive observation budget are required')
        self.provider = provider
        self.emit = emit
        self.clock = clock
        self.orphan_after = orphan_after
        self.result_path = result_path

    def _event(self, row, kind, state, job=None, **extra):
        observation = Observation(kind, row['turn_id'], state,
                                  job.uid if job else row.get('job_uid'),
                                  job.phase if job else None, **extra)
        self.emit(observation.as_dict())
        return observation

    def _verify(self, row, spec, job):
        identity = spec.identity
        if (job.namespace != identity.namespace or job.name != identity.name
                or job.image != spec.image
                or any(job.labels.get(k) != v for k, v in identity.labels.items())
                or (row.get('job_uid') and row['job_uid'] != job.uid)):
            raise Refusal('job-conflict')
        return job

    def _observe(self, row, spec):
        identity = spec.identity
        # List by turn, not full labels: a mismatched epoch/profile is a
        # conflict, not an absent Job that permits a new create.
        jobs = self.provider.list(identity.namespace,
                                  {LABEL_TURN_ID: identity.turn_id})
        named = self.provider.get(identity.namespace, identity.name)
        by_uid = {job.uid: job for job in jobs}
        if named is not None:
            by_uid[named.uid] = named
        if len(by_uid) > 1:
            raise Refusal('job-conflict')
        return self._verify(row, spec, next(iter(by_uid.values()))) if by_uid else None

    def _cancel(self, row, spec, recovery):
        job = self._observe(row, spec)
        if job is not None and not job.terminal:
            try:
                self.provider.delete(job.namespace, job.name, uid=job.uid)
            except JobNotFound:
                pass
            job = self._observe(row, spec)
        if job is None or job.terminal:
            # A cancellation already fenced in S1 revokes even a successful
            # result. Never read logs on this path.
            state = ('lost' if job is None else 'expired') if recovery else 'cancelled'
            return self._event(row, state, state, job)
        since = row.get('cancel_requested_at')
        timed_out = (since is not None
                     and self.clock() - _deadline(since) >= self.orphan_after)
        state = 'orphaned' if timed_out or row['state'] == 'orphaned' else 'cancelling'
        return self._event(row, state, state, job,
                           refusal='sandbox_orphan' if state == 'orphaned' else None)

    def step(self, row: Mapping, spec: JobSpec, *, recovery=False) -> Observation:
        """Reconcile a trusted runner snapshot; cancellation always wins.

        Snapshot identity is checked before any provider call. A terminal
        snapshot is never launched again. Lease loss is conveyed by the
        runner fencing the old row; the orphan sweep calls this same cancel
        path and interprets its observation through S1's recovery=True gate.
        """
        identity = spec.identity
        if (row['turn_id'] != identity.turn_id or row['agent'] != identity.agent
                or row['lease_epoch'] != identity.epoch
                or row['namespace'] != identity.namespace
                or row['job_name'] != identity.name
                or row['profile_ref'] != identity.profile
                or row['placement_ref'] != identity.placement
                or row['image_digest'] != spec.image):
            raise Refusal('job-conflict')
        if row['state'] in TERMINAL:
            return self._event(row, 'terminal', row['state'])
        cancelling = row.get('cancel_requested_at') is not None or row['state'] in (
            'cancelling', 'orphaned')
        if recovery and not cancelling:
            return self._event(row, 'cancel-requested', 'cancelling', refusal='stale')
        job = None
        try:
            if cancelling:
                return self._cancel(row, spec, recovery)
            if self.clock() >= _deadline(row['deadline_ts']):
                # Fence FIRST. Only a refreshed row can authorize delete.
                return self._event(row, 'cancel-requested', 'cancelling', refusal='late')
            job = self._observe(row, spec)
            if self.clock() >= _deadline(row['deadline_ts']):
                return self._event(row, 'cancel-requested', 'cancelling', job,
                                   refusal='late')
            if job is None:
                if row.get('job_uid') or row['state'] != 'intent':
                    # Bound Job disappeared (TTL/operator): never replace it.
                    return self._event(row, 'cancel-requested', 'cancelling',
                                       refusal='job-lost')
                try:
                    job = self.provider.create(spec)
                except JobAlreadyExists:
                    job = self._observe(row, spec)
                    if job is None:
                        raise Refusal('job-conflict')
                self._verify(row, spec, job)
            if self.clock() >= _deadline(row['deadline_ts']):
                return self._event(row, 'cancel-requested', 'cancelling', job,
                                   refusal='late')
            if job.deletion_timestamp is not None:
                return self._event(row, 'cancel-requested', 'cancelling', job,
                                   refusal='job-deleting')
            if not row.get('job_uid'):
                return self._event(row, 'bound', 'bound', job)
            if job.phase == PHASE_SUCCEEDED:
                raw = self.provider.logs(job.namespace, job.name)
                if len(raw.encode('utf-8')) > MAX_RESULT_BYTES:
                    raise Refusal('missing')
                try:
                    result = json.loads(raw)
                    _, digest = _canonical(result)
                    _validate(row, result)
                    message = json.loads(job.message)
                    if message != {'result_sha256': digest}:
                        raise Refusal('result-mismatch')
                except (ValueError, TypeError, RecursionError) as exc:
                    if isinstance(exc, Refusal):
                        raise
                    raise Refusal('missing') from exc
                # Re-check time after slow log reading. The runner checks
                # revocation, live lease, epoch and deadline again atomically.
                if self.clock() >= _deadline(row['deadline_ts']):
                    return self._event(row, 'cancel-requested', 'cancelling', job,
                                       refusal='late')
                if self.result_path is not None:
                    write_result(self.result_path, result)
                return self._event(row, 'result', row['state'], job, result=result)
            if job.phase == PHASE_FAILED:
                return self._event(row, 'failed', 'failed', job)
            return self._event(row, 'heartbeat',
                               'running' if job.phase == PHASE_RUNNING else 'bound', job)
        except (Refusal, JobUIDConflict) as exc:
            # A foreign Job is neither adopted nor deleted.
            if (isinstance(exc, Refusal) and exc.code != 'job-conflict'
                    and job is not None and job.terminal):
                return self._event(row, 'failed', 'failed', job, refusal=exc.code)
            return self._event(row, 'orphaned', 'orphaned',
                               refusal=getattr(exc, 'code', 'job-conflict'))
        except ProviderError as exc:
            return self._event(row, 'orphaned' if cancelling else 'retry',
                               'orphaned' if cancelling else row['state'],
                               refusal=exc.code)

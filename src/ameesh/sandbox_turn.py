# SPDX-License-Identifier: AGPL-3.0-only
"""Dormant runner-side sandbox adapter (#2202 S4d-1).

Trusted runner API only, deliberately unconnected to runner.py. Admission
receives a validated S2 snapshot, never placement settings from a mail payload.
The child owns provider I/O; only this parent owns S1/S3 transactions. Every
step is acknowledged with a fresh database snapshot before another step.
"""
from __future__ import annotations

import importlib
import json
import math
import os
import selectors
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from . import turn_context
from .sandbox_provider import JobIdentity, JobSpec
from .sandbox_supervisor import SandboxSupervisor
from .storage.postgres import sandbox as s1, turn_admission as s3

MAX_EVENT_BYTES = s1.MAX_RESULT_BYTES + 8192


def authorize(context, request_id):
    """Use the reserved request, not a caller-supplied copy or delivery state."""
    matches = [r for r in context['requests'] if r['id'] == request_id]
    if type(request_id) is not int or len(matches) != 1:
        raise s1.Refusal('unauthorized-request')
    payload = matches[0]['payload']
    if payload.get('schema') != 'ameesh-sandbox-request/1':
        raise s1.Refusal('not-a-sandbox-request')
    return payload


class SandboxTurn:
    """One context identity, one durable admission, one child at a time.

    Construct from ``current`` in the runner. Recovery is a separate trusted
    constructor: it reads the old row without borrowing the replacement turn's
    context, and can only cancel/observe it. No recovery result is accepted.
    """
    def __init__(self, db, context):
        turn_context.validate(context)
        self.db = db
        # Capture once: later context-file rotations must not change authority.
        self.context = json.loads(json.dumps(context))
        self.turn_id = context['turn']['id']
        self.owner = context['lease']['owner']
        self.epoch = context['lease']['epoch']
        self.recovery = False
        self.child = None

    @classmethod
    def current(cls, db, *, environ=None):
        context = turn_context.current(environ=environ)
        turn_context.verify(context, db)
        return cls(db, context)

    @classmethod
    def recover(cls, db, turn_id):
        obj = cls.__new__(cls)
        obj.db, obj.turn_id = db, turn_id
        obj.context, obj.child, obj.recovery = None, None, True
        row = obj.snapshot()
        obj.owner, obj.epoch = row['owner'], row['lease_epoch']
        return obj

    def snapshot(self):
        rows = self.db.query('select * from sandbox_turns where turn_id = %s',
                             (self.turn_id,))
        if not rows:
            raise s1.Refusal('unknown-turn')
        row = rows[0]
        if hasattr(self, 'owner') and (row['owner'], row['lease_epoch']) != (self.owner, self.epoch):
            raise s1.Refusal('stale')
        return row

    def admit(self, request_id, *, snapshot, billing_account, tranche_id):
        """Commit both reservations and intent before a child may start.

        ``snapshot`` is the runner's trusted S2/source/scope envelope. Its
        request/turn/lease/spend identity is always replaced by the context.
        S3 serializes replay and rejects a second admission for this turn.
        """
        if self.recovery:
            raise s1.Refusal('stale')
        authorize(self.context, request_id)
        accounting = self.context['accounting']
        marker = accounting['marker']
        if not marker or marker['turn_id'] != self.turn_id or not accounting['spend_key']:
            raise s1.Refusal('missing')
        envelope = dict(snapshot, owner=self.owner, lease_epoch=self.epoch,
                        request_ids=[request_id])
        return s3.ameesh_admit_turn(
            self.db, self.context['turn']['agent'], billing_account,
            envelope['limits']['usd_per_turn'], accounting['spend_key'],
            tranche_id, 'sandbox', turn_id=self.turn_id, sandbox=envelope)

    def cancel(self):
        if self.recovery:
            # Recovery API requires an explicit orphan report as the fence.
            return s1.ameesh_sandbox_cancel(self.db, self.turn_id, self.owner,
                                            self.epoch, recovery=True, orphaned=True)
        return s1.ameesh_sandbox_cancel(self.db, self.turn_id, self.owner, self.epoch)

    def consume(self, event):
        """Apply a bounded child event through the authoritative S1 gates.

        S1 owns exact-once application; local de-duplication would lose a
        receipt if the adapter crashed after commit but before acknowledgement.
        """
        if not isinstance(event, dict) or event.get('turn_id') != self.turn_id:
            raise s1.Refusal('stale')
        kind = event.get('kind')
        if self.recovery and kind in ('bound', 'result'):
            raise s1.Refusal('stale')
        if kind not in ('bound', 'result', 'cancel-requested', 'failed', 'cancelled',
                        'lost', 'expired', 'orphaned', 'heartbeat', 'retry',
                        'cancelling', 'terminal'):
            raise s1.Refusal('contract-mismatch')
        s1.observe_metadata(self.db, self.turn_id, self.owner, self.epoch,
                            job_phase=event.get('job_phase'), refusal=event.get('refusal'),
                            job_uid=event.get('job_uid'),
                            recovery=self.recovery)
        args = self.db, self.turn_id, self.owner, self.epoch
        if kind == 'bound':
            s1.ameesh_sandbox_bind(*args, event.get('job_uid'))
        elif kind == 'result':
            return s1.ameesh_sandbox_complete(*args, event.get('result'), settle=s3.sandbox_settle)
        elif kind in ('cancel-requested', 'failed'):
            # A failed Job has no accepted result. Fence before observation;
            # its unknown relay cost is kept by S3 on the cancel path.
            self.cancel()
        elif kind in ('cancelled', 'lost', 'expired'):
            if kind != 'cancelled' and not self.recovery:
                raise s1.Refusal('stale')
            s1.ameesh_sandbox_cancel(*args, observed=True, recovery=self.recovery,
                                    terminal_state=kind, settle=s3.sandbox_settle)
        elif kind == 'orphaned':
            s1.ameesh_sandbox_cancel(*args, orphaned=True, recovery=self.recovery)
        elif kind not in ('heartbeat', 'retry', 'cancelling', 'terminal'):
            raise s1.Refusal('contract-mismatch')
        return self.snapshot()

    def start(self, spec, *, provider_factory, state_dir, orphan_after, timeout):
        """Start a supervisor in the caller's process group, after intent.

        The trusted factory is ``package.module:callable`` (no arguments).
        Provider credentials belong to its own explicit credential mechanism;
        database/harness secrets are not inherited. No default real provider.
        """
        if self.child is not None:
            raise RuntimeError('sandbox child already started')
        row = self.snapshot()
        if row['state'] in s1.TERMINAL:
            raise s1.Refusal('late')
        if self.recovery:
            self.cancel()
            row = self.snapshot()
        self.child = SupervisorChild(spec, provider_factory=provider_factory,
                                     state_dir=state_dir, orphan_after=orphan_after,
                                     timeout=timeout)
        return self.child

    def step(self):
        if self.child is None:
            raise RuntimeError('sandbox child not started')
        try:
            event = self.child.step(self.snapshot(), recovery=self.recovery)
            return event, self.consume(event)
        except BaseException:
            # Discard a transport with an unacknowledged command. Reusing it
            # could apply an old reply to a later step after a timeout.
            self.close()
            raise

    def close(self):
        """Stop only the local child. Durable intent survives for adoption."""
        if self.child is not None:
            self.child.close()
            self.child = None


class SupervisorChild:
    """Private file handoff and bounded JSONL; no DB handle crosses exec."""
    def __init__(self, spec, *, provider_factory, state_dir, orphan_after, timeout):
        if not all(math.isfinite(v) and v > 0 for v in (timeout, orphan_after)):
            raise ValueError('positive timeouts required')
        self.timeout = timeout
        self.process = None
        self.directory = tempfile.TemporaryDirectory(prefix='sandbox-turn-', dir=state_dir)
        self.path = Path(self.directory.name) / 'turn.json'
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(dict(spec=asdict(spec), provider_factory=provider_factory,
                               orphan_after=orphan_after), stream)
            # Do not start a new session/group: cancellation of the turn must
            # reach its supervisor too. Only non-secret interpreter settings.
            env = {k: os.environ[k] for k in ('PATH', 'PYTHONPATH', 'LANG', 'LC_ALL')
                   if k in os.environ}
            self.process = subprocess.Popen(
                [sys.executable, '-m', 'ameesh.sandbox_turn', str(self.path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, close_fds=True)
            self.pending = b''
        except BaseException:
            self.close()
            raise

    def step(self, row, *, recovery=False):
        raw = (json.dumps(dict(row=row, recovery=recovery), default=str) + '\n').encode()
        deadline = time.monotonic() + self.timeout
        # Bound writing too: a stuck provider constructor may never read a
        # snapshot larger than the pipe capacity.
        os.set_blocking(self.process.stdin.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdin, selectors.EVENT_WRITE)
            pending = memoryview(raw)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError('sandbox observation timeout')
                try:
                    written = os.write(self.process.stdin.fileno(), pending)
                except BlockingIOError:
                    continue
                pending = pending[written:]
            selector.unregister(self.process.stdin)
            # Unbuffered reads also bound a child emitting a partial line.
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b'\n' not in self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError('sandbox observation timeout')
                chunk = os.read(self.process.stdout.fileno(), 8192)
                if not chunk:
                    raise RuntimeError('sandbox supervisor exited')
                self.pending += chunk
                if len(self.pending) > MAX_EVENT_BYTES:
                    raise s1.Refusal('contract-mismatch')
        line, self.pending = self.pending.split(b'\n', 1)
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            raise s1.Refusal('contract-mismatch') from None
        return event

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            for stream in (self.process.stdin, self.process.stdout):
                stream.close()
        self.directory.cleanup()


def _child(filename):
    """Internal subprocess protocol. No database connection or environment DSN."""
    with open(filename, encoding='utf-8') as stream:
        handoff = json.load(stream)
    spec_data = handoff['spec']
    spec = JobSpec(**dict(spec_data, identity=JobIdentity(**spec_data['identity'])))
    module, name = handoff['provider_factory'].split(':', 1)
    provider = getattr(importlib.import_module(module), name)()
    def emit(event):
        print(json.dumps(event, ensure_ascii=False, separators=(',', ':')), flush=True)
    supervisor = SandboxSupervisor(provider, emit, orphan_after=handoff['orphan_after'])
    for line in sys.stdin:
        command = json.loads(line)
        supervisor.step(command['row'], spec, recovery=command['recovery'])


if __name__ == '__main__':
    _child(sys.argv[1])

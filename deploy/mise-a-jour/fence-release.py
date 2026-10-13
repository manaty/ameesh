#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Signed-authority gate for independent-controller PostgreSQL release fences."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import time

from ameesh.storage.postgres.release_fences import ReleaseFences

_spec = importlib.util.spec_from_file_location('release_fence_protocol', Path(__file__).with_name('controleur-release.py'))
_protocol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_protocol)

class FenceError(ValueError):
    pass


def validate(request):
    fields = {'operation','manifest_digest','action_id','manifest','step'}
    if not isinstance(request, dict) or set(request) != fields:
        raise FenceError('invalid fence request')
    operation = request['operation']
    if not isinstance(operation, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', operation):
        raise FenceError('invalid fence operation')
    if not isinstance(request['step'], str) or request['action_id'] != operation + ':' + request['step']:
        raise FenceError('invalid fence action')
    manifest = request['manifest']
    _protocol.validate(manifest)
    if (not isinstance(manifest, dict) or not isinstance(manifest.get('host'), str)
            or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', manifest['host'])):
        raise FenceError('invalid fence host')
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != request['manifest_digest']:
        raise FenceError('invalid fence manifest digest')


class SignedReleaseFence:
    """No default credentials, expiry/force release or implicit enrollment.

    Only a controller-enrolled connection and signed authority are accepted.
    Host-side process/cgroup quiescence must also be verified before effects;
    PostgreSQL quiescence is necessary but does not prove every process exited.
    """
    def __init__(self, db, authority):
        self.store = ReleaseFences(db)
        self.authority = authority

    def _authorized(self, request):
        validate(request)
        try:
            granted = self.authority.validate(request['operation'], request['manifest_digest'], request['step'])
        except Exception:
            raise FenceError('fence authority unavailable') from None
        if granted is not True:
            raise FenceError('fence authority denied')
        return True

    def perform(self, step, request, *, drain_seconds=0, poll_seconds=0.2):
        request = json.loads(json.dumps(request))
        if step != request.get('step') or step not in ('fence','unfence','recovery_fence','recovery_unfence'):
            raise FenceError('invalid fence step')
        if (type(drain_seconds) not in (int, float) or not 0 <= drain_seconds <= 7200
                or type(poll_seconds) not in (int, float) or not 0.01 <= poll_seconds <= 1):
            raise FenceError('invalid drain deadline')
        deadline = time.monotonic() + drain_seconds
        while True:
            self._authorized(request)
            try:
                method = self.store.close if step in ('fence','recovery_fence') else self.store.open
                receipt = method(request, authorize=lambda: self._authorized(request))
            except Exception:
                raise FenceError('fence outcome unavailable; reconcile request') from None
            if receipt is not None:
                return receipt
            if time.monotonic() >= deadline:
                raise FenceError('fence draining; reconcile request')
            time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))

    def fenced(self, request):
        self._authorized(request)
        try:
            return self.store.drained(request, authorize=lambda: self._authorized(request))
        except Exception:
            raise FenceError('live fence unavailable') from None

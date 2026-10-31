#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Signed continuation of an interrupted bootstrap; old receipts stay immutable."""
import json
import os
import re
import importlib.util
from pathlib import Path


_spec = importlib.util.spec_from_file_location(
    'continuation_native', Path(__file__).with_name('bootstrap-release.py'))
bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bootstrap)

PREFIX = 'Approve this exact bootstrap continuation and replacement delegation:\n'
INHERITED = ('runtime', 'sources', 'account')


def reference(backend, operation, previous, decision):
    b = bootstrap
    if (operation == previous or not isinstance(previous, str)
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', previous) is None
            or type(decision) is not int or decision <= 0):
        raise b.BootstrapError('distinct previous operation and signed decision required')
    old = backend.prepared(previous).bundle
    new = backend.prepared(operation).bundle
    b.delegation.question(old); b.delegation.question(new)
    # This operation only adds the second transport. A runtime/account/policy
    # change needs another explicit provisioning design, never receipt reuse.
    old_plan, new_plan = dict(old['plan']), dict(new['plan'])
    local = new_plan.pop('local_transport', None)
    old_plan.pop('local_transport', None)
    if (local is None or old_plan != new_plan or old['account'] != new['account']
            or old['host'] != new['host'] or old['machine_id'] != new['machine_id']
            or {k:v for k,v in old['files'].items() if k != '/etc/ameesh-release/authorized_keys'}
               != {k:v for k,v in new['files'].items() if k != '/etc/ameesh-release/authorized_keys'}):
        raise b.BootstrapError('continuation may only replace the transport delegation')
    record = backend.record(previous)
    if (set(record) != {'bundle', 'intent', 'receipts', 'complete'}
            or record['bundle'] != old or record['complete'] is not False
            or record['intent'] not in (None, 'validate')
            or set(record['receipts']) not in (set(b.STEPS[:6]), set(b.STEPS[:7]))):
        raise b.BootstrapError('previous bootstrap must be held before enable')
    scopes = b.scopes(previous, old)
    for step, receipt in record['receipts'].items():
        expected = dict(operation=previous, step=step, action_id=previous+':'+step,
                        scope_digest=scopes[step], bundle=old, ok=True)
        if receipt != expected:
            raise b.BootstrapError('previous bootstrap receipt mismatch')
    # The append-only native journal connects these receipts to the old decision.
    entries = backend.gestes.entries()
    if not any(e.get('command') == 'install' and e.get('phase') == 'start'
               and e.get('operation') == previous and e.get('bundle_digest') == old['digest']
               and e.get('decision') == decision for e in entries):
        raise b.BootstrapError('previous signed install provenance unavailable')
    return dict(version=1, operation=previous, decision=decision, digest=old['digest'],
                journal_sha256=b.protocol.identity(record), code_sha256=backend.gestes.code)


def question(bootstrap, operation, bundle, ref, expires_at):
    value = dict(version=1, operation=operation, bundle_digest=bundle['digest'],
                 host=bundle['host'], machine_id=bundle['machine_id'], previous=ref,
                 expires_at=expires_at, steps=list(bootstrap.STEPS))
    text = PREFIX + json.dumps(value, sort_keys=True, separators=(',', ':'))
    if type(expires_at) is not int or expires_at <= 0 or len(text) > bootstrap.signed.decisions.MAX_QUESTION:
        raise bootstrap.BootstrapError('bounded continuation signing window required')
    return text


def authority(bootstrap, backend, decision, operation, bundle, ref):
    class ContinuationAuthority(bootstrap.SignedBootstrapAuthority):
        prefix = PREFIX

        def render(self, operation, expires_at):
            return question(bootstrap, operation, self.bundle, ref, expires_at)

        def validate(self, operation, digest, step):
            if reference(backend, operation, ref['operation'], ref['decision']) != ref:
                raise bootstrap.BootstrapError('previous bootstrap changed')
            old = bootstrap.signed.decisions.get(self.db, ref['decision'])
            new = bootstrap.signed.decisions.get(self.db, self.decision_id)
            if (not old or not new or old.get('superseded_by') != self.decision_id
                    or new.get('supersedes') != ref['decision']
                    or new.get('supersedes_option') != 'install'
                    or not bootstrap.signed.decisions.supersession_approved(new)):
                raise bootstrap.BootstrapError('reciprocal signed supersession required')
            return super().validate(operation, digest, step)
    return ContinuationAuthority(backend.db, decision, operation, bundle)


def run(bootstrap, backend, operation, decision, *, wrap=lambda adapter: adapter):
    prepared = backend.prepared(operation)
    path = backend.stage(operation) / 'continuation.json'
    ref = json.loads(backend.private(path, 65536))
    auth = authority(bootstrap, backend, decision, operation, prepared.bundle, ref)
    native = backend.adapter(auth, prepared.bundle)

    def check_inherited(step):
        effects = backend.effects(step, ref['operation'], prepared.bundle)
        if step == 'account':
            valid = effects.get('account') == effects.get('group') == 'present'
        else:
            valid = list(effects.values()) == ['present']
            logical = '/opt/ameesh-release/' + ('runtime' if step == 'runtime' else 'lib')
            target = backend.path(logical)
            inventory = bootstrap.module('runtime-release').inventory(target)
            valid = valid and not any((target/name).stat().st_mode & 0o222
                                      for name in inventory['dirs']+list(inventory['files']))
        if not valid:
            raise bootstrap.BootstrapError('inherited bootstrap effect changed')

    class Adapter:
        def perform(self, step, request):
            # Recheck inherited effects even when a retry has already recorded
            # their new receipts. No account/tree effect is executed again.
            if step in (*INHERITED, 'validate', 'enable'):
                native.approved(request)
                for inherited in INHERITED:
                    check_inherited(inherited)
            if step not in INHERITED:
                return native.perform(step, request)
            native.approved(request)
            return dict(request, ok=True)

    # Same permanent lock and host reservation as ordinary bootstrap. The
    # reservation transfer is replayable; neither old journal nor effects move.
    seal = bootstrap.module('scellement-runtime')
    with seal.locked(backend.path('/etc/ameesh-release/runtime-seal.lock'), exclusive=True,
                     uid=os.geteuid()):
        scopes = bootstrap.scopes(operation, prepared.bundle)
        if auth.validate(operation, scopes['preflight'], 'preflight') is not True:
            raise bootstrap.BootstrapError('signed continuation authority required')
        for step in INHERITED:
            check_inherited(step)
        return bootstrap._run(backend.journal(), operation, prepared.bundle,
                              authority=auth, adapter=wrap(Adapter()), predecessor=ref)

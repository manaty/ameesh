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


def replaced(backend, previous, old, new):
    """Proof that the trees moved by a signed replacement naming `previous`."""
    b = bootstrap
    swap = b.module('remplacement-runtime')
    found = swap.receipt_for(backend, previous)
    if found is None:
        raise b.BootstrapError('runtime change requires a completed signed replacement')
    item, bundle, text, record = found
    seal = b.delegation.SEAL_DOCUMENT
    if (old['plan']['runtime_tree_sha256'] != bundle['previous']['runtime']
            or old['plan']['source_tree_sha256'] != bundle['previous']['lib']
            or new['plan']['runtime_tree_sha256'] != bundle['next']['runtime']
            or new['plan']['source_tree_sha256'] != bundle['next']['lib']
            or (old['files'].get(seal) or {}).get('sha256') != bundle['previous']['seal_sha256']
            or (new['files'].get(seal) or {}).get('content') != bundle['seal']):
        raise b.BootstrapError('continuation does not match the signed replacement')
    decisions = b.signed.decisions
    entries = [e for e in backend.gestes.entries() if e.get('command') == 'replace'
               and e.get('phase') == 'start' and e.get('operation') == item.operation
               and e.get('bundle_digest') == bundle['digest']]
    identifier = entries[-1].get('decision') if entries else None
    view = decisions.get(backend.db, identifier) if type(identifier) is int else None
    answer = (view or {}).get('answer') or {}
    if (view is None or view.get('state') != 'answered' or view.get('needs_signature') is not True
            or view.get('question') != text or view.get('options') != swap.OPTIONS
            or answer.get('channel') != 'signed' or answer.get('option') != 'replace'
            or not decisions.verify_proof(backend.db, view)[0]):
        raise b.BootstrapError('exact signed replacement decision required')
    return dict(operation=item.operation, decision=identifier, digest=bundle['digest'],
                journal_sha256=b.protocol.identity(record))


def reference(backend, operation, previous, decision):
    b = bootstrap
    if (operation == previous or not isinstance(previous, str)
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', previous) is None
            or type(decision) is not int or decision <= 0):
        raise b.BootstrapError('distinct previous operation and signed decision required')
    previous_prepared = backend.prepared(previous)
    old = previous_prepared.bundle
    new = backend.prepared(operation).bundle
    b.delegation.question(old); b.delegation.question(new)
    # This operation adds the second transport. Trees and pin may differ only
    # by the signed, completed replacement of this very bootstrap (#5166); an
    # account/policy change needs another explicit design, never receipt reuse.
    old_plan, new_plan = dict(old['plan']), dict(new['plan'])
    local = new_plan.pop('local_transport', None)
    old_plan.pop('local_transport', None)
    ignored = {'/etc/ameesh-release/authorized_keys'}
    replacement = None
    if (old_plan['runtime_tree_sha256'], old_plan['source_tree_sha256']) != (
            new_plan['runtime_tree_sha256'], new_plan['source_tree_sha256']):
        replacement = replaced(backend, previous, old, new)
        ignored = ignored | {b.delegation.SEAL_DOCUMENT}
        for name in ('runtime_tree_sha256', 'source_tree_sha256'):
            old_plan[name] = new_plan[name]
    if (local is None or old_plan != new_plan or old['account'] != new['account']
            or old['host'] != new['host'] or old['machine_id'] != new['machine_id']
            or {k:v for k,v in old['files'].items() if k not in ignored}
               != {k:v for k,v in new['files'].items() if k not in ignored}):
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
    # A start entry is written even for a refused mandate. It is provenance,
    # not authority: authenticate the exact historical question and proof too.
    decisions = b.signed.decisions
    def signed_previous(identifier, *, current=False):
        view = decisions.get(backend.db, identifier)
        answer = (view or {}).get('answer') or {}
        return (view is not None and (not current or view.get('superseded_by') is None)
                and view.get('state') == 'answered'
                and view.get('needs_signature') is True and view.get('options') == b.OPTIONS
                and view.get('question') == previous_prepared.question
                and answer.get('channel') == 'signed' and answer.get('option') == 'install'
                and decisions.match_option(view, answer.get('text') or '') == 'install'
                and decisions.verify_proof(backend.db, view)[0])
    if not signed_previous(decision):
        raise b.BootstrapError('exact signed previous bootstrap decision required')
    # The append-only native journal connects these receipts to the old decision.
    entries = backend.gestes.entries()
    if not any(e.get('command') == 'install' and e.get('phase') == 'start'
               and e.get('operation') == previous and e.get('bundle_digest') == old['digest']
               and e.get('decision') == decision for e in entries):
        raise b.BootstrapError('previous signed install provenance unavailable')
    # Every signed install mandate can execute the old bundle, including one
    # never passed to install(). Scan the exact question without a history
    # limit: journal provenance alone cannot enumerate outstanding authority.
    # Superseded duplicates are permanently refused by install(); the named
    # predecessor remains historical authority for its continuation receipts.
    others = backend.db.query(
        "SELECT id FROM agent_mailbox WHERE kind = 'request'"
        " AND payload -> 'decision' ->> 'question' = %s"
        " AND payload -> 'decision' ->> 'state' = 'answered'"
        " AND id <> %s",
        (previous_prepared.question, decision))
    others = [row['id'] for row in others]
    if any(type(identifier) is int and signed_previous(identifier, current=True) for identifier in others):
        raise b.BootstrapError('multiple previous signed bootstrap decisions require reconciliation')
    result = dict(version=1, operation=previous, decision=decision, digest=old['digest'],
                  journal_sha256=b.protocol.identity(record), code_sha256=backend.gestes.code)
    if replacement is not None:
        result['replacement'] = replacement
    return result


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

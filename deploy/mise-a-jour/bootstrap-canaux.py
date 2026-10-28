#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Scoped signed bootstrap of the handoff and secret channel accounts (#4327).

The second signed bundle kind beside the release bootstrap: same durable
ordering engine (`bootstrap-release._run`, same host reservation and lock),
its own ordered steps, scope digests, signed question prefix and rollback
decision. An install answer never authorizes revoke, and a revoke answer
never authorizes an install step. No CLI here: `backend-administrateur.py`
is the only entry, and every effect goes through the host adapter.
"""
import importlib.util
import json
from pathlib import Path
import re
import time


def module(name):
    spec=importlib.util.spec_from_file_location('channels_bootstrap_'+name.replace('-','_'),
        Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


release=module('bootstrap-release')
delegation=module('delegation-canaux')
account=release.account
protocol=release.protocol
BootstrapError=release.BootstrapError
#: One account step per channel, in the renderer's fixed order.
ACCOUNT_STEPS={'account_'+name:name for name in sorted(delegation.CHANNELS)}
STEPS=('preflight','layout',*ACCOUNT_STEPS,'configuration','validate','enable')
ACTIONS=STEPS+('revoke',)
OPTIONS=[dict(key='install',label='Install these exact channel accounts and bounded delegations'),
         dict(key='leave',label='Leave this host unchanged')]
PREFIX='Approve the exact host channel accounts and delegations below:\n'
REVOKE_OPTIONS=[dict(key='revoke',label='Revoke these exact channel delegations'),
                dict(key='leave',label='Leave this host unchanged')]
REVOKE_PREFIX='Approve the exact host channel delegations revocation below:\n'
#: `decide` caps a signed question at 4000 characters (src/ameesh/decisions.py).
MAX_QUESTION=4000


def scopes(operation,bundle):
    if not isinstance(operation,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',operation):
        raise BootstrapError('bootstrap operation required')
    # Canonical renderer rejects altered inventory, including numeric ids.
    delegation.question(bundle)
    result={step:protocol.identity(dict(operation=operation,bundle_digest=bundle['digest'],step=step))
            for step in ACTIONS}
    for step,name in ACCOUNT_STEPS.items():
        ids=bundle['accounts'][name]
        result[step]=account.scope_digest(operation,bundle['machine_id'],ids['uid'],ids['gid'],
                                          name=ids['name'],home=ids['home'])
    return result


def question(operation,bundle, *, expires_at):
    if type(expires_at) is not int or expires_at<=0:
        raise BootstrapError('signed bootstrap deadline required')
    # The complete immutable review inventory is bound by its digest; the
    # administrator publishes it beside the request (4000-character cap).
    # Account numeric ids stay readable; their fixed fields are digest-bound.
    value=dict(version=1,kind=delegation.KIND,operation=operation,host=bundle['host'],
        machine_id=bundle['machine_id'],bundle_digest=bundle['digest'],
        accounts={name:'%s %d:%d'%(ids['name'],ids['uid'],ids['gid'])
                  for name,ids in sorted(bundle['accounts'].items())},
        accounts_digest=protocol.identity(bundle['accounts']),
        host_key_sha256=bundle['plan']['host_key_sha256'],
        runtime_tree_sha256=bundle['plan']['runtime_tree_sha256'],
        source_tree_sha256=bundle['plan']['source_tree_sha256'],
        controllers={name:dict(source=access['controller_source'],
                               key_sha256=protocol.identity(access['controller_key']))
                     for name,access in sorted(bundle['plan']['channels'].items())},
        file_sha256={path:info['sha256'] for path,info in bundle['files'].items()},
        previous_files=bundle['previous_files'],
        directories_digest=protocol.identity(bundle['directories']),services=bundle['services'],
        revoke_digest=protocol.identity(bundle['revoke']),
        scopes=scopes(operation,bundle),steps=list(ACTIONS),expires_at=expires_at)
    return PREFIX+json.dumps(value,sort_keys=True,separators=(',',':'))


def revoke_question(operation,bundle, *, expires_at):
    if type(expires_at) is not int or expires_at<=0:
        raise BootstrapError('signed revoke deadline required')
    value=dict(version=1,kind=delegation.KIND,operation=operation,host=bundle['host'],
        machine_id=bundle['machine_id'],bundle_digest=bundle['digest'],
        revoke_digest=protocol.identity(bundle['revoke']),
        file_sha256={path:info['sha256'] for path,info in bundle['files'].items()},
        previous_files=bundle['previous_files'],scope=scopes(operation,bundle)['revoke'],
        step='revoke',expires_at=expires_at)
    return REVOKE_PREFIX+json.dumps(value,sort_keys=True,separators=(',',':'))


class SignedChannelAuthority(release.SignedBootstrapAuthority):
    """The signed install of one exact channel bundle (steps of STEPS only)."""
    steps,option,options,prefix=STEPS,'install',OPTIONS,PREFIX

    def __init__(self,db,decision_id,operation,bundle, *, clock=time.time):
        if type(decision_id) is not int or decision_id<=0:
            raise BootstrapError('bootstrap decision required')
        self.db,self.decision_id,self.operation,self.clock=db,decision_id,operation,clock
        self.bundle=json.loads(json.dumps(bundle))
        self.scopes=scopes(operation,self.bundle)

    def render(self,operation,expires_at):
        return question(operation,self.bundle,expires_at=expires_at)


class SignedChannelRevokeAuthority(SignedChannelAuthority):
    """The separately signed rollback of one exact channel bundle."""
    steps,option,options,prefix=('revoke',),'revoke',REVOKE_OPTIONS,REVOKE_PREFIX

    def render(self,operation,expires_at):
        return revoke_question(operation,self.bundle,expires_at=expires_at)


def run(journal,operation,bundle, *, authority,adapter):
    """Ordered signed channel bootstrap; resumes exact intents, never rolls back.

    No tree or pin is published here, so the runtime seal lock is not taken;
    the host reservation shared with the release bootstrap serializes both.
    """
    return release._run(journal,operation,bundle,authority=authority,adapter=adapter,
                        steps=STEPS,scopes_of=scopes)


def destinations(*, plan, account_ids):
    """Exact reviewed destinations the host inventory must cover, sorted."""
    try:
        return sorted(delegation.render(plan,account_ids=account_ids)['files'])
    except delegation.DelegationError:
        raise BootstrapError('invalid channel bootstrap plan') from None


def prepare(*, operation, plan, account_ids, observed, expires_at):
    """Assemble the exact signed channel bundle and its question; no host effect.

    `observed` is the host inventory of exactly the reviewed destinations,
    produced by the administrator backend itself; a partial or unprotected
    report refuses. Returns (bundle, question).
    """
    if type(expires_at) is not int or expires_at<=0:
        raise BootstrapError('signed bootstrap deadline required')
    expected=destinations(plan=plan,account_ids=account_ids)
    if not isinstance(observed,dict) or set(observed)!=set(expected):
        raise BootstrapError('exact host file inventory required')
    try:
        bundle=delegation.render(plan,account_ids=account_ids,previous_files=observed)
    except delegation.DelegationError:
        raise BootstrapError('exact protected prior file required') from None
    text=question(operation,bundle,expires_at=expires_at)
    if len(text.encode())>MAX_QUESTION:
        raise BootstrapError('channel bootstrap question exceeds the signed limit')
    return bundle,text

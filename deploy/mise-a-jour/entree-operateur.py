#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Operator entry: assemble one signed bootstrap for an administered host.

Source only. This module has no CLI, no root and no transport: it never chooses
a program, a privilege or a destination. `prepare()` measures the exact staged
runtime/source trees, requires the host inventory of the exact reviewed
destinations, enrolls the probe turn as a protected document and renders the
canonical review bundle with its compact owner-signed question. `install()`
hands that reviewed bundle to the enrolled administrator backend
(`bootstrap.run` over the host adapter), which revalidates the scoped signature
before every effect and reconciles its own durable receipts.

`installateur-bootstrap.py` deliberately refuses a caller-selected CLI or root;
this entry adds none: assembly is pure, and execution accepts only the typed
result of `prepare()` plus an already enrolled authority/backend pair. The
probe document is built by `probe()` from the real registry row on the exact
host and the enrolled restart policy - never invented for an absent agent - and
`prepare()` re-attests the supplied document through that same path (plan host
and policy) before rendering the question: a document of another host or of an
absent agent refuses.

No root, SSH, systemd, PostgreSQL migration or production effect is executed by
building or testing this module: tests use disposable trees, a simulated
registry and simulated host effects.
"""
import importlib.util
import json
from pathlib import Path
import re


def module(name):
    spec=importlib.util.spec_from_file_location('operator_'+name.replace('-','_'),
        Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


bootstrap=module('bootstrap-release')
delegation=bootstrap.delegation
protocol=bootstrap.protocol
runtime=module('runtime-release')
pilot=module('pilote-sante')

#: Staged tree name -> pinned plan field. Both are inventoried, never declared.
TREES={'runtime':'runtime_tree_sha256','sources':'source_tree_sha256'}
TURN=delegation.TURN_DOCUMENT
#: `decide` caps a signed question at 4000 characters (src/ameesh/decisions.py).
MAX_QUESTION=4000
REGISTRY="SELECT name, host FROM agent_registry WHERE name = %s"


class OperatorError(ValueError):
    pass


class Prepared:
    """One assembled bootstrap: operation, exact review bundle and question."""

    __slots__=('operation','bundle','question','turn')

    def __init__(self,operation,bundle,question,turn=None):
        self.operation,self.bundle,self.question,self.turn=operation,bundle,question,turn

    def __repr__(self):
        # Never print document bodies or a private path.
        return 'Prepared(operation=%r,digest=%r)'%(self.operation,self.bundle.get('digest'))


def canonical(document):
    """The exact canonical JSON text an enrollment document is reviewed as."""
    if not isinstance(document,dict):
        raise OperatorError('enrollment document required')
    return json.dumps(document,sort_keys=True,separators=(',',':'))


def probe_document(enrollment):
    """Validated copy of a probe enrollment, by `HealthTurn`'s own rules."""
    try:
        return pilot.validate_enrollment(enrollment)
    except pilot.TurnError:
        raise OperatorError('invalid probe enrollment') from None


def probe(*, db, host, agent, account, home, seconds, policy):
    """Exact probe document for an agent really enrolled on this host.

    The document is the one `HealthTurn` reads. The probe must exist in the
    registry on this exact host and its own runner unit must belong to the
    enrolled restart policy, so a release can restart what the health step then
    probes. The unit is the one enrolled in `policy['units']` for this agent,
    never one fabricated here: none, or both deployed spellings at once,
    refuses. An unreadable or divergent registry refuses; nothing is fabricated.
    """
    try:
        delegation.restart.validate_policy(policy)
    except delegation.restart.HelperError:
        raise OperatorError('invalid restart policy') from None
    if policy['host']!=host:
        raise OperatorError('probe policy host mismatch')
    enrolled=[unit for unit in policy['units'] if unit in pilot.runner_units(agent)]
    if not enrolled:
        raise OperatorError('probe runner unit is not enrolled for this agent')
    if len(enrolled)>1:
        raise OperatorError('ambiguous enrolled probe runner units for this agent')
    document=probe_document(dict(agent=agent,unit=enrolled[0],
                                 account=account,home=home,seconds=seconds))
    if not callable(getattr(db,'query',None)):
        raise OperatorError('enrolled probe registry required')
    try:
        rows=db.query(REGISTRY,(document['agent'],))
    except Exception:
        raise OperatorError('probe registry unavailable') from None
    if (not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict)
            or rows[0].get('name')!=document['agent'] or rows[0].get('host')!=host):
        raise OperatorError('probe agent is not enrolled on this host')
    return document


def enrolled_probe(turn, *, db, plan):
    """The one probe document the enrolled registry and the pinned plan attest.

    `probe()` is the only path that reads the registry row on the plan's host
    and binds the runner unit to the plan's restart policy. A document supplied
    by a caller is accepted only when it is exactly the one that path returns,
    so a document of another host, or of an agent absent from the registry,
    cannot enter a signed question. A missing or unreadable registry refuses.
    """
    attested=probe(db=db,host=plan['host'],agent=turn['agent'],account=turn['account'],
                   home=turn['home'],seconds=turn['seconds'],policy=plan['policy'])
    if canonical(attested)!=canonical(turn):
        raise OperatorError('probe document does not match the enrolled registry')
    return attested


def destinations(*, plan, account_ids, probe_turn=None, runtime_seal=None):
    """Exact reviewed destinations the host inventory must cover, sorted.

    Rendering is pure: this is the same canonical inventory `prepare()` signs,
    so the host report cannot silently miss or add a file.
    """
    enrollment=None
    if probe_turn is not None:
        enrollment={TURN:canonical(probe_document(probe_turn))}
    if runtime_seal is not None:
        enrollment=dict(enrollment or {})
        enrollment[delegation.SEAL_DOCUMENT]=canonical(runtime_seal)
    try:
        review=delegation.render(plan,account_ids=account_ids,enrollment=enrollment)
    except delegation.DelegationError:
        raise OperatorError('invalid bootstrap plan') from None
    return sorted(review['files'])


def previous_files(observed, *, plan, account_ids, probe_turn=None, runtime_seal=None):
    """Exact reviewed prior state of every destination (None: absent).

    `observed` is the host inventory produced by the enrolled administrator
    backend for exactly these destinations. The host installer re-verifies
    these values against the live files before its first replacement, so a
    stale or partial report cannot authorize a write.
    """
    expected=destinations(plan=plan,account_ids=account_ids,probe_turn=probe_turn,runtime_seal=runtime_seal)
    if not isinstance(observed,dict) or set(observed)!=set(expected):
        raise OperatorError('exact host file inventory required')
    prior={}
    for path in expected:
        item=observed[path]
        if item is None:
            prior[path]=None
            continue
        if (not isinstance(item,dict) or set(item)!={'sha256','mode'}
                or not isinstance(item['sha256'],str)
                or re.fullmatch(r'[0-9a-f]{64}',item['sha256']) is None
                or type(item['mode']) is not int or not 0<=item['mode']<=0o777
                or item['mode'] & 0o022):
            raise OperatorError('exact protected prior file required')
        prior[path]=dict(item)
    return prior


def prepare(*, operation, plan, account_ids, trees, observed, probe_turn=None, expires_at,
            db=None, runtime_seal=None):
    """Assemble the exact signed bundle; no host effect and no chosen path.

    `trees` names the staged `runtime` and `sources` trees: both are measured
    here and must match the plan's pinned digests (a mismatch refuses rather
    than silently repinning). `observed` is the host's current inventory of
    every reviewed destination, `probe_turn` is the enrolled probe document
    (`probe()`) re-attested here through `enrolled_probe()` against the plan's
    host and policy (so `db`, the enrolled registry, is required as soon as a
    probe is enrolled), installed as a protected `turn.json` whose bytes the
    signed question binds, and `expires_at` is the signed execution deadline.
    """
    if not isinstance(operation,str) or not operation:
        raise OperatorError('bootstrap operation required')
    if type(expires_at) is not int or expires_at<=0:
        raise OperatorError('signed bootstrap deadline required')
    if not isinstance(trees,dict) or set(trees)!=set(TREES):
        raise OperatorError('exact staged trees required')
    turn=None; enrollment=None
    if probe_turn is not None:
        turn=probe_document(probe_turn)
        enrollment={TURN:canonical(turn)}
    if runtime_seal is not None:
        enrollment=dict(enrollment or {})
        enrollment[delegation.SEAL_DOCUMENT]=canonical(runtime_seal)
    try:
        template=delegation.render(plan,account_ids=account_ids,enrollment=enrollment)
    except delegation.DelegationError:
        raise OperatorError('invalid bootstrap plan') from None
    pinned=template['plan']
    if turn is not None:
        turn=enrolled_probe(turn,db=db,plan=pinned)
    for name,field in TREES.items():
        staged=str(trees[name])
        if not staged.startswith('/'):
            raise OperatorError('absolute staged '+name+' tree required')
        try:
            digest=runtime.inventory(staged)['digest']
        except (runtime.RuntimeError,OSError,TypeError,ValueError):
            raise OperatorError('staged '+name+' tree unavailable') from None
        if digest!=pinned[field]:
            raise OperatorError('staged '+name+' tree does not match the pinned digest')
    prior=previous_files(observed,plan=pinned,account_ids=account_ids,probe_turn=turn,runtime_seal=runtime_seal)
    try:
        bundle=delegation.render(pinned,account_ids=account_ids,previous_files=prior,
                                 enrollment=enrollment)
    except delegation.DelegationError:
        raise OperatorError('invalid bootstrap plan') from None
    if protocol.identity(bundle['files'])!=protocol.identity(template['files']):
        raise OperatorError('staged review changed during assembly')
    try:
        question=bootstrap.question(operation,bundle,expires_at=expires_at)
    except bootstrap.BootstrapError:
        raise OperatorError('invalid bootstrap operation') from None
    if len(question.encode())>MAX_QUESTION:
        raise OperatorError('bootstrap question exceeds the signed limit')
    return Prepared(operation,bundle,question,turn)


def install(prepared, *, authority, adapter, journal):
    """Execute a prepared bundle through the enrolled administrator backend.

    `authority` is the enrolled scoped signed bootstrap (it revalidates the
    host operation before every effect); `adapter` is the fixed host backend
    (`BootstrapHostAdapter` on a real host) and `journal` its private durable
    directory. All three are supplied by the administrator enrollment, not by
    a request: the reviewed bundle is the only input, and no caller-selected
    CLI, root or destination exists.
    """
    if not isinstance(prepared,Prepared):
        raise OperatorError('prepared bootstrap required')
    if not callable(getattr(authority,'validate',None)):
        raise OperatorError('enrolled bootstrap authority required')
    if not callable(getattr(adapter,'perform',None)):
        raise OperatorError('enrolled administrator backend required')
    return bootstrap.run(Path(journal),prepared.operation,prepared.bundle,
                         authority=authority,adapter=adapter)

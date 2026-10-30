#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Scoped signed bootstrap authority and durable independent ordering engine.

No CLI/root executor is installed. Each adapter must reconcile its own effects;
the access key is enabled only after runtime/account/config/service validation.
"""
import importlib.util
import json
from pathlib import Path
import re
import time


def module(name):
    spec=importlib.util.spec_from_file_location('bootstrap_'+name.replace('-','_'),
        Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


delegation=module('delegation-release')
account=module('compte-release')
signed=module('autorite-release')
protocol=delegation.restart.protocol
STEPS=('preflight','layout','runtime','sources','account','configuration','validate','enable')
ACTIONS=STEPS+('revoke',)
OPTIONS=[dict(key='install',label='Install this exact host bootstrap and bounded delegation'),
         dict(key='leave',label='Leave this host unchanged')]
PREFIX='Approve the exact host bootstrap and delegation below:\n'
#: Rollback is its own signed decision (#4323): an install answer never
#: authorizes revoke, and a revoke answer never authorizes an install step.
REVOKE_OPTIONS=[dict(key='revoke',label='Revoke this exact host bootstrap delegation'),
                dict(key='leave',label='Leave this host unchanged')]
REVOKE_PREFIX='Approve the exact host bootstrap revocation below:\n'


class BootstrapError(ValueError):
    pass


def scopes(operation,bundle):
    if not isinstance(operation,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',operation):
        raise BootstrapError('bootstrap operation required')
    # Canonical renderer rejects altered inventory, including numeric ids.
    delegation.question(bundle)
    ids=bundle['account']
    if set(ids)!={'name','system','shell','supplementary_groups','password_login','uid','gid'}:
        raise BootstrapError('exact bootstrap account ids required')
    result={step:protocol.identity(dict(operation=operation,bundle_digest=bundle['digest'],step=step)) for step in ACTIONS}
    result['account']=account.scope_digest(operation,bundle['machine_id'],ids['uid'],ids['gid'])
    for step,target,field in (('runtime','/opt/ameesh-release/runtime','runtime_tree_sha256'),
                              ('sources','/opt/ameesh-release/lib','source_tree_sha256')):
        result[step]=protocol.identity(dict(operation=operation,target=target,digest=bundle['plan'][field]))
    return result


def question(operation,bundle, *, expires_at):
    if type(expires_at) is not int or expires_at<=0:
        raise BootstrapError('signed bootstrap deadline required')
    # decide caps questions at 4000 characters. The complete immutable review
    # inventory is bound by its digest, not expanded into this signed question.
    # The administrator must publish that exact inventory beside the request.
    value=dict(version=1,operation=operation,host=bundle['host'],machine_id=bundle['machine_id'],
        bundle_digest=bundle['digest'],account=bundle['account'],policy_id=bundle['policy_id'],
        host_key_sha256=bundle['plan']['host_key_sha256'],
        runtime_tree_sha256=bundle['plan']['runtime_tree_sha256'],
        source_tree_sha256=bundle['plan']['source_tree_sha256'],
        controller_source=bundle['plan']['controller_source'],
        controller_key_sha256=protocol.identity(bundle['plan']['controller_key']),
        file_sha256={path:info['sha256'] for path,info in bundle['files'].items()},
        previous_files=bundle['previous_files'],
        directories_digest=protocol.identity(bundle['directories']),services=bundle['services'],
        revoke_digest=protocol.identity(bundle['revoke']),
        scopes=scopes(operation,bundle),steps=list(ACTIONS),expires_at=expires_at)
    if 'local_transport' in bundle['plan']:
        value['local_transport']=dict(
            source=bundle['plan']['local_transport']['controller_source'],
            key_sha256=protocol.identity(bundle['plan']['local_transport']['controller_key']))
    return PREFIX+json.dumps(value,sort_keys=True,separators=(',',':'))


def revoke_question(operation,bundle, *, expires_at):
    if type(expires_at) is not int or expires_at<=0:
        raise BootstrapError('signed revoke deadline required')
    value=dict(version=1,operation=operation,host=bundle['host'],machine_id=bundle['machine_id'],
        bundle_digest=bundle['digest'],revoke_digest=protocol.identity(bundle['revoke']),
        file_sha256={path:info['sha256'] for path,info in bundle['files'].items()},
        previous_files=bundle['previous_files'],scope=scopes(operation,bundle)['revoke'],
        step='revoke',expires_at=expires_at)
    return REVOKE_PREFIX+json.dumps(value,sort_keys=True,separators=(',',':'))


class SignedBootstrapAuthority(signed.SignedDecisionAuthority):
    def __init__(self,db,decision_id,operation,bundle, *, clock=time.time):
        if type(decision_id) is not int or decision_id<=0:
            raise BootstrapError('bootstrap decision required')
        self.db,self.decision_id,self.operation,self.clock=db,decision_id,operation,clock
        self.bundle=json.loads(json.dumps(bundle))
        self.scopes=scopes(operation,self.bundle)

    #: Steps, answer option, options, prefix and question of this decision.
    steps,option,options,prefix=STEPS,'install',OPTIONS,PREFIX

    def render(self,operation,expires_at):
        return question(operation,self.bundle,expires_at=expires_at)

    def validate(self,operation,digest,step):
        if operation!=self.operation or step not in self.steps or self.scopes[step]!=digest:
            return False
        view=signed.decisions.get(self.db,self.decision_id)
        if not view or view.get('state')!='answered' or view.get('needs_signature') is not True:
            return False
        answer=view.get('answer') or {}
        # #4788: install/revoke (release and channel bundles share this check)
        # name the option through the bounded native forms of
        # decisions.match_option, never the full signed text as the option.
        # The text stays whole in answer_digest/proof.
        if (view.get('options')!=self.options or answer.get('option')!=self.option
                or answer.get('channel')!='signed'
                or signed.decisions.match_option(view,answer.get('text') or '')!=self.option):
            return False
        text=view.get('question') or ''
        if not text.startswith(self.prefix):
            return False
        try:
            expires=json.loads(text[len(self.prefix):])['expires_at']
            if type(expires) is not int or text!=self.render(operation,expires):
                return False
        except (KeyError,ValueError,TypeError):
            return False
        now=self.clock()
        return now<expires and self._proof_live(view,now,expires)


class SignedBootstrapRevokeAuthority(SignedBootstrapAuthority):
    """The separately signed rollback of one exact bootstrap bundle."""
    steps,option,options,prefix=('revoke',),'revoke',REVOKE_OPTIONS,REVOKE_PREFIX

    def render(self,operation,expires_at):
        return revoke_question(operation,self.bundle,expires_at=expires_at)


def _run(journal,operation,bundle, *, authority,adapter,steps=STEPS,scopes_of=None):
    """Resume exact bootstrap intents; no automatic destructive rollback.

    adapter validates the real host/runtime closure before copying, preserves
    operation-specific config snapshots, validates effective SSH/sudo settings,
    proves independent controller readiness and enables the key last. Revoke
    remains the separately signed administrator artifact; failures/expired
    authority preserve journals, accounts, runtime and existing live fences.
    """
    bundle=json.loads(json.dumps(bundle))
    # Another signed bundle kind (delegation-canaux) supplies its own ordered
    # steps and scope function; the journal, reservation and lock are shared.
    action_scopes=(scopes_of or scopes)(operation,bundle)
    def approved(step):
        try:
            valid=authority.validate(operation,action_scopes[step],step) is True
        except Exception:
            valid=False
        if not valid:
            raise BootstrapError('bootstrap authority unavailable')
    approved('preflight')
    with protocol.locked(journal,bundle['host']) as root:
        approved('preflight')
        reservation=root/(bundle['host']+'.active.json')
        binding=dict(operation=operation,digest=bundle['digest'])
        if reservation.exists() or reservation.is_symlink():
            if protocol.load(reservation)!=binding:
                raise BootstrapError('host has another unresolved bootstrap')
        else:
            protocol.save(reservation,binding)
        path=root/(operation+'.json')
        if path.exists() or path.is_symlink():
            record=protocol.load(path)
            if (set(record)!={'bundle','intent','receipts','complete'}
                    or protocol.identity(record['bundle'])!=protocol.identity(bundle)):
                raise BootstrapError('bootstrap journal mismatch')
        else:
            record=dict(bundle=bundle,intent=None,receipts={},complete=False)
            protocol.save(path,record)
        if (not isinstance(record['receipts'],dict) or type(record['complete']) is not bool
                or record['intent'] not in (*steps,None)):
            raise BootstrapError('bootstrap journal state mismatch')
        def request_for(step):
            return dict(operation=operation,step=step,action_id=operation+':'+step,
                        scope_digest=action_scopes[step],bundle=bundle)
        for step,receipt in record['receipts'].items():
            if (step not in steps or not isinstance(receipt,dict)
                    or protocol.identity(receipt)!=protocol.identity(dict(request_for(step),ok=True))):
                raise BootstrapError('bootstrap recorded receipt mismatch')
        if record['complete'] and (set(record['receipts'])!=set(steps) or record['intent'] is not None):
            raise BootstrapError('bootstrap completion journal mismatch')
        if record['complete'] is True:
            # Do not reprovision/re-enable on replay after a revocation.
            reservation.unlink(missing_ok=True)
            return 'installed'
        for step in steps:
            if step in record['receipts']:
                continue
            approved(step)
            request=request_for(step)
            record['intent']=step
            protocol.save(path,record)
            try:
                receipt=adapter.perform(step,request)
            except Exception:
                raise BootstrapError('bootstrap outcome uncertain; reconcile intent') from None
            if (not isinstance(receipt,dict) or type(receipt.get('ok')) is not bool
                    or protocol.identity(receipt)!=protocol.identity(dict(request,ok=receipt['ok']))):
                raise BootstrapError('bootstrap receipt mismatch')
            if receipt['ok'] is not True:
                raise BootstrapError('bootstrap held; preserve artifacts and reservation')
            approved(step)
            record['receipts'][step]=receipt
            record['intent']=None
            protocol.save(path,record)
        record['complete']=True
        protocol.save(path,record)
        reservation.unlink()
        return 'installed'


def run(journal,operation,bundle, *, authority,adapter):
    """Serialize signed pin/tree publication against every H8 invocation.

    The administrator enrolls the permanent root-owned lock before bootstrap;
    lock creation is not an authorization and grants no execution permission.
    Never replace/unlink that inode, including on bounded rollback.
    """
    if (delegation.SEAL_DOCUMENT not in bundle.get('files',{})
            and not Path(delegation.SEAL_DOCUMENT).exists()):
        return _run(journal,operation,bundle,authority=authority,adapter=adapter)
    seal=module('scellement-runtime')
    with seal.locked(exclusive=True):
        return _run(journal,operation,bundle,authority=authority,adapter=adapter)

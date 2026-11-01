# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in runner path for admitted sandbox cards (#5121).

No request chooses a provider, placement or billing account. Absence of a
sandbox block returns to the existing runner before any turn is opened.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from . import account_budget, account_turn, account_window, canon, canon_sync
from . import placement, registry, relay, turn_context, version
from .sandbox_provider import JobIdentity, JobSpec
from .sandbox_supervisor import _deadline
from .sandbox_turn import SandboxTurn, authorize
from .storage.postgres import sandbox as s1

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SandboxCredentials:
    """Trusted issuer's token and verified claims; never supplied by mail."""
    base_url: str
    token: str
    identity: relay.Identity


def credentials(worker, row):
    """Require an installed turn-scoped issuer; legacy lease tokens cannot do this.

    S6 must wire ``runner.sandbox_credentials_for(agent, owner, epoch, turn,
    deadline_ts)`` to the actual issuer/verifier. Missing wiring is a named
    refusal, never a fallback to the mediated executor's lease token.
    """
    issuer = getattr(worker.runner, 'sandbox_credentials_for', None)
    if not callable(issuer):
        raise s1.Refusal('profile-refused')
    value = issuer(worker.name, worker.runner.runner_id, worker.epoch,
                   row['turn_id'], row['deadline_ts'])
    if not isinstance(value, SandboxCredentials) or not isinstance(value.identity, relay.Identity):
        raise s1.Refusal('profile-refused')
    identity = value.identity
    expiry = identity.expires_ts
    if (identity.agent != worker.name or identity.host != worker.runner.host
            or identity.lease_owner != worker.runner.runner_id
            or identity.lease_epoch != worker.epoch or identity.turn != row['turn_id']
            or isinstance(expiry, bool) or not isinstance(expiry, (int, float))
            or not math.isfinite(expiry) or not time.time() < expiry <= _deadline(row['deadline_ts'])
            or not isinstance(value.token, str) or not value.token
            or not isinstance(value.base_url, str)):
        raise s1.Refusal('profile-refused')
    url = urlsplit(value.base_url)
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username
            or url.password or url.query or url.fragment):
        raise s1.Refusal('profile-refused')
    return value


def _pinned_canon(worker, matches=None):
    """The owning canon at its synchronized revision.

    `select` passes the list it has already loaded, so a legacy turn pays one
    `canon.load_configured` instead of two (~0.2 s each); callers that need a
    fresh read omit it.
    """
    if matches is None:
        matches = [c for c in canon.load_configured(worker.cfg) if c.owns(worker._canon_id())]
    if len(matches) != 1:
        raise s1.Refusal('profile-refused')
    loaded = matches[0]
    state = canon_sync.state(worker.db, worker.runner.host, loaded)
    pinned = str((state or {}).get('last_good_commit') or '')
    if not pinned:
        raise s1.Refusal('profile-refused')
    if not loaded.sources or loaded.sources[0].commit != pinned:
        loaded = canon.load(loaded.root, ref=pinned, default=loaded.is_default)
    return loaded, state, pinned


def select(worker):
    """Read the Agent's owning canon at its synchronized revision.

    An unreadable declaration is a refusal, including parser failures that
    leave card.sandbox=None.
    """
    if not getattr(worker.cfg, 'canon', None):
        return None
    # Legacy agents (including spawned agents without a card) do not depend
    # on sandbox admission or on a synchronized sandbox revision.
    matches = [c for c in canon.load_configured(worker.cfg) if c.owns(worker._canon_id())]
    declared = [c for c in matches if c.agent(worker.name) is not None
                and 'sandbox' in c.agent(worker.name).fiche.data]
    try:
        loaded, state, pinned = _pinned_canon(worker, matches)
    except Exception:
        # No declared sandbox must ever make a legacy agent depend on sync.
        # When a pinned revision is available it remains authoritative: a
        # local card edit must not bypass a still-synchronized sandbox block.
        if not declared:
            return None
        raise s1.Refusal('profile-refused') from None
    try:
        card = loaded.agent(worker.name)
        if card is None:
            return None
        declared = 'sandbox' in card.fiche.data
        if not declared:
            return None
        if (not loaded.readable or loaded.untrusted or not placement.canon_fresh(state)[0]
                or any(f.severity == canon.ERROR and f.agent == worker.name
                       and 'sandbox' in f.code for f in loaded.load_findings)):
            raise s1.Refusal('profile-refused')
        account = account_turn.for_turn(worker)
        admitted = placement.sandbox_admission(
            loaded, worker.name, worker.runner.host, canon_ref=pinned,
            reserve_usd=getattr(account, 'reserve_usd', None))
        if not admitted.ok or not admitted.tranche or not account_budget.billing_of(account):
            raise s1.Refusal(admitted.refusal or 'profile-refused')
        return admitted, loaded.host(worker.runner.host), account
    except s1.Refusal:
        raise
    except Exception:
        # Never let an unreadable block turn into an unrestricted local turn.
        raise s1.Refusal('profile-refused') from None


def grant_valid(worker, admitted):
    """A synchronized revocation or changed bound fences the running turn."""
    try:
        loaded, state, pinned = _pinned_canon(worker)
        if not placement.canon_fresh(state)[0]:
            return False
        return placement.sandbox_recheck(loaded, admitted, canon_ref=pinned).verdict in ('same', 'cosmetic')
    except Exception:
        return False


def source():
    """The admitted executable source, never a request's claimed source."""
    commit = os.environ.get('AMEESH_SOURCE_COMMIT', '')
    if not commit:
        try:
            commit = json.loads(Path(__file__).with_name('source.json').read_text())['commit']
        except (OSError, ValueError, KeyError, TypeError):
            raise s1.Refusal('source-unavailable') from None
    if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit):
        raise s1.Refusal('source-unavailable')
    return dict(release=version(), commit=commit, contract='ameesh-sandbox/1')


def kubernetes_provider(options):
    """Child-only factory; material arrives through the private handoff."""
    from .sandbox_kubernetes import KubernetesJobProvider
    data = dict(options['placement'])
    data['job'] = canon.SandboxJob(**data['job'])
    host = SimpleNamespace(policy=canon.HostPolicy(sandbox=canon.SandboxPlacement(**data)))
    return KubernetesJobProvider(host, turn_seconds=options['turn_seconds'],
                                 secrets=lambda identity: options['secrets'])


def run(worker, spec, selected):
    """Open one ordinary leased turn, then let only S1/S3 finish it."""
    admitted, host, account = selected
    if worker.runner.dry_run:
        return True
    turn_id = spec['turn_id']
    worker._turn_id = turn_id
    adapter = None
    intent = False
    marker = None
    spend_key = None
    monitor_done = threading.Event()
    pgid = None
    completed = False
    cancelling = False
    provider_io = False
    start_options = None
    job = None
    cancel_requested = threading.Event()

    def cancel_and_observe():
        """Fence first; keep the supervisor alive until deletion is observed.

        Before the first step no provider call can have occurred. Otherwise a
        broken transport is replaced by an observer of the same fenced turn,
        never by a recovery adapter borrowing another lease.
        """
        nonlocal completed, pgid
        adapter.cancel()
        registry.restore_prompt(worker.db, worker.name, worker.runner.runner_id, worker.epoch)
        if not provider_io:
            outcome = adapter.consume(dict(kind='cancelled', turn_id=turn_id))
        else:
            if adapter.child is None or adapter.child.process.poll() is not None:
                adapter.close()
                if pgid is not None:
                    worker._close_turn_resource(turn_id, pgid)
                child = adapter.start(job, **start_options)
                with worker.lock:
                    worker.proc = child.process
                    # start_new_session makes the new child its group leader.
                    pgid = worker.pgid = child.process.pid
                worker._open_turn_resource(turn_id, pgid)
            while True:
                _, outcome = adapter.step()
                if outcome.get('state') in s1.TERMINAL or outcome.get('state') == 'orphaned':
                    break
                time.sleep(0.05)
        completed = outcome.get('state') in s1.TERMINAL
        if completed:
            worker._compta_travail = None

    try:
        executable_source = source()
        if not callable(getattr(worker.runner, 'sandbox_credentials_for', None)):
            raise s1.Refusal('profile-refused')
        worker.stop_requested.clear()
        boundary = spec.get('_preempt_gen', worker.preempt_gen)
        with worker._preempt_lock:
            if worker.preempt_gen <= boundary:
                worker.preempting.clear()
        requests = spec.get('rows') or ()
        candidates = [r for r in requests if isinstance(r.get('payload'), dict)
                      and r['payload'].get('schema') == 'ameesh-sandbox-request/1']
        if len(candidates) != 1:
            raise s1.Refusal('unauthorized-request')
        claim = account_window.claim_start(
            worker.db, worker.agent['harness'], getattr(account, 'name', None), worker.cfg.host,
            worker.name, profile=account,
            begin=lambda tx: registry.begin_turn(tx, worker.name, worker.runner.runner_id,
                                                worker.epoch, worker._marqueurs_durables('sandbox turn')))
        if not claim.ok:
            registry.restore_prompt(worker.db, worker.name, worker.runner.runner_id, worker.epoch)
            return False
        if not claim.begun and not registry.begin_turn(
                worker.db, worker.name, worker.runner.runner_id, worker.epoch, 'sandbox turn'):
            return False
        if not worker._compta_marque(0, 'sandbox', worker.agent.get('model'),
                                     work_item_id=spec.get('lot'), turn_id=turn_id):
            raise s1.Refusal('missing')
        marker = worker._compta_travail
        spend_key = worker._spend_key(0, marker)
        document = turn_context.build(
            turn_id=turn_id, agent=worker.name, host=worker.runner.host,
            kind=spec.get('kind') or 'mail', started_ts=time.time(), session=None,
            work_item_id=spec.get('lot'), lease_owner=worker.runner.runner_id,
            lease_epoch=worker.epoch, lease_expires_ts=worker.lease_deadline,
            requests=requests, harness=worker.agent['harness'], model=worker.agent.get('model'),
            budget_usd=worker.agent.get('budget_usd'), spent_usd=worker.agent.get('spent_usd'),
            spend_key=spend_key, marker=marker, reservation=None)
        turn_context.write(worker.state_dir, document, secrets=turn_context.environment_secrets())
        adapter = SandboxTurn.current(worker.db, environ={
            turn_context.ENV: turn_context.path(worker.state_dir), 'AMEESH_TURN_ID': turn_id,
            'AGENT_MAIL_NAME': worker.name, 'AMEESH_RUNNER_ID': worker.runner.runner_id,
            'AMEESH_LEASE_EPOCH': str(worker.epoch)})
        request = authorize(document, candidates[0]['id'])
        snapshot = {k: getattr(admitted, k) for k in (
            'profile_ref', 'profile_digest', 'placement_ref', 'placement_digest',
            'canon_ref', 'image_digest', 'provider', 'namespace')}
        limits = dict(admitted.limits, profile_label=admitted.profile, placement_label=host.title)
        snapshot.update(limits=limits, scope=request['scope'], source=executable_source,
                        max_attempts=admitted.limits['max_attempts'],
                        max_concurrent_jobs=host.policy.sandbox.max_concurrent_jobs)
        row = adapter.admit(candidates[0]['id'], snapshot=snapshot,
                            billing_account=account_budget.billing_of(account), tranche_id=admitted.tranche)
        intent = True
        reservation = account_budget.reservation(worker.db, spend_key)
        document['accounting']['reservation'] = turn_context._reservation(reservation)
        turn_context.write(worker.state_dir, document, secrets=turn_context.environment_secrets())
        material = credentials(worker, row)
        job_input = dict(schema='ameesh-sandbox-job/1', turn_id=turn_id, epoch=worker.epoch,
                         owner=worker.runner.runner_id, deadline_ts=_deadline(row['deadline_ts']),
                         image_digest=admitted.image_digest,
                         scope=row['scope'], scope_digest=row['scope_digest'], limits=admitted.limits,
                         source=executable_source, prompt=spec['prompt'], relay_url=material.base_url)
        job = JobSpec(JobIdentity(turn_id, worker.name, worker.epoch, host.title,
                                  admitted.profile, admitted.namespace), admitted.image,
                      admitted.active_deadline_seconds, admitted.ttl_seconds_after_finished)
        options = dict(placement=asdict(host.policy.sandbox), turn_seconds=admitted.limits['turn_seconds'],
                       secrets=dict(prompt=json.dumps(job_input), relay_token=material.token))
        start_options = dict(provider_factory=__name__ + ':kubernetes_provider',
                             provider_options=options, new_session=True, state_dir=worker.state_dir,
                             orphan_after=admitted.limits['job_seconds'],
                             timeout=admitted.limits['turn_seconds'])
        with worker.lock:
            worker._sandbox_cancel_requested = cancel_requested
        child = adapter.start(job, **start_options)
        with worker.lock:
            worker.proc = child.process
            pgid = worker.pgid = os.getpgid(child.process.pid)
        worker._open_turn_resource(turn_id, pgid)
        threading.Thread(target=worker._preempt_monitor, args=(monitor_done,),
                         daemon=True, name='sandbox-preempt-monitor').start()
        if worker.lease_lost.is_set():
            worker.kill_if_stop_requested()
            return False
        if (cancel_requested.is_set() or worker.stopping.is_set()
                or worker.stop_requested.is_set() or worker.preempting.is_set()):
            cancel_and_observe()
            return False
        worker._turn_launched = True
        worker._consume_scheduled_wake(spec)
        worker._livre_courrier(spec)
        while True:
            if worker.lease_lost.is_set():
                worker.kill_if_stop_requested()
                return False
            if (cancel_requested.is_set() or worker.stopping.is_set()
                    or worker.stop_requested.is_set() or worker.preempting.is_set()):
                cancel_and_observe()
                return False
            if not cancelling and not grant_valid(worker, admitted):
                adapter.cancel()
                cancelling = True
            provider_io = True
            event, outcome = adapter.step()
            if event['kind'] == 'result':
                worker._compta_travail = None
                completed = True
                return True
            if outcome.get('state') in s1.TERMINAL or outcome.get('state') == 'orphaned':
                completed = outcome.get('state') in s1.TERMINAL
                if completed:
                    worker._compta_travail = None
                return False
            worker.stopping.wait(min(1.0, admitted.limits['turn_seconds']))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, turn_context.ContextError) else getattr(exc, 'code', type(exc).__name__)
        LOG.warning('sandbox turn refused (agent=%s, reason=%s)', worker.name,
                    reason)
        if intent and adapter is not None:
            try:
                cancel_and_observe()
            except Exception as cancel_error:
                LOG.warning('sandbox cancellation pending (error=%s)', type(cancel_error).__name__)
        elif marker:
            worker._annule_tour_avant_lancement(marker, spend_key)
        if not intent:
            worker.fail_turn('sandbox refused: ' + reason, reason)
        return False
    finally:
        monitor_done.set()
        if adapter is not None:
            adapter.close()
        with worker.lock:
            worker._sandbox_cancel_requested = None
            worker.proc = None
            worker.pgid = None
        worker.last_activity = time.monotonic()
        worker._turn_end_mono = worker.last_activity
        if completed:
            with worker.runner.lock:
                worker.runner.turns += 1
        if pgid is not None:
            worker._close_turn_resource(turn_id, pgid)


def dispatch(worker, spec):
    """None means exactly the legacy path; a refused sandbox never falls back."""
    try:
        selected = select(worker)
    except s1.Refusal as exc:
        worker.fail_turn('sandbox refused: ' + exc.code, exc.code)
        return False
    return None if selected is None else run(worker, spec, selected)

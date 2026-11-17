# SPDX-License-Identifier: AGPL-3.0-only
"""#7441: one durable Claude wave, consumed at each persona's next turn.

Only account names and journal epochs cross hosts. No running worker,
session, configuration directory or provider process is modified here.

#7712 : une vague ne doit jamais immobiliser un critique. L'origine de
session ne vaut que pour son propre harnais, une bascule effective déjà
arrivée à la destination proposée ne fait pas refuser le tour, et un vrai
changement concurrent refuse au plus un tour avant de laisser passer.
"""
from __future__ import annotations

import uuid

from . import accounts, registry, storage

#: #7712: a wave coordination refusal is not a budget pause. The runner writes
#: the reason with this prefix, so its status and the alerts name the cause
#: ("coordination de vague") instead of "paused on budget".
WAVE_PREFIX = "account wave: "
WAVE_RETRY_REASON = WAVE_PREFIX + "changed while planning; retry next turn"
#: #7712 (c): the local state of the one-retry latch. The value is the journal
#: snapshot that was refused; the same snapshot on the next pass reconciles
#: on the effective account and goes on.
WAVE_RETRY_STATE = "wave_retry"


def _snapshot(last, pending) -> str:
    return "%s:%s" % (int((last or {}).get("id") or 0),
                      int((pending or {}).get("id") or 0))


def _retry_marker(worker) -> str:
    read = getattr(worker, "_state_read", None)
    if callable(read):
        return str(read(WAVE_RETRY_STATE) or "")
    return str(getattr(worker, "_wave_retry", "") or "")


def _set_retry_marker(worker, value: str) -> None:
    worker._wave_retry = value
    note = getattr(worker, "_state_note", None)
    if callable(note):
        note(WAVE_RETRY_STATE, value)


def _choose(worker, items, book, *, now, pause, margin, origin):
    """Le choix courant relu depuis une autre origine (#7712 (c))."""
    from . import account_turn

    return accounts.choose(
        worker.db, worker.cfg.host, 'claude', items, book, agent=worker.name,
        session_account=origin, critical=True,
        model=account_turn.session_model(worker, book), cfg=worker.cfg,
        now=now, paced=pause, margin=margin)


def coordinate(worker, items, book, choice, *, now, pause=True, margin=False):
    store = storage.of(worker.db).accounts
    pending = store.pending_agent_plan('claude', worker.name)
    last = store.last_agent_switch('claude', worker.name)
    snapshot = _snapshot(last, pending)
    marker = _retry_marker(worker)
    retry = bool(marker) and marker == snapshot
    if retry:
        # #7712 (c): a genuine concurrent change refused the previous turn.
        # Reconcile on the effective account so the same snapshot cannot
        # produce the same refusal forever.
        origin = (last or {}).get('to_account')
        if origin:
            choice = _choose(worker, items, book, now=now, pause=pause,
                             margin=margin, origin=origin)
        pending = store.pending_agent_plan('claude', worker.name)
        snapshot = _snapshot(last, pending)
    if choice.switched:
        proposals = []
        rows = registry.overview(worker.db)
        rows = [row for row in rows if row['name'] != worker.name]
        rows.append(dict(worker.agent or {}, name=worker.name, host=worker.cfg.host,
                         harness='claude', session_account=choice.switched['from']))
        for row in rows:
            if row.get('harness') != 'claude' or row.get('status') == 'stopped':
                continue
            if row['name'] != worker.name \
                    and row.get('session_harness') not in (None, '', 'claude'):
                # #7712 (a): an account name is local to its harness; a
                # session opened under another harness has no Claude origin.
                continue
            if not worker.runner.is_critical(row['name'], row):
                continue
            old = store.pending_agent_plan('claude', row['name'])
            effective = store.last_agent_switch('claude', row['name'])
            origin = row.get('session_account')
            if not origin:
                continue  # no known session origin: never infer a remote secret
            candidate = (choice if row['name'] == worker.name else accounts.choose(
                worker.db, row.get('host') or worker.cfg.host, 'claude', items, book,
                agent=row['name'], session_account=origin, critical=True,
                model=row.get('model') or '', cfg=worker.cfg, now=now,
                simulate=True, paced=pause, margin=margin))
            if candidate.retained:
                store.record_hysteresis(row.get('host') or worker.cfg.host, 'claude',
                                        agent=row['name'], **candidate.retained)
            if not (candidate.switched and not candidate.auth_blocked):
                continue
            if (effective or {}).get('to_account') == candidate.switched['to']:
                # #7712 (b): the last effective switch already designates the
                # proposed destination. Nothing left to plan for this persona,
                # and no reason to refuse the turn: it goes on that account.
                continue
            proposals.append(dict(candidate.switched, agent=row['name'],
                                  host=row.get('host') or worker.cfg.host,
                                  expected_plan=int((old or {}).get('id') or 0),
                                  expected_switch=int((effective or {}).get('id') or 0)))
        if proposals:
            published = store.plan_agent_wave('claude', proposals, wave=str(uuid.uuid4()))
            pending = store.pending_agent_plan('claude', worker.name)
            if not published and pending is None:
                if retry:
                    # #7712 (c): one refusal was enough. A second rejection
                    # of the same snapshot must not stop the turn forever.
                    _set_retry_marker(worker, "")
                    return choice
                _set_retry_marker(worker, snapshot)
                return accounts.Choice(None, choice.active, reason=WAVE_RETRY_REASON)
    if pending:
        from . import account_turn
        origin = account_turn.session_account(worker, items)
        if origin is None:
            last = store.last_agent_switch('claude', worker.name)
            origin = (last or {}).get('to_account') or pending['from_account']
        if pending['from_account'] == origin:
            # The destination is a preference, rechecked against every local
            # guard (profiles differ by host). Dwell may still hold this persona.
            choice = accounts.choose(
                worker.db, worker.cfg.host, 'claude', items, book, agent=worker.name,
                session_account=origin, critical=True,
                model=account_turn.session_model(worker, book), cfg=worker.cfg,
                now=now, paced=pause, margin=margin,
                planned_account=pending['to_account'])
            if choice.switched and choice.profile.name == pending['to_account']:
                choice.switched = dict(choice.switched, epoch=pending['wave_epoch'],
                                       reason=pending['reason'])
    if marker:
        # Ne pas fsync un état vide à chaque tour : le marqueur n'existe qu'au
        # lendemain d'un refus.
        _set_retry_marker(worker, "")
    return choice

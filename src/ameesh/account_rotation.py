# SPDX-License-Identifier: AGPL-3.0-only
"""#7441: one durable Claude wave, consumed at each persona's next turn.

Only account names and journal epochs cross hosts. No running worker,
session, configuration directory or provider process is modified here.
"""
from __future__ import annotations

import uuid

from . import accounts, registry, storage


def coordinate(worker, items, book, choice, *, now, pause=True, margin=False):
    store = storage.of(worker.db).accounts
    pending = store.pending_agent_plan('claude', worker.name)
    if choice.switched:
        proposals = []
        rows = registry.overview(worker.db)
        rows = [row for row in rows if row['name'] != worker.name]
        rows.append(dict(worker.agent or {}, name=worker.name, host=worker.cfg.host,
                         harness='claude', session_account=choice.switched['from']))
        for row in rows:
            if row.get('harness') != 'claude' or row.get('status') == 'stopped':
                continue
            if not worker.runner.is_critical(row['name'], row):
                continue
            old = store.pending_agent_plan('claude', row['name'])
            last = store.last_agent_switch('claude', row['name'])
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
            if candidate.switched and not candidate.auth_blocked:
                proposals.append(dict(candidate.switched, agent=row['name'],
                                      host=row.get('host') or worker.cfg.host,
                                      expected_plan=int((old or {}).get('id') or 0),
                                      expected_switch=int((last or {}).get('id') or 0)))
        if proposals:
            published = store.plan_agent_wave('claude', proposals, wave=str(uuid.uuid4()))
            pending = store.pending_agent_plan('claude', worker.name)
            if not published and pending is None:
                return accounts.Choice(None, choice.active,
                                       reason='account wave changed while planning; retry next turn')
    if pending:
        from . import account_turn
        origin = account_turn.session_account(worker, items)
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
    return choice

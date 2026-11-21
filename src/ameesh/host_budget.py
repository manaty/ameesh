# SPDX-License-Identifier: AGPL-3.0-only
"""Observed infrastructure costs, never an estimate of an unknown bill."""
from . import host_idle, work


def project(canons, observations):
    cards = {}
    for canon in canons:
        for host in canon.hosts:
            cards.setdefault(host.title, []).append(host.fiche.data)
    observed = {row['host']: row for row in observations}
    result = []
    for name in sorted(set(cards) | set(observed)):
        cost = host_idle.cost_states(cards.get(name, []))
        obs = observed.get(name) or {}
        start, end = obs.get('on_ts'), obs.get('seen_ts')
        hours = None
        if (obs.get('provider_state') == 'running' and start is not None
                and end is not None and 0 <= start <= end):
            hours = (end - start) / 3600
        rate = cost.get('on') if cost else None
        result.append({'host': name, 'provider_state': obs.get('provider_state'),
                       'observed_at': end, 'observed_on_hours': hours,
                       'rate_eur_h': rate, 'rate_source': cost.get('source') if cost else None,
                       'observed_cost_eur': round(hours * rate, 6)
                       if hours is not None and rate is not None else None,
                       'total_cost_eur': None})
    return result


def status(cfg, db):
    from .storage.postgres.vm_adapter import Queue
    try:
        observations = Queue(db).observations()
    except Exception:
        observations = []
    return project(work.configured_canons(cfg), observations)

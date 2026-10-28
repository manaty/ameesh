# SPDX-License-Identifier: AGPL-3.0-only
"""#4151: reserved idle capacity, observed and addressed to mesh agents only."""
from . import host_idle, resources, storage

TYPES = ('host_idle_reserved', 'baseline_oversized')


def project(footprints, listing, *, now, idle_after_s=1800, baseline_hosts=2,
            held_hosts=(), draining_hosts=(), orchestrators=()):
    """Alert projection over A2 evidence; no host lifecycle effects."""
    from .exploitation import _alert
    excluded = set(held_hosts) | set(draining_hosts)
    idle = {}
    for host, fp in sorted(footprints.items()):
        since = resources.finite_float(fp.get('idle_since'))
        if (host in excluded or fp.get('state') != 'idle' or fp.get('unknown')
                or since is None or not idle_after_s <= now - since):
            continue
        idle[host] = fp
    out = []
    for host, fp in idle.items():
        if fp.get('capacity') != 'burst':
            continue
        agents = sorted(a['name'] for a in listing if a.get('host') == host)
        detail = ('Host %s idle with reserved capacity: %s. Holders: agents %s; '
                  'lot leases %s; declared runs %s (%s). Action: propose drainage '
                  '(ameesh host drain %s), hold (ameesh hosts %s --hold --until 2h '
                  '--reason <reason>), or stop an unnecessary reserve after '
                  'checking the leader and reserve guards. No automatic action.' % (
                      host, host_idle.line(fp), ', '.join(agents) or 'none',
                      fp.get('leases'), fp.get('runs'), fp.get('runs_source'), host, host))
        out.append(_alert('host_idle_reserved', None, fp['idle_since'],
                          now - fp['idle_since'], idle_after_s, detail,
                          host=host, agents=agents, footprint=fp,
                          orchestrators=list(orchestrators)))
    if len(idle) > baseline_hosts:
        # Keep protected leaders/reserves first; suggest no guarded host for
        # reclassification, even if the critical roles currently span > N hosts.
        keep = sorted(idle, key=lambda h: (not idle[h].get('baseline_guard', False),
                                           idle[h].get('capacity') != 'baseline', h))
        candidates = [h for h in keep[baseline_hosts:]
                      if not idle[h].get('baseline_guard', False)]
        agents = sorted(a['name'] for a in listing if a.get('host') in idle)
        since = max(fp['idle_since'] for fp in idle.values())
        detail = ('%d hosts idle together for at least %g s (baseline_hosts=%d): %s. '
                  'Hosts to classify as burst or release if already burst: %s. '
                  'Regroup resident/critical agents first: %s. Preserve the elected '
                  'leaders and reserves; no automatic action.' % (
                      len(idle), now - since, baseline_hosts, ', '.join(sorted(idle)),
                      ', '.join(candidates) or 'none until critical guards can be regrouped',
                      ', '.join(agents) or 'none'))
        detail += ' Footprints: ' + '; '.join(h + ': ' + host_idle.line(idle[h])
                                                for h in sorted(idle))
        out.append(_alert('baseline_oversized', None, since, len(idle), baseline_hosts,
                          detail, hosts=sorted(idle), burst_candidates=candidates,
                          agents=agents, footprints=idle,
                          orchestrators=list(orchestrators)))
    return out


def alerts(cfg, db, listing, now, *, canons, readings, held_hosts=(), orchestrators=()):
    """Use the shared A2 evidence and existing lifecycle controls, without writes."""
    if not readings or cfg is None:
        return []
    from .host_lifecycle import staleness_s
    fresh = staleness_s(cfg.resource_interval)
    readings = {host: r for host, r in readings.items()
                if r.get('turns_in_progress') == 0
                and (r.get('cgroup') or {}).get('limits_readable') is True
                and resources.finite_float(r.get('sampled_ts')) is not None
                and 0 <= now - float(r['sampled_ts']) <= fresh}
    if not readings:
        return []
    controls = storage.of(db).host_lifecycle.listing()
    excluded = {row['host'] for row in controls if row.get('mode') != 'active'}
    readings = {h: r for h, r in readings.items()
                if h not in excluded and h not in held_hosts}
    if not readings:
        return []
    # The alert pass runs every 30 s: read the lots of the agents on the
    # candidate hosts only, not of the whole mesh (53 per-agent reads, about
    # 8 MB per pass on the real base). Members, lots and holds of these hosts
    # are the same; the full listing still names holders in project().
    members = [a for a in listing if a.get('host') in readings]
    footprints = host_idle.snapshot(cfg, db, canons, readings, now=now, listing=members)
    return project(footprints, listing, now=now, idle_after_s=cfg.idle_after_s,
                   baseline_hosts=cfg.baseline_hosts, held_hosts=held_hosts,
                   draining_hosts=excluded, orchestrators=orchestrators)

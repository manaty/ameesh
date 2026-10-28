# SPDX-License-Identifier: AGPL-3.0-only
"""#4150: read-only host work and reserved-capacity projection.

A quiet instant is not an idle interval. Published samples must cover the
interval without missing turns or freshness gaps; agent and lot activity can
only shorten that interval. This module never drains or changes a host.
"""
import time

from . import resources, storage
from .host_lifecycle import staleness_s

DONE = frozenset(('merged', 'promoted', 'closed'))


def project(reading, history, agents, lots, *, now, idle_after_s=1800,
            fresh_within=180, capacity='burst', cost_eur_h=None):
    """Pure projection. None inputs mean unreadable sources, never empty ones."""
    out = resources.footprint(reading)
    out.update(active_turns=None, leases=None, runs=0,
               runs_source='aucune interface de déclaration', state='busy',
               idle_since=None, capacity=capacity, cost_eur_h=cost_eur_h,
               idle_after_s=idle_after_s, unknown=[])
    unknown = out['unknown']
    reading = reading or {}
    ts = resources.finite_float(reading.get('sampled_ts'))
    if ts is None or not 0 <= now - ts <= fresh_within:
        unknown.append('host sample stale or unreadable')
    if any(out[key] is None for key in ('reserved_cpu', 'reserved_mem_bytes')):
        unknown.append('reserved capacity unreadable')
    if (reading.get('cgroup') or {}).get('limits_readable') is not True:
        unknown.append('executor cgroup unreadable')
    turns = reading.get('turns_in_progress')
    if turns is None:
        unknown.append('active turns unreadable')
    else:
        out['active_turns'] = turns
    if agents is None or lots is None:
        unknown.append('agent/lot activity unreadable')
        return out
    busy = bool(turns)
    since = 0.0
    leases = 0
    for agent in agents:
        required = ('lease_live', 'actionable_lots', 'unread', 'status_since_ts')
        if any(agent.get(key) is None for key in required):
            unknown.append('agent activity incomplete: %s' % agent.get('name'))
        # Runner leases live between turns. Only a lease with an open session
        # lot is a lot lease; a runner without a lot does not reserve work.
        lot_lease = (agent.get('lease_live') and agent.get('session_lot_state')
                     and agent['session_lot_state'] not in DONE)
        leases += bool(lot_lease)
        busy |= bool(lot_lease or agent.get('turn_started_ts')
                     or agent.get('status') in ('running', 'working')
                     or agent.get('actionable_lots') or agent.get('unread')
                     or agent.get('passive_unread') or agent.get('has_pending_prompt'))
        for key in ('status_since_ts', 'last_turn_ts', 'last_turn_recorded_ts'):
            value = resources.finite_float(agent.get(key))
            if value is not None:
                since = max(since, value)
    observed_turns = sum(bool(a.get('turn_started_ts') or
                              a.get('status') in ('running', 'working')) for a in agents)
    if turns is not None:
        out['active_turns'] = max(turns, observed_turns)
    out['leases'] = leases
    for lot in lots:
        updated = resources.finite_float(lot.get('updated_ts'))
        if updated is not None:
            since = max(since, updated)
        wake = resources.finite_float(lot.get('wake_ts'))
        if lot.get('state') not in DONE and wake is not None and wake <= now + idle_after_s:
            busy = True
    if busy or unknown:
        return out
    # Walk backwards only through the uninterrupted, quiet suffix. Do not
    # infer inactivity across a missing sample, even on a currently fresh host.
    cursor = now
    quiet_since = None
    for sample in sorted(history or [], key=lambda r: r.get('sampled_ts') or 0,
                         reverse=True):
        stamp = resources.finite_float(sample.get('sampled_ts'))
        if stamp is None or stamp > cursor:
            continue
        if (cursor - stamp > fresh_within or sample.get('turns_in_progress') != 0
                or (sample.get('cgroup') or {}).get('limits_readable') is not True):
            break
        quiet_since = stamp
        cursor = stamp
    if quiet_since is None:
        unknown.append('quiet sample history unavailable')
        return out
    out['idle_since'] = max(quiet_since, since)
    if now - out['idle_since'] >= idle_after_s:
        out['state'] = 'idle'
    return out


def capacities(canons, names, *, protected=(), baseline_hosts=2):
    """Explicit cards plus critical elected leaders/reserves when underspecified."""
    out, costs = {}, {}
    for name in names:
        cards = [host.fiche.data for canon in canons for host in canon.hosts
                 if host.title == name]
        out[name] = ('baseline' if any(c.get('capacity') == 'baseline' for c in cards)
                     else 'burst')
        values = [resources.finite_float(c.get('tarif_eur_h')) for c in cards]
        costs[name] = next((v for v in values if v is not None and v >= 0), None)
    if sum(v == 'baseline' for v in out.values()) < baseline_hosts:
        for name in protected:
            if name in out:
                out[name] = 'baseline'
    return out, costs


def snapshot(cfg, db, canons, readings, *, now=None):
    """Read all common sources once, then project each host. No mutations."""
    from . import election, host_lifecycle
    now = time.time() if now is None else now
    idle_s = max(1.0, float(cfg.idle_after_s))
    fresh = staleness_s(cfg.resource_interval)
    names = set(readings) | {h.title for c in canons for h in c.hosts}
    protected = set()
    agents = lots = None
    try:
        agents = storage.of(db).operations.listing()
        # Bounded per-agent read includes closed lots: closing work also
        # resets the quiet interval. Truncation cannot prove an idle host.
        # Lots only matter where the reading itself could still prove idle:
        # a host with turns, or without readable limits, stays busy whatever
        # its lots say (one read per agent there costs seconds per view).
        candidates = {host for host, r in readings.items()
                      if (r or {}).get('turns_in_progress') == 0
                      and ((r or {}).get('cgroup') or {}).get('limits_readable') is True}
        lots = {}
        for agent in agents:
            if agent.get('host') not in candidates:
                lots[agent['name']] = []
                continue
            rows = storage.of(db).work.items(state=None, assignee=agent['name'], limit=1000)
            lots[agent['name']] = rows if len(rows) < 1000 else None
        for name in host_lifecycle.critical_names(cfg, db):
            role = election.status(db, name)
            if role.get('holder'):
                protected.add(role['holder'])
            reserve = next((c['host'] for c in role['candidates']
                            if c.get('ready') and c['host'] != role.get('holder')), None)
            if reserve:
                protected.add(reserve)
    except Exception:  # unavailable inputs must prevent an idle verdict
        agents = None
    capacity, costs = capacities(canons, names, protected=protected,
                                 baseline_hosts=cfg.baseline_hosts)
    out = {}
    for host, reading in readings.items():
        members = ([a for a in agents if a.get('host') == host]
                   if agents is not None else None)
        host_lots = [] if members is not None else None
        for agent in members or []:
            rows = lots.get(agent['name'])
            if rows is None:
                host_lots = None
                break
            host_lots.extend(rows)
        try:
            history = storage.of(db).hosts.since(host, idle_s + fresh)
        except Exception:
            history = None
        out[host] = project(reading, history, members, host_lots, now=now,
                            idle_after_s=idle_s, fresh_within=fresh,
                            capacity=capacity[host], cost_eur_h=costs[host])
    return out


def line(value):
    """Shared text for hosts, show and doctor; missing reservations say unknown."""
    def fmt(key):
        v = value.get(key)
        return 'unknown' if v is None else '%g' % v
    return ('footprint  : %s (%s); reserved CPU %s, memory %s bytes; '
            'used CPU %s, memory %s bytes; turns %s; leases %s; runs %s (%s); '
            'cost %s EUR/h%s' % (
                value['state'], value['capacity'], fmt('reserved_cpu'),
                fmt('reserved_mem_bytes'), fmt('used_cpu'), fmt('used_mem_bytes'),
                fmt('active_turns'), fmt('leases'), fmt('runs'), value['runs_source'],
                fmt('cost_eur_h'), '; unknown: ' + ', '.join(value['unknown'])
                if value['unknown'] else ''))

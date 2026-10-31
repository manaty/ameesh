# SPDX-License-Identifier: AGPL-3.0-only
"""#4150: read-only host work and reserved-capacity projection.

A quiet instant is not an idle interval. Published samples must cover the
interval without missing turns or freshness gaps; agent and lot activity can
only shorten that interval. This module never drains or changes a host.
"""

from ameesh import work_lifecycle as lifecycle
import datetime
import time

from . import resources, storage
from .host_lifecycle import staleness_s

DONE = frozenset(lifecycle.COMPLETED)


def project(reading, history, agents, lots, *, now, idle_after_s=1800,
            fresh_within=180, capacity='burst', cost_eur_h=None, holds=None):
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
    from .backlog import _hold_blocks
    held_lots = {int(lot['id']) for lot in lots if lot.get('id') is not None
                 and _hold_blocks(lot, (holds or {}).get(int(lot['id'])), now)}
    future_wakes = {str(lot['id']) for lot in lots
                    if lot.get('id') is not None
                    and (resources.finite_float(lot.get('wake_ts')) or 0) > now}
    today = datetime.date.fromtimestamp(now).isoformat()
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
                     and agent['session_lot_state'] not in DONE
                     and str(agent.get('session_work_item')) not in
                     {str(ident) for ident in held_lots})
        lot_lease = lot_lease and str(agent.get('session_work_item')) not in future_wakes
        leases += bool(lot_lease)
        held_actions = sum(lot.get('assignee') == agent.get('name')
                           and lot.get('state') in lifecycle.ACTIVE
                           and str(lot.get('planned_start') or '')[:10] <= today
                           and lot.get('id') in held_lots
                           and (resources.finite_float(lot.get('wake_ts')) or 0) <= now
                           for lot in lots)
        actionable = max(0, int(agent.get('actionable_lots') or 0) - held_actions)
        busy |= bool(lot_lease or agent.get('turn_started_ts')
                     or agent.get('status') in ('running', 'working')
                     or actionable or agent.get('unread')
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
        if lot.get('state') not in DONE and wake is not None and wake < now + idle_after_s:
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


def apply_release(footprint, control):
    """#4148: overlay the host lifecycle release state on an A footprint.

    Read-only. `idle-drained` means claims are closed and the adapter may have
    released the resource; `draining` means the withdrawal is in progress and
    no proof of release exists yet. An `available` host keeps the projection's
    own busy/idle state. `runs` stays A's measurement (never the adapter).
    """
    out = dict(footprint)
    release = {
        'generation': control.get('generation'),
        'mode': control.get('mode'),
        'state': control.get('release_state') or 'busy',
        'hook_state': control.get('hook_state'),
        'claims_open': bool(control.get('claims_open', True)),
        'automatic': bool(control.get('automatic')),
        'requested_ts': control.get('requested_ts'),
        'released_ts': control.get('released_ts'),
        'error': control.get('release_error'),
        'error_state': control.get('release_error_state'),
        'error_ts': control.get('release_error_ts'),
    }
    mode = control.get('mode')
    if mode == 'lost':
        out['state'] = 'draining'
        out['unknown'] = list(out.get('unknown') or []) + ['host lost']
    elif mode == 'returning':
        # claims closed: still withdrawing; claims open: the host is busy
        # again (agents are coming back), never idle.
        out['state'] = 'busy' if release['claims_open'] else 'draining'
    elif mode == 'draining':
        out['state'] = ('idle-drained' if release['state'] == 'idle-drained'
                        else 'draining')
    out['release'] = release
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


def _can_prove_idle(reading, *, now, fresh_within):
    """The reading alone still leaves room for an idle verdict (#4178).

    Mirrors the unknown conditions of `project`: a sample that is stale,
    turns that are running or unreadable, limits that are unreadable, or a
    reservation that cannot be read already rules idleness out. Those hosts
    do not need their agents' full lot lists."""
    reading = reading or {}
    ts = resources.finite_float(reading.get('sampled_ts'))
    if ts is None or not 0 <= now - ts <= fresh_within:
        return False
    if reading.get('turns_in_progress') != 0:
        return False
    if (reading.get('cgroup') or {}).get('limits_readable') is not True:
        return False
    footprint = resources.footprint(reading)
    return (footprint['reserved_cpu'] is not None
            and footprint['reserved_mem_bytes'] is not None)


def _complete_canons(cfg, canons):
    """`canons` when it holds every configured canon, once each; else None.

    `load_configured` skips an unreadable canon and a duplicate id, where
    `from_config_all` refuses both: only an equal count proves the same list.
    """
    from . import canon as canon_mod
    try:
        entries = canon_mod.configured(cfg)
    except Exception:  # noqa: BLE001 - unreadable configuration: own read
        return None
    canons = list(canons or ())
    if not entries or len(canons) != len(entries):
        return None
    if len({getattr(c, 'id', None) for c in canons}) != len(canons):
        return None
    return canons


def snapshot(cfg, db, canons, readings, *, now=None, listing=None, controls=None):
    """Read all common sources once, then project each host. No mutations.

    `controls` (#4148): the host_lifecycle rows, mapping host to row, when the
    caller already read them. The release state then overlays the footprint
    (draining/idle-drained, declared runs); without them the projection stays
    the read-only A view.
    """
    from . import canon as canon_mod
    from . import planned_waits
    now = time.time() if now is None else now
    idle_s = max(1.0, float(cfg.idle_after_s))
    fresh = staleness_s(cfg.resource_interval)
    names = set(readings) | {h.title for c in canons for h in c.hosts}
    protected = set()
    agents = lots = None
    holds = {}
    candidates = {host for host, reading in readings.items()
                  if _can_prove_idle(reading, now=now, fresh_within=fresh)}
    try:
        agents = storage.of(db).operations.listing() if listing is None else listing
        # Bounded per-agent read includes closed lots: closing work also
        # resets the quiet interval. Truncation cannot prove an idle host.
        # A host whose own reading already excludes rest only needs the
        # session lot, for the lease count and the hold filter: its full
        # per-agent lists cost seconds per view and cannot change the verdict
        # (#4178, taking back 9b11d526b after its revert b207dbce5).
        lots = {}
        for agent in agents:
            if agent.get('host') not in candidates:
                lots[agent['name']] = []
                continue
            rows = storage.of(db).work.items(state=None, assignee=agent['name'], limit=1000)
            lots[agent['name']] = rows if len(rows) < 1000 else None
        by_id = {int(lot['id']): lot for rows in lots.values() if rows is not None
                 for lot in rows}
        for agent in agents:
            # Only the hosts of `readings` are projected: the session lot of
            # an agent elsewhere feeds neither a lease count nor a hold filter
            # of this view (one read per agent of the mesh, #4377).
            if agent.get('host') not in readings:
                continue
            ident = str(agent.get('session_work_item') or '')
            if ident.isdigit() and int(ident) not in by_id:
                row = storage.of(db).work.get(int(ident))
                if row is not None:
                    by_id[int(ident)] = row
        ids = {ident for ident, lot in by_id.items() if lot.get('state') not in DONE}
        if ids:
            events = storage.of(db).progress.lot_hold_events(
                sorted(ids), planned_waits.HOLD_NOTE_PATTERNS)
            # The canons were already read for this view: without seeding the
            # cache, `pilot_holds` reloads them once per hold author (14 reads,
            # ~6 s on the live mesh), which dominated the same projection.
            with canon_mod.configured_cache(cfg, canons):
                holds = planned_waits.pilot_holds(db, events, cfg=cfg, strict=True)
    except Exception:  # unavailable inputs must prevent an idle verdict
        agents = None
    capacity, costs = capacities(canons, names, protected=protected,
                                 baseline_hosts=cfg.baseline_hosts)
    # Ordinary views need elections only for an underspecified baseline
    # (#4178). The A3 alert projection also needs guards only while
    # baseline_oversized is possible: more hosts than the baseline can still
    # prove idle. Below that, no reclassification proposal can name a
    # candidate and the election would be discarded (seconds per 30 s pass).
    underspecified = (sum(v == 'baseline' for v in capacity.values())
                      < cfg.baseline_hosts)
    oversized_possible = (listing is not None
                          and len(candidates) > cfg.baseline_hosts)
    if agents is not None and (underspecified or oversized_possible):
        from . import election, host_lifecycle
        try:
            # #4377: the canons of this view, when every configured canon is
            # among them, are the list `critical_names` would load again
            # (`from_config_all`, 1 to 2.4 s); a partial list keeps its own
            # all-or-nothing read.
            complete = _complete_canons(cfg, canons)
            for name in host_lifecycle.critical_names(cfg, db, canons=complete):
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
        if host_lots is not None:
            present = {lot['id'] for lot in host_lots}
            for agent in members or []:
                ident = str(agent.get('session_work_item') or '')
                if ident.isdigit() and int(ident) in by_id and int(ident) not in present:
                    host_lots.append(by_id[int(ident)])
                    present.add(int(ident))
        # History can only shorten a quiet interval: a reading that already
        # excludes rest does not need the samples read (#4178).
        history = None
        if host in candidates:
            try:
                history = storage.of(db).hosts.since(host, idle_s + fresh)
            except Exception:
                history = None
        out[host] = project(reading, history, members, host_lots, now=now,
                            idle_after_s=idle_s, fresh_within=fresh,
                            capacity=capacity[host], cost_eur_h=costs[host], holds=holds)
        out[host]["baseline_guard"] = host in protected
        control = (controls or {}).get(host) if isinstance(controls, dict) else None
        if control is not None:
            out[host] = apply_release(out[host], control)
    return out


def line(value):
    """Shared text for hosts, show and doctor; missing reservations say unknown."""
    def fmt(key):
        v = value.get(key)
        return 'unknown' if v is None else '%g' % v
    release = value.get('release') or {}
    detail = ''
    if release:
        detail = ('; release %s (hook %s), claims %s%s%s' % (
            release.get('state') or '?', release.get('hook_state') or 'none',
            'open' if release.get('claims_open') else 'closed',
            ', automatic' if release.get('automatic') else '',
            ', error: ' + str(release.get('error'))[:120]
            if release.get('error') else ''))
    return ('footprint  : %s (%s); reserved CPU %s, memory %s bytes; '
            'used CPU %s, memory %s bytes; turns %s; leases %s; runs %s (%s); '
            'cost %s EUR/h%s%s' % (
                value['state'], value['capacity'], fmt('reserved_cpu'),
                fmt('reserved_mem_bytes'), fmt('used_cpu'), fmt('used_mem_bytes'),
                fmt('active_turns'), fmt('leases'), fmt('runs'), value['runs_source'],
                fmt('cost_eur_h'), detail,
                '; unknown: ' + ', '.join(value['unknown'])
                if value['unknown'] else ''))

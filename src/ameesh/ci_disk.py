# SPDX-License-Identifier: AGPL-3.0-only
"""Mesures CI : seul le workflow dédié sur la base résout une alerte.

#5751, décision mesh-design du 04/11/2026 : aucune connexion SSH. Le
veilleur compense les tics cron manqués par workflow_dispatch après 60 min.
Les signaux des autres runs ne peuvent que lever ou aggraver une alerte.
"""
from __future__ import annotations

import json
import math
import re
from urllib.parse import quote

from . import resources

WORKFLOW = 'ci-disque.yml'
SCHEMA = 'ameesh-ci-disk/1'
MAX_AGE = 2 * 3600
READ_INTERVAL = 300
#: #5633 : une VM qui n'apparaît plus dans les jobs du workflow de base depuis
#: ce délai est sortie du périmètre (matrice/labels modifiés) : sa ligne est
#: oubliée, ses alertes avec. Un simple échec de lecture ne passe pas par
#: `collect`, donc n'oublie rien ; `expect_disk` repousse `seen_at` à chaque
#: job vu, même sans mesure.
FORGET_AGE = 24 * 3600
HOST = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
#: #5754 : volumes vérifiés par la garde, vocabulaire fermé (même liste que
#: `scripts/ci-test-space.py`). Le courrier nomme le volume sans recopier un
#: chemin du code testé.
VOLUMES = ('root', 'runner_temp', 'cwd', 'tmp')
VOLUME_LABELS = {'root': 'racine (/)', 'runner_temp': 'RUNNER_TEMP',
                 'cwd': 'répertoire courant (cwd)', 'tmp': 'temporaires (TMPDIR)'}
#: #5754 : une ligne illisible ne peut pas recevoir de courrier ; elle est
#: acquittée sur place. Ce plafond borne le balayage d'un passage quand des
#: lignes illisibles occupent la fenêtre de 100 de la boîte durable.
REFUSAL_SCAN = 500
REFUSAL_WINDOW = 100


def annotation(row, host):
    if row.get('title') != SCHEMA:
        return None
    try:
        data = json.loads(row.get('message') or '')
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('schema') != SCHEMA or data.get('host') != host:
        return None
    free, total = data.get('free_bytes'), data.get('total_bytes')
    if (data.get('volume') != '/' or type(free) is not int or type(total) is not int
            or not 0 <= free <= total or not 0 < total < 2**63):
        return None
    return dict(host=host, volume='/', free_bytes=free, total_bytes=total)


def collect(api, repo, branch, store):
    """Au plus 5 runs / 100 jobs / 100 annotations par job, sur la base seule.

    Un job en attente est enregistré sans mesure. Une mesure absente ou
    illisible ne remplace jamais une mesure acquise par un faux vert.

    #5633 : les noms de jobs des deux runs lus sont le périmètre courant (la
    matrice du workflow) ; une VM absente depuis FORGET_AGE en est oubliée.
    Aucun ensemble de noms vu (lecture vide ou en échec) n'oublie rien, et un
    échec de lecture lève avant l'oubli : les dernières mesures restent.
    """
    from .ci import _iso_ts
    runs = api('repos/%s/actions/workflows/%s/runs?branch=%s&per_page=5'
               % (repo, WORKFLOW, quote(branch, safe=''))) or {}
    candidates = [r for r in runs.get('workflow_runs', ())
                  if r.get('head_branch') == branch
                  and r.get('event') in ('schedule', 'workflow_dispatch')]
    hosts_vus: set[str] = set()
    # Lire les deux plus récentes : le nouveau run en file ne masque pas
    # les mesures acquises par le précédent.
    for run in sorted(candidates, key=lambda r: int(r['id']), reverse=True)[:2]:
        jobs = api('repos/%s/actions/runs/%s/jobs?per_page=100' % (repo, run['id'])) or {}
        for job in jobs.get('jobs', ()):
            name = str(job.get('name') or '')
            host = name.removeprefix('disque ')
            if not name.startswith('disque ') or not HOST.fullmatch(host):
                continue
            hosts_vus.add(host)
            store.expect_disk(repo, host=host, volume='/')
            sampled = _iso_ts(job.get('completed_at'))
            if job.get('conclusion') != 'success' or sampled is None:
                continue
            # Checks API : identifiant du check lié au job, jamais une URL
            # arbitraire fournie par le contenu de l'annotation.
            match = re.fullmatch(r'https://api.github.com/repos/%s/check-runs/(\d+)'
                                 % re.escape(repo), str(job.get('check_run_url') or ''))
            if not match:
                continue
            rows = api('repos/%s/check-runs/%s/annotations?per_page=100'
                       % (repo, match[1])) or []
            for row in rows:
                data = annotation(row, host)
                if data is not None:
                    document = json.loads(row['message'])
                    physical = document.get('sampled_ts', sampled)
                    if timestamp(physical, sampled):
                        store.observe_disk(repo, run_id=int(run['id']), sampled_ts=physical, **data)
    if hosts_vus:
        store.forget_disks(repo, hosts_vus, older_than_s=FORGET_AGE)
    return any(r.get('status') in ('queued', 'in_progress', 'waiting', 'pending')
               for r in candidates)


DISPATCH_AGE = 60 * 60  # +45 min de part en file +5 min de sonde < MAX_AGE


def needs_dispatch(rows, now):
    return not rows or any(row.get('sampled_ts') is None
                           or now - row['sampled_ts'] >= DISPATCH_AGE for row in rows)


def findings(rows, canons, now, signals=()):
    """Seuils physiques des Hosts ; une mesure saine seule résout le rouge.

    #5633 : une VM tout juste vue par ``expect_disk`` n'a pas encore de
    première mesure ; MAX_AGE de grâce après sa **première** apparition
    (``first_seen_ts``, stable) évite l'alerte de la première passe. La grâce
    ne se renouvelle pas à chaque collecte : une VM qui apparaît dans les jobs
    sans jamais produire de mesure alerte après MAX_AGE. Une mesure déjà
    acquise puis devenue vieille, elle, alerte toujours (la sonde ne remonte
    plus).
    """
    out = []
    trusted = {(r['repo'], r['host']): r.get('sampled_ts') for r in rows}
    raised = []
    for signal in signals:
        # #5754 : une ligne illisible, déjà acquittée sans courrier, ne doit
        # pas faire échouer le relevé d'alertes ; sans date, elle ne lève rien.
        if not isinstance(signal, dict) or not timestamp(signal.get('sampled_ts'), now):
            continue
        measured = trusted.get((signal['repo'], signal['host']))
        if measured is None or measured < signal['sampled_ts']:
            raised.append(dict(signal, signal_only=True, volume='/'))
    for row in list(rows) + raised:
        host, repo = row['host'], row['repo']
        sampled = row.get('sampled_ts')
        first = row.get('first_seen_ts')
        grace = sampled is None and first is not None and now - first <= MAX_AGE
        stale = not grace and (sampled is None or now - sampled > MAX_AGE
                               or sampled > now + 300)
        common = dict(agent=None, since=sampled, host=host, repo=repo,
                      reason='%s:%s:%s' % (repo, host, row['volume']), run_id=row.get('run_id'))
        if stale and not row.get('signal_only'):
            out.append(dict(common, type='ci_disk_unreadable', value=None, threshold=MAX_AGE,
                            detail='CI disk %s (%s): no fresh disk measurement; inspect native retention timer, '
                                   'machine label and CI watcher' % (host, repo)))
        limits = resources.host_limits(canons, host)['limits']
        if row.get('refusal'):
            out.append(dict(common, type='ci_disk_low', value=row.get('free_bytes'),
                            threshold=5 * 1024**3, critical=True, urgent=not row.get('fork'),
                            detail='CI blocked: disk margin refusal on %s (%s); '
                                   'raise-only signal, dedicated base probe required' % (host, repo)))
            continue
        reading = {'volumes': [{'path': row['volume'], 'disk_free_bytes': row.get('free_bytes'),
                                'disk_total_bytes': row.get('total_bytes')}]}
        warnings = resources.disk_warnings(reading, limits)
        if warnings:
            warning = warnings[0]
            by_free = 'free' in warning['reasons']
            out.append(dict(common, type='ci_disk_low',
                            value=row['free_bytes'] if by_free else warning['used'],
                            threshold=limits['warn_disk_free'] if by_free else limits['warn_disk_used'],
                            critical=warning['critical'],
                            urgent=warning['critical'] and not row.get('fork'),
                            detail='CI disk %s (%s) %s: %s bytes free, %.1f%% used%s; '
                                   'check host retention timer (bureau)%s' % (
                                       host, repo, row['volume'], row['free_bytes'],
                                       100 * warnings[0]['used'],
                                       ' (last known low measurement, stale)' if stale else '',
                                       ' (raise-only CI signal)' if row.get('signal_only') else '')))
    unique = {}
    for finding in out:
        key = (finding['repo'], finding['host'], finding['type'])
        rank = (bool(finding.get('critical')), bool(finding.get('urgent')))
        if key not in unique or rank > (bool(unique[key].get('critical')),
                                        bool(unique[key].get('urgent'))):
            unique[key] = finding
    return list(unique.values())


def timestamp(value, now):
    return (type(value) in (int, float) and 0 < value <= now + 300
            and math.isfinite(value))


def volume(data):
    """#5754 : volume vérifié, projection fermée ; None si absent ou inconnu."""
    if not isinstance(data, dict):
        return None
    value = data.get('volume')
    return value if value in VOLUMES else None


def refusal_event(refusal):
    """#5754 : données d'une ligne de refus lisible, None sinon.

    Une ligne dont l'identifiant, le run ou la tentative ne sont pas les types
    attendus ne peut ni être nommée dans un courrier ni être acquittée par un
    identifiant stable : le veilleur la consomme sans courrier plutôt que de
    la laisser retenir la fenêtre des 100 (N7).
    """
    if not isinstance(refusal, dict):
        return None
    data = refusal.get('payload')
    if not isinstance(data, dict):
        return None
    if not re.fullmatch(r"[0-9a-f]{32}", str(refusal.get('event_id', ''))):
        return None
    if (type(data.get('run_id')) is not int or not 0 < data['run_id'] < 2**64
            or type(data.get('attempt')) is not int or not 0 < data['attempt'] < 2**32):
        return None
    return data


def observe_refusal(repo, host, refusal, store, *, now, fork=False):
    if (not isinstance(refusal, dict) or refusal.get("repo") != repo
            or refusal.get("schema") != "ameesh-ci-disk-refusal/1"
            or not re.fullmatch(r"[0-9a-f]{32}", str(refusal.get("id", "")))
            or not timestamp(refusal.get("sampled_ts"), now)
            or now - refusal["sampled_ts"] > 7 * 86400):
        return
    run_id = str(refusal.get("run_id", ""))
    attempt = str(refusal.get("attempt", ""))
    if not run_id.isdecimal() or not attempt.isdecimal() or len(run_id) > 20 or len(attempt) > 10:
        return
    if not 0 < int(run_id) < 2**64 or not 0 < int(attempt) < 2**32:
        return
    payload = numeric_metrics(refusal)
    payload.update(run_id=int(run_id), attempt=int(attempt), sampled_ts=refusal["sampled_ts"],
                   volume=volume(refusal))
    if fork:
        payload["fork"] = True  # #5755 : lève sans urgence ni courrier
    store.observe_disk_refusal(repo, host=host, event_id=refusal["id"], payload=payload)


def numeric_metrics(data):
    """Projection fermée : les données du code testé ne sont jamais du texte
    d'instruction pour le pilote, pas même dans la charge du courrier."""
    return {key: value if type(value := data.get(key)) is int and 0 <= value < 2**63 else None
            for key in ("free_bytes", "available_inodes", "required_bytes")}

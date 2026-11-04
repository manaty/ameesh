# SPDX-License-Identifier: AGPL-3.0-only
"""Mesures physiques CI : relevé natif SSH (#5751), annotations de base (#5630).

Le périmètre natif configuré est collecté avant les appels GitHub. La date
vient de la sonde sur la VM, jamais de l'heure de lecture ; un timer arrêté
reste donc visible. Les anciens dépôts non migrés gardent leur transport
GitHub. Aucun accès SQL ni secret du mesh sur une VM de tests.
"""
from __future__ import annotations

import json
import math
import subprocess
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
                    store.observe_disk(repo, run_id=int(run['id']), sampled_ts=sampled, **data)
    if hosts_vus:
        store.forget_disks(repo, hosts_vus, older_than_s=FORGET_AGE)


def findings(rows, canons, now):
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
    for row in rows:
        host, repo = row['host'], row['repo']
        sampled = row.get('sampled_ts')
        first = row.get('first_seen_ts')
        grace = sampled is None and first is not None and now - first <= MAX_AGE
        stale = not grace and (sampled is None or now - sampled > MAX_AGE
                               or sampled > now + 300)
        common = dict(agent=None, since=sampled, host=host, repo=repo,
                      reason='%s:%s:%s' % (repo, host, row['volume']), run_id=row.get('run_id'))
        if stale:
            out.append(dict(common, type='ci_disk_unreadable', value=None, threshold=MAX_AGE,
                            detail='CI disk %s (%s): no fresh disk measurement; inspect native retention timer, '
                                   'machine label and CI watcher' % (host, repo)))
        limits = resources.host_limits(canons, host)['limits']
        reading = {'volumes': [{'path': row['volume'], 'disk_free_bytes': row.get('free_bytes'),
                                'disk_total_bytes': row.get('total_bytes')}]}
        warnings = resources.disk_warnings(reading, limits)
        if warnings:
            warning = warnings[0]
            by_free = 'free' in warning['reasons']
            out.append(dict(common, type='ci_disk_low',
                            value=row['free_bytes'] if by_free else warning['used'],
                            threshold=limits['warn_disk_free'] if by_free else limits['warn_disk_used'],
                            critical=warning['critical'], urgent=warning['critical'],
                            detail='CI disk %s (%s) %s: %s bytes free, %.1f%% used%s; '
                                   'check host retention timer (bureau)' % (
                                       host, repo, row['volume'], row['free_bytes'],
                                       100 * warnings[0]['used'],
                                       ' (last known low measurement, stale)' if stale else '')))
    return out


# #5751 : destinations de confiance configurées sur l'hôte du veilleur,
# jamais tirées d'une PR ou d'une annotation. Commande distante fixe.
SSH_TARGET = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]{0,252}\Z")
NATIVE_COMMAND = "/usr/local/lib/ameesh-ci/ci-disque.py natif"


def native_settings(raw, repos):
    if not isinstance(raw, dict):
        raise ValueError("object {repo: {host: SSH destination}} expected")
    out = {}
    count = 0
    for repo, hosts in raw.items():
        if repo not in repos or not isinstance(hosts, dict) or not hosts:
            raise ValueError("declared repository and non-empty host map required")
        for host, target in hosts.items():
            if (not isinstance(host, str) or not HOST.fullmatch(host)
                    or not isinstance(target, str) or not SSH_TARGET.fullmatch(target)):
                raise ValueError("invalid host or SSH destination")
            count += 1
        out[repo] = dict(hosts)
    if count > 32:
        raise ValueError("at most 32 native hosts per watcher")
    return out


def read_native(target):
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                             "-o", "StrictHostKeyChecking=yes", "--", target, NATIVE_COMMAND],
                            capture_output=True, text=True, timeout=15, check=True)
    if len(result.stdout) > 16 * 1024 * 1024:
        raise ValueError("native payload too large")
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError("native object expected")
    return data


def timestamp(value, now):
    return (type(value) in (int, float) and math.isfinite(value)
            and 0 < value <= now + 300)


def collect_native(repo, hosts, store, *, now, read=None, log=lambda _: None):
    """Une panne SSH garde la dernière mesure et son âge réel.

    Aucun fallback GitHub dans le périmètre natif : il masquerait un timer
    cassé. Le dépôt et l'hôte viennent de la configuration authentifiée SSH.
    """
    read = read or read_native
    for host, target in hosts.items():
        store.expect_disk(repo, host=host, volume='/')
        try:
            document = read(target)
            snapshot = document.get("snapshot")
            if isinstance(snapshot, dict) and timestamp(snapshot.get("sampled_ts"), now):
                data = annotation({"title": SCHEMA, "message": json.dumps(snapshot)}, host)
                if data is not None:
                    store.observe_disk(repo, run_id=None, sampled_ts=snapshot["sampled_ts"], **data)
            for refusal in document.get("refusals", ())[:10000]:
                observe_refusal(repo, host, refusal, store, now=now)
        except Exception as exc:  # une VM n'arrête ni les autres ni leur alerte
            log("CI %s: native disk %s unreadable (%s)" % (repo, host, type(exc).__name__))
    store.forget_disks(repo, hosts, older_than_s=FORGET_AGE)


def observe_refusal(repo, host, refusal, store, *, now):
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
    payload = {k: str(refusal.get(k, ""))[:1000] for k in ("job", "runner", "reason")}
    payload.update(run_id=int(run_id), attempt=int(attempt), sampled_ts=refusal["sampled_ts"])
    store.observe_disk_refusal(repo, host=host, event_id=refusal["id"], payload=payload)

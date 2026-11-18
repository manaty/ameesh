# SPDX-License-Identifier: AGPL-3.0-only
"""#7477: durable, exact-head packet composition receipts.

The Markdown is retained as a compatibility rendering for existing parsers;
structured units and provenance are stored alongside it. A present but invalid
receipt is an error, never permission to read another head or an inherited file.
"""
from __future__ import annotations

import re
from . import plan_git, storage

#: #4304/#7740: the line a packet manifest carries when its per-unit direct
#: tests run before the push, on each unit's composed prefix. Both wordings
#: are read: already-pushed #4304 manifests said "in parallel with the full
#: CI". It must not read as ``pending``: that would withdraw the composer
#: exemption from every newly composed packet.
PARALLEL_TESTS_RE = re.compile(
    r"^- Tests locaux : (?:joués par le composeur sur le préfixe composé de "
    r"chaque unité, avant la poussée \(#7740\)|joués par le composeur après "
    r"la poussée, en parallèle de la CI complète \(#4304\))", re.MULTILINE)


class SnapshotError(RuntimeError):
    pass


def target_name(value: str) -> str:
    value = str(value or '')
    for prefix in ('refs/remotes/origin/', 'refs/heads/', 'origin/'):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def repository_key(value: str) -> str:
    """GitHub is case-insensitive: one spelling for writes and reads."""
    from .migrations import repo_key
    return repo_key(value)


def repository_name(checkout: str) -> str:
    if not checkout:
        return ''
    try:
        return repository_key(plan_git.github_slug(str(checkout)) or '')
    except plan_git.GitError:
        return ''


def payload(db, units: list[dict], manifest: str) -> dict:
    """Capture immutable admissions, exclusions and review references.

    Tests announced for later execution carry no success proof. Completed
    results must explicitly name their count and outcome in this head.
    """
    from . import packet_parallel, release_cutoff
    sections = {}
    heading = ''
    for line in manifest.splitlines():
        if line.startswith('## '):
            heading = line[3:].strip()
            sections[heading] = []
        elif heading:
            sections[heading].append(line)
    reviews = []
    for unit in units:
        for mark in storage.of(db).work.milestones(unit['work_item_id'], limit=None):
            if mark['kind'] == 'verdict' and mark.get('sha') == unit.get('frozen_sha'):
                reviews.append({key: mark.get(key) for key in
                    ('id', 'work_item_id', 'sha', 'actor', 'verdict', 'at_ts')})
    tests = {'status': 'pending'}
    match = re.search(r'^- Tests locaux : .*?(\d+) tests exécutés, (\d+) échec\(s\)',
                      manifest, re.MULTILINE)
    if match:
        tests = {'status': 'success' if int(match[2]) == 0 and int(match[1]) > 0
                 else 'failed' if int(match[2]) else 'inconclusive',
                 'count': int(match[1]), 'failures': int(match[2])}
    elif PARALLEL_TESTS_RE.search(manifest):
        tests = {'status': 'parallel'}
    return {'version': 1, 'units': units, 'manifest': manifest,
            'admissions': packet_parallel.manifest_units(manifest),
            'excluded': [{'work_item_id': lot, 'reason': reason} for lot, reason
                         in release_cutoff.parse_manifest(manifest)['excluded']],
            'reviews': reviews, 'tests': tests,
            'local_checks': {key: '\n'.join(value) for key, value in sections.items()
                             if 'contrôle' in key.lower() or 'test' in key.lower()}}


def validate(row: dict) -> dict:
    """Refuse a stored receipt whose shape is not an exact composition.

    Callers that already hold rows (a single query for a whole cutoff) use
    this rather than re-querying each head through :func:`exact`.
    """
    payload = row.get('payload')
    if (not isinstance(payload, dict) or payload.get('version') != 1
            or not isinstance(payload.get('units'), list)
            or not isinstance(payload.get('manifest'), str)
            or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', row.get('base_sha', ''))):
        raise SnapshotError('invalid packet snapshot for exact head '
                            + str(row.get('head_sha') or '?'))
    units = payload['units']
    ids = []
    for unit in units:
        if (not isinstance(unit, dict) or not isinstance(unit.get('work_item_id'), int)
                or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',
                                    unit.get('unit_sha') or '')):
            raise SnapshotError('invalid packet snapshot unit')
        for name in ('source_sha', 'frozen_sha'):
            if unit.get(name) and not re.fullmatch(r'[0-9a-f]{7,64}', unit[name]):
                raise SnapshotError('invalid packet snapshot ' + name)
        ids.append(unit['work_item_id'])
    if len(ids) != len(set(ids)):
        raise SnapshotError('duplicate packet snapshot unit')
    return row


def exact(db, *, repository: str, head: str, branch: str,
          target: str | None = None, package_id: int | None = None) -> dict | None:
    """Only an exact full SHA can select a receipt; absence permits Git fallback."""
    repository = repository_key(repository)
    if not repository or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', head or ''):
        raise SnapshotError('packet snapshot requires repository and exact head SHA')
    rows = storage.of(db).work.packet_snapshots(
        repository=repository, target=target_name(target) if target else None,
        package_id=package_id, branch=branch, head_sha=head)
    if not rows:
        return None
    if len(rows) != 1:
        raise SnapshotError('ambiguous packet snapshot: specify target and carrier')
    return validate(rows[0])


def lookup(*, checkout: str = '', repository: str = '', head: str,
           branch: str, target: str | None = None, package_id: int | None = None,
           db=None) -> dict | None:
    """Read a receipt using the caller's connection, or the configured reader.

    A missing receipt permits historical Git fallback. Connection or query
    failures are not evidence of receipt absence and remain explicit.
    """
    repository = repository or repository_name(checkout)
    if not repository:
        return None
    owned = None
    if db is None:
        from . import config, db as database
        try:
            owned = database.connect(config.load(register_credentials=False), writer=False)
        except database.Unavailable as exc:
            raise SnapshotError('packet snapshot database unavailable') from exc
        db = owned
    try:
        return exact(db, repository=repository, head=head, branch=branch,
                     target=target, package_id=package_id)
    finally:
        if owned is not None:
            owned.close()


def text(*, fallback, **kwargs) -> str | None:
    """Prefer the receipt; call the historical Git reader only on absence."""
    receipt = lookup(**kwargs)
    return receipt['payload']['manifest'] if receipt else fallback()

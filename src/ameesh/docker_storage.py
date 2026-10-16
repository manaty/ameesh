# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only Docker storage measurements, sampled by housekeeping (#411).

No prune/remove operations. No recursive filesystem scan or system df:
volume enumeration and BuildKit metadata each have a bounded CLI deadline.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

from .containers import parse_labels
from .resources import parse_bytes


DEFAULT_ORPHAN_VOLUMES = 100
DEFAULT_BUILD_CACHE = 20 * 1024 ** 3
MAX_AGE_S = 1800


def _run(runtime, args):
    try:
        return runtime.run([runtime.binary, *args], capture_output=True, text=True,
                           timeout=min(10, max(0.5, runtime.timeout)))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def _rows(runtime, args):
    result = _run(runtime, args)
    if result is None or result.returncode:
        return None
    try:
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if not all(isinstance(row, dict) for row in rows):
            return None
        return rows
    except (ValueError, TypeError):
        return None


def _buildx_available(runtime):
    """Whether the docker CLI exposes the buildx plugin (client-side probe)."""
    result = _run(runtime, ['buildx', 'version'])
    return result is not None and result.returncode == 0


def collect(runtime, *, now=None) -> dict | None:
    """Count dangling volumes (no container reference, including stopped).

    Names and descriptions are deliberately absent from the persisted
    summary. An error is unknown, never a zero measurement. Build cache is
    for the default Docker builder; shared records are not reclaimable disk.
    A runtime without the buildx plugin leaves the cache unmeasured and
    flagged unsupported, which is not an error: the counter stays unknown.
    """
    if (runtime is None or os.path.basename(getattr(runtime, 'binary', '')) != 'docker'
            or not hasattr(runtime, 'run') or not hasattr(runtime, 'timeout')):
        return None
    out = {'sampled_ts': time.time() if now is None else now, 'builder': 'default',
           'orphan_volumes': None, 'anonymous_orphan_volumes': None,
           'named_orphan_volumes': None, 'build_cache_bytes': None,
           'build_cache_reclaimable_bytes': None, 'build_cache_unsupported': False,
           'errors': []}
    volumes = _rows(runtime, ['volume', 'ls', '--filter', 'dangling=true',
                              '--format', '{{json .}}'])
    if volumes is None or any(not isinstance(row.get('Name'), str) for row in volumes):
        out['errors'].append('volume inventory unavailable')
    else:
        anonymous = sum('com.docker.volume.anonymous' in parse_labels(row.get('Labels'))
                        for row in volumes)
        out.update(orphan_volumes=len(volumes), anonymous_orphan_volumes=anonymous,
                   named_orphan_volumes=len(volumes) - anonymous)
    cache = _rows(runtime, ['buildx', 'du', '--builder', 'default', '--format=json'])
    if cache is None:
        # Debian docker.io ships no buildx plugin (agents-1/2): the cache is
        # not measurable here, a durable fact rather than a failed measure.
        if _buildx_available(runtime):
            out['errors'].append('default builder cache inventory unavailable')
        else:
            out['build_cache_unsupported'] = True
    else:
        try:
            total = reclaimable = 0
            for row in cache:
                size = parse_bytes(row['Size'])
                if size is None:
                    raise ValueError('invalid cache size')
                if type(row['Reclaimable']) is not bool or type(row['Shared']) is not bool:
                    raise ValueError('invalid cache flags')
                total += size
                if row['Reclaimable'] and not row['Shared']:
                    reclaimable += size
            out.update(build_cache_bytes=total, build_cache_reclaimable_bytes=reclaimable)
        except (KeyError, ValueError, TypeError):
            out['errors'].append('default builder cache inventory malformed')
    return out


def findings(data: dict, policy, host: str, now: float) -> list[dict]:
    """Findings from the last persisted measurement, never run Docker here."""
    since = data.get('sampled_ts')
    if not isinstance(since, (int, float)):
        return []
    common = {'host': host, 'agent': None, 'since': None, 'sampled_ts': since}
    if now - since > MAX_AGE_S:
        return [dict(common, type='docker_storage_unavailable', value=None, threshold=MAX_AGE_S,
                     message='Docker storage inventory on %s is stale; run ameesh menage' % host)]
    out = []
    for key, threshold, kind, subject in (
        ('orphan_volumes', policy.docker_orphan_volumes_alert, 'docker_orphan_volumes', 'orphan volumes'),
        ('build_cache_reclaimable_bytes', policy.docker_build_cache_alert,
         'docker_build_cache', 'private reclaimable build cache bytes')):
        value = data.get(key)
        if isinstance(value, (int, float)) and value >= threshold:
            out.append(dict(common, type=kind, value=value, threshold=threshold,
                            message='Docker on %s: %s %s (threshold %s); inspect with '
                                    'ameesh menage and request owner cleanup approval' %
                                    (host, value, subject, threshold)))
    if data.get('errors'):
        out.append(dict(common, type='docker_storage_unavailable', value=None, threshold=0,
                        message='Docker storage on %s: %s' % (host, '; '.join(data['errors']))))
    return out

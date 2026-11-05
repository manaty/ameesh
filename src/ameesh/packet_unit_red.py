# SPDX-License-Identifier: AGPL-3.0-only
"""#5971 : mémoire bornée des sources retirées pour tests par unité rouges.

Une note sur le lot survit au porteur et au clone du composeur. La première
note d'une source/cible donne au plus une heure d'exclusion ; les retraits
répétés ne renouvellent ni cette borne ni la notification. Une source neuve
ou une nouvelle tête de la cible est admise immédiatement. Les non-concluants ne passent pas par ce chemin :
paquet-auto ne les retire pas (§3). La borne protège aussi contre un rouge
initialement attribué au code qui se révèle ensuite instable ou lié au banc.
"""
from __future__ import annotations

import datetime
import json
import re
import time

PREFIX = 'packet-unit-red: '
PATTERN = '^' + re.escape(PREFIX)
EXCLUSION_S = 3600


def read_note(note):
    if not isinstance(note, str) or not note.startswith(PREFIX):
        return None
    try:
        value = json.loads(note[len(PREFIX):])
    except ValueError:
        return None
    if not isinstance(value, dict) or not re.fullmatch('[0-9a-f]{40}', str(value.get('source', ''))):
        return None
    if not isinstance(value.get('target'), str) or not value['target']:
        return None
    if not isinstance(value.get('tests'), list) or not all(isinstance(t, str) for t in value['tests']):
        return None
    if 'base' in value and (not isinstance(value['base'], str)
                            or not re.fullmatch('[0-9a-f]{40}', value['base'])):
        return None
    return value


def note(source, target, tests, *, base=None):
    value = dict(source=source, target=target, tests=list(tests))
    if base is not None:
        if not isinstance(base, str) or not re.fullmatch('[0-9a-f]{40}', base):
            raise ValueError('invalid unit-red target base')
        value['base'] = base
    text = PREFIX + json.dumps(value, sort_keys=True)
    if read_note(text) is None:
        raise ValueError('invalid unit-red source, target or tests')
    return text


def apply(candidates, events, *, now=None, base=None):
    """Retire les sources identiques ; aucune écriture ni consommation au tic."""
    now = time.time() if now is None else now
    first = {}
    for event in events:
        value = read_note(event.get('note'))
        stamp = event.get('created_ts')
        if value is None or not isinstance(stamp, (int, float)):
            continue
        key = (int(event['work_item_id']), value['source'], value['target'])
        if key not in first or stamp < first[key][0]:
            first[key] = (stamp, value)
    for entry in candidates:
        prior = first.get((entry['work_item'], entry.get('source'), entry.get('target')))
        if not entry.get('ready') or prior is None:
            continue
        stamp, value = prior
        if base and value.get("base") and value["base"] != base:
            continue
        if now >= stamp + EXCLUSION_S:
            continue
        until = datetime.datetime.fromtimestamp(
            stamp + EXCLUSION_S, datetime.timezone.utc).strftime('%H:%M UTC')
        entry['ready'] = False
        entry['causes'].append(dict(
            code='unit-tests-red', detail='exclu jusqu’à %s (rouge : %s) ; corriger puis regeler '
            'ou réessayer sur une nouvelle tête de la cible'
            % (until, ', '.join(value['tests'][:3]) or 'module non nommé'),
            source=value['source'], until_ts=stamp + EXCLUSION_S))


def remember(db, lot, source, target, tests, *, actor='', base=None):
    """Une note et une seule notification par tête, sous verrou du lot.

    Lecture ciblée de tout l'historique des marqueurs : work show est limité
    aux cent derniers événements et ne suffit pas pour cette déduplication.
    """
    from . import storage, work
    text = note(source, target, tests, base=base)
    with db.transaction() as tx:
        st = storage.of(tx)
        if st.work.lock(lot) is None:
            raise work.WorkError('lot %s not found' % lot)
        for event in st.progress.lot_hold_events([lot], [PATTERN]):
            value = read_note(event.get('note'))
            if value and value['source'] == source and value['target'] == target:
                return False
        work.note(tx, lot, text, actor=actor, once=True)
    return True

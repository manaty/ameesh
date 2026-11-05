# SPDX-License-Identifier: AGPL-3.0-only
"""#5971 : mémoire bornée des sources retirées pour tests par unité rouges.

Une note sur le lot survit au porteur et au clone du composeur. La première
note d'une source/cible/base donne au plus deux heures d'exclusion ; les
retraits répétés sur la même base ne renouvellent pas cette borne. Le courrier
reste unique par source/cible, même si la base change. Une source neuve
ou une nouvelle tête de la cible est admise immédiatement. Les non-concluants ne passent pas par ce chemin :
paquet-auto ne les retire pas (§3). La borne protège aussi contre un rouge
initialement attribué au code qui se révèle ensuite instable ou lié au banc.

La cible est normalisée (`origin/main`, `refs/heads/main` et `main` nomment
`main`) à l'écriture comme à la comparaison : une note écrite avec une cible
préfixée n'est jamais une exclusion muette.
"""
from __future__ import annotations

import datetime
import json
import re
import time

PREFIX = 'packet-unit-red: '
PATTERN = '^' + re.escape(PREFIX)
#: Le tic paquet-horaire passe toutes les dix minutes, mais un porteur ouvert
#: retient la composition suivante ; sa ci-full dure environ une heure (jobs
#: bornés à soixante minutes), puis le tic suivant compose. Deux heures
#: couvrent ce cycle — CI, tic de fermeture, tic de composition — avec une
#: marge, sans exclure indéfiniment un rouge instable.
EXCLUSION_S = 2 * 3600
#: Mêmes préfixes que `merge_queue._remote_branch`, dont `packet_candidates`
#: normalise déjà la cible des candidats.
TARGET_PREFIXES = ('refs/heads/', 'refs/remotes/origin/', 'origin/')


def normalize_target(target):
    """La branche nommée par `target` : `origin/main`, `refs/heads/main`,
    `refs/remotes/origin/main` et `main` nomment tous `main`."""
    text = target if isinstance(target, str) else ''
    for prefix in TARGET_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


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
    value['target'] = normalize_target(value['target'])
    if not value['target']:
        return None
    return value


def note(source, target, tests, *, base=None):
    value = dict(source=source, target=normalize_target(target), tests=list(tests))
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
        key = (int(event['work_item_id']), value['source'], value['target'], value.get('base'))
        if key not in first or stamp < first[key][0]:
            first[key] = (stamp, value)
    for entry in candidates:
        identity = (entry['work_item'], entry.get('source'),
                    normalize_target(entry.get('target')))
        prior = first.get((*identity, base)) or first.get((*identity, None))
        if not entry.get('ready') or prior is None:
            continue
        stamp, value = prior
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
    """Une note par source/cible/base, une notification par source/cible.

    Rend True seulement si le premier courrier doit être envoyé ; False peut
    accompagner une nouvelle note sur une autre base. Le verrou du lot couvre
    la déduplication de ces deux identités.

    Lecture ciblée de tout l'historique des marqueurs : work show est limité
    aux cent derniers événements et ne suffit pas pour cette déduplication.
    """
    from . import storage, work
    text = note(source, target, tests, base=base)
    with db.transaction() as tx:
        st = storage.of(tx)
        if st.work.lock(lot) is None:
            raise work.WorkError('lot %s not found' % lot)
        seen_source = False
        wanted = normalize_target(target)
        for event in st.progress.lot_hold_events([lot], [PATTERN]):
            value = read_note(event.get('note'))
            if value and value['source'] == source and value['target'] == wanted:
                seen_source = True
                if value.get('base') == base:
                    return False
        work.note(tx, lot, text, actor=actor, once=True)
    return not seen_source

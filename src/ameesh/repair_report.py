# SPDX-License-Identifier: AGPL-3.0-only
"""Rapport A1–A12 de la boucle d'auto-réparation (`ameesh repair-report`).

Étude #6308 (`docs/design/etudes/boucle-auto-reparation.md`), unité U1, lot
#6313 : un **lecteur** en lecture seule qui publie les compteurs de la section
« Écart mesuré sur une semaine » et déclare ses inconnues. Ce module ne répare
rien, n'ouvre aucun lot et n'active aucune politique : il lit la base et rend
un rapport reproductible.

Fenêtre **UTC fixe** `[start, end[` : les lots sont cohortés par
`work_items.created_at`, les occurrences par la date de leur événement. Une
borne explicite (`--start`/`--end`) rejoue une fenêtre passée ; sans borne, le
rapport couvre les `--days` derniers jours (7 par défaut).

Trois règles de lecture, testées :

* **alertes ≠ défauts** : `alert.raised` est compté dans A2/A9/A12, jamais
  ajouté à la cohorte des défauts (A1). Une alerte `decision_pending` n'est
  pas une panne, un `dead_runner` n'est pas nécessairement un défaut de code ;
* **événements ≠ runs** : les snapshots `staging.sequence` sont des
  événements ; seuls les `data->>'run'` distincts sont des runs, et un
  événement sans `run` n'en crée pas un ;
* **une valeur inconnue n'est jamais zéro** : elle sort `null` avec sa raison
  dans `unknown`, et le texte affiche « inconnu ». Un zéro n'est rendu que
  lorsqu'une mesure vide a réellement été lue.

Les compteurs suivent les capacités A1 à A12 de l'étude ; les requêtes sont
celles de « Requêtes reproductibles » (constantes `_*_SQL` ci-dessous), toutes
paramétrées et exécutées dans une transaction `READ ONLY`.

#6323 : le rapport **raccorde** U2 (#6315) et U4 (#6317) au lieu de les
laisser dormir (§5). Chaque `alert.raised` de la fenêtre devient une
enveloppe `repair-signal/1` par `repair_signals.from_alert` (clé
`data->>'key'`), résumée par `repair_signals.summary` (A1), puis corrélée à
la version active par `repair_correlation.correlate` avec les vues
`active_view`/`journal_view` (A4). L'environnement du mesh est **déclaré une
fois** (`SIGNAL_ENVIRONMENT` = `staging`, canon §14) : l'enveloppe U2 exige
un environnement et le rapport ne l'attribue pas signal par signal. Une
alerte qui ne porte pas de révision observée reste `inconnu` : le rapport le
dit (`correlations_par_verdict`, `correlations_raisons`) et le compte, jamais
un zéro inventé. Un `alert.raised` sans clé d'alerte est refusé et compté,
jamais identifié par son texte.

Sortie : `ameesh-repair-report/1` en `--json`, un tableau lisible sinon.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from . import repair_correlation, repair_signals

SCHEMA = "ameesh-repair-report/1"
#: la section « Écart mesuré sur une semaine » et ses requêtes reproductibles
SOURCE = "docs/design/etudes/boucle-auto-reparation.md#U1"
DEFAULT_PROJECT = "ameesh"
#: fenêtre par défaut : la semaine de l'étude
DEFAULT_DAYS = 7
#: borne de sécurité d'une fenêtre glissante (année bissextile comprise)
MAX_DAYS = 3660

DEFAULT_UNKNOWN = "mesure non lue ou non calculable dans cette fenêtre"

#: Environnement déclaré des signaux : celui du mesh ameesh (canon §14, le
#: staging est l'environnement actif) et celui que U4 sait corréler. U2 exige
#: un environnement ; le rapport le déclare une fois pour la fenêtre, il ne
#: l'invente pas alerte par alerte.
SIGNAL_ENVIRONMENT = repair_correlation.ENVIRONNEMENT

#: Les compteurs A1 du raccord U2 (`repair-signals-summary/1`).
SIGNAL_COUNTERS = (
    "signaux_recus", "signaux_occurrences", "signaux_par_source",
    "signaux_par_environnement", "signaux_par_issue", "signaux_sans_cle_cause",
    "signaux_sans_version_observee", "signaux_sans_date_observation",
    "signaux_refuses", "signaux_refuses_raisons",
)
#: Les compteurs A4 du raccord U4 (`ameesh-repair-correlation/1`).
CORRELATION_COUNTERS = (
    "signaux_correles", "correlations_inconnues", "correlations_par_verdict",
    "correlations_raisons",
)

CAPABILITIES = (
    ("A1", "Réception durable des signaux"),
    ("A2", "Qualification, sévérité et routage"),
    ("A3", "Identité, déduplication et récidive"),
    ("A4", "Corrélation à la version active"),
    ("A5", "Politique d'auto-réparation"),
    ("A6", "Réparation par un agent et garde sur le diff"),
    ("A7", "Autorité liée au contenu exact"),
    ("A8", "Preuves de vérification"),
    ("A9", "Décisions, échéances et escalades"),
    ("A10", "Livraison, surveillance et récidive"),
    ("A11", "Reprise des exécutions"),
    ("A12", "Balayage et surveillance de la boucle"),
)

#: Raisons des inconnues structurelles (l'étude les nomme ; aucune n'est un 0).
UNKNOWN = {
    "defauts_sans_lot": (
        "aucun journal exhaustif des signaux avec leur issue de triage : le "
        "dénominateur des défauts réels reste inconnu, et une alerte sans lot "
        "n'est pas un défaut"),
    "qualifications": (
        "aucune trace de qualification structurée (nature, sévérité, confiance) "
        "dans la fenêtre : A2 n'est pas mesuré"),
    "signaux_correles": (
        "les enveloppes A1 ou la vue des attestations n'ont pas été lues : la "
        "corrélation à la version active n'est pas mesurée"),
    "signaux_recus": (
        "le résumé A1 (`repair-signals-summary/1`) n'a pas été lu dans la "
        "fenêtre"),
    "signaux_occurrences": (
        "le résumé A1 des occurrences n'a pas été lu dans la fenêtre"),
    "signaux_par_source": (
        "le résumé A1 par source n'a pas été lu dans la fenêtre"),
    "signaux_par_environnement": (
        "le résumé A1 par environnement n'a pas été lu dans la fenêtre"),
    "signaux_par_issue": (
        "le résumé A1 par issue de triage n'a pas été lu dans la fenêtre"),
    "signaux_sans_cle_cause": (
        "le résumé A1 des clés de cause absentes n'a pas été lu dans la "
        "fenêtre"),
    "signaux_sans_version_observee": (
        "le résumé A1 des révisions observées absentes n'a pas été lu dans la "
        "fenêtre"),
    "signaux_sans_date_observation": (
        "le résumé A1 des dates d'observation absentes n'a pas été lu dans la "
        "fenêtre"),
    "signaux_refuses": (
        "les `alert.raised` refusés comme enveloppes n'ont pas été lus dans la "
        "fenêtre"),
    "signaux_refuses_raisons": (
        "les motifs de refus des enveloppes n'ont pas été lus dans la fenêtre"),
    "correlations_inconnues": (
        "les corrélations inconnues n'ont pas été lues : la vue des "
        "attestations manque"),
    "correlations_par_verdict": (
        "la ventilation des corrélations par verdict n'a pas été lue : la vue "
        "des attestations manque"),
    "correlations_raisons": (
        "les raisons des corrélations inconnues n'ont pas été lues : la vue "
        "des attestations manque"),
    "politique_presente": (
        "aucune politique A5 écrite au canon (décision séparée) : l'absence "
        "n'est pas une politique observée"),
    "admissions_evaluees": (
        "aucune politique A5 : aucune admission de réparation n'a pu être "
        "évaluée ; un vide n'est pas un zéro"),
    "refus_motives": (
        "aucune politique A5 : aucun motif de refus d'admission n'est mesuré"),
    "plans_reproduction": (
        "aucun plan de reproduction structuré (symptôme, témoin négatif, "
        "périmètre) sur les lots de la cohorte"),
    "gestes_avec_digest": (
        "aucun reçu lié à un plan de réparation : l'autorité par digest n'est "
        "pas mesurée"),
    "gestes_hors_scope": (
        "aucun reçu lié à un plan de réparation : les gestes hors portée ne "
        "sont pas mesurés"),
    "preuves_verification": (
        "les preuves de vérification (prevalidation, attestation) ne sont pas "
        "reliées à un plan de réparation"),
    "non_concluants": (
        "aucune preuve de vérification reliée à un plan : les non-concluants "
        "ne sont pas mesurés"),
    "decisions_sans_echeance": (
        "aucune mesure des demandes de décision sans échéance dans la fenêtre"),
    "escalades": (
        "aucune mesure des escalades de décision dans la fenêtre"),
    "signaux_apres_livraison": (
        "aucun suivi structuré des signaux après livraison : la résolution "
        "observée n'est pas mesurée"),
    "reprises_par_origine": (
        "l'origine (opérateur, agent, automate) des reprises n'est pas unifiée "
        "dans le journal : une reprise manuelle n'est pas distinguable"),
    "syntheses": (
        "aucune passe de balayage A12 mesurée ; le rapport ne clôt, ne "
        "supprime et ne répare rien"),
}

#: Limites de lecture, reprises de « Ce que cela permet de conclure ».
LIMITS = {
    "A1": (
        "la cohorte compte les défauts déclarés (app et type), pas tous les "
        "incidents réels",
        "un taux de couverture ne peut pas être calculé sans le dénominateur "
        "des signaux éligibles effectivement reçus",
        "les enveloppes ne couvrent que les alert.raised de la fenêtre : le "
        "fil, la boîte et la veille GitHub n'ont pas encore de lecteur commun "
        "ici ; un signal refusé est compté, jamais identifié par son texte",
        "les compteurs A1 du raccord sont ceux de repair_signals.summary/1 ; "
        "l'environnement déclaré (staging) est celui du mesh, pas une preuve "
        "que chaque alerte vient de lui",
    ),
    "A2": (
        "une alerte n'est pas un défaut : les alertes de décision et de runner "
        "n'entrent pas dans la cohorte des défauts",
        "le type d'alerte vient de data->>'alert', la référence de lot de "
        "data->>'lot' ; l'absence de référence ne prouve pas l'absence de défaut",
    ),
    "A3": (
        "superseded_by compte des remplacements, pas nécessairement des "
        "doublons : ne pas l'ajouter aux liens duplicates",
        "zéro doublon technique ouvert ne prouve pas l'absence de doublons "
        "sémantiques ; les liens reconnus sont une borne basse",
        "les récidives de la clé générique du tic paquet peuvent réunir des "
        "causes différentes : une récidive n'est pas une régression démontrée",
    ),
    "A4": (
        "une proximité temporelle n'est pas une preuve que le correctif "
        "tournait sur l'hôte du signal",
        "une attestation de staging n'est pas une preuve de résolution",
        "le journal staging est global au mesh (staging.attested ne porte pas "
        "de projet) : il n'est pas filtré par projet",
        "une alerte qui ne porte pas de révision observée reste inconnue : "
        "elle est comptée (correlations_par_verdict, correlations_raisons), "
        "jamais corrélée par défaut",
        "l'hôte vient de data->>'host' quand l'alerte le porte ; sans lui, "
        "aucune portée n'est supposée et la corrélation est inconnue",
    ),
    "A5": (
        "politique absente ou inconnue : aucune nouvelle réparation "
        "automatique n'est admise, et ce rapport ne l'active pas",
        "l'interrupteur arrête les admissions, jamais brutalement une action "
        "engagée ; ce lecteur n'actionne aucun interrupteur",
    ),
    "A6": (
        "les retours QA → build constatent des reprises de travail, pas la "
        "reproduction d'un symptôme ni un correctif",
        "aucune admission de réparation n'est lue ici",
    ),
    "A7": (
        "une preuve (hash, attestation) n'est pas une permission ; aucun reçu "
        "de réparation n'est lu par ce rapport",
        "un contenu changé après l'accord doit être hors portée, jamais "
        "converti en accord sur le nouveau diff",
    ),
    "A8": (
        "absence de preuve n'est jamais verte ; le non-concluant ne retient "
        "pas à lui seul paquet ou release (canon §3)",
        "le rapport ne rejoue aucun test et ne signe aucune preuve",
    ),
    "A9": (
        "une alerte de décision prouve l'usage de la file, pas l'absence de "
        "mécanisme d'échéance",
        "aucun silence, délai ou escalade ne vaut approbation (décision 0022)",
    ),
    "A10": (
        "l'absence de nouveaux signaux n'est une preuve utile que si la source "
        "est fraîche et son exposition connue",
        "une récidive après version active rouvre le diagnostic, elle "
        "n'efface pas l'historique",
        "le journal staging est global au mesh : il n'est pas filtré par projet",
    ),
    "A11": (
        "un snapshot staging.sequence n'est pas une tentative : les événements "
        "ne sont pas des reprises",
        "un événement sans run ne compte pas comme un run ; les runs distincts "
        "sont un plancher, pas un total de tentatives",
        "QA → build ne mesure pas les redémarrages manuels de processus",
    ),
    "A12": (
        "le balayage et la synthèse A12 ne sont pas encore mesurés ; ce "
        "rapport ne clôt, ne supprime et ne répare rien",
        "une alerte sans référence de lot signale un runner, pas "
        "nécessairement un défaut de code",
    ),
}

# -- fenêtre -----------------------------------------------------------------

class RepairReportError(Exception):
    """Argument ou fenêtre invalide (rendu en code 2 par la CLI)."""


def parse_instant(text: str) -> float:
    """Un instant ISO 8601 **avec fuseau**, ramené en UTC (epoch)."""
    if not isinstance(text, str) or not text.strip():
        raise RepairReportError("instant ISO 8601 attendu")
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RepairReportError("instant ISO 8601 illisible : %r" % text) from exc
    if moment.tzinfo is None:
        raise RepairReportError(
            "instant sans fuseau : %r (fenêtre UTC fixe, ajouter Z ou +00:00)"
            % text)
    return moment.astimezone(timezone.utc).timestamp()


def iso_z(ts: float) -> str:
    """Un epoch en ISO 8601 UTC, `Z` compris (microsecondes si présentes)."""
    moment = datetime.fromtimestamp(float(ts), timezone.utc)
    if moment.microsecond:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _instant(value: Any) -> float:
    if isinstance(value, str):
        return parse_instant(value)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RepairReportError("instant illisible : %r" % (value,)) from exc


def window(*, now: Any = None, days: int = DEFAULT_DAYS,
           start: str | None = None, end: str | None = None) -> tuple[float, float]:
    """Bornes `[start, end[` d'une fenêtre UTC fixe.

    * `start` et `end` : bornes explicites (rejeu d'une fenêtre passée) ;
    * `start` seul : `end = start + days` ;
    * `end` seul : `start = end - days` ;
    * aucun : `end = now` (ou l'instant courant), `start = end - days`.
    """
    if isinstance(days, bool) or not isinstance(days, int) or not 0 < days <= MAX_DAYS:
        raise RepairReportError("--days attend un entier entre 1 et %d" % MAX_DAYS)
    at = _instant(now) if now is not None else time.time()
    start_ts = parse_instant(start) if start else None
    end_ts = parse_instant(end) if end else None
    if start_ts is not None and end_ts is not None:
        pass
    elif start_ts is not None:
        end_ts = start_ts + days * 86400.0
    elif end_ts is not None:
        start_ts = end_ts - days * 86400.0
    else:
        end_ts = at
        start_ts = end_ts - days * 86400.0
    if end_ts <= start_ts:
        raise RepairReportError("fenêtre vide ou inversée : [%s, %s["
                                % (iso_z(start_ts), iso_z(end_ts)))
    return start_ts, end_ts


# -- lectures (requêtes reproductibles de l'étude) ---------------------------
#
# psycopg traite `%` dans une requête paramétrée : un `%` littéral (joker
# LIKE) s'y écrit `%%`. Le pilote psql (`db.bind`) ne réécrit que `%s` et
# laisse `%%` au serveur, où deux jokers consécutifs valent le même joker.

_COHORT_SQL = """
    SELECT count(*) AS lots,
           count(*) FILTER (WHERE source LIKE 'empreinte:%%') AS empreinte_lots,
           count(DISTINCT lower(source)) FILTER (WHERE source LIKE 'empreinte:%%')
               AS empreinte_cles,
           count(*) FILTER (WHERE superseded_by IS NOT NULL) AS remplaces,
           count(*) FILTER (WHERE title LIKE '%%récidive de #%%') AS recidives,
           count(*) FILTER (WHERE source LIKE 'empreinte:%%:paquet-horaire:tick:%%'
                              AND title LIKE '%%récidive de #%%') AS recidives_cle_paquet
      FROM work_items
     WHERE app = %s AND type = 'bug'
       AND created_at >= to_timestamp(%s)
       AND created_at <  to_timestamp(%s)
"""

_ALERTS_SQL = """
    SELECT data->>'alert' AS alerte, count(*) AS levees,
           count(*) FILTER (WHERE data->>'lot' IS NULL) AS sans_reference_lot
      FROM ameesh_events
     WHERE type = 'alert.raised' AND project = %s
       AND created_at >= to_timestamp(%s)
       AND created_at <  to_timestamp(%s)
     GROUP BY 1
     ORDER BY 1
"""

_OCCURRENCES_SQL = """
    SELECT count(*) AS notes, count(DISTINCT e.work_item_id) AS lots
      FROM work_item_events e
      JOIN work_items w ON w.id = e.work_item_id
     WHERE w.app = %s
       AND e.note LIKE 'Empreinte revue%%'
       AND e.created_at >= to_timestamp(%s)
       AND e.created_at <  to_timestamp(%s)
"""

_OPEN_DUPLICATES_SQL = """
    SELECT lower(source) AS cle, count(*) AS lots
      FROM work_items
     WHERE app = %s AND source LIKE 'empreinte:%%'
       AND state NOT IN ('merged', 'promoted', 'delivered', 'closed')
     GROUP BY 1
    HAVING count(*) > 1
"""

_DUPLICATE_LINKS_SQL = """
    SELECT count(DISTINCT w.id) AS lots
      FROM work_items w
      JOIN work_item_links l ON l.work_item_id = w.id AND l.kind = 'duplicates'
     WHERE w.app = %s AND w.type = 'bug'
       AND w.created_at >= to_timestamp(%s)
       AND w.created_at <  to_timestamp(%s)
"""

#: le lag se calcule sur l'histoire entière avant de filtrer la fenêtre
#: (commentaire de la requête de l'étude) ; `e.created_at < end` borne la
#: partition sans perdre l'événement précédant la borne.
_QA_RETURNS_SQL = """
    WITH transitions AS (
      SELECT e.work_item_id, e.state, e.created_at,
             lag(e.state) OVER (PARTITION BY e.work_item_id ORDER BY e.id) AS precedent
        FROM work_item_events e
        JOIN work_items w ON w.id = e.work_item_id
       WHERE w.app = %s
         AND e.created_at < to_timestamp(%s)
    )
    SELECT count(*) AS reprises, count(DISTINCT work_item_id) AS lots
      FROM transitions
     WHERE precedent = 'qa' AND state = 'build'
       AND created_at >= to_timestamp(%s)
"""

#: journal global du mesh : les attestations ne portent pas de projet
_STAGING_SQL = """
    SELECT type, count(*) AS evenements, count(DISTINCT data->>'run') AS runs
      FROM ameesh_events
     WHERE type IN ('staging.sequence', 'staging.attested', 'staging.request')
       AND created_at >= to_timestamp(%s)
       AND created_at <  to_timestamp(%s)
     GROUP BY type
     ORDER BY type
"""

#: #6323 : une ligne par alerte levée, pour l'enveloppe A1 (U2) et la
#: corrélation A4 (U4). L'identité durable est la clé d'alerte
#: (`data->>'key'`, sha256 de alert_ack), jamais le texte ; l'hôte et une
#: éventuelle révision observée sont lus quand l'émetteur les porte.
_SIGNAL_ALERTS_SQL = """
    SELECT data->>'key' AS cle,
           data->>'host' AS hote,
           data->>'revision' AS revision,
           data->>'observed_version' AS version_observee,
           data->>'since' AS depuis,
           extract(epoch FROM created_at) AS cree
      FROM ameesh_events
     WHERE type = 'alert.raised' AND project = %s
       AND created_at >= to_timestamp(%s)
       AND created_at <  to_timestamp(%s)
     ORDER BY created_at, id
"""


def read(db, *, project: str = DEFAULT_PROJECT, now: Any = None,
         days: int = DEFAULT_DAYS, start: str | None = None,
         end: str | None = None) -> dict[str, Any]:
    """Lit les mesures de la fenêtre et rend le rapport (`build`).

    Toutes les requêtes sont des SELECT, exécutées dans une transaction
    `READ ONLY` : une écriture accidentelle échouerait avant de modifier quoi
    que ce soit.
    """
    at = _instant(now) if now is not None else float(int(time.time()))
    start_ts, end_ts = window(now=at, days=days, start=start, end=end)
    with db.transaction() as tx:
        tx.execute("SET TRANSACTION READ ONLY")
        results = tx.query_batch([
            (_COHORT_SQL, (project, start_ts, end_ts)),
            (_ALERTS_SQL, (project, start_ts, end_ts)),
            (_OCCURRENCES_SQL, (project, start_ts, end_ts)),
            (_OPEN_DUPLICATES_SQL, (project,)),
            (_DUPLICATE_LINKS_SQL, (project, start_ts, end_ts)),
            (_QA_RETURNS_SQL, (project, end_ts, start_ts)),
            (_STAGING_SQL, (start_ts, end_ts)),
            (_SIGNAL_ALERTS_SQL, (project, start_ts, end_ts)),
        ])
    cohort_rows, alert_rows, occurrence_rows, open_rows, link_rows, qa_rows, \
        staging_rows, signal_rows = results
    # #6323 : la vue des attestations et le journal se lisent hors de la
    # transaction en lecture seule. Sans aucun signal, ces lectures ne
    # servent à rien : les compteurs A4 sont alors des zéros mesurés.
    active = journal = None
    if signal_rows:
        active = repair_correlation.active_view(db)
        journal = repair_correlation.journal_view(iso_z(start_ts))
    return build(
        project=project, at=at, start=start_ts, end=end_ts,
        cohort=cohort_rows[0] if cohort_rows else None,
        alerts=alert_rows,
        occurrences=occurrence_rows[0] if occurrence_rows else None,
        open_duplicates=open_rows,
        duplicate_links=link_rows[0] if link_rows else None,
        qa_returns=qa_rows[0] if qa_rows else None,
        staging=staging_rows,
        signal_alerts=signal_rows,
        active=active,
        journal=journal,
    )


# -- calcul (pur, témoin-testable sans base) ---------------------------------

def _int(row: Mapping[str, Any] | None, key: str) -> int | None:
    if not row or key not in row or row[key] is None:
        return None
    try:
        return int(row[key])
    except (TypeError, ValueError):
        return None


def _sum(rows: Iterable[Mapping[str, Any]] | None, key: str) -> int | None:
    if rows is None:
        return None
    total = 0
    for row in rows:
        value = _int(row, key)
        if value is not None:
            total += value
    return total


def _alerts(rows: Sequence[Mapping[str, Any]] | None
            ) -> tuple[int | None, dict[str, int] | None, int | None]:
    """Total, ventilation par type et alertes sans référence de lot.

    Une alerte décision ou runner ne devient jamais un défaut : ce calcul ne
    touche pas la cohorte (A1).
    """
    if rows is None:
        return None, None, None
    by_type: dict[str, int] = {}
    for row in rows:
        name = str(row.get("alerte") or "inconnu")
        by_type[name] = by_type.get(name, 0) + (_int(row, "levees") or 0)
    return (sum(by_type.values()), dict(sorted(by_type.items())),
            _sum(rows, "sans_reference_lot"))


def _staging(rows: Sequence[Mapping[str, Any]] | None
             ) -> dict[str, int | None]:
    """Compteurs staging : événements et runs distincts restent séparés."""
    if rows is None:
        return {"sequence_events": None, "sequence_runs": None,
                "attested": None, "requests": None}
    by_type = {row.get("type"): row for row in rows if row.get("type")}

    def value(kind: str, key: str) -> int:
        row = by_type.get(kind)
        if row is None:
            return 0
        return _int(row, key) or 0

    return {"sequence_events": value("staging.sequence", "evenements"),
            "sequence_runs": value("staging.sequence", "runs"),
            "attested": value("staging.attested", "evenements"),
            "requests": value("staging.request", "evenements")}


def _number(value: Any) -> float | None:
    """Un epoch numérique, ou None : une chaîne illisible ne devient pas 0."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _envelopes(rows: Sequence[Mapping[str, Any]],
               *, project: str, environment: str, received_at: str
               ) -> tuple[list, list[list[str]], Counter]:
    """Construit les enveloppes A1 d'un lot de `alert.raised`.

    Rend `(signaux, portées, refus)` : la portée d'hôte de chaque signal (vide
    quand l'alerte n'en porte pas) et les refus motivés. Un refus n'arrête pas
    le rapport et ne recopie aucun texte de l'alerte.
    """
    signals: list = []
    scopes: list[list[str]] = []
    refused: Counter = Counter()
    for row in rows:
        observed = _number(row.get("depuis"))
        if observed is None:
            observed = _number(row.get("cree"))
        alert = {"schema": "ameesh-alert/1", "key": row.get("cle"),
                 "project": project}
        if observed is not None:
            alert["since"] = observed
        try:
            signal = repair_signals.from_alert(
                alert, project=project, environment=environment,
                received_at=received_at,
                observed_version=(row.get("revision")
                                  or row.get("version_observee") or ""))
        except repair_signals.SignalError as exc:
            refused[str(exc)] += 1
            continue
        signals.append(signal)
        host = row.get("hote")
        scopes.append([host] if isinstance(host, str) and host.strip() else [])
    return signals, scopes, refused


def _signal_counters(summary: Mapping[str, Any] | None, refused: Counter | None
                     ) -> dict[str, Any]:
    """Les compteurs A1 du raccord U2, ou des inconnues nommées."""
    if summary is None:
        return {name: None for name in SIGNAL_COUNTERS}
    unknown = summary.get("unknown") or {}
    return {
        "signaux_recus": summary["signals"],
        "signaux_occurrences": summary["occurrences"],
        "signaux_par_source": dict(summary["by_source"]),
        "signaux_par_environnement": dict(summary["by_environment"]),
        "signaux_par_issue": dict(summary["by_outcome"]),
        "signaux_sans_cle_cause": int(unknown.get("cause_key", 0)),
        "signaux_sans_version_observee": int(unknown.get("observed_version", 0)),
        "signaux_sans_date_observation": int(unknown.get("observed_at", 0)),
        "signaux_refuses": sum((refused or Counter()).values()),
        "signaux_refuses_raisons": dict(sorted((refused or Counter()).items())),
    }


def _correlations(signals: Sequence, scopes: Sequence[Sequence[str]], *,
                  active: Mapping[str, Any], journal: Sequence[Mapping[str, Any]],
                  at: float, is_ancestor=None) -> list[dict]:
    """La corrélation A4 de chaque enveloppe (U4, pure avec `now=at`)."""
    return [repair_correlation.correlate(
                signal.record(), active, journal=journal or (),
                hosts=list(scope) or None, now=at, is_ancestor=is_ancestor)
            for signal, scope in zip(signals, scopes)]


def _correlation_counters(reports: Sequence[Mapping[str, Any]] | None
                          ) -> dict[str, Any]:
    """Les compteurs A4 du raccord U4, ou des inconnues nommées."""
    if reports is None:
        return {name: None for name in CORRELATION_COUNTERS}
    verdicts = Counter(report.get("verdict") for report in reports)
    raisons = Counter(report.get("raison") for report in reports
                      if report.get("verdict") == repair_correlation.INCONNU
                      and report.get("raison"))
    return {
        "signaux_correles": sum(count for verdict, count in verdicts.items()
                                if verdict != repair_correlation.INCONNU),
        "correlations_inconnues": verdicts.get(repair_correlation.INCONNU, 0),
        "correlations_par_verdict": dict(sorted(verdicts.items())),
        "correlations_raisons": dict(sorted(raisons.items())),
    }


def _capability(ident: str, title: str, counters: Mapping[str, Any],
                limits: Sequence[str]) -> dict[str, Any]:
    unknown = {key: UNKNOWN.get(key, DEFAULT_UNKNOWN)
               for key, value in counters.items() if value is None}
    return {"id": ident, "title": title, "counters": dict(counters),
            "unknown": unknown, "limits": list(limits)}


def build(*, project: str = DEFAULT_PROJECT, at: float,
          start: float, end: float,
          cohort: Mapping[str, Any] | None,
          alerts: Sequence[Mapping[str, Any]] | None,
          occurrences: Mapping[str, Any] | None,
          open_duplicates: Sequence[Mapping[str, Any]] | None,
          duplicate_links: Mapping[str, Any] | None,
          qa_returns: Mapping[str, Any] | None,
          staging: Sequence[Mapping[str, Any]] | None,
          signal_alerts: Sequence[Mapping[str, Any]] | None = None,
          active: Mapping[str, Any] | None = None,
          journal: Sequence[Mapping[str, Any]] | None = None,
          environment: str = SIGNAL_ENVIRONMENT,
          is_ancestor=None) -> dict[str, Any]:
    """Assemble le rapport depuis les lignes lues (aucune base ici).

    Chaque mesure vaut `None` quand elle n'a pas pu être lue : le compteur
    sort alors `null` avec sa raison, jamais `0`. Une liste vide, elle, est
    une mesure réelle de zéro.

    `signal_alerts` : les `alert.raised` de la fenêtre (une ligne par alerte,
    `_SIGNAL_ALERTS_SQL`). `active`/`journal` : la vue des attestations et le
    journal staging fournis par le lecteur (`active_view`/`journal_view`) ;
    `active=None` avec des signaux rend la corrélation A4 inconnue, jamais
    concluante par défaut. `is_ancestor` est le prédicat d'ascendance injecté
    de U4 : sans lui, deux SHA inégaux restent `version-differente`.
    """
    if end <= start:
        raise RepairReportError("fenêtre vide ou inversée")
    cohort_counts = {
        "lots": _int(cohort, "lots"),
        "empreinte_lots": _int(cohort, "empreinte_lots"),
        "empreinte_cles": _int(cohort, "empreinte_cles"),
        "remplaces": _int(cohort, "remplaces"),
        "recidives": _int(cohort, "recidives"),
        "recidives_cle_paquet": _int(cohort, "recidives_cle_paquet"),
    }
    alert_total, alert_types, alert_without = _alerts(alerts)
    occurrence_notes = _int(occurrences, "notes")
    occurrence_lots = _int(occurrences, "lots")
    open_groups = None if open_duplicates is None else len(open_duplicates)
    open_lots = _sum(open_duplicates, "lots")
    link_lots = _int(duplicate_links, "lots")
    qa_transitions = _int(qa_returns, "reprises")
    qa_lots = _int(qa_returns, "lots")
    stage = _staging(staging)

    # #6323 — A1 : les enveloppes U2 des alertes lues ; A4 : leur corrélation
    # U4. Sans signal lu, aucun compteur n'est un zéro ; sans vue des
    # attestations, la corrélation est inconnue, jamais concluante.
    signals = scopes = refused = None
    if signal_alerts is not None:
        signals, scopes, refused = _envelopes(
            signal_alerts, project=project, environment=environment,
            received_at=iso_z(at))
    summary = (repair_signals.summary(signals) if signals is not None
               else None)
    if signals is None:
        correlations = None
    elif not signals:
        correlations = []
    elif active is None:
        correlations = None
    else:
        correlations = _correlations(signals, scopes, active=active,
                                     journal=journal or (), at=at,
                                     is_ancestor=is_ancestor)

    decision_alerts = (None if alert_types is None else
                       alert_types.get("decision_pending", 0)
                       + alert_types.get("decision_overdue", 0))

    counters = {
        "A1": {
            "cohorte_lots_defaut": cohort_counts["lots"],
            "empreinte_lots": cohort_counts["empreinte_lots"],
            "empreinte_cles": cohort_counts["empreinte_cles"],
            "defauts_sans_lot": None,
            **_signal_counters(summary, refused),
        },
        "A2": {
            "alertes_levees": alert_total,
            "alertes_par_type": alert_types,
            "alertes_sans_reference_lot": alert_without,
            "qualifications": None,
        },
        "A3": {
            "occurrences_empreinte_revue": occurrence_notes,
            "lots_avec_occurrence": occurrence_lots,
            "doublons_techniques_ouverts": open_groups,
            "doublons_techniques_lots": open_lots,
            "doublons_reconnus": link_lots,
            "remplaces_superseded": cohort_counts["remplaces"],
            "recidives_empreinte": cohort_counts["recidives"],
            "recidives_cle_paquet": cohort_counts["recidives_cle_paquet"],
        },
        "A4": {
            "attestations_staging": stage["attested"],
            "demandes_staging": stage["requests"],
            **_correlation_counters(correlations),
        },
        "A5": {
            "politique_presente": None,
            "admissions_evaluees": None,
            "refus_motives": None,
        },
        "A6": {
            "retours_qa_build": qa_transitions,
            "lots_repris": qa_lots,
            "plans_reproduction": None,
        },
        "A7": {
            "gestes_avec_digest": None,
            "gestes_hors_scope": None,
        },
        "A8": {
            "preuves_verification": None,
            "non_concluants": None,
        },
        "A9": {
            "alertes_decision": decision_alerts,
            "decisions_sans_echeance": None,
            "escalades": None,
        },
        "A10": {
            "attestations_staging": stage["attested"],
            "signaux_apres_livraison": None,
            "recidives_empreinte": cohort_counts["recidives"],
        },
        "A11": {
            "retours_qa_build": qa_transitions,
            "lots_repris": qa_lots,
            "snapshots_staging": stage["sequence_events"],
            "runs_staging": stage["sequence_runs"],
            "reprises_par_origine": None,
        },
        "A12": {
            "alertes_levees": alert_total,
            "alertes_sans_reference_lot": alert_without,
            "syntheses": None,
        },
    }
    capabilities = [_capability(ident, title, counters[ident], LIMITS[ident])
                    for ident, title in CAPABILITIES]
    span_days = (end - start) / 86400.0
    return {
        "schema": SCHEMA,
        "source": SOURCE,
        "project": project,
        "at": iso_z(at),
        "read_only": True,
        "window": {
            "start": iso_z(start),
            "end": iso_z(end),
            "days": int(span_days) if span_days == int(span_days) else round(span_days, 3),
            "timezone": "UTC",
        },
        "cohort": cohort_counts,
        "capabilities": capabilities,
        "unknown_count": sum(len(cap["unknown"]) for cap in capabilities),
    }


# -- texte -------------------------------------------------------------------

def _fmt(value: Any) -> str:
    if value is None:
        return "inconnu"
    if isinstance(value, dict):
        return "{" + ", ".join("%s=%s" % (key, value[key])
                               for key in sorted(value)) + "}"
    return str(value)


def format_text(report: Mapping[str, Any]) -> str:
    """Le rapport lisible : une capacité par bloc, inconnues et limites visibles."""
    moment = report.get("window") or {}
    cohort = report.get("cohort") or {}
    lines = [
        "Rapport auto-réparation A1–A12 — %s — lecture seule"
        % report.get("project"),
        "Fenêtre UTC [%s, %s[ (%s j), relevé %s"
        % (moment.get("start"), moment.get("end"), moment.get("days"),
           report.get("at")),
        "Cohorte : %s lot(s) de défaut, %s d'empreinte sur %s clé(s), "
        "%s remplacé(s), %s récidive(s)"
        % (_fmt(cohort.get("lots")), _fmt(cohort.get("empreinte_lots")),
           _fmt(cohort.get("empreinte_cles")), _fmt(cohort.get("remplaces")),
           _fmt(cohort.get("recidives"))),
    ]
    for capability in report.get("capabilities") or ():
        lines.append("")
        lines.append("%s — %s" % (capability.get("id"), capability.get("title")))
        for key, value in (capability.get("counters") or {}).items():
            lines.append("  %-30s %s" % (key, _fmt(value)))
            reason = (capability.get("unknown") or {}).get(key)
            if reason:
                lines.append("  %-30s ? %s" % ("", reason))
        for limit in capability.get("limits") or ():
            lines.append("  · %s" % limit)
    lines += [
        "",
        "%s compteur(s) déclaré(s) inconnu(s), jamais remplacés par zéro ; "
        "alertes ≠ défauts ; événements ≠ runs." % report.get("unknown_count"),
    ]
    return "\n".join(lines)


# -- CLI ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh repair-report",
        description="Rapport A1–A12 en lecture seule de la boucle "
                    "d'auto-réparation (étude #6308, unité U1) : fenêtre UTC "
                    "fixe, compteurs reproductibles, inconnues déclarées.",
    )
    parser.add_argument("--json", action="store_true",
                        help="sortir le schéma %s" % SCHEMA)
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help="projet de la cohorte (défaut : %s)" % DEFAULT_PROJECT)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="taille de la fenêtre en jours (défaut : %d)"
                             % DEFAULT_DAYS)
    parser.add_argument("--at", default=None,
                        help="instant du relevé et fin de fenêtre glissante "
                             "(ISO 8601 UTC ; défaut : maintenant)")
    parser.add_argument("--start", default=None,
                        help="début explicite de la fenêtre (ISO 8601 UTC)")
    parser.add_argument("--end", default=None,
                        help="fin exclusive explicite de la fenêtre (ISO 8601 UTC)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    from . import config as config_mod
    from . import db as db_mod
    try:
        # Valider la fenêtre avant de toucher la base : `--days 0` ou un
        # instant sans fuseau sont des erreurs d'argument (code 2), pas des
        # pannes de connexion.
        at = _instant(args.at) if args.at is not None else float(int(time.time()))
        window(now=at, days=args.days, start=args.start, end=args.end)
        cfg = config_mod.load()
        db = db_mod.connect(cfg)
        try:
            db_mod.require_schema(db, defer=True)
            report = read(db, project=args.project, now=at,
                          days=args.days, start=args.start, end=args.end)
        finally:
            db.close()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(format_text(report))
        return 0
    except RepairReportError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except db_mod.SchemaMissing as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("error: database unreachable: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

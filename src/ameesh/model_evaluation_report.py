# SPDX-License-Identifier: AGPL-3.0-only
"""Rapport en lecture seule d'évaluation des modèles, par projet et fenêtre.

  ameesh model-report [--json] [--project P] [--days N] [--at T]
                      [--start T] [--end T]

Étude #6650 (`docs/design/etudes/evaluation-modeles.md`), unité **M1** :
un lecteur qui publie les tableaux de la section « Mesures par famille, type
et taille » et leurs règles de couverture, sans écrire, ouvrir un lot ni
changer une politique. Les unités M2/M4 n'existent pas encore : les champs
qu'elles ajouteront (risque au gel, effort exécuté) sortent **inconnus**, avec
leur raison, jamais zéro.

Trois grains restent séparés, comme dans l'étude :

* **porteurs** (`integration/**`, `packet/**`) : hors des cohortes d'unités,
  publiés à part (ratio médian 0,274 dans le relevé) ;
* **cohorte d'intégration** : unités dont la première fusion non annulée (ou
  livraison) tombe dans la fenêtre ;
* **cohorte de staging** : unités dont au moins une attestation
  `staging.attested` de la fenêtre suit la première fusion ; une attestation
  compatible est une preuve enregistrée, pas une recette physique.

Règles testées (témoins du lot) :

* **fenêtre UTC fixe** `[start, end[`, 14 jours par défaut, bornes explicites
  rejouables ; aucune limite silencieuse à 1000 lignes ;
* **famille = harnais exécuté**, lue dans la dernière désignation du gel
  antérieure à l'intégration, jamais dans le registre courant ; plusieurs
  familles prouvées donnent `mixed`, une attribution absente ou incomplète
  `unknown` ;
* **SHA historiques** : les gels sont comparés par texte, sans équivalence de
  préfixe ; un regel est un texte distinct après le premier ;
* **cache Codex** : `input_tokens` inclut déjà le cache lu chez Codex, il est
  donc ajouté au contraire de Claude et DeepSeek (convention
  `TurnCosts.session_total`) ; le cache écrit reste séparé ;
* **compte forfait ≠ facture** : les USD du livre sont une estimation de
  consommation, jamais une facture ni une dépense marginale comparable entre
  un forfait et un paiement au jeton ;
* **donnée absente ≠ zéro** : une source non lue vaut `None` avec sa raison ;
  une lecture vide (liste vide) est un zéro mesuré ; les lignes `void_reason`
  sont écartées des sommes et comptées à part.

Ce module ne lit ni corps de courrier, ni prompt, ni secret : les requêtes ne
prennent que des métadonnées et des identifiants. Toutes les lectures sont
exécutées dans une transaction `READ ONLY`.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from . import estimates

SCHEMA = "ameesh-model-evaluation-report/1"
SOURCE = "docs/design/etudes/evaluation-modeles.md#M1"
#: la version de calcul, conservée à côté des tableaux (l'étude le demande)
CALCULATION_VERSION = 1
DEFAULT_PROJECT = "ameesh"
#: la fenêtre de l'étude : quatorze jours complets, `[start, end[` en UTC
DEFAULT_DAYS = 14
#: borne de sécurité d'une fenêtre glissante (dix ans)
MAX_DAYS = 3660

#: l'ordre des tableaux de l'étude
FAMILY_ORDER = ("claude", "codex", "deepseek", "mixed", "unknown")
#: les types déclarés (l'étude nomme `improvement` « amélioration »)
TYPE_ORDER = ("bug", "evolution", "improvement")
SIZE_ORDER = ("s", "m", "l", "unknown")

UNKNOWN = {
    "class": (
        "la classe historique (léger/sensible) n'est pas persistée : le "
        "quorum de la désignation ne la prouve pas et `ameesh review-class` "
        "la recalcule sur l'état courant, pas sur le gel historique"),
    "effort": (
        "aucun champ historique n'atteste l'effort réellement exécuté : le "
        "catalogue des efforts acceptés ne le remplace pas (M4)"),
    "b_justes": (
        "aucune source structurée ne dit qu'un B est juste : il faudrait un "
        "constat identifiable, son témoin sur le SHA lu, une résolution "
        "distincte et une adjudication indépendante (M3)"),
    "ratio": (
        "ratio réel/estimé inconnu : début mesuré, estimation antérieure au "
        "début et intégration valide requis"),
    "usd": (
        "coût USD inconnu pour cette unité : aucune ligne de grand livre "
        "non écartée dans sa fenêtre"),
    "turns": (
        "tours inconnus : le début du travail n'est pas mesuré, la fenêtre "
        "des tours ne peut pas être bornée"),
    "prompt": (
        "entrées relues inconnues : aucune ligne de grand livre non écartée"),
    "output": (
        "sorties inconnues : aucune ligne de grand livre non écartée"),
    "family": (
        "famille inconnue : aucune désignation `designated` avant "
        "l'intégration, ou attribution des auteurs absente/incomplète"),
    "size": "taille déclarée absente : elle n'est pas devinée",
    "type": "type déclaré absent ou hors du référentiel",
    "staging": (
        "aucune attestation compatible : absence de preuve dans ce relevé, "
        "pas preuve d'absence de livraison"),
    "review_family": (
        "famille du reçu de verdict absente : la notification n'est pas "
        "affectée au registre courant"),
}

#: Ce que le lecteur ne peut pas conclure ; l'étude les nomme déjà.
LIMITS = (
    "une attestation de staging compatible est une preuve enregistrée, pas "
    "une recette physique ni la preuve que tout le code du lot est activé",
    "les USD sont une estimation comptable de consommation : ni facture, ni "
    "dépense marginale comparable entre forfait et paiement au jeton ; aucune "
    "économie n'en est déduite",
    "un B déclaré n'est pas un défaut prouvé ; un lot fusionné ne tranche pas "
    "la justesse d'un B, et le nombre de B justes reste inconnu",
    "l'attribution des lots, des relecteurs et des familles n'est pas "
    "aléatoire : les différences décrivent des populations, pas un effet "
    "causal du modèle",
    "la classe historique est inconnue pour toutes les unités ; le type et la "
    "taille sont déclaratifs et la taille manque pour une partie des lots",
    "les cohortes d'intégration et de staging sont publiées séparément et "
    "leurs coûts portent sur des phases différentes : ils ne se comparent pas",
    "les unités abandonnées ou ouvertes ne sont pas dans ces cohortes ; leur "
    "ajout est une unité distincte (M5)",
    "aucune p-value ni intervalle de confiance n'est inventé ; une cellule de "
    "petit effectif ne soutient aucune recommandation",
    "le rapport ne lit ni corps de courrier, ni prompt, ni secret, et "
    "n'écrit rien : une panne du rapport retire le conseil, pas les preuves",
)

COVERAGE_RULES = (
    ("window", "la fenêtre est UTC et fixe, `[start, end[` ; les bornes "
               "explicites rejouent une fenêtre passée ; aucune troncature "
               "silencieuse à 1000 lignes"),
    ("family", "la famille est le harnais exécuté, lu dans la dernière "
               "désignation du gel antérieure à l'intégration ; plusieurs "
               "familles prouvées donnent `mixed`, une attribution absente "
               "ou incomplète `unknown`, jamais le registre courant"),
    ("sha", "les SHA de gel sont comparés par texte, sans équivalence de "
            "préfixe ; un regel est un texte distinct après le premier ; un "
            "SHA vide ne compte pas comme tête"),
    ("carriers", "les porteurs `integration/**` et `packet/**` sont hors des "
                 "cohortes d'unités et publiés à part : les mélanger ferait "
                 "paraître l'évolution artificiellement plus rapide"),
    ("cohorts", "cohorte d'intégration = première fusion non annulée ou "
                "livraison dans la fenêtre ; cohorte de staging = au moins "
                "une attestation de la fenêtre après cette fusion ; les deux "
                "restent séparées et peuvent se recouvrir"),
    ("ratio", "réel/estimé = (intégration − début mesuré) / estimation en "
              "vigueur au début, en minutes écoulées ; une estimation tardive "
              "ou un début absent laissent le ratio inconnu"),
    ("cost", "les USD somment les lignes de grand livre non écartées entre le "
             "début et la fin de la phase (intégration ou attestation) ; les "
             "lignes `void_reason` sont comptées à part et exclues ; le coût "
             "du porteur et les régressions après l'attestation restent hors "
             "de ce coût attribué"),
    ("cache", "entrées relues = `input_tokens` pour Codex (le cache lu y est "
              "déjà inclus), `input_tokens + cached_input_tokens` pour Claude "
              "et DeepSeek ; le cache écrit reste séparé et n'est jamais "
              "ajouté aux relectures"),
    ("usd", "les USD sont une estimation de consommation du livre comptable : "
            "ni facture, ni dépense marginale comparable entre un compte "
            "forfait et un compte au jeton ; `usd_is_invoice` reste faux"),
    ("missing", "une valeur non lue est `null` avec sa raison ; une lecture "
                "vide est un zéro mesuré ; un zéro n'est jamais le "
                "remplacement d'une donnée absente"),
    ("b", "un B est une ligne de verdict `blocked` déclarée dans la fenêtre "
          "du lot ; il n'est ni un défaut prouvé ni un verdict admis par la "
          "porte ; les lignes répétées restent comptées"),
    ("class", "la classe historique n'est pas persistée : elle reste inconnue "
              "pour chaque unité ; un quorum ne la prouve pas"),
    ("effort", "l'effort exécuté n'est pas persisté : il reste inconnu ; le "
               "catalogue des efforts acceptés ne le remplace pas"),
    ("review", "les sièges notifiés et les verdicts rendus ne forment pas le "
               "même entonnoir ; une notification sans famille reste hors "
               "des trois lignes et n'est pas affectée au registre courant"),
    ("causality", "les tableaux sont descriptifs : aucune inférence causale "
                  "n'est tirée d'une différence entre familles, et aucune "
                  "recommandation n'est produite par ce lecteur"),
)


class ModelEvaluationReportError(Exception):
    """Argument ou fenêtre invalide (rendu en code 2 par la CLI)."""


# -- fenêtre -----------------------------------------------------------------

def parse_instant(text: str) -> float:
    """Un instant ISO 8601 **avec fuseau**, ramené en UTC (epoch)."""
    if not isinstance(text, str) or not text.strip():
        raise ModelEvaluationReportError("instant ISO 8601 attendu")
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ModelEvaluationReportError(
            "instant ISO 8601 illisible : %r" % text) from exc
    if moment.tzinfo is None:
        raise ModelEvaluationReportError(
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
    if isinstance(value, bool) or value is None:
        raise ModelEvaluationReportError("instant illisible : %r" % (value,))
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ModelEvaluationReportError(
            "instant illisible : %r" % (value,)) from exc


def window(*, now: Any = None, days: int = DEFAULT_DAYS,
           start: str | None = None,
           end: str | None = None) -> tuple[float, float]:
    """Bornes `[start, end[` d'une fenêtre UTC fixe.

    * `start` et `end` : bornes explicites (rejeu d'une fenêtre passée) ;
    * `start` seul : `end = start + days` ;
    * `end` seul : `start = end - days` ;
    * aucun : `end = now` (ou l'instant courant), `start = end - days`.
    """
    if (isinstance(days, bool) or not isinstance(days, int)
            or not 0 < days <= MAX_DAYS):
        raise ModelEvaluationReportError(
            "--days attend un entier entre 1 et %d" % MAX_DAYS)
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
        raise ModelEvaluationReportError(
            "fenêtre vide ou inversée : [%s, %s["
            % (iso_z(start_ts), iso_z(end_ts)))
    return start_ts, end_ts


# -- lectures (requêtes de l'étude) -----------------------------------------

#: Une ligne par unité candidate : fusion effective, attestation compatible,
#: estimation au début, désignations du gel (notes brutes, parsées en Python),
#: tours et coûts sur les deux phases, B et regels.
#:
#: `agent_mesh_merged_annulled` est le prédicat de #2317 : une correction
#: `merged → build` annule la fusion ; les jalons annulés restent dans la
#: table et sont écartés ici.
_UNITS_SQL = """
WITH bounds AS (
  SELECT to_timestamp(%s) AS lo, to_timestamp(%s) AS hi
), staged AS (
  SELECT a.id AS event_id, x.value::bigint AS lot, a.created_at AS at
    FROM ameesh_events a
    CROSS JOIN bounds b
    CROSS JOIN LATERAL jsonb_array_elements_text(a.data->'lots') x(value)
   WHERE a.type = 'staging.attested'
     AND a.project = %s
     AND a.created_at >= b.lo AND a.created_at < b.hi
     AND x.value ~ '^[0-9]+$'
), candidates AS (
  SELECT w.id, w.type, w.change_size, w.branch, w.state, w.close_reason,
         extract(epoch FROM w.started_at) AS started_at,
         extract(epoch FROM f.at) AS finished_at,
         extract(epoch FROM s.at) AS active_at,
         w.started_at AS started_ts, f.at AS finished_ts, s.at AS active_ts,
         e.minutes AS estimate_minutes, e.estimated_by
    FROM work_items w
    CROSS JOIN bounds b
    JOIN LATERAL (
      SELECT min(m.at) AS at
        FROM work_item_milestones m
       WHERE m.work_item_id = w.id
         AND m.kind IN ('merged', 'delivered')
         AND (m.kind <> 'merged'
              OR NOT agent_mesh_merged_annulled(w.id, m.at))
    ) f ON f.at IS NOT NULL
    LEFT JOIN LATERAL (
      SELECT min(s0.at) AS at
        FROM staged s0
       WHERE s0.lot = w.id AND s0.at >= f.at
    ) s ON true
    LEFT JOIN LATERAL (
      SELECT minutes, estimated_by
        FROM work_item_estimates e
       WHERE e.work_item_id = w.id
         AND w.started_at IS NOT NULL
         AND e.at <= w.started_at
       ORDER BY e.at DESC, e.id DESC
       LIMIT 1
    ) e ON true
   WHERE w.app = %s
     AND f.at < b.hi
     AND (f.at >= b.lo OR EXISTS (SELECT 1 FROM staged s1 WHERE s1.lot = w.id))
     AND (w.state <> 'closed' OR w.close_reason = 'delivered')
)
SELECT c.*,
       d.notes,
       ts.turns, ts.usd, ts.cost_rows, ts.void_rows, ts.prompt,
       ts.input_tokens, ts.cached_input_tokens, ts.cache_write_tokens,
       ts.output_tokens, ts.model_unknown, ts.turn_ids,
       tt.turns AS active_turns, tt.usd AS active_usd,
       tt.cost_rows AS active_cost_rows, tt.void_rows AS active_void_rows,
       tt.prompt AS active_prompt, tt.input_tokens AS active_input_tokens,
       tt.cached_input_tokens AS active_cached_input_tokens,
       tt.cache_write_tokens AS active_cache_write_tokens,
       tt.output_tokens AS active_output_tokens,
       tt.model_unknown AS active_model_unknown,
       tt.turn_ids AS active_turn_ids,
       mm.blocked AS blocked, mm.heads AS distinct_heads
  FROM candidates c
  LEFT JOIN LATERAL (
    SELECT array_agg(e.note ORDER BY e.created_at DESC, e.id DESC) AS notes
      FROM work_item_events e
     WHERE e.work_item_id = c.id
       AND e.note LIKE 'review-designation:%%'
       AND e.created_at <= c.finished_ts
  ) d ON true
  LEFT JOIN LATERAL (
    SELECT
      CASE WHEN c.started_ts IS NULL THEN NULL ELSE count(*) END AS turns,
      count(*) FILTER (WHERE void_reason IS NULL) AS cost_rows,
      count(*) FILTER (WHERE void_reason IS NOT NULL) AS void_rows,
      sum(usd) FILTER (WHERE void_reason IS NULL) AS usd,
      sum(input_tokens) FILTER (WHERE void_reason IS NULL) AS input_tokens,
      sum(cached_input_tokens) FILTER (WHERE void_reason IS NULL)
        AS cached_input_tokens,
      sum(cache_write_tokens) FILTER (WHERE void_reason IS NULL)
        AS cache_write_tokens,
      sum(output_tokens) FILTER (WHERE void_reason IS NULL) AS output_tokens,
      sum(input_tokens + CASE WHEN harness = 'codex' THEN 0
                               ELSE cached_input_tokens END)
        FILTER (WHERE void_reason IS NULL) AS prompt,
      count(*) FILTER (WHERE void_reason IS NULL
                        AND (model IS NULL OR model = '')) AS model_unknown,
      count(DISTINCT turn_id) FILTER (WHERE void_reason IS NULL) AS turn_ids
      FROM turn_costs tc
     WHERE tc.work_item_id = c.id
       AND c.started_ts IS NOT NULL
       AND tc.recorded_at >= c.started_ts
       AND tc.recorded_at <= c.finished_ts
  ) ts ON true
  LEFT JOIN LATERAL (
    SELECT
      CASE WHEN c.started_ts IS NULL THEN NULL ELSE count(*) END AS turns,
      count(*) FILTER (WHERE void_reason IS NULL) AS cost_rows,
      count(*) FILTER (WHERE void_reason IS NOT NULL) AS void_rows,
      sum(usd) FILTER (WHERE void_reason IS NULL) AS usd,
      sum(input_tokens) FILTER (WHERE void_reason IS NULL) AS input_tokens,
      sum(cached_input_tokens) FILTER (WHERE void_reason IS NULL)
        AS cached_input_tokens,
      sum(cache_write_tokens) FILTER (WHERE void_reason IS NULL)
        AS cache_write_tokens,
      sum(output_tokens) FILTER (WHERE void_reason IS NULL) AS output_tokens,
      sum(input_tokens + CASE WHEN harness = 'codex' THEN 0
                               ELSE cached_input_tokens END)
        FILTER (WHERE void_reason IS NULL) AS prompt,
      count(*) FILTER (WHERE void_reason IS NULL
                        AND (model IS NULL OR model = '')) AS model_unknown,
      count(DISTINCT turn_id) FILTER (WHERE void_reason IS NULL) AS turn_ids
      FROM turn_costs tc
     WHERE tc.work_item_id = c.id
       AND c.started_ts IS NOT NULL
       AND tc.recorded_at >= c.started_ts
       AND tc.recorded_at <= coalesce(c.active_ts, c.finished_ts)
  ) tt ON true
  LEFT JOIN LATERAL (
    SELECT count(*) FILTER (WHERE kind = 'verdict'
                              AND verdict = 'blocked') AS blocked,
           array_agg(DISTINCT sha) FILTER (WHERE kind = 'frozen'
                                             AND sha IS NOT NULL
                                             AND sha <> '') AS heads
      FROM work_item_milestones m
     WHERE m.work_item_id = c.id
       AND m.at <= c.finished_ts
       AND (c.started_ts IS NULL OR m.at >= c.started_ts)
  ) mm ON true
 ORDER BY c.id
"""

#: Les attestations de la fenêtre, pour la couverture du staging (événements,
#: références, unités rapprochées, références séparées).
_STAGED_SQL = """
SELECT a.id AS event_id, x.value::bigint AS lot,
       extract(epoch FROM a.created_at) AS at
  FROM ameesh_events a
  LEFT JOIN LATERAL jsonb_array_elements_text(a.data->'lots') x(value) ON true
 WHERE a.type = 'staging.attested'
   AND a.project = %s
   AND a.created_at >= to_timestamp(%s)
   AND a.created_at < to_timestamp(%s)
   AND (x.value IS NULL OR x.value ~ '^[0-9]+$')
 ORDER BY a.created_at, a.id, lot
"""

#: Le livre comptable par harnais, toute la fenêtre. `app = ''` lit le mesh
#: entier (l'étude le nomme « tous projets du mesh pour le livre comptable ») ;
#: un projet ne lit que ses lignes attribuées. Les lignes écartées
#: (`void_reason`) sont comptées, jamais dans les sommes.
_PERIOD_SQL = """
SELECT c.harness,
       count(*) AS rows,
       count(c.work_item_id) AS with_lot,
       count(*) FILTER (WHERE c.void_reason IS NOT NULL) AS void_rows,
       count(*) FILTER (WHERE c.void_reason IS NULL) AS cost_rows,
       sum(c.usd) FILTER (WHERE c.void_reason IS NULL) AS usd,
       sum(c.input_tokens) FILTER (WHERE c.void_reason IS NULL) AS input_tokens,
       sum(c.cached_input_tokens) FILTER (WHERE c.void_reason IS NULL)
         AS cached_input_tokens,
       sum(c.cache_write_tokens) FILTER (WHERE c.void_reason IS NULL)
         AS cache_write_tokens,
       sum(c.output_tokens) FILTER (WHERE c.void_reason IS NULL) AS output_tokens,
       sum(c.input_tokens + CASE WHEN c.harness = 'codex' THEN 0
                                  ELSE c.cached_input_tokens END)
         FILTER (WHERE c.void_reason IS NULL) AS prompt,
       count(*) FILTER (WHERE c.void_reason IS NULL
                         AND (c.model IS NULL OR c.model = '')) AS model_unknown,
       count(*) FILTER (WHERE c.void_reason IS NULL
                         AND (c.billing_account IS NULL
                              OR c.billing_account = ''))
         AS billing_account_unknown,
       count(DISTINCT c.turn_id) AS turn_ids
  FROM turn_costs c
  LEFT JOIN work_items w ON w.id = c.work_item_id
 WHERE c.recorded_at >= to_timestamp(%s)
   AND c.recorded_at < to_timestamp(%s)
   AND (%s = '' OR w.app = %s)
 GROUP BY c.harness
 ORDER BY c.harness
"""

#: Verdicts et gels de la fenêtre, famille du reçu exact (`milestone:<id>`).
_REVIEW_SQL = """
SELECT m.kind, coalesce(m.verdict, '') AS verdict,
       coalesce(p.harness, 'unknown') AS family,
       count(*) AS n, count(DISTINCT m.work_item_id) AS lots
  FROM work_item_milestones m
  JOIN work_items w ON w.id = m.work_item_id
  LEFT JOIN provenance_receipts p ON p.subject = 'milestone:' || m.id
 WHERE w.app = %s
   AND m.kind IN ('frozen', 'verdict')
   AND m.at >= to_timestamp(%s)
   AND m.at < to_timestamp(%s)
 GROUP BY 1, 2, 3
 ORDER BY 1, 2, 3
"""

#: Les sièges notifiés, lus seulement dans la charge de désignation de la
#: boîte, jamais dans le corps. `reviewer` y est la famille du siège.
_DESIGNATED_SQL = """
SELECT payload->'review_designation'->>'reviewer' AS family,
       count(*) AS mails,
       count(DISTINCT (payload->'review_designation'->>'lot',
                       payload->'review_designation'->>'freeze',
                       recipient)) AS seats
  FROM agent_mailbox
 WHERE payload ? 'review_designation'
   AND created_at >= to_timestamp(%s)
   AND created_at < to_timestamp(%s)
   AND work_item_id IN (SELECT id::text FROM work_items WHERE app = %s)
 GROUP BY 1
 ORDER BY 1
"""


def _number(value: Any) -> float | None:
    """Un nombre (entier ou décimal), ou None : une chaîne illisible n'est
    pas zéro."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _head_count(value: Any) -> int:
    """Le nombre de textes SHA distincts d'un gel ; une liste vide vaut 0."""
    if isinstance(value, (list, tuple, set)):
        return len({str(item).strip() for item in value
                    if str(item or "").strip()})
    return max(0, int(_int(value) or 0))


def read(db, *, project: str = DEFAULT_PROJECT, now: Any = None,
         days: int = DEFAULT_DAYS, start: str | None = None,
         end: str | None = None) -> dict[str, Any]:
    """Lit les mesures de la fenêtre et rend le rapport (`build`).

    Toutes les requêtes sont des SELECT, exécutées dans une transaction
    `READ ONLY`. Aucune limite de lignes n'est appliquée : une fenêtre de
    14 jours reste bornée par ses bornes, pas par un `LIMIT` silencieux.
    """
    at = _instant(now) if now is not None else float(int(time.time()))
    start_ts, end_ts = window(now=at, days=days, start=start, end=end)
    with db.transaction() as tx:
        tx.execute("SET TRANSACTION READ ONLY")
        results = tx.query_batch([
            (_UNITS_SQL, (start_ts, end_ts, project, project)),
            (_STAGED_SQL, (project, start_ts, end_ts)),
            (_PERIOD_SQL, (start_ts, end_ts, project, project)),
            (_PERIOD_SQL, (start_ts, end_ts, "", "")),
            (_REVIEW_SQL, (project, start_ts, end_ts)),
            (_DESIGNATED_SQL, (start_ts, end_ts, project)),
        ])
    units, staged, project_period, mesh_period, reviews, designated = results
    return build(
        project=project, at=at, start=start_ts, end=end_ts,
        units=units, staged=staged,
        period_project=project_period, period_mesh=mesh_period,
        reviews=reviews, designated=designated,
    )


# -- calcul pur, témoin-testable sans base -----------------------------------

def _payload(note: Any) -> Mapping[str, Any] | None:
    """La charge JSON d'une note `review-designation:` ; None si illisible."""
    if not isinstance(note, str):
        return None
    head, separator, tail = note.partition(": ")
    if not separator or "review-designation" not in head:
        return None
    try:
        value = json.loads(tail)
    except ValueError:
        return None
    return value if isinstance(value, Mapping) else None


def family_of(notes: Sequence[Any] | None) -> tuple[str, str | None]:
    """La famille historique d'une unité, depuis ses notes de désignation.

    `notes` est rendu de la plus récente à la plus ancienne (comme la requête).
    La première note `designated` valide fait foi : elle est la dernière
    désignation antérieure à l'intégration. Rend `(famille, raison)`, la
    raison n'étant renseignée que pour `unknown`.
    """
    if not notes:
        return "unknown", UNKNOWN["family"]
    for note in notes:
        payload = _payload(note)
        if payload is None or str(payload.get("state") or "") != "designated":
            continue
        authors = payload.get("authors")
        unknown = payload.get("unknown_authors") or []
        if not isinstance(authors, list) or not authors:
            return "unknown", UNKNOWN["family"]
        if isinstance(unknown, list) and any(str(value).strip()
                                             for value in unknown):
            return "unknown", UNKNOWN["family"]
        families: set[str] = set()
        missing = False
        for author in authors:
            if not isinstance(author, Mapping):
                missing = True
                continue
            proved = author.get("families")
            values = ([str(value).strip() for value in proved
                       if str(value or "").strip()]
                      if isinstance(proved, list) else [])
            if values:
                families.update(values)
            else:
                single = str(author.get("family") or "").strip()
                if single:
                    families.add(single)
                else:
                    missing = True
        if missing or not families:
            return "unknown", UNKNOWN["family"]
        if len(families) == 1:
            return next(iter(families)), None
        return "mixed", None
    return "unknown", UNKNOWN["family"]


def _unit(row: Mapping[str, Any]) -> dict[str, Any]:
    """Une unité calculée depuis une ligne de `_UNITS_SQL`."""
    started = _number(row.get("started_at"))
    finished = _number(row.get("finished_at"))
    active = _number(row.get("active_at"))
    minutes = _number(row.get("estimate_minutes"))
    ratio = None
    if (started is not None and finished is not None and minutes is not None
            and minutes > 0 and finished >= started):
        ratio = (finished - started) / 60.0 / minutes
    family, family_unknown = family_of(row.get("notes"))
    heads = _head_count(row.get("distinct_heads"))
    return {
        "id": _int(row.get("id")),
        "type": str(row.get("type") or ""),
        "size": str(row.get("change_size") or "unknown"),
        "branch": str(row.get("branch") or ""),
        "family": family,
        "family_unknown": family_unknown,
        "started_at": started,
        "finished_at": finished,
        "active_at": active,
        "estimate_minutes": minutes,
        "estimated_by": row.get("estimated_by"),
        "ratio": ratio,
        "ratio_unknown": UNKNOWN["ratio"] if ratio is None else None,
        "blocked": int(_int(row.get("blocked")) or 0),
        "heads": heads,
        "refreezes": max(0, heads - 1),
        "turns": _int(row.get("turns")),
        "usd": _number(row.get("usd")) if (_int(row.get("cost_rows")) or 0) else None,
        "cost_rows": int(_int(row.get("cost_rows")) or 0),
        "void_rows": int(_int(row.get("void_rows")) or 0),
        "prompt": _number(row.get("prompt")),
        "output": _number(row.get("output_tokens")),
        "input_tokens": _number(row.get("input_tokens")),
        "cached_input_tokens": _number(row.get("cached_input_tokens")),
        "cache_write_tokens": _number(row.get("cache_write_tokens")),
        "model_unknown": int(_int(row.get("model_unknown")) or 0),
        "active_turns": _int(row.get("active_turns")),
        "active_usd": (_number(row.get("active_usd"))
                       if (_int(row.get("active_cost_rows")) or 0) else None),
        "active_cost_rows": int(_int(row.get("active_cost_rows")) or 0),
        "active_void_rows": int(_int(row.get("active_void_rows")) or 0),
        "active_prompt": _number(row.get("active_prompt")),
        "active_output": _number(row.get("active_output_tokens")),
        "active_input_tokens": _number(row.get("active_input_tokens")),
        "active_cached_input_tokens": _number(
            row.get("active_cached_input_tokens")),
        "active_cache_write_tokens": _number(
            row.get("active_cache_write_tokens")),
        "active_model_unknown": int(_int(row.get("active_model_unknown")) or 0),
        # la phase de coût publiée avec la ligne : sur une cohorte, le coût
        # d'intégration s'arrête à la fusion, celui du staging à l'attestation
        "cost": {
            "integration": {"turns": _int(row.get("turns")),
                            "usd": (_number(row.get("usd"))
                                    if (_int(row.get("cost_rows")) or 0)
                                    else None),
                            "prompt": _number(row.get("prompt")),
                            "output": _number(row.get("output_tokens"))},
            "staging": {"turns": _int(row.get("active_turns")),
                        "usd": (_number(row.get("active_usd"))
                                if (_int(row.get("active_cost_rows")) or 0)
                                else None),
                        "prompt": _number(row.get("active_prompt")),
                        "output": _number(row.get("active_output_tokens"))},
        },
    }


def _mean(values: Iterable[float | None]) -> tuple[float | None, int]:
    known = [value for value in values if value is not None]
    if not known:
        return None, 0
    return round(sum(known) / len(known), 3), len(known)


def _total(values: Iterable[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    if not known:
        return None
    return round(sum(known), 6)


def _percentile(values: Iterable[float | None], q: float) -> float | None:
    known = [float(value) for value in values if value is not None]
    return estimates.quantile(known, q)


def _group(units: Sequence[Mapping[str, Any]], key) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for unit in units:
        groups.setdefault(str(key(unit)), []).append(unit)
    return [dict(name=name, **_family_row(members))
            for name, members in groups.items()]


def _family_row(units: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Une ligne de tableau (famille, type, taille) et ses dénominateurs."""
    ratios = [unit["ratio"] for unit in units]
    turns = [unit["turns"] for unit in units]
    usd = [unit["usd"] for unit in units]
    prompt = [unit["prompt"] for unit in units]
    output = [unit["output"] for unit in units]
    median_usd = _percentile(usd, 0.5)
    mean_usd, usd_known = _mean(usd)
    mean_turns, turns_known = _mean(turns)
    mean_prompt, prompt_known = _mean(prompt)
    mean_output, output_known = _mean(output)
    return {
        "lots": len(units),
        "ratios_known": sum(1 for value in ratios if value is not None),
        "median_ratio": _percentile(ratios, 0.5),
        "p80_ratio": _percentile(ratios, 0.8),
        "turns": _total(turns),
        "lots_with_turns": turns_known,
        "mean_turns": mean_turns,
        "usd": _total(usd),
        "lots_with_usd": usd_known,
        "mean_usd": mean_usd,
        "median_usd": median_usd,
        "prompt": _total(prompt),
        "lots_with_prompt": prompt_known,
        "mean_prompt": mean_prompt,
        "output": _total(output),
        "lots_with_output": output_known,
        "mean_output": mean_output,
        "blocked_rows": sum(int(unit["blocked"]) for unit in units),
        "lots_blocked": sum(1 for unit in units if int(unit["blocked"]) > 0),
        "refreezes": sum(int(unit["refreezes"]) for unit in units),
        "units_with_refreezes": sum(1 for unit in units
                                    if int(unit["refreezes"]) > 0),
        "class_unknown": len(units),
        "effort_unknown": len(units),
        "size_unknown": sum(1 for unit in units
                            if unit["size"] not in ("s", "m", "l")),
        "b_justes": None,
        "b_justes_unknown": sum(int(unit["blocked"]) for unit in units),
        "unknown": {
            "ratio": sum(1 for value in ratios if value is None),
            "family": sum(1 for unit in units
                          if unit["family"] == "unknown"),
            "turns": sum(1 for value in turns if value is None),
            "usd": sum(1 for value in usd if value is None),
            "prompt": sum(1 for value in prompt if value is None),
            "class": len(units),
            "effort": len(units),
        },
    }


def _family_rows(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = _group(units, lambda unit: unit["family"])
    return sorted(rows, key=lambda row: (
        FAMILY_ORDER.index(row["name"]) if row["name"] in FAMILY_ORDER
        else len(FAMILY_ORDER), row["name"]))


def _type_rows(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = _group(units, lambda unit: unit["type"] or "unknown")
    return sorted(rows, key=lambda row: (
        TYPE_ORDER.index(row["name"]) if row["name"] in TYPE_ORDER
        else len(TYPE_ORDER), row["name"]))


def _size_rows(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = _group(units, lambda unit: unit["size"] or "unknown")
    return sorted(rows, key=lambda row: (
        SIZE_ORDER.index(row["name"]) if row["name"] in SIZE_ORDER
        else len(SIZE_ORDER), row["name"]))


def _cross_rows(units: Sequence[Mapping[str, Any]], key) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for unit in units:
        groups.setdefault(tuple(key(unit)), []).append(unit)
    rows = []
    for parts, members in groups.items():
        row = dict(name=" × ".join(parts), **_family_row(members))
        row["parts"] = list(parts)
        rows.append(row)
    return sorted(rows, key=lambda row: (
        FAMILY_ORDER.index(row["parts"][0]) if row["parts"][0] in FAMILY_ORDER
        else len(FAMILY_ORDER), row["parts"]))


def _cohort(units: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    row = _family_row(units)
    return {
        "units": row["lots"],
        "ratios_known": row["ratios_known"],
        "median_ratio": row["median_ratio"],
        "p80_ratio": row["p80_ratio"],
        "turns": row["turns"],
        "lots_with_turns": row["lots_with_turns"],
        "usd": row["usd"],
        "lots_with_usd": row["lots_with_usd"],
        "mean_usd": row["mean_usd"],
        "median_usd": row["median_usd"],
        "blocked_rows": row["blocked_rows"],
        "lots_blocked": row["lots_blocked"],
        "refreezes": row["refreezes"],
    }


def _activity(rows: Sequence[Mapping[str, Any]] | None
              ) -> list[dict[str, Any]] | None:
    """Le livre par harnais, avec la convention de cache et `void` à part."""
    if rows is None:
        return None
    out = []
    for row in rows:
        usd = _number(row.get("usd"))
        cost_rows = int(_int(row.get("cost_rows")) or 0)
        out.append({
            "harness": str(row.get("harness") or ""),
            "rows": int(_int(row.get("rows")) or 0),
            "with_lot": int(_int(row.get("with_lot")) or 0),
            "cost_rows": cost_rows,
            "void_rows": int(_int(row.get("void_rows")) or 0),
            "usd": usd if cost_rows else None,
            "input_tokens": _number(row.get("input_tokens")),
            "cached_input_tokens": _number(row.get("cached_input_tokens")),
            "cache_write_tokens": _number(row.get("cache_write_tokens")),
            "output_tokens": _number(row.get("output_tokens")),
            "prompt": _number(row.get("prompt")),
            "model_unknown": int(_int(row.get("model_unknown")) or 0),
            "billing_account_unknown": int(
                _int(row.get("billing_account_unknown")) or 0),
            "turn_ids": int(_int(row.get("turn_ids")) or 0),
        })
    return out


def _review(rows: Sequence[Mapping[str, Any]] | None,
            designated: Sequence[Mapping[str, Any]] | None,
            ) -> dict[str, Any]:
    """Les reçus de verdict, leur famille, et les sièges notifiés à part."""
    if rows is None:
        return {"by_family": None,
                "notified_unattributed": None,
                "unknown_receipt_family": None,
                "b_justes": None,
                "b_justes_reason": UNKNOWN["b_justes"],
                "unknown": UNKNOWN["review_family"]}
    verdicts: dict[str, dict[str, int]] = {}
    for row in rows or ():
        family = str(row.get("family") or "unknown")
        entry = verdicts.setdefault(family, {"frozen": 0, "ok": 0,
                                            "blocked": 0, "lots": 0})
        count = int(_int(row.get("n")) or 0)
        if str(row.get("kind")) == "frozen":
            entry["frozen"] += count
        elif str(row.get("verdict")) == "blocked":
            entry["blocked"] += count
        elif str(row.get("verdict")) == "ok":
            entry["ok"] += count
        entry["lots"] = max(entry["lots"], int(_int(row.get("lots")) or 0))
    notified: dict[str, dict[str, int]] = {}
    unattributed_mails = 0
    unattributed_seats = 0
    for row in designated or ():
        family = str(row.get("family") or "").strip()
        mails = int(_int(row.get("mails")) or 0)
        seats = int(_int(row.get("seats")) or 0)
        if not family:
            unattributed_mails += mails
            unattributed_seats += seats
            continue
        notified[family] = {"mails": mails, "seats": seats}
    families = []
    for family in sorted(set(verdicts) | set(notified)):
        entry = verdicts.get(family) or {"frozen": 0, "ok": 0,
                                         "blocked": 0, "lots": 0}
        lines = entry["ok"] + entry["blocked"]
        families.append({
            "family": family,
            "frozen": entry["frozen"],
            "ok": entry["ok"],
            "blocked": entry["blocked"],
            "verdict_lines": lines,
            "blocked_rate": (round(entry["blocked"] / lines, 4)
                             if lines else None),
            "lots": entry["lots"],
            "notified_mails": (notified.get(family) or {}).get("mails", 0),
            "notified_seats": (notified.get(family) or {}).get("seats", 0),
            "b_justes": None,
            "b_justes_unknown": entry["blocked"],
        })
    return {
        "by_family": families,
        "notified_unattributed": {"mails": unattributed_mails,
                                  "seats": unattributed_seats},
        "unknown_receipt_family": {
            "verdicts": verdicts.get("unknown", {}).get("ok", 0)
            + verdicts.get("unknown", {}).get("blocked", 0),
            "name": UNKNOWN["review_family"],
        },
        "b_justes": None,
        "b_justes_reason": UNKNOWN["b_justes"],
    }


def _staging_coverage(staged: Sequence[Mapping[str, Any]] | None,
                      units: Sequence[Mapping[str, Any]] | None,
                      staging: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Rapproche les attestations des unités, sans conclure une livraison."""
    if staged is None or units is None:
        return {"events": None, "lots": None, "matched": None,
                "matched_ratio": None, "carrier": None,
                "attestation_before_integration": None,
                "no_eligible_integration": None,
                "unknown": UNKNOWN["staging"]}
    by_id = {unit["id"]: unit for unit in units}
    lots_seen = {int(row.get("lot")) for row in staged
                 if row.get("lot") is not None}
    matched = sum(1 for unit in staging if unit["id"] in lots_seen)
    carrier, before, no_eligible = [], [], []
    for lot in sorted(lots_seen):
        unit = by_id.get(lot)
        if unit is None:
            no_eligible.append(lot)
        elif unit["branch"].startswith(("integration/", "packet/")):
            carrier.append(lot)
        elif unit["active_at"] is None:
            before.append(lot)
    lots = len(lots_seen)
    return {
        "events": len({row.get("event_id") for row in staged}),
        "lots": lots,
        "matched": matched,
        "matched_ratio": (round(matched / lots, 4) if lots else None),
        "carrier": carrier,
        "attestation_before_integration": before,
        "no_eligible_integration": no_eligible,
        "unknown": None,
    }


def build(*, project: str = DEFAULT_PROJECT, at: float,
          start: float, end: float,
          units: Sequence[Mapping[str, Any]] | None,
          staged: Sequence[Mapping[str, Any]] | None,
          period_project: Sequence[Mapping[str, Any]] | None,
          period_mesh: Sequence[Mapping[str, Any]] | None,
          reviews: Sequence[Mapping[str, Any]] | None,
          designated: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Assemble le rapport depuis les lignes lues (aucune base ici).

    Chaque source `None` signifie « non lue » : les mesures qui en dépendent
    sortent `None` avec leur raison, jamais `0`. Une séquence vide est une
    lecture réelle de zéro.
    """
    if end <= start:
        raise ModelEvaluationReportError("fenêtre vide ou inversée")

    computed_units = []
    if units is None:
        integration = staging = carriers = []
        separated = None
    else:
        computed = [_unit(row) for row in units]
        computed_units = computed
        carriers = [unit for unit in computed
                    if unit["branch"].startswith(("integration/", "packet/"))]
        integration = [unit for unit in computed
                       if not unit["branch"].startswith(
                           ("integration/", "packet/"))
                       and unit["finished_at"] is not None
                       and start <= unit["finished_at"] < end]
        staging = [unit for unit in computed
                   if not unit["branch"].startswith(
                       ("integration/", "packet/"))
                   and unit["active_at"] is not None]
        separated = [unit for unit in computed
                     if unit not in carriers and unit not in integration
                     and unit not in staging]

    # la cohorte de staging publie son coût jusqu'à l'attestation ; la cohorte
    # d'intégration, jusqu'à la fusion. Les deux phases restent nommées.
    staging_cost = []
    for unit in staging:
        clone = dict(unit)
        clone["turns"] = unit["cost"]["staging"]["turns"]
        clone["usd"] = unit["cost"]["staging"]["usd"]
        clone["prompt"] = unit["cost"]["staging"]["prompt"]
        clone["output"] = unit["cost"]["staging"]["output"]
        staging_cost.append(clone)
    integration_cost = []
    for unit in integration:
        clone = dict(unit)
        clone["turns"] = unit["cost"]["integration"]["turns"]
        clone["usd"] = unit["cost"]["integration"]["usd"]
        clone["prompt"] = unit["cost"]["integration"]["prompt"]
        clone["output"] = unit["cost"]["integration"]["output"]
        integration_cost.append(clone)
    carrier_cost = []
    for unit in carriers:
        clone = dict(unit)
        clone["turns"] = unit["cost"]["integration"]["turns"]
        clone["usd"] = unit["cost"]["integration"]["usd"]
        clone["prompt"] = unit["cost"]["integration"]["prompt"]
        clone["output"] = unit["cost"]["integration"]["output"]
        carrier_cost.append(clone)

    families_staging = _family_rows(staging_cost)
    families_integration = _family_rows(integration_cost)
    families_carriers = _family_rows(carrier_cost)
    by_type_staging = _type_rows(staging_cost)
    by_type_integration = _type_rows(integration_cost)
    by_size_staging = _size_rows(staging_cost)
    by_size_integration = _size_rows(integration_cost)
    by_family_type = _cross_rows(
        staging_cost, lambda unit: (unit["family"], unit["type"] or "unknown"))
    by_family_size = _cross_rows(
        staging_cost, lambda unit: (unit["family"], unit["size"] or "unknown"))
    by_family_type_size = _cross_rows(
        staging_cost, lambda unit: (unit["family"], unit["type"] or "unknown",
                                    unit["size"] or "unknown"))

    cohort = {
        "candidates": len(units) if units is not None else None,
        "integration": _cohort(integration_cost),
        "staging": _cohort(staging_cost),
        "carriers": _cohort(carrier_cost),
        "separated": len(separated) if separated is not None else None,
    }
    coverage = {
        "window": {
            "start": iso_z(start), "end": iso_z(end),
            "days": (int((end - start) / 86400.0)
                     if (end - start) % 86400 == 0
                     else round((end - start) / 86400.0, 3)),
            "timezone": "UTC", "end_exclusive": True,
        },
        "denominators": {
            "candidates": cohort["candidates"],
            "integration": cohort["integration"]["units"],
            "staging": cohort["staging"]["units"],
            "carriers": cohort["carriers"]["units"],
            "staging_ratios_known": cohort["staging"]["ratios_known"],
            "integration_ratios_known": cohort["integration"]["ratios_known"],
            "staging_usd_known": cohort["staging"]["lots_with_usd"],
            "staging_class_unknown": cohort["staging"]["units"],
            "staging_effort_unknown": cohort["staging"]["units"],
            "b_justes_known": 0,
        },
        "staging_attestations": _staging_coverage(
            staged, computed_units if units is not None else None, staging),
        "unknown": dict(UNKNOWN),
        "families_absent": [
            family for family in FAMILY_ORDER
            if family not in {row["name"] for row in families_staging}
        ],
        "rules": [{"id": ident, "rule": text}
                  for ident, text in COVERAGE_RULES],
        "limits": list(LIMITS),
    }

    activity = {
        "project": _activity(period_project),
        "mesh": _activity(period_mesh),
        "usd_is_invoice": False,
        "usd_basis": (
            "estimation de consommation du livre comptable ; ni facture ni "
            "dépense marginale comparable entre forfait et paiement au jeton"),
    }
    review = _review(reviews, designated)

    tables = {
        "families": {"integration": families_integration,
                     "staging": families_staging,
                     "carriers": families_carriers},
        "by_type": {"integration": by_type_integration,
                    "staging": by_type_staging},
        "by_size": {"integration": by_size_integration,
                    "staging": by_size_staging},
        "by_family_type": by_family_type,
        "by_family_size": by_family_size,
        "by_family_type_size": by_family_type_size,
        "review": review["by_family"],
        "activity": activity,
    }

    report = {
        "schema": SCHEMA,
        "source": SOURCE,
        "calculation_version": CALCULATION_VERSION,
        "project": project,
        "at": iso_z(at),
        "read_only": True,
        "window": coverage["window"],
        "cohorts": {
            "candidates": cohort["candidates"],
            "integration": cohort["integration"],
            "staging": cohort["staging"],
            "carriers": cohort["carriers"],
            "separated": cohort["separated"],
        },
        "activity": activity,
        "families": {"integration": families_integration,
                     "staging": families_staging,
                     "carriers": families_carriers},
        "cross": {
            "integration": {"by_type": by_type_integration,
                            "by_size": by_size_integration},
            "staging": {"by_type": by_type_staging,
                        "by_size": by_size_staging,
                        "by_family_type": by_family_type,
                        "by_family_size": by_family_size,
                        "by_family_type_size": by_family_type_size},
        },
        "review": review,
        "coverage": coverage,
        "tables": tables,
    }
    report["unknown_count"] = sum(
        len(row.get("unknown") or {}) for row in families_staging
    )
    return report


# -- texte -------------------------------------------------------------------

def _fmt(value: Any) -> str:
    if value is None:
        return "inconnu"
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        return ("%.3f" % value).rstrip("0").rstrip(".")
    return str(value)


def _ratio(value: Any) -> str:
    return "—" if value is None else ("%.3f" % float(value)).rstrip("0").rstrip(".")


def _family_table(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    out = [
        "  %-9s %5s %7s %8s %7s %9s %9s %9s"
        % ("FAMILLE", "LOTS", "RATIOS", "MÉDIANE", "P80", "TOURS/LOT",
           "USD MOY", "USD MÉD"),
    ]
    for row in rows:
        out.append(
            "  %-9s %5d %7d %8s %7s %9s %9s %9s"
            % (row.get("name"), row.get("lots"), row.get("ratios_known"),
               _ratio(row.get("median_ratio")), _ratio(row.get("p80_ratio")),
               _fmt(row.get("mean_turns")), _fmt(row.get("mean_usd")),
               _fmt(row.get("median_usd"))))
    if not rows:
        out.append("  (aucune unité dans cette cohorte)")
    return out


def format_text(report: Mapping[str, Any]) -> str:
    """Le rapport lisible : cohortes, familles, revue, couverture."""
    window = report.get("window") or {}
    cohorts = report.get("cohorts") or {}
    lines = [
        "Évaluation des modèles — %s — lecture seule"
        % report.get("project"),
        "Fenêtre UTC [%s, %s[ (%s j), relevé %s"
        % (window.get("start"), window.get("end"), window.get("days"),
           report.get("at")),
        "Cohortes : %s unité(s) candidate(s), %s intégrée(s), %s attestée(s), "
        "%s porteur(s) séparé(s)"
        % (_fmt(cohorts.get("candidates")),
           _fmt((cohorts.get("integration") or {}).get("units")),
           _fmt((cohorts.get("staging") or {}).get("units")),
           _fmt((cohorts.get("carriers") or {}).get("units"))),
        "",
        "Unités attestées (phase début → première attestation compatible)",
    ]
    lines += _family_table((report.get("families") or {}).get("staging") or ())
    lines += ["", "Unités intégrées (phase début → première fusion)",
              ]
    lines += _family_table((report.get("families") or {}).get("integration") or ())
    lines += ["", "Porteurs, séparés des unités"]
    lines += _family_table((report.get("families") or {}).get("carriers") or ())
    review = report.get("review") or {}
    lines += ["", "Relecture : reçus et sièges notifiés"]
    lines.append("  %-9s %8s %6s %9s %10s %10s"
                 % ("FAMILLE", "SIÈGES", "B", "OK", "B/LIGNES", "B JUSTES"))
    for row in review.get("by_family") or ():
        lines.append("  %-9s %8s %6s %9s %10s %10s"
                     % (row.get("family"), _fmt(row.get("notified_seats")),
                        _fmt(row.get("blocked")), _fmt(row.get("ok")),
                        _fmt(row.get("blocked_rate")), "inconnu"))
    unattributed = review.get("notified_unattributed") or {}
    if unattributed.get("seats"):
        lines.append("  notification(s) sans famille : %s siège(s), hors des "
                     "trois lignes" % _fmt(unattributed.get("seats")))
    activity = report.get("activity") or {}
    lines += ["", "Livre comptable du projet (USD = estimation, pas facture)"]
    lines.append("  %-9s %7s %7s %9s %9s %10s"
                 % ("HARNAIS", "LIGNES", "ÉCARTÉES", "USD", "ENTRÉES", "SORTIES"))
    for row in activity.get("project") or ():
        lines.append("  %-9s %7s %7s %9s %9s %10s"
                     % (row.get("harness"), _fmt(row.get("rows")),
                        _fmt(row.get("void_rows")), _fmt(row.get("usd")),
                        _fmt(row.get("prompt")), _fmt(row.get("output_tokens"))))
    coverage = report.get("coverage") or {}
    lines += ["", "Couverture"]
    for key, value in (coverage.get("denominators") or {}).items():
        lines.append("  %-28s %s" % (key, _fmt(value)))
    staging = coverage.get("staging_attestations") or {}
    lines.append("  %-28s %s événement(s), %s référence(s), %s rapprochée(s)"
                 % ("attestations staging", _fmt(staging.get("events")),
                    _fmt(staging.get("lots")), _fmt(staging.get("matched"))))
    lines += [
        "",
        "B justes : inconnu ; classe et effort exécuté : inconnus ; "
        "USD ≠ facture ; données absentes ≠ zéro.",
        "Ce rapport ne conclut aucune causalité et ne produit aucun conseil "
        "de routage.",
    ]
    return "\n".join(lines)


# -- CLI ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh model-report",
        description="Rapport en lecture seule d'évaluation des modèles "
                    "(étude #6650, unité M1) : fenêtre UTC fixe, cohortes "
                    "séparées, familles historiques, inconnues déclarées.",
    )
    parser.add_argument("--json", action="store_true",
                        help="sortir le schéma %s" % SCHEMA)
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help="projet des unités (défaut : %s)" % DEFAULT_PROJECT)
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
        # Valider la fenêtre avant de toucher la base : un instant sans fuseau
        # ou une fenêtre vide sont des erreurs d'argument (code 2).
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
            print(json.dumps(report, ensure_ascii=False, indent=2,
                             sort_keys=True))
        else:
            print(format_text(report))
        return 0
    except ModelEvaluationReportError as exc:
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

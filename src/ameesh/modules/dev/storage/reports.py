# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 (étude #7616 §4.6) : les tables `dev` de l'étude de modèles.

Le rapport `model_evaluation_report` garde le calcul et les lectures du noyau ;
ce domaine porte le SQL des tables du module `dev` qu'il lit : reçus de
provenance, journal des résolutions de constats et runs de CI. Les noms
restent logiques (`ci_runs`, `provenance_receipts`,
`review_finding_resolutions`, `ci_test_failures`) : `ModuleDb` les qualifie au
registre `module_schemas` (`schema.relation`), donc le même code passe avant et
après P2 (#7651).
"""
from __future__ import annotations

from .schema import DevDomain, qualify, relation

#: Verdicts et gels de la fenêtre, famille du reçu exact (`milestone:<id>`).
#: #6674 : la ligne `family_total` (ensemble de regroupement par famille seule)
#: porte l'union des lots de la famille ; deux lots disjoints OK et B en font 2.
_REVIEW_SQL = """
SELECT m.kind, coalesce(m.verdict, '') AS verdict,
       coalesce(p.harness, 'unknown') AS family,
       count(*) AS n, count(DISTINCT m.work_item_id) AS lots,
       GROUPING(m.kind) = 1 AS family_total
  FROM work_item_milestones m
  JOIN work_items w ON w.id = m.work_item_id
  LEFT JOIN provenance_receipts p ON p.subject = 'milestone:' || m.id
 WHERE w.app = %s
   AND m.kind IN ('frozen', 'verdict')
   AND m.at >= to_timestamp(%s)
   AND m.at < to_timestamp(%s)
 GROUP BY GROUPING SETS (
       (m.kind, coalesce(m.verdict, ''), coalesce(p.harness, 'unknown')),
       (coalesce(p.harness, 'unknown')))
 ORDER BY 3, 1, 2
"""

#: #6669 (M3) : les constats (verdicts bloqués) de la fenêtre et l'état
#: effectif de leur résolution (dernière ligne du journal 0564 **antérieure à
#: la fin de fenêtre**, pour qu'une fenêtre passée se rejoue). Une absence de
#: ligne reste un constat non tranché ; un OK postérieur ne tranche rien.
_RESOLUTIONS_SQL = """
SELECT m.id AS milestone_id, m.work_item_id AS lot,
       lower(btrim(m.sha)) AS sha,
       coalesce(p.harness, 'unknown') AS family,
       r.state, r.basis, r.adjudicator, r.independent
  FROM work_item_milestones m
  JOIN work_items w ON w.id = m.work_item_id
  LEFT JOIN provenance_receipts p ON p.subject = 'milestone:' || m.id
  LEFT JOIN LATERAL (
      SELECT state, basis, adjudicator, independent
        FROM review_finding_resolutions r
       WHERE r.milestone_id = m.id
         AND r.created_at < to_timestamp(%s)
       ORDER BY r.id DESC LIMIT 1
  ) r ON true
 WHERE w.app = %s
   AND m.kind = 'verdict' AND m.verdict = 'blocked'
   AND m.at >= to_timestamp(%s)
   AND m.at < to_timestamp(%s)
 ORDER BY m.id
"""

#: M5 — un verdict par ligne : famille du reçu exact du relecteur, et les
#: marques de désignation du lot jusqu'au verdict (le gel est choisi en Python
#: par texte SHA exact, la marque n'est jamais lue dans un gel voisin).
_VERDICTS_SQL = """
SELECT m.id, m.work_item_id AS lot, coalesce(w.branch, '') AS branch,
       m.sha, m.actor, m.verdict,
       coalesce(p.harness, '') AS reviewer_family,
       extract(epoch FROM m.at) AS at,
       d.notes
  FROM work_item_milestones m
  JOIN work_items w ON w.id = m.work_item_id
  LEFT JOIN provenance_receipts p ON p.subject = 'milestone:' || m.id
  LEFT JOIN LATERAL (
    SELECT array_agg(e.note ORDER BY e.created_at DESC, e.id DESC) AS notes
      FROM work_item_events e
     WHERE e.work_item_id = m.work_item_id
       AND e.note LIKE 'review-designation:%%'
       AND e.created_at <= m.at
  ) d ON true
 WHERE w.app = %s
   AND m.kind = 'verdict'
   AND m.at >= to_timestamp(%s)
   AND m.at < to_timestamp(%s)
 ORDER BY m.at, m.id
"""

#: M5 — les notes de rouges de la fenêtre, la source extraite par motif (une
#: note illisible garde des liens nuls, sans faire échouer la lecture), le
#: reçu du commit et les runs CI de cette tête, toutes dates confondues.
_REDS_SQL = """
WITH notes AS (
  SELECT e.id, e.work_item_id AS lot, e.note,
         extract(epoch FROM e.created_at) AS at,
         coalesce(w.branch, '') AS branch,
         substring(e.note FROM '"source": "([0-9a-f]{40})"') AS source
    FROM work_item_events e
    JOIN work_items w ON w.id = e.work_item_id
   WHERE w.app = %s
     AND e.note LIKE 'packet-unit-red: %%'
     AND e.created_at >= to_timestamp(%s)
     AND e.created_at < to_timestamp(%s)
)
SELECT n.id, n.lot, n.note, n.at, n.branch, n.source,
       coalesce(p.harness, '') AS source_family,
       ci.runs, ci.failure, ci.success, ci.inconclusive
  FROM notes n
  LEFT JOIN provenance_receipts p ON p.subject = 'commit:' || n.source
  LEFT JOIN LATERAL (
    SELECT count(DISTINCT (r.repo, r.run_id)) AS runs,
           count(DISTINCT (r.repo, r.run_id))
             FILTER (WHERE r.status = 'failure') AS failure,
           count(DISTINCT (r.repo, r.run_id))
             FILTER (WHERE r.status = 'success') AS success,
           count(DISTINCT (r.repo, r.run_id))
             FILTER (WHERE r.status IN ('cancelled', 'superseded', 'running'))
             AS inconclusive
      FROM ci_runs r
     WHERE n.source IS NOT NULL AND r.head_sha = n.source
  ) ci ON true
 ORDER BY n.id
"""

#: M5 — runs CI de la fenêtre rattachés à un lot du projet, par statut ; les
#: runs sans lot ne sont pas attribuables à un projet et sortent à part.
_CI_SQL = """
SELECT r.status,
       (r.work_item_id IS NULL) AS without_lot,
       count(DISTINCT (r.repo, r.run_id)) AS runs,
       count(DISTINCT r.head_sha) AS heads,
       count(DISTINCT r.work_item_id) AS lots,
       coalesce(sum(f.tests), 0) AS failed_tests,
       coalesce(sum(f.inherited), 0) AS inherited_tests
  FROM ci_runs r
  LEFT JOIN work_items w ON w.id = r.work_item_id
  LEFT JOIN LATERAL (
    SELECT count(*) AS tests, count(*) FILTER (WHERE t.inherited) AS inherited
      FROM ci_test_failures t
     WHERE t.repo = r.repo AND t.run_id = r.run_id
  ) f ON true
 WHERE r.first_seen_at >= to_timestamp(%s)
   AND r.first_seen_at < to_timestamp(%s)
   AND (w.app = %s OR r.work_item_id IS NULL)
 GROUP BY 1, 2
 ORDER BY 2, 1
"""


class EvaluationReport(DevDomain):
    """Les lectures de l'étude de modèles dans les tables du module `dev`."""

    def journal_present(self) -> bool:
        """La table du journal 0564 existe-t-elle sur ce schéma ?

        Un schéma N-1 (code déployé avant la migration) reste lisible : les
        constats sont alors tous non tranchés, jamais un zéro inventé.
        `to_regclass` ne résout rien d'autre que la présence.
        """
        rows = self.db.query(
            "SELECT to_regclass(%s) IS NOT NULL AS present",
            (relation(self.db, "review_finding_resolutions"),))
        return bool((rows[0] or {}).get("present")) if rows else False

    def review_families(self, project, start_ts, end_ts):
        """Verdicts et gels de la fenêtre, famille du reçu exact."""
        return self.db.query(_REVIEW_SQL, (project, start_ts, end_ts))

    def finding_resolutions(self, project, start_ts, end_ts):
        """Constats bloqués de la fenêtre et état effectif de leur résolution.

        L'ordre des paramètres suit `_RESOLUTIONS_SQL` : la borne de lecture du
        journal (fin de fenêtre) vient avant le projet et les bornes des gels.
        """
        return self.db.query(
            _RESOLUTIONS_SQL, (end_ts, project, start_ts, end_ts))

    def verdicts(self, project, start_ts, end_ts):
        """Un verdict par ligne, avec la famille exacte du relecteur."""
        return self.db.query(_VERDICTS_SQL, (project, start_ts, end_ts))

    def reds(self, project, start_ts, end_ts):
        """Notes de rouges de la fenêtre, reçu du commit et runs de la tête."""
        return self.db.query(_REDS_SQL, (project, start_ts, end_ts))

    def ci_status(self, project, start_ts, end_ts):
        """Runs CI de la fenêtre rattachés à un lot du projet, par statut."""
        return self.db.query(_CI_SQL, (start_ts, end_ts, project))

    def batch_queries(self, project, start_ts, end_ts):
        """Les cinq paires `(sql, params)` du domaine, déjà qualifiées.

        Le rapport les place dans l'ordre de ses tableaux et les envoie avec
        ses lectures noyau en un seul `query_batch` ; la qualification déjà
        faite n'est pas redoublée par `ModuleDb.query_batch`. La cinquième
        paire (résolutions) n'est insérée que si `journal_present()` a répondu
        vrai.
        """
        return [(qualify(self.db, sql), params)
                for sql, params in (
                    (_REVIEW_SQL, (project, start_ts, end_ts)),
                    (_VERDICTS_SQL, (project, start_ts, end_ts)),
                    (_REDS_SQL, (project, start_ts, end_ts)),
                    (_CI_SQL, (start_ts, end_ts, project)),
                    (_RESOLUTIONS_SQL, (end_ts, project, start_ts, end_ts)),
                )]

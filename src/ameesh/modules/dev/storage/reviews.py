# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 : les tables de revue du module `dev`.

Deux familles de l'inventaire #7616 §3.2 :

* `review_finding_resolutions` — le journal additif des constats de revue
  (#6669), écrit et relu par `review_finding` et par l'étude de modèles ;
* `work_review_withdrawals` — les retraits de relecteur (#5087), lus par le
  report des relecteurs.

Le noyau garde la décision, les messages et le journal du lot ; ce domaine
porte le SQL des tables. Le nom physique du schéma est lu au registre
(`schema.relation`), donc le même code passe avant et après P2 (#7651).
"""
from __future__ import annotations

import json

from .schema import DevDomain, relation


class ReviewFindings(DevDomain):
    """`review_finding_resolutions` : l'état effectif d'un constat (#6669)."""

    def journal_present(self) -> bool:
        """La table du journal 0564 existe-t-elle sur ce schéma ?

        Un schéma N-1 (code déployé avant la migration) reste lisible : les
        constats sont alors tous non tranchés. Une écriture, elle, refuse
        clairement au lieu d'échouer sur une table absente.
        """
        rows = self.db.query(
            "SELECT to_regclass(%s) IS NOT NULL AS present",
            (relation(self.db, "review_finding_resolutions"),))
        return bool((rows[0] or {}).get("present")) if rows else False

    def add(self, *, milestone_id, work_item_id, witness_sha, state, basis,
            correction_sha, regression_lot, corrector, withdrawn_by,
            adjudicator, adjudicator_family, judged_families, independent,
            review_room, note) -> int:
        """Écrit une résolution et rend son identifiant (transaction du lot)."""
        rows = self.db.query(
            "INSERT INTO review_finding_resolutions ("
            " milestone_id, work_item_id, witness_sha, state, basis,"
            " correction_sha, regression_lot, corrector, withdrawn_by,"
            " adjudicator, adjudicator_family, judged_families, independent,"
            " review_room, note)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            "         %s::jsonb, %s) RETURNING id",
            (int(milestone_id), int(work_item_id), witness_sha, state, basis,
             correction_sha or None, regression_lot, corrector, withdrawn_by,
             adjudicator, adjudicator_family, sorted(judged_families),
             bool(independent),
             json.dumps(review_room, ensure_ascii=False)
             if review_room is not None else None,
             note))
        return int(rows[0]["id"])

    def for_lot(self, lot) -> list[dict]:
        """Les résolutions d'un lot, de la plus ancienne à la plus récente."""
        return [dict(row) for row in self.db.query(
            "SELECT *, extract(epoch FROM created_at)::float8 AS created_ts"
            "  FROM review_finding_resolutions WHERE work_item_id = %s"
            " ORDER BY id", (int(lot),))]


class ReviewWithdrawals(DevDomain):
    """`work_review_withdrawals` : qui a retiré sa relecture (#5087)."""

    def carried_reviewers(self, lot, names) -> list[str]:
        """Les relecteurs reportables : ni retirés, ni en retenue de revue.

        Rend les noms de `names` (JSON) sans retrait effectif ni `review_hold`
        du registre, en résolvant les alias d'identité. La famille de revue
        n'est jamais déduite d'un nom.
        """
        return [row["name"] for row in self.db.query(
            "SELECT n.name FROM jsonb_array_elements_text(%s::jsonb) n(name)"
            " WHERE NOT EXISTS (SELECT 1 FROM work_review_withdrawals w"
            " WHERE w.work_item_id = %s AND w.withdrawn"
            " AND w.reviewer = coalesce(ameesh_agent_primary_locked(n.name), n.name))"
            " AND NOT EXISTS (SELECT 1 FROM agent_registry a"
            " WHERE a.review_hold IS NOT NULL"
            " AND (a.name = n.name OR n.name = ANY(a.aliases)))",
            (json.dumps(list(names)), int(lot)))]

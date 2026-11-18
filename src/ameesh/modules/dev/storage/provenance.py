# SPDX-License-Identifier: AGPL-3.0-only
"""#1465 : preuves exactes d'origine, sur Postgres (migration 0445)."""
from __future__ import annotations

import json

from ....storage.provenance import Provenance as Contract
from .schema import DevDomain

_COLUMNS = """
    subject, sha, actor, session, turn, harness, work_item_id,
    extract(epoch FROM at)::float8 AS at_ts
"""

#: #6873 : la révocation, fait séparé de la preuve (la ligne 0445 ne bouge pas).
_REVOCATION_COLUMNS = """
    subject, sha, actor, reason, extract(epoch FROM at)::float8 AS at_ts
"""

#: une liste de textes passée en paramètre : `NULL` si vide, sinon un JSON
#: que l'instruction convertit en `text[]` (même idiome que `hosts.py`)
_ARRAY_SQL = "ARRAY(SELECT jsonb_array_elements_text(%s::jsonb))"


class Provenance(DevDomain, Contract):

    def __init__(self, db):
        super().__init__(db)

    def add(self, subject, sha, actor, session, turn, harness, *, work_item_id=None):
        with self.db.transaction() as tx:
            rows = tx.query(
                "INSERT INTO provenance_receipts"
                "       (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING"
                " RETURNING " + _COLUMNS,
                (subject, sha, actor, session, turn, harness, work_item_id))
            if rows:
                return dict(rows[0], recorded=True)
            # Une preuve existe déjà pour ce sujet : la première fait foi. On
            # la relit DANS la même transaction, l'appelant compare l'identité.
            rows = tx.query("SELECT " + _COLUMNS +
                            " FROM provenance_receipts WHERE subject = %s", (subject,))
            if not rows:
                # course avec un DELETE hors contrat : la table est en ajout
                # seul, cela ne devrait pas arriver ; on refuse plutôt que de
                # rendre une preuve disparue.
                raise RuntimeError("provenance receipt vanished for %s" % subject)
            return dict(rows[0], recorded=False)

    def get(self, subject):
        rows = self.db.query("SELECT " + _COLUMNS +
                             " FROM provenance_receipts WHERE subject = %s", (subject,))
        return rows[0] if rows else None

    def revocation(self, subject):
        rows = self.db.query(
            "SELECT " + _REVOCATION_COLUMNS +
            "  FROM provenance_revocations WHERE subject = %s", (subject,))
        return rows[0] if rows else None

    def revoke(self, subject, sha, actor, reason):
        with self.db.transaction() as tx:
            rows = tx.query(
                "INSERT INTO provenance_revocations (subject, sha, actor, reason)"
                " SELECT subject, sha, %s, %s"
                "   FROM provenance_receipts"
                "  WHERE subject = %s AND sha = %s"
                " ON CONFLICT (subject) DO NOTHING"
                " RETURNING " + _REVOCATION_COLUMNS,
                (actor, reason, subject, sha))
            if rows:
                return dict(rows[0], recorded=True)
            # Une révocation existe déjà (la première fait foi), ou la preuve
            # n'existe pas / ne porte pas ce SHA : l'appelant distingue les
            # deux en relisant la ligne de preuve.
            rows = tx.query(
                "SELECT " + _REVOCATION_COLUMNS +
                "  FROM provenance_revocations WHERE subject = %s", (subject,))
            if rows:
                return dict(rows[0], recorded=False)
            return None

    def milestone(self, subject):
        rows = self.db.query(
            "SELECT p.subject, p.sha, p.actor, p.session, p.turn, p.harness,"
            "       p.work_item_id, extract(epoch FROM p.at)::float8 AS at_ts,"
            "       m.id AS milestone_id, m.actor AS mark_actor, m.sha AS mark_sha,"
            "       m.verdict, extract(epoch FROM m.at)::float8 AS mark_at_ts,"
            # #4299: a rename rewrites the milestone actor, never the receipt;
            # both spellings of one registry identity are the same actor.
            "       EXISTS (SELECT 1 FROM agent_registry r"
            "                WHERE (r.name = p.actor OR p.actor = ANY(r.aliases))"
            "                  AND (r.name = m.actor OR m.actor = ANY(r.aliases)))"
            "         AS same_identity"
            "  FROM provenance_receipts p"
            "  JOIN work_item_milestones m ON p.subject = 'milestone:' || m.id"
            " WHERE p.subject = %s", (subject,))
        return rows[0] if rows else None

    def commits(self, actor, *, since_ts=None, until_ts=None, limit=None):
        sql = ("SELECT " + _COLUMNS +
               "  FROM provenance_receipts p"
               # #4299: the receipt keeps the spelling of its day; read every
               # spelling of the registry identity.
               " WHERE actor IN (SELECT %s::text UNION SELECT unnest(ARRAY[r.name] || r.aliases)"
               "                   FROM agent_registry r"
               "                  WHERE r.name = %s::text OR %s::text = ANY(r.aliases))"
               "   AND subject LIKE 'commit:%%'"
               # #6873: a revoked receipt is no longer a proof.
               "   AND NOT EXISTS (SELECT 1 FROM provenance_revocations v"
               "                    WHERE v.subject = p.subject)")
        params = [str(actor)] * 3
        if since_ts is not None:
            sql += " AND extract(epoch FROM at) >= %s"
            params.append(float(since_ts))
        if until_ts is not None:
            sql += " AND extract(epoch FROM at) <= %s"
            params.append(float(until_ts))
        sql += " ORDER BY at DESC, subject DESC"
        if limit is not None:
            sql += " LIMIT %s"
            params.append(max(int(limit), 1))
        return [dict(row) for row in self.db.query(sql, tuple(params))]

    def origins(self, commits):
        values = sorted({str(sha).strip().lower() for sha in commits or ()
                         if str(sha or "").strip()})
        if not values:
            return []
        rows = self.db.query(
            "SELECT DISTINCT p.work_item_id"
            "  FROM provenance_receipts p"
            "  JOIN work_item_milestones m ON p.subject = 'milestone:' || m.id"
            " WHERE p.work_item_id IS NOT NULL"
            "   AND m.kind IN ('frozen', 'verdict')"
            "   AND p.sha = ANY(" + _ARRAY_SQL + ")"
            " ORDER BY p.work_item_id",
            (json.dumps(values),))
        return [int(row["work_item_id"]) for row in rows]

    def milestone_receipt(self, tx, milestone_id, *, sha, actor, session, turn,
                          harness, work_item_id) -> None:
        """Le reçu d'un jalon, écrit dans la transaction du jalon lui-même.

        P1b #7650 : `storage.postgres.work.add_milestone` garde la transaction
        et lève les jalons ; l'écriture de la preuve vit ici, avec sa table.
        """
        tx.execute(
            "INSERT INTO provenance_receipts"
            "       (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(milestone_id), sha, actor, session, turn,
             harness, int(work_item_id)))

    def harness(self, subject) -> str:
        """La famille (harnais) du reçu de `subject`, ou "" sans preuve."""
        rows = self.db.query(
            "SELECT harness FROM provenance_receipts WHERE subject = %s",
            (str(subject),))
        return str((rows[0] or {}).get("harness") or "").strip().lower() if rows else ""

    def carry_milestone(self, tx, milestone_id, *, sha, source_id) -> None:
        """#2928 : la preuve d'un verdict reporté suit le nouveau jalon."""
        tx.execute(
            "INSERT INTO provenance_receipts"
            "       (subject, sha, actor, session, turn, harness, work_item_id)"
            " SELECT 'milestone:' || %s, %s, actor, session, turn, harness, work_item_id"
            "   FROM provenance_receipts"
            "  WHERE subject = 'milestone:' || %s"
            " ON CONFLICT (subject) DO NOTHING",
            (int(milestone_id), sha, int(source_id)))

    def pending_lots(self, spellings, open_states, *, limit=6) -> list[int]:
        """#4299 : les lots encore ouverts où ces orthographes ont signé.

        Le renommage d'agent refuse tant que la porte de relecture peut lire
        un reçu signé d'un côté et un jalon réécrit de l'autre ; seuls les
        lots fusionnés ou fermés sont past the gate.
        """
        from ....storage.postgres import catalog as _catalog
        rows = self.db.query(
            "SELECT DISTINCT w.id FROM provenance_receipts p"
            "  LEFT JOIN work_item_milestones m ON p.subject = 'milestone:' || m.id"
            "  JOIN work_items w ON w.id = coalesce(p.work_item_id, m.work_item_id)"
            " WHERE p.actor = ANY(%s::text[]) AND w.state = ANY(%s::text[])"
            " ORDER BY w.id LIMIT %s",
            (_catalog._text_array(list(spellings)),
             _catalog._text_array(list(open_states)), int(limit)))
        return [int(row["id"]) for row in rows]

    @staticmethod
    def record_label(subject) -> str:
        """L'étiquette de la ligne de preuve (API `train_review.Receipt`)."""
        return "provenance_receipts:%s" % str(subject)

# SPDX-License-Identifier: AGPL-3.0-only
"""#1465 : preuves exactes d'origine, sur Postgres (migration 0445)."""
from __future__ import annotations

from ..provenance import Provenance as Contract

_COLUMNS = """
    subject, sha, actor, session, turn, harness, work_item_id,
    extract(epoch FROM at)::float8 AS at_ts
"""


class Provenance(Contract):

    def __init__(self, db):
        self.db = db

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

    def milestone(self, subject):
        rows = self.db.query(
            "SELECT p.subject, p.sha, p.actor, p.session, p.turn, p.harness,"
            "       p.work_item_id, extract(epoch FROM p.at)::float8 AS at_ts,"
            "       m.id AS milestone_id, m.actor AS mark_actor, m.sha AS mark_sha,"
            "       m.verdict, extract(epoch FROM m.at)::float8 AS mark_at_ts"
            "  FROM provenance_receipts p"
            "  JOIN work_item_milestones m ON p.subject = 'milestone:' || m.id"
            " WHERE p.subject = %s", (subject,))
        return rows[0] if rows else None

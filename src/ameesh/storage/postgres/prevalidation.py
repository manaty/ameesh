# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : rapports de prévalidation au gel (`prevalidation_reports`,
0450, #1770).

L'écriture est une instruction unique : elle a lieu APRÈS le run distant, hors
de toute transaction, pour ne tenir aucun verrou pendant que la VM exécute la
sélection. Le rapport enregistré est une preuve, pas un verdict."""
from __future__ import annotations

import json

from .. import interface

#: colonnes relues, la durée relative en secondes pour la lecture
COLUMNS = """
    work_item_id, head_sha, base_sha, card_commit, schema, outcome, ok, causes, report,
    extract(epoch from recorded_at)::float8 AS recorded_ts
"""


class PrevalidationReports(interface.PrevalidationReports):

    def record(self, report: dict) -> None:
        if not isinstance(report, dict):
            raise ValueError("rapport de prévalidation attendu : objet")
        lot = report.get("lot")
        head = str(report.get("sha") or "")
        if not isinstance(lot, int) or isinstance(lot, bool) or lot <= 0 or not head:
            raise ValueError("rapport sans clé (lot, tête) : %r" % (report.get("lot"),))
        self.db.execute(
            """
            INSERT INTO prevalidation_reports
                (work_item_id, head_sha, base_sha, card_commit, schema, outcome, ok,
                 causes, report)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (work_item_id, head_sha) DO UPDATE
               SET base_sha = excluded.base_sha,
                   card_commit = excluded.card_commit,
                   schema = excluded.schema,
                   outcome = excluded.outcome,
                   ok = excluded.ok,
                   causes = excluded.causes,
                   report = excluded.report,
                   recorded_at = now()
            """,
            (int(lot), head, str(report.get("base") or ""),
             str(report.get("card") or ""), str(report.get("schema") or ""),
             str(report.get("outcome") or ""), bool(report.get("ok")),
             json.dumps(list(report.get("causes") or ()), ensure_ascii=False),
             json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True)))

    def report(self, work_item_id: int, head_sha: str) -> dict | None:
        rows = self.db.query(
            "SELECT %s FROM prevalidation_reports WHERE work_item_id = %%s"
            " AND head_sha = %%s" % COLUMNS, (int(work_item_id), str(head_sha)))
        return rows[0] if rows else None

    def latest(self, work_item_id: int) -> dict | None:
        rows = self.db.query(
            "SELECT %s FROM prevalidation_reports WHERE work_item_id = %%s"
            " ORDER BY recorded_at DESC, head_sha LIMIT 1" % COLUMNS,
            (int(work_item_id),))
        return rows[0] if rows else None

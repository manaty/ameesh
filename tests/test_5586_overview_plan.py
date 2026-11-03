# SPDX-License-Identifier: AGPL-3.0-only
"""#5586 : garde de plan d'`Agents.overview()` (registre + courrier).

Diagnostic #5579 (base centrale, 54 agents) : 373 ms par appel à vide, 2,6 s
sous charge, ~1,4 cœur en continu. Chaque ligne relisait `agent_registry`
six fois (alias, `relit`, passage de l'exécuteur, harnais de session,
réclamation) alors que la latérale `j.__row` porte déjà la ligne, comptait
son courrier non remis par deux sous-requêtes de la vue et lisait
`canon_state` trois fois.

Le témoin, sur le modèle de #5555 : 54 agents, 10 000 messages, 20 lignes de
`canon_state` ; les comptes et l'état du canon sont ceux de la vue (0483), le
plan (sous psycopg) n'a qu'un agrégat du courrier et aucune sous-requête par
ligne sur `agent_registry` ou `agent_mailbox`, et l'appel tient sous 100 ms.
"""
from __future__ import annotations

import json
import unittest

from ameesh import db as db_mod, storage
from ameesh.storage.postgres.registry import OVERVIEW_SQL

from .support import PgTestCase

AGENTS = 54
MESSAGES = 10_000
CANON_ROWS = 20


def _nodes(roots, in_subplan=False):
    """(nœud, sous un SubPlan ?) de tout le plan, profondeur d'abord."""
    for node in roots:
        inside = in_subplan or "Subplan Name" in node
        yield node, inside
        yield from _nodes(node.get("Plans", []), inside)


class OverviewPlanDbTest(PgTestCase):

    def setUp(self) -> None:
        # `PgTestCase.setUp` vide le registre et le courrier avant chaque test
        super().setUp()
        self.db.execute("DELETE FROM agent_mailbox")
        self.db.execute("DELETE FROM canon_state")
        self.db.execute("DELETE FROM agent_registry")
        # les hôtes h5586-10 et h5586-11 n'ont pas de ligne `canon_state`
        self.db.execute("""
            INSERT INTO agent_registry (name, harness, host, status, canon,
                                        aliases, last_seen)
            SELECT 'a5586-' || i, 'claude', 'h5586-' || (i %% 12), 'idle',
                   CASE WHEN i %% 3 = 0 THEN 'c' || (i %% 2) END,
                   ARRAY['alias5586-' || i], now() - make_interval(secs => i)
              FROM generate_series(1, %s) i""" % AGENTS)
        self.db.execute("""
            INSERT INTO canon_state (host, canon, status, root, source, diagnostic)
            SELECT 'h5586-' || (i %% 10), CASE WHEN i < 10 THEN '' ELSE 'c' || (i %% 2) END,
                   CASE WHEN i %% 4 = 0 THEN 'invalid' ELSE 'ok' END,
                   '/r', 's', 'diag-' || i
              FROM generate_series(0, %s - 1) i""" % CANON_ROWS)
        # un tiers remis, et parmi les non remis : passifs (accusé, copie,
        # info, radar), urgents (jamais passifs, même accusés) et actifs.
        self.db.execute("""
            INSERT INTO agent_mailbox (sender, recipient, body, kind, payload,
                                       delivered_at, host)
            SELECT 'a5586-' || (1 + (i * 7) %% %(n)s), 'a5586-' || (1 + i %% %(n)s),
                   'm' || i, 'notify',
                   CASE i %% 6 WHEN 0 THEN '{"ack": true}'
                               WHEN 1 THEN '{"cc": "x"}'
                               WHEN 2 THEN '{"info": true, "urgent": true}'
                               WHEN 3 THEN '{"radar": {}}'
                               ELSE '{}' END::jsonb,
                   CASE WHEN i %% 3 = 0 THEN now() END, 'h5586-' || (i %% 10)
              FROM generate_series(1, %(m)s) i""" % {"n": AGENTS, "m": MESSAGES})
        self.db.execute("ANALYZE agent_registry")
        self.db.execute("ANALYZE agent_mailbox")
        self.db.execute("ANALYZE canon_state")

    def _plan(self):
        """Plan réel (EXPLAIN ANALYZE) de la requête exacte, sous psycopg."""
        driver = self.db
        if not isinstance(driver, db_mod.PsycopgDriver):
            try:
                driver = db_mod.PsycopgDriver(self.cfg)
            except db_mod.Unavailable:
                self.skipTest("EXPLAIN est enveloppé par le pilote psql "
                              "et psycopg est absent (#5586)")
            self.addCleanup(driver.close)
        with driver.transaction() as tx:
            rows = tx.query("EXPLAIN (ANALYZE, FORMAT JSON) " + OVERVIEW_SQL)
        plan = json.loads(json.dumps(rows))[0]["QUERY PLAN"][0]
        return plan["Plan"], plan["Execution Time"]

    def test_counts_and_canon_match_the_view(self):
        rows = {row["name"]: row for row in storage.of(self.db).agents.overview()}
        self.assertEqual(len(rows), AGENTS)
        view = self.db.query("""
            SELECT o.name, o.unread, o.passive_unread, s.status, s.diagnostic,
                   r.aliases, r.relit, r.session_harness
              FROM agent_mesh_overview o
              JOIN agent_registry r ON r.name = o.name
              LEFT JOIN canon_state s
                ON s.host = o.host AND s.canon = coalesce(o.canon, '')""")
        self.assertEqual(len(view), AGENTS)
        seen_passive = seen_canon = 0
        for want in view:
            got = rows[want["name"]]
            self.assertEqual(int(got["unread"]), int(want["unread"]), want["name"])
            self.assertEqual(int(got["passive_unread"]), int(want["passive_unread"]),
                             want["name"])
            self.assertEqual(got["canon_status"], want["status"], want["name"])
            self.assertEqual(got["canon_diagnostic"], want["diagnostic"], want["name"])
            self.assertEqual(list(got["aliases"] or []), list(want["aliases"] or []))
            self.assertEqual(got["relit"], want["relit"])
            self.assertEqual(got["session_harness"], want["session_harness"])
            seen_passive += int(want["passive_unread"]) > 0
            seen_canon += want["status"] is not None
        # la fixture exerce bien les deux comptes et les deux issues du canon
        self.assertGreater(seen_passive, 0)
        self.assertGreater(seen_canon, 0)
        self.assertLess(seen_canon, AGENTS)

    def test_agent_without_mail_counts_zero(self):
        self.db.execute("INSERT INTO agent_registry (name, harness, host, status)"
                        " VALUES ('b5586-muet', 'claude', 'h5586-0', 'idle')")
        self.addCleanup(self.db.execute,
                        "DELETE FROM agent_registry WHERE name = 'b5586-muet'")
        row = next(r for r in storage.of(self.db).agents.overview()
                   if r["name"] == "b5586-muet")
        self.assertEqual((int(row["unread"]), int(row["passive_unread"])), (0, 0))
        self.assertIsNone(row["last_mail_ts"])

    def test_one_mail_aggregate_and_no_registry_subquery_per_row(self):
        root, elapsed_ms = self._plan()
        nodes = list(_nodes([root]))
        problems = []
        for node, in_subplan in nodes:
            relation = node.get("Relation Name")
            if in_subplan and relation == "agent_mailbox":
                problems.append("SubPlan sur agent_mailbox (%s)" % node["Subplan Name"]
                                if "Subplan Name" in node else "SubPlan sur agent_mailbox")
            # la lignée des éphémères (`canon_governed_sql`) relit le
            # registre par `created_by`, jamais par le nom de la ligne
            if in_subplan and relation == "agent_registry" \
                    and "o.name" in (node.get("Index Cond", "") + node.get("Filter", "")):
                problems.append("SubPlan relisant agent_registry par nom")
        mail_aggregates = [node for node, _ in nodes
                           if node.get("Node Type") == "Aggregate"
                           and any(child.get("Relation Name") == "agent_mailbox"
                                   for child, _ in _nodes(node.get("Plans", [])))]
        self.assertEqual(len(mail_aggregates), 1, json.dumps(root, indent=1))
        self.assertEqual(int(mail_aggregates[0].get("Actual Loops", 1)), 1)
        self.assertEqual(problems, [], json.dumps(root, indent=1))
        self.assertLess(elapsed_ms, 100, "overview() : %.1f ms" % elapsed_ms)


if __name__ == "__main__":
    unittest.main()

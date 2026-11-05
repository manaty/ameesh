# SPDX-License-Identifier: AGPL-3.0-only
"""#5982 : lectures étroites du registre là où `OVERVIEW_SQL` ne servait à rien.

Le diagnostic #5972 (part CPU de #5764) a relevé `OVERVIEW_SQL` toutes les
60 s par exécuteur via `host_lifecycle.critical_names`, qui n'en gardait que
le nom et le canon, et jusqu'à 3,3 s sous charge. `Runner.served_rows` et
`_idle_pilot` n'en lisaient pas davantage. Les trois lecteurs étroits rendent
les mêmes valeurs que la vue complète, sans le courrier, `canon_state`, les
baux ni `to_jsonb(agent_registry)`.

Même témoin que #5586 : 54 agents et 10 000 messages ; la structure du plan
est la preuve, les durées mesurées dépendent de la charge du banc (canon §3).
"""
from __future__ import annotations

import json
import types
import unittest
from unittest import mock

from ameesh import db as db_mod, host_lifecycle, registry, runner, storage
from ameesh.storage.postgres.registry import (
    CRITICAL_ROWS_SQL, OVERVIEW_SQL, ROUTING_ROWS_SQL, SERVED_AGENTS_SQL)

from .support import PgTestCase

AGENTS = 54
MESSAGES = 10_000
CANON_ROWS = 20

NARROW_SQL = {
    "critical_rows": CRITICAL_ROWS_SQL,
    "served_agents": SERVED_AGENTS_SQL,
    "routing_rows": ROUTING_ROWS_SQL,
}


def _nodes(roots):
    for node in roots:
        yield node
        yield from _nodes(node.get("Plans", []))


def _relations(roots):
    return {node.get("Relation Name") for node in _nodes(roots)
            if node.get("Relation Name")}


class _NarrowFixture(PgTestCase):
    """Le registre, le courrier et le canon du témoin #5586."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM agent_mailbox")
        self.db.execute("DELETE FROM canon_state")
        self.db.execute("DELETE FROM agent_registry")
        self.db.execute("""
            INSERT INTO agent_registry (name, harness, host, status, canon,
                                        projects, team, workstream, chantier,
                                        mode, last_seen)
            SELECT 'a5982-' || i,
                   CASE WHEN i %% 2 = 0 THEN 'claude' ELSE 'codex' END,
                   'h5982-' || (i %% 4),
                   CASE WHEN i %% 7 = 0 THEN 'stopped' ELSE 'idle' END,
                   CASE WHEN i %% 3 = 0 THEN 'c' || (i %% 2) END,
                   CASE WHEN i %% 5 = 0 THEN ARRAY['p5982'] END,
                   'team5982', 'ws5982', 'chantier5982',
                   CASE WHEN i %% 11 = 0 THEN 'externe' ELSE 'execute' END,
                   now() - make_interval(secs => i)
              FROM generate_series(1, %s) i""" % AGENTS)
        self.db.execute("""
            INSERT INTO canon_state (host, canon, status, root, source, diagnostic)
            SELECT 'h5982-' || (i %% 10), CASE WHEN i < 10 THEN '' ELSE 'c' || (i %% 2) END,
                   CASE WHEN i %% 4 = 0 THEN 'invalid' ELSE 'ok' END,
                   '/r', 's', 'diag-' || i
              FROM generate_series(0, %s - 1) i""" % CANON_ROWS)
        self.db.execute("""
            INSERT INTO agent_mailbox (sender, recipient, body, kind, payload,
                                       delivered_at, host)
            SELECT 'a5982-' || (1 + (i * 7) %% %(n)s), 'a5982-' || (1 + i %% %(n)s),
                   'm' || i, 'notify', '{}'::jsonb,
                   CASE WHEN i %% 3 = 0 THEN now() END, 'h5982-' || (i %% 4)
              FROM generate_series(1, %(m)s) i""" % {"n": AGENTS, "m": MESSAGES})
        self.db.execute("ANALYZE agent_registry")
        self.db.execute("ANALYZE agent_mailbox")
        self.db.execute("ANALYZE canon_state")


class NarrowValuesTest(_NarrowFixture):
    def test_critical_rows_are_name_and_canon(self):
        full = {row["name"]: row for row in registry.overview(self.db)}
        rows = registry.critical_rows(self.db)
        self.assertEqual(len(rows), AGENTS)
        self.assertEqual({row["name"]: row["canon"] for row in rows},
                         {name: row["canon"] for name, row in full.items()})
        self.assertEqual(set(rows[0]), {"name", "canon"})

    def test_served_agents_is_the_previous_served_filter(self):
        for host in ("h5982-0", "h5982-1"):
            with self.subTest(host=host):
                full = [row for row in registry.overview(self.db)
                        if (row.get("host") or "") == host
                        and (row.get("mode") or "execute") == "execute"
                        and row.get("status") != "stopped"]
                rows = storage.of(self.db).agents.served_agents(host)
                self.assertEqual({row["name"] for row in rows},
                                 {row["name"] for row in full})
                self.assertEqual(set(rows[0]), {"name", "harness", "mode"})
                # `Runner.served_rows` ne filtre plus que par admission.
                worker = types.SimpleNamespace(host=host, agents_filter=None)
                self.assertEqual({row["name"] for row in
                                  runner.Runner.served_rows(worker, self.db)},
                                 {row["name"] for row in full})
                names = sorted(row["name"] for row in full)
                worker.agents_filter = names[:1]
                self.assertEqual([row["name"] for row in
                                  runner.Runner.served_rows(worker, self.db)],
                                 names[:1])

    def test_routing_rows_carry_the_pilot_fields(self):
        full = {row["name"]: row for row in registry.overview(self.db)}
        rows = registry.routing_rows(self.db)
        self.assertEqual(len(rows), AGENTS)
        self.assertEqual(set(rows[0]), {"name", "canon", "mode", "projects",
                                        "team", "chantier"})
        for row in rows:
            want = full[row["name"]]
            self.assertEqual(
                (row["canon"], row["mode"], list(row["projects"] or []),
                 row["team"], row["chantier"]),
                (want["canon"], want["mode"], list(want["projects"] or []),
                 want["team"], want["chantier"]))

    def test_served_rows_is_empty_for_a_mediated_executor(self):
        # L109 : l'annuaire réduit du contrat 1.1 ne porte ni hôte ni harnais ;
        # la lecture étroite locale n'existe pas pour un stockage distant.
        worker = types.SimpleNamespace(host="h5982-0", agents_filter=None)
        with mock.patch("ameesh.storage.is_remote", return_value=True), \
                mock.patch("ameesh.registry.served_agents",
                           side_effect=AssertionError("lecture locale")):
            self.assertEqual(runner.Runner.served_rows(worker, object()), [])

    def test_critical_names_does_not_read_the_full_view(self):
        with mock.patch("ameesh.registry.overview",
                        side_effect=AssertionError("vue complète lue")):
            names = host_lifecycle.critical_names(self.cfg, self.db, canons=[])
        self.assertIsInstance(names, set)


class NarrowPlanTest(_NarrowFixture):
    def _plan(self, sql):
        """Plan réel (EXPLAIN ANALYZE) de la requête étroite, sous psycopg."""
        driver = self.db
        if not isinstance(driver, db_mod.PsycopgDriver):
            try:
                driver = db_mod.PsycopgDriver(self.cfg)
            except db_mod.Unavailable:
                self.skipTest("EXPLAIN est enveloppé par le pilote psql "
                              "et psycopg est absent (#5982)")
            self.addCleanup(driver.close)
        with driver.transaction() as tx:
            rows = tx.query("EXPLAIN (ANALYZE, FORMAT JSON) " + sql)
        plan = json.loads(json.dumps(rows))[0]["QUERY PLAN"][0]
        return plan["Plan"], plan["Execution Time"]

    def test_critical_rows_touch_only_the_registry(self):
        root, elapsed_ms = self._plan(CRITICAL_ROWS_SQL)
        self.assertEqual(_relations([root]), {"agent_registry"})
        # Plafond large : la structure du plan est la preuve, le temps mesuré
        # dépend de la charge du banc (canon §3, #5607).
        self.assertLess(elapsed_ms, 1000, "critical_rows : %.1f ms" % elapsed_ms)

    def test_routing_rows_touch_only_the_registry(self):
        root, elapsed_ms = self._plan(ROUTING_ROWS_SQL)
        self.assertEqual(_relations([root]), {"agent_registry"})
        self.assertLess(elapsed_ms, 1000, "routing_rows : %.1f ms" % elapsed_ms)

    def test_narrow_sql_never_names_the_full_view_parts(self):
        for name, sql in NARROW_SQL.items():
            with self.subTest(reader=name):
                for forbidden in ("agent_mesh_overview", "agent_mailbox",
                                  "canon_state", "to_jsonb"):
                    self.assertNotIn(forbidden, sql)
        # La vue complète, elle, les porte bien : le témoin reste comparable.
        self.assertIn("agent_mailbox", OVERVIEW_SQL)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

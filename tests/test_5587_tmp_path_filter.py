# SPDX-License-Identifier: AGPL-3.0-only
"""#5587 : un épisode lit un chemin, la passe seulement les candidats utiles."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ameesh import db as db_mod, housekeeping, storage
from ameesh.storage.postgres import menage as menage_pg

from .support import PgTestCase


class TmpPathTest(PgTestCase):
    def test_exact_path_preserves_latest_host_and_episode(self):
        hk = storage.of(self.db).housekeeping
        hk.log("h1", [{"kind": "tmp", "action": "truncated", "path": "/tmp/a",
                       "data": {"episode_since": 123.0}},
                      {"kind": "tmp", "action": "resolved", "path": "/tmp/b"}],
               actor="test")
        hk.log("h2", [{"kind": "tmp", "action": "resolved", "path": "/tmp/a"}],
               actor="test")
        self.assertEqual(housekeeping.tmp_episode(
            self.db, "h1", "/tmp/a", {"truncated": True}, now=500),
            {"since": 123.0, "resolved": None})
        self.assertEqual(housekeeping.tmp_episode(
            self.db, "h1", "/tmp/a", {}, now=500),
            {"since": 123.0, "resolved": "truncated"})
        self.assertEqual(hk.latest_by_path("h1", "tmp", path="/tmp/missing"), [])
        self.assertEqual(len(hk.latest_by_path(None, "tmp", path="/tmp/a")), 2)
        self.assertEqual(hk.latest_by_path("h1", "tmp", path=""), [])
        self.assertEqual(hk.latest_by_path("h1", "tmp", path="/tmp/b",
                                         only_paths_with=("truncated",)), [])
        # Une clôture ultérieure gagne même si le candidat est une ancienne alerte.
        hk.log("h1", [{"kind": "tmp", "action": "resolved", "path": "/tmp/a"}],
               actor="test")
        rows = hk.latest_by_path("h1", "tmp", path="/tmp/a",
                                 only_paths_with=("truncated",))
        self.assertEqual([r["action"] for r in rows], ["resolved"])

    def test_pass_reads_only_unowned_and_over_quota_candidates(self):
        hk = storage.of(self.db).housekeeping
        with mock.patch.object(type(hk), "latest_by_path",
                               autospec=True, return_value=[]) as read:
            housekeeping.run_pass(self.cfg, self.db, housekeeping.Policy(),
                                  host="h1", actor="test", dry_run=True,
                                  artifact_scan=False)
        calls = [c for c in read.call_args_list if c.args[2] == "tmp"]
        self.assertTrue(calls)
        self.assertEqual(calls[0].kwargs,
                         {"only_paths_with": ("unowned", "over_quota")})

    def test_plans_on_half_a_million_rows(self):
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest("EXPLAIN is wrapped by the psql driver")
        self.db.execute("DELETE FROM housekeeping_log")
        # #5599 : le remplissage prépare, il ne mesure pas. Sous la charge du
        # banc, 500 000 lignes à travers le trigger `ameesh_agent_spelling` et
        # les index du journal dépassaient le statement_timeout (44 s pour
        # 30 s) : trigger coupé le temps du remplissage (#3941), délai levé
        # pour cette instruction (#4822).
        self.db.execute(
            "ALTER TABLE housekeeping_log DISABLE TRIGGER ameesh_agent_spelling")
        try:
            db_mod.execute_without_statement_timeout(
                self.db,
                "INSERT INTO housekeeping_log (host, at, actor, kind, action, path)"
                " SELECT 'h1', now() - g * interval '1 second', 'test', 'tmp',"
                " 'signaled', '/tmp/p' || (g % 2000) FROM generate_series(1, 500000) g")
        finally:
            self.db.execute(
                "ALTER TABLE housekeeping_log ENABLE TRIGGER ameesh_agent_spelling")
        hk = storage.of(self.db).housekeeping
        hk.log("h1", [{"kind": "tmp", "action": "unowned", "path": "/tmp/keep"},
                      {"kind": "tmp", "action": "over_quota", "path": "/tmp/gone"},
                      {"kind": "tmp", "action": "resolved", "path": "/tmp/gone"}],
               actor="test")
        self.db.execute("ANALYZE housekeeping_log")
        sql, params = menage_pg._latest_by_path_sql("tmp", host="h1", path="/tmp/p1")
        plan = self.db.query("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
        text = json.dumps(plan)
        self.assertIn("housekeeping_log_kind_host_path_time_idx", text)
        self.assertIn("path =", text)
        self.assertNotIn("Seq Scan", text)
        # Structure bornée, indépendante de la charge de l'hôte : une entrée lue.
        document = plan[0]["QUERY PLAN"][0]
        self.assertEqual(document["Plan"]["Actual Rows"], 1)
        self.assertEqual(document["Plan"]["Plans"][0]["Actual Rows"], 1)
        print("#5587 path execution ms:", document["Execution Time"])
        sql, params = menage_pg._latest_by_path_sql(
            "tmp", host="h1", actions=["unowned", "over_quota"])
        plan = json.dumps(self.db.query("EXPLAIN (FORMAT JSON) " + sql, params))
        self.assertIn("housekeeping_log_kind_action_path_time_idx", plan)
        self.assertNotIn("Seq Scan", plan)
        rows = hk.latest_by_path("h1", "tmp", only_paths_with=("unowned", "over_quota"))
        self.assertEqual({r["path"]: r["action"] for r in rows},
                         {"/tmp/keep": "unowned", "/tmp/gone": "resolved"})


class TmpEpisodeReadTest(unittest.TestCase):
    def test_episode_pushes_the_path_filter_to_storage(self):
        hk = mock.Mock()
        hk.latest_by_path.return_value = []
        with mock.patch.object(housekeeping.storage, "of",
                               return_value=mock.Mock(housekeeping=hk)):
            housekeeping.tmp_episode(object(), "h1", "/tmp/a", {}, now=500)
        hk.latest_by_path.assert_called_once_with("h1", "tmp", path="/tmp/a")

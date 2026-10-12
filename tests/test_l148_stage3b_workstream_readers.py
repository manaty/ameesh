# SPDX-License-Identifier: AGPL-3.0-only
"""L148 (#147) stage 3, PR B : les lecteurs libres lisent `workstream`.

`agent_registry.workstream` existe depuis la migration A (0410) et reste en
phase avec `chantier`. Ce module vérifie que les lecteurs des fichiers libres
lisent le nouveau nom, que `chantier` reste émis à la même valeur pour les
lecteurs pas encore renommés (PR C), et que l'ancien nom reste lu quand il est
le seul fourni (ligne, dictionnaire ou argument).

Ce module touche la couche base : `tests/parts/pilote-psql.txt` le fait
tourner aussi sous le pilote psql.
"""
from __future__ import annotations

import unittest

from ameesh import decisions, plan_github, progress, projects, registry, storage

from .support import PgTestCase


class AgentProjectTest(unittest.TestCase):
    """`projects.agent_project` : le nouveau nom d'abord, l'ancien en secours."""

    def test_workstream_gagne_sur_chantier(self):
        self.assertEqual(projects.agent_project({"workstream": "w", "chantier": "c"}), "w")

    def test_chantier_reste_lu_seul(self):
        self.assertEqual(projects.agent_project({"chantier": "c"}), "c")

    def test_l_equipe_gagne_sur_les_deux(self):
        self.assertEqual(projects.agent_project(
            {"team": "t", "workstream": "w", "chantier": "c"}), "t")

    def test_aucun_projet(self):
        self.assertIsNone(projects.agent_project({}))


class ProgressAgentTest(unittest.TestCase):
    """`progress.build_agent` : même ordre de lecture pour le projet."""

    def row(self, **keys):
        return {"name": "dev", "status": "idle", **keys}

    def test_le_projet_lit_workstream_puis_chantier(self):
        self.assertEqual(progress.build_agent(self.row(workstream="w"), 0.0)["project"], "w")
        self.assertEqual(progress.build_agent(self.row(chantier="c"), 0.0)["project"], "c")
        self.assertEqual(progress.build_agent(
            self.row(workstream="w", chantier="c"), 0.0)["project"], "w")


class DecisionsProjectTest(unittest.TestCase):
    """`decisions._project` : la ligne du demandeur, ancien nom compris."""

    def test_le_demandeur_lit_workstream_puis_chantier(self):
        self.assertEqual(decisions._project(
            None, {}, {"workstream": "w", "chantier": "c"}), "w")
        self.assertEqual(decisions._project(None, {}, {"chantier": "c"}), "c")

    def test_le_lot_gagne_sur_le_demandeur(self):
        self.assertEqual(decisions._project(
            None, {"workstream": "lot"}, {"workstream": "w", "chantier": "c"}), "lot")


class LotProjectTest(unittest.TestCase):
    """`plan_github.lot_project` : le projet de l'assigné, les deux clés lues."""

    def test_assignee_workstream_est_lu(self):
        self.assertEqual(plan_github.lot_project(
            {"assignee": "dev", "assignee_workstream": "w"}), "w")

    def test_assignee_chantier_reste_lu(self):
        self.assertEqual(plan_github.lot_project(
            {"assignee": "dev", "assignee_chantier": "c"}), "c")


class WorkstreamReadersTest(PgTestCase):
    """En base : `workstream` est lu, `chantier` reste émis à la même valeur."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "dev1", chantier="ameesh", harness="deepseek",
                        host=self.cfg.host, cwd=self.tmp, session_id="s1",
                        status="idle", status_text="", model=None, budget_usd=None)

    def board_agent(self, name="dev1"):
        board = storage.of(self.db).projects.board(max_lots=10)
        return next(row for row in board["agents"] if row["name"] == name)

    def test_la_vue_par_projet_rend_les_deux_cles(self):
        row = self.board_agent()
        self.assertEqual(row["workstream"], "ameesh")
        self.assertEqual(row["chantier"], "ameesh")

    def test_la_vue_progress_rend_les_deux_cles(self):
        rows = storage.of(self.db).progress.agents("ameesh")
        row = next(r for r in rows if r["name"] == "dev1")
        self.assertEqual(row["workstream"], "ameesh")
        self.assertEqual(row["chantier"], "ameesh")

    def test_le_filtre_par_projet_lit_workstream(self):
        self.assertEqual(storage.of(self.db).progress.agents("nexlink"), [])

    def test_lock_recipient_rend_les_deux_cles(self):
        row = storage.of(self.db).mailbox.lock_recipient("dev1")
        self.assertEqual(row["workstream"], "ameesh")
        self.assertEqual(row["chantier"], "ameesh")

    def test_une_ecriture_workstream_est_vue_par_les_lecteurs(self):
        self.db.execute(
            "UPDATE agent_registry SET workstream = 'nexlink' WHERE name = 'dev1'")
        row = self.board_agent()
        self.assertEqual(row["workstream"], "nexlink")
        self.assertEqual(row["chantier"], "nexlink")

    def test_un_client_d_avant_reste_lisible(self):
        """Une ligne écrite par l'ancien code (chantier seul) se lit en workstream."""
        self.db.execute(
            "INSERT INTO agent_registry (name, chantier) VALUES ('avant1', 'legacy')")
        row = self.board_agent("avant1")
        self.assertEqual(row["workstream"], "legacy")
        self.assertEqual(row["chantier"], "legacy")

    def test_register_chat_ecrit_workstream(self):
        storage.of(self.db).decisions.register_chat(
            "chat-dev", human="human:alice", host=self.cfg.host, harness="claude",
            cwd=self.tmp, status_text="conversation de human:alice")
        row = self.db.query(
            "SELECT chantier, workstream FROM agent_registry WHERE name = 'chat-dev'")[0]
        self.assertEqual((row["chantier"], row["workstream"]), ("", ""))


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""#3779 — la fenêtre courte du fournisseur Codex est suivie et partagée.

Le bucket hebdomadaire de Codex est à 11 % ; le fournisseur refuse pourtant en
429 plusieurs sessions du même compte (bucket de rafale/concurrence non
documenté). Ce module vérifie le journal partagé, le cooldown de compte, l'écart
entre deux débuts de tour, le plafond de tours actifs, la jauge d'`accounts list`
et le branchement du runner.
"""
from __future__ import annotations

import os
import threading
import unittest

from ameesh import account_turn, account_window, accounts, cost

from .test_l30_accounts import _Base, _dossier

ERREUR_429 = "exceeded retry limit, last status: 429 Too Many Requests"


class CooldownTest(unittest.TestCase):
    def test_echelle_du_cooldown(self):
        self.assertEqual(account_window.cooldown_for(0), 0.0)
        self.assertEqual(account_window.cooldown_for(1), 240.0)
        self.assertEqual(account_window.cooldown_for(2), 480.0)
        self.assertEqual(account_window.cooldown_for(3), 900.0)
        self.assertEqual(account_window.cooldown_for(99), 900.0)

    def test_harnais_non_suivi(self):
        self.assertTrue(account_window.enabled("codex"))
        self.assertFalse(account_window.enabled("claude"))
        self.assertFalse(account_window.enabled(""))
        self.assertEqual(account_window.max_active("codex"), 2)
        self.assertEqual(account_window.max_active("claude"), 0)
        self.assertEqual(account_window.min_start_gap("codex"), 30.0)

    def test_seuils_regles_par_le_profil(self):
        profil = accounts.Profile("codex", "primaire", max_concurrent=1,
                                  min_start_gap_s=0.0)
        self.assertEqual(account_window.max_active("codex", profil), 1)
        self.assertEqual(account_window.min_start_gap("codex", profil), 0.0)
        libre = accounts.Profile("codex", "primaire", max_concurrent=0)
        self.assertEqual(account_window.max_active("codex", libre), 0)


class FenetreCourteTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM account_rate_events")
        self.db.execute("DELETE FROM account_turn_starts")

    def _comptes_codex(self) -> dict:
        dossier = _dossier(os.path.join(self.tmp, "comptes", "codex-3779"),
                           creds="auth.json")
        return {"codex": [{"name": "primaire", "path": dossier}]}

    def _worker_codex(self, name: str):
        runner, worker = self._worker(name, "codex", self._comptes_codex())
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _events(self) -> list[dict]:
        return self.db.query(
            "SELECT harness, account, host, agent, kind, detail"
            " FROM account_rate_events ORDER BY id")

    def test_journal_et_cooldown_partages(self):
        account_window.record_rate_limit(
            self.db, "codex", "primaire", "ameesh-agents-2", "ameesh-cx1",
            ERREUR_429)
        etat = account_window.state(self.db, "codex", "primaire")
        self.assertEqual(etat["recent_429"], 1)
        self.assertEqual(etat["last_agent"], "ameesh-cx1")
        self.assertIsNotNone(etat["cooldown_until"])
        self.assertAlmostEqual(etat["cooldown_s"], 240.0)
        raison = account_window.reason(self.db, "codex", "primaire")
        self.assertIn("short-window cooldown", raison)
        self.assertIn("429", raison)
        self.assertEqual(len(self._events()), 1)
        # La jauge de `accounts list` lit le même état, sans écrire.
        lignes = account_window.rows(
            self.db, [{"harness": "codex", "account": "primaire"}])
        self.assertEqual(len(lignes), 1)
        self.assertEqual(lignes[0]["recent_429"], 1)
        self.assertEqual(lignes[0]["max_active"], 2)
        self.assertEqual(lignes[0]["min_start_gap_s"], 30.0)
        self.assertEqual(lignes[0]["key"], "codex-short-window")
        texte = "\n".join(account_window.format_rows(lignes))
        self.assertIn("codex-short-window", texte)
        self.assertIn("0/2 turn active", texte)
        self.assertIn("start gap 30s", texte)
        self.assertIn("429 in 15 min", texte)
        self.assertIn("cooldown", texte)

    def test_escalade_du_cooldown(self):
        for _ in range(3):
            account_window.record_rate_limit(
                self.db, "codex", "primaire", "h", "cx", ERREUR_429)
        etat = account_window.state(self.db, "codex", "primaire")
        self.assertEqual(etat["recent_429"], 3)
        self.assertAlmostEqual(etat["cooldown_s"], 900.0)

    def test_journal_sans_rapport_ou_hors_harnais(self):
        account_window.record_rate_limit(
            self.db, "claude", "primaire", "h", "dev", ERREUR_429)
        account_window.record_rate_limit(
            self.db, "codex", None, "h", "cx", ERREUR_429)
        self.assertEqual(self._events(), [])
        self.assertEqual(account_window.state(self.db, "codex", "primaire")["recent_429"], 0)

    def test_ecart_entre_deux_debuts(self):
        account_window.record_start(self.db, "codex", "primaire",
                                    "ameesh-agents-1", "codex1")
        etat = account_window.state(self.db, "codex", "primaire")
        self.assertIsNotNone(etat["last_start_at"])
        self.assertAlmostEqual(etat["next_start_at"],
                               etat["last_start_at"] + 30.0, delta=0.01)
        raison = account_window.reason(self.db, "codex", "primaire",
                                       now=float(etat["last_start_at"]) + 1.0)
        self.assertIn("start gap", raison)
        self.assertIn("next turn in", raison)
        self.assertEqual(account_window.reason(
            self.db, "codex", "primaire",
            now=float(etat["last_start_at"]) + 31.0), "")

    def test_plafond_de_tours_actifs_par_compte(self):
        for name, host in (("codex-autre-1", "h1"), ("codex-autre-2", "h2")):
            self.db.execute(
                "INSERT INTO agent_registry"
                " (name, chantier, harness, host, status, session_account,"
                "  status_since, lease_owner, lease_expires_at)"
                " VALUES (%s, '', 'codex', %s, 'running', 'primaire', now(),"
                "         'runner', now() + interval '1 hour')",
                (name, host))
        # B1 auditeur : un hôte tombé garde `running` mais son bail est expiré ;
        # il ne doit pas compter dans le plafond (sinon 2/2 pour toujours).
        self.db.execute(
            "INSERT INTO agent_registry"
            " (name, chantier, harness, host, status, session_account,"
            "  status_since, lease_owner, lease_expires_at)"
            " VALUES ('codex-mort', '', 'codex', 'h3', 'running', 'primaire',"
            "         now() - interval '1 hour', 'runner',"
            "         now() - interval '1 minute')")
        etat = account_window.state(self.db, "codex", "primaire")
        self.assertEqual(etat["active_count"], 2)
        raison = account_window.reason(self.db, "codex", "primaire",
                                       exclude_agent="moi")
        self.assertIn("2/2 concurrent turns", raison)
        # l'appelant ne se compte pas lui-même (choix d'avant tour)
        self.assertEqual(account_window.state(
            self.db, "codex", "primaire",
            exclude_agent="codex-autre-1")["active_count"], 1)
        # un profil peut relever le plafond
        profil = accounts.Profile("codex", "primaire", max_concurrent=3)
        self.assertEqual(account_window.state(
            self.db, "codex", "primaire", profile=profil,
            exclude_agent="moi")["max_active"], 3)

    def test_admission_atomique(self):
        premier = account_window.claim_start(
            self.db, "codex", "primaire", "ameesh-agents-1", "codex1")
        self.assertEqual(premier, "")
        self.assertEqual(len(self.db.query(
            "SELECT id FROM account_turn_starts")), 1)
        second = account_window.claim_start(
            self.db, "codex", "primaire", "ameesh-agents-2", "cx2")
        self.assertIn("start gap", second)
        self.assertEqual(len(self.db.query(
            "SELECT id FROM account_turn_starts")), 1)
        # le cooldown est relu sous le même verrou
        self.db.execute("DELETE FROM account_turn_starts")
        account_window.record_rate_limit(
            self.db, "codex", "primaire", "h", "cx1", ERREUR_429)
        self.assertIn("cooldown", account_window.claim_start(
            self.db, "codex", "primaire", "h", "cx2"))

    def test_departs_concurrents_ne_depassent_pas_l_ecart(self):
        """Témoin cx2 : deux runners cross-hôtes se présentent ensemble ; le
        verrou du compte et l insertion du départ dans la même transaction
        n en laissent passer qu un."""
        barriere = threading.Barrier(2)
        resultats: list[str] = []
        garde = threading.Lock()

        def essai(agent: str) -> None:
            db = self.connect()
            try:
                barriere.wait(timeout=10)
                raison = account_window.claim_start(
                    db, "codex", "primaire", "h", agent)
            finally:
                db.close()
            with garde:
                resultats.append(raison)

        threads = [threading.Thread(target=essai, args=(agent,))
                   for agent in ("cx-a", "cx-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(len(resultats), 2)
        self.assertEqual(sum(1 for raison in resultats if raison == ""), 1,
                         resultats)
        self.assertEqual(sum(1 for raison in resultats if "start gap" in raison), 1,
                         resultats)
        self.assertEqual(len(self.db.query(
            "SELECT id FROM account_turn_starts")), 1)

    def test_choose_refuse_avant_le_tour_et_le_runner_journalise(self):
        worker = self._worker_codex("cx-3779")
        account_window.record_rate_limit(
            self.db, "codex", "primaire", "h", "ameesh-cx1", ERREUR_429)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db,
                             tools={worker.name: "codex"})
        raison = account_turn.choose(worker, book)
        self.assertIn("short-window cooldown", raison or "")

        # un tour réellement refusé en 429 laisse sa ligne au journal partagé
        self.db.execute("DELETE FROM account_rate_events")
        self.db.execute("DELETE FROM account_turn_starts")
        self.assertIsNotNone(self._tour(
            worker, AMEESH_TEST_TURN_FAILED=ERREUR_429))
        lignes = self._events()
        self.assertEqual(len(lignes), 1)
        self.assertEqual(lignes[0]["agent"], "cx-3779")
        self.assertEqual(lignes[0]["account"], "primaire")
        self.assertEqual(lignes[0]["harness"], "codex")
        self.assertIn("429", lignes[0]["detail"])
        self.assertEqual(len(self.db.query(
            "SELECT id FROM account_turn_starts WHERE agent = 'cx-3779'")), 1)

    def test_choose_refuse_pendant_lecrat_de_debut(self):
        worker = self._worker_codex("cx-gap")
        account_window.record_start(self.db, "codex", "primaire",
                                    "ameesh-agents-1", "codex1")
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db,
                             tools={worker.name: "codex"})
        raison = account_turn.choose(worker, book)
        self.assertIn("start gap", raison or "")

    def test_migration_presente(self):
        for table in ("account_rate_events", "account_turn_starts"):
            with self.subTest(table=table):
                self.assertIsNotNone(self.db.query(
                    "SELECT to_regclass(%s) AS table", (table,))[0]["table"])

    def test_configuration_des_seuils(self):
        profils = accounts.parse({"codex": [{
            "name": "primaire", "max_concurrent": 3, "min_start_gap_s": 45}]})
        profil = profils["codex"][0]
        self.assertEqual(profil.max_concurrent, 3)
        self.assertAlmostEqual(profil.min_start_gap_s, 45.0)
        for item in ({"name": "x", "max_concurrent": -1},
                     {"name": "x", "max_concurrent": True},
                     {"name": "x", "min_start_gap_s": -1},
                     {"name": "x", "min_start_gap_s": "vite"}):
            with self.subTest(item=item):
                with self.assertRaises(accounts.AccountError):
                    accounts.parse({"codex": [item]})


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""L13 — garde de budget avant chaque tour, modèle/effort par agent (0019)."""
from __future__ import annotations

import dataclasses
import json
import os
import time
import unittest
from unittest import mock

from ameesh import adapters, cost, db as db_mod, registry
from ameesh.adapters import adapter_for
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


class SetModelEffortTest(PgTestCase):
    def _travail(self, name: str = "l13") -> str:
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        return cwd

    def test_set_ecrit_modele_et_effort_puis_le_tour_les_applique(self):
        self._travail()
        proc = self.mesh("set", "l13", "model=claude-opus-4", "effort=high")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(registry.get(self.db, "l13")["model"], "claude-opus-4")
        with open(os.path.join(self.cfg.state_dir, "l13", "effort"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "high")
        runner = Runner(self.cfg, self.db, once=True)
        lease = registry.claim(self.db, "l13", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "l13"), lease)
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "tour", "ids": []}))
        finally:
            worker.watchdog_stop.set()
        argv = " ".join(self.turns()[-1]["argv"])
        self.assertIn("--model claude-opus-4", argv)
        self.assertIn("--effort high", argv)

    def test_set_valeur_vide_revient_au_defaut(self):
        self._travail()
        self.mesh("set", "l13", "model=claude-opus-4", "effort=high")
        proc = self.mesh("set", "l13", "model=", "effort=")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(registry.get(self.db, "l13")["model"])
        self.assertFalse(os.path.exists(
            os.path.join(self.cfg.state_dir, "l13", "effort")))

    def test_options_declarees_par_harnais(self):
        claude = adapter_for("claude", binary="/bin/echo")
        argv = claude.command("txt", None, model="m", effort="e")
        self.assertIn("--model", argv)
        self.assertIn("m", argv)
        self.assertIn("--effort", argv)
        self.assertIn("e", argv)
        codex = adapter_for("codex", binary="/bin/echo")
        argv = codex.command("txt", None, model="m", effort="e")
        self.assertIn("-m", argv)
        self.assertIn('model_reasoning_effort="e"', argv)
        dsh = adapter_for("deepseek", binary="/bin/echo")
        patch = os.path.join(self.tmp, "model.patch.yml")
        argv = dsh.command("txt", None, model="deepseek-pro", effort="max", patch=patch)
        self.assertIn("--patch", argv)
        with open(patch, encoding="utf-8") as fh:
            contenu = fh.read()
        self.assertIn("model: deepseek-pro", contenu)
        self.assertIn("reasoningEffort: max", contenu)


class BudgetGateTest(PgTestCase):
    def _worker(self, name: str = "budget", *, plafond: float = 10.0,
                harness: str = "deepseek"):
        cfg = dataclasses.replace(self.cfg, budget_usd_per_hour=plafond,
                                  budget_check_interval=0.0)
        runner = Runner(cfg, self.db, once=True)
        # le plafond horaire porte sur toutes les dépenses de l'hôte : on part
        # d'un grand livre propre pour que chaque cas soit isolé, et on efface
        # un éventuel marqueur de comptabilité laissé par un test précédent.
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("DELETE FROM spend_pending WHERE agent = %s", (name,))
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, harness, cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        return runner, AgentWorker(runner, registry.get(self.db, name), lease)

    def _depense(self, agent: str, usd: float) -> None:
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, recorded_at) "
            "VALUES (%s, 'deepseek', 'deepseek-flash', %s, now())", (agent, usd))

    def test_depense_horaire_met_en_pause_et_trace_dans_le_fil(self):
        runner, worker = self._worker(plafond=10.0)
        self._depense("budget", 12.0)
        try:
            self.assertFalse(worker.budget_ok())
            self.assertIsNone(worker.pick())
            ligne = registry.get(self.db, "budget")
            self.assertEqual(ligne["status"], "blocked")
            self.assertIn("budget horaire", ligne["status_text"])
        finally:
            worker.watchdog_stop.set()

    def test_sous_le_plafond_le_tour_passe(self):
        runner, worker = self._worker(plafond=10.0)
        self._depense("budget", 1.0)
        self.assertTrue(worker.budget_ok())

    def test_garde_desactivee_si_plafond_nul(self):
        runner, worker = self._worker(plafond=0.0)
        self._depense("budget", 999.0)
        self.assertTrue(worker.budget_ok())
        self.assertEqual(worker.budget_reason(), "")

    def test_forfait_au_dessus_du_rythme_met_en_pause(self):
        runner, worker = self._worker(plafond=10.0)
        worker.agent["harness"] = "claude"
        jauge = cost.Gauge(harness="claude", key="5h", used=0.95,
                           resets_at=time.time() + 3600, window_s=5 * 3600)
        with mock.patch("ameesh.cost.CostBook.gauges", return_value=[jauge]):
            raison = worker.budget_reason()
        self.assertIn("forfait claude", raison)

    def test_modele_de_la_ligne_est_applique(self):
        runner, worker = self._worker(plafond=10.0)
        registry.upsert(self.db, "budget", model="deepseek-pro")
        worker.agent = registry.get(self.db, "budget")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "tour", "ids": []}))
        finally:
            worker.watchdog_stop.set()
        argv = " ".join(self.turns()[-1]["argv"])
        self.assertIn("--patch", argv)
        with open(os.path.join(self.cfg.state_dir, "budget", "model.patch.yml"),
                  encoding="utf-8") as fh:
            self.assertIn("model: deepseek-pro", fh.read())

    def test_un_tour_reel_ecrit_le_grand_livre(self):
        """Verdict L13 B1 : après un tour réel, `turn_costs` porte la dépense."""
        runner, worker = self._worker(plafond=10.0, harness="claude")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "tour", "ids": []}))
        finally:
            worker.watchdog_stop.set()
        lignes = self.db.query(
            "select count(*)::int as n from turn_costs where agent = 'budget'")[0]["n"]
        self.assertEqual(lignes, 1)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db,
                             tools={"budget": "claude"})
        self.assertGreater(book.spent("budget", 3600), 0.0)

    def test_echec_de_comptabilite_suspend_les_tours(self):
        """Verdict L13 B1 : sans grand livre, plus de tour (fail-closed)."""
        runner, worker = self._worker(plafond=10.0, harness="claude")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("ameesh.cost.CostBook.record",
                               side_effect=RuntimeError("disque plein")):
                worker.run_turn({"kind": "prompt", "prompt": "tour", "ids": []})
                self.assertTrue(worker._compta_en_echec)
                self.assertFalse(worker.budget_ok())  # la réparation échoue encore
            # base réparée : le travail en attente est rejoué et la reprise revient
            self.assertTrue(worker.budget_ok())
            self.assertFalse(worker._compta_en_echec)
        finally:
            worker.watchdog_stop.set()

    def test_le_forfait_ne_compte_pas_dans_le_plafond_token(self):
        """Verdict L13 B2 : une estimation de forfait (Claude) n'entre pas dans
        le plafond horaire payé au token."""
        runner, worker = self._worker(plafond=0.1)
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, recorded_at) "
            "VALUES ('forfait', 'claude', 'claude-opus-4', 100.0, now())")
        self.assertEqual(worker.budget_reason(), "")

    def test_pas_de_cache_positif_de_la_garde(self):
        """Verdict L13 B3 : un tour autorisé ne met pas la décision en cache ;
        un dépassement survenu juste après est vu au tour suivant."""
        runner, worker = self._worker(plafond=0.1)
        self.assertTrue(worker.budget_ok())
        self._depense("budget", 1.0)
        self.assertFalse(worker.budget_ok())

    def test_modele_effectif_du_lancement_est_facture(self):
        """Verdict L13 B4 : le barème suit le modèle choisi au lancement, pas le
        fichier d'état relu au moment de l'écriture."""
        runner, worker = self._worker(plafond=10.0, harness="deepseek")
        with open(worker._path("events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "status", "phase": "step_end",
                "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                          "outputTokens": 0},
            }) + "\n")
        self.assertTrue(worker._compta_marque(0, "tour", "deepseek-pro"))
        worker._compta_termine()
        ligne = self.db.query(
            "select model, usd from turn_costs where agent = 'budget' "
            "order by id desc limit 1")[0]
        self.assertEqual(ligne["model"], "deepseek-pro")
        # barème pro : 1 M de jetons d'entrée = 0,55 $ (le défaut serait 0,27 $)
        self.assertAlmostEqual(float(ligne["usd"]), 0.55, places=6)

    def test_modele_inconnu_facture_le_plus_cher(self):
        """B4 (arbitrage mesh-design) : un modèle inconnu (vide) facture le
        tarif le plus cher connu de la famille, jamais le défaut, et ne suit
        pas un état local modifié pendant le tour."""
        runner, worker = self._worker(plafond=10.0, harness="deepseek")
        with open(worker._path("events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "status", "phase": "step_end",
                "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                          "outputTokens": 0},
            }) + "\n")
        self.assertTrue(worker._compta_marque(0, "tour", ""))  # modèle inconnu
        os.makedirs(worker.state_dir, exist_ok=True)
        with open(os.path.join(worker.state_dir, "model"), "w", encoding="utf-8") as fh:
            fh.write("deepseek-pro\n")  # état modifié pendant le tour
        worker._compta_termine()
        ligne = self.db.query(
            "select model, usd from turn_costs where agent = 'budget' "
            "order by id desc limit 1")[0]
        self.assertEqual(ligne["model"], "inconnu")
        # plus cher connu DeepSeek = pro : 0,55 $/M d'entrée (défaut 0,27)
        self.assertAlmostEqual(float(ligne["usd"]), 0.55, places=6)

    def test_modele_annonce_par_le_flux_gagne(self):
        """B4 : le modèle annoncé par le flux du harnais est la source du tour
        (il prime sur le marqueur figé au lancement)."""
        runner, worker = self._worker(plafond=10.0, harness="deepseek")
        with open(worker._path("events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "session", "sessionId": "s1",
                                 "model": "deepseek-pro"}) + "\n")
            fh.write(json.dumps({
                "type": "status", "phase": "step_end",
                "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                          "outputTokens": 0},
            }) + "\n")
        self.assertTrue(worker._compta_marque(0, "tour", ""))
        worker._compta_termine("deepseek-pro")
        ligne = self.db.query(
            "select model, usd from turn_costs where agent = 'budget' "
            "order by id desc limit 1")[0]
        self.assertEqual(ligne["model"], "deepseek-pro")
        self.assertAlmostEqual(float(ligne["usd"]), 0.55, places=6)

    def test_suspension_durable_et_reparation_avant_reprise(self):
        """Verdict L13 B5 : un échec de comptabilité survit au redémarrage, et
        le travail en attente est rejoué avant d'autoriser un nouveau tour."""
        runner, worker = self._worker(plafond=10.0, harness="claude")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("ameesh.cost.CostBook.record",
                               side_effect=RuntimeError("disque plein")):
                worker.run_turn({"kind": "prompt", "prompt": "tour", "ids": []})
            self.assertTrue(worker._compta_en_echec)
            self.assertIsNotNone(registry.pending_spend_get(self.db, "budget"))
            # redémarrage : même agent, même bail, nouveau worker
            worker2 = AgentWorker(runner, registry.get(self.db, "budget"), {
                "lease_epoch": worker.epoch,
                "lease_expires_ts": time.time() + 3600,
            })
            self.assertTrue(worker2._compta_en_echec, "la suspension n'a pas survécu")
            with mock.patch("ameesh.cost.CostBook.record",
                            side_effect=RuntimeError("disque plein")):
                self.assertFalse(worker2.budget_ok())  # réparation impossible
            # réparation : l'écriture en attente est rejouée, puis la reprise
            self.assertTrue(worker2.budget_ok())
            self.assertIsNone(registry.pending_spend_get(self.db, "budget"))
            lignes = self.db.query(
                "select count(*)::int as n from turn_costs where agent = 'budget'")[0]["n"]
            self.assertEqual(lignes, 1)
        finally:
            worker.watchdog_stop.set()

    def test_ligne_comptable_presente_ou_incoherente_suspend(self):
        """Verdict L13 B5 (état en base) : une ligne présente suspend un nouveau
        worker ; une ligne incohérente (index négatif) ne peut pas être réparée
        et reste bloquante — jamais d'autorisation."""
        runner, worker = self._worker(plafond=10.0, harness="claude")
        self.assertTrue(registry.pending_spend_put(self.db, "budget", 0, "tour", ""))
        worker2 = AgentWorker(runner, registry.get(self.db, "budget"), {
            "lease_epoch": worker.epoch,
            "lease_expires_ts": time.time() + 3600,
        })
        self.assertTrue(worker2._compta_en_echec, "ligne en attente non vue")
        self.assertTrue(worker2.budget_ok())  # la réparation réussit
        # la réparation a écrit la ligne et effacé le marqueur
        self.assertIsNone(registry.pending_spend_get(self.db, "budget"))
        # ligne incohérente : index négatif, réparation impossible
        self.assertTrue(registry.pending_spend_put(self.db, "budget", -1, "tour", ""))
        worker3 = AgentWorker(runner, registry.get(self.db, "budget"), {
            "lease_epoch": worker.epoch,
            "lease_expires_ts": time.time() + 3600,
        })
        self.assertTrue(worker3._compta_en_echec)
        self.assertFalse(worker3.budget_ok())
        self.assertIsNotNone(registry.pending_spend_get(self.db, "budget"))

    def test_marqueur_non_pose_refuse_le_tour(self):
        """Verdict L13 B5 : si la trace comptable ne peut pas être posée en
        base, le tour ne démarre pas (aucune dépense à l'aveugle)."""
        runner, worker = self._worker(plafond=10.0, harness="claude")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("ameesh.registry.pending_spend_put",
                               side_effect=db_mod.DbError("base indisponible")):
                self.assertFalse(worker.run_turn(
                    {"kind": "prompt", "prompt": "tour", "ids": []}))
            self.assertTrue(worker._compta_en_echec)
            self.assertIsNone(registry.pending_spend_get(self.db, "budget"))
            self.assertEqual(self.db.query(
                "select count(*)::int as n from turn_costs")[0]["n"], 0)
        finally:
            worker.watchdog_stop.set()

    def test_la_comptabilite_ne_requiert_pas_le_binaire_du_harnais(self):
        """Relire le flux d'un tour passé n'exige pas le binaire : il a pu être
        désinstallé depuis (ou n'existe pas, comme en CI). La ligne s'écrit."""
        runner, worker = self._worker(plafond=10.0, harness="deepseek")
        with open(worker._path("events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "session", "sessionId": "s1",
                                 "model": "deepseek-pro"}) + "\n")
            fh.write(json.dumps({
                "type": "status", "phase": "step_end",
                "usage": {"inputTokens": 1_000_000, "cacheReadTokens": 0,
                          "outputTokens": 0},
            }) + "\n")
        self.assertTrue(worker._compta_marque(0, "tour", ""))
        sans = {k: v for k, v in os.environ.items()
                if not k.endswith("_BIN") and not k.endswith("_BIN_DIR")}
        with mock.patch.dict(os.environ, sans, clear=True), \
                mock.patch("ameesh.adapters.shutil.which", return_value=None):
            with self.assertRaises(adapters.HarnessMissing):
                adapter_for("deepseek")  # le binaire est bien introuvable
            worker._compta_termine()
        self.assertFalse(worker._compta_en_echec)
        ligne = self.db.query(
            "select model, usd from turn_costs where agent = 'budget' "
            "order by id desc limit 1")[0]
        self.assertEqual(ligne["model"], "deepseek-pro")
        self.assertAlmostEqual(float(ligne["usd"]), 0.55, places=6)


class AnnouncedModelTest(PgTestCase):
    """Verdict codex3 L13 B4 : le modèle **annoncé par le flux** est celui qu'on
    facture, y compris quand l'écriture du grand livre échoue et qu'un autre
    worker répare. L'annonce vit donc en base dès qu'elle arrive."""

    def setUp(self):
        super().setUp()
        self.name = "annonce"
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("DELETE FROM spend_pending WHERE agent = %s", (self.name,))
        cwd = os.path.join(self.tmp, "work", self.name)
        os.makedirs(cwd, exist_ok=True)
        self.register(self.name, "deepseek", cwd=cwd)
        registry.upsert(self.db, self.name, model="deepseek-flash")  # lancement : flash
        cfg = dataclasses.replace(self.cfg, budget_usd_per_hour=0.4, budget_check_interval=0.0)
        self.runner = Runner(cfg, self.db, once=True)
        self.lease = registry.claim(self.db, self.name, self.runner.runner_id, 3600)
        self.workers = []
        self.worker = self._worker()
        self.bins = os.path.join(self.tmp, "bin-annonce")
        os.makedirs(self.bins, exist_ok=True)

    def tearDown(self):
        for worker in self.workers:
            worker.watchdog_stop.set()
        super().tearDown()

    def _worker(self) -> AgentWorker:
        worker = AgentWorker(self.runner, registry.get(self.db, self.name), self.lease)
        self.workers.append(worker)
        return worker

    def _harnais(self, *events: dict) -> dict:
        """Un faux `dsh` qui joue ces événements, comme le vrai flux."""
        chemin = os.path.join(self.bins, "dsh")
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env python3\n")
            for event in events:
                fh.write("print(%r, flush=True)\n" % json.dumps(event))
        os.chmod(chemin, 0o700)
        return {"AMEESH_BIN_DIR": self.bins}

    def _flux_pro(self, *, annonces: int = 1) -> dict:
        evenements = [{"type": "session", "sessionId": "s", "model": "deepseek-pro"}
                      for _ in range(annonces)]
        evenements += [{"type": "status", "phase": "step_end",
                        "usage": {"inputTokens": 1_000_000}},
                       {"type": "final", "text": "ok"}]
        return self._harnais(*evenements)

    def _tour(self, worker: AgentWorker, env: dict) -> bool:
        with mock.patch.dict(os.environ, env, clear=False):
            return worker.run_turn({"kind": "prompt", "prompt": "payé", "ids": []})

    def _ligne(self) -> dict:
        return self.db.query("select model, usd from turn_costs where agent = %s",
                             (self.name,))[0]

    def test_annonce_persistee_et_facturee_par_un_autre_worker_apres_panne(self):
        """La sonde B4 : lancement flash, flux pro, grand livre en panne, nouveau
        worker : la réparation facture pro (0,55 $/M), pas flash (0,27)."""
        env = self._flux_pro()
        with mock.patch("ameesh.cost.CostBook.record",
                        side_effect=RuntimeError("grand livre indisponible")):
            self.assertTrue(self._tour(self.worker, env))
        marqueur = registry.pending_spend_get(self.db, self.name)
        self.assertEqual(marqueur["model"], "deepseek-pro", "annonce non persistée")
        autre = self._worker()
        self.assertTrue(autre._compta_en_echec)
        autorise = autre.budget_ok()  # réparation, puis décision du plafond
        ligne = self._ligne()
        self.assertEqual(ligne["model"], "deepseek-pro")
        self.assertAlmostEqual(float(ligne["usd"]), 0.55, places=6)
        self.assertFalse(autorise, "0,55 $ dépasse le plafond de 0,40 $/h")
        self.assertIsNone(registry.pending_spend_get(self.db, self.name))

    def test_annonce_ecrite_une_fois_par_changement_pas_a_chaque_ligne(self):
        env = self._flux_pro(annonces=5)
        reel = registry.pending_spend_set_model
        with mock.patch("ameesh.registry.pending_spend_set_model", side_effect=reel) as appels:
            self.assertTrue(self._tour(self.worker, env))
        self.assertEqual(appels.call_count, 1)
        self.assertEqual(self._ligne()["model"], "deepseek-pro")

    def test_annonce_identique_au_lancement_n_ecrit_rien_de_plus(self):
        env = self._harnais({"type": "session", "sessionId": "s", "model": "deepseek-flash"},
                            {"type": "status", "phase": "step_end", "usage": {"inputTokens": 1_000_000}},
                            {"type": "final", "text": "ok"})
        self.assertTrue(self._tour(self.worker, env))
        ligne = self._ligne()
        self.assertEqual(ligne["model"], "deepseek-flash")
        self.assertAlmostEqual(float(ligne["usd"]), 0.27, places=6)

    def test_annonce_non_persistee_suspend_et_ce_worker_repare_au_bon_tarif(self):
        """Si la base refuse l'annonce, les tours se suspendent (fail-closed) et la
        mémoire de CE worker garde le modèle : sa réparation facture pro."""
        env = self._flux_pro()
        with mock.patch("ameesh.registry.pending_spend_set_model",
                        side_effect=db_mod.DbError("base indisponible")), \
                mock.patch("ameesh.cost.CostBook.record",
                           side_effect=RuntimeError("grand livre indisponible")):
            self.assertTrue(self._tour(self.worker, env))
        self.assertTrue(self.worker._compta_en_echec)
        self.assertEqual(registry.pending_spend_get(self.db, self.name)["model"], "deepseek-flash")
        self.worker.budget_ok()  # la réparation de ce worker passe par sa mémoire
        ligne = self._ligne()
        self.assertEqual(ligne["model"], "deepseek-pro")
        self.assertAlmostEqual(float(ligne["usd"]), 0.55, places=6)

    def test_double_panne_puis_reprise_par_un_nouveau_worker_facture_le_modele_du_flux(self):
        """La sonde B4 résiduelle (codex3) : ni l'annonce ni la ligne du grand livre
        n'ont pu s'écrire ; le marqueur garde flash ; un NOUVEAU worker (pas la
        mémoire du premier) doit facturer pro, relu dans les événements du tour."""
        env = self._flux_pro()
        with mock.patch("ameesh.registry.pending_spend_set_model",
                        side_effect=db_mod.DbError("base indisponible")), \
                mock.patch("ameesh.cost.CostBook.record",
                           side_effect=RuntimeError("grand livre indisponible")):
            self.assertTrue(self._tour(self.worker, env))
        self.assertEqual(registry.pending_spend_get(self.db, self.name)["model"], "deepseek-flash")
        self.worker.watchdog_stop.set()
        nouveau = self._worker()
        self.assertEqual(nouveau._annonce_ram, "", "pas de mémoire héritée")
        autorise = nouveau.budget_ok()
        ligne = self._ligne()
        self.assertEqual(ligne["model"], "deepseek-pro")
        self.assertAlmostEqual(float(ligne["usd"]), 0.55, places=6)
        self.assertFalse(autorise, "0,55 $ dépasse le plafond de 0,40 $/h")

    def test_flux_sans_annonce_la_reparation_garde_le_modele_du_marqueur(self):
        sans = self._harnais({"type": "status", "phase": "step_end", "usage": {"inputTokens": 1_000_000}},
                             {"type": "final", "text": "ok"})
        with mock.patch("ameesh.cost.CostBook.record", side_effect=RuntimeError("panne")):
            self.assertTrue(self._tour(self.worker, sans))
        self.worker.watchdog_stop.set()
        self._worker().budget_ok()
        ligne = self._ligne()
        self.assertEqual(ligne["model"], "deepseek-flash")
        self.assertAlmostEqual(float(ligne["usd"]), 0.27, places=6)

    def test_flux_illisible_ou_absent_ne_change_pas_le_modele_et_ne_plante_pas(self):
        env = self._flux_pro()
        with mock.patch("ameesh.cost.CostBook.record", side_effect=RuntimeError("panne")):
            self.assertTrue(self._tour(self.worker, env))
        self.worker.watchdog_stop.set()
        nouveau = self._worker()
        with mock.patch.object(AgentWorker, "_path", side_effect=OSError("flux perdu")):
            self.assertEqual(nouveau._annonce_des_evenements(0), "")
        # le tour d'avant ne se mélange pas à celui-ci : l'index de départ borne la relecture
        self.assertEqual(nouveau._annonce_des_evenements(10_000), "")

    def test_marqueur_absent_pendant_le_tour_est_un_echec_pas_une_autorisation(self):
        env = self._flux_pro()
        with mock.patch("ameesh.registry.pending_spend_set_model", return_value=False):
            self.assertTrue(self._tour(self.worker, env))
        # le tour a pourtant été compté (le marqueur existait pour `_compta_termine`)
        self.assertEqual(self._ligne()["model"], "deepseek-pro")
        self.assertFalse(self.worker._compta_en_echec, "la ligne écrite lève la suspension")

    def test_la_memoire_de_l_annonce_est_remise_a_zero_par_le_marqueur_d_un_tour_neuf(self):
        """Une annonce gardée parce que le grand livre était en panne ne doit pas
        coller à un tour suivant : le marqueur d'un tour neuf l'efface."""
        env = self._flux_pro()
        with mock.patch("ameesh.cost.CostBook.record", side_effect=RuntimeError("panne")):
            self.assertTrue(self._tour(self.worker, env))
        self.assertEqual(self.worker._annonce_ram, "deepseek-pro")
        self.assertTrue(self.worker._compta_marque(0, "suivant", "deepseek-flash"))
        self.assertEqual(self.worker._annonce_ram, "")

    def test_un_tour_neuf_n_herite_pas_de_l_annonce_du_precedent(self):
        env = self._flux_pro()
        self.assertTrue(self._tour(self.worker, env))
        self.assertEqual(self.worker._annonce_ram, "")
        sans_annonce = self._harnais({"type": "status", "phase": "step_end",
                                      "usage": {"inputTokens": 1_000_000}},
                                     {"type": "final", "text": "ok"})
        self.db.execute("DELETE FROM turn_costs")
        self.assertTrue(self._tour(self.worker, sans_annonce))
        ligne = self._ligne()
        self.assertEqual(ligne["model"], "deepseek-flash")  # le lancement, pas le tour d'avant
        self.assertAlmostEqual(float(ligne["usd"]), 0.27, places=6)


if __name__ == "__main__":
    unittest.main()

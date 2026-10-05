# SPDX-License-Identifier: AGPL-3.0-only
"""Exécuteur : tours, reprise de session, baux, expiration, réveil sur NOTIFY."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from ameesh import adapters, db as db_mod, mail, registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase

HARNESSES = {
    "claude": {"session": "claude-sess-1", "reprise": ["--resume", "claude-sess-1"]},
    "codex": {"session": "codex-thread-1", "reprise": ["resume", "codex-thread-1"]},
    "deepseek": {"session": "dsh-session-1", "reprise": ["--session-id", "dsh-session-1"]},
}


class RunnerTest(PgTestCase):
    def _cwd(self, name: str) -> str:
        path = os.path.join(self.tmp, "work", name)
        os.makedirs(path, exist_ok=True)
        return path

    def _messages(self) -> list[str]:
        return [m["body"] for m in mail.unread(self.db, "orchestrateur")]

    # -- tours et sessions -------------------------------------------------
    def test_un_tour_par_harnais_puis_reprise_de_session(self):
        for harness, attendu in HARNESSES.items():
            with self.subTest(harness=harness):
                name = "agent-" + harness
                cwd = self._cwd(name)
                self.assertEqual(
                    self.register(name, harness, cwd=cwd, prompt="premier tour").returncode, 0)
                proc = self.runner("--once", "--agents", name)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

                row = registry.get(self.db, name)
                self.assertEqual(row["session_id"], attendu["session"])
                self.assertEqual(int(row["turns"]), 1)
                self.assertEqual(row["status"], "idle")
                self.assertIn("consigne", row["status_text"])
                # l'événement brut du harnais est journalisé sur disque
                events = os.path.join(self.state, name, "events.jsonl")
                self.assertTrue(os.path.exists(events))
                with open(events, encoding="utf-8") as fh:
                    self.assertIn(attendu["session"], fh.read())

                # deuxième consigne : la session est reprise
                self.register(name, harness, prompt="deuxième tour")
                proc = self.runner("--once", "--agents", name)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                appels = [t for t in self.turns() if t["harness"] == harness]
                self.assertEqual(len(appels), 2, appels)
                self.assertEqual(appels[1]["argv"][-1], "deuxième tour")
                for marqueur in attendu["reprise"]:
                    self.assertIn(marqueur, appels[1]["argv"])
                self.assertEqual(int(registry.get(self.db, name)["turns"]), 2)

    def test_cout_et_session_enregistres(self):
        cwd = self._cwd("couteux")
        self.register("couteux", "claude", cwd=cwd, prompt="tour")
        self.runner("--once", "--agents", "couteux")
        row = registry.get(self.db, "couteux")
        self.assertAlmostEqual(float(row["spent_usd"]), 0.0125, places=4)

    def test_dry_run_n_execute_rien(self):
        cwd = self._cwd("sec")
        self.register("sec", "claude", cwd=cwd, prompt="tour")
        proc = self.runner("--once", "--dry-run", "--agents", "sec")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("dry-run", proc.stdout)
        self.assertEqual(self.turns(), [])
        row = registry.get(self.db, "sec")
        self.assertEqual(int(row["turns"]), 0)

    def test_harnais_absent_marque_bloque_sans_boucle(self):
        cwd = self._cwd("sansharnais")
        self.register("sansharnais", "claude", cwd=cwd, prompt="tour")
        env = self.env(AMEESH_CLAUDE_BIN=os.path.join(self.tmp, "claude-inexistant"))
        proc = self.runner("--once", "--agents", "sansharnais", env=env)
        self.assertEqual(proc.returncode, 0)
        row = registry.get(self.db, "sansharnais")
        self.assertEqual(row["status"], "blocked")
        self.assertIn("introuvable", row["last_error"])
        self.assertEqual(self.turns(), [])
        self.assertEqual(proc.stdout.count("introuvable"), 1)

    def test_consigne_restauree_apres_echec_au_demarrage(self):
        """Sonde codex3 (6) : un tour qui ne démarre pas ne perd pas sa consigne."""
        cwd = self._cwd("restaure")
        self.register("restaure", "claude", cwd=cwd, prompt="consigne à retrouver")
        env = self.env(AMEESH_CLAUDE_BIN=os.path.join(self.tmp, "claude-absent"))
        proc = self.runner("--once", "--agents", "restaure", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        row = registry.get(self.db, "restaure")
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["pending_prompt"], "consigne à retrouver")
        self.assertIsNone(row["current_prompt"])

        # le tour suivant repart avec la même consigne, sans intervention
        proc = self.runner("--once", "--agents", "restaure")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.turns()), 1)
        self.assertEqual(self.turns()[0]["argv"][-1], "consigne à retrouver")
        self.assertIsNone(registry.get(self.db, "restaure")["pending_prompt"])

    def test_battement_arrete_le_harnais_si_la_base_reste_injoignable(self):
        """Sonde codex3 (5) : un DbError ne doit pas laisser le harnais sans bail."""
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "bravo", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 3600},
        )
        with mock.patch.object(registry, "renew", side_effect=db_mod.DbError("base absente")):
            self.assertTrue(worker.renew())    # 1er échec : sursis
            self.assertTrue(worker.renew())    # 2e échec : sursis
            self.assertFalse(worker.renew())   # 3e : bail considéré perdu
        self.assertTrue(worker.lease_lost.is_set())

    def test_battement_arrete_le_harnais_a_l_echeance_connue(self):
        """Même avec un seul échec, si l'échéance du bail est atteinte on arrête."""
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "charlie", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() - 1},
        )
        with mock.patch.object(registry, "renew", side_effect=db_mod.DbError("base absente")):
            self.assertFalse(worker.renew())
        self.assertTrue(worker.lease_lost.is_set())

    def test_dossier_absent_marque_bloque(self):
        self.register("sansdossier", "claude", cwd=os.path.join(self.tmp, "jamais-cree"),
                      prompt="tour")
        proc = self.runner("--once", "--agents", "sansdossier")
        self.assertEqual(proc.returncode, 0)
        row = registry.get(self.db, "sansdossier")
        self.assertEqual(row["status"], "blocked")
        self.assertIn("dossier", row["last_error"])

    # -- baux --------------------------------------------------------------
    def test_deux_executeurs_se_disputent_un_bail(self):
        """Deux processus concurrents : un seul tour, un 0 et un 3."""
        cwd = self._cwd("coureur")
        self.register("coureur", "claude", cwd=cwd, prompt="course")
        env = self.env(AMEESH_TEST_SLEEP="4")
        premier = self.runner_popen("--once", "--agents", "coureur", env=env)
        second = self.runner_popen("--once", "--agents", "coureur", env=env)
        sortie1, _ = premier.communicate(timeout=60)
        sortie2, _ = second.communicate(timeout=60)
        codes = sorted([premier.returncode, second.returncode])
        self.assertEqual(codes, [0, 3], "codes %r\n%s\n%s" % (codes, sortie1, sortie2))
        self.assertEqual(len(self.turns()), 1, "un seul tour doit avoir lieu")
        gagnant = [s for s in (sortie1, sortie2) if "bail acquis" in s]
        perdant = [s for s in (sortie1, sortie2) if "bail acquis" not in s]
        self.assertEqual(len(gagnant), 1)
        self.assertIn("aucun travail disponible", perdant[0])
        self.assertEqual(int(registry.get(self.db, "coureur")["turns"]), 1)
        # le gagnant a rendu le bail en sortant
        self.assertIsNone(registry.get(self.db, "coureur")["lease_owner"])

    def test_bail_expire_repris_par_un_autre_executeur(self):
        cwd = self._cwd("tombe")
        self.register("tombe", "claude", cwd=cwd, prompt="après expiration")
        registry.upsert(self.db, "tombe", harness="claude", host=self.cfg.host, cwd=cwd)
        registry.claim(self.db, "tombe", "runner-mort", 1.0)
        time.sleep(1.3)  # l'exécuteur est mort, son bail a expiré
        proc = self.runner("--once", "--agents", "tombe")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        row = registry.get(self.db, "tombe")
        self.assertGreaterEqual(int(row["lease_epoch"]), 2)
        self.assertEqual(int(row["turns"]), 1)
        self.assertEqual(len(self.turns()), 1)

    def test_bail_vivant_n_est_pas_vole(self):
        cwd = self._cwd("vivant")
        self.register("vivant", "claude", cwd=cwd, prompt="pas touche")
        registry.upsert(self.db, "vivant", harness="claude", host=self.cfg.host, cwd=cwd)
        registry.claim(self.db, "vivant", "runner-actif", 300)
        proc = self.runner("--once", "--agents", "vivant")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.turns(), [])
        self.assertEqual(registry.get(self.db, "vivant")["lease_owner"], "runner-actif")

    def test_bail_perdu_interrompt_le_tour(self):
        """Si le bail est volé, le harnais est terminé et le tour n'est pas compté."""
        cwd = self._cwd("vole")
        self.register("vole", "claude", cwd=cwd, prompt="long tour")
        env = self.env(AMEESH_TEST_SLEEP="12", AMEESH_LEASE_TTL="3")
        proc = self.runner_popen("--agents", "vole", "--lease-ttl", "3", "--poll", "2", env=env)
        lignes: list[str] = []
        threading.Thread(
            target=lambda: [lignes.append(line) for line in proc.stdout], daemon=True
        ).start()
        try:
            self.wait_for(lambda: len(self.turns()) == 1, timeout=20)  # le tour a démarré
            debut = time.monotonic()
            self.db.execute(
                "UPDATE agent_registry SET lease_owner = 'voleur', "
                "lease_epoch = lease_epoch + 1, lease_expires_at = now() + interval '60 seconds', "
                "status = 'running' WHERE name = 'vole'"
            )
            # Deux issues également justes : le renouvellement voit l'epoch
            # volé (« bail perdu ») ou l'échéance est atteinte avant l'appel
            # (« échéance du bail atteinte »). Dans les deux cas le harnais est
            # arrêté et le tour n'est pas compté.
            self.wait_for(
                lambda: any(("bail perdu" in l or "échéance du bail atteinte" in l)
                            for l in lignes),
                timeout=10)
            self.assertLess(
                time.monotonic() - debut, 7.0,
                "le harnais aurait dû être terminé par le battement de bail (12 s de sommeil)",
            )
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        # end_turn est fencé : le tour volé n'est pas compté
        self.assertEqual(int(registry.get(self.db, "vole")["turns"]), 0)

    # -- NOTIFY ------------------------------------------------------------
    def test_notify_reveille_un_executeur_en_service(self):
        cwd = self._cwd("dort")
        self.register("dort", "claude", cwd=cwd)
        proc = self.runner_popen("--max-turns", "1", "--poll", "60")
        try:
            time.sleep(2.0)  # LISTEN établi (pilote psql : ~0,5 s), aucun tour à faire
            self.assertEqual(self.turns(), [])
            debut = time.monotonic()
            envoi = self.cli("send", "dort", "réveille-toi",
                             env=self.env(AGENT_MAIL_NAME="orchestrateur"))
            self.assertEqual(envoi.returncode, 0)
            sortie, _ = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        self.assertLess(time.monotonic() - debut, 25.0)
        self.assertEqual(proc.returncode, 0, sortie)
        self.assertIn("message pour dort : réveil du tour", sortie)
        tours = self.turns()
        self.assertEqual(len(tours), 1, sortie)
        # le contenu du message est dans la consigne (pas un renvoi vers inbox)
        self.assertIn("réveille-toi", tours[0]["argv"][-1])
        self.assertIn(adapters.AUTHORITY_NOTE, tours[0]["argv"][-1])
        # le message a été remis, le tour a été compté
        self.assertEqual(mail.unread(self.db, "dort"), [])
        self.assertEqual(int(registry.get(self.db, "dort")["turns"]), 1)

    def test_notify_lease_reveille_apres_liberation(self):
        """Un bail qui se libère réveille les autres exécuteurs de l'hôte.

        Le sondage est à 60 s : sans NOTIFY, le test ne passerait pas.
        """
        cwd = self._cwd("libre")
        self.register("libre", "claude", cwd=cwd, prompt="reprends")
        registry.upsert(self.db, "libre", harness="claude", host=self.cfg.host, cwd=cwd)
        registry.claim(self.db, "libre", "runner-parti", 300)

        proc = self.runner_popen("--max-turns", "1", "--poll", "60")
        try:
            time.sleep(2.0)  # LISTEN établi
            self.assertEqual(self.turns(), [], "rien ne doit tourner sous bail")
            debut = time.monotonic()
            registry.release(self.db, "libre", "runner-parti", 1)
            sortie, _ = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        self.assertLess(time.monotonic() - debut, 25.0)
        self.assertEqual(proc.returncode, 0, sortie)
        self.assertEqual(len(self.turns()), 1, sortie)

    # -- cycle de vie ------------------------------------------------------
    def test_agent_arrete_n_est_plus_reclame(self):
        self.register("fini", "claude", cwd=self._cwd("fini"), prompt="non")
        stop = self.runner("stop", "fini")
        self.assertEqual(stop.returncode, 0)
        self.assertEqual(registry.get(self.db, "fini")["status"], "stopped")
        proc = self.runner("--once")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.turns(), [])

    def test_register_ecrit_modele_budget_et_consigne(self):
        self.register("riche", "codex", cwd=self._cwd("riche"), prompt="tour",
                      model="gpt-5", budget="3.5", chantier="nexlink")
        row = registry.get(self.db, "riche")
        self.assertEqual(row["harness"], "codex")
        self.assertEqual(row["model"], "gpt-5")
        self.assertEqual(float(row["budget_usd"]), 3.5)
        self.assertEqual(row["chantier"], "nexlink")
        self.assertEqual(row["pending_prompt"], "tour")
        self.assertEqual(row["status"], "queued")

    def test_anomalie_du_harnais_marque_bloque(self):
        cwd = self._cwd("plante")
        self.register("plante", "codex", cwd=cwd, prompt="tour")
        env = self.env(AMEESH_TEST_EXIT="2")
        proc = self.runner("--once", "--agents", "plante", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        row = registry.get(self.db, "plante")
        self.assertEqual(row["status"], "blocked")
        self.assertIn("code 2", row["last_error"])
        self.assertEqual(int(row["turns"]), 1)  # le tour a eu lieu, il a échoué
        # B6a : un tour en échec ne consomme pas la consigne
        self.assertEqual(row["pending_prompt"], "tour")
        self.assertIsNone(row["current_prompt"])

    def test_consigne_survit_a_un_harnais_tue(self):
        """B6a (variante SIGKILL) : le harnais tué ne fait pas perdre la consigne."""
        cwd = self._cwd("tue")
        self.register("tue", "claude", cwd=cwd, prompt="consigne d'un tour tué")
        env = self.env(AMEESH_TEST_KILL_SELF="1")
        proc = self.runner("--once", "--agents", "tue", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        row = registry.get(self.db, "tue")
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["pending_prompt"], "consigne d'un tour tué")
        self.assertIsNone(row["current_prompt"])

    def test_renouvellement_borne_par_l_echeance_meme_si_l_appel_bloque(self):
        """Sonde codex3 : un renew qui pend ne dépasse pas l'échéance du bail."""
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "golf", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 1},
        )

        def bloque(*_args, **_kwargs):
            time.sleep(30)
            return time.time() + 300

        debut = time.monotonic()
        with mock.patch.object(registry, "renew", side_effect=bloque):
            self.assertFalse(worker.renew())
        self.assertLess(time.monotonic() - debut, 3.0)
        self.assertTrue(worker.lease_lost.is_set())

    def test_le_harnais_recoit_son_bail_dans_l_environnement(self):
        """L'identité du harnais est liée au bail : nom, runner et epoch."""
        cwd = self._cwd("lie")
        self.register("lie", "claude", cwd=cwd, prompt="tour lié")
        proc = self.runner("--once", "--agents", "lie")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        env = self.turns()[0]["env"]
        self.assertEqual(env["AGENT_MAIL_NAME"], "lie")
        self.assertTrue(env["AMEESH_RUNNER_ID"])
        # le tour tourne sous le premier bail ; le rendre en sortant incrémente
        # l'epoch en base, donc on compare au bail du moment, pas à l'après.
        self.assertEqual(int(env["AMEESH_LEASE_EPOCH"]), 1)

    def test_echeance_atteinte_arrete_sans_appeler_la_base(self):
        """Verdict codex3 B5a : plus de budget plancher après l'échéance.

        Réveil tardif : l'échéance est déjà passée, on n'appelle même pas
        `registry.renew` — on arrête le harnais immédiatement.
        """
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "india", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() - 1},
        )
        debut = time.monotonic()
        with mock.patch.object(registry, "renew") as appel:
            self.assertFalse(worker.renew())
        self.assertLess(time.monotonic() - debut, 0.3)
        appel.assert_not_called()
        self.assertTrue(worker.lease_lost.is_set())

    def test_budget_borne_par_le_temps_restant_sans_marge(self):
        """Le budget d'appel vaut le temps restant, pas un plancher de 0,5 s."""
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "juliette", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 0.3},
        )
        self.assertLessEqual(worker.call_budget(), 0.4)
        self.assertGreater(worker.call_budget(), 0.0)
        expire = AgentWorker(
            runner,
            {"name": "kilo", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() - 5},
        )
        self.assertTrue(expire.deadline_passed())
        self.assertEqual(expire.call_budget(), 0.0)  # aucun plancher au-delà

        # entré juste avant l'échéance : le budget vaut le temps restant exact
        juste = AgentWorker(
            runner,
            {"name": "lima", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 0.02},
        )
        self.assertLessEqual(juste.call_budget(), 0.03)

    def test_appel_entame_juste_avant_l_echeance_ne_survit_pas(self):
        """Verdict codex3 B5a : entré 20 ms avant l'échéance, l'appel est coupé."""
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "mike", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 0.05},
        )

        def bloque(*_args, **_kwargs):
            time.sleep(5)
            return time.time() + 300

        debut = time.monotonic()
        with mock.patch.object(registry, "renew", side_effect=bloque):
            self.assertFalse(worker.renew())
        ecoule = time.monotonic() - debut
        self.assertTrue(worker.lease_lost.is_set())
        self.assertLess(ecoule, 0.4, "l'appel ne doit pas survivre à l'échéance")

    def test_battement_se_reveille_a_l_echeance_du_bail(self):
        """B5a : with a long interval, the heartbeat still wakes at the deadline."""
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "delta", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 1},
        )
        stop = threading.Event()
        with mock.patch.object(registry, "renew", side_effect=db_mod.DbError("base absente")):
            thread = threading.Thread(target=worker._heartbeat, args=(stop,), daemon=True)
            thread.start()
            self.wait_for(lambda: worker.lease_lost.is_set(), timeout=5)
            stop.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_attente_du_battement_ne_depasse_pas_l_echeance(self):
        """Verdict codex3 B5a : `wait_timeout` n'a aucun plancher positif.

        Nouvelle sonde réelle : bail à +20 ms ; un plancher de 50 ms laissait
        l'ancien harnais vivant et `lease_lost=False` après l'échéance, pendant
        qu'un vrai remplaçant réclamait l'agent. Ici le vrai `_heartbeat` reçoit
        l'attente calculée : elle vaut le temps restant (~20 ms), et exactement
        0 une fois l'échéance passée.
        """
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "papa", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 0.02},
        )
        self.assertLessEqual(worker.wait_timeout(300), 0.03)
        self.assertGreaterEqual(worker.wait_timeout(300), 0.0)

        stop = mock.Mock()
        stop.wait.return_value = False  # un tour de boucle, puis renew() coupe
        with mock.patch.object(AgentWorker, "renew", return_value=False) as renew:
            worker._heartbeat(stop)
        renew.assert_called_once()
        attente = stop.wait.call_args[0][0]
        self.assertLessEqual(attente, 0.03, "le battement dort au-delà de l'échéance")
        self.assertGreaterEqual(attente, 0.0)

        expire = AgentWorker(
            runner,
            {"name": "quebec", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() - 1},
        )
        self.assertEqual(expire.wait_timeout(300), 0.0)  # aucun plancher
        stop_expire = mock.Mock()
        stop_expire.wait.return_value = False
        with mock.patch.object(AgentWorker, "renew", return_value=False) as renew2:
            expire._heartbeat(stop_expire)
        self.assertEqual(stop_expire.wait.call_args[0][0], 0.0)
        renew2.assert_called_once()

    def test_branche_bail_perdu_ne_perd_pas_la_consigne(self):
        """Sonde codex3 (2) : la clôture d'un tour au bail perdu restaure la consigne."""
        cwd = self._cwd("perdu")
        self.register("perdu", "claude", cwd=cwd, prompt="consigne du tour perdu")
        env = self.env(AMEESH_TEST_SLEEP="12", AMEESH_LEASE_TTL="3")
        proc = self.runner_popen("--agents", "perdu", "--lease-ttl", "3", "--poll", "2", env=env)
        try:
            self.wait_for(lambda: len(self.turns()) == 1, timeout=20)
            self.db.execute(
                "UPDATE agent_registry SET lease_expires_at = now() - interval '1 second' "
                "WHERE name = 'perdu'")
            self.wait_for(
                lambda: registry.get(self.db, "perdu")["pending_prompt"] is not None, timeout=15)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        row = registry.get(self.db, "perdu")
        self.assertEqual(row["pending_prompt"], "consigne du tour perdu")
        self.assertIsNone(row["current_prompt"])
        self.assertEqual(int(row["turns"]), 0)

    def test_terminate_tue_le_groupe_de_processus(self):
        """Sonde codex3 (descendants) : un enfant du harnais ne survit pas."""
        pidfile = os.path.join(self.tmp, "enfant.pid")
        parent = subprocess.Popen(
            [sys.executable, "-c",
             "import os, signal, subprocess, sys, time;"
             "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
             "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
             "open(sys.argv[1], 'w').write(str(child.pid));"
             "time.sleep(60)", pidfile],
            start_new_session=True,
        )
        for _ in range(50):
            if os.path.exists(pidfile):
                break
            time.sleep(0.1)
        enfant_pid = int(open(pidfile, encoding="utf-8").read())
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "foxtrot", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 3600},
        )
        worker.proc = parent
        worker.pgid = parent.pid
        try:
            worker.terminate(grace=0)
            self.assertIsNotNone(parent.poll())
            self.wait_for(
                lambda: not os.path.exists("/proc/%d" % enfant_pid), timeout=5)
        finally:
            if parent.poll() is None:
                parent.kill()
            try:
                os.kill(enfant_pid, signal.SIGKILL)
            except OSError:
                pass

    def test_terminate_tue_un_descendant_qui_ignore_sigterm(self):
        """Sonde codex3 B5b : le parent meurt sur SIGTERM, l'enfant l'ignore.

        L'escalade ne doit pas s'arrêter à la mort du parent.
        """
        pidfile = os.path.join(self.tmp, "enfant-recalcitrant.pid")
        parent = subprocess.Popen(
            [sys.executable, "-c",
             "import os, signal, subprocess, sys, time;"
             "child = subprocess.Popen([sys.executable, '-c',"
             " 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']);"
             "open(sys.argv[1], 'w').write(str(child.pid));"
             "time.sleep(60)", pidfile],
            start_new_session=True,
        )
        for _ in range(50):
            if os.path.exists(pidfile):
                break
            time.sleep(0.1)
        enfant_pid = int(open(pidfile, encoding="utf-8").read())
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "hotel", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 3600},
        )
        worker.proc = parent
        worker.pgid = parent.pid
        try:
            worker.terminate(grace=1.0)
            self.assertIsNotNone(parent.poll())
            self.wait_for(lambda: not os.path.exists("/proc/%d" % enfant_pid), timeout=5)
        finally:
            if parent.poll() is None:
                parent.kill()
            try:
                os.kill(enfant_pid, signal.SIGKILL)
            except OSError:
                pass

    def test_terminate_escalade_vers_sigkill(self):
        """B5b : un harnais qui ignore SIGTERM est tué, pas laissé vivant."""
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
            start_new_session=True)  # comme un vrai harnais : son propre groupe
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "echo", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 3600},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        try:
            debut = time.monotonic()
            worker.terminate()
            # La mort du processus est un effet du SIGKILL, distinct du retour de
            # `terminate` : on attend la sortie (test stable sous charge), tout
            # en gardant la borne sémantique de l'escalade.
            self.wait_for(lambda: proc.poll() is not None, timeout=12, interval=0.02)
            self.assertLess(time.monotonic() - debut, 12.0)
            self.assertLess(proc.returncode, 0)  # tué par un signal (SIGKILL)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_terminate_moissonne_le_zombie_sans_attendre_deux_secondes(self):
        """Constat codex3 b731f4f : un zombie est compté vivant par killpg(0).

        `terminate(hard=True)` doit moissonner l'enfant direct après le SIGKILL ;
        sinon le zombie garde le groupe « vivant » et l'arrêt attend 2 s pour
        rien, avec un faux « groupe toujours vivant après SIGKILL ».
        """
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "romeo", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 3600},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        try:
            debut = time.monotonic()
            worker.terminate(grace=0, hard=True)
            ecoule = time.monotonic() - debut
            self.wait_for(lambda: proc.poll() is not None, timeout=3, interval=0.01)
            self.assertFalse(AgentWorker._group_alive(proc.pid))
            self.assertLess(ecoule, 1.0, "un zombie ne doit pas faire durer l'arrêt 2 s")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_le_sigkill_part_avant_tout_journal(self):
        """Sonde codex3 b731f4f : le journal avant le signal laissait, sous
        charge, un remplaçant réclamer pendant que l'ancien harnais vivait.

        Ici l'ordre est instrumenté : sur une échéance atteinte, le premier
        événement doit être le signal, pas une écriture de journal.
        """
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "tango", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() - 1},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        ordre: list[tuple] = []
        vrai_signal = AgentWorker._signal_group

        def signal_trace(pgid, cible, sig):
            ordre.append(("signal", sig))
            vrai_signal(pgid, cible, sig)

        def log_trace(message):
            ordre.append(("log", message))

        try:
            with mock.patch.object(AgentWorker, "_signal_group",
                                   staticmethod(signal_trace)), \
                    mock.patch("ameesh.runner.log", side_effect=log_trace):
                self.assertFalse(worker.renew())
            self.assertTrue(worker.lease_lost.is_set())
            self.assertTrue(ordre, "l'arrêt doit être tracé")
            self.assertEqual(ordre[0][0], "signal", "un journal précède le SIGKILL")
            self.assertEqual(ordre[0][1], signal.SIGKILL)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_panne_de_base_definitive_tue_avant_tout_journal(self):
        """Revue codex3 de 852c8da : au 3e échec de base, le journal partait
        avant le test définitif et le SIGKILL.

        Un journal bloqué ne doit pas retarder l'arrêt : ici l'ordre est
        instrumenté, le premier événement doit être le signal.
        """
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "uniform", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 3600},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        worker.renew_failures = AgentWorker.MAX_RENEW_FAILURES - 1
        ordre: list[tuple] = []
        vrai_signal = AgentWorker._signal_group

        def signal_trace(pgid, cible, sig):
            ordre.append(("signal", sig))
            vrai_signal(pgid, cible, sig)

        def log_trace(message):
            ordre.append(("log", message))

        try:
            with mock.patch.object(AgentWorker, "_signal_group",
                                   staticmethod(signal_trace)), \
                    mock.patch("ameesh.runner.log", side_effect=log_trace), \
                    mock.patch.object(registry, "renew",
                                      side_effect=db_mod.DbError("base injoignable")):
                self.assertFalse(worker.renew())
            self.assertTrue(worker.lease_lost.is_set())
            self.assertTrue(ordre, "l'arrêt doit être tracé")
            self.assertEqual(ordre[0][0], "signal", "un journal précède le SIGKILL")
            self.assertEqual(ordre[0][1], signal.SIGKILL)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_journal_bloque_ne_bloque_pas_le_battement(self):
        """Sonde codex3 B5a-N : un puits de journal saturé ne doit pas
        immobiliser le fil du battement.

        Le diagnostic d'une erreur non définitive passe par une file bornée
        (perte comptée) : `renew` rend la main tout de suite, puis l'échéance
        tue quand même le harnais.
        """
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "xray", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 30},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        bloque = threading.Event()

        def puits_bloque(message):
            bloque.wait(2)  # le puits ne rend pas la main

        try:
            with mock.patch("ameesh.runner.log", side_effect=puits_bloque), \
                    mock.patch.object(registry, "renew",
                                      side_effect=db_mod.DbError("base injoignable")):
                debut = time.monotonic()
                self.assertTrue(worker.renew())  # 1er échec : non définitif
                self.assertLess(time.monotonic() - debut, 0.3,
                                "le journal bloqué immobilise le battement")
                self.assertFalse(worker.lease_lost.is_set())
                # l'échéance tombe : le battement doit encore pouvoir arrêter
                worker.lease_deadline = time.time() - 1
                bloque.set()  # libère le puits pour la fin du test
                self.assertFalse(worker.renew())
            self.assertTrue(worker.lease_lost.is_set())
            self.wait_for(lambda: proc.poll() is not None, timeout=5, interval=0.02)
        finally:
            bloque.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_veilleur_arrete_meme_battement_bloque(self):
        """Sonde codex3 B5a-N traitée par classe : le veilleur d'échéance est
        indépendant du battement.

        Même si `renew` ne rend jamais la main (journal, requête, fichier),
        le harnais est arrêté à l'échéance connue du bail.
        """
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "whiskey", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 0.3},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        bloque = threading.Event()
        stop = threading.Event()
        try:
            with mock.patch.object(AgentWorker, "renew",
                                   side_effect=lambda: bloque.wait(5)):
                thread = threading.Thread(target=worker._heartbeat, args=(stop,), daemon=True)
                thread.start()
                self.wait_for(lambda: worker.lease_lost.is_set(), timeout=5, interval=0.01)
                # La coupe est un effet du veilleur, distinct de `lease_lost` :
                # on attend la sortie du processus plutôt qu'une durée fixe
                # (test instable sous charge CPU, constat mesh-design).
                self.wait_for(lambda: proc.poll() is not None, timeout=10, interval=0.02)
            self.assertIsNotNone(proc.poll(), "le veilleur doit tuer à l'échéance")
        finally:
            bloque.set()
            stop.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_veilleur_ne_tue_pas_un_bail_renouvele(self):
        """Le veilleur n'est pas un minuteur : une échéance repoussée par un
        renouvellement réussi ne doit pas tuer le harnais."""
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "yankee", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 0.25},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        try:
            worker.ensure_watchdog()
            time.sleep(0.1)
            worker.lease_deadline = time.time() + 1.5  # renouvellement réussi
            time.sleep(0.4)  # bien au-delà de l'ancienne échéance
            self.assertFalse(worker.lease_lost.is_set(), "un bail renouvelé a été tué")
            self.assertIsNone(proc.poll(), "le harnais doit rester vivant")
        finally:
            worker.watchdog_stop.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_veilleur_reste_actif_pendant_l_arret_escalade(self):
        """Sonde codex3 B5a-S : `stopping` (arrêt gracieux) ne doit pas
        relâcher le veilleur.

        Un harnais qui ignore SIGTERM vit pendant les 3 s de grâce ; si le bail
        expire dans cette fenêtre, un remplaçant peut le réclamer. Le veilleur
        doit rester actif jusqu'à la mort du harnais ou le relâchement du bail.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
             " time.sleep(60)"],
            start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "zulu", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 0.3},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        worker.ensure_watchdog()
        try:
            worker.stopping.set()
            arret = threading.Thread(target=worker.terminate, daemon=True)
            debut = time.monotonic()
            arret.start()
            self.wait_for(lambda: worker.lease_lost.is_set(), timeout=3, interval=0.01)
            # La coupe est un effet du veilleur, distinct de `lease_lost` :
            # on attend la sortie du processus (test stable sous charge) tout en
            # exigeant qu'elle ait lieu pendant les 3 s de grâce SIGTERM.
            self.wait_for(lambda: proc.poll() is not None, timeout=5, interval=0.02)
            self.assertLess(time.monotonic() - debut, 3.5,
                            "le veilleur doit tuer pendant la grâce")
            arret.join(timeout=5)
        finally:
            worker.watchdog_stop.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_harnais_publie_apres_la_perte_du_bail_est_tue(self):
        """Sonde codex3 B5a-P : course de publication du Popen.

        Le veilleur conclut pendant que `Popen` n'est pas encore revenu (il voit
        `proc=None`) ; si la publication de `self.proc` ne recontrôle pas, un
        harnais sans stdout survit au bail jusqu'à la fin du tour.
        """
        cwd = self._cwd("pubrace")
        self.register("pubrace", "claude", cwd=cwd, prompt="tour")
        runner = Runner(self.cfg, self.db, once=True)
        lease = registry.claim(self.db, "pubrace", runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        agent = registry.get(self.db, "pubrace")
        worker = AgentWorker(runner, agent, lease)
        vrai_popen = subprocess.Popen

        def popen_retarde(*args, **kwargs):
            proc = vrai_popen(*args, **kwargs)
            worker.lease_lost.set()  # le veilleur a conclu avant la publication
            time.sleep(0.05)
            return proc

        fakebin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakebin")
        env = {"AMEESH_BIN_DIR": fakebin, "AMEESH_TEST_SLEEP": "5",
               "AMEESH_TEST_LOG": self.turns_log}
        debut = time.monotonic()
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("ameesh.runner.subprocess.Popen", side_effect=popen_retarde):
                ok = worker.run_turn({"kind": "prompt", "prompt": "tour", "ids": []})
            ecoule = time.monotonic() - debut
            self.assertFalse(ok)
            self.assertLess(ecoule, 2.0, "le harnais publié doit être tué, pas attendu")
            self.assertEqual(registry.get(self.db, "pubrace").get("pending_prompt"), "tour")
        finally:
            if worker.proc is not None and worker.proc.poll() is None:
                worker.proc.kill()
                worker.proc.wait(timeout=5)
            worker.watchdog_stop.set()

    def test_arret_relache_le_bail_apres_avoir_tue_le_harnais(self):
        """Le relâchement du bail ne doit jamais laisser un harnais vivant.

        Le terminate initial peut tomber avant la publication du `Popen` ; un
        arrêt dur après la jointure garantit que le bail n'est rendu qu'une fois
        le harnais tué (course de publication, sonde codex3 B5a-P).
        """
        runner = Runner(self.cfg, self.db, once=True)
        ordre: list[tuple] = []

        class FauxWorker:
            def __init__(self):
                self.stopping = threading.Event()
                self.wake = threading.Event()

            def terminate(self, grace=None, hard=False):
                ordre.append(("terminate", hard))

            def stop_group_now(self, reason, grace=None, hard=False):
                # point de passage unique : le double enregistre l'ordre réel
                ordre.append(("stop", reason, hard))
                self.terminate(grace=grace, hard=hard)

            def join(self, timeout=None):
                pass

            def release_lease(self):
                ordre.append(("release", None))

        runner.workers = {"fantome": FauxWorker()}
        runner.shutdown()
        self.assertIn(("terminate", True), ordre)
        self.assertLess(ordre.index(("terminate", True)), ordre.index(("release", None)))

    def test_publication_tardive_apres_relachement_est_tuee(self):
        """Sonde codex3 B5a-Q : une publication tardive après un shutdown qui a
        rendu le bail et arrêté le veilleur doit être tuée, même si l'ancienne
        échéance n'est pas encore atteinte.
        """
        cwd = self._cwd("pubshutdown")
        self.register("pubshutdown", "claude", cwd=cwd, prompt="tour")
        runner = Runner(self.cfg, self.db, once=True)
        lease = registry.claim(self.db, "pubshutdown", runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        agent = registry.get(self.db, "pubshutdown")
        worker = AgentWorker(runner, agent, lease)
        vrai_popen = subprocess.Popen

        def popen_retarde(*args, **kwargs):
            proc = vrai_popen(*args, **kwargs)
            # shutdown a rendu le bail et arrêté le veilleur pendant la création
            worker.stopping.set()
            worker.watchdog_stop.set()
            time.sleep(0.05)
            return proc

        fakebin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakebin")
        env = {"AMEESH_BIN_DIR": fakebin, "AMEESH_TEST_SLEEP": "5",
               "AMEESH_TEST_LOG": self.turns_log}
        debut = time.monotonic()
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("ameesh.runner.subprocess.Popen", side_effect=popen_retarde):
                ok = worker.run_turn({"kind": "prompt", "prompt": "tour", "ids": []})
            ecoule = time.monotonic() - debut
            self.assertFalse(ok)
            self.assertLess(ecoule, 2.0, "le harnais publié doit être tué, pas attendu")
            self.assertTrue(worker.lease_lost.is_set())
        finally:
            if worker.proc is not None and worker.proc.poll() is None:
                worker.proc.kill()
                worker.proc.wait(timeout=5)
            worker.watchdog_stop.set()


if __name__ == "__main__":
    unittest.main()

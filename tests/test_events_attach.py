# SPDX-License-Identifier: AGPL-3.0-only
"""L8 — événements `kind='event'` (regroupement, urgent) et `ameesh attach` (C9)."""
from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from ameesh import adapters, db as db_mod, mail, registry
from ameesh.runner import AgentWorker, Runner, attach_owner, run_attach

from .support import FAKEBIN, PgTestCase


class EventsTest(PgTestCase):
    def _worker(self, name: str = "evt", *, senders=()):
        cfg = dataclasses.replace(self.cfg, interrupt_senders=tuple(senders))
        runner = Runner(cfg, self.db, once=True)
        runner.event_coalesce = 120.0
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        return runner, worker

    def test_kind_event_et_urgent(self):
        mail.send(self.db, "src", "evt", "déploiement terminé", kind="event")
        row = mail.unread(self.db, "evt")[0]
        self.assertTrue(mail.is_event(row))
        self.assertFalse(mail.is_urgent(row))
        mail.send(self.db, "src", "evt", "incident", kind="event",
                  payload={"urgent": True})
        rows = mail.unread(self.db, "evt")
        self.assertTrue(mail.is_urgent(rows[-1]))

    def test_regroupement_des_evenements(self):
        """Un lot d'événements ne réveille qu'une fois par fenêtre, sauf urgent."""
        runner, worker = self._worker()
        mail.send(self.db, "src", "evt", "événement 1", kind="event")
        spec = worker.pick()
        self.assertEqual(spec["kind"], "event")
        self.assertIn("événement 1", spec["prompt"])
        self.assertIn(adapters.EVENT_HEADER % 1, spec["prompt"])
        self.assertGreater(registry.get(self.db, "evt")["last_event_ts"] or 0, 0)

        # deuxième événement non urgent : la fenêtre est encore ouverte
        mail.send(self.db, "src", "evt", "événement 2", kind="event")
        self.assertIsNone(worker.pick())

        # fenêtre nulle : réveil immédiat
        runner.event_coalesce = 0.0
        self.assertEqual(worker.pick()["kind"], "event")

    def test_urgent_habilite_passe_en_prioritaire(self):
        # un urgent d'un expéditeur habilité n'est jamais regroupé : `pick()` le
        # sert en tête comme tour prioritaire (0018)
        runner, worker = self._worker(senders=("src",))
        registry.mark_event_wake(self.db, "evt")  # fenêtre fraîchement ouverte
        mail.send(self.db, "src", "evt", "incident", kind="event",
                  payload={"urgent": True})
        spec = worker.pick()
        self.assertEqual(spec["kind"], "urgent")
        self.assertIn("incident", spec["prompt"])

    def test_urgent_non_habilite_ne_perce_pas_la_fenetre(self):
        runner, worker = self._worker()
        registry.mark_event_wake(self.db, "evt")
        mail.send(self.db, "inconnu", "evt", "faux urgent", kind="event",
                  payload={"urgent": True})
        self.assertIsNone(worker.pick())  # remis comme un événement normal

    def test_courrier_ordinaire_non_regroupe(self):
        runner, worker = self._worker()
        registry.mark_event_wake(self.db, "evt")
        mail.send(self.db, "src", "evt", "message normal")
        spec = worker.pick()
        self.assertEqual(spec["kind"], "mail")

    def test_tour_evenement_remis_par_le_runner(self):
        cwd = os.path.join(self.tmp, "work", "evt-run")
        os.makedirs(cwd, exist_ok=True)
        self.register("evt-run", "claude", cwd=cwd)
        mail.send(self.db, "src", "evt-run", "événement de bout en bout", kind="event")
        proc = self.runner("--once", "--agents", "evt-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(mail.unread(self.db, "evt-run"), [])

    def test_cli_send_evenement(self):
        registry.upsert(self.db, "evt", harness="claude", host=self.cfg.host)
        env = self.env(AGENT_MAIL_NAME="src")
        proc = self.cli("send", "evt", "alerte", "--kind", "event", "--urgent", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = mail.unread(self.db, "evt")[0]
        self.assertTrue(mail.is_event(row))
        self.assertTrue(mail.is_urgent(row))
        # L125 : --urgent vaut aussi pour un message ordinaire (réveil sans
        # délai de regroupement)
        proc = self.cli("send", "evt", "x", "--urgent", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = mail.unread(self.db, "evt")[-1]
        self.assertFalse(mail.is_event(row))
        self.assertTrue(mail.is_urgent(row))
        proc = self.cli("send", "evt", "x", "--kind", "inconnu", env=env)
        self.assertEqual(proc.returncode, 2)


class AttachTest(PgTestCase):
    def _agent(self, name: str = "att") -> dict:
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        return registry.get(self.db, name)

    def _env(self) -> dict:
        return {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}

    def test_attach_prend_le_bail_idle_puis_le_rend(self):
        """Un bail vivant sans tour en cours est repris : la réclamation
        automatique est suspendue, l'ancien exécuteur est fencé par l'epoch."""
        self._agent("att")
        ancien = registry.claim(self.db, "att", "runner-avant", 3600)
        self.assertIsNotNone(ancien)
        resultat: list[int] = []

        def pris():
            row = registry.get(self.db, "att")
            return row if (row.get("lease_owner") or "").startswith("attach:") else None

        env = dict(self._env(), AMEESH_TEST_SLEEP="1.5")
        with mock.patch.dict(os.environ, env, clear=False):
            thread = threading.Thread(
                target=lambda: resultat.append(run_attach(self.cfg, self.db, "att", ttl=30)),
                daemon=True)
            thread.start()
            row = self.wait_for(pris, timeout=10, interval=0.05)
            self.assertEqual(int(row["lease_epoch"]), int(ancien["lease_epoch"]) + 1)
            # l'ancien exécuteur ne peut plus renouveler (fencing par epoch)
            self.assertIsNone(registry.renew(
                self.db, "att", "runner-avant", int(ancien["lease_epoch"]), 30))
            thread.join(timeout=15)
        self.assertEqual(resultat, [0])
        self.assertIsNone(registry.get(self.db, "att")["lease_owner"])
        turn = self.turns()[0]
        self.assertTrue(turn["env"]["AMEESH_RUNNER_ID"].startswith("attach:"))

    def test_attach_refuse_pendant_un_tour(self):
        self._agent("att")
        lease = registry.claim(self.db, "att", "runner-actif", 3600)
        # le marqueur d'un tour vivant est `status='running'` + bail vivant
        # (un tour de courrier ne remplit pas current_prompt)
        self.db.execute(
            "UPDATE agent_registry SET status = 'running', "
            "status_text = 'tour en cours' WHERE name = 'att'")
        with mock.patch.dict(os.environ, self._env(), clear=False):
            code = run_attach(self.cfg, self.db, "att", ttl=30)
        self.assertEqual(code, 3)
        row = registry.get(self.db, "att")
        self.assertEqual(row["lease_owner"], "runner-actif")
        self.assertEqual(int(row["lease_epoch"]), int(lease["lease_epoch"]))

    def test_attach_refuse_pendant_un_tour_de_courrier(self):
        """Verdict L8 B1 : un vrai tour de courrier (current_prompt NULL) bloque
        l'attach : le marqueur est `status='running'`, pas `current_prompt`."""
        self._agent("att")
        lease = registry.claim(self.db, "att", "runner-actif", 3600)
        self.db.execute(
            "UPDATE agent_registry SET status = 'running', current_prompt = NULL "
            "WHERE name = 'att'")
        self.assertTrue(registry.turn_in_progress(self.db, "att"))
        self.assertIsNone(registry.attach_claim(self.db, "att", "attach:test", 30))
        row = registry.get(self.db, "att")
        self.assertEqual(row["lease_owner"], "runner-actif")
        self.assertEqual(int(row["lease_epoch"]), int(lease["lease_epoch"]))

    def test_attach_wait_attend_la_fin_du_tour(self):
        self._agent("att")
        registry.claim(self.db, "att", "runner-actif", 3600)
        self.db.execute(
            "UPDATE agent_registry SET status = 'running', current_prompt = 'tour en cours' "
            "WHERE name = 'att'")

        def finir() -> None:
            time.sleep(0.5)
            self.db.execute(
                "UPDATE agent_registry SET status = 'idle', current_prompt = NULL "
                "WHERE name = 'att'")

        threading.Thread(target=finir, daemon=True).start()
        with mock.patch.dict(os.environ, self._env(), clear=False):
            code = run_attach(self.cfg, self.db, "att", wait=True, ttl=30)
        self.assertEqual(code, 0)
        self.assertIsNone(registry.get(self.db, "att")["lease_owner"])

    def test_attach_cli_route(self):
        self._agent("att")
        env = self.env(AMEESH_DRIVER="psql")
        proc = subprocess.run(
            [sys.executable, "-m", "ameesh", "attach", "att", "--ttl", "30"],
            capture_output=True, text=True, env=env,
            cwd=self.tmp, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNone(registry.get(self.db, "att")["lease_owner"])

    def test_attach_meurt_a_l_echeance_meme_si_renew_echoue(self):
        """Verdict L8 B2 : une vraie erreur SQL de renouvellement ne doit pas
        laisser la session interactive vivante après l'expiration du bail
        (un remplaçant peut réclamer)."""
        self._agent("att")
        env = dict(self._env(), AMEESH_TEST_SLEEP="8")
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(registry, "renew",
                                  side_effect=db_mod.DbError("panne SQL")):
            debut = time.monotonic()
            code = run_attach(self.cfg, self.db, "att", ttl=5)
            ecoule = time.monotonic() - debut
        self.assertLess(ecoule, 7.5, "la session interactive a survécu à l'échéance")
        self.assertLess(code, 0, "le harnais devait être tué à l'échéance")

    def test_attach_coupe_sans_journal_sur_le_chemin_d_arret(self):
        """Sonde codex3 du 04:42 : un journal synchrone bloqué (tube plein) ne
        doit pas retarder l'arrêt du harnais — le veilleur pose `perdu` avant de
        journaliser, et le fil principal coupe avant de journaliser."""
        self._agent("att")
        env = dict(self._env(), AMEESH_TEST_SLEEP="8")

        def faux_log(message, *args, **kwargs):
            if "échéance" in str(message) or "bail perdu" in str(message):
                time.sleep(30)  # simule un tube saturé

        resultat: list = []

        def lance() -> None:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(registry, "renew",
                                      side_effect=db_mod.DbError("panne SQL")), \
                    mock.patch("ameesh.runner.log", side_effect=faux_log):
                resultat.append(run_attach(self.cfg, self.db, "att", ttl=5))

        debut = time.monotonic()
        thread = threading.Thread(target=lance, daemon=True)
        thread.start()
        thread.join(timeout=12)
        ecoule = time.monotonic() - debut
        self.assertFalse(thread.is_alive(),
                         "le chemin d'arrêt a été bloqué par un journal synchrone")
        self.assertLess(ecoule, 8.0)
        self.assertTrue(resultat and resultat[0] < 0)

    def test_attach_owner_est_attache_a_l_hote(self):
        self.assertTrue(attach_owner("laptop").startswith("attach:"))
        self.assertTrue(attach_owner("laptop").endswith("@laptop"))

    def test_attache_respecte_les_regles_du_canon(self):
        """L2 est fusionné : attach et claim refusent un éphémère échu ou un
        responsable non résolu (mêmes règles que `claimable`)."""
        self._agent("att")
        # éphémère issu d'un agent manuel : non gouverné par le canon, donc
        # seule la règle de l'éphémère échu s'applique ici (fail closed L2).
        self.register("parent", "claude", cwd=self.tmp)
        self.db.execute(
            "UPDATE agent_registry SET created_by = 'parent', ephemeral = true, "
            "ephemeral_expires_at = now() - interval '1 second' WHERE name = 'att'")
        self.assertIsNone(registry.attach_claim(self.db, "att", "attach:test", 30))
        self.assertIsNone(registry.claim(self.db, "att", "runner-x", 30))
        self.db.execute(
            "UPDATE agent_registry SET ephemeral_expires_at = now() + interval '1 hour' "
            "WHERE name = 'att'")
        self.assertIsNotNone(registry.claim(self.db, "att", "runner-x", 30))
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = NULL, lease_expires_at = NULL, "
            "status = 'idle' WHERE name = 'att'")
        self.db.execute("UPDATE agent_registry SET responsible = NULL WHERE name = 'att'")
        with mock.patch.object(type(self.cfg), "responsible_required", True):
            self.assertIsNone(registry.attach_claim(self.db, "att", "attach:test", 30))
            self.assertIsNone(registry.claim(self.db, "att", "runner-y", 30))


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""#2810 : le NOTIFY d'un message urgent déjà servi n'arrête pas son tour.

N de l'auditeur sur le gel #14001 de #2729 (e7867684) : `request_preempt`
part sur tout urgent autorisé, que le tour soit en cours ou non. Si `_pick` a
déjà pris le message urgent avant le traitement de son NOTIFY, le tour urgent
est arrêté une fois par son propre message, puis le message est re-livré et
servi au tour suivant.

Règles vérifiées ici :

- un NOTIFY traité entre `_pick` et le lancement du tour ne préempte pas le
  tour qui sert déjà ce message (il n'ajoute aucune génération de demande) ;
- un NOTIFY traité pendant le tour du message ne le coupe pas non plus ;
- un urgent DIFFÉRENT coupe toujours le tour en cours (la correction ne
  supprime pas l'interruption prioritaire) ;
- un message déjà remis (hook mi-tour, ou tour précédent) ne préempte rien.
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
import unittest
from unittest import mock

from ameesh import mail, registry
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


class PropreMessageTest(PgTestCase):
    def _worker(self, name: str = "pre", *, senders=("orch",)):
        cfg = dataclasses.replace(self.cfg, interrupt_senders=tuple(senders))
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        runner.workers[name] = worker
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker

    def _urgent(self, to: str, body: str = "incident prioritaire") -> dict:
        mail.send(self.db, "orch", to, body, kind="event",
                  payload={"urgent": True})
        return [m for m in mail.unread(self.db, to) if m["body"] == body][-1]

    def _dispatch(self, runner: Runner, name: str, message_id: int) -> None:
        runner.dispatch({"channel": "agent_mail",
                         "payload": json.dumps({"to": name,
                                                "id": int(message_id)})})

    @staticmethod
    def _attendre(condition, timeout: float = 5.0) -> bool:
        fin = time.monotonic() + timeout
        while time.monotonic() < fin:
            if condition():
                return True
            time.sleep(0.02)
        return bool(condition())

    def test_notify_entre_pick_et_lancement_ne_tue_pas_le_tour(self):
        runner, worker = self._worker()
        row = self._urgent("pre")
        spec = worker.pick()
        self.assertEqual(spec["kind"], "urgent")
        self.assertTrue(worker.serves_mail(int(row["id"])))

        # Le NOTIFY de son propre message est traité APRÈS `_pick` : c'est le
        # scénario du N de l'auditeur (aucune génération de demande nouvelle).
        generation = worker.preempt_gen
        self._dispatch(runner, "pre", row["id"])
        self.assertFalse(worker.preempting.is_set())
        self.assertEqual(worker.preempt_gen, generation)

        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_SLEEP": "0"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(worker.run_turn(spec),
                            "le tour a été tué par le NOTIFY de son propre message")
        self.assertEqual(mail.unread(self.db, "pre"), [])

    def test_notify_pendant_le_tour_du_message_ne_le_coupe_pas(self):
        runner, worker = self._worker()
        row = self._urgent("pre")
        spec = worker.pick()
        verdict: list = []
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_SLEEP": "3"}
        with mock.patch.dict(os.environ, env, clear=False):
            fil = threading.Thread(
                target=lambda: verdict.append(worker.run_turn(spec)), daemon=True)
            fil.start()
            self.assertTrue(self._attendre(lambda: worker.proc is not None),
                            "le harnais du tour n'a pas été publié")
            self._dispatch(runner, "pre", row["id"])
            self.assertFalse(worker.preempting.is_set())
            fil.join(timeout=15)
        self.assertFalse(fil.is_alive(), "le tour n'a pas rendu la main")
        self.assertEqual(verdict, [True],
                         "le tour a été interrompu par le NOTIFY de son message")

    def test_un_autre_urgent_pendant_le_tour_coupe_toujours(self):
        runner, worker = self._worker()
        self._urgent("pre", "premier incident")
        spec = worker.pick()
        self.assertEqual(spec["kind"], "urgent")
        verdict: list = []
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_SLEEP": "5"}
        with mock.patch.dict(os.environ, env, clear=False):
            fil = threading.Thread(
                target=lambda: verdict.append(worker.run_turn(spec)), daemon=True)
            fil.start()
            self.assertTrue(self._attendre(lambda: worker.proc is not None))
            # Un autre urgent, lui, reste une demande : il coupe le tour.
            autre = self._urgent("pre", "second incident")
            self._dispatch(runner, "pre", autre["id"])
            self.assertTrue(self._attendre(lambda: worker.preempting.is_set()))
            fil.join(timeout=15)
        self.assertFalse(fil.is_alive())
        self.assertEqual(verdict, [False],
                         "un urgent différent n'a pas interrompu le tour")
        self.assertIn(int(autre["id"]),
                      [int(m["id"]) for m in mail.unread(self.db, "pre")],
                      "le second urgent doit rester à servir")

    def test_un_urgent_deja_remis_ne_preempte_plus(self):
        runner, worker = self._worker()
        row = self._urgent("pre")
        spec = worker.pick()
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_SLEEP": "0"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(worker.run_turn(spec))
        self.assertEqual(mail.unread(self.db, "pre"), [])
        # NOTIFY tardif du message déjà remis, pendant un tour ordinaire : il
        # n'y a plus de tour à interrompre pour lui.
        registry.set_pending_prompt(self.db, "pre", "tour long")
        suivant = worker.pick()
        self.assertEqual(suivant["kind"], "prompt")
        verdict: list = []
        env["AMEESH_TEST_SLEEP"] = "3"
        with mock.patch.dict(os.environ, env, clear=False):
            fil = threading.Thread(
                target=lambda: verdict.append(worker.run_turn(suivant)), daemon=True)
            fil.start()
            self.assertTrue(self._attendre(lambda: worker.proc is not None))
            self._dispatch(runner, "pre", row["id"])
            self.assertFalse(worker.preempting.is_set())
            fil.join(timeout=15)
        self.assertFalse(fil.is_alive())
        self.assertEqual(verdict, [True])

    def test_la_demande_du_message_servi_ne_cree_pas_de_generation(self):
        runner, worker = self._worker()
        row = self._urgent("pre")
        worker.pick()
        self.assertFalse(worker.request_preempt(int(row["id"])),
                         "la demande d'un message servi doit être ignorée")
        self.assertFalse(worker.preempting.is_set())
        self.assertEqual(worker.preempt_gen, 0)

        # Un message différent, ou un appel direct sans id, reste une demande.
        autre = self._urgent("pre", "second incident")
        self.assertTrue(worker.request_preempt(int(autre["id"])))
        self.assertTrue(worker.preempting.is_set())
        self.assertEqual(worker.preempt_gen, 1)


if __name__ == "__main__":
    unittest.main()

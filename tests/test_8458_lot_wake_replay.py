# SPDX-License-Identifier: AGPL-3.0-only
"""#8458 : la dette d'un réveil de lot survit au multi-lots et au redémarrage.

Paniers #8427 (relecture de #8423) : `_settle_lot_wake` effaçait le rejeu sans
comparer `spec.lot`, et le gardait en mémoire seulement ; les sorties de `pick`
après `note_wake` (place réservée, sonde de crédit, déplacement) le perdaient.
La dette vit désormais dans `agent_lot_sessions` (`wake_pending_at`), par
(agent, lot), et se rejoue sans être recomptée. Postgres réel ; faux harnais.
"""
from __future__ import annotations

import os
import time
from unittest import mock

from ameesh import registry, storage, work
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN
from .test_l165c_sessions_par_lot import _FORCE, _Base

PRESSION = {"blocked": True, "critical": False,
            "breaches": [{"metric": "load", "value": 40.0, "limit": 12.0}]}


class RejeuDetteTest(_Base):
    def _worker(self, name, *, repos=10_000):
        runner = Runner(self.cfg, self.db, once=True)
        self.register(name, "claude", cwd=self._cwd(name))
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        worker.lease = lease
        worker.last_activity = time.monotonic() - repos
        worker.nudged = True
        self.addCleanup(worker.watchdog_stop.set)
        self.addCleanup(worker._forget_wake_claim)
        return worker

    def _env(self, session):
        return mock.patch.dict(os.environ, {
            "AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
            "AMEESH_SESSION_STORE_CHECK": "0", "AMEESH_TEST_NEW_SESSION": session})

    def choix(self, worker):
        worker._lot_wake_checked = float("-inf")
        worker._scheduled_wake_checked = float("-inf")
        return worker._pick_for_turn()

    def dette(self, name):
        return {str(r["id"]): r for r in self.st.pending_lot_wakes(name)}

    def wakes(self, name, lot):
        for row in self.st.listing(name):
            if int(row["work_item_id"]) == int(lot):
                return int(row["wakes"] or 0)
        return 0

    def _lot(self, name, *, priority=2):
        return work.add(self.db, title="à faire", assignee=name, force=_FORCE,
                        priority=priority)["id"]

    def _refuse_par_pression(self, worker, spec):
        with self._env("sess-8458-refus"), \
                mock.patch.object(worker.runner, "host_pressure", return_value=PRESSION):
            self.assertFalse(worker.run_turn(spec))

    def test_rejeu_du_lot_a_survit_au_lancement_du_lot_b(self):
        worker = self._worker("t8458a")
        a = self._lot("t8458a")
        spec_a = self.choix(worker)
        self.assertEqual((spec_a["kind"], spec_a["lot"]), ("idle", str(a)))
        self._refuse_par_pression(worker, spec_a)
        self.assertIn(str(a), self.dette("t8458a"))

        # B, plus prioritaire, part avant le rejeu de A : son lancement ne
        # doit pas effacer la dette de A (#8427).
        b = self._lot("t8458a", priority=1)
        spec_b = self.choix(worker)
        self.assertEqual(spec_b["lot"], str(b), "le lot prioritaire part d'abord")
        with self._env("sess-8458-b"):
            self.assertTrue(worker.run_turn(spec_b))
        self.assertEqual(set(self.dette("t8458a")), {str(a)},
                         "le lancement du lot B n'efface pas le rejeu du lot A")
        self.assertEqual(self.wakes("t8458a", a), 1)

        again = self.choix(worker)
        self.assertIsNotNone(again, "le rejeu du lot A reste dû")
        self.assertEqual(again["lot"], str(a))
        self.assertTrue(again.get("lot_wake"))
        self.assertEqual(self.wakes("t8458a", a), 1, "le rejeu n'est pas recompté")
        with self._env("sess-8458-a2"):
            self.assertTrue(worker.run_turn(again))
        self.assertEqual(self.dette("t8458a"), {})
        self.assertEqual(self.wakes("t8458a", a), 1)

    def test_rejeu_apres_redemarrage_de_l_executeur(self):
        worker = self._worker("t8458b")
        lot = self._lot("t8458b")
        spec = self.choix(worker)
        self._refuse_par_pression(worker, spec)
        self.assertIn(str(lot), self.dette("t8458b"))

        # mémoire neuve, même bail : l'état ne doit venir que de la base.
        restarted = AgentWorker(worker.runner, registry.get(self.db, "t8458b"),
                                worker.lease)
        restarted.last_activity = time.monotonic()  # rien ne repose encore
        restarted.nudged = True
        self.addCleanup(restarted.watchdog_stop.set)
        self.addCleanup(restarted._forget_wake_claim)
        again = self.choix(restarted)
        self.assertIsNotNone(again, "la dette se relit au démarrage, sans le repos")
        self.assertEqual(again["lot"], str(lot))
        self.assertEqual(self.wakes("t8458b", lot), 1)
        with self._env("sess-8458-restart"):
            self.assertTrue(restarted.run_turn(again))
        self.assertEqual(self.dette("t8458b"), {})

    def test_sortie_de_pick_apres_note_wake_laisse_la_dette(self):
        worker = self._worker("t8458c")
        lot = self._lot("t8458c")
        with mock.patch.object(worker, "_credits_claim", return_value=False):
            self.assertIsNone(self.choix(worker), "la sonde de crédit refuse le tour")
        self.assertIn(str(lot), self.dette("t8458c"),
                      "une sortie de pick après note_wake garde le rejeu")
        self.assertEqual(self.wakes("t8458c", lot), 1)

        again = self.choix(worker)
        self.assertIsNotNone(again, "le rejeu reste dû après la sortie de pick")
        self.assertEqual(again["lot"], str(lot))
        self.assertEqual(self.wakes("t8458c", lot), 1, "pas recompté")
        with self._env("sess-8458-pick"):
            self.assertTrue(worker.run_turn(again))
        self.assertEqual(self.dette("t8458c"), {})

    def test_empreinte_du_repos_porte_la_dette(self):
        # La passe au repos voit la dette sans lecture de plus : le saut du
        # choix (lot_wake_due) ne peut pas l'ignorer.
        name = "t8458d"
        worker = self._worker(name)
        lot = self._lot(name)
        spec = self.choix(worker)
        self._refuse_par_pression(worker, spec)
        lu = storage.of(self.db).leases.idle_signatures([name])
        self.assertTrue(lu["agents"][name]["wake_pending"],
                        "la dette est dans l'empreinte du repos")
        with self._env("sess-8458-signature"):
            self.assertTrue(worker.run_turn(self.choix(worker)))
        lu = storage.of(self.db).leases.idle_signatures([name])
        self.assertFalse(lu["agents"][name]["wake_pending"],
                         "la dette effacée change aussi l'empreinte")

    def test_ligne_ecrite_par_l_executeur_n_1_n_est_pas_une_dette(self):
        # Migration 0614 additive : les colonnes neuves prennent leur défaut
        # sur une ligne écrite par le code d'avant (aucune dette, aucun rejeu).
        name = "t8458n1"
        self.register(name, "claude", cwd=self._cwd(name))
        lot = self._lot(name)
        self.previous_write(
            "INSERT INTO agent_lot_sessions"
            " (agent, work_item_id, wakes, last_wake_at, wake_activity_at)"
            " VALUES (%s, %s, 1, clock_timestamp(), NULL)",
            (name, lot),
            added=("wake_pending_at", "wake_retries", "wake_retry_reason"))
        self.assertEqual(self.st.pending_lot_wakes(name), [])
        row = next(r for r in self.st.actionable(name) if int(r["id"]) == lot)
        self.assertFalse(row["wake_pending"])
        self.assertEqual(int(row["wake_retries"] or 0), 0)

    #: L'écriture de `note_wake` du code d'avant 0614 (N-1), copiée de
    #: `git show 37ed22d3b:src/ameesh/storage/postgres/lot_sessions.py` : elle
    #: avance `last_wake_at` sans connaître les colonnes de la dette.
    NOTE_WAKE_N1 = (
        "INSERT INTO agent_lot_sessions AS l"
        "       (agent, work_item_id, wakes, last_wake_at, wake_activity_at)"
        " VALUES (%s, %s, 1, clock_timestamp(), to_timestamp(%s::float8))"
        " ON CONFLICT (agent, work_item_id) DO UPDATE"
        "    SET wakes = CASE WHEN l.wake_activity_at IS NULL"
        "                       OR excluded.wake_activity_at"
        "                          > l.wake_activity_at + interval '1 millisecond'"
        "                     THEN 1 ELSE l.wakes + 1 END,"
        "        last_wake_at = excluded.last_wake_at,"
        "        wake_activity_at = greatest(l.wake_activity_at,"
        "                                    excluded.wake_activity_at)")

    def test_note_wake_n_1_sur_une_ligne_en_dette_la_rend_caduque(self):
        # B mty-amh-impl-11 : pendant un déploiement par vagues, un exécuteur
        # N-1 qui note un réveil sur la ligne déjà en dette avance
        # `last_wake_at` sans les colonnes neuves. Ce tour-là a pris le réveil
        # en charge ; au retour du code N, la dette ne doit pas être rejouée
        # hors du délai doublé.
        name = "t8458n1c"
        self.register(name, "claude", cwd=self._cwd(name))
        lot = self._lot(name)
        self.st.note_wake(name, lot, None)
        self.assertIn(str(lot), self.dette(name))
        time.sleep(0.01)   # l'instant N-1 doit suivre celui de la dette
        self.previous_write(self.NOTE_WAKE_N1, (name, lot, None),
                            added=("wake_pending_at", "wake_retries",
                                   "wake_retry_reason"))
        self.assertEqual(self.st.pending_lot_wakes(name), [],
                         "la dette N-1 est caduque, pas rejouée")
        self.assertIsNone(self.st.bump_lot_wake_replay(name, lot))
        row = next(r for r in self.st.actionable(name) if int(r["id"]) == lot)
        self.assertFalse(row["wake_pending"])
        # un nouveau réveil du code N repose une dette due
        self.st.note_wake(name, lot, None)
        self.assertIn(str(lot), self.dette(name))

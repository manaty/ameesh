# SPDX-License-Identifier: AGPL-3.0-only
"""#3550 : la sortie de file du feu ne vaut que pour son entrée.

Le panier #3532 a rendu une sortie de file définitive pour le feu qui la
précède. Trois défauts non bloquants restaient : le reçu `removed` n'était pas
porté par sa cible et son gel (une sortie d'une autre cible, ou d'un ancien
gel, faisait rendre `taken-out` à tort) ; la relecture du reçu dans le chemin
`QueueWithdrawn` de `fire._queue` pouvait lever au lieu de rendre `not-ready` ;
et la course lecture de file → atterrissage n'avait pas de témoin.

Ces témoins rejouent les deux premiers sur la base ; le troisième est dans
`test_l160e2_land.QueueRemovalBeforeLandingTest`.
"""
from __future__ import annotations

import os
import types
import unittest
from unittest import mock

from ameesh import fire, merge_queue, packet_guard, radar, resolutions, storage, work

from .support import PgTestCase

SHA_A = "1aab3a4beda0d353ac07db16ef4e7f67dc52ddb8"
SHA_B = "2bbc4b5cebf1e464bd18ec27f5f8a78ed63eecc9"
PROJECT = types.SimpleNamespace(name="nexlink", repo="/nonexistent/nexlink", target="develop",
                                land="local")


class FireQueueTakeOutScopeTest(PgTestCase):

    def setUp(self):
        super().setUp()
        patches = [
            mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": "", "AMEESH_ROLE": ""}),
            mock.patch.object(merge_queue, "readiness", side_effect=self._ready),
            mock.patch.object(merge_queue, "source_of",
                              return_value="github.com/manaty/nexlink"),
            mock.patch.object(radar, "github_ci", return_value=None),
            mock.patch.object(radar, "clone_dir", return_value="/nonexistent/clone"),
            mock.patch.object(resolutions, "Replayer"),
            mock.patch.object(fire, "_passive"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.lot = work.add(self.db, title="paquet 173", branch="integration/paquet-173",
                            app="nexlink")["id"]
        self.freeze_a = work.milestone(self.db, self.lot, "frozen", sha=SHA_A, actor="impl-02")
        self.freeze_b = None
        self.ready_sha, self.ready_id = SHA_A, self.freeze_a["id"]
        packet_guard.declare(self.db, self.lot, "incident", "test: #3550", SHA_A,
                             actor="pilote")

    def _ready(self, db, lot, repo, **_kw):
        return {"target": "develop", "frozen_sha": self.ready_sha,
                "frozen_id": self.ready_id, "pr": None, "class": "light",
                "class_why": ""}

    def _fallback(self):
        """Un passage du feu sur le lot : son repli file, tel que `_act` l'appelle."""
        marks = work.milestones(self.db, self.lot)
        return fire._queue(self.db, work.get(self.db, self.lot), marks, fire.active(marks),
                           project=PROJECT, repo=PROJECT.repo, target="develop", cfg=None,
                           dry_run=False, out={"lot": self.lot})

    def _row(self):
        return next((r for r in storage.of(self.db).work.queue()
                     if int(r["work_item_id"]) == self.lot), None)

    def test_a_take_out_of_another_target_does_not_stop_the_fire_fallback(self):
        """Une sortie de la file de `main` ne vaut pas pour `develop`."""
        ops = storage.of(self.db).work
        ops.queue_add(self.lot, target="main", frozen_sha=SHA_A, note="queued",
                      actor="pilote", frozen_id=self.freeze_a["id"])
        fire.give(self.db, self.lot, sha=SHA_A, actor="pilote")
        self.assertTrue(merge_queue.leave(self.db, self.lot, actor="pilote"))

        out = self._fallback()

        self.assertEqual(out["result"], "queued", out)
        self.assertEqual(self._row()["target"], "develop")

    def test_a_take_out_of_an_old_freeze_does_not_stop_the_fire_fallback(self):
        """Une sortie du gel A ne retient pas un feu donné sur le gel B."""
        ops = storage.of(self.db).work
        ops.queue_add(self.lot, target="develop", frozen_sha=SHA_A, note="queued",
                      actor="pilote", frozen_id=self.freeze_a["id"])
        self.freeze_b = work.milestone(self.db, self.lot, "frozen", sha=SHA_B,
                                       actor="impl-02")
        self.ready_sha, self.ready_id = SHA_B, self.freeze_b["id"]
        packet_guard.declare(self.db, self.lot, "incident", "test: #3550 B", SHA_B,
                             actor="pilote")
        fire.give(self.db, self.lot, sha=SHA_B, actor="pilote")
        # la ligne encore en file est celle du gel A : sa sortie nomme A
        self.assertTrue(merge_queue.leave(self.db, self.lot, actor="pilote"))

        out = self._fallback()

        self.assertEqual(out["result"], "queued", out)
        row = self._row()
        self.assertEqual((row["frozen_id"], row["frozen_sha"]),
                         (self.freeze_b["id"], SHA_B))

    def test_a_legacy_receipt_without_target_still_binds(self):
        """Un reçu écrit avant la colonne `target` ne peut pas être distingué :
        il garde son effet (pas de ré-entrée silencieuse)."""
        ops = storage.of(self.db).work
        ops.queue_add(self.lot, target="develop", frozen_sha=SHA_A, note="queued",
                      actor="pilote", frozen_id=self.freeze_a["id"])
        fire.give(self.db, self.lot, sha=SHA_A, actor="pilote")
        self.assertTrue(merge_queue.leave(self.db, self.lot, actor="pilote"))
        self.db.execute("UPDATE integration_train_queue_receipts SET target = NULL"
                        " WHERE work_item_id = %s AND kind = 'removed'", (self.lot,))

        out = self._fallback()

        self.assertEqual(out["result"], "taken-out", out)
        self.assertIsNone(self._row())

    def test_a_failed_reread_is_not_ready_not_an_error(self):
        """#3550 (2) : la relecture du reçu dans le chemin QueueWithdrawn ne
        laisse pas remonter une lecture DB en échec."""
        ops = storage.of(self.db).work
        fire.give(self.db, self.lot, sha=SHA_A, actor="pilote")
        calls = []

        def read(*_a, **_kw):
            calls.append(1)
            if len(calls) == 1:
                return None
            raise RuntimeError("database read failed")

        with mock.patch.object(merge_queue, "enter",
                               side_effect=merge_queue.QueueWithdrawn("taken")), \
                mock.patch.object(type(ops), "queue_withdrawn_since", side_effect=read):
            out = self._fallback()

        self.assertEqual(out["result"], "not-ready", out)
        self.assertIn("cannot read the queue take-outs", out["detail"])
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

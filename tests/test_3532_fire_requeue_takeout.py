# SPDX-License-Identifier: AGPL-3.0-only
"""#3532 : le repli file du feu ne défait plus une sortie de file.

Nexlink, 25/10 : le feu de #3515 (09:43) est actif ; le lot sort de la file
(10:17, 10:23), le repli le ré-inscrit au passage suivant (gén. 107 à 10:19,
108 à 10:26), et la seconde ré-entrée atterrit dès que le train libère la
cible. Ces témoins rejouent la séquence sur la base : la réservation du train
refuse l'atterrissage, la sortie est définitive pour ce feu (y compris si elle
tombe entre la lecture du repli et l'insertion), et seul un nouveau feu ou un
`queue add` explicite remet le lot en file.
"""
from __future__ import annotations

import io
import os
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock

from ameesh import fire, land, merge_queue, packet_guard, radar, resolutions, storage, work

from .support import PgTestCase

SHA = "1aab3a4beda0d353ac07db16ef4e7f67dc52ddb8"
PROJECT = types.SimpleNamespace(name="nexlink", repo="/nonexistent/nexlink", target="develop",
                                land="local")
KEY = land.lock_key(PROJECT, "develop")
TRAIN = "train:runner-mty-nxl-site-01@ameesh-agents-2"


class FireRequeueAfterTakeOutTest(PgTestCase):

    def setUp(self):
        super().setUp()
        patches = [
            mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": "", "AMEESH_ROLE": ""}),
            mock.patch.object(merge_queue, "readiness", side_effect=self._ready),
            mock.patch.object(merge_queue, "source_of", return_value="github.com/manaty/nexlink"),
            mock.patch.object(radar, "github_ci", return_value=None),
            mock.patch.object(radar, "clone_dir", return_value="/nonexistent/clone"),
            mock.patch.object(resolutions, "Replayer"),
            mock.patch.object(fire, "_passive"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.during_readiness = None
        self.lot = work.add(self.db, title="paquet 173", branch="integration/paquet-173",
                            app="nexlink")["id"]
        self.frozen = work.milestone(self.db, self.lot, "frozen", sha=SHA, actor="impl-02")
        packet_guard.declare(self.db, self.lot, "incident", "test: fire fixture", SHA,
                             actor="pilote")
        fire.give(self.db, self.lot, sha=SHA, actor="orchestrateur")

    def tearDown(self):
        ops = storage.of(self.db).work
        ops.land_unlock(KEY, TRAIN, ops.land_lock(KEY, TRAIN, 60) or 0)
        super().tearDown()

    def _ready(self, db, lot, repo, **_kw):
        if self.during_readiness is not None:
            self.during_readiness()
        return {"target": "develop", "frozen_sha": SHA, "frozen_id": self.frozen["id"],
                "pr": None, "class": "light", "class_why": ""}

    def _fallback(self):
        """Un passage du feu sur le lot : son repli file, tel que `_act` l'appelle."""
        marks = work.milestones(self.db, self.lot)
        return fire._queue(self.db, work.get(self.db, self.lot), marks, fire.active(marks),
                           project=PROJECT, repo=PROJECT.repo, target="develop", cfg=None,
                           dry_run=False, out={"lot": self.lot})

    def _row(self):
        return next((r for r in storage.of(self.db).work.queue()
                     if int(r["work_item_id"]) == self.lot), None)

    def _land(self):
        with mock.patch.object(radar, "settings_of",
                               return_value=types.SimpleNamespace(projects={})):
            return land.land_head(None, self.db, PROJECT, self._row(), owner="runner-radar")

    def test_the_take_out_is_final_for_the_fire_that_predates_it(self):
        ops = storage.of(self.db).work
        epoch = ops.land_lock(KEY, TRAIN, 1800)   # le train 20261025-1000 en CI
        self.assertEqual(self._fallback()["result"], "queued")          # gén. 107, 10:19
        self.assertEqual(self._land()["result"], "busy")                # la réservation tient
        self.assertTrue(merge_queue.leave(self.db, self.lot, actor="mty-nxl-impl-02"))
        out = self._fallback()                                          # 10:26 : plus de gén. 108
        self.assertEqual(out["result"], "taken-out", out)
        self.assertIn("fire again", out["detail"])
        self.assertIsNone(self._row())
        self.assertTrue(ops.land_unlock(KEY, TRAIN, epoch))             # le train se termine
        self.assertEqual(self._fallback()["result"], "taken-out")
        report = {"queue": {"targets": {"develop": [{"lot": self.lot}]}}}
        self.assertEqual(land.run_pass(None, self.db, PROJECT, report, owner="runner-radar"), [])
        self.assertEqual(self._row(), None, "aucune fusion : la ligne n'existe plus")

    def test_a_take_out_between_the_check_and_the_insertion_still_refuses(self):
        ops = storage.of(self.db).work

        def concurrent():
            # un autre geste met le lot en file puis l'en sort, pendant que le
            # repli prépare son entrée : après sa lecture, avant son insertion
            ops.queue_add(self.lot, target="develop", frozen_sha=SHA, note="queued",
                          actor="pilote", frozen_id=self.frozen["id"])
            merge_queue.leave(self.db, self.lot, actor="mty-nxl-impl-02")

        self.during_readiness = concurrent
        out = self._fallback()
        self.assertEqual(out["result"], "taken-out", out)
        self.assertIsNone(self._row())
        self.assertIsNotNone(ops.queue_withdrawn_since(self.lot, 0))

    def test_a_new_fire_or_an_explicit_queue_add_puts_it_back(self):
        self.assertEqual(self._fallback()["result"], "queued")
        merge_queue.leave(self.db, self.lot, actor="mty-nxl-impl-02")
        self.assertEqual(self._fallback()["result"], "taken-out")
        # `ameesh work queue add` : le geste explicite n'est pas retenu par la sortie
        merge_queue.enter(self.db, self.lot, repo=PROJECT.repo, project=PROJECT, actor="pilote")
        self.assertIsNotNone(self._row())
        merge_queue.leave(self.db, self.lot, actor="mty-nxl-impl-02")
        fire.withdraw(self.db, self.lot, actor="orchestrateur")
        fire.give(self.db, self.lot, sha=SHA, actor="orchestrateur")
        self.assertEqual(self._fallback()["result"], "queued")

    def test_queue_remove_warns_when_it_condemns_a_live_train(self):
        self.assertEqual(self._fallback()["result"], "queued")
        ops = storage.of(self.db).work
        epoch = ops.land_lock(KEY, TRAIN, 1800)
        state = {"phase": "ci", "stamp": "20261025-1000",
                 "plan": {"members": [{"work_item_id": self.lot}]}}
        self.assertIsNotNone(ops.train_save(KEY, state, revision=0, owner=TRAIN, epoch=epoch))
        args = types.SimpleNamespace(queue_command="remove", id=self.lot, actor="orchestrateur",
                                     message="", json=False)
        err = io.StringIO()
        with mock.patch.object(merge_queue, "declared_projects",
                               return_value={"nexlink": PROJECT}), redirect_stderr(err):
            self.assertEqual(merge_queue.run(None, self.db, args), 0)
        self.assertIn("live train 20261025-1000", err.getvalue())
        self.assertIsNone(self._row(), "l'avertissement ne retient pas la sortie")


if __name__ == "__main__":
    unittest.main()

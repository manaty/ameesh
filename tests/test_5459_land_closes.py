# SPDX-License-Identifier: AGPL-3.0-only
"""#5459 : la file de fusion ferme elle-même le lot qu'elle atterrit.

Un véhicule de paquet resté sans assigné n'était jamais vu par le balayage
des branches, et l'hôte du balayage peut ne pas avoir le clone du dépôt : le
lot restait en qa après « landed by ameesh », avec un faux SLO « non
assigné ». La file connaît le SHA de la fusion qu'elle vient de faire : elle
livre elle-même le lot, par le même connecteur que le feu et les relevés.
"""
from __future__ import annotations

from unittest import mock

from ameesh import land, radar, storage, work

from .support import PgTestCase
from .test_l160b_radar import _git, make_repo
from .test_l160e2_land import _Land


class LandingClosesLotTest(_Land):
    """Sans base : la file appelle `work.deliver` sur le SHA qu'elle a fusionné."""

    def test_the_landing_delivers_the_known_sha(self):
        self.queued(4)
        seen = []

        def deliver(db, lot, *, connector, evidence, actor="", **options):
            seen.append((lot, connector, dict(evidence), dict(options)))
            return {"id": lot, "result": "merged", "detail": "closed",
                    "review_lot": {}}

        with mock.patch.object(work, "deliver", side_effect=deliver):
            out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        (lot, connector, evidence, options), = seen
        self.assertEqual((lot, connector), (4, "git-merge"))
        self.assertEqual(evidence["sha"], _git(self.origin, "rev-parse", "main"))
        self.assertEqual(evidence["seen"], "the merge queue of main")
        self.assertEqual(evidence["pr"], None)
        # clôture indépendante du balayeur : le dépôt passé est le clone radar
        self.assertEqual(options["repo"], radar.clone_dir(self.cfg, "p"))
        self.assertEqual(out["sha"], _git(self.origin, "rev-parse", "main"))
        self.assertEqual(out["closure"]["result"], "merged")

    def test_a_closure_failure_keeps_the_landing_and_says_so(self):
        """La fusion est un fait : une clôture en erreur ne la défait pas et
        reste lisible sur le résultat de la passe ; le SHA de la file est
        gardé sur la ligne `landed`."""
        self.queued(4)
        with mock.patch.object(work, "deliver", side_effect=RuntimeError("db down")):
            out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertIn("lot not closed", out["error"])
        self.assertIn("db down", out["error"])
        row = self.fake.queue_rows[4]
        self.assertEqual(row["state"], "landed")
        self.assertEqual(row["landing_sha"], out["sha"])

    def test_a_withheld_or_refrozen_closure_is_not_an_error(self):
        """Porte de production ou nouveau gel : le lot reste ouvert à dessein,
        ce n'est pas un échec de la passe."""
        for result in ("withheld", "refrozen", "refused", "already"):
            with self.subTest(result=result):
                self.fake.queue_rows.clear()
                self.queued(4)
                with mock.patch.object(work, "deliver", return_value={
                        "id": 4, "result": result, "detail": result}):
                    out = self.land(4)
                self.assertEqual(out["result"], "landed", out)
                self.assertNotIn("error", out)
                self.assertEqual(out["closure"]["result"], result)


class LandingClosesDbTest(PgTestCase):
    """Sur la base : le lot ouvert passe livré, sans dépôt qui porte le SHA."""

    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp)
        self.project = radar.Project(name="p", repo=self.repo, land="local")
        self.lot = work.add(self.db, title="file closes the lot", app="ameesh")["id"]
        self.ops = storage.of(self.db).work

    def _landing(self, row):
        key = "p:main"
        epoch = self.ops.land_lock(key, "test-runner", 60)
        self.addCleanup(self.ops.land_unlock, key, "test-runner", epoch)
        return land._Landing(self.db, self.project, row, key, "test-runner", epoch,
                             {}, cfg=self.cfg)

    def test_the_queue_landing_closes_the_open_lot(self):
        # Le SHA n'est dans aucun clone de ce banc : la clôture ne lit que la
        # file (c'est le défaut corrigé — dépendre du clone du balayeur).
        sha = "b" * 40
        frozen = self.ops.add_milestone(self.lot, "frozen", sha=sha, actor="a1",
                                        verdict=None, note="frozen")
        row = self.ops.queue_add(self.lot, target="main", frozen_sha=sha,
                                 frozen_id=frozen["id"], note="queued", actor="t")
        self.assertIsNotNone(row)
        # `land_head` passe la ligne en `landing` avant de pousser la fusion.
        self.assertIsNotNone(self.ops.queue_update(self.lot, state="landing"))
        row = dict(row, work_item_id=self.lot, title="file closes the lot",
                   item_state="qa", assignee="", state="landing")
        out = self._landing(row).landed(
            {"sha": sha, "state": "local", "files": []}, "main")
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(out["closure"]["result"], "merged", out)
        item = work.get(self.db, self.lot)
        self.assertIn(item["state"], work.MERGED_STATES)
        merged = [m for m in work.milestones(self.db, self.lot)
                  if m["kind"] == "merged" and not m.get("annulled")]
        self.assertEqual([m["sha"] for m in merged], [sha])
        queue = next(r for r in self.ops.queue() if int(r["work_item_id"]) == self.lot)
        self.assertEqual((queue["state"], queue["landing_sha"]), ("landed", sha))
        notes = [e["note"] for e in work.events(self.db, self.lot)]
        self.assertTrue(any("merge seen by the merge queue of main" in n
                            for n in notes), notes)

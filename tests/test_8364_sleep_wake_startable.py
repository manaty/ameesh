# SPDX-License-Identifier: AGPL-3.0-only
"""#8364 — un lot démarrable lève le sommeil d'un projet tout endormi.

Constat (auditeur, 27/11) : un paquet fusionné libère dix lots ameesh
recouverts par empreinte ; tous les exécutants dormaient sur `SLEEP_REASON`,
le projet n'avait donc pas de mesure (`measure` écarte les pauses) et ses lots
démarrables restaient à zéro. Rien n'était pris avant une note manuelle
`roadmap: rouverte`. Témoin : le lot libéré par l'empreinte lève les dormeurs
à la passe suivante, sans note de réouverture.
"""
from __future__ import annotations

import time
from unittest import mock

from ameesh import file_decoupage as fd
from ameesh import registry, storage, work

from .support import PgTestCase
from .test_4623_sleep_roadmap_reopen import SETTINGS, agent, team


class SleepWakeStartableTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("pilote", "dev0", "dev1", "devb0", "devb1", "devb2"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice'")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=["pilote"])
        pilots.start()
        self.addCleanup(pilots.stop)
        self.state: dict = {}
        self.t0 = time.time()

    def run_pass(self, offset, listing=None):
        return fd.run(None, self.db, now=self.t0 + offset, state=self.state,
                      listing=listing if listing is not None else team(2, 3),
                      canons=[], settings=SETTINGS)

    def asleep(self, *names):
        return [agent(name, operator_paused=True,
                      operator_pause_reason=fd.SLEEP_REASON % "ameesh",
                      operator_pause_by=fd.SLEEP_ACTOR,
                      operator_pause_for="human:alice",
                      operator_pause_ts=float(self.t0 + 1900))
                for name in names]

    def footprint(self, lot):
        storage.of(self.db).work_footprints.replace(
            int(lot["id"]), "manual", ["src/ameesh/x.py"], actor="test")

    def test_a_footprint_release_lifts_an_all_asleep_project(self):
        busy = work.add(self.db, title="occupe", app="ameesh", actor="test")
        self.db.execute("UPDATE work_items SET state = 'build', assignee = 'devb0'"
                        " WHERE id = %s", (int(busy["id"]),))
        self.footprint(busy)
        covered = work.add(self.db, title="recouvert", app="ameesh", actor="test")
        self.footprint(covered)

        self.assertEqual(self.run_pass(0), [])
        self.assertEqual([g["kind"] for g in self.run_pass(1800)], ["split"])
        split = int(self.db.query(
            "SELECT id FROM work_items WHERE source = %s",
            ("file-decoupage:ameesh",))[0]["id"])
        work.note(self.db, split, "roadmap: épuisée ameesh", actor="pilote")
        # Le lot recouvert n'est pas démarrable : le projet s'endort.
        self.assertEqual([g["kind"] for g in self.run_pass(1900)], ["sleep"])

        listing = self.asleep("dev0", "dev1")
        # Toujours recouvert : les dormeurs restent endormis.
        self.assertEqual(self.run_pass(1950, listing), [])
        for name in ("dev0", "dev1"):
            self.assertTrue(registry.get(self.db, name)["operator_paused"])

        # Le lot occupant se ferme : l'empreinte est libérée.
        work.close(self.db, int(busy["id"]), abandoned=True, actor="test")
        out = self.run_pass(2000, listing)
        self.assertEqual([g["kind"] for g in out], ["wake"])
        self.assertEqual(sorted(out[0]["agents"]), ["dev0", "dev1"])
        for name in ("dev0", "dev1"):
            self.assertFalse(registry.get(self.db, name)["operator_paused"])

    def test_a_sleeping_project_is_measured_without_wakeable_agents(self):
        item = work.add(self.db, title="libre", app="ameesh", actor="test")
        lots = storage.of(self.db).operations.open_lots_activity(None)
        out = fd.measure(self.asleep("dev0"), lots, {}, self.t0, [],
                         configured=False, projects={"ameesh"})
        self.assertEqual(out["ameesh"].wakeable, [])
        self.assertEqual(out["ameesh"].startable, [int(item["id"])])
        # Sans agent réveillable, la condition de découpage ne le retient pas.
        self.assertEqual(fd.held(out, {}, self.t0, 0.2, 0.0), ([], {}))

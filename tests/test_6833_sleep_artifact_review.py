# SPDX-License-Identifier: AGPL-3.0-only
"""#6833 — une relecture d'artefact hors git retient le sommeil jusqu'au verdict.

Constat (mesh-design, 11/11) : impl-12 relisait par courrier le dossier v3 de
#6002 (exécutant externe, sans gel, empreinte SHA256SUMS) ; le sommeil
« roadmap épuisée » l'a endormi à 04:32Z. Sans gel, aucun siège de relecture
n'existait, et le courrier lu ne comptait plus.

Témoins du lot :

* le relecteur cité par une note `artifact-review-requested:` sur un lot
  ouvert n'est pas endormi ;
* la note `artifact-review-done:` de la même empreinte rend le sommeil
  possible ; un verdict d'une autre empreinte ne ferme rien ;
* une demande écrite par l'agent qu'elle cite, ou sur un lot clos, ne compte
  pas ;
* le dormeur à qui une relecture est demandée est levé.
"""
from __future__ import annotations

import time
from unittest import mock

from ameesh import file_decoupage as fd
from ameesh import mechanics, registry, work

from .support import PgTestCase
from .test_4610_sleep_work_mail import agent

SETTINGS = mechanics.QueueFill(repos_part=0.20, repos_s=1800, wake_mail_s=7200)
V3 = "3fd9924a38874044e4fbd1ae505bdcdc89dc6d5eaa10cf9f4cc4aed7ef8ba0ee"
V2 = "4bc5feb2" + "0" * 56


class SleepArtifactReviewTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("pilote", "dev0", "dev1"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice',"
                        " team = 'ameesh'")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=["pilote"])
        pilots.start()
        self.addCleanup(pilots.stop)
        self.state: dict = {}
        self.t0 = time.time()
        self._lot = None

    @property
    def lot(self):
        """Le lot à exécutant externe, en build, sans assigné ni gel ; créé
        après le lot de découpage, qui ne naît que sans lot démarrable."""
        if self._lot is None:
            self._lot = int(work.add(self.db, title="dossier externe",
                                     app="ameesh", actor="pilote")["id"])
            self.db.execute("UPDATE work_items SET state = 'build',"
                            " external_executor = 'bureau' WHERE id = %s",
                            (self._lot,))
        return self._lot

    def run_pass(self, offset, listing=None):
        listing = listing if listing is not None else [agent("dev0"), agent("dev1")]
        return fd.run(None, self.db, now=self.t0 + offset, state=self.state,
                      listing=listing, canons=[], settings=SETTINGS)

    def exhausted_split(self):
        self.run_pass(0)
        self.run_pass(1800)
        split = int(self.db.query("SELECT id FROM work_items WHERE source = %s",
                                  ("file-decoupage:ameesh",))[0]["id"])
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        return split

    def sleep_out(self, offset=1900):
        out = self.run_pass(offset)
        sleep = next(g for g in out if g["kind"] == "sleep")
        return {row["agent"]: row for row in sleep["agents"]}

    def ask(self, name, digest=V3, actor="pilote"):
        work.note(self.db, self.lot, "%s %s sha256:%s dossier v3"
                  % (fd.ARTIFACT_REVIEW_PREFIX, name, digest), actor=actor)

    def verdict(self, name, digest=V3, actor="pilote"):
        work.note(self.db, self.lot, "%s %s sha256:%s OK"
                  % (fd.ARTIFACT_DONE_PREFIX, name, digest), actor=actor)

    def open_lots(self):
        return [{"id": self.lot}]

    def owed(self):
        owed, error = fd.owed_work(self.db, ["dev0", "dev1"], self.open_lots(),
                                   now=self.t0)
        self.assertIsNone(error)
        return owed

    def test_an_open_request_is_owed_work_until_its_verdict(self):
        self.ask("dev1")
        self.assertEqual(self.owed(), {
            "dev1": "artifact review requested on #%d (sha256:3fd9924a…)" % self.lot})
        # un verdict sur une autre empreinte (v2) ne ferme pas la v3
        self.verdict("dev1", digest=V2)
        self.assertEqual(list(self.owed()), ["dev1"])
        # le verdict v3, abrégé, ferme la demande
        self.verdict("dev1", digest=V3[:8], actor="dev1")
        self.assertEqual(self.owed(), {})
        # une nouvelle demande après le verdict rouvre
        self.ask("dev1", digest=V2)
        self.assertEqual(list(self.owed()), ["dev1"])

    def test_a_self_request_or_a_closed_lot_does_not_count(self):
        self.ask("dev1", actor="dev1")
        self.assertEqual(self.owed(), {})
        self.ask("dev0")
        owed, error = fd.owed_work(self.db, ["dev0"], [], now=self.t0)
        self.assertIsNone(error)
        self.assertEqual(owed, {})

    def test_the_requested_reviewer_is_not_slept_then_may_sleep(self):
        split = self.exhausted_split()
        self.ask("dev1")
        by_agent = self.sleep_out()
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertTrue(by_agent["dev1"]["why"].startswith(
            "artifact review requested on #%d" % self.lot))
        self.assertTrue(by_agent["dev0"]["slept"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        notes = [r["note"] for r in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s", (split,))]
        self.assertTrue(any(n.startswith("sleep exempted: dev1: artifact review")
                            for n in notes))
        # verdict rendu : le sommeil redevient possible
        self.verdict("dev1")
        by_agent = self.sleep_out(offset=2000)
        self.assertTrue(by_agent["dev1"]["slept"])

    def test_a_sleeper_is_woken_by_a_request(self):
        self.exhausted_split()
        by_agent = self.sleep_out()
        self.assertTrue(by_agent["dev1"]["slept"])

        def listing():
            return [dict(agent(name), **{
                k: registry.get(self.db, name)[k]
                for k in ("operator_paused", "operator_pause_reason",
                          "operator_pause_by", "operator_pause_for")})
                for name in ("dev0", "dev1")]
        self.ask("dev1")
        out = self.run_pass(2000, listing=listing())
        woken = [g for g in out if g["kind"] == "wake"]
        self.assertEqual([g["agents"] for g in woken], [["dev1"]])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(registry.get(self.db, "dev0")["operator_paused"])
        event = self.db.query(
            "SELECT reason FROM operator_pause_events WHERE agent = 'dev1'"
            " AND paused = false ORDER BY id DESC LIMIT 1")[0]
        self.assertTrue(event["reason"].startswith(
            "%s artifact review requested on #%d" % (fd.SLEEP_LIFT_PREFIX, self.lot)))

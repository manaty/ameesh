# SPDX-License-Identifier: AGPL-3.0-only
"""#6828 — le sommeil épargne l'auteur d'un lot refusé au paquet de son fait.

Constat (mesh-design #211193, 11/11) : impl-03 a été endormi à 04:28Z
(« roadmap: épuisée ameesh ») alors que #6806, validé, était écarté du paquet
pour `freeze-not-head` (gel 943fd7a39f7c ≠ tête publiée 835661f9573d) :
`owed_work` ne lisait pas la cause côté auteur que lisent déjà l'escalade SLO
et le débit (#4572). Le regel dû n'avait plus de destinataire vivant.

Témoins du lot :

* un lot à valider de l'agent, refusé pour une cause côté auteur, est un
  travail engagé (`owed_work`), avec la lecture de `paquet-candidats` ;
* une lecture inconclusive, un refus qui n'est pas côté auteur ou un lot en
  cours ne retiennent personne ;
* l'agent n'est pas endormi ; un dormeur est levé, motif au journal des pauses.
"""
from __future__ import annotations

import time
from unittest import mock

from ameesh import file_decoupage as fd
from ameesh import mechanics, packet_candidates, registry, work

from .support import PgTestCase
from .test_4610_sleep_work_mail import agent

SETTINGS = mechanics.QueueFill(repos_part=0.20, repos_s=1800, wake_mail_s=7200)

NOT_HEAD = packet_candidates._author_refusal(
    "freeze-not-head",
    "freeze 943fd7a39f7c is not the published head 835661f9573d")


class SleepAuthorRefusalTest(PgTestCase):
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
        self.causes: dict = {}
        self.read: list = []

        def refusal(db, cfg, row, marks=None, **_):
            self.read.append(int(row["id"]))
            return self.causes.get(int(row["id"]), {"code": "", "unknown": True})
        patch = mock.patch("ameesh.packet_candidates.candidate_refusal",
                           side_effect=refusal)
        patch.start()
        self.addCleanup(patch.stop)

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

    def notes(self, lot):
        return [r["note"] for r in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(lot),))]

    def sleep_out(self, offset=1900):
        out = self.run_pass(offset)
        sleep = next(g for g in out if g["kind"] == "sleep")
        return {row["agent"]: row for row in sleep["agents"]}

    def lot(self, assignee, *, state="validated", branch="lot/6806-x", cause=None):
        lot = int(work.add(self.db, title="regel dû", app="ameesh",
                           actor="pilote")["id"])
        work.assign(self.db, lot, assignee, actor="pilote")
        self.db.execute("UPDATE work_items SET state = %s, branch = %s WHERE id = %s",
                        (state, branch, lot))
        if cause is not None:
            self.causes[lot] = cause
        return lot

    def open_lots(self):
        return self.db.query("SELECT id, assignee, state, branch, branch_target,"
                             " branch_head, app FROM work_items")

    # -- motif -----------------------------------------------------------
    def test_an_author_side_refusal_is_owed_work(self):
        lot = self.lot("dev1", cause=NOT_HEAD)
        owed, error = fd.owed_work(self.db, ["dev0", "dev1"], self.open_lots())
        self.assertIsNone(error)
        self.assertEqual(owed, {"dev1": "packet refusal on #%d: freeze-not-head"
                                        " (author)" % lot})

    def test_inconclusive_or_non_author_reads_hold_nobody(self):
        self.lot("dev0")  # lecture inconclusive (`unknown`)
        self.lot("dev1", cause={"code": "", "ready": True, "owner": ""})
        owed, error = fd.owed_work(self.db, ["dev0", "dev1"], self.open_lots())
        self.assertIsNone(error)
        self.assertEqual(owed, {})

    def test_a_failed_read_names_nobody(self):
        self.lot("dev0", cause=NOT_HEAD)
        with mock.patch("ameesh.packet_candidates.candidate_refusal",
                        side_effect=RuntimeError("git")):
            owed, error = fd.owed_work(self.db, ["dev0"], self.open_lots())
        self.assertIsNone(error)
        self.assertEqual(owed, {})

    def test_only_lots_awaiting_validation_with_a_branch_are_read(self):
        self.lot("dev0", state="build", cause=NOT_HEAD)
        self.lot("dev1", branch="", cause=NOT_HEAD)
        owed, _ = fd.owed_work(self.db, ["dev0", "dev1"], self.open_lots())
        self.assertEqual(owed, {})
        self.assertEqual(self.read, [])

    # -- sommeil et levée ------------------------------------------------
    def test_the_author_owing_a_refreeze_is_not_slept(self):
        split = self.exhausted_split()
        lot = self.lot("dev1", cause=NOT_HEAD)
        by_agent = self.sleep_out()
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertEqual(by_agent["dev1"]["why"],
                         "packet refusal on #%d: freeze-not-head (author)" % lot)
        self.assertTrue(by_agent["dev0"]["slept"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertIn("sleep exempted: dev1: packet refusal on #%d: freeze-not-head"
                      " (author)" % lot, self.notes(split))

    def test_a_sleeper_owing_a_refreeze_is_woken(self):
        self.exhausted_split()
        lot = self.lot("dev1")  # encore lisible comme prêt : endormi
        by_agent = self.sleep_out()
        self.assertTrue(by_agent["dev1"]["slept"])
        self.causes[lot] = dict(NOT_HEAD)  # la tête publiée a bougé
        listing = [dict(agent(name), **{
            k: registry.get(self.db, name)[k]
            for k in ("operator_paused", "operator_pause_reason",
                      "operator_pause_by", "operator_pause_for")})
            for name in ("dev0", "dev1")]
        out = self.run_pass(2000, listing=listing)
        woken = [g for g in out if g["kind"] == "wake"]
        self.assertEqual([g["agents"] for g in woken], [["dev1"]])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(registry.get(self.db, "dev0")["operator_paused"])
        event = self.db.query(
            "SELECT actor, reason FROM operator_pause_events WHERE agent = 'dev1'"
            " AND paused = false ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(event["actor"], fd.SLEEP_ACTOR)
        self.assertEqual(event["reason"],
                         "%s packet refusal on #%d: freeze-not-head (author)"
                         % (fd.SLEEP_LIFT_PREFIX, lot))

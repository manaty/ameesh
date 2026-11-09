# SPDX-License-Identifier: AGPL-3.0-only
"""#6575 — le sommeil « feuille de route épuisée » épargne le travail engagé.

Constat (bureau #205826, 09/11) : codex3 a été endormi alors qu'il devait le
regel de #6456, membre du paquet 513 (porteur #6465 en qa, CI rouge). Les
exemptions couvraient les relectures et les gels, pas le correctif dû.

Témoins du lot :

* l'auteur d'un lot membre d'un paquet dont le porteur est ouvert n'est pas
  endormi ; un porteur fusionné ne retient plus personne ;
* une correction due après un verdict bloquant (`due_fixes`) retient l'agent ;
* une commande `ameesh heavy` tenue sur l'hôte de la passe retient l'agent ;
* une lecture illisible n'endort personne ;
* un dormeur à qui un tel travail est dû est levé, motif au journal des pauses.
"""
from __future__ import annotations

import os
import time
from unittest import mock

from ameesh import file_decoupage as fd
from ameesh import heavy, mechanics, registry, work

from .support import PgTestCase
from .test_4610_sleep_work_mail import agent

SETTINGS = mechanics.QueueFill(repos_part=0.20, repos_s=1800, wake_mail_s=7200)


class SleepOwedWorkTest(PgTestCase):
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

    def listing(self, *names):
        return [agent(name) for name in (names or ("dev0", "dev1"))]

    def run_pass(self, offset, listing=None, cfg=None):
        return fd.run(cfg, self.db, now=self.t0 + offset, state=self.state,
                      listing=listing if listing is not None else self.listing(),
                      canons=[], settings=SETTINGS)

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

    def packet(self, member_assignee, *, carrier_state="qa", merged_sha=None):
        """Un porteur et une unité assignée, en qa (non actionnable)."""
        carrier = int(work.add(self.db, title="paquet", app="ameesh",
                               actor="pilote")["id"])
        work.assign(self.db, carrier, "pilote", actor="pilote")
        self.db.execute("UPDATE work_items SET state = %s WHERE id = %s",
                        (carrier_state, carrier))
        unit = int(work.add(self.db, title="unité", app="ameesh", actor="pilote")["id"])
        work.assign(self.db, unit, member_assignee, actor="pilote")
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (unit,))
        # gelé, sans verdict : le lot n'est pas actionnable (cas de codex3)
        work.milestone(self.db, unit, "frozen", sha="c" * 40, actor=member_assignee)
        rows = [{"package_id": carrier, "work_item_id": unit,
                 "package_state": carrier_state, "package_merged_sha": merged_sha}]
        patch = mock.patch("ameesh.packet_link.memberships",
                           side_effect=lambda db, item_ids=None, package_ids=None: [
                               r for r in rows if item_ids is None
                               or r["work_item_id"] in item_ids])
        patch.start()
        self.addCleanup(patch.stop)
        return carrier, unit

    def sleep_out(self, offset=1900, cfg=None, listing=None):
        out = self.run_pass(offset, listing=listing, cfg=cfg)
        sleep = next(g for g in out if g["kind"] == "sleep")
        return {row["agent"]: row for row in sleep["agents"]}

    # -- épargnés --------------------------------------------------------
    def test_the_author_of_an_open_packet_member_is_spared(self):
        split = self.exhausted_split()
        carrier, unit = self.packet("dev1")
        by_agent = self.sleep_out()
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertEqual(by_agent["dev1"]["why"],
                         "member #%d of open packet #%d" % (unit, carrier))
        self.assertTrue(by_agent["dev0"]["slept"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertIn("sleep exempted: dev1: member #%d of open packet #%d"
                      % (unit, carrier), self.notes(split))

    def test_a_merged_packet_no_longer_holds_its_author(self):
        self.exhausted_split()
        self.packet("dev1", carrier_state="merged", merged_sha="a" * 40)
        by_agent = self.sleep_out()
        self.assertTrue(by_agent["dev1"]["slept"])

    def test_an_owed_fix_is_spared(self):
        split = self.exhausted_split()
        rows = [{"lot": 6456, "name": "DEV1", "sha": "b" * 40}]
        with mock.patch("ameesh.review_designation_activation.due_fixes",
                        return_value=rows):
            by_agent = self.sleep_out()
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertEqual(by_agent["dev1"]["why"], "fix owed on #6456")
        self.assertTrue(by_agent["dev0"]["slept"])
        self.assertIn("sleep exempted: dev1: fix owed on #6456", self.notes(split))

    def test_a_heavy_job_held_on_this_host_is_spared(self):
        # Lecture directe : une passe avec `cfg` lirait le canon de la
        # configuration (rôles critiques), hors du propos de ce témoin.
        with mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": "dev1",
                                          "AMEESH_MAX_HEAVY_SUITES": "1"}):
            held = heavy.acquire(self.cfg, command="scripts/test.sh -p sql")
            self.addCleanup(heavy.release, held[1])
            owed, error = fd.owed_work(self.db, ["dev0", "dev1"], [], cfg=self.cfg)
        self.assertIsNone(error)
        self.assertEqual(owed, {"dev1": "heavy job running (slot %d)" % held[0]})
        self.assertNotIn("scripts/test.sh", str(owed))

    def test_a_heavy_command_never_reaches_the_reason_or_journal(self):
        # Le motif finit dans `work.note` (partagé) : une commande sentinelle
        # ne doit sortir ni dans `why`, ni dans la note du lot de découpage.
        split = self.exhausted_split()
        sentinel = "FAKE_PRIVATE_COMMAND_6575"
        with mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": "dev1",
                                          "AMEESH_MAX_HEAVY_SUITES": "1"}):
            held = heavy.acquire(self.cfg, command=sentinel)
            self.addCleanup(heavy.release, held[1])
            owed, error = fd.owed_work(self.db, ["dev0", "dev1"], [], cfg=self.cfg)
            self.assertIsNone(error)
            self.assertEqual(owed, {"dev1": "heavy job running (slot %d)" % held[0]})
            self.assertNotIn(sentinel, str(owed))
            fd._journal_exemption(self.db, split, "dev1", owed["dev1"],
                                  since_ts=self.t0)
        notes = "\n".join(self.notes(split))
        self.assertNotIn(sentinel, notes)
        self.assertIn("sleep exempted: dev1: heavy job running (slot %d)" % held[0],
                      notes)

    def test_unreadable_owed_work_sleeps_nobody(self):
        split = self.exhausted_split()
        with mock.patch("ameesh.review_designation_activation.due_fixes",
                        side_effect=RuntimeError("base")):
            by_agent = self.sleep_out()
        self.assertFalse(any(row["slept"] for row in by_agent.values()))
        self.assertTrue(any(note.startswith("sleep exempted: *: owed work unreadable")
                            for note in self.notes(split)))

    # -- levée -----------------------------------------------------------
    def test_a_sleeper_owing_a_packet_fix_is_woken(self):
        self.exhausted_split()
        by_agent = self.sleep_out()
        self.assertTrue(by_agent["dev1"]["slept"])
        self.assertTrue(registry.get(self.db, "dev1")["operator_paused"])
        carrier, unit = self.packet("dev1")
        listing = [dict(agent(name), **{
            k: registry.get(self.db, name)[k]
            for k in ("operator_paused", "operator_pause_reason",
                      "operator_pause_by", "operator_pause_for")})
            for name in ("dev0", "dev1")]
        out = self.run_pass(2000, listing=listing)
        woken = [g for g in out if g["kind"] == "wake"]
        self.assertEqual([g["agents"] for g in woken], [["dev1"]])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        event = self.db.query(
            "SELECT actor, reason FROM operator_pause_events WHERE agent = 'dev1'"
            " AND paused = false ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(event["actor"], fd.SLEEP_ACTOR)
        self.assertEqual(event["reason"], "%s member #%d of open packet #%d"
                         % (fd.SLEEP_LIFT_PREFIX, unit, carrier))

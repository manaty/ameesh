# SPDX-License-Identifier: AGPL-3.0-only
"""#4610 — le sommeil « feuille de route épuisée » respecte le travail confié.

Témoins du lot :

* un agent à qui un lot actionnable vient d'être attribué n'est pas endormi,
  et le motif est journalisé sur le lot de découpage ;
* un courrier non lu récent du pilote (ou d'un orchestrateur) retient l'agent
  de la même façon ;
* la pause est levée automatiquement dès qu'un lot actionnable lui est
  attribué ou qu'un courrier du pilote ou d'un orchestrateur arrive, avec
  l'acteur `ameesh:file-decoupage` et la cause au journal des pauses ;
* le cas normal reste le sommeil, et un lot non actionnable (prévu plus tard)
  ne provoque pas de va-et-vient sommeil/réveil.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from ameesh import file_decoupage as fd
from ameesh import mail, mechanics, registry, undeliverable, work

from .support import PgTestCase

#: La fenêtre du courrier est large : les témoins sautent de 1800 s pour
#: créer le lot de découpage, un courrier déposé au début resterait sinon plus
#: vieux que `repos_s` au moment du sommeil.
SETTINGS = mechanics.QueueFill(repos_part=0.20, repos_s=1800, wake_mail_s=7200)


def agent(name, *, idle=True, project="ameesh", harness="deepseek", **extra):
    row = {"name": name, "mode": "execute", "status": "idle", "lease_live": True,
           "lease_owner": "runner:h1", "unread": 0, "assigned_lot_id": None,
           "active_lots": 0, "actionable_lots": 0 if idle else 1,
           "projects": [project], "team": project, "harness": harness,
           "responsible": "human:alice"}
    row.update(extra)
    return row


class SleepWorkMailTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("pilote", "dev0", "dev1", "dev2"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice',"
                        " team = 'ameesh'")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=["pilote"])
        pilots.start()
        self.addCleanup(pilots.stop)
        self.state: dict = {}
        self.t0 = time.time()

    # -- helpers ---------------------------------------------------------
    def listing(self, *names):
        return [agent(name) for name in (names or ("dev0", "dev1", "dev2"))]

    def run_pass(self, offset, listing=None):
        return fd.run(None, self.db, now=self.t0 + offset, state=self.state,
                      listing=listing, canons=[], settings=SETTINGS)

    def split_lot(self):
        rows = self.db.query("SELECT id FROM work_items WHERE source = %s",
                             ("file-decoupage:ameesh",))
        return int(rows[0]["id"])

    def make_split(self):
        self.run_pass(0, self.listing())
        self.run_pass(1800, self.listing())
        return self.split_lot()

    def notes(self, lot):
        return [r["note"] for r in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(lot),))]

    def sleep_all(self):
        split = self.make_split()
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        out = self.run_pass(1900, self.listing())
        slept = next(g for g in out if g["kind"] == "sleep")
        self.assertTrue(any(row["slept"] for row in slept["agents"]))
        return split

    def lift_event(self, name):
        rows = self.db.query(
            "SELECT actor, principal, reason FROM operator_pause_events"
            " WHERE agent = %s AND paused = false ORDER BY id DESC LIMIT 1", (name,))
        return rows[0] if rows else None

    # -- trois cas épargnés ---------------------------------------------
    def test_an_assigned_actionable_lot_is_spared(self):
        split = self.make_split()
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        made = work.add(self.db, title="travail confié", app="ameesh", actor="pilote")
        work.assign(self.db, int(made["id"]), "dev1", actor="pilote")
        out = self.run_pass(1900, self.listing("dev0", "dev1"))
        sleep = next(g for g in out if g["kind"] == "sleep")
        by_agent = {row["agent"]: row for row in sleep["agents"]}
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertIn("assigned actionable lot #%d" % int(made["id"]),
                      by_agent["dev1"]["why"])
        self.assertTrue(by_agent["dev0"]["slept"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(any(note.startswith("sleep exempted: dev1:")
                            and "assigned actionable lot #%d" % int(made["id"]) in note
                            for note in self.notes(split)))

    def test_recent_unread_pilot_mail_is_spared(self):
        split = self.make_split()
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        mail.send(self.db, "pilote", "dev1", "Prends ce lot, il est pour toi.")
        out = self.run_pass(1900, self.listing("dev0", "dev1"))
        sleep = next(g for g in out if g["kind"] == "sleep")
        by_agent = {row["agent"]: row for row in sleep["agents"]}
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertIn("unread mail from pilote", by_agent["dev1"]["why"])
        self.assertTrue(by_agent["dev0"]["slept"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(any(note.startswith("sleep exempted: dev1:")
                            and "unread mail from pilote" in note
                            for note in self.notes(split)))

    def test_the_normal_case_still_sleeps(self):
        split = self.make_split()
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        out = self.run_pass(1900, self.listing("dev0", "dev1"))
        sleep = next(g for g in out if g["kind"] == "sleep")
        self.assertEqual(sorted(row["agent"] for row in sleep["agents"] if row["slept"]),
                         ["dev0", "dev1"])
        self.assertFalse(any(note.startswith(fd.SLEEP_EXEMPT_PREFIX)
                             for note in self.notes(split)))

    # -- levée automatique ----------------------------------------------
    def test_an_assigned_lot_wakes_the_sleeper(self):
        split = self.sleep_all()
        self.assertTrue(registry.get(self.db, "dev1")["operator_paused"])
        made = work.add(self.db, title="travail confié", app="ameesh", actor="pilote")
        assigned = work.assign(self.db, int(made["id"]), "dev1", actor="pilote")
        self.assertIn("automatic roadmap sleep",
                      (assigned.get("check") or {}).get("warning") or "")
        out = self.run_pass(2000)
        woken = [g for g in out if g["kind"] == "wake"]
        self.assertEqual(len(woken), 1)
        self.assertEqual(woken[0]["project"], "ameesh")
        self.assertEqual(woken[0]["agents"], ["dev1"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(registry.get(self.db, "dev0")["operator_paused"])
        lifted = self.lift_event("dev1")
        self.assertEqual((lifted["actor"], lifted["principal"]),
                         (fd.SLEEP_ACTOR, "human:alice"))
        self.assertTrue(lifted["reason"].startswith(
            "automatic sleep lifted: assigned actionable lot #%d" % int(made["id"])))
        # une seule fois : le passage suivant ne relève plus rien
        self.assertFalse([g for g in self.run_pass(2100) if g["kind"] == "wake"])

    def test_pilot_mail_wakes_the_sleeper(self):
        self.sleep_all()
        mail.send(self.db, "pilote", "dev1", "Nouveau travail : regarde ce lot.")
        out = self.run_pass(2000)
        woken = [g for g in out if g["kind"] == "wake"]
        self.assertEqual(len(woken), 1)
        self.assertEqual(woken[0]["agents"], ["dev1"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(registry.get(self.db, "dev0")["operator_paused"])
        lifted = self.lift_event("dev1")
        self.assertEqual((lifted["actor"], lifted["principal"]),
                         (fd.SLEEP_ACTOR, "human:alice"))
        self.assertEqual(lifted["reason"],
                         "automatic sleep lifted: unread mail from pilote")

    def test_orchestrator_mail_wakes_the_sleeper(self):
        # le rôle n'est pas un nom en dur : il vient de la lecture du registre
        registry.upsert(self.db, "orchestrateur", harness="deepseek",
                        host=self.cfg.host, cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice',"
                        " team = 'ameesh' WHERE name = 'orchestrateur'")
        self.sleep_all()
        mail.send(self.db, "orchestrateur", "dev1", "Nouveau travail.")
        with mock.patch("ameesh.sous_utilisation.alert_orchestrators",
                        return_value=["orchestrateur"]):
            out = self.run_pass(2000)
        woken = [g for g in out if g["kind"] == "wake"]
        self.assertEqual(len(woken), 1)
        self.assertEqual(woken[0]["agents"], ["dev1"])
        self.assertEqual(self.lift_event("dev1")["reason"],
                         "automatic sleep lifted: unread mail from orchestrateur")

    def test_a_review_designated_after_the_sleep_wakes_the_agent(self):
        # Cas du pilote (#164353) : codex2, relecteur désigné de #4577, remis
        # en pause après une levée. Une relecture due est du travail actionnable
        # (#1726) : le dormeur doit être réveillé, même sans lot à lui.
        self.sleep_all()
        lot = work.add(self.db, title="gel en attente", app="ameesh",
                       assignee="dev2", actor="pilote")
        lot_id = int(lot["id"])
        work.move(self.db, lot_id, "build", actor="dev2")
        work.milestone(self.db, lot_id, "frozen", sha="a" * 40, actor="dev2")
        self.db.execute("UPDATE work_items SET reviewers = ARRAY['dev1']"
                        " WHERE id = %s", (lot_id,))
        out = self.run_pass(2000)
        woken = [g for g in out if g["kind"] == "wake"]
        self.assertEqual(len(woken), 1)
        self.assertEqual(woken[0]["agents"], ["dev1"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertEqual(self.lift_event("dev1")["reason"],
                         "automatic sleep lifted: actionable work due")

    def test_mail_from_an_ordinary_agent_does_not_wake(self):
        self.sleep_all()
        # #4610 (relecture auditeur) : l'expéditeur est averti que son
        # courrier n'ouvrira pas de tour ; le message est déposé quand même.
        warned = undeliverable.check_send(self.cfg, self.db,
                                          sender="dev2", dest="dev1")
        self.assertEqual(len(warned), 1)
        self.assertIn("automatic roadmap sleep", warned[0])
        self.assertIn("address another agent", warned[0])
        mail.send(self.db, "dev2", "dev1", "Coucou, rien d'urgent.")
        out = self.run_pass(2000)
        self.assertFalse([g for g in out if g["kind"] == "wake"])
        self.assertTrue(registry.get(self.db, "dev1")["operator_paused"])

    def test_a_non_actionable_assigned_lot_does_not_wake(self):
        # Un lot attribué mais prévu plus tard n'est pas actionnable : le
        # réveiller à chaque passage ferait un va-et-vient sommeil/réveil.
        self.sleep_all()
        made = work.add(self.db, title="plus tard", app="ameesh", actor="pilote")
        work.assign(self.db, int(made["id"]), "dev1", actor="pilote")
        self.db.execute("UPDATE work_items SET planned_start = DATE '2999-01-01'"
                        " WHERE id = %s", (int(made["id"]),))
        out = self.run_pass(2000)
        self.assertFalse([g for g in out if g["kind"] == "wake"])
        self.assertTrue(registry.get(self.db, "dev1")["operator_paused"])


if __name__ == "__main__":
    unittest.main()

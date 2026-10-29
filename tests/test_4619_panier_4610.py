# SPDX-License-Identifier: AGPL-3.0-only
"""#4619 — panier #4610 : les cinq défauts non bloquants.

Témoins du lot :

* `_recent_mail` voit un non-lu pertinent plus vieux que les 200 derniers
  courriers (lus ou passifs) : `mail.unread` complète `mail.history` ;
* la fenêtre du courrier est la même au sommeil et à la levée : un non-lu
  plus vieux que `file.wake_mail_s` ne réveille plus un dormeur (fin du
  va-et-vient sommeil/levée) ;
* le journal nomme le lot réellement actionnable, pas le lot assigné le plus
  récemment touché quand celui-ci est prévu plus tard ;
* `wake_note` n'avertit plus le pilote ni l'orchestrateur que son courrier
  n'ouvrira pas de tour, puisque leur courrier lève le sommeil ;
* le maintien de `file.wake_mail_s` hors canon avant que tous les hôtes
  lisent ce code est une consigne de déploiement, notée dans la conception
  (`docs/design/3256-debit-file.md`) et à la définition de la clé.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from ameesh import file_decoupage as fd
from ameesh import mail, mechanics, registry, undeliverable, work

from .support import PgTestCase

SETTINGS = mechanics.QueueFill(repos_part=0.20, repos_s=1800, wake_mail_s=7200)


def agent(name, *, idle=True, project="ameesh", harness="deepseek", **extra):
    row = {"name": name, "mode": "execute", "status": "idle", "lease_live": True,
           "lease_owner": "runner:h1", "unread": 0, "assigned_lot_id": None,
           "active_lots": 0, "actionable_lots": 0 if idle else 1,
           "projects": [project], "team": project, "harness": harness,
           "responsible": "human:alice"}
    row.update(extra)
    return row


class Panier4610Test(PgTestCase):
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

    def make_split(self):
        self.run_pass(0, self.listing())
        self.run_pass(1800, self.listing())
        rows = self.db.query("SELECT id FROM work_items WHERE source = %s",
                             ("file-decoupage:ameesh",))
        return int(rows[0]["id"])

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

    def notes(self, lot):
        return [r["note"] for r in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(lot),))]

    def assign_later_and_actionable(self):
        """Deux lots pour dev1 : le premier actionnable, le second prévu plus
        tard (donc non actionnable) et touché en dernier, pour que
        `assigned_lot_id` ne soit pas le lot actionnable."""
        first = work.add(self.db, title="actionnable", app="ameesh", actor="pilote")
        work.assign(self.db, int(first["id"]), "dev1", actor="pilote")
        later = work.add(self.db, title="plus tard", app="ameesh", actor="pilote")
        # #540 : le second lot non démarré irait à la file de l'équipe ;
        # l'exception est explicite, c'est un montage de témoin.
        work.assign(self.db, int(later["id"]), "dev1", actor="pilote",
                    force="témoin #4619 : lot prévu plus tard")
        # Ordre du `assigned_lot_id` (lot assigné le plus récemment touché)
        # rendu déterministe : le lot prévu plus tard est le plus récent.
        self.db.execute("UPDATE work_items SET updated_at = now() + interval '1 minute'"
                        " WHERE id = %s", (int(later["id"]),))
        self.db.execute("UPDATE work_items SET updated_at = now() - interval '1 minute'"
                        " WHERE id = %s", (int(first["id"]),))
        self.db.execute("UPDATE work_items SET planned_start = DATE '2999-01-01'"
                        " WHERE id = %s", (int(later["id"]),))
        return int(first["id"]), int(later["id"])

    # -- 1. les non-lus au-delà des 200 derniers courriers ---------------
    def test_relevant_unread_beyond_the_last_200_is_seen(self):
        split = self.make_split()
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        mail.send(self.db, "pilote", "dev1", "Prends ce lot, il est pour toi.",
                  thread=False)
        # 205 courriers lus plus récents : le courrier du pilote sort de la
        # fenêtre de `mail.history` (200 derniers), pas de `mail.unread`.
        delivered = [mail.send(self.db, "dev0", "dev1", "bruit %d" % i,
                               thread=False)
                     for i in range(205)]
        mail.mark_delivered(self.db, delivered)
        out = self.run_pass(1900, self.listing("dev0", "dev1"))
        sleep = next(g for g in out if g["kind"] == "sleep")
        by_agent = {row["agent"]: row for row in sleep["agents"]}
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertIn("unread mail from pilote", by_agent["dev1"]["why"])
        self.assertTrue(by_agent["dev0"]["slept"])

    # -- 2. une seule fenêtre de courrier --------------------------------
    def test_mail_older_than_the_window_does_not_wake(self):
        self.sleep_all()
        mail.send(self.db, "pilote", "dev1", "Vieux message.", thread=False)
        out = self.run_pass(1900 + 7200 + 60)
        self.assertFalse([g for g in out if g["kind"] == "wake"])
        self.assertTrue(registry.get(self.db, "dev1")["operator_paused"])
        self.assertIsNone(self.lift_event("dev1"))

    # -- 3. le lot réellement actionnable au journal ---------------------
    def test_sleep_journal_names_the_actionable_lot(self):
        split = self.make_split()
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        actionable, later = self.assign_later_and_actionable()
        out = self.run_pass(1900, self.listing("dev0", "dev1"))
        sleep = next(g for g in out if g["kind"] == "sleep")
        by_agent = {row["agent"]: row for row in sleep["agents"]}
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertIn("assigned actionable lot #%d" % actionable,
                      by_agent["dev1"]["why"])
        self.assertNotIn("#%d" % later, by_agent["dev1"]["why"])
        self.assertTrue(any(note.startswith("sleep exempted: dev1:")
                            and "#%d" % actionable in note
                            for note in self.notes(split)))

    def test_wake_journal_names_the_actionable_lot(self):
        self.sleep_all()
        actionable, later = self.assign_later_and_actionable()
        out = self.run_pass(2000)
        woken = [g for g in out if g["kind"] == "wake"]
        self.assertEqual(len(woken), 1)
        self.assertEqual(woken[0]["agents"], ["dev1"])
        self.assertEqual(self.lift_event("dev1")["reason"],
                         "automatic sleep lifted: assigned actionable lot #%d"
                         % actionable)
        self.assertNotIn("#%d" % later, self.lift_event("dev1")["reason"])

    # -- 4/5. l'avertissement tient compte de l'expéditeur ---------------
    def test_pilot_mail_does_not_warn_the_pilot(self):
        self.sleep_all()
        warned = undeliverable.check_send(self.cfg, self.db,
                                          sender="pilote", dest="dev1")
        self.assertEqual(warned, [])

    def test_orchestrator_mail_does_not_warn_the_orchestrator(self):
        registry.upsert(self.db, "orchestrateur", harness="deepseek",
                        host=self.cfg.host, cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice',"
                        " team = 'ameesh' WHERE name = 'orchestrateur'")
        self.sleep_all()
        with mock.patch("ameesh.sous_utilisation.alert_orchestrators",
                        return_value=["orchestrateur"]):
            warned = undeliverable.check_send(self.cfg, self.db,
                                              sender="orchestrateur", dest="dev1")
        self.assertEqual(warned, [])

    def test_ordinary_mail_still_warns(self):
        self.sleep_all()
        warned = undeliverable.check_send(self.cfg, self.db,
                                          sender="dev2", dest="dev1")
        self.assertEqual(len(warned), 1)
        self.assertIn("automatic roadmap sleep", warned[0])
        self.assertIn("address another agent", warned[0])


if __name__ == "__main__":
    unittest.main()

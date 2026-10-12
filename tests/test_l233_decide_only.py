# SPDX-License-Identifier: AGPL-3.0-only
"""L233: owner decisions only through `ameesh decide`.

(a) the orchestrator instructions and the generated briefs require
`ameesh decide ask`; (b) a lot enters `waiting_human` only through an open
decision request (refused otherwise; alert `waiting_without_question` for
existing rows); (c) `ameesh decisions` — and so `ameesh chat` — lists the
decisions written in prose in mail to bureau or to a human, and offers to
convert them (`ameesh decide ask --from-mail <id>`).
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from ameesh import alert_briefs, chat, decisions, exploitation, mail, notify, work

from .support import hold_for_owner
from .test_l124_decisions import ALICE, _Base

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ==========================================================================
# (b) waiting_human only through an open request
# ==========================================================================

class WaitingHumanTest(_Base):
    def test_move_refused_without_request(self):
        with self.assertRaises(work.WorkError) as ctx:
            work.move(self.db, self.lot["id"], "waiting_human", actor="orch")
        text = str(ctx.exception)
        self.assertIn("only through an open owner decision request", text)
        self.assertIn("ameesh decide ask --lot %d" % self.lot["id"], text)
        self.assertEqual(self.lot_state(), "build")

    def test_ask_moves_the_lot_and_answer_releases_it(self):
        view = self.ask()
        self.assertEqual(view["lot_state"], "waiting_human")
        self.answer(view["id"], "a")
        self.assertEqual(self.lot_state(), "build")
        # with the request closed, the manual move is refused again
        with self.assertRaises(work.WorkError):
            work.move(self.db, self.lot["id"], "waiting_human")

    def test_move_allowed_while_a_request_is_open(self):
        hold_for_owner(self.db, self.lot["id"], requester="orch")
        self.assertEqual(self.lot_state(), "waiting_human")
        work.move(self.db, self.lot["id"], "blocked")
        work.move(self.db, self.lot["id"], "waiting_human")      # request still open
        self.assertEqual(self.lot_state(), "waiting_human")

    def test_cli_move_refused(self):
        proc = self.mesh("work", "move", str(self.lot["id"]), "waiting_human",
                         env=self.mesh_env(AGENT_MAIL_NAME="dev1"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ameesh decide ask --lot %d" % self.lot["id"], proc.stderr)

    def _without_question(self):
        return [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "waiting_without_question"]

    def test_alert_for_existing_rows(self):
        # a row that predates the rule
        self.db.execute("UPDATE work_items SET state = 'waiting_human' WHERE id = %s",
                        (self.lot["id"],))
        alerts = self._without_question()
        self.assertEqual([(a["lot"], a["agent"], a["lot_state"]) for a in alerts],
                         [(self.lot["id"], "dev1", "waiting_human")])
        self.assertIn("ameesh decide ask --lot %d" % self.lot["id"], alerts[0]["detail"])
        self.assertIn("waiting_without_question", exploitation.ALERT_TYPES)
        self.assertIn("waiting_without_question", notify.TYPE_LABELS)
        # once a question is asked, the alert is gone
        self.ask()
        self.assertEqual(self._without_question(), [])

    def test_alert_when_left_waiting_after_answer(self):
        self.db.execute("UPDATE work_items SET state = 'waiting_human' WHERE id = %s",
                        (self.lot["id"],))
        view = self.ask()                       # held before the request: stays waiting
        self.answer(view["id"], "a")
        self.assertEqual(self.lot_state(), "waiting_human")
        self.assertEqual([a["lot"] for a in self._without_question()], [self.lot["id"]])

    def test_move_and_open_request_checked_together(self):
        # r1: a request answered between the check and the update — the
        # conditional UPDATE (request row locked) refuses the move
        view = self.ask()
        self.answer(view["id"], "a")
        self.assertEqual(self.lot_state(), "build")
        from ameesh import storage
        real = storage.of(self.db).decisions.pending_for_lot
        stale = [{"id": view["id"]}]
        calls = []

        def first_call_stale(lot):
            calls.append(lot)
            return stale if len(calls) == 1 else real(lot)

        with mock.patch.object(type(storage.of(self.db).decisions), "pending_for_lot",
                               side_effect=first_call_stale):
            with self.assertRaises(work.WorkError) as ctx:
                work.move(self.db, self.lot["id"], "waiting_human")
        self.assertIn("only through an open owner decision request", str(ctx.exception))
        self.assertEqual(self.lot_state(), "build")
        self.assertEqual(len(calls), 2)
        # the storage statement alone: no open request, nothing written
        self.assertIsNone(storage.of(self.db).work.move(
            self.lot["id"], "waiting_human", current="build", loops=0, note="x",
            actor="t", held=True))
        self.assertEqual(self.lot_state(), "build")

    def test_alert_key_stable_across_notes(self):
        hold_for_owner(self.db, self.lot["id"], requester="orch")
        # the request closes without releasing the lot (legacy-like)
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now() WHERE kind = 'request'")
        first = self._without_question()
        self.assertEqual(len(first), 1)
        time.sleep(1.1)
        work.note(self.db, self.lot["id"], "still thinking", actor="dev1")
        again = self._without_question()
        self.assertEqual([notify.key_of(a) for a in again], [notify.key_of(first[0])])
        # a new waiting episode is a new alert
        self.db.execute("UPDATE work_items SET state = 'build' WHERE id = %s",
                        (self.lot["id"],))
        time.sleep(1.1)
        work.note(self.db, self.lot["id"], "back to build", actor="dev1")
        hold_for_owner(self.db, self.lot["id"], requester="orch")
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now() WHERE kind = 'request'")
        third = self._without_question()
        self.assertEqual(len(third), 1)
        self.assertNotEqual(notify.key_of(third[0]), notify.key_of(first[0]))
        self.assertNotEqual(third[0]["episode"], first[0]["episode"])

    def test_alert_key_tells_episodes_apart_within_a_second(self):
        # `since` is rounded to the second: the episode (journal line id)
        # keeps two episodes starting in the same second apart
        base = {"type": "waiting_without_question", "agent": "dev1", "lot": 7,
                "since": 1000.2}
        self.assertNotEqual(exploitation.alert_key(dict(base, since=1000.2, episode=11)),
                            exploitation.alert_key(dict(base, since=1000.4, episode=12)))
        self.assertEqual(exploitation.alert_key(dict(base, episode=11)),
                         exploitation.alert_key(dict(base, since=1000.4, episode=11)))

    def test_decisions_queue_does_not_count_unasked_lots(self):
        self.db.execute("UPDATE work_items SET state = 'waiting_human' WHERE id = %s",
                        (self.lot["id"],))
        proc = self.mesh("decisions", env=self.mesh_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no pending decision", proc.stdout)
        self.assertIn("lots in waiting_human with no question asked", proc.stdout)
        self.assertIn("RT-9 : cercle de confiance", proc.stdout)


# ==========================================================================
# (a) instructions and generated briefs
# ==========================================================================

class BriefsTest(unittest.TestCase):
    ALERT = {"type": "waiting_without_question", "agent": "dev1", "lot": 99,
             "title": "export", "lot_state": "waiting_human", "value": 600,
             "orchestrators": ["orch", "orch-b"]}

    def setUp(self):
        self.listing = [dict(name="orch", team="a"), dict(name="orch-b", team="b"),
                        dict(name="dev1", team="a"), dict(name="auditeur", team="")]

    def test_brief_to_assignee_and_team_orchestrator(self):
        briefs = alert_briefs.briefs(self.ALERT, self.listing, auditor_names=["auditeur"])
        self.assertEqual(sorted(briefs), ["auditeur", "dev1", "orch"])
        text = briefs["dev1"]
        self.assertIn("#99 « export »", text)
        self.assertIn('ameesh decide ask --lot 99 --question "…"', text)
        self.assertIn("ameesh work move 99 <state>", text)
        self.assertIn("never counts", text)
        self.assertIn("waiting_without_question", alert_briefs.BRIEFED_TYPES)

    def test_orchestrator_instructions(self):
        with open(os.path.join(ROOT, "docs", "ORCHESTRATEUR.md"), encoding="utf-8") as fh:
            text = fh.read()
        for expected in ("Prose never counts (L233)", "ameesh decide ask --from-mail <id>",
                         "A lot enters `waiting_human` only through an open request",
                         "waiting_without_question"):
            self.assertIn(expected, text)


# ==========================================================================
# (c) decisions written in prose in mail
# ==========================================================================

class ProseTest(_Base):
    REPORT = ("Rapport 02:56, section 4 — décisions : 1) GO ? pour la bascule ; "
              "2) le schéma est à valider.")

    def send(self, body, *, sender="orch", recipient="bureau", lot=None, kind="notify"):
        return mail.send(self.db, sender, recipient, body, kind=kind,
                         work_item_id=str(lot) if lot else None)

    def test_cues(self):
        self.assertEqual(decisions.prose_cues(self.REPORT), ["décisions", "go ?", "à valider"])
        self.assertEqual(decisions.prose_cues("Decision needed: go/no-go on deploy; "
                                              "needs your approval"),
                         ["decision", "go/no-go", "needs your approval"])
        self.assertEqual(decisions.prose_cues("Tests verts, PR #12 ouverte, au repos."), [])
        self.assertEqual(decisions.prose_cues("Google ? ago?"), [])

    def test_detected_and_filtered(self):
        report = self.send(self.REPORT, lot=self.lot["id"])
        to_human = self.send("Decision: which carrier?", recipient=ALICE)
        self.send("Point d'étape : rien à valider.", recipient="dev1")     # not bureau
        self.send("décision système", sender="ameesh")                    # system mail
        self.send("décision du propriétaire", sender=ALICE)               # a human
        self.send("décision à transmettre", sender="chat-alice")          # the chat
        self.send("alerte : décision attendue", kind="event")             # an alert
        self.send("Point d'étape, tests verts.")                           # no cue
        self.ask()                                                         # a request
        rows = decisions.prose_decisions(self.db)
        self.assertEqual([r["id"] for r in rows], [report, to_human])
        first = rows[0]
        self.assertEqual((first["sender"], first["recipient"], first["lot"]),
                         ("orch", "bureau", self.lot["id"]))
        self.assertEqual(first["cues"], ["décisions", "go ?", "à valider"])
        self.assertIn("ameesh decide ask --from-mail %d --question" % report, first["convert"])
        self.assertIn("--lot <id>", rows[1]["convert"])          # mail with no lot
        # out of the window
        self.assertEqual(decisions.prose_decisions(self.db, since_s=0.0) , [])

    def test_convert_from_mail(self):
        report = self.send(self.REPORT, lot=self.lot["id"])
        with self.assertRaises(decisions.DecisionError) as ctx:
            self.ask(requester="dev1", source_mail=report, lot=None)
        self.assertIn("only its author", str(ctx.exception))
        with self.assertRaises(decisions.DecisionError):
            self.ask(source_mail=987654, lot=None)
        view = self.ask(source_mail=report, lot=None,
                        question="GO pour la bascule ?")     # lot of the mail by default
        self.assertEqual((view["lot"], view["lot_state"]), (self.lot["id"], "waiting_human"))
        stored = decisions.get(self.db, view["id"])
        self.assertEqual(stored["id"], view["id"])
        row = self.db.query("SELECT payload FROM agent_mailbox WHERE id = %s", (view["id"],))
        payload = row[0]["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        self.assertEqual(payload["decision"]["source_mail"], report)
        self.assertEqual(decisions.prose_decisions(self.db), [])     # converted

    def test_cli_decisions_and_from_mail(self):
        report = self.send(self.REPORT, lot=self.lot["id"])
        proc = self.mesh("decisions", "--json", env=self.mesh_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        queue = json.loads(proc.stdout)
        self.assertEqual([p["id"] for p in queue["prose"]], [report])
        self.assertEqual(queue["requests"], [])
        texte = self.mesh("decisions", env=self.mesh_env()).stdout
        for expected in ("no pending decision",
                         "decisions written in prose in mail — not requests",
                         "mail #%d orch → bureau" % report,
                         "ameesh decide ask --from-mail %d" % report):
            self.assertIn(expected, texte)
        # --for keeps bureau and the human's own mail, not another human's
        self.send("Decision for bob: carrier?", recipient="human:bob")
        queue = json.loads(self.mesh("decisions", "--for", ALICE, "--json",
                                     env=self.mesh_env()).stdout)
        self.assertEqual([p["id"] for p in queue["prose"]], [report])
        # the author converts it
        proc = self.mesh("decide", "ask", "--from-mail", str(report), "--question",
                         "GO pour la bascule ?", "--option", "a=oui", "--option", "b=non",
                         "--json", env=self.mesh_env(AGENT_MAIL_NAME="orch"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["lot"], self.lot["id"])
        queue = json.loads(self.mesh("decisions", "--json", env=self.mesh_env()).stdout)
        self.assertEqual([p["recipient"] for p in queue["prose"]], ["human:bob"])
        self.assertEqual(len(queue["requests"]), 1)

    def test_chat_shows_only_its_human_prose(self):
        # r1: in the chat (plain `ameesh decisions`), no excerpt of mail
        # addressed to another human
        mine = self.send("Decision: which carrier?", recipient=ALICE)
        desk = self.send(self.REPORT)
        self.send("Decision for bob: carrier?", recipient="human:bob")
        env = self.mesh_env(AMEESH_CHAT="chat-alice")
        proc = self.mesh("decisions", "--json", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([p["id"] for p in json.loads(proc.stdout)["prose"]], [mine, desk])
        texte = self.mesh("decisions", env=env).stdout
        self.assertNotIn("Decision for bob", texte)
        # outside the chat, without --for, everything (the owner's terminal)
        queue = json.loads(self.mesh("decisions", "--json", env=self.mesh_env()).stdout)
        self.assertEqual(len(queue["prose"]), 3)

    def test_chat_consigne_offers_conversion(self):
        text = chat.consigne("human:alice", "chat-alice")
        for expected in ("Decisions written in prose (L233)", "--from-mail <id>",
                         "You never file the request yourself",
                         "lots in waiting_human with no question asked"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""#4525 — le sommeil « feuille de route épuisée » épargne les relecteurs.

Témoins du lot :

* un relecteur désigné (une relecture encore due sur le dernier gel) n'est
  pas endormi, et le motif est journalisé sur le lot de découpage ;
* le dernier relecteur disponible d'une famille n'est pas endormi tant qu'un
  gel du projet attend un avis : sans cela, la famille entière disparaît et
  la désignation rend « 0 eligible families » (constat #4507) ;
* l'acteur du geste est `ameesh:file-decoupage`, l'owner n'étant que le
  principal ; la levée automatique porte le même acteur ;
* l'alerte de pause prolongée reconnaît `SLEEP_REASON` et ne demande pas à
  l'owner de mettre fin à une maintenance.
"""
from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from ameesh import agent_pause, alert_briefs, exploitation, file_decoupage as fd
from ameesh import mechanics, registry, storage, work

from .support import PgTestCase
from .test_l1616_mechanics import MECHANICS

SETTINGS = mechanics.QueueFill(repos_part=0.20, repos_s=1800)


def agent(name, *, idle=True, project="ameesh", harness="deepseek", **extra):
    row = {"name": name, "mode": "execute", "status": "idle", "lease_live": True,
           "lease_owner": "runner:h1", "unread": 0, "assigned_lot_id": None,
           "active_lots": 0, "actionable_lots": 0 if idle else 1,
           "projects": [project], "team": project, "harness": harness,
           "responsible": "human:alice",
           # #4779 : un relecteur prouve la capacité `propose` et un rôle.
           "capabilities": ["read", "report-drift", "propose"],
           "roles": ["implementation"]}
    row.update(extra)
    return row


def team(idle: int, busy: int, project="ameesh", prefix="dev", harness="deepseek"):
    return ([agent("%s%d" % (prefix, i), project=project, harness=harness)
             for i in range(idle)]
            + [agent("%sb%d" % (prefix, i), idle=False, project=project,
                     harness=harness) for i in range(busy)])


class SleepSparesTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name, harness in (("pilote", "deepseek"), ("author", "deepseek"),
                              ("dev0", "deepseek"), ("dev1", "deepseek"),
                              ("codex1", "codex"), ("codex2", "codex"),
                              ("codex3", "codex")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice',"
                        " team = 'ameesh'")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=["pilote"])
        pilots.start()
        self.addCleanup(pilots.stop)
        self.state: dict = {}
        self.t0 = time.time()

    def listing(self):
        return [agent("dev0"), agent("dev1"), agent("codex1", harness="codex"),
                agent("codex2", harness="codex"), agent("codex3", harness="codex")]

    def run_pass(self, offset, listing=None):
        return fd.run(None, self.db, now=self.t0 + offset, state=self.state,
                      listing=listing if listing is not None else self.listing(),
                      canons=[], settings=SETTINGS)

    def split_lot(self):
        rows = self.db.query("SELECT id FROM work_items WHERE source = %s",
                             ("file-decoupage:ameesh",))
        return int(rows[0]["id"])

    def make_split(self):
        self.run_pass(0)
        self.run_pass(1800)
        return self.split_lot()

    def notes(self, lot):
        return [r["note"] for r in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(lot),))]

    def qa_lot(self, *, assignee="author", reviewers=None, author_family="deepseek",
               author_rows=None, sha=None):
        """Un lot gelé en qa, avec sa marque de désignation (auteurs lisibles)."""
        sha = sha or "a" * 40
        lot = work.add(self.db, title="gel en attente", app="ameesh",
                       assignee=assignee, actor="author")
        lot_id = int(lot["id"])
        work.move(self.db, lot_id, "build", actor="author")
        freeze = work.milestone(self.db, lot_id, "frozen", sha=sha,
                                actor="author")
        if str(self.db.query("SELECT state FROM work_items WHERE id = %s",
                             (lot_id,))[0]["state"]) != "qa":
            work.move(self.db, lot_id, "qa", actor="author")
        payload = {
            "state": "waiting",
            "required": 1,
            "reviewers": [],
            "missing": 1,
            "why": "test freeze waiting for a reviewer",
            "authors": (author_rows if author_rows is not None
                        else [{"name": assignee, "family": author_family}]),
            "freeze": {"id": int(freeze["id"]), "sha": sha},
            "unknown_authors": [],
            "policy_ref": "test",
            "sent": [],
        }
        work.note(self.db, lot_id,
                  "review-designation:%d:%d: %s"
                  % (lot_id, int(freeze["id"]), json.dumps(payload)),
                  actor="ameesh")
        if reviewers:
            self.db.execute("UPDATE work_items SET reviewers = ARRAY[%s] WHERE id = %s",
                            (reviewers[0], lot_id))
            for name in reviewers:
                work.note(self.db, lot_id, "reviewer %s designated" % name,
                          actor="orchestrateur")
        return lot_id, int(freeze["id"])

    def test_a_designated_reviewer_is_spared_and_the_motive_is_journaled(self):
        split = self.make_split()
        lot, _freeze = self.qa_lot(reviewers=["codex3"])
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        out = self.run_pass(1900)
        sleep = next(g for g in out if g["kind"] == "sleep")
        by_agent = {row["agent"]: row for row in sleep["agents"]}
        self.assertFalse(by_agent["codex3"]["slept"])
        self.assertIn("designated review pending on #%d" % lot,
                      by_agent["codex3"]["why"])
        self.assertFalse(registry.get(self.db, "codex3")["operator_paused"])
        self.assertTrue(any(note.startswith("sleep exempted: codex3:")
                            and "designated review pending" in note
                            for note in self.notes(split)))

    def test_the_last_reviewer_of_a_family_is_spared_while_a_freeze_waits(self):
        split = self.make_split()
        lot, _freeze = self.qa_lot(author_family="deepseek")
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        out = self.run_pass(1900)
        sleep = next(g for g in out if g["kind"] == "sleep")
        by_agent = {row["agent"]: row for row in sleep["agents"]}
        # codex3 est le dernier nom de la famille codex : les deux autres
        # peuvent dormir, lui reste disponible pour le gel en attente.
        self.assertTrue(by_agent["codex1"]["slept"])
        self.assertTrue(by_agent["codex2"]["slept"])
        self.assertFalse(by_agent["codex3"]["slept"])
        self.assertIn("only eligible reviewer of family codex for pending freeze #%d"
                      % lot, by_agent["codex3"]["why"])
        self.assertFalse(registry.get(self.db, "codex3")["operator_paused"])
        # La famille de l'auteur (deepseek) n'est pas un relecteur éligible :
        # ses agents dorment, le sommeil n'épargne pas une famille interdite.
        self.assertTrue(by_agent["dev0"]["slept"])
        self.assertTrue(by_agent["dev1"]["slept"])
        for name in ("codex1", "codex2"):
            row = registry.get(self.db, name)
            self.assertTrue(row["operator_paused"])
            self.assertEqual(row["operator_pause_by"], fd.SLEEP_ACTOR)
            self.assertEqual(row["operator_pause_for"], "human:alice")
            self.assertEqual(row["operator_pause_reason"], fd.SLEEP_REASON % "ameesh")
        self.assertTrue(any(note.startswith("sleep exempted: codex3:")
                            and "only eligible reviewer of family codex" in note
                            for note in self.notes(split)))

    def test_two_pending_freezes_keep_a_reviewer_each(self):
        # B/P2 du second tour cx2 : deux gels excluent des noms différents de
        # la même famille ; garder un seul max global laisserait l'un des deux
        # gels sans relecteur.
        split = self.make_split()
        self.qa_lot(author_rows=[{"name": "codex3", "family": ""}], sha="a" * 40)
        self.qa_lot(author_rows=[{"name": "codex2", "family": ""}], sha="b" * 40)
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        out = self.run_pass(1900)
        sleep = next(g for g in out if g["kind"] == "sleep")
        by_agent = {row["agent"]: row for row in sleep["agents"]}
        # A (codex3 exclu) garde codex2 ; B (codex2 exclu) garde codex3.
        self.assertFalse(by_agent["codex2"]["slept"])
        self.assertFalse(by_agent["codex3"]["slept"])
        self.assertTrue(by_agent["codex1"]["slept"])

    def test_without_a_pending_freeze_every_reviewer_can_sleep(self):
        split = self.make_split()
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        out = self.run_pass(1900)
        sleep = next(g for g in out if g["kind"] == "sleep")
        self.assertEqual(sorted(row["agent"] for row in sleep["agents"] if row["slept"]),
                         ["codex1", "codex2", "codex3", "dev0", "dev1"])

    def test_a_light_freeze_without_review_keeps_no_family_awake(self):
        split = self.make_split()
        lot = work.add(self.db, title="lot léger gelé", app="ameesh",
                       assignee="author", actor="author")
        lot_id = int(lot["id"])
        work.move(self.db, lot_id, "build", actor="author")
        work.milestone(self.db, lot_id, "frozen", sha="b" * 40, actor="author")
        self.db.execute("UPDATE work_items SET reviewers = ARRAY['-none']"
                        " WHERE id = %s", (lot_id,))
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        out = self.run_pass(1900)
        sleep = next(g for g in out if g["kind"] == "sleep")
        self.assertEqual(sorted(row["agent"] for row in sleep["agents"] if row["slept"]),
                         ["codex1", "codex2", "codex3", "dev0", "dev1"])

    def test_the_project_pilot_is_not_the_family_representative(self):
        # B/P2 de la relecture cx2 : si le pilote est le plus grand nom de la
        # famille, il ne doit pas être retenu comme représentant (la
        # désignation l'exclut) ; c'est le vrai relecteur qui reste éveillé.
        registry.upsert(self.db, "zz-pilote", harness="codex", host=self.cfg.host,
                        cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice',"
                        " team = 'ameesh' WHERE name = 'zz-pilote'")
        split = self.make_split()
        self.qa_lot(author_family="deepseek")
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        listing = [agent("codex1", harness="codex"),
                   agent("zz-pilote", harness="codex"), agent("dev0")]
        with mock.patch("ameesh.alert_briefs.project_pilots",
                        return_value=["zz-pilote"]):
            out = self.run_pass(1900, listing)
        sleep = next(g for g in out if g["kind"] == "sleep")
        by_agent = {row["agent"]: row for row in sleep["agents"]}
        self.assertFalse(by_agent["codex1"]["slept"])
        self.assertTrue(by_agent["zz-pilote"]["slept"])
        self.assertFalse(registry.get(self.db, "codex1")["operator_paused"])
        self.assertTrue(registry.get(self.db, "zz-pilote")["operator_paused"])

    def test_an_unreadable_review_read_spares_everyone(self):
        split = self.make_split()
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        with mock.patch("ameesh.review_designation_activation.pending_reviews",
                        side_effect=RuntimeError("base illisible")):
            out = self.run_pass(1900)
        sleep = next(g for g in out if g["kind"] == "sleep")
        self.assertFalse(any(row["slept"] for row in sleep["agents"]))
        self.assertTrue(all(row["why"].startswith("review exemptions unreadable")
                            for row in sleep["agents"]))
        for name in ("dev0", "dev1", "codex1", "codex2", "codex3"):
            self.assertFalse(registry.get(self.db, name)["operator_paused"])

    def test_the_wake_uses_the_automatic_actor_and_the_human_principal(self):
        split = self.make_split()
        self.qa_lot(author_family="deepseek")
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        out = self.run_pass(1900)
        self.assertEqual([g["kind"] for g in out], ["sleep"])
        self.assertTrue(registry.get(self.db, "codex1")["operator_paused"])
        # un lot démarrable arrive : le premier passage réveille les dormeurs
        work.add(self.db, title="nouvelle étape", app="ameesh", actor="owner")
        asleep = [
            agent(name, harness=registry.get(self.db, name)["harness"],
                  operator_paused=True,
                  operator_pause_reason=fd.SLEEP_REASON % "ameesh",
                  operator_pause_by=fd.SLEEP_ACTOR,
                  operator_pause_for="human:alice",
                  operator_pause_ts=float(self.t0 + 1900))
            for name in ("codex1", "codex2", "dev0", "dev1")
        ]
        out = self.run_pass(2000, asleep + [agent("codex3", harness="codex")])
        woken = [g for g in out if g["kind"] == "wake"]
        self.assertEqual(woken[0]["agents"], ["codex1", "codex2", "dev0", "dev1"])
        for name in ("codex1", "codex2", "dev0", "dev1"):
            self.assertFalse(registry.get(self.db, name)["operator_paused"])
        lifted = self.db.query(
            "SELECT actor, principal FROM operator_pause_events"
            " WHERE agent = 'codex1' AND paused = false ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((lifted["actor"], lifted["principal"]),
                         (fd.SLEEP_ACTOR, "human:alice"))


class PauseActorTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "worker", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)

    def test_the_automatic_actor_is_admitted_only_with_a_human_principal(self):
        out = agent_pause.change(
            self.db, "worker", paused=True, reason=fd.SLEEP_REASON % "ameesh",
            actor=fd.SLEEP_ACTOR, principal="human:alice",
            until=time.time() + 600)
        self.assertTrue(out["changed"])
        row = registry.get(self.db, "worker")
        self.assertEqual((row["operator_pause_by"], row["operator_pause_for"]),
                         (fd.SLEEP_ACTOR, "human:alice"))
        for kw in ({"actor": "ameesh:other", "principal": "human:alice"},
                   {"actor": fd.SLEEP_ACTOR, "principal": fd.SLEEP_ACTOR},
                   {"actor": "agent:worker", "principal": "human:alice"}):
            with self.subTest(kw=kw), self.assertRaises(agent_pause.PauseRefused):
                agent_pause.change(self.db, "worker", paused=True, reason="maintenance",
                                   **kw)

    def test_a_capped_automatic_sleep_expires_and_keeps_the_audit_actor(self):
        agent_pause.change(
            self.db, "worker", paused=True, reason=fd.SLEEP_REASON % "ameesh",
            actor=fd.SLEEP_ACTOR, principal="human:alice",
            until=time.time() + 600)
        self.db.execute("UPDATE agent_registry SET operator_pause_until ="
                        " clock_timestamp() - interval '1 second' WHERE name = 'worker'")
        done = agent_pause.expire(self.db, "worker")
        self.assertEqual([row["agent"] for row in done], ["worker"])
        self.assertFalse(registry.get(self.db, "worker")["operator_paused"])
        lifted = self.db.query(
            "SELECT actor, principal, reason FROM operator_pause_events"
            " WHERE agent = 'worker' AND paused = false ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((lifted["actor"], lifted["principal"]),
                         (fd.SLEEP_ACTOR, "human:alice"))
        self.assertIn("deadline reached", lifted["reason"])

    def test_the_long_pause_alert_recognises_the_automatic_sleep(self):
        agent_pause.change(
            self.db, "worker", paused=True, reason=fd.SLEEP_REASON % "ameesh",
            actor=fd.SLEEP_ACTOR, principal="human:alice",
            until=time.time() + 7200)
        self.db.execute("UPDATE agent_registry SET operator_pause_at ="
                        " clock_timestamp() - interval '31 minutes' WHERE name = 'worker'")
        alerts = [a for a in exploitation.alerts(self.cfg, self.db)
                  if a["type"] == "operator_pause_long"]
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertTrue(alert.get("sleep"))
        self.assertNotIn("maintenance", alert["detail"])
        text = alert_briefs._operator_pause(alert)
        self.assertIn("Automatic sleep still in place", text)
        self.assertIn("lifted by the first startable lot", text)
        self.assertIn("pilot/orchestrator mail", text)
        self.assertNotIn("check with whoever set it", text)

    def test_the_normal_operator_pause_alert_keeps_its_maintenance_action(self):
        alert = {"type": "operator_pause_long", "agent": "worker",
                 "detail": "worker under operator pause by human:alice",
                 "reason": "maintenance", "by": "human:alice"}
        text = alert_briefs._operator_pause(alert)
        self.assertIn("Operator pause still in place", text)
        self.assertIn("check with whoever set it", text)


if __name__ == "__main__":
    unittest.main()

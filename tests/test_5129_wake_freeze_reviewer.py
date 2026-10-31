# SPDX-License-Identifier: AGPL-3.0-only
"""#5129 — un gel arrivé après le sommeil réveille le relecteur de la famille.

Constat du 31/10 : le sommeil « feuille de route épuisée » (#5122) avait
endormi toute la famille Codex quand #4971 a gelé ; la désignation a déclaré
la famille manquante alors que trois relecteurs étaient seulement endormis,
et le pilote a dû désigner à la main.

Témoins :

* un gel qui manque une famille dont des relecteurs éligibles dorment du
  sommeil roadmap lève le mieux classé et le désigne, sans geste du pilote ;
  la levée est journalisée sur le lot de découpage ;
* plusieurs endormis : un seul est réveillé ;
* une pause opérateur humaine laisse la famille manquante et le message
  inchangé (aucune levée, aucune note) ;
* une pause budgétaire n'est jamais levée par ce chemin.
"""
from __future__ import annotations

import importlib.util
import time
import unittest
from unittest import mock

from ameesh import agent_pause, file_decoupage as fd, registry, work
from ameesh import review_designation as rd
from ameesh import review_designation_activation as act
from ameesh.work_reviewers import designated

from .support import PgTestCase
from .test_2447_review_designation_activation import (
    _ActivationFixture, register_agent,
)

SLEEP_NOTE = "roadmap: épuisée ameesh (test)"
LIFT_MARK = "sleep lifted for review:"
HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None


class WakeMissingFamilyTest(_ActivationFixture, PgTestCase):
    def setUp(self):
        super().setUp()
        # Une deuxième voix codex : le témoin « plusieurs endormis » doit
        # vérifier qu'un seul est réveillé.
        register_agent(self.db, "r3", harness="codex", host=self.cfg.host,
                       cwd=self.repo.root, status="idle")
        self.db.execute(
            "UPDATE agent_registry SET team = 'ameesh', capabilities ="
            " ARRAY['read','report-drift','propose'], roles = ARRAY['implementation']"
            " WHERE name = 'r3'")
        # Le lot de découpage ouvert qui a posé le sommeil : la levée doit y
        # laisser sa trace.
        self.split = work.add(
            self.db, type="evolution", source=fd.source_of("ameesh"),
            title="Découper la feuille de route de ameesh", app="ameesh",
            actor="pilote")
        work.note(self.db, int(self.split["id"]), SLEEP_NOTE, actor="pilote")

    # -- helpers ----------------------------------------------------------
    def asleep(self, name, *, reason=None, actor=fd.SLEEP_ACTOR,
               principal="human:alice"):
        return agent_pause.change(
            self.db, name, paused=True,
            reason=reason or (fd.SLEEP_REASON % "ameesh"),
            actor=actor, principal=principal, until=time.time() + 3600)

    def split_notes(self):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(self.split["id"]),))]

    def lifts(self, name):
        return self.db.query(
            "SELECT actor, principal, reason FROM operator_pause_events"
            " WHERE agent = %s AND paused = false ORDER BY id", (name,))

    # -- witnesses --------------------------------------------------------
    def test_a_freeze_wakes_the_best_sleeper_of_the_missing_family(self):
        self.asleep("r2")
        self.asleep("r3")
        _mark, out = self.freeze()
        item = work.get(self.db, self.lot)
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(set(designated(item)), {"r1", "r2"})
        # Un seul des deux endormis est levé : le mieux classé.
        self.assertFalse(registry.get(self.db, "r2")["operator_paused"])
        self.assertTrue(registry.get(self.db, "r3")["operator_paused"])
        lifted = self.lifts("r2")
        self.assertEqual(len(lifted), 1)
        self.assertEqual(lifted[0]["actor"], fd.SLEEP_ACTOR)
        self.assertEqual(lifted[0]["principal"], "human:alice")
        self.assertIn("family codex", lifted[0]["reason"])
        # La levée est journalisée sur le lot de découpage, une fois.
        notes = [note for note in self.split_notes()
                 if note.startswith(LIFT_MARK)]
        self.assertEqual(len(notes), 1)
        self.assertIn("r2", notes[0])
        self.assertIn("codex", notes[0])
        # Le relecteur réveillé reçoit sa demande sur la tête gelée.
        self.assertEqual(
            [row["recipient"] for row in self.mailbox()
             if row["kind"] == "request"], ["r1", "r2"])
        # Aucun avis de capacité au pilote : la famille a été pourvue.
        self.assertFalse(any(row["recipient"] == "pilot" for row in self.mailbox()))

    def test_only_the_seats_the_selection_needs_are_woken(self):
        # Deux familles endormies, un seul siège manquant : la sélection à
        # blanc ne réveille que le relecteur qu'elle retient, pas l'autre
        # famille.
        self.four_families()
        register_agent(self.db, "r4", harness="gemini", host=self.cfg.host,
                       cwd=self.repo.root, status="idle")
        self.db.execute(
            "UPDATE agent_registry SET team = 'ameesh', capabilities ="
            " ARRAY['read','report-drift','propose'], roles = ARRAY['implementation']"
            " WHERE name = 'r4'")
        self.asleep("r2")
        self.asleep("r3")
        self.asleep("r4")
        _mark, out = self.freeze()
        item = work.get(self.db, self.lot)
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(set(designated(item)), {"r1", "r2"})
        self.assertFalse(registry.get(self.db, "r2")["operator_paused"])
        self.assertTrue(registry.get(self.db, "r3")["operator_paused"])
        self.assertTrue(registry.get(self.db, "r4")["operator_paused"])
        notes = [note for note in self.split_notes()
                 if note.startswith(LIFT_MARK)]
        self.assertEqual(len(notes), 1)
        self.assertIn("r2", notes[0])
        self.assertIn("codex", notes[0])

    def test_the_recovery_of_a_waiting_freeze_wakes_the_family(self):
        # Une attente écrite avant ce correctif (ou dont la levée a échoué) :
        # la reprise `ameesh work reviewers --apply` doit aussi réveiller.
        self.db.execute(
            "UPDATE agent_registry SET status = 'stopped'"
            " WHERE name IN ('r2', 'r3')")
        _mark, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING)
        self.assertEqual(set(designated(work.get(self.db, self.lot))), {"r1"})
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle'"
            " WHERE name IN ('r2', 'r3')")
        self.asleep("r2")
        self.asleep("r3")
        out = act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(set(designated(work.get(self.db, self.lot))), {"r1", "r2"})
        self.assertFalse(registry.get(self.db, "r2")["operator_paused"])
        self.assertTrue(registry.get(self.db, "r3")["operator_paused"])
        self.assertEqual(len([note for note in self.split_notes()
                              if note.startswith(LIFT_MARK)]), 1)

    def test_a_recovery_does_not_redesignate_a_removed_seat(self):
        # B1 cx3/auditeur sur e5ebbbcf9d4c : la reprise d'une attente ne doit
        # pas réinscrire un siège que le pilote a retiré, même quand la levée
        # d'un dormeur reconstruit une partie du vivier.
        self.db.execute(
            "UPDATE agent_registry SET status = 'stopped'"
            " WHERE name IN ('r2', 'r3')")
        _mark, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING)
        self.assertEqual(set(designated(work.get(self.db, self.lot))), {"r1"})
        # Le pilote retire r1 du gel ; r2 redevient disponible mais dort.
        self.db.execute(
            "UPDATE work_items SET reviewers = ARRAY[]::text[] WHERE id = %s",
            (self.lot,))
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle' WHERE name = 'r2'")
        self.asleep("r2")
        out = act.designate(self.cfg, self.db, self.lot)
        item = work.get(self.db, self.lot)
        self.assertEqual(out["state"], rd.WAITING)
        # r1 retiré n'est pas réinscrit ; r2 est levé et prend le siège.
        self.assertEqual(set(designated(item)), {"r2"})
        self.assertFalse(registry.get(self.db, "r2")["operator_paused"])
        self.assertEqual(self.lifts("r1"), [])
        self.assertTrue(any(note.startswith(LIFT_MARK)
                            for note in self.split_notes()))

    def test_a_human_pause_leaves_the_family_missing_and_the_message_unchanged(self):
        self.asleep("r2", actor="human:alice", principal="human:alice")
        self.asleep("r3", actor="human:alice", principal="human:alice")
        _mark, out = self.freeze()
        item = work.get(self.db, self.lot)
        self.assertEqual(out["state"], rd.WAITING)
        self.assertEqual(set(designated(item)), {"r1"})
        self.assertIn("1 independent review missing", out["why"])
        self.assertIn("1 eligible family (claude)", out["why"])
        self.assertTrue(registry.get(self.db, "r2")["operator_paused"])
        self.assertTrue(registry.get(self.db, "r3")["operator_paused"])
        self.assertEqual(self.lifts("r2"), [])
        self.assertFalse(any(note.startswith(LIFT_MARK)
                             for note in self.split_notes()))

    def test_a_budget_pause_is_never_lifted_by_this_path(self):
        self.asleep("r2")
        self.asleep("r3")
        # La pause budgétaire garde l'agent même une fois le sommeil levé :
        # ce chemin ne doit donc pas le réveiller pour rien.
        self.db.execute(
            "UPDATE agent_registry SET status = 'blocked',"
            " status_text = 'budget : gauge 100 %'"
            " WHERE name IN ('r2', 'r3')")
        _mark, out = self.freeze()
        item = work.get(self.db, self.lot)
        self.assertEqual(out["state"], rd.WAITING)
        self.assertEqual(set(designated(item)), {"r1"})
        self.assertTrue(registry.get(self.db, "r2")["operator_paused"])
        self.assertTrue(registry.get(self.db, "r3")["operator_paused"])
        self.assertEqual(self.lifts("r2"), [])
        self.assertEqual(self.lifts("r3"), [])
        self.assertFalse(any(note.startswith(LIFT_MARK)
                             for note in self.split_notes()))

    def test_a_wake_only_lifts_the_sleepers_of_the_missing_seats(self):
        # #5136 : la sélection à blanc ne doit classer que les sièges manquants.
        # r1 (claude, chargé) couvre déjà une famille ; sans reprendre son
        # siège, la prévision classe r5 (claude, sans charge) devant r2 (codex)
        # et lèverait aussi r4 (gemini), pour un seul siège manquant.
        self.four_families()
        for name, harness in (("r4", "gemini"), ("r5", "claude")):
            register_agent(self.db, name, harness=harness, host=self.cfg.host,
                           cwd=self.repo.root, status="idle")
            self.db.execute(
                "UPDATE agent_registry SET team = 'ameesh', capabilities ="
                " ARRAY['read','report-drift','propose'], roles = ARRAY['implementation']"
                " WHERE name = %s", (name,))
        work.add(self.db, title="charge de r1", app="ameesh",
                 assignee="r1", actor="pilote")
        self.asleep("r2")
        self.asleep("r3")
        self.asleep("r4")
        self.asleep("r5")
        _mark, out = self.freeze()
        item = work.get(self.db, self.lot)
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(set(designated(item)), {"r1", "r2"})
        self.assertFalse(registry.get(self.db, "r2")["operator_paused"])
        # Ni le second relecteur claude (famille déjà couverte) ni le relecteur
        # gemini (famille absente mais siège non manquant) ne sont levés.
        self.assertTrue(registry.get(self.db, "r4")["operator_paused"])
        self.assertTrue(registry.get(self.db, "r5")["operator_paused"])
        self.assertEqual(self.lifts("r4"), [])
        self.assertEqual(self.lifts("r5"), [])
        self.assertEqual(len([note for note in self.split_notes()
                              if note.startswith(LIFT_MARK)]), 1)

    @unittest.skipUnless(HAVE_PSYCOPG,
                         "psql ferme la session sur l'erreur SQL : rien à rattraper")
    def test_a_sql_failure_lifting_the_sleeper_does_not_abort_the_recovery(self):
        # #5136 : l'erreur SQL est avalée par fd.wake_reviewer, comme en
        # production ; sans point de retour, la transaction resterait avortée
        # et la reprise échouerait au lieu de désigner comme avant la levée.
        self.db.execute(
            "UPDATE agent_registry SET status = 'stopped'"
            " WHERE name IN ('r2', 'r3')")
        _mark, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING)
        self.assertEqual(set(designated(work.get(self.db, self.lot))), {"r1"})
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle'"
            " WHERE name IN ('r2', 'r3')")
        self.asleep("r2")
        self.asleep("r3")
        db = self.connect(driver="psycopg")
        self.addCleanup(db.close)
        original = agent_pause.change

        def broken_change(conn, *args, **kwargs):
            conn.execute("SELECT * FROM table_absente_5136")
            return original(conn, *args, **kwargs)

        with mock.patch.object(agent_pause, "change", broken_change):
            out = act.designate(self.cfg, db, self.lot)
        self.assertEqual(out["state"], rd.WAITING)
        self.assertEqual(set(designated(work.get(self.db, self.lot))), {"r1"})
        self.assertTrue(registry.get(self.db, "r2")["operator_paused"])
        self.assertTrue(registry.get(self.db, "r3")["operator_paused"])
        self.assertEqual(self.lifts("r2"), [])
        self.assertEqual(self.lifts("r3"), [])


if __name__ == "__main__":
    import unittest
    unittest.main()

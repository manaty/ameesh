# SPDX-License-Identifier: AGPL-3.0-only
"""L368: planned waits suppress stale alerts consistently across all views."""
from __future__ import annotations

import datetime
import os
import re
import time
import unittest
from unittest import mock

from ameesh import (mesh_cli, plan, planned_waits as W, progress, roadmap,
                    stagnation, storage, work, work_dependencies)
from ameesh import work_dependencies as D
from ameesh.storage import interface as SI
from ameesh.storage.postgres.progress import Progress
from tests.support import PgTestCase


class UnitTest(unittest.TestCase):
    now = plan.day_start_ts("2026-10-12") + 12 * 3600
    rows = [{"id": 1, "state": "build", "package_id": "child"}]
    packages = {"child": {"id": "child", "parent": "epic", "present": True},
                "epic": {"id": "epic", "present": True},
                "prerequisite": {"id": "prerequisite", "present": True}}

    def resolve(self, *, dates=None, commitments=(), items=(), rows=None, now=None,
                holds=None):
        return W.resolve(rows or self.rows, dates or {}, commitments, self.packages,
                         items, now=self.now if now is None else now, holds=holds)

    def test_future_start_ends_at_local_midnight_without_changing_activity(self):
        dates = {1: {"planned_start": "2026-10-13"}}
        self.assertEqual(self.resolve(dates=dates), {1: {"what": "scheduled", "start": "2026-10-13"}})
        self.assertEqual(self.resolve(dates=dates, now=plan.day_start_ts("2026-10-13")), {})
        for start in (None, "", "invalid", "2026-10-12", "2026-10-11"):
            self.assertEqual(self.resolve(dates={1: {"planned_start": start}}), {})

    def test_only_explicit_open_lots_block(self):
        for reference in ("#2", "2", "lot 2", "lot #2", "task #2", "TASK 2"):
            with self.subTest(reference=reference):
                commitment = {"status": "open", "work_item_id": 1, "depends_on": [reference]}
                self.assertEqual(self.resolve(commitments=[commitment], items=[{"id": 2, "state": "qa"}]),
                                 {1: {"what": "dependency", "lots": [2]}})
                for state in ("merged", "promoted", "closed"):
                    self.assertEqual(self.resolve(commitments=[commitment], items=[{"id": 2, "state": state}]), {})
        for reference in ("after #2 merges", "main carries abc123", "#999", "#1", "e2", None):
            self.assertEqual(self.resolve(commitments=[{"status": "open", "work_item_id": 1,
                                                       "depends_on": [reference]}],
                                          items=[{"id": 1, "state": "build"}, {"id": 2, "state": "qa"}]), {})

    def test_proposals_cancelled_and_unrelated_commitments_do_not_suppress(self):
        for status, lot in (("proposed", 1), ("cancelled", 1), ("done", 1), ("open", 3)):
            self.assertEqual(self.resolve(commitments=[{"status": status, "work_item_id": lot,
                                                       "depends_on": ["#2"]}],
                                          items=[{"id": 2, "state": "build"}]), {})

    def test_plan_dependency_inherits_through_ancestor_and_resolves_package(self):
        c = {"status": "open", "package_id": "epic", "depends_on": ["prerequisite"]}
        items = [{"id": 2, "state": "build", "package_id": "prerequisite"},
                 {"id": 3, "state": "merged", "package_id": "prerequisite"}]
        self.assertEqual(self.resolve(commitments=[c], items=items), {1: {"what": "dependency", "lots": [2]}})
        for reference in ("lot prerequisite", "package prerequisite", "WorkPackage prerequisite"):
            self.assertEqual(self.resolve(commitments=[dict(c, depends_on=[reference])], items=items),
                             {1: {"what": "dependency", "lots": [2]}})
        self.assertEqual(self.resolve(commitments=[c], items=[]), {})
        self.assertEqual(self.resolve(commitments=[c], items=items,
                                      rows=[{"id": 4, "state": "build", "package_id": "other"}]), {})
        packages = {"a": {"parent": "b"}, "b": {"parent": "a"}}
        self.assertEqual(list(W.ancestors("a", packages)), ["a", "b"])
        self.assertEqual(list(W.ancestors("retired", {"retired": {"present": False}})), [])

    def test_plan_start_inherits_and_explicit_lot_start_overrides_it(self):
        packages = {k: dict(v) for k, v in self.packages.items()}
        packages["epic"]["planned_start"] = "2026-10-13"
        self.assertEqual(W.resolve(self.rows, {}, [], packages, [], now=self.now),
            {1: {"what": "scheduled", "start": "2026-10-13", "package": "epic"}})
        self.assertEqual(W.resolve(self.rows, {1: {"planned_start": "2026-10-12"}}, [],
                                    packages, [], now=self.now), {})

    def test_external_gate_holds_until_the_end_of_the_due_day(self):
        commitment = {"id": 7, "status": "open", "work_item_id": 1, "due_on": "2026-10-13",
                      "depends_on": ["gate:liquichain-window"]}
        expected = {1: {"what": "hold", "gate": "liquichain-window", "until": "2026-10-13",
                        "commitment": 7}}
        self.assertEqual(self.resolve(commitments=[commitment]), expected)
        for alias in ("external:liquichain-window", "GATE:liquichain-window"):
            self.assertEqual(self.resolve(commitments=[dict(commitment, depends_on=[alias])]),
                             expected)
        end = plan.day_start_ts("2026-10-14")
        self.assertEqual(self.resolve(commitments=[commitment], now=end - 1), expected)
        self.assertEqual(self.resolve(commitments=[commitment], now=end), {})

    def test_external_gate_on_a_package_holds_its_descendants(self):
        commitment = {"id": 8, "status": "open", "package_id": "epic", "due_on": "2026-10-13",
                      "depends_on": ["gate:epic-window"]}
        self.assertEqual(self.resolve(commitments=[commitment]),
                         {1: {"what": "hold", "gate": "epic-window", "until": "2026-10-13",
                              "commitment": 8}})
        rows = [{"id": 4, "state": "build", "package_id": "other"}]
        self.assertEqual(self.resolve(commitments=[commitment], rows=rows), {})

    def test_unrepresentable_hold_end_does_not_break_other_waits(self):
        rows = [{"id": 1, "state": "build"}, {"id": 2, "state": "build"}]
        commitments = [
            {"status": "open", "work_item_id": 1, "due_on": "9999-12-31",
             "depends_on": ["gate:out-of-range"]},
            {"status": "open", "work_item_id": 2, "due_on": "2026-10-13",
             "depends_on": ["gate:valid"]},
        ]
        self.assertEqual(self.resolve(rows=rows, commitments=commitments),
                         {2: {"what": "hold", "gate": "valid", "until": "2026-10-13"}})

    def test_malformed_finished_or_undated_gate_does_not_suppress(self):
        base = {"id": 7, "status": "open", "work_item_id": 1, "due_on": "2026-10-13"}
        for reference in ("gate:", "gate", "external", "gate liquichain", "hold:liquichain",
                          "after the window", None):
            with self.subTest(reference=reference):
                self.assertEqual(self.resolve(commitments=[dict(base, depends_on=[reference])]), {})
        for status in ("proposed", "done", "cancelled"):
            self.assertEqual(self.resolve(commitments=[dict(base, status=status,
                                                            depends_on=["gate:x"])]), {})
        for due in (None, "", "someday"):
            self.assertEqual(self.resolve(commitments=[dict(base, due_on=due,
                                                            depends_on=["gate:x"])]), {})
        self.assertEqual(self.resolve(commitments=[dict(base, due_on="2026-10-11",
                                                        depends_on=["gate:x"])]), {})
        # a lot dependency stays a dependency, and wins over a gate on the same lot
        both = dict(base, depends_on=["gate:x", "#2"])
        self.assertEqual(self.resolve(commitments=[both], items=[{"id": 2, "state": "build"}]),
                         {1: {"what": "dependency", "lots": [2]}})


    def test_explicit_hold_is_a_planned_wait_until_lifted_or_moved(self):
        """#1664 : `hold:` tient le lot (alerte stale éteinte) ; une note
        ordinaire (triage, relevé) ne le lève pas, seuls une levée explicite
        (`unhold:`…) ou un changement d'état le font."""
        def event(note, state="build", actor="mesh-design", ts=1.0, ident=1):
            return {"work_item_id": 1, "state": state, "note": note,
                    "actor": actor, "created_ts": ts, "id": ident}

        hold = W.explicit_holds([event("created", state="intake", ts=0.5, ident=1),
                                 event("hold: fenêtre opérateur", ts=1.0, ident=2)])
        self.assertEqual(hold, {1: {"what": "hold", "by": "mesh-design", "since": 1.0}})
        self.assertEqual(self.resolve(holds=hold),
                         {1: {"what": "hold", "by": "mesh-design", "since": 1.0}})
        # une note ordinaire ne lève pas la retenue
        self.assertEqual(W.explicit_holds([event("hold: op", ident=2),
                                           event("triage: relevé", ts=2.0, ident=3)]),
                         {1: {"what": "hold", "by": "mesh-design", "since": 1.0}})
        # une levée explicite la lève
        for lift in ("unhold: feu reçu", "hold lifted: feu reçu",
                     "hold released: feu reçu", "hold cleared: feu reçu"):
            with self.subTest(lift=lift):
                self.assertEqual(W.explicit_holds([event("hold: op", ident=2),
                                                   event(lift, ts=2.0, ident=3)]), {})
        # un changement d'état lève
        self.assertEqual(W.explicit_holds([event("hold: op", state="qa", ident=2),
                                           event("qa → build", state="build", ts=2.0,
                                                 ident=3)]), {})
        # une note vide ne compte pas ; un hold reposé après du bruit tient
        again = W.explicit_holds([event("hold: op", ident=2),
                                  event("triage: relevé", ts=2.0, ident=3),
                                  event("", ts=2.5, ident=4),
                                  event("hold: retour opérateur", ts=3.0, ident=5)])
        self.assertEqual(again[1]["since"], 3.0)
        # l'ordre est celui de l'écriture (created_ts, id), pas celui de la liste
        self.assertEqual(W.explicit_holds([event("hold: op", ts=2.0, ident=3),
                                           event("triage", ts=1.0, ident=2)]),
                         {1: {"what": "hold", "by": "mesh-design", "since": 2.0}})
        self.assertEqual(W.explicit_holds([event("triage", ts=2.0, ident=3),
                                           event("hold: op", ts=1.0, ident=2)]),
                         {1: {"what": "hold", "by": "mesh-design", "since": 1.0}})

    def test_only_the_allowed_author_holds_and_lifts(self):
        """#1664 : `allowed` restreint le hold à un pilote — le hold d'un autre
        acteur ne compte pas, et lui seul peut le lever."""
        def event(note, actor, ts, ident):
            return {"work_item_id": 1, "state": "build", "note": note,
                    "actor": actor, "created_ts": ts, "id": ident}

        self.assertEqual(W.explicit_holds([event("hold: op", "dev7", 1.0, 2)],
                                          allowed={"mesh-design"}), {})
        self.assertEqual(W.explicit_holds([event("hold: op", "dev7", 1.0, 2)],
                                          allowed={"dev7"})[1]["by"], "dev7")
        # la note et la levée d'un autre acteur ne comptent pas
        events = [event("hold: op", "mesh-design", 1.0, 2),
                  event("triage: relevé", "dev7", 2.0, 3),
                  event("unhold: à tort", "dev7", 3.0, 4)]
        self.assertEqual(W.explicit_holds(events, allowed={"mesh-design"})[1]["since"], 1.0)
        # la levée du pilote compte
        self.assertEqual(W.explicit_holds(
            events + [event("unhold: feu reçu", "mesh-design", 4.0, 5)],
            allowed={"mesh-design"}), {})
        # un prédicat d'autorisation est accepté
        self.assertEqual(W.explicit_holds([event("hold: op", "mesh-design", 1.0, 2)],
                                          allowed=lambda who: who == "mesh-design")[1]["by"],
                         "mesh-design")

    def test_hold_outranks_a_declared_dependency_and_its_label(self):
        hold = {1: {"what": "hold", "by": "pilote", "since": 1700000000.0}}
        commitment = {"status": "open", "work_item_id": 1, "depends_on": ["#2"]}
        self.assertEqual(self.resolve(commitments=[commitment],
                                      items=[{"id": 2, "state": "qa"}], holds=hold), hold)
        label = D.wait_label(hold[1])
        self.assertTrue(label.startswith("hold by pilote since "), label)
        self.assertIsNone(D.wait_label(None))

    def test_hold_patterns_cover_the_exact_notes(self):
        """#1664 B2 : les motifs donnés au stockage couvrent toutes les notes
        que `is_hold_note`/`is_lift_note` acceptent, et aucune note ordinaire —
        le filtre SQL ne peut ni manquer une retenue ni transférer un texte
        ordinaire."""
        acceptees = ("hold: fenêtre", "HOLD: fenêtre",
                     "unhold: feu", "UNHOLD: feu", "unhold : feu",
                     "hold lifted: feu", "hold released: feu", "hold cleared: feu",
                     "hold   lifted : feu")
        for text in acceptees:
            with self.subTest(text=text):
                self.assertTrue(W.is_hold_note(text) or W.is_lift_note(text))
                self.assertTrue(any(re.match(p, text.strip(), re.I)
                                    for p in W.HOLD_NOTE_PATTERNS))
        for text in ("triage: relevé", "unholding the gate", "holding pattern",
                     "hold-up: staging", "threshold reached", ""):
            with self.subTest(text=text):
                self.assertFalse(W.is_hold_note(text) or W.is_lift_note(text))
                self.assertFalse(any(re.match(p, text.strip(), re.I)
                                     for p in W.HOLD_NOTE_PATTERNS))

    def test_targeted_stream_holds_like_the_full_journal(self):
        """#1664 B2 : le flux ciblé du stockage (transitions + notes hold/levée)
        donne le même hold que le journal complet, sans le texte des notes
        ordinaires ; une note portée par une transition est rendue avec elle."""
        class Journal:
            def __init__(self, events):
                self.events = list(events)

            def lot_events(self, ids):
                gardes = {int(i) for i in ids}
                return [e for e in self.events if int(e["work_item_id"]) in gardes]

        def row(ident, state, note="", actor="dev7"):
            return {"id": ident, "work_item_id": 1, "state": state, "note": note,
                    "actor": actor, "created_ts": float(ident)}

        bruit = "triage: " + "x" * 5000
        proche = "holding pattern: bruit ordinaire"
        journal = [row(1, "intake", "created"),
                   row(2, "build", "prise"),
                   row(3, "build", bruit),
                   row(4, "build", proche),
                   row(5, "build", "hold: fenêtre opérateur", actor="human:pilote"),
                   row(6, "build", bruit),
                   row(7, "qa", "hold: porte portée par la transition",
                       actor="human:pilote"),
                   row(8, "qa", bruit)]
        flux = SI.Progress.lot_hold_events(Journal(journal), [1], W.HOLD_NOTE_PATTERNS)
        notes = [str(e.get("note") or "") for e in flux]
        self.assertNotIn(bruit, notes)
        self.assertNotIn(proche, notes)     # même un mot proche ne sort pas
        self.assertIn("hold: fenêtre opérateur", notes)
        # la note portée par la transition est rendue avec la transition
        porte = next(e for e in flux if e["id"] == 7)
        self.assertEqual(porte["state"], "qa")
        self.assertEqual(porte["note"], "hold: porte portée par la transition")
        # le flux ciblé et le journal complet donnent le même hold
        autorises = {"human:pilote"}
        self.assertEqual(W.explicit_holds(journal, allowed=autorises),
                         W.explicit_holds(flux, allowed=autorises))
        self.assertEqual(W.explicit_holds(flux, allowed=autorises)[1]["since"], 7.0)
        # sans motif, le flux ne porte que les transitions, sans note
        transitions = SI.Progress.lot_hold_events(Journal(journal), [1], ())
        self.assertEqual([e["id"] for e in transitions], [1, 2, 7])
        self.assertTrue(all("note" not in e for e in transitions))
        # un motif vide est refusé : il ferait sortir toutes les notes
        with self.assertRaises(ValueError):
            SI.Progress.lot_hold_events(Journal(journal), [1],
                                        (W.HOLD_NOTE_PATTERNS[0], ""))


class PgTest(PgTestCase):
    #: #1664 r2 : l'identité du lanceur ne décide pas du test.
    IDENTITY_ENV = ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                    "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH",
                    "AMEESH_ALERT_ORCHESTRATORS")

    def setUp(self):
        self.db.execute("TRUNCATE work_items, commitments, work_packages CASCADE")
        self.now = time.time()

    def unbound(self, **extra):
        """Environnement d'identité neutre pour ce test, restauré à la fin."""
        saved = {key: os.environ.get(key) for key in self.IDENTITY_ENV}
        for key in self.IDENTITY_ENV:
            os.environ.pop(key, None)
        os.environ.update(extra)

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def old(self, item):
        for sql in ("UPDATE work_items SET updated_at = to_timestamp(%s), created_at = to_timestamp(%s) WHERE id = %s",
                    "UPDATE work_item_events SET created_at = to_timestamp(%s) WHERE work_item_id = %s",
                    "UPDATE work_item_milestones SET at = to_timestamp(%s) WHERE work_item_id = %s"):
            args = (self.now - 10 * 3600, self.now - 11 * 3600, item["id"]) if "created_at = to_timestamp(%s) WHERE id" in sql else (self.now - 10 * 3600, item["id"])
            self.db.execute(sql, args)

    def item(self, title):
        item = work.add(self.db, title=title, app="acme")
        self.old(item)
        return item

    def test_planned_start_in_alert_work_and_progress_and_limit_after_filter(self):
        future = self.item("planned tomorrow")
        actionable = self.item("unexplained wait")
        tomorrow = (datetime.date.fromtimestamp(self.now) + datetime.timedelta(days=1)).isoformat()
        storage.of(self.db).roadmap.plan_item(future["id"], {"start": tomorrow}, source="test", actor="test")
        alerts = stagnation.stagnant_lots(self.db, now=self.now, limit=1)
        self.assertEqual([a["id"] for a in alerts], [actionable["id"]])
        annotated = plan.annotate(self.db, [work.get(self.db, future["id"])], now=self.now)[0]
        self.assertIsNone(annotated["stale"])
        self.assertEqual(annotated["planned_wait"]["start"], tomorrow)
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, gantt=False)
        lots = {r["id"]: r for r in snap["lots"]}
        self.assertIsNone(lots[future["id"]]["stale"])
        self.assertIsNotNone(lots[actionable["id"]]["stale"])
        after_start = stagnation.stagnant_lots(self.db, now=plan.day_start_ts(tomorrow))
        self.assertIn(future["id"], [r["id"] for r in after_start])

    def test_open_blocker_beyond_render_limit_and_release_when_closed(self):
        waiting = self.item("wait for a real lot")
        blocker = work.add(self.db, title="blocker", app="other-project")
        roadmap.add_commitment(self.db, "release", due="2999-01-01", lot=waiting["id"],
                               depends_on=["task #%d" % blocker["id"]])
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now, limit=1), [])
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        self.assertEqual(row["planned_wait"]["lots"], [blocker["id"]])
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, max_lots=1, gantt=False)
        self.assertIsNone(snap["lots"][0]["stale"])
        work.close(self.db, blocker["id"], abandoned=True)
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)], [waiting["id"]])

    def test_cancelled_commitment_or_prose_does_not_mask_stagnation(self):
        waiting = self.item("wait in prose only")
        blocker = work.add(self.db, title="blocker")
        commitment = roadmap.add_commitment(self.db, "release", due="2999-01-01", lot=waiting["id"],
                                             depends_on=["after #%d merges" % blocker["id"]])
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)], [waiting["id"]])
        storage.of(self.db).roadmap.set_commitment(commitment["id"],
            values={"depends_on": ["#%d" % blocker["id"]], "status": "cancelled"}, current=["open"])
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)], [waiting["id"]])

    def test_package_plan_dependency_is_read_from_storage(self):
        st = storage.of(self.db)
        for ident, kind, parent in (("epic", "epic", None), ("child", "lot", "epic"),
                                    ("prerequisite", "lot", None)):
            st.packages.upsert({"id": ident, "kind": kind, "title": ident, "parent": parent,
                                "canon_ref": "test", "present": True})
        waiting = work.add(self.db, title="waiting", app="acme", package="child")
        self.old(waiting)
        blocker = work.add(self.db, title="blocker", app="other", package="prerequisite")
        roadmap.add_commitment(self.db, "epic release", due="2999-01-01", package="epic",
                               depends_on=["prerequisite"])
        rows = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)
        self.assertEqual(rows[0]["planned_wait"]["lots"], [blocker["id"]])
        self.assertIsNone(rows[0]["stale"])

    def test_external_hold_defers_stagnation_and_ends_at_its_due_day(self):
        waiting = self.item("blocked by an external gate")
        due = (datetime.date.fromtimestamp(self.now)
               + datetime.timedelta(days=1)).isoformat()
        commitment = roadmap.add_commitment(
            self.db, "hold until the liquichain window", due=due, lot=waiting["id"],
            depends_on=["gate:liquichain-window"])
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        self.assertEqual(row["planned_wait"],
                         {"what": "hold", "gate": "liquichain-window", "until": due,
                          "commitment": commitment["id"]})
        self.assertIsNone(row["stale"])
        self.assertEqual(work_dependencies.wait_label(row["planned_wait"]),
                         "external hold until %s (liquichain-window)" % due)
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, gantt=False)
        self.assertIsNone(next(r for r in snap["lots"] if r["id"] == waiting["id"])["stale"])
        # the due day counts in full: the ordinary rule applies again the next day
        after = plan.day_start_ts(
            (datetime.date.fromtimestamp(self.now) + datetime.timedelta(days=2)).isoformat())
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=after)],
                         [waiting["id"]])
        # cancelling the commitment releases the lot immediately
        storage.of(self.db).roadmap.set_commitment(
            commitment["id"], values={"status": "cancelled"}, current=["open"])
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)],
                         [waiting["id"]])

    def test_hold_note_suppresses_stagnation_until_lifted_or_moved(self):
        """#1664 : une note `hold:` d'un pilote éteint la relance 6 h et ses vues
        (work, progression) ; une note ordinaire ne la lève pas, seuls une levée
        explicite ou un changement d'état le font ; le hold d'un acteur ordinaire
        (règle 1) ne compte pas."""
        self.unbound()
        lot = self.item("parked on an operator window")
        work.note(self.db, lot["id"], "hold: fenêtre opérateur + accès",
                  actor="human:pilote")
        self.old(lot)
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        row = plan.annotate(self.db, [work.get(self.db, lot["id"])], now=self.now)[0]
        self.assertEqual(row["planned_wait"]["what"], "hold")
        self.assertEqual(row["planned_wait"]["by"], "human:pilote")
        self.assertIsNone(row["stale"])
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, gantt=False)
        held = {r["id"]: r for r in snap["lots"]}[lot["id"]]
        self.assertEqual(held["planned_wait"]["what"], "hold")
        self.assertIsNone(held["stale"])
        # une note ordinaire ne lève pas la retenue
        work.note(self.db, lot["id"], "triage: relevé de passage", actor="dev7")
        self.old(lot)
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        # le hold d'un acteur ordinaire (règle 1) ne compte pas pour la stagnation
        other = self.item("hold sans autorité")
        work.note(self.db, other["id"], "hold: décision d'équipe", actor="dev7")
        self.old(other)
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)],
                         [other["id"]])
        # une levée explicite du pilote rallume la relance
        work.note(self.db, lot["id"], "unhold: fenêtre ouverte", actor="human:pilote")
        self.old(lot)
        self.assertIn(lot["id"],
                      [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        # un hold reposé tient, et un changement d'état le lève
        work.note(self.db, lot["id"], "hold: re-park", actor="human:pilote")
        self.old(lot)
        self.assertNotIn(lot["id"],
                         [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        work.move(self.db, lot["id"], "build", actor="human:pilote")
        self.old(lot)
        self.assertIn(lot["id"],
                      [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])

    def test_hold_note_authenticates_the_claimed_pilot(self):
        """#1664 r2 (B1) : une session ordinaire ne pose ni ne lève un hold de
        pilote en donnant `--actor human:…` ; sa propre note `hold:` (règle 1)
        reste permise et n'éteint pas la stagnation."""
        lot = self.item("porte pilote")
        other = self.item("faux hold de pilote")
        # un terminal humain sans liaison dit qui il est : le hold de pilote passe
        self.unbound()
        work.note(self.db, lot["id"], "hold: porte opérateur", actor="human:pilote")
        self.old(lot)
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)],
                         [other["id"]])
        # une session liée ordinaire ne se dit pas pilote…
        self.unbound(AGENT_MAIL_NAME="dev7")
        with self.assertRaisesRegex(work.WorkError, "belongs to dev7"):
            work.note(self.db, other["id"], "hold: faux pilote", actor="human:pilote")
        with self.assertRaisesRegex(work.WorkError, "belongs to dev7"):
            work.move(self.db, other["id"], "build", note="hold: faux pilote",
                      actor="human:pilote")
        # …ni lever le hold du pilote, même en son propre nom
        with self.assertRaisesRegex(work.WorkError, "neither a human"):
            work.note(self.db, lot["id"], "unhold: à tort", actor="dev7")
        # sa propre note `hold:` (règle 1) passe, mais la stagnation reste due
        work.note(self.db, other["id"], "hold: je ne peux pas avancer", actor="dev7")
        self.old(other)
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)],
                         [other["id"]])
        self.old(lot)
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)],
                         [other["id"]])

    def test_cli_refuses_a_pilot_hold_from_an_ordinary_session(self):
        """#1664 r2 (B1) : le chemin réel — `work.resolve_actor` rend `--actor`
        explicite avant tout contrôle, et la garde d'écriture refuse la session
        ordinaire ; un terminal humain sans liaison dit qui il est, et le pilote
        lié pose son hold en son nom."""
        lot = self.item("porte pilote")
        spoof = self.mesh("work", "note", str(lot["id"]), "hold: faux pilote",
                          "--actor", "human:pilote", env=self.env(AGENT_MAIL_NAME="dev7"))
        self.assertNotEqual(spoof.returncode, 0, spoof.stdout)
        self.assertIn("refused", spoof.stderr)
        # un humain à son terminal : aucune liaison, il dit qui il est
        ok = self.mesh("work", "note", str(lot["id"]), "hold: fenêtre opérateur",
                       "--actor", "human:pilote", env=self.env())
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.old(lot)
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        # le pilote lié (rôle déclaré) pose son hold en son nom
        other = self.item("porte pilote liée")
        self.unbound(AGENT_MAIL_NAME="mesh-design",
                     AMEESH_ALERT_ORCHESTRATORS="mesh-design")
        work.note(self.db, other["id"], "hold: fenêtre opérateur", actor="mesh-design")
        self.old(other)
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])

    def test_hold_carried_by_a_transition_suppresses_then_the_next_move_lifts(self):
        """#1664 B2 : un hold porté par une transition (`work move --note`) est
        lu du flux ciblé ; la transition suivante le lève, comme le journal
        complet."""
        self.unbound()
        lot = self.item("porte portée par une transition")
        work.move(self.db, lot["id"], "build", note="hold: fenêtre opérateur",
                  actor="human:pilote")
        self.old(lot)
        described = stagnation.describe(self.db, [work.get(self.db, lot["id"])],
                                        now=self.now)[lot["id"]]
        self.assertEqual(described["planned_wait"]["what"], "hold")
        self.assertIsNone(described["stale"])
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now,
                                 gantt=False)
        held = {r["id"]: r for r in snap["lots"]}[lot["id"]]
        self.assertEqual(held["planned_wait"]["what"], "hold")
        self.assertIsNone(held["stale"])
        # la transition suivante lève la retenue (build → blocked)
        work.move(self.db, lot["id"], "blocked", actor="human:pilote")
        self.old(lot)
        self.assertIn(lot["id"],
                      [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        # la note de transition est stockée telle qu'authentifiée, blancs ôtés :
        # un préfixe Unicode ne peut pas échapper au filtre littéral du pilote SQL
        espace = self.item("porte sur transition espacée")
        work.move(self.db, espace["id"], "build", note="\u00a0hold: fenêtre opérateur ",
                  actor="human:pilote")
        self.old(espace)
        stockee = next(e for e in work.events(self.db, espace["id"])
                       if e["state"] == "build")["note"]
        self.assertEqual(stockee, "hold: fenêtre opérateur")
        self.assertNotIn(espace["id"],
                         [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])

    def test_grouped_views_read_the_targeted_stream_never_the_journal(self):
        """#1664 B2 : avec un hold posé, les vues groupées (alerte, work list,
        progrès) et le repli `planned_waits.read` lisent le flux ciblé du
        stockage — jamais le journal complet (contrat #1647 conservé)."""
        self.unbound()
        lot = self.item("porte relue sans journal")
        work.note(self.db, lot["id"], "hold: fenêtre opérateur", actor="human:pilote")
        work.note(self.db, lot["id"], "triage: relevé de passage", actor="dev7")
        self.old(lot)

        def interdit(*_args, **_kwargs):
            raise AssertionError(
                "le journal complet est relu par une vue groupée (#1647/#1664)")

        with mock.patch.object(Progress, "lot_events", interdit):
            alertes = stagnation.stagnant_lots(self.db, now=self.now)
            rows = mesh_cli.work_rows(self.db, limit=50)
            snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now,
                                     gantt=False)
            waits = W.read(self.db, [work.get(self.db, lot["id"])], now=self.now,
                           open_items=[])
        self.assertEqual(alertes, [])
        ligne = next(r for r in rows if int(r["id"]) == lot["id"])
        self.assertEqual(ligne["planned_wait"]["what"], "hold")
        self.assertIsNone(ligne["stale"])
        vue = next(l for l in snap["lots"] if int(l["id"]) == lot["id"])
        self.assertEqual(vue["planned_wait"]["what"], "hold")
        self.assertIsNone(vue["stale"])
        self.assertEqual(waits[lot["id"]]["what"], "hold")

    def test_targeted_stream_keeps_unicode_holds_and_lifts(self):
        """Le filtre SQL garde les levées Unicode reconnues par le métier,
        y compris les blancs de bord des notes de transition historiques."""
        lot = self.item("porte Unicode")
        # Déclarés dans le témoin, sans reprendre la constante de production.
        spaces = "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680" + "".join(
            chr(c) for c in range(0x2000, 0x200b)) + "\u2028\u2029\u202f\u205f\u3000"
        notes = [text for s in spaces for text in
                 (s + "hold: porte" + s, "unhold" + s + ": feu",
                  "hold" + s + "lifted: feu")]
        notes += ["hold lİfted: feu", "hold lıfted: feu", "hold releaſed: feu"]
        self.assertTrue(all(W.is_hold_note(n) or W.is_lift_note(n) for n in notes))
        bruit = "triage: " + "x" * 5000
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " SELECT %s, 'intake', v.note, 'human:pilote' FROM (VALUES "
            + ",".join(["(%s)"] * (len(notes) + 1)) + ") v(note)",
            (lot["id"], *notes, bruit))
        st = storage.of(self.db).progress
        journal = st.lot_events([lot["id"]])
        flux = st.lot_hold_events([lot["id"]], W.HOLD_NOTE_PATTERNS)
        self.assertEqual([e["note"] for e in flux if "note" in e], notes)
        faux = mock.Mock(lot_events=mock.Mock(return_value=journal))
        self.assertEqual(flux, SI.Progress.lot_hold_events(
            faux, [lot["id"]], W.HOLD_NOTE_PATTERNS))
        self.assertEqual(W.explicit_holds(journal), W.explicit_holds(flux))

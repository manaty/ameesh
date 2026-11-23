# SPDX-License-Identifier: AGPL-3.0-only
"""#8119 — journal des refus de la prise automatique (L165), échéance d'une
heure, sommeil du découpage qui tient compte des paniers, et relation
non bloquante `--see-also` de `work add --parent-lot`.

Les témoins couvrent les motifs nommés par le constat du 22/11 : prérequis
bloquant (#8107), empreinte, agents endormis, panier derrière un lot de
roadmap ; puis le journal (une note par heure, une alerte par motif), la
levée du sommeil à l'échéance et le lien `see_also`.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from ameesh import (agent_pause, backlog, file_decoupage as fd, registry,
                    storage, take_refusals, work, work_dependencies)

from .support import PgTestCase

HEURE = 3600.0


class Base(PgTestCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()

    def agent(self, name, *, project="ameesh", paused=False, capabilities=None):
        registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle', team = %s,"
            " lease_owner = %s, lease_expires_at = now() + interval '1 hour',"
            " responsible = 'human:proprio', capabilities = %s::text[],"
            " roles = ARRAY['implementation']::text[],"
            " status_since = now() - interval '2 hours',"
            " last_turn_at = now() - interval '2 hours'"
            " WHERE name = %s",
            (project, "runner-%s@h" % name,
             "{" + ",".join(capabilities or ("read", "report-drift", "propose")) + "}",
             name))
        if paused:
            self.db.execute(
                "UPDATE agent_registry SET operator_paused = true,"
                " operator_pause_reason = %s, operator_pause_by = %s,"
                " operator_pause_for = 'human:proprio' WHERE name = %s",
                (fd.SLEEP_REASON % project, fd.SLEEP_ACTOR, name))

    def lot(self, title, *, app="ameesh", age_s=2 * HEURE, **kw):
        item = work.add(self.db, title=title, app=app, actor="test", **kw)
        self.db.execute(
            "UPDATE work_items SET created_at = now() - make_interval(secs => %s)"
            " WHERE id = %s", (float(age_s), int(item["id"])))
        return work.get(self.db, item["id"])

    def row(self, rows, ident):
        return next(row for row in rows if row["id"] == int(ident))


class ExplainTest(Base):
    def test_an_open_prerequisite_is_named(self):
        parent = self.lot("parent")
        suite = self.lot("suite")
        work_dependencies.add(self.db, int(suite["id"]), int(parent["id"]),
                              actor="test")
        rows = take_refusals.explain(self.cfg, self.db, now=self.now)
        row = self.row(rows, suite["id"])
        self.assertEqual(row["reason"], "prerequisite")
        self.assertIn("#%d" % int(parent["id"]), row["detail"])
        self.assertFalse(row["startable"])

    def test_a_footprint_blocker_is_named(self):
        busy = self.lot("occupe")
        self.db.execute("UPDATE work_items SET state = 'build', assignee = 'devb0'"
                        " WHERE id = %s", (int(busy["id"]),))
        storage.of(self.db).work_footprints.replace(
            int(busy["id"]), "manual", ["src/ameesh/x.py"], actor="test")
        covered = self.lot("recouvert")
        storage.of(self.db).work_footprints.replace(
            int(covered["id"]), "manual", ["src/ameesh/x.py"], actor="test")
        rows = take_refusals.explain(self.cfg, self.db, now=self.now)
        row = self.row(rows, covered["id"])
        self.assertEqual(row["reason"], "footprint")
        self.assertIn("#%d" % int(busy["id"]), row["detail"])

    def test_an_eligible_asleep_agent_is_named(self):
        self.agent("dev0", paused=True)
        item = self.lot("endormi")
        rows = take_refusals.explain(self.cfg, self.db, now=self.now)
        row = self.row(rows, item["id"])
        self.assertEqual(row["reason"], "asleep")
        self.assertEqual(row["asleep"], ["dev0"])
        self.assertTrue(row["startable"])

    def test_a_basket_behind_a_startable_roadmap_lot_names_the_roadmap(self):
        self.agent("dev0")
        self.lot("lot de roadmap")
        basket = backlog.add(self.db, title="panier", expected_value="gain",
                             value_score=10, team="ameesh", actor="test",
                             estimate="30m")
        rows = take_refusals.explain(self.cfg, self.db, now=self.now)
        row = self.row(rows, basket["id"])
        self.assertEqual(row["reason"], "roadmap")
        self.assertFalse(row["startable"])
        self.assertIn("#", row["detail"])


class JournalTest(Base):
    def test_one_note_per_hour_and_one_alert_per_reason(self):
        self.agent("dev0", paused=True)
        item = self.lot("endormi")
        with mock.patch("ameesh.sous_utilisation.orchestrators",
                        return_value=["pilote"]), \
                mock.patch.object(take_refusals, "_alert_pilot") as alert:
            take_refusals.journal(self.cfg, self.db, now=self.now)
            notes = [row["note"] for row in work.events(self.db, int(item["id"]))]
            self.assertEqual(sum(note.startswith(take_refusals.REFUSAL_PREFIX)
                                 for note in notes), 1)
            self.assertTrue(any(note.startswith(take_refusals.REFUSAL_PREFIX)
                                for note in notes))
            self.assertEqual(alert.call_count, 1)
            take_refusals.journal(self.cfg, self.db, now=self.now + 60)
            notes = [row["note"] for row in work.events(self.db, int(item["id"]))]
            self.assertEqual(sum(note.startswith(take_refusals.REFUSAL_PREFIX)
                                 for note in notes), 1)
            self.assertEqual(alert.call_count, 1)

    def test_wake_deadline_lifts_the_automatic_sleep(self):
        self.agent("dev0", paused=True)
        item = self.lot("endormi")
        woken = take_refusals.wake_deadline(self.cfg, self.db, now=self.now)
        self.assertEqual([(row["lot"], row["agents"]) for row in woken],
                         [(int(item["id"]), ["dev0"])])
        self.assertFalse(registry.get(self.db, "dev0")["operator_paused"])

    def test_a_young_lot_is_not_journaled(self):
        self.agent("dev0", paused=True)
        item = self.lot("jeune", age_s=120)
        with mock.patch.object(take_refusals, "_alert_pilot") as alert:
            take_refusals.journal(self.cfg, self.db, now=self.now)
        self.assertEqual(alert.call_count, 0)
        notes = [row["note"] for row in work.events(self.db, int(item["id"]))]
        self.assertFalse(any(note.startswith(take_refusals.REFUSAL_PREFIX)
                             for note in notes))


class DecoupageBasketTest(Base):
    def test_a_ready_basket_keeps_the_queue_awake(self):
        listing = [{"name": "dev0", "mode": "execute", "status": "idle",
                    "lease_live": True, "lease_owner": "runner:h1", "unread": 0,
                    "assigned_lot_id": None, "active_lots": 0,
                    "actionable_lots": 0, "projects": ["ameesh"],
                    "responsible": "human:proprio", "capabilities": ["propose"],
                    "roles": ["implementation"]}]
        item = fd.Measure("ameesh", wakeable=["dev0"], idle=["dev0"],
                          baskets=[42])
        self.assertEqual(fd.held({"ameesh": item}, {"ameesh": 0.0}, 4000.0,
                                 0.20, 1800), ([], {}))
        item.baskets = []
        due, _ = fd.held({"ameesh": item}, {"ameesh": 0.0}, 4000.0, 0.20, 1800)
        self.assertEqual(due, ["ameesh"])
        self.assertEqual(listing[0]["name"], "dev0")   # garde le fixture lisible


class SeeAlsoTest(Base):
    def test_see_also_is_a_non_blocking_typed_link(self):
        parent = self.lot("parent")
        suite = self.lot("suite")
        self.assertTrue(work_dependencies.add_link(
            self.db, int(suite["id"]), int(parent["id"]), "see_also",
            actor="test"))
        links = work_dependencies.links(self.db, [int(suite["id"])])
        row = links[int(suite["id"])][0]
        self.assertEqual((row["kind"], row["lot"]), ("see_also", int(parent["id"])))
        # aucun prérequis bloquant n'est créé
        waits = work_dependencies.grouped(self.db, [int(suite["id"])])
        self.assertEqual(waits.get(int(suite["id"]), []), [])

    def test_work_add_warns_on_parent_lot_and_links_see_also(self):
        parent = self.lot("parent")
        out = self.mesh("work", "add", "--title", "suite bloquante",
                       "--app", "ameesh", "--new-project",
                       "--parent-lot", str(int(parent["id"])),
                       "--estimate", "30m", "--actor", "test")
        self.assertEqual(out.returncode, 0, (out.stdout, out.stderr))
        self.assertIn("BLOCKING", out.stderr)
        out = self.mesh("work", "add", "--title", "suite voir aussi",
                       "--app", "ameesh", "--new-project",
                       "--see-also", str(int(parent["id"])),
                       "--estimate", "30m", "--actor", "test")
        self.assertEqual(out.returncode, 0, (out.stdout, out.stderr))
        self.assertIn("see also", out.stdout)
        rows = self.db.query(
            "SELECT id FROM work_items WHERE title = %s ORDER BY id DESC LIMIT 1",
            ("suite voir aussi",))
        links = work_dependencies.links(self.db, [int(rows[0]["id"])])
        self.assertEqual(links[int(rows[0]["id"])][0]["kind"], "see_also")


if __name__ == "__main__":
    unittest.main()

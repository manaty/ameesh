# SPDX-License-Identifier: AGPL-3.0-only
"""#8193 (tranche 3 de #8191, issue manaty/ameesh#6403) : sujets de lots.

`work subject add|list|show|close|attach`, `work add --subject`,
`work list --subject` (slug ou fiche WorkPackage). Le sujet est porté par
`work_items.subject_id` (0608), jamais par `workstream`.
"""
from __future__ import annotations

import json

from ameesh import storage, work, work_subjects

from .test_l119_backlog import _Base

APP = "t8193"


class _SubjectBase(_Base):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE work_subjects RESTART IDENTITY")


class SubjectStoreTest(_SubjectBase):
    def lot(self, title="Lot", app=APP):
        return work.add(self.db, title=title, app=app, actor="human:proprio", footprint=[])

    def test_add_is_idempotent_on_the_slug(self):
        first, created = work_subjects.add(self.db, "pricing-v2", "Pricing v2",
                                           project=APP, actor="human:proprio")
        self.assertTrue(created)
        self.assertEqual((first["slug"], first["state"], first["author"]),
                         ("pricing-v2", "open", "human:proprio"))
        self.assertIsNone(first["github_ref"])
        again, created = work_subjects.add(self.db, "pricing-v2", "Autre titre",
                                           project=APP, actor="other")
        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["title"], "Pricing v2")
        # même slug, autre projet : un autre sujet
        other, created = work_subjects.add(self.db, "pricing-v2", "Ailleurs", project="t-other")
        self.assertTrue(created)
        self.assertNotEqual(other["id"], first["id"])
        with self.assertRaisesRegex(work.WorkError, "several projects"):
            work_subjects.find(self.db, "pricing-v2")
        self.assertEqual(work_subjects.find(self.db, "t-other:pricing-v2")["id"], other["id"])

    def test_bad_slug_and_missing_title_refused(self):
        for slug in ("Pricing", "-x", "x-", "a b", "", "x" * 64):
            with self.subTest(slug=slug), self.assertRaises(work.WorkError):
                work_subjects.add(self.db, slug, "T", project=APP)
        with self.assertRaises(work.WorkError):
            work_subjects.add(self.db, "ok", "  ", project=APP)

    def test_attach_detach_and_list_by_subject(self):
        work_subjects.add(self.db, "pricing-v2", "Pricing v2", project=APP)
        a, b, c = self.lot("A"), self.lot("B"), self.lot("C")
        for lot in (a, b):
            work_subjects.attach(self.db, lot["id"], "pricing-v2", actor="human:proprio")
        _, rows = work_subjects.lots(self.db, "pricing-v2")
        self.assertEqual([r["id"] for r in rows], [a["id"], b["id"]])
        self.assertIn("subject: pricing-v2", [e["note"] for e in work.events(self.db, a["id"])])
        # le sujet n'écrit pas `workstream`
        self.assertIsNone(work.get(self.db, a["id"])["workstream"])
        work_subjects.attach(self.db, b["id"], None, actor="human:proprio")
        _, rows = work_subjects.lots(self.db, "pricing-v2")
        self.assertEqual([r["id"] for r in rows], [a["id"]])
        self.assertNotIn(c["id"], [r["id"] for r in rows])
        work.close(self.db, a["id"], abandoned=True, actor="human:proprio")
        self.assertEqual([r["id"] for r in work_subjects.filtered(self.db, "pricing-v2")],
                         [a["id"]])
        self.assertEqual(work_subjects.filtered(self.db, "pricing-v2", state="intake"), [])

    def test_unknown_closed_or_foreign_subject_refused(self):
        lot = self.lot()
        with self.assertRaisesRegex(work.WorkError, "work subject add"):
            work_subjects.attach(self.db, lot["id"], "nope")
        with self.assertRaisesRegex(work.WorkError, "work subject add"):
            work_subjects.lots(self.db, "nope")
        work_subjects.add(self.db, "elsewhere", "Autre projet", project="t-other")
        with self.assertRaisesRegex(work.WorkError, "work subject add"):
            work_subjects.attach(self.db, lot["id"], "elsewhere")
        work_subjects.add(self.db, "done", "Clos", project=APP)
        closed = work_subjects.close(self.db, "done", actor="human:proprio")
        self.assertEqual((closed["state"], closed["closed_by"]), ("closed", "human:proprio"))
        again = work_subjects.close(self.db, "done", actor="other")
        self.assertEqual((again["closed_by"], again["closed_ts"]),
                         (closed["closed_by"], closed["closed_ts"]))
        with self.assertRaisesRegex(work.WorkError, "closed"):
            work_subjects.attach(self.db, lot["id"], "done")
        self.assertEqual([s["slug"] for s in work_subjects.list_subjects(
            self.db, project=APP, open_only=True)], [])

    def test_workpackage_card_is_a_subject(self):
        storage.of(self.db).packages.upsert({
            "id": "WP-EPIC", "kind": "epic", "title": "Épique", "parent": None,
            "responsible": None, "team": APP, "scope": None, "status": None,
            "canon_ref": "c", "canon": "c", "start_on": None, "end_on": None,
            "delivery_on": None})
        storage.of(self.db).packages.upsert({
            "id": "WP-LOT", "kind": "lot", "title": "Lot de l'épique", "parent": "WP-EPIC",
            "responsible": None, "team": APP, "scope": None, "status": None,
            "canon_ref": "c", "canon": "c", "start_on": None, "end_on": None,
            "delivery_on": None})
        on_epic, on_child, other = self.lot("E"), self.lot("L"), self.lot("X")
        work.link(self.db, on_epic["id"], "WP-EPIC")
        work.link(self.db, on_child["id"], "WP-LOT")
        described, rows = work_subjects.lots(self.db, "WP-EPIC")
        self.assertEqual(described["package"], "WP-EPIC")
        self.assertEqual([r["id"] for r in rows], [on_epic["id"], on_child["id"]])
        self.assertNotIn(other["id"], [r["id"] for r in rows])
        # aucune copie : la fiche n'est pas devenue une ligne de work_subjects
        self.assertEqual(work_subjects.list_subjects(self.db), [])


class SubjectCliTest(_SubjectBase):
    def run_ok(self, *args):
        result = self.mesh(*args)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def test_cli_round_trip(self):
        # un lot du projet le rend connu de `--app` (#495)
        work.add(self.db, title="Amorce", app=APP, actor="human:proprio", footprint=[])
        add = self.run_ok("work", "subject", "add", "pricing-v2", "--title", "Pricing v2",
                          "--app", APP, "--actor", "human:proprio")
        self.assertIn("subject pricing-v2 created in %s" % APP, add.stdout)
        again = self.run_ok("work", "subject", "add", "pricing-v2", "--title", "Pricing v2",
                            "--app", APP, "--actor", "human:proprio")
        self.assertIn("already exists", again.stdout)

        created = self.run_ok("work", "add", "--title", "Grille tarifaire", "--app", APP,
                              "--actor", "human:proprio", "--subject", "pricing-v2")
        self.assertIn("subject: pricing-v2", created.stdout)
        lot_id = int(self.db.query("SELECT max(id) AS id FROM work_items")[0]["id"])
        self.assertIsNotNone(self.db.query(
            "SELECT subject_id FROM work_items WHERE id = %s", (lot_id,))[0]["subject_id"])

        refused = self.mesh("work", "add", "--title", "Orphelin", "--app", APP,
                            "--actor", "human:proprio", "--subject", "inconnu")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("work subject add", refused.stderr + refused.stdout)
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM work_items WHERE title = 'Orphelin'")[0]["n"], 0)

        other = work.add(self.db, title="Rattaché après coup", app=APP,
                         actor="human:proprio", footprint=[])
        self.run_ok("work", "subject", "attach", str(other["id"]), "pricing-v2",
                    "--actor", "human:proprio")

        listed = json.loads(self.run_ok("work", "list", "--subject", "pricing-v2",
                                        "--json").stdout)
        self.assertEqual(sorted(r["id"] for r in listed), sorted([lot_id, other["id"]]))
        text = self.run_ok("work", "list", "--subject", "pricing-v2").stdout
        self.assertIn("Grille tarifaire", text)
        unknown = self.mesh("work", "list", "--subject", "inconnu")
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("work subject add", unknown.stderr + unknown.stdout)

        show = self.run_ok("work", "subject", "show", "pricing-v2").stdout
        self.assertIn("NEXT WAKE", show)
        self.assertIn("#%d" % other["id"], show)
        shown = json.loads(self.run_ok("work", "subject", "show", "pricing-v2",
                                       "--json").stdout)
        self.assertEqual(len(shown["lots"]), 2)

        self.run_ok("work", "subject", "close", "pricing-v2", "--actor", "human:proprio")
        self.assertIn("no subjects",
                      self.run_ok("work", "subject", "list", "--open").stdout)
        self.assertIn("closed", self.run_ok("work", "subject", "list").stdout)

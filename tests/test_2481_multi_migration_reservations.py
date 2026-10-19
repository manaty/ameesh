# SPDX-License-Identifier: AGPL-3.0-only
"""#2481 : un lot garde plusieurs numéros de migration réservés.

Le constat codex2 (Nexlink #589 : 528 hub + 529 CONTENT) : le champ scalaire
remplaçait la première réservation par la seconde, et l'alerte de migration
sans réservation tournait en boucle. Un lot ajoute désormais ses numéros à
`work_migration_reservations` (créneaux `legacy-<n>`), le scalaire restant la
réservation principale pour les lecteurs d'avant #2481.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import migration_reservations as reservations, radar, work
from ameesh.storage.postgres import work as pg_work

from .support import PgTestCase
from .test_l122_numeros_de_migration import NEXLINK, _depot, _git


class MultiReservationTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.repo = _depot(os.path.join(self.tmp, "nexlink"), "manaty/nexlink",
                           NEXLINK.directory, ["527_base.sql"])
        # #2620 : le numéro exact lit la cible du lot ; ce dépôt n'a que
        # `develop` (pas d'`origin/HEAD`), la cible est déclarée.
        _git(self.repo, "config", "ameesh.target", "develop")
        self.lot = work.add(self.db, title="CIV-3", issue_ref="manaty/nexlink#589",
                            app="nexlink", actor="mesh-design")

    def reserve(self, number=None, **kwargs):
        return work.reserve_migration(self.db, self.lot["id"], number,
                                      repo=self.repo, **kwargs)

    def numbers(self, item=None):
        item = item if item is not None else work.get(self.db, self.lot["id"])
        return [row["number"] for row in item["migrations"]]

    def test_two_numbers_are_kept_and_the_alert_is_silent(self):
        self.assertEqual(self.reserve(528)["migration"], 528)
        second = self.reserve(529)
        # le scalaire reste la première réservation, la liste porte les deux
        self.assertEqual((second["migration"], second["migration_repo"]),
                         (528, "manaty/nexlink"))
        self.assertEqual(self.numbers(second), [528, 529])
        for number in (528, 529):
            holder = work.storage.of(self.db).work.migration_holder(
                number, "manaty/nexlink")
            self.assertIsNotNone(holder, number)
            self.assertEqual(int(holder["id"]), self.lot["id"])
        # la sonde qui produit l'alerte ne voit plus de migration sans
        # réservation : les deux fichiers sont tenus par le même lot
        project = radar.Project("nexlink", self.repo, migrations=NEXLINK.directory,
                                github="manaty/nexlink")
        branches = [dict(work_item=self.lot["id"], target="develop", added=[
            "supabase/migrations/528_hub.sql",
            "supabase/migrations/529_content.sql"])]
        self.assertEqual(reservations.probe(self.db, project, branches), [])

        # réserver un numéro déjà tenu par le lot est idempotent
        self.assertEqual(self.numbers(self.reserve(529)), [528, 529])
        # un autre lot ne peut pas prendre 529
        other = work.add(self.db, title="autre", issue_ref="manaty/nexlink#590",
                         app="nexlink", actor="mesh-design")
        with self.assertRaises(work.WorkError) as ctx:
            work.reserve_migration(self.db, other["id"], 529, repo=self.repo)
        self.assertIn("lot #%d" % self.lot["id"], str(ctx.exception))

    def test_ref_columns_do_not_probe_the_slots_table(self):
        """#2481 B1 : le chemin `ref` (work list, progress) ne paie pas la
        sonde de table ; il rend la liste vide et se replie sur le scalaire."""
        with mock.patch.object(pg_work, "has_migration_slots",
                               side_effect=AssertionError("sonde sur le chemin ref")):
            columns = pg_work.item_columns(self.db, "work_items")
        self.assertIn("'[]'::jsonb AS migrations", columns)
        # la ligne complète garde la liste réelle : work show affiche les deux
        self.reserve(528)
        self.reserve(529)
        item = work.get(self.db, self.lot["id"])
        self.assertEqual([row["number"] for row in item["migrations"]], [528, 529])

    def test_total_release_note_names_every_number(self):
        """#2481 N2 : une libération totale ne rend pas le sentinelle 0."""
        self.reserve(528)
        self.reserve(529)
        item = work.get(self.db, self.lot["id"])
        work.storage.of(self.db).work.reserve_migration(
            self.lot["id"], "manaty/nexlink", None, 0, actor="test",
            issue_ref=item.get("issue_ref"), release=True,
            note="migrations %(number)s released")
        note = self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s"
            " ORDER BY id DESC LIMIT 1", (self.lot["id"],))[0]["note"]
        self.assertEqual(note, "migrations 528, 529 released")

    def test_release_one_number_then_all(self):
        self.reserve(528)
        self.reserve(529)
        released = work.reserve_migration(self.db, self.lot["id"], 529,
                                          release=True, repo=self.repo)
        self.assertEqual(self.numbers(released), [528])
        self.assertIsNone(work.storage.of(self.db).work.migration_holder(
            529, "manaty/nexlink"))
        self.assertIsNotNone(work.storage.of(self.db).work.migration_holder(
            528, "manaty/nexlink"))
        released = work.reserve_migration(self.db, self.lot["id"], 528,
                                          release=True, repo=self.repo)
        self.assertEqual(self.numbers(released), [])
        self.assertIsNone(work.storage.of(self.db).work.migration_holder(
            528, "manaty/nexlink"))

    def test_releasing_the_primary_promotes_the_next_number(self):
        self.reserve(528)
        self.reserve(529)
        released = work.reserve_migration(self.db, self.lot["id"], 528,
                                          release=True, repo=self.repo)
        self.assertEqual((released["migration"], released["migration_repo"]),
                         (529, "manaty/nexlink"))
        self.assertEqual(self.numbers(released), [529])
        self.assertIsNone(work.storage.of(self.db).work.migration_holder(
            528, "manaty/nexlink"))
        self.assertIsNotNone(work.storage.of(self.db).work.migration_holder(
            529, "manaty/nexlink"))

    def test_cli_lists_both_reservations(self):
        first = self.mesh("work", "migration", str(self.lot["id"]),
                          "--number", "528", "--repo", self.repo)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("migration 528 reserved in manaty/nexlink", first.stdout)
        second = self.mesh("work", "migration", str(self.lot["id"]),
                           "--number", "529", "--repo", self.repo)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("migrations 528, 529 reserved in manaty/nexlink", second.stdout)
        proc = self.mesh("work", "migration", str(self.lot["id"]), "--json")
        self.assertIn('"number": 528', proc.stdout)
        self.assertIn('"number": 529', proc.stdout)
        self.assertIn("supabase/migrations/529_<name>.sql", proc.stdout)
        proc = self.mesh("work", "show", str(self.lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("migration: migrations 528, 529 reserved in manaty/nexlink (L122)",
                      proc.stdout)


class ImportMultiReservationTest(PgTestCase):
    """L160g : deux lignes du registre historique peuvent viser le même lot."""

    def test_import_two_numbers_to_one_lot(self):
        lot = work.add(self.db, title="legacy", issue_ref="manaty/nexlink#589",
                       app="nexlink", actor="mesh-design")
        rows = [{"number": 323, "label": "Billing", "who": "a", "status": "reserved"},
                {"number": 324, "label": "Billing 2", "who": "a", "status": "reserved"}]
        out = reservations.import_reservations(
            self.db, rows, {"323": lot["id"], "324": lot["id"]},
            repo="manaty/nexlink", apply=True, actor="orch")
        self.assertEqual([row["action"] for row in out], ["reserved", "reserved"])
        item = work.get(self.db, lot["id"])
        self.assertEqual([row["number"] for row in item["migrations"]], [323, 324])
        self.assertEqual(item["migration"], 323)

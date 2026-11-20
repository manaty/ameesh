# SPDX-License-Identifier: AGPL-3.0-only
"""#7893: a delivered but not closed lot whose verification waits for a merged
fix that staging has not deployed counts like an open held lot.

The delivery note — the notes written in the delivery stage of the journal —
is read with the same #3297 clause rules as an active hold; a delivered lot
without such a note does not count. The #7870 case is the regression: its
last delivery note names #7873, merged in h-20261119-11 and absent from the
active staging 1.64.0.
"""
import types
import unittest
from unittest import mock

from ameesh import release_cutoff, staging_pending
from ameesh.storage import interface
from tests.test_2448_release_cutoff import ReleaseFixtureMixin


class DeliveredWaitingTest(ReleaseFixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.old = self.git(self.clone, "rev-parse", "v1.11.7^{commit}")
        self.active = {"vm": {"sha": self.old, "at": 1}}
        self.rows = [
            {"id": 7870, "app": "ameesh", "state": "delivered"},
            {"id": 7873, "app": "ameesh", "state": "delivered"},
        ]
        self.milestones = {
            7873: [{"id": 10, "kind": "merged", "sha": self.head,
                    "at_ts": 5, "at": "2026-10-24T04:00:00Z"}],
        }
        self.notes = []
        self.events = []
        st = types.SimpleNamespace(
            work=types.SimpleNamespace(
                items=lambda **kw: self.rows,
                milestones=lambda ident, **kw: self.milestones.get(ident, [])),
            progress=types.SimpleNamespace(
                lot_hold_events=lambda *a: self.events,
                lot_delivery_notes=self.delivery_notes))
        patch = mock.patch.object(staging_pending.storage, "of", return_value=st)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(staging_pending.planned_waits, "_pilot_allowed",
                                  return_value=lambda actor: actor == "pilot")
        patch.start()
        self.addCleanup(patch.stop)

    def delivery_notes(self, ids):
        wanted = {int(ident) for ident in ids}
        return [event for event in self.notes
                if int(event["work_item_id"]) in wanted]

    def note(self, ident, text, *, state="merged", event_id=None):
        self.notes.append({"id": event_id or len(self.notes) + 1,
                           "work_item_id": ident, "state": state,
                           "actor": "mesh-design", "note": text})

    def scan(self):
        return staging_pending.waiting(None, self.project, active=self.active)

    def test_7870_delivered_lot_waiting_for_its_merged_fix_counts(self):
        self.note(7870, (
            "Pilote (22:4xZ), voie (b) après la coupure 1.64.0 (staging actif "
            "d6c55dbc sur les 3 VM à 22:29Z). 0598 est posée sur la base "
            "partagée. Mesure en lecture seule : il manque à bureau_goal "
            "SELECT sur module_schemas (c'est le défaut de #7873).\n"
            "Suite : le rejeu automatique de #7873 (h-20261119-11, en CI) à son "
            "déploiement staging, au plus tard à la coupure de 10:00Z le 20/11. "
            "#7870 n'est pas fait avant les droits rejoués et l'audit vert."))
        result = self.scan()
        self.assertEqual(result["lots"], [7870])
        self.assertEqual(len(result["waiting"]), 1)
        row = result["waiting"][0]
        self.assertEqual(row["lot"], 7870)
        self.assertEqual(row["blocker"], 7873)
        self.assertEqual(row["app"], "ameesh")
        self.assertEqual(row["hosts"], ["vm"])
        self.assertEqual(row["merged_at"], 5)
        # Le correctif devient actif : l'attente est finie.
        self.active["vm"]["sha"] = self.head
        self.assertEqual(self.scan()["lots"], [])

    def test_delivered_lot_without_a_deployment_wait_does_not_count(self):
        self.rows.append({"id": 7871, "app": "ameesh", "state": "delivered"})
        self.note(7871, "merge seen by packet manifest (packet-snapshot:115) "
                        "(commit cbfedcf6eed2) [via git-merge]")
        self.note(7871, "livré : PR #6104 fusionnée, déployée sur le poste")
        self.assertEqual(self.scan()["lots"], [])

    def test_delivery_note_keeps_the_3297_clause_rules(self):
        self.rows.append({"id": 7871, "app": "ameesh", "state": "delivered"})
        for text in (
                "livré : PR #7873 pas déployée",
                "écarté de h-03 : conflit sémantique avec #7873, pas déployé",
                "ticket IMA #7873, pas déployé",
                "gel #7873, pas déployé",
                "relu par #7873, pas déployé",
                "paquet #7873 pas déployé"):
            with self.subTest(text=text):
                self.notes.clear()
                self.note(7871, text)
                self.assertEqual(self.scan()["lots"], [])
        self.note(7871, "attendre #7873")
        self.assertEqual(self.scan()["lots"], [7871])

    def test_delivery_note_must_name_a_merged_fix(self):
        self.note(7870, "attendre #9999 déployé")
        self.assertEqual(self.scan()["lots"], [])
        # Un lot encore ouvert n'est pas un correctif à déployer.
        self.rows.append({"id": 9999, "app": "ameesh", "state": "qa"})
        self.assertEqual(self.scan()["lots"], [])

    def test_absent_full_sha_is_unknown_without_a_git_lookup(self):
        # #7893: un commit absent du clone de release est une preuve inconnue
        # (#3297). Le demander à git sur le clone partiel tente un lazy fetch
        # d'une dizaine de secondes : l'énumération locale des objets tranche
        # sans lui.
        self.note(7870, "attendre #7873")
        self.milestones[7873][0]["sha"] = "f" * 40
        real = release_cutoff.commit_of
        with mock.patch.object(release_cutoff, "commit_of",
                               side_effect=real) as commit_of:
            result = self.scan()
        self.assertEqual(result["lots"], [])
        self.assertEqual(result["unknown"][0]["reason"],
                         "merged commit absent from the release clone")
        self.assertNotIn(mock.call(self.project.repo, "f" * 40),
                         commit_of.call_args_list)

    def test_delivery_note_of_another_project_counts_for_this_staging(self):
        # Le dépendant peut appartenir à Nexlink : c'est le projet du
        # correctif (ameesh) qui détermine le staging, pas celui du lot.
        self.rows[0]["app"] = "nexlink"
        self.note(7870, "le correctif #7873 n'est pas encore actif en staging")
        self.assertEqual(self.scan()["lots"], [7870])

    def test_only_delivery_stage_notes_are_read(self):
        # La note de livraison n'est pas une note de l'étape antérieure : le
        # repli de l'interface écarte les états qa/validated.
        events = [
            {"id": 1, "work_item_id": 7870, "state": "validated",
             "note": "hold: attendre #7873"},
            {"id": 2, "work_item_id": 7870, "state": "merged",
             "note": "merge seen by packet manifest"},
            {"id": 3, "work_item_id": 7870, "state": "merged", "note": "   "},
            {"id": 4, "work_item_id": 7870, "state": "promoted",
             "note": "attendre #7873"},
        ]

        class Stub:
            def lot_events(self, ids):
                return events

        notes = interface.Progress.lot_delivery_notes(Stub(), [7870])
        self.assertEqual([event["id"] for event in notes], [2, 4])


if __name__ == "__main__":
    unittest.main()

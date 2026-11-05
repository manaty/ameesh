# SPDX-License-Identifier: AGPL-3.0-only
"""#5891 : constater la fusion et composer dans le même tic, sauf hold."""
from unittest import mock

from .test_3845_paquet_horaire import Base, HEAD, carrier, ph


class HourlyMergedCarrierTest(Base):
    def setUp(self):
        super().setUp()
        self.previous = carrier(lot=3990)
        self.shell.items[3990] = self.previous
        self.shell.items[4000] = carrier()
        self.shell.lists["qa"] = [self.previous]
        self.shell.lists[""] = [self.previous]
        self.shell.ancestor = True
        self.held = False
        self.sync_rc = 0
        original = self.shell.run

        def run(cmd, **kwargs):
            if cmd[:3] == ["ameesh", "work", "sync-merges"]:
                self.shell.calls.append(list(cmd))
                if not self.held and not self.sync_rc:
                    self.previous["state"] = "delivered"
                    self.shell.lists["qa"] = []
                    # La nouvelle tête n'est pas déjà dans la cible.
                    self.shell.ancestor = False
                return ph.Result(self.sync_rc)
            return original(cmd, **kwargs)

        self.shell.run = run

    def test_closes_then_composes_in_the_same_tick(self):
        self.assertIn("ci-full demandée", ph.tick(self.ctx))
        calls = self.shell.ran("work", "sync-merges")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index("--id") + 1], "3990")
        self.assertEqual(calls[0][calls[0].index("--target") + 1], "origin/main")
        self.assertEqual(len(self.shell.added), 1)

    def test_pilot_hold_keeps_the_slot_reserved(self):
        self.held = True
        self.assertIn("fusionné, rien à faire", ph.tick(self.ctx))
        self.assertEqual(self.previous["state"], "qa")
        self.assertEqual(self.shell.added, [])
        self.assertEqual(self.shell.ran("ci", "run"), [])

    def test_forge_note_without_ancestry_does_not_close(self):
        self.shell.ancestor = False
        self.previous["events"] = [{"note": "auto-merge merged repo=manaty/ameesh "
                                    "pr=1 sha=%s base=main commit=%s" % (HEAD, HEAD)}]
        ph.tick(self.ctx)
        self.assertEqual(self.shell.ran("work", "sync-merges"), [])
        self.assertEqual(self.shell.added, [])

    def test_failed_sync_does_not_compose(self):
        self.sync_rc = 1
        with self.assertRaises(ph.HoraireError):
            ph.tick(self.ctx)
        self.assertEqual(self.shell.added, [])

    def test_dry_run_does_not_close_or_compose(self):
        self.ctx.dry_run = True
        ph.tick(self.ctx)
        self.assertEqual(self.shell.ran("work", "sync-merges"), [])
        self.assertEqual(self.shell.added, [])

    def test_active_composition_is_not_closed(self):
        with mock.patch.object(ph.auto, "composition_holder", return_value=("host", 42)):
            self.assertIn("composition en cours", ph.tick(self.ctx))
        self.assertEqual(self.shell.ran("work", "sync-merges"), [])
        self.assertEqual(self.shell.added, [])

    def test_another_reservation_survives_the_closure(self):
        other = carrier(lot=3991, source="pilote manuel")
        self.shell.lists["build"] = [other]
        self.assertIn("réservée par #3991", ph.tick(self.ctx))
        self.assertEqual(self.previous["state"], "delivered")
        self.assertEqual(self.shell.added, [])

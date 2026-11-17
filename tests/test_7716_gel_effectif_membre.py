# SPDX-License-Identifier: AGPL-3.0-only
"""#7716 — le gel d'un membre de véhicule est son gel effectif, pas le dernier
gel déclaré.

Constat (mty-nxl-rel-01, #224347) sur le paquet Nexlink #7715 : le membre
#7678 avait un jalon `frozen` (29824769) antérieur à un verdict ok sur une
tête plus récente (b21417f3). `packet_reviews._member_freeze` lisait
`plan_git.latest_freeze` — le dernier gel *déclaré* — au lieu de
`plan_git.freeze_candidate`, que `packet_link._effective_freeze` enregistre
dans `work_packet_members.frozen_sha` et que `work show` affiche. Le delta
propre du véhicule comptait donc les commits du membre relus après le gel
déclaré : 9 chemins et 11 commits au lieu de 0, et la désignation au gel du
véhicule refusait le paquet.

Témoins du lot :
(1) dernier gel déclaré avant un verdict ok sur une tête plus récente :
    0 chemin propre, 0 avis de véhicule ;
(2) unité sans gel ni verdict : refus inchangé (une déclaration d'atterrissage
    seule, `declared_only`, reste ignorée — #2775) ;
(3) gel déclaré postérieur au verdict : le gel déclaré prime.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import lot_class, merge_queue, packet_reviews

from .test_l160b_radar import _git, _write


def _commit_all(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _merge(repo, branch, lot, source=None):
    """Une composition comme #2157 : fusion --no-ff, trailer `ameesh-work`."""
    source = source or _git(repo, "rev-parse", branch)
    _git(repo, "merge", "-q", "--no-ff", branch, "-m",
         "Merge #%d — %s\n\nameesh-work: %d" % (lot, source[:12], lot))
    return _git(repo, "rev-parse", "HEAD")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-7716-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _write(self.repo, "README.md", "base\n")
        _commit_all(self.repo, "base")
        self.base = _git(self.repo, "rev-parse", "main")

    # -- un membre dont le gel déclaré précède un verdict plus récent -------
    def compose_reviewed_member(self):
        """m1 : gel déclaré sur A, verdict ok sur C, composition de C."""
        _git(self.repo, "checkout", "-q", "-b", "m1", "main")
        _write(self.repo, "src/app.py", "print(1)\n")
        self.declared = _commit_all(self.repo, "m1: tête gelée déclarée")
        _write(self.repo, "src/app.py", "print(2)\n")
        _write(self.repo, "src/lib.py", "def f():\n    return 2\n")
        self.verdict = _commit_all(self.repo, "m1: correction relue")
        _git(self.repo, "checkout", "-q", "main")
        self.head = _merge(self.repo, "m1", 101)
        self.marks = {101: [
            {"id": 1, "kind": "frozen", "sha": self.declared, "at_ts": 1.0},
            {"id": 2, "kind": "verdict", "verdict": "ok", "sha": self.verdict,
             "actor": "rev1", "at_ts": 2.0},
        ]}

    # -- les lectures du module, sur des jalons fixés ----------------------
    def freeze(self, lot, marks, *, declared_only=False):
        with mock.patch.object(packet_reviews.work, "get",
                               side_effect=lambda _db, key: {"id": key, "app": "ameesh"}):
            with mock.patch.object(packet_reviews.work, "milestones",
                                   side_effect=lambda _db, key: marks):
                return packet_reviews._member_freeze(
                    None, lot, declared_only=declared_only, repo=self.repo,
                    target=self.base)

    def designation(self, marks, head, *, target=None):
        class DB:
            def query(self, _sql, _args=()):
                return []

        with mock.patch.object(packet_reviews.work, "get",
                               side_effect=lambda _db, key: {"id": key, "app": "ameesh"}):
            with mock.patch.object(packet_reviews.work, "milestones",
                                   side_effect=lambda _db, key: marks[key]):
                return packet_reviews.designation_changes(
                    DB(), 999, {"id": 999, "app": "ameesh",
                                "branch": "integration/paquet"},
                    self.repo, self.base, head, target=target)

    def need(self, marks, head, landings=()):
        class DB:
            def query(self, _sql, _args=()):
                return list(landings)

        with mock.patch.object(packet_reviews.work, "get",
                               side_effect=lambda _db, key: {"id": key, "app": "ameesh"}):
            with mock.patch.object(packet_reviews.work, "milestones",
                                   side_effect=lambda _db, key: marks[key]):
                with mock.patch.object(lot_class, "of_lot",
                                       return_value={"class": "light", "files": [],
                                                     "doubt": False}):
                    with mock.patch.object(merge_queue, "review_gate",
                                           return_value={"ok": True}):
                        return packet_reviews.need(
                            DB(), 999, {"id": 999, "app": "ameesh",
                                        "branch": "integration/paquet"},
                            marks[101], self.repo, self.base, head, cfg=None)


class EffectiveFreezeTest(_Base):
    """(1) un verdict ok plus récent que le dernier gel déclaré fait foi."""

    def test_the_member_freeze_is_the_verdict_head(self):
        self.compose_reviewed_member()
        _row, _marks, freeze = self.freeze(101, self.marks[101])
        self.assertEqual(freeze["sha"], self.verdict)
        self.assertEqual(freeze["origin"], "verdict")

    def test_the_vehicle_has_no_proper_delta(self):
        # avant #7716 : own_changes(base, tête, [29824769]) rendait les commits
        # du membre relus après le gel déclaré (9 chemins, 11 commits).
        self.compose_reviewed_member()
        own = self.designation(self.marks, self.head, target=self.base)
        self.assertEqual(own["paths"], [])
        self.assertEqual(own["commits"], [])

    def test_the_vehicle_gate_requires_no_vehicle_review(self):
        self.compose_reviewed_member()
        out = self.need(self.marks, self.head)
        self.assertIsNotNone(out)
        self.assertTrue(out["ok"])
        self.assertEqual(out["required"], 0)
        self.assertEqual(out["own_paths"], [])
        self.assertIn("no proper vehicle delta", out["why"])


class NoFreezeTest(_Base):
    """(2) une unité sans gel ni verdict reste refusée telle quelle."""

    def test_a_composed_member_without_freeze_or_verdict_is_refused(self):
        with self.assertRaisesRegex(packet_reviews.VehicleError,
                                    "#101 has no frozen head"):
            self.freeze(101, [])

    def test_the_vehicle_gate_refuses_it(self):
        _git(self.repo, "checkout", "-q", "-b", "m1", "main")
        _write(self.repo, "src/other.py", "x = 1\n")
        _commit_all(self.repo, "m1: non gelé")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "merge", "-q", "--no-ff", "m1", "-m",
             "Merge #101 — m1\n\nameesh-work: 101")
        head = _git(self.repo, "rev-parse", "HEAD")
        out = self.need({101: []}, head)
        self.assertFalse(out["ok"])
        self.assertIn("#101 has no frozen head", out["why"])

    def test_a_declared_only_landing_without_freeze_is_still_ignored(self):
        # `declared_only` garde son sens (#2775) : une intention d'atterrissage
        # sans source composée ni gel attend, elle ne refuse pas le véhicule.
        row, marks, freeze = self.freeze(103, [], declared_only=True)
        self.assertEqual(row["id"], 103)
        self.assertEqual(marks, [])
        self.assertIsNone(freeze)


class DeclaredLandingRefusalTest(_Base):
    """B/P2 (#224424) : un candidat refusé lève, même pour un landing déclaré.

    Un landing sans source composée ne doit pas faire sauter un membre dont le
    gel effectif est refusé (verdict bloqué, gel postérieur non reporté) : son
    blocage ne serait plus appliqué par la porte du véhicule.
    """

    def compose_a_clean_member(self):
        _git(self.repo, "checkout", "-q", "-b", "m1", "main")
        _write(self.repo, "src/app.py", "print(1)\n")
        self.m1 = _commit_all(self.repo, "m1: membre relu")
        _git(self.repo, "checkout", "-q", "main")
        self.head = _merge(self.repo, "m1", 101)
        self.marks = {101: [{"id": 1, "kind": "frozen", "sha": self.m1,
                             "at_ts": 1.0}]}

    def test_a_blocked_verdict_on_a_declared_landing_refuses_the_vehicle(self):
        self.compose_a_clean_member()
        _git(self.repo, "checkout", "-q", "-b", "m2", "main")
        _write(self.repo, "src/blocked.py", "x = 1\n")
        blocked = _commit_all(self.repo, "m2: tête bloquée")
        _git(self.repo, "checkout", "-q", "main")
        self.marks[102] = [
            {"id": 2, "kind": "frozen", "sha": blocked, "at_ts": 2.0},
            {"id": 3, "kind": "verdict", "verdict": "blocked", "sha": blocked,
             "actor": "rev2", "at_ts": 3.0},
        ]
        out = self.need(self.marks, self.head, landings=[{"work_item_id": 102}])
        self.assertFalse(out["ok"])
        self.assertIn("constituent #102", out["why"])
        self.assertIn("blocked-verdict", out["why"])

    def test_a_freeze_after_the_verdict_on_a_landing_refuses_the_vehicle(self):
        self.compose_a_clean_member()
        _git(self.repo, "checkout", "-q", "-b", "m2", "main")
        _write(self.repo, "src/late.py", "x = 1\n")
        reviewed = _commit_all(self.repo, "m2: tête relue")
        _write(self.repo, "src/late.py", "x = 2\n")
        frozen_after = _commit_all(self.repo, "m2: gelée après le verdict")
        _git(self.repo, "checkout", "-q", "main")
        self.marks[102] = [
            {"id": 2, "kind": "verdict", "verdict": "ok", "sha": reviewed,
             "actor": "rev1", "at_ts": 2.0},
            {"id": 3, "kind": "frozen", "sha": frozen_after, "at_ts": 3.0},
        ]
        out = self.need(self.marks, self.head, landings=[{"work_item_id": 102}])
        self.assertFalse(out["ok"])
        self.assertIn("constituent #102", out["why"])
        self.assertIn("freeze-after-verdict", out["why"])


class DeclaredAfterVerdictTest(_Base):
    """(3) un gel déclaré postérieur au verdict prime sur ce verdict."""

    def test_a_redeclared_head_after_the_verdict_wins(self):
        _git(self.repo, "checkout", "-q", "-b", "m1", "main")
        _write(self.repo, "src/app.py", "print(1)\n")
        self.declared = _commit_all(self.repo, "m1: tête")
        _write(self.repo, "src/app.py", "print(2)\n")
        reviewed = _commit_all(self.repo, "m1: correction relue")
        # re-gel de la même tête, après le verdict : le gel déclaré prime.
        marks = [
            {"id": 1, "kind": "frozen", "sha": self.declared, "at_ts": 1.0},
            {"id": 2, "kind": "verdict", "verdict": "ok", "sha": reviewed,
             "actor": "rev1", "at_ts": 2.0},
            {"id": 3, "kind": "frozen", "sha": reviewed, "at_ts": 3.0},
        ]
        _row, _marks, freeze = self.freeze(101, marks)
        self.assertEqual(freeze["sha"], reviewed)
        self.assertEqual(freeze["origin"], "frozen")

    def test_a_mechanically_rebased_head_after_the_verdict_wins(self):
        _git(self.repo, "checkout", "-q", "-b", "m1", "main")
        _write(self.repo, "src/app.py", "print(1)\n")
        self.declared = _commit_all(self.repo, "m1: tête")
        _write(self.repo, "src/app.py", "print(2)\n")
        reviewed = _commit_all(self.repo, "m1: correction relue")
        # même contenu, commit réécrit (rebase mécanique) puis gelé après le
        # verdict : le gel déclaré de la nouvelle tête prime.
        _git(self.repo, "commit", "-q", "--amend", "-m", "m1: correction relue (rebasée)")
        frozen_after = _git(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(frozen_after, reviewed)
        marks = [
            {"id": 1, "kind": "frozen", "sha": self.declared, "at_ts": 1.0},
            {"id": 2, "kind": "verdict", "verdict": "ok", "sha": reviewed,
             "actor": "rev1", "at_ts": 2.0},
            {"id": 3, "kind": "frozen", "sha": frozen_after, "at_ts": 3.0},
        ]
        _row, _marks, freeze = self.freeze(101, marks)
        self.assertEqual(freeze["sha"], frozen_after)
        self.assertEqual(freeze["origin"], "frozen")
        self.assertTrue(any(note.startswith("mechanical-rebase")
                            for note in freeze["notes"]))


if __name__ == "__main__":
    unittest.main()

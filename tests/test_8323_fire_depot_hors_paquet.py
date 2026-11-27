# SPDX-License-Identifier: AGPL-3.0-only
"""#8323 : le feu d'un lot dont la PR vise un dépôt hors paquet.

#8095 : lot nexlink (issue manaty/nexlink) dont la PR vit dans manaty-its et
vise son `main`. La garde du paquet (#2333) lisait le dépôt de l'issue et
refusait au nom du §11 bis, alors qu'aucun paquet ne porte manaty-its. Le
dépôt réel de la PR est celui que la marque du gel nomme
(`ameesh-freeze-repo`) ; seuls les dépôts des projets déclarés
(`github.projects`, `radar.projects`) passent par paquet, les autres reçoivent
les gardes du §2 (le feu les applique ensuite). Le doute refuse toujours.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import fire, packet_guard

A = "a" * 40
GITHUB = {"projects": {"ameesh": "manaty/ameesh",
                       "nexlink": {"repo": "manaty/nexlink", "base": ["develop"]}}}
ITEM = {"id": 8095, "title": "t", "state": "validated", "branch": "codex/home-server-console",
        "branch_target": "main", "issue_ref": "manaty/nexlink#4841", "app": "nexlink",
        "assignee": ""}


def _cfg(github=GITHUB):
    return types.SimpleNamespace(github=github)


def _freeze(repo: str = "manaty/manaty-its") -> dict:
    note = "PR manaty/manaty-its#8" + ("\nameesh-freeze-repo: %s" % repo if repo else "")
    return {"kind": "frozen", "sha": A, "actor": "codex1", "at_ts": 1.0, "id": 1,
            "note": note}


class PacketGuardRepositoryTest(unittest.TestCase):
    def test_a_repository_of_no_declared_project_gets_the_section_2_gates(self):
        for target in ("main", "develop"):
            with self.subTest(target=target):
                out = packet_guard.check_merge(None, 8095, "codex/home-server-console",
                                               target, A, repo="manaty/manaty-its",
                                               cfg=_cfg())
                self.assertTrue(out["allowed"])
                self.assertIn("§2", out["why"])
                self.assertFalse(out["journaled"])

    def test_the_packet_repositories_keep_the_refusal_on_every_integration_target(self):
        for repo, target in (("manaty/nexlink", "develop"), ("manaty/nexlink", "main"),
                             ("Manaty/Ameesh", "main")):
            with self.subTest(repo=repo, target=target), \
                    self.assertRaises(packet_guard.PacketGuardError) as caught:
                packet_guard.check_merge(None, 8095, "lot/8095-x", target, A,
                                         repo=repo, cfg=_cfg())
            self.assertIn("§11 bis", str(caught.exception))

    def test_the_doubt_refuses(self):
        cases = {"unreadable repository": ("", _cfg()),
                 "no declared project": ("manaty/manaty-its", _cfg({})),
                 "no cfg": ("manaty/manaty-its", None)}
        for label, (repo, cfg) in cases.items():
            with self.subTest(label), self.assertRaises(packet_guard.PacketGuardError):
                packet_guard.check_merge(None, 8095, "lot/8095-x", "main", A,
                                         repo=repo, cfg=cfg)


class FireRepositoryTest(unittest.TestCase):
    def test_the_freeze_mark_names_the_pr_repository(self):
        self.assertEqual(fire._slug(_cfg(), ITEM, None, [_freeze()]), "manaty/manaty-its")

    def test_without_a_mark_the_issue_still_names_it(self):
        for marks in (None, [], [_freeze("")]):
            with self.subTest(marks=marks):
                self.assertEqual(fire._slug(_cfg(), ITEM, None, marks), "manaty/nexlink")

    def _give(self, marks):
        with mock.patch.object(fire.work, "get", return_value=dict(ITEM)), \
                mock.patch.object(fire.work, "milestones", return_value=marks), \
                mock.patch.object(fire, "review_checkout", return_value=None), \
                mock.patch.object(fire.work, "milestone",
                                  side_effect=lambda db, lot, kind, **kw: dict(kw, kind=kind)):
            return fire._give_locked(None, 8095, sha=A, actor="orchestrateur", cfg=_cfg())

    def test_witness_a_nexlink_lot_whose_pr_targets_another_repository(self):
        row = self._give([_freeze()])
        self.assertEqual((row["kind"], row["sha"]), ("fire", A))

    def test_the_same_lot_frozen_in_nexlink_is_still_refused(self):
        for marks in ([_freeze("manaty/nexlink")], [_freeze("")]):
            with self.subTest(marks=marks), self.assertRaises(fire.FireError) as caught:
                self._give(marks)
            self.assertIn("§11 bis", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

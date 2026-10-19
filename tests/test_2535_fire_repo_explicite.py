# SPDX-License-Identifier: AGPL-3.0-only
"""#2535 : un `--repo` explicite qui n'est pas un checkout est refusé.

Relevé par ameesh-dev2 en relecture de #2473 (18/10, P3 non bloquant) : dans
`fire._act`, `_checkout(repo)` rendait None pour un `--repo` qui n'est pas un
checkout git, puis le passage retombait sans bruit sur le clone déclaré du
projet (`radar.projects.<p>.repo`). Le feu pouvait donc lire une autre copie
que celle demandée et rendre « prêt » (ou « neither local nor on origin »)
sans dire qu'il avait changé de dépôt.

Le témoin monte un vrai clone déclaré pour le projet, puis passe à `_act` un
`--repo` qui n'est pas un checkout (un dossier existant hors dépôt, puis un
chemin absent) : le feu refuse nommément, sans lire ni mettre en file le clone
du projet. Un `--repo` valide reste accepté.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import fire, merge_queue, radar, storage, work

from .test_2473_fire_readiness_repo import BRANCH, TARGET, _RepoCase
from .test_l160e_merge_queue import FakeQueue


class ExplicitRepoWitnessTest(_RepoCase):
    """`_act` ne remplace jamais un `--repo` invalide par un autre clone."""

    def _act(self, cfg, repo):
        lot = 42
        row = {"id": lot, "title": "paquet 68", "branch": BRANCH,
               "branch_target": TARGET, "assignee": "orchestrateur",
               "app": "nexlink", "issue_ref": "manaty/nexlink#1617", "state": "qa"}
        fake = FakeQueue([row])
        marks = [{"id": 1, "kind": "frozen", "sha": self.head, "at_ts": 1.0},
                 {"id": 2, "kind": "fire", "sha": self.head, "at_ts": 2.0,
                  "actor": "orchestrateur"}]
        store = mock.Mock(work=fake)
        green = {"number": 7, "green": True, "head": self.head, "draft": False}
        patches = [
            mock.patch.object(storage, "of", return_value=store),
            mock.patch.object(work, "get", side_effect=lambda db, n: fake.lots.get(n)),
            mock.patch.object(work, "milestones", side_effect=lambda db, n, *a: marks),
            mock.patch("ameesh.registry.get", return_value={"cwd": self.tmp}),
            mock.patch.object(fire, "_pr_status", return_value=None),
            mock.patch.object(fire, "_repo_info", return_value={}),
            mock.patch.object(fire, "_target", return_value=TARGET),
            mock.patch.object(fire, "ready_verdicts",
                              return_value=(True, "", {"class": "sensitive"})),
            mock.patch.object(fire, "_passive"),
            mock.patch.object(merge_queue, "_class_of",
                              return_value={"class": "sensitive", "why": "test"}),
            mock.patch.object(merge_queue, "review_gate",
                              return_value={"ok": True, "why": "", "required": 0,
                                            "waived": False}),
            mock.patch.object(radar, "github_ci",
                              return_value=lambda branch: dict(green)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        out = fire._act(cfg, None, row, marks, gh=lambda *a, **k: None,
                        dry_run=False, repo=repo, cache={})
        return out, fake

    def test_an_explicit_repo_that_is_not_a_checkout_is_refused(self):
        """Le pilote qui se trompe de `--repo` reçoit un refus nommé, pas le
        clone du projet lu à sa place."""
        cfg = self.config()
        for bad in (self.tmp, os.path.join(self.tmp, "absent")):
            with self.subTest(repo=bad):
                out, fake = self._act(cfg, bad)
                self.assertEqual(out["result"], "no-repository", out)
                self.assertIn("--repo", out["detail"])
                self.assertIn(bad, out["detail"])
                self.assertNotEqual(out.get("repo"), os.path.realpath(self.repo),
                                    "le clone déclaré du projet a été lu à la place")
                self.assertEqual(fake.queue_rows, {},
                                 "le clone déclaré du projet a été mis en file")

    def test_an_explicit_checkout_is_still_used(self):
        """Le refus ne doit pas écarter un `--repo` qui est bien un checkout."""
        cfg = self.config()
        out, fake = self._act(cfg, self.repo)
        self.assertEqual(out["result"], "queued", out)
        self.assertEqual(out["repo"], os.path.realpath(self.repo))
        self.assertEqual(fake.queue_rows[42]["frozen_sha"], self.head)


if __name__ == "__main__":
    unittest.main()

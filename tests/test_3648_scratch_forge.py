# SPDX-License-Identifier: AGPL-3.0-only
"""#3648 — le dépôt scratch de `merge-pr` rapatrie un gel antérieur écrasé.

Constat (mesh-design, 26/10 04:52Z) : `merge-pr 2819 --dry-run` refusait
« prior freeze 3a5bc588faac of lot #3581 is absent from the repository and
cannot be recovered » alors que la forge servait le SHA. Le scratch est un
`git init` sans `origin` (#2931) : la forge de #3147 n'y était jamais tentée.

Témoins :
* le scratch créé avec l'URL de lecture rapatrie le gel antérieur, par la
  lecture partagée comme par la porte d'avis (chemin des membres de paquet),
  sans recevoir de remote `origin` ;
* le scratch sans URL garde le refus fermé (le constat, #2924 B5) ;
* une forge qui ne sert pas l'objet garde le refus fermé ;
* `origin` reste la forge d'un dépôt qui en a un.
"""
from __future__ import annotations

import os
import shutil
from unittest import mock

from ameesh import merge_pr, merge_queue, packet_reviews, plan_git, review_provenance, work

from .test_1003_merge_pr import _Base
from .test_3147_prior_freeze_forge import FAMILIES, _ForgeFixture, _has, _run


class ScratchForgeTest(_ForgeFixture):
    def bare_scratch(self, forge: str = "") -> str:
        path = merge_pr._scratch(forge)
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def scratch(self, forge: str, *, url: bool = True) -> str:
        """Le scratch de la porte, rempli comme `merge_pr._fetch` le remplit."""
        path = self.bare_scratch(forge if url else "")
        _run(path, "fetch", "-q", "--no-tags", forge,
             "+refs/heads/main:refs/ameesh/merge-pr/target")
        return path

    def test_the_scratch_with_its_read_url_recovers_the_prior_freeze(self):
        scratch = self.scratch(self.forge("forge.git", serves=True))
        self.assertFalse(_has(scratch, self.head_a))
        authors, unknown, correctors = self.read(scratch)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})
        self.assertTrue(_has(scratch, self.head_a))
        # Aucun remote : les autres lectures du scratch ne changent pas.
        self.assertEqual(_run(scratch, "remote"), "")
        self.assertEqual(_run(scratch, "rev-parse", "refs/ameesh/merge-pr/target"),
                         self.head_b)

    def test_the_review_gate_path_recovers_in_the_scratch(self):
        # Le chemin du constat : `packet_reviews` lit la porte de chaque membre
        # par `merge_queue.review_gate` sur le scratch, sans `recover`.
        scratch = self.scratch(self.forge("forge-gate.git", serves=True))
        authors, _correctors = merge_queue._attribution_on_freeze(
            self.db, work.milestones(self.db, self.lot), self.head_b,
            repo=scratch, klass={"base_sha": self.base}, item=self.lot_item(),
            families=FAMILIES, strict=True)
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})

    def test_the_vehicle_head_fetch_uses_the_scratch_forge(self):
        scratch = self.scratch(self.forge("forge-head.git", serves=True))
        self.assertTrue(packet_reviews._fetch_head(scratch, self.head_a))
        self.assertTrue(_has(scratch, self.head_a))

    def test_a_scratch_without_url_keeps_the_closed_refusal(self):
        scratch = self.scratch(self.forge("forge-sans-url.git", serves=True), url=False)
        with self.assertRaises(review_provenance.ProvenanceError) as caught:
            self.read(scratch)
        self.assertIn("cannot be recovered", str(caught.exception))
        self.assertFalse(_has(scratch, self.head_a))

    def test_a_forge_that_does_not_serve_the_freeze_keeps_the_closed_refusal(self):
        scratch = self.scratch(self.forge("forge-absente.git", serves=False))
        with self.assertRaises(review_provenance.ProvenanceError) as caught:
            self.read(scratch)
        self.assertIn("cannot be recovered", str(caught.exception))

    def test_origin_stays_the_forge_of_a_clone(self):
        forge = self.forge("forge-origin.git", serves=True)
        clone = self.clone(forge, "clone")
        _run(clone, "config", plan_git.FORGE_CONFIG_KEY, os.path.join(self.tmp, "ailleurs"))
        self.assertEqual(plan_git.forge_remote(clone), "origin")
        self.assertEqual(plan_git.forge_remote(self.bare_scratch(forge)), forge)
        self.assertIsNone(plan_git.forge_remote(self.bare_scratch()))


class DirectMergeScratchForgeTest(_Base):
    """#3656 : `direct_merge` donne sa forge au scratch de la porte.

    Le correctif #3648 pose `ameesh.forge` dans `_scratch(fetch_url)` ; les
    témoins ci-dessus couvrent le scratch seul et la lecture partagée, pas le
    câblage du geste. Ici `direct_merge` complet (dry-run, vraies gardes, faux
    `gh`) crée son scratch et le premier `_fetch` observe son `ameesh.forge` :
    sans l'argument, il est vide et le test rougit.
    """

    def test_direct_merge_gives_its_scratch_the_read_forge(self):
        observed = {}
        real_fetch = merge_pr._fetch

        def fetch(owner, fetch_url, *args, **kwargs):
            observed["forge"] = plan_git.forge_remote(owner)
            return real_fetch(owner, fetch_url, *args, **kwargs)

        with mock.patch.object(merge_pr, "_fetch", side_effect=fetch):
            out = self.merge(self.gh(), dry_run=True)
        self.assertEqual(out["state"], "dry-run", out)
        self.assertEqual(observed.get("forge"), self.origin)

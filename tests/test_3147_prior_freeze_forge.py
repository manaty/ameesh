# SPDX-License-Identifier: AGPL-3.0-only
"""#3147 — un gel antérieur absent se demande à la forge par SHA exact.

Constat : `review_authors.reading(..., strict=True)` refusait #3012, #3115,
#3116, #2994 et #2914 avec « prior freeze … absent from the repository and
cannot be recovered », alors que `git fetch <forge> <sha>` sert encore le
commit après le push forcé. Le même lot était lisible dans un clone qui avait
gardé les objets et illisible dans le clone de la veille : le résultat de la
porte dépendait du clone, pas des faits.

Témoins :
* un clone sans l'objet + une forge factice qui le sert par SHA → lecture
  normale, sans `recover` d'appelant (la forge est toujours tentée) ;
* la même forge sans l'objet → refus fermé inchangé (#2924 B5) ;
* la porte (`_attribution_on_freeze`, chemin de packet_candidates,
  packet_watch, fire et review_gate) et le train (`review_authors.authors`)
  récupèrent aussi, sans hook ;
* un dépôt sans `origin` (pas de forge) garde le refus fermé.
"""
from __future__ import annotations

import os
import subprocess

from ameesh import merge_queue, review_authors, review_provenance, work

from .test_2924_history_rewrite_provenance import FAMILIES, _RewriteFixture


def _run(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr.strip()))
    return proc.stdout.strip()


def _has(repo: str, sha: str) -> bool:
    """Le commit est-il lisible **localement** dans `repo` (aucun fetch) ?"""
    proc = subprocess.run(
        ["git", "-C", repo, "cat-file", "-e", sha + "^{commit}"],
        capture_output=True)
    return proc.returncode == 0


class _ForgeFixture(_RewriteFixture):
    """Tête B (squash) dont le gel antérieur A n'est pas dans un clone neuf."""

    def setUp(self):
        super().setUp()
        parent = self.repo.git("rev-parse", self.head_a + "^")
        tree = self.repo.git("rev-parse", self.head_a + "^{tree}")
        self.base = parent
        self.head_b = self.squash(tree, parent, "contenu consolidé")
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def forge(self, name: str, *, serves: bool) -> str:
        """Une forge factice : `main` = tête B, et (si `serves`) l'objet de A
        sans aucune ref, comme après un push forcé."""
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        _run(root, "init", "-q", "--bare")
        self.repo.git("push", "-q", root, "%s:refs/heads/main" % self.head_b)
        if serves:
            # L'objet reste dans la forge sans ref publiée : la forge le sert
            # par SHA exact (allowAnySHA1InWant), comme GitHub.
            self.repo.git("push", "-q", root, "%s:refs/heads/ghost" % self.head_a)
            _run(root, "update-ref", "-d", "refs/heads/ghost")
            _run(root, "config", "uploadpack.allowAnySHA1InWant", "true")
        return root

    def clone(self, forge: str, name: str) -> str:
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        _run(root, "init", "-q")
        _run(root, "remote", "add", "origin", forge)
        _run(root, "fetch", "-q", "origin", "main")
        return root

    def read(self, repo: str, *, strict: bool = True, fetch: bool = True):
        return review_authors.reading(
            self.db, self.lot_item(), self.milestone, [self.head_b], repo,
            FAMILIES, base=self.base, head=self.head_b, strict=strict,
            fetch=fetch)


class PriorFreezeForgeRecoveryTest(_ForgeFixture):
    def test_a_forge_that_serves_the_freeze_makes_the_reading_normal(self):
        scratch = self.clone(self.forge("forge.git", serves=True), "scratch")
        self.assertFalse(_has(scratch, self.head_a),
                         "le clone de lecture doit partir sans l'objet")
        authors, unknown, correctors = self.read(scratch)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})
        # La récupération a bien ajouté l'objet au clone, sans changer la ref
        # de lecture (aucune tête déplacée par le fetch par SHA).
        self.assertTrue(_has(scratch, self.head_a))
        self.assertEqual(_run(scratch, "rev-parse", "refs/remotes/origin/main"),
                         self.head_b)

    def test_the_review_gate_path_recovers_without_a_caller_hook(self):
        # packet_candidates / packet_watch / fire passent par
        # `merge_queue.review_gate` → `_attribution_on_freeze`, qui n'a jamais
        # fourni de `recover` : c'est le chemin du constat.
        scratch = self.clone(self.forge("forge-gate.git", serves=True),
                             "scratch-gate")
        authors, _correctors = merge_queue._attribution_on_freeze(
            self.db, work.milestones(self.db, self.lot), self.head_b,
            repo=scratch, klass={"base_sha": self.base}, item=self.lot_item(),
            families=FAMILIES, strict=True)
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})

    def test_the_train_reader_recovers_without_a_caller_hook(self):
        # train_history (Preparation) appelle `review_authors.authors`
        # directement, sans `recover`.
        scratch = self.clone(self.forge("forge-train.git", serves=True),
                             "scratch-train")
        authors, unknown = review_authors.authors(
            self.db, self.lot_item(), self.milestone, [self.head_b], scratch,
            FAMILIES, base=self.base, head=self.head_b, strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})

    def test_a_forge_that_does_not_serve_the_freeze_keeps_the_closed_refusal(self):
        scratch = self.clone(self.forge("forge-absente.git", serves=False),
                             "scratch-absente")
        with self.assertRaises(review_provenance.ProvenanceError) as caught:
            self.read(scratch)
        self.assertIn("prior freeze", str(caught.exception))
        self.assertIn("cannot be recovered", str(caught.exception))
        authors, unknown, _correctors = self.read(scratch, strict=False)
        self.assertEqual(unknown, [self.head_a[:12]])
        self.assertNotIn("cx3", {row["name"] for row in authors})

    def test_a_repository_without_a_forge_keeps_the_closed_refusal(self):
        orphan = os.path.join(self.tmp, "orphan")
        os.makedirs(orphan)
        _run(orphan, "init", "-q")
        _run(orphan, "fetch", "-q", self.repo.root, self.head_b)
        self.assertFalse(_has(orphan, self.head_a))
        with self.assertRaises(review_provenance.ProvenanceError):
            self.read(orphan)

    def test_fetch_false_never_reaches_the_forge(self):
        # #3836 : la lecture du gel tourne dans la transaction qui tient les
        # verrous du lot et du dépôt de migration. Même quand la forge sert
        # l'objet, elle ne doit pas être appelée : un `git fetch` de 180 s y
        # laissait la session `idle in transaction` (incident du 27/10).
        scratch = self.clone(self.forge("forge-no-fetch.git", serves=True),
                             "scratch-no-fetch")
        self.assertFalse(_has(scratch, self.head_a))
        authors, unknown, _correctors = self.read(scratch, strict=False,
                                                  fetch=False)
        self.assertEqual(unknown, [self.head_a[:12]])
        self.assertFalse(_has(scratch, self.head_a),
                         "une lecture de transaction ne doit pas fetcher")
        self.assertNotIn("cx3", {row["name"] for row in authors})

    def test_prefetch_before_the_transaction_recovers_the_same_reading(self):
        # #3836 : le rapatriement se fait hors transaction, avant le gel ; la
        # lecture `fetch=False` retrouve alors les mêmes auteurs qu'avant.
        scratch = self.clone(self.forge("forge-prefetch.git", serves=True),
                             "scratch-prefetch")
        self.assertFalse(_has(scratch, self.head_a))
        fetched = review_authors.prefetch_prior_freezes(
            self.db, self.lot_item(), scratch)
        self.assertEqual(fetched, [self.head_a])
        self.assertTrue(_has(scratch, self.head_a))
        authors, unknown, _correctors = self.read(scratch, strict=False,
                                                  fetch=False)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})

# SPDX-License-Identifier: AGPL-3.0-only
"""#3835 — le refus nomme le gel antérieur, son SHA et le lot, et dit un délai.

Sur le paquet Nexlink 197, `queue add` a refusé #3699 avec « the authors of the
frozen range cannot be read » et un SHA nu (`a410a7ac5ad4`) : l'orchestrateur a
purgé des refs et regelé deux fois pour rien. Le SHA venait d'un **gel
antérieur** du lot, non marqué `--supersedes` ; un délai de `ref_reachable`
(30 s) rendait `None`, faisait relire ce gel en strict, et le refus ne disait
pas que la cause était un délai.

Le refus doit nommer le gel (`prior freeze #N sha … of lot #L`) et ne proposer
`--supersedes` que pour un gel dont l'absence de toute ref est **prouvée**. Un
délai se dit comme tel, avec le remède adapté (réessayer), jamais comme un
dépôt illisible.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from unittest import TestCase, mock

from ameesh import merge_queue, plan_git, review_authors, work
from ameesh.canon import GitError

from .test_2447_review_designation_activation import _Repo
from .test_2924_history_rewrite_provenance import FAMILIES, _RewriteFixture
from .test_3399_dangling_supersedes import _DanglingSupersededFreezeFixture


class RefReachableFastPathTest(TestCase):
    """#3835 : `name-rev` d'abord, `for-each-ref --contains` en référence."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ameesh-3835-refs-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = _Repo(self.root)
        self.repo.write("f", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("f", "base\nnext\n")
        self.head = self.repo.commit("head")
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("f", "base\nother\n")
        self.dangling = self.repo.commit("dangling")
        self.repo.git("reset", "--hard", self.base)
        self.repo.git("branch", "keep", self.head)

    def _proc(self, args, code=0, out=b""):
        return subprocess.CompletedProcess(args, code, stdout=out, stderr=b"")

    def test_the_fast_path_reads_all_local_refs(self):
        self.assertTrue(plan_git.ref_reachable(self.repo.root, self.head))
        self.assertFalse(plan_git.ref_reachable(self.repo.root, self.dangling))

    def test_an_annotated_tag_and_a_custom_ref_agree_with_for_each_ref(self):
        self.repo.git("tag", "-a", "v1", "-m", "release", self.head)
        self.repo.git("update-ref", "refs/archive/x", self.dangling)
        for sha in (self.head, self.dangling):
            raw = bool(self.repo.git(
                "for-each-ref", "--contains=" + sha, "--format=%(refname)"))
            self.assertEqual(
                plan_git.ref_reachable(self.repo.root, sha), raw, sha)
        self.assertTrue(plan_git.ref_reachable(self.repo.root, self.dangling))

    def test_a_ref_named_undefined_does_not_read_as_the_sentinel(self):
        # `name-rev` rend `undefined` pour la sentinelle, mais aussi pour une
        # ref dont le nom court est exactement `undefined` : le chemin rapide
        # ne doit pas la confondre avec l'absence.
        self.repo.git("update-ref", "refs/heads/undefined", self.dangling)
        self.assertTrue(plan_git.ref_reachable(self.repo.root, self.dangling))

    def test_the_fast_path_is_used_before_for_each_ref(self):
        seen = []

        def fake(repo, *args, **kwargs):
            seen.append(args[0])
            if args[0] == "name-rev":
                return self._proc(("git",) + args, 0, b"keep~1\n")
            raise AssertionError("for-each-ref must not be the first reader")

        with mock.patch.object(plan_git, "_git", side_effect=fake):
            self.assertTrue(plan_git.ref_reachable(self.repo.root, self.dangling))
        self.assertEqual(seen[0], "name-rev")

    def test_a_timeout_is_reported_as_a_timeout(self):
        why = []
        with mock.patch.object(
                plan_git, "_git",
                side_effect=GitError("git name-rev: timed out")):
            self.assertIsNone(
                plan_git.ref_reachable(self.repo.root, self.head, why=why))
        self.assertEqual(why, ["timeout"])

    def test_an_unreadable_git_is_not_called_a_timeout(self):
        why = []
        with mock.patch.object(
                plan_git, "_git",
                side_effect=GitError("git name-rev: bad revision")):
            self.assertIsNone(
                plan_git.ref_reachable(self.repo.root, "0" * 40, why=why))
        self.assertEqual(why, ["git name-rev: bad revision"])


class UnknownRefusalTextTest(TestCase):
    """Le texte du refus, sans base : groupement et motifs."""

    def test_two_freezes_share_one_supersedes_command(self):
        first = review_authors.PriorFreezeUnknown(
            "a410a7ac5ad4", freeze_id=20009,
            freeze_sha="0610094b3df8fa4df2ab2fcb0e5478302ee03914", lot=3699,
            reachable=False, repo="/clone")
        second = review_authors.PriorFreezeUnknown(
            "a410a7ac5ad4", freeze_id=20066,
            freeze_sha="ef279c071293027af50e3c45d84788081d3cbc7f", lot=3699,
            reachable=False, repo="/clone")
        text = review_authors.unknown_refusal(
            [first, second], head_sha="66274f581c959ca5c59f4cf6fe5096bfe44a691c")
        self.assertIn("prior freeze #20009", text)
        self.assertIn("prior freeze #20066", text)
        self.assertIn("sha 0610094b3df8", text)
        self.assertIn("of lot #3699", text)
        self.assertIn(
            "ameesh work milestone 3699 frozen --sha "
            "66274f581c959ca5c59f4cf6fe5096bfe44a691c "
            "--supersedes 20009 --supersedes 20066", text)

    def test_a_doubt_is_not_presented_as_replaceable(self):
        value = review_authors.PriorFreezeUnknown(
            "a410a7ac5ad4", freeze_id=20009,
            freeze_sha="0610094b3df8fa4df2ab2fcb0e5478302ee03914", lot=3699,
            reachable=None, reason="timeout", repo="/clone")
        text = review_authors.unknown_refusal([value], head_sha="66" * 20)
        self.assertIn("timed out after 30s in /clone", text)
        self.assertNotIn("ameesh work milestone", text)


class PriorFreezeRefusalTest(_RewriteFixture):
    """Un gel antérieur pendant dont l'inconnu bloque la porte."""

    def setUp(self):
        super().setUp()
        # un commit tiers sans reçu dans la plage du gel A
        self.repo.write("src/ameesh/runner.py", "base\nlot\noperator fix\n")
        self.op = self.repo.commit("operator: fix", agent="operator")
        self.op_freeze = self.freeze(self.op)
        self.tree = self.repo.git("rev-parse", self.op + "^{tree}")
        # force-push : le gel A et son commit tiers deviennent pendants
        self.repo.git("reset", "--hard", self.base)
        self.assertEqual(
            self.repo.git("for-each-ref", "--contains", self.op,
                          "--format=%(refname)"), "")
        self.head_b = self.squash(self.tree, self.base, "contenu réécrit")
        self.repo.git("reset", "--hard", self.head_b)
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {
            "id": int(self.freeze_b["id"]), "sha": self.head_b,
            "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def klass(self):
        return {"class": "sensitive", "doubt": False,
                "files": [{"path": "src/ameesh/runner.py",
                           "class": "sensitive",
                           "rule": ["src/ameesh/runner.py"]}],
                "repo": self.repo.root, "target": self.base,
                "base_sha": self.base}

    def read(self, *, strict=True):
        commits = review_authors._commits_between(
            self.repo.root, self.base, self.head_b)
        return review_authors.reading(
            self.db, self.lot_item(), self.milestone, commits,
            self.repo.root, FAMILIES, base=self.base, head=self.head_b,
            strict=strict)

    def complete_authors(self):
        return merge_queue._attribution_on_freeze(
            self.db, work.milestones(self.db, self.lot), self.head_b,
            repo=self.repo.root, klass=self.klass(), item=self.lot_item(),
            strict=True)

    def test_the_unknown_carries_its_freeze_sha_and_lot(self):
        _authors, unknown, _correctors = self.read()
        self.assertEqual(len([value for value in unknown
                              if isinstance(value, review_authors.PriorFreezeUnknown)]),
                         1, unknown)
        value = next(value for value in unknown
                     if isinstance(value, review_authors.PriorFreezeUnknown))
        self.assertEqual(value.freeze_id, int(self.op_freeze["id"]))
        self.assertEqual(value.freeze_sha, self.op)
        self.assertEqual(value.lot, self.lot)
        self.assertIs(value.reachable, False)

    def test_the_refusal_names_the_prior_freeze_and_the_lot(self):
        with self.assertRaises(ValueError) as caught:
            self.complete_authors()
        text = str(caught.exception)
        self.assertIn("prior freeze #%d" % int(self.op_freeze["id"]), text)
        self.assertIn(self.op[:12], text)
        self.assertIn("of lot #%d" % self.lot, text)
        self.assertIn("no local ref contains", text)
        self.assertIn(
            "ameesh work milestone %d frozen --sha %s --supersedes %d"
            % (self.lot, self.head_b, int(self.op_freeze["id"])), text)

    def test_a_reachable_prior_freeze_is_not_offered_for_replacement(self):
        self.repo.git("branch", "keep/old-freeze", self.op)
        with self.assertRaises(ValueError) as caught:
            self.complete_authors()
        text = str(caught.exception)
        self.assertIn("still reachable from a ref", text)
        self.assertNotIn("frozen --sha %s --supersedes" % self.head_b, text)

    def test_a_timeout_is_said_as_such_and_does_not_offer_supersedes(self):
        def timeout(repo, rev, *, timeout=None, why=None):
            if why is not None:
                why.append("timeout")
            return None

        with mock.patch.object(plan_git, "ref_reachable", side_effect=timeout):
            with self.assertRaises(ValueError) as caught:
                self.complete_authors()
        text = str(caught.exception)
        self.assertIn("timed out after %gs" % plan_git.REACHABLE_TIMEOUT_S, text)
        self.assertIn("accessibility not established", text)
        self.assertNotIn("frozen --sha %s --supersedes" % self.head_b, text)

    def test_superseding_the_proven_ghost_unblocks_the_reading(self):
        row = self.freeze(self.head_b, supersedes=[self.op_freeze["id"]])
        self.milestone = {"id": int(row["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": row.get("at_ts")}
        authors, unknown, _correctors = self.read()
        self.assertEqual(unknown, [])
        self.assertIn("impl", {row["name"] for row in authors})

    def test_the_gate_why_names_the_prior_freeze(self):
        # deux avis ok pour franchir le compte requis et atteindre la lecture
        self.verdict("cx3", self.head_b)
        self.verdict("dev1", self.head_b)
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.head_b,
            repo=self.repo.root, klass=self.klass(), item=self.lot_item(),
            required=2)
        self.assertFalse(out["ok"], out)
        self.assertIn("prior freeze #%d" % int(self.op_freeze["id"]),
                      out["why"])
        self.assertIn("ameesh work milestone %d frozen" % self.lot, out["why"])


class SupersedesTimeoutTest(_DanglingSupersededFreezeFixture):
    """#3835 : `--supersedes` dit un délai de `ref_reachable`, pas un dépôt cassé."""

    def test_a_timeout_in_supersedes_is_reported_as_a_timeout(self):
        def timeout(repo, rev, *, timeout=None, why=None):
            if why is not None:
                why.append("timeout")
            return None

        with mock.patch.object(plan_git, "ref_reachable", side_effect=timeout):
            with self.assertRaises(work.WorkError) as caught:
                self.supersede()
        text = str(caught.exception)
        self.assertIn("timed out after %gs" % plan_git.REACHABLE_TIMEOUT_S, text)
        self.assertIn("retry once the repository answers", text)
        self.assertNotIn("refs cannot be read", text)

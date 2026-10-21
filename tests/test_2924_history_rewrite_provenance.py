# SPDX-License-Identifier: AGPL-3.0-only
"""#2924 — une réécriture d'historique à contenu identique garde les auteurs.

Cas réel #286 (`40192bd5ff4a`) : un commit unique signé deepseek reprend
l'arbre exact de la tête antérieure `1f827402b` fusionnée avec main. Les reçus
commit par commit ne voient plus que le nouveau commit, donc `review_authors`
oublie les correcteurs codex et claude : leurs familles redeviennent
« indépendantes » et un correcteur pourrait relire son propre code.

Le lecteur relit les gels antérieurs du même lot et reprend leurs auteurs tant
que leur contenu subsiste : arbre identique (ou fusion du gel avec la base),
ancêtre, ou au moins une ligne significative encore présente. Un correctif
réellement réécrit n'est pas repris (cas admissible, documenté par le gel) ; le
doute conserve l'auteur.

Témoin obligatoire : tête A (auteur deepseek + correctif codex), puis tête B =
squash à arbre identique signé deepseek → codex reste auteur, son avis est
refusé. Une réécriture réelle de son code le retire des auteurs.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from unittest import TestCase, mock

from ameesh import merge_queue, registry, review_authors, work
from ameesh import review_provenance

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")
LOT = {"id": 2924, "title": "réécriture #2924"}


class _RewriteFixture(PgTestCase):
    """Un lot gelé avec un auteur deepseek et un correcteur codex."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        for name, harness in (("impl", "deepseek"), ("cx3", "codex"),
                              ("dev1", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.item = work.add(self.db, title="réécriture #2924", app="ameesh",
                             assignee="impl")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")
        self.db.execute("UPDATE work_items SET reviewers = ARRAY['cx3']"
                        " WHERE id = %s", (self.lot,))
        self._build_head_a()

    def env(self, agent: str) -> dict:
        address = "%s@ameesh.invalid" % agent
        return dict(self.repo.dry, GIT_AUTHOR_NAME=agent,
                    GIT_AUTHOR_EMAIL=address, GIT_COMMITTER_NAME=agent,
                    GIT_COMMITTER_EMAIL=address)

    def receipt(self, actor, harness, commits):
        for sha in commits:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
                 self.lot))

    def freeze(self, sha):
        return work.milestone(self.db, self.lot, "frozen", sha=sha,
                              actor="impl", repo=self.repo.root)

    def _build_head_a(self):
        """Base, commit de l'auteur (deepseek), deux correctifs codex."""
        self.repo.write("src/ameesh/runner.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/runner.py", "base\nlot\n")
        self.author = self.repo.commit("impl: lot", agent="impl")
        self.receipt("impl", "deepseek", [self.author])
        self.repo.write("src/ameesh/runner.py",
                        "base\nlot\n"
                        "if not admitted:\n"
                        "    return deferred  # cx3 fix\n")
        self.fix = self.repo.commit("cx3: garde d'admission", agent="cx3")
        self.receipt("cx3", "codex", [self.fix])
        self.repo.write("tests/test_runner.py",
                        "def test_admission():\n"
                        "    assert admitted() is False  # cx3 witness\n")
        self.witness = self.repo.commit("cx3: témoin d'admission", agent="cx3")
        self.receipt("cx3", "codex", [self.witness])
        self.head_a = self.witness
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.freeze(self.head_a)

    def squash(self, tree: str, parent: str, message: str) -> str:
        sha = self.repo.git("commit-tree", tree, "-p", parent, "-m", message,
                            env=self.env("impl"))
        self.receipt("impl", "deepseek", [sha])
        return sha

    def lot_item(self):
        return work.get(self.db, self.lot)

    def reading(self, milestone, commits, base, head):
        return review_authors.reading(
            self.db, self.lot_item(), milestone, commits, self.repo.root,
            FAMILIES, base=base, head=head)


class IdenticalRewriteTest(_RewriteFixture):
    """Tête B = squash à arbre identique : codex reste auteur (témoin)."""

    def setUp(self):
        super().setUp()
        parent = self.repo.git("rev-parse", self.head_a + "^")
        tree = self.repo.git("rev-parse", self.head_a + "^{tree}")
        self.head_b = self.squash(tree, parent, "contenu consolidé")
        self.freeze_b = self.freeze(self.head_b)
        self.base = parent

    def test_the_corrector_stays_an_author_of_the_squashed_head(self):
        milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                     "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}
        authors, unknown, correctors = self.reading(
            milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})

    def test_the_designation_reads_the_same_carried_authors(self):
        from ameesh import review_designation_activation as act

        milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                     "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}
        authors, unknown, _correctors = act.attribution_of_head(
            self.db, self.lot_item(), milestone, self.repo.root,
            self.base, self.head_b, families=FAMILIES)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})

    def test_the_corrector_verdict_is_refused_on_the_squashed_head(self):
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/runner.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/runner.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        for actor in ("impl", "cx3", "dev1"):
            work.milestone(self.db, self.lot, "verdict", sha=self.head_b,
                           actor=actor, verdict="ok")
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.head_b,
            repo=self.repo.root, klass=klass, item=self.lot_item(), required=2)
        self.assertFalse(out.get("error"), out)
        self.assertFalse(out["ok"], out["why"])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["dev1"], out)
        self.assertIn("cx3", {row["name"] for row in out["authors"]})
        self.assertIn("2 independent review verdict(s) required", out["why"])


class RealCaseMergeTest(_RewriteFixture):
    """Cas réel : B = fusion de A avec une cible avancée, signée deepseek."""

    def test_a_consolidation_on_an_advanced_target_stays_attributed(self):
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/other.py", "main\n")
        main = self.repo.commit("main: avance", agent="impl")
        self.receipt("impl", "deepseek", [main])
        tree = self.repo.git("merge-tree", "--write-tree", self.head_a, main)
        head_b = self.squash(tree.splitlines()[0], main,
                             "contenu consolidé sur main")
        freeze_b = self.freeze(head_b)
        milestone = {"id": int(freeze_b["id"]), "sha": head_b,
                     "actor": "impl", "at_ts": freeze_b.get("at_ts")}
        authors, unknown, _correctors = self.reading(
            milestone, [head_b], main, head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})


class RealRewriteTest(_RewriteFixture):
    """Un correctif réellement réécrit n'est plus attribué à son correcteur."""

    def rewrite(self):
        # Une vraie réécriture d'historique : la tête C ne descend pas de A,
        # elle repart de la base avec un contenu différent (cas admissible).
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/runner.py",
                        "base\nlot\n"
                        "def _interrupt_admitted():\n"
                        "    return rewrite_is_admitted()  # impl rewrite\n")
        self.repo.write("tests/test_runner.py",
                        "def test_rewrite():\n"
                        "    assert rewrite_is_admitted()\n")
        self.head_c = self.repo.commit("impl: corrections réécrites",
                                       agent="impl")
        self.receipt("impl", "deepseek", [self.head_c])
        self.freeze_c = self.freeze(self.head_c)
        self.milestone = {"id": int(self.freeze_c["id"]), "sha": self.head_c,
                          "actor": "impl", "at_ts": self.freeze_c.get("at_ts")}

    def test_the_rewritten_correction_leaves_the_authors(self):
        self.rewrite()
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_c], self.base, self.head_c)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek"})
        self.assertEqual(correctors, [])

    def test_the_two_families_can_review_the_rewritten_head(self):
        self.rewrite()
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/runner.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/runner.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        for actor in ("cx3", "dev1"):
            work.milestone(self.db, self.lot, "verdict", sha=self.head_c,
                           actor=actor, verdict="ok")
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.head_c,
            repo=self.repo.root, klass=klass, item=self.lot_item(), required=2)
        self.assertFalse(out.get("error"), out)
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual({row["actor"] for row in out["accepted"]},
                         {"cx3", "dev1"})

    def test_a_modified_comment_keeps_the_corrector(self):
        # #2924 B2 : une ligne/commentaire modifié ne suffit pas à déclarer le
        # commit réécrit ; les autres lignes du correctif subsistent.
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/runner.py",
                        "base\nlot\n"
                        "if not admitted:\n"
                        "    return deferred  # cx3 fix, commentaire ajusté\n")
        self.repo.write("tests/test_runner.py",
                        "def test_admission():\n"
                        "    assert admitted() is False  # cx3 witness\n")
        head = self.repo.commit("impl: commentaire ajusté", agent="impl")
        self.receipt("impl", "deepseek", [head])
        freeze = self.freeze(head)
        milestone = {"id": int(freeze["id"]), "sha": head,
                     "actor": "impl", "at_ts": freeze.get("at_ts")}
        authors, unknown, _correctors = self.reading(
            milestone, [head], self.base, head)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"] for row in authors}, {"impl", "cx3"})

    def test_an_unrelated_change_keeps_the_corrector(self):
        # Même contenu que A (les lignes de cx3), plus un fichier sans rapport :
        # le contenu du correctif subsiste, il reste auteur.
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/runner.py",
                        "base\nlot\n"
                        "if not admitted:\n"
                        "    return deferred  # cx3 fix\n")
        self.repo.write("tests/test_runner.py",
                        "def test_admission():\n"
                        "    assert admitted() is False  # cx3 witness\n")
        self.repo.write("docs/note.md", "documentation sans rapport\n")
        head = self.repo.commit("impl: documentation", agent="impl")
        self.receipt("impl", "deepseek", [head])
        freeze = self.freeze(head)
        milestone = {"id": int(freeze["id"]), "sha": head,
                     "actor": "impl", "at_ts": freeze.get("at_ts")}
        authors, unknown, _correctors = self.reading(
            milestone, [head], self.base, head)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"] for row in authors}, {"impl", "cx3"})


class ParserTest(TestCase):
    """#2924 B1 : chaque fichier du diff garde ses propres lignes."""

    def test_two_files_do_not_share_a_path(self):
        root = tempfile.mkdtemp(prefix="ameesh-2924-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        repo = _Repo(root)
        repo.write("alpha.py", "base\n")
        repo.write("docs/note.md", "doc\n")
        repo.commit("base")
        repo.write("alpha.py", "base\nfix alpha\n")
        repo.write("docs/note.md", "doc\ndoc beta\n")
        sha = repo.commit("cx3: correctif + doc", agent="cx3")
        adds, _removes, binary = review_authors._commit_lines(repo.root, sha)
        self.assertFalse(binary)
        self.assertIn("fix alpha", adds["alpha.py"])
        self.assertIn("doc beta", adds["docs/note.md"])
        self.assertNotIn("doc beta", adds.get("alpha.py", []))
        self.assertNotIn("fix alpha", adds.get("docs/note.md", []))


class FreezeReadFailureTest(TestCase):
    """#2924 B3 : un journal de jalons illisible ne rend jamais une liste vide."""

    def test_unreadable_freezes_fail_closed_when_strict(self):
        with mock.patch.object(review_authors.storage, "of",
                               side_effect=RuntimeError("journal illisible")):
            with self.assertRaises(review_provenance.ProvenanceError):
                review_authors._carried_from_previous(
                    object(), dict(LOT), None, [], "/depot", FAMILIES,
                    strict=True)
            authors, unknown, correctors = review_authors._carried_from_previous(
                object(), dict(LOT), None, [], "/depot", FAMILIES,
                strict=False)
        self.assertEqual((authors, correctors), ([], []))
        self.assertEqual(unknown, ["milestones:2924"])


class PacketIsNotWidenedTest(TestCase):
    """Un véhicule de paquet ne relit pas les gels des membres (#2803)."""

    def test_a_packet_branch_carries_nothing(self):
        item = dict(LOT, branch="integration/paquet-h-20261021-0200",
                    assignee="composer")
        out = review_authors._carried_from_previous(
            object(), item, None, [], "/tmp", FAMILIES)
        self.assertEqual(out, ([], [], []))

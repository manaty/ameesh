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

from ameesh import merge_queue, registry, review_authors, storage, work
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

    def receipt(self, actor, harness, commits, lot=None):
        for sha in commits:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
                 self.lot if lot is None else lot))

    def freeze(self, sha, **kwargs):
        return work.milestone(self.db, self.lot, "frozen", sha=sha,
                              actor="impl", repo=self.repo.root, **kwargs)

    def verdict(self, actor, sha, verdict="ok"):
        """Pose un verdict avec son reçu exact (#2928, classe sensible)."""
        mark = work.milestone(self.db, self.lot, "verdict", sha=sha,
                              actor=actor, verdict=verdict)
        harness = {"impl": "deepseek", "cx3": "codex",
                   "dev1": "claude"}.get(actor, "deepseek")
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), sha, actor, "s-" + harness,
             "t", harness, self.lot))
        return mark

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
            self.verdict(actor, self.head_b)
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
            self.verdict(actor, self.head_c)
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


class MovedCorrectionTest(_RewriteFixture):
    """#2924 B8 : un fichier déplacé ne prouve pas une réécriture.

    Le gel B repart de la base et consolide exactement le même source et le
    même test sous des noms déplacés. Le correcteur a écrit ces lignes : son
    contenu n'a pas été réécrit, seule son adresse a changé. La preuve de
    survie ne doit pas chercher uniquement l'ancien chemin ; sans preuve de
    suppression, le doute conserve l'auteur.
    """

    def setUp(self):
        super().setUp()
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/moved_runner.py",
                        self.repo.git("show", self.head_a
                                      + ":src/ameesh/runner.py") + "\n")
        self.repo.write("tests/test_moved_runner.py",
                        self.repo.git("show", self.head_a
                                      + ":tests/test_runner.py") + "\n")
        self.head_b = self.repo.commit("impl: contenu déplacé", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_a_moved_correction_stays_an_author(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})

    def test_the_gate_refuses_the_moved_corrector_verdict(self):
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/moved_runner.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/moved_runner.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        for actor in ("impl", "cx3", "dev1"):
            self.verdict(actor, self.head_b)
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.head_b,
            repo=self.repo.root, klass=klass, item=self.lot_item(), required=2)
        self.assertFalse(out.get("error"), out)
        self.assertFalse(out["ok"], out["why"])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["dev1"], out)
        self.assertIn("cx3", {row["name"] for row in out["authors"]})


class NoReceiptCorrectorTest(_RewriteFixture):
    """#2924 B4 : sans reçu exact, le correcteur désigné reste auteur (#2721)."""

    def squash_without_receipt(self, lines=0):
        self.db.execute("DELETE FROM provenance_receipts WHERE actor = 'cx3'")
        head = self.head_a
        if lines:
            self.repo.write(
                "src/ameesh/runner.py",
                "base\nlot\n"
                "if not admitted:\n"
                "    return deferred  # cx3 fix\n"
                + "".join("guard %02d = %d\n" % (i, i) for i in range(lines)))
            head = self.repo.commit("cx3: grosse correction", agent="cx3")
            self.freeze(head)
        # La famille du correcteur n'est plus dans un reçu : elle vient du
        # journal des tours (#2721), comme pour la lecture courante.
        self.db.execute("INSERT INTO turn_costs (agent, harness, session,"
                        " recorded_at) VALUES ('cx3', 'codex', 's-codex', now())")
        tree = self.repo.git("rev-parse", head + "^{tree}")
        head_b = self.squash(tree, self.base, "contenu consolidé")
        freeze_b = self.freeze(head_b)
        milestone = {"id": int(freeze_b["id"]), "sha": head_b,
                     "actor": "impl", "at_ts": freeze_b.get("at_ts")}
        return milestone, head_b

    def test_a_small_correction_without_a_receipt_stays_an_author(self):
        milestone, head_b = self.squash_without_receipt()
        authors, unknown, correctors = self.reading(
            milestone, [head_b], self.base, head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})

    def test_a_large_correction_without_a_receipt_stays_an_author(self):
        milestone, head_b = self.squash_without_receipt(lines=60)
        authors, unknown, correctors = self.reading(
            milestone, [head_b], self.base, head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertNotIn("cx3", {row["name"] for row in correctors})


class AmbiguousFamiliesTest(_RewriteFixture):
    """#2924 B6 : l'ensemble ambigu de familles d'un acteur est transporté.

    Le même agent a écrit sous codex puis sous deepseek dans un gel antérieur ;
    le squash à arbre identique est signé deepseek. La lecture courante ne doit
    pas réduire {codex, deepseek} à la seule famille du reçu courant : les deux
    familles restent prouvées, la famille de l'auteur reste ambiguë et la porte
    refuse de deviner.
    """

    def setUp(self):
        super().setUp()
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/runner.py", "base\nlot\ncodex guard\n")
        self.codex_commit = self.repo.commit("impl: commit codex", agent="impl")
        self.receipt("impl", "codex", [self.codex_commit])
        self.repo.write("src/ameesh/runner.py",
                        "base\nlot\ncodex guard\ndeepseek guard\n")
        self.deepseek_commit = self.repo.commit("impl: commit deepseek",
                                                agent="impl")
        self.receipt("impl", "deepseek", [self.deepseek_commit])
        self.freeze_a = self.freeze(self.deepseek_commit)
        parent = self.repo.git("rev-parse", self.deepseek_commit + "^")
        tree = self.repo.git("rev-parse", self.deepseek_commit + "^{tree}")
        self.head_b = self.squash(tree, parent, "contenu consolidé ambigu")
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_ambiguous_families_are_both_carried(self):
        families_out: dict = {}
        authors, unknown, correctors = review_authors.reading(
            self.db, self.lot_item(), self.milestone, [self.head_b],
            self.repo.root, FAMILIES, base=self.base, head=self.head_b,
            families_out=families_out)
        self.assertEqual(unknown, [])
        got = {row["name"]: row["family"] for row in authors}
        self.assertEqual(got.get("impl"), "")
        self.assertEqual(families_out.get("impl"), {"codex", "deepseek"})

    def test_the_gate_does_not_guess_the_ambiguous_family(self):
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/runner.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/runner.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        with self.assertRaises(merge_queue._ProvenanceError) as caught:
            merge_queue._complete_authors(
                self.db, self.lot_item(), self.head_b, self.repo.root,
                self.base, klass=klass,
                marks=work.milestones(self.db, self.lot))
        self.assertIn("ambiguous", str(caught.exception))
        self.assertIn("codex", str(caught.exception))
        self.assertIn("deepseek", str(caught.exception))


class MissingPriorFreezeTest(_RewriteFixture):
    """#2924 B5/B7 : un gel antérieur absent est récupéré, sinon refusé."""

    def setUp(self):
        super().setUp()
        parent = self.repo.git("rev-parse", self.head_a + "^")
        tree = self.repo.git("rev-parse", self.head_a + "^{tree}")
        self.head_b = self.squash(tree, parent, "contenu consolidé")
        self.freeze_b = self.freeze(self.head_b)
        self.base = parent
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}
        # Un dépôt qui ne contient que la cible et la tête 2 : le gel 1 (tête A)
        # n'y est pas, comme le clone scratch de `merge-pr`.
        self.scratch = _Repo(os.path.join(self.tmp, "scratch"))
        self.scratch.git("remote", "add", "origin", self.repo.root)
        self.scratch.git("fetch", "-q", "origin", self.head_b)

    def test_the_reader_recovers_the_missing_freeze_without_a_caller_hook(self):
        # #3147 : le lecteur direct n'a plus besoin d'un `recover` d'appelant ;
        # il demande le gel absent à la forge du clone (`origin`) par SHA exact.
        #
        # Avant #3147, ce lecteur refusait fermé : `recover_one` rendait False
        # sans hook, même quand l'origine servait le commit. C'est ce qui
        # faisait dépendre la porte du clone (packet_candidates, packet_watch,
        # train, merge_queue lisent tous `review_authors`).
        authors, unknown, correctors = review_authors.reading(
            self.db, self.lot_item(), self.milestone, [self.head_b],
            self.scratch.root, FAMILIES, base=self.base, head=self.head_b,
            strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})
        # Le gel récupéré est bien devenu lisible dans le clone de lecture.
        self.scratch.git("cat-file", "-e", self.head_a + "^{commit}")

    def test_the_gate_recovers_the_missing_freeze(self):
        # B7 : le gel antérieur n'est pas dans le dépôt de la porte, mais son
        # SHA est récupérable depuis l'origine du dépôt (remote ou chemin).
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/runner.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/runner.py"]}],
                 "repo": self.scratch.root, "target": self.base,
                 "base_sha": self.base}
        authors, _families = merge_queue._complete_authors(
            self.db, self.lot_item(), self.head_b, self.scratch.root,
            self.base, klass=klass,
            marks=work.milestones(self.db, self.lot))
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})

    def test_the_gate_refuses_when_recovery_is_impossible(self):
        # Sans origine et sans autre dépôt à interroger, aucun objet du gel
        # antérieur ne peut être relu : la porte refuse toujours fermé.
        orphan = _Repo(os.path.join(self.tmp, "orphan"))
        orphan.git("fetch", "-q", self.repo.root, self.head_b)
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/runner.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/runner.py"]}],
                 "repo": orphan.root, "target": self.base,
                 "base_sha": self.base}
        with self.assertRaises(merge_queue._ProvenanceError):
            merge_queue._complete_authors(
                self.db, self.lot_item(), self.head_b, orphan.root,
                self.base, klass=klass,
                marks=work.milestones(self.db, self.lot))

    def test_a_lot_without_corrector_passes_after_a_rewrite(self):
        # Témoin B7 positif : un gel réécrit sans correcteur, lu depuis un
        # dépôt qui ne porte que la cible et la tête, passe la lecture stricte.
        repo = _Repo(os.path.join(self.tmp, "plain"))
        repo.write("src/ameesh/runner.py", "base\n")
        base = repo.commit("base")
        repo.write("src/ameesh/runner.py", "base\nimpl\n")
        head_a = repo.commit("impl: lot", agent="impl")
        item = work.add(self.db, title="réécriture sans correcteur", app="ameesh",
                        assignee="impl")
        lot = int(item["id"])
        work.move(self.db, lot, "build")
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (base, lot))
        self.receipt("impl", "deepseek", [head_a], lot=lot)
        work.milestone(self.db, lot, "frozen", sha=head_a, actor="impl",
                       repo=repo.root)
        tree = repo.git("rev-parse", head_a + "^{tree}")
        head_b = repo.git("commit-tree", tree, "-p", base, "-m",
                          "contenu consolidé sans correcteur", env=self.env("impl"))
        self.receipt("impl", "deepseek", [head_b], lot=lot)
        work.milestone(self.db, lot, "frozen", sha=head_b, actor="impl",
                       repo=repo.root)
        scratch = _Repo(os.path.join(self.tmp, "plain-scratch"))
        scratch.git("remote", "add", "origin", repo.root)
        scratch.git("fetch", "-q", "origin", head_b)
        authors, _families = merge_queue._complete_authors(
            self.db, work.get(self.db, lot), head_b, scratch.root, base,
            marks=work.milestones(self.db, lot))
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek"})


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


class SupersededGhostFreezeTest(_RewriteFixture):
    """#2989 : un gel fantôme déclaré remplacé ne bloque plus la provenance."""

    GHOST = "66a06cf09d5c2f2dfe45be5c15cf0b6ea3df8c4c"

    def setUp(self):
        super().setUp()
        self.ghost = storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha=self.GHOST, actor="impl", verdict=None,
            note="gel fantôme : SHA erroné, jamais poussé")
        parent = self.repo.git("rev-parse", self.head_a + "^")
        tree = self.repo.git("rev-parse", self.head_a + "^{tree}")
        self.head_b = self.squash(tree, parent, "contenu consolidé")
        self.freeze_b = self.freeze(self.head_b, supersedes=[self.ghost["id"]])
        self.base = parent
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_ghost_is_ignored_and_the_corrector_is_carried(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})

    def test_the_designation_reads_the_same_authors(self):
        from ameesh import review_designation_activation as act

        authors, unknown, _correctors = act.attribution_of_head(
            self.db, self.lot_item(), self.milestone, self.repo.root,
            self.base, self.head_b, families=FAMILIES)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})

    def test_the_merge_gate_does_not_fail_on_the_ghost(self):
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/runner.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/runner.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        authors, _families = merge_queue._complete_authors(
            self.db, self.lot_item(), self.head_b, self.repo.root, self.base,
            klass=klass, marks=work.milestones(self.db, self.lot))
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})

    def test_the_supersedes_marker_is_written_not_forged(self):
        row = self.freeze(
            self.head_b,
            note="ameesh-freeze-supersedes: 1\nrevue demandée",
            supersedes=[self.ghost["id"]])
        self.assertIn("ameesh-freeze-supersedes: %d" % int(self.ghost["id"]),
                      row["note"])
        self.assertNotIn("ameesh-freeze-supersedes: 1\n", row["note"])
        self.assertIn("revue demandée", row["note"])

    def test_an_unknown_superseded_freeze_is_refused(self):
        with self.assertRaises(work.WorkError) as caught:
            self.freeze(self.head_b, supersedes=[999999])
        self.assertIn("not a prior", str(caught.exception))

    def test_supersedes_a_present_freeze_is_refused(self):
        # #2989 N1 (revue dev2) : la marque ne vaut que pour un fantôme prouvé.
        # Le gel de la tête A est encore accessible par une ref : le remplacer
        # effacerait ses auteurs si sa récupération venait à échouer (refus
        # fermé B5). #3399 : c'est l'accessibilité par une ref qui décide, pas
        # la seule présence de l'objet dans l'ODB.
        present = next(row for row in work.milestones(self.db, self.lot)
                       if row.get("kind") == "frozen"
                       and row.get("sha") == self.head_a)
        with self.assertRaises(work.WorkError) as caught:
            self.freeze(self.head_b, supersedes=[present["id"]])
        self.assertIn("reachable from a ref", str(caught.exception))
        self.assertIn("proven ghost", str(caught.exception))

    def test_supersedes_is_refused_for_a_verdict(self):
        with self.assertRaises(work.WorkError) as caught:
            work.milestone(self.db, self.lot, "verdict", sha=self.head_b,
                           actor="dev1", verdict="ok",
                           supersedes=[self.ghost["id"]])
        self.assertIn("only makes sense", str(caught.exception))

    def test_a_ghost_not_declared_replaced_still_refuses(self):
        # #2924 B5 reste la règle : seul le gel déclaré remplacé est ignoré.
        other = storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha="b" * 40, actor="impl", verdict=None,
            note="un autre gel fantôme, non déclaré remplacé")
        again = self.freeze(self.head_b)
        milestone = {"id": int(again["id"]), "sha": self.head_b,
                     "actor": "impl", "at_ts": again.get("at_ts")}
        with self.assertRaises(review_provenance.ProvenanceError):
            review_authors.reading(
                self.db, self.lot_item(), milestone, [self.head_b],
                self.repo.root, FAMILIES, base=self.base, head=self.head_b,
                strict=True)


class SupersededPresentFreezeTest(_RewriteFixture):
    """#2989 N3 : un gel remplacé mais encore lisible reste lu (B5)."""

    def setUp(self):
        super().setUp()
        self.present = next(row for row in work.milestones(self.db, self.lot)
                            if row.get("kind") == "frozen"
                            and row.get("sha") == self.head_a)
        parent = self.repo.git("rev-parse", self.head_a + "^")
        tree = self.repo.git("rev-parse", self.head_a + "^{tree}")
        self.head_b = self.squash(tree, parent, "contenu consolidé")
        self.freeze_b = self.freeze(self.head_b)
        # Marque posée hors CLI (donnée historique ou forgée) : le gel nommé
        # est encore présent. La lecture ne doit pas l'effacer — c'est le refus
        # fermé B5 qui reste, comme si la marque n'existait pas.
        self.db.execute(
            "UPDATE work_item_milestones SET note = %s WHERE id = %s",
            ("ameesh-freeze-supersedes: %d" % int(self.present["id"]),
             int(self.freeze_b["id"])))
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_present_replaced_freeze_is_still_read(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})


class ForgedSupersedesNoteTest(_RewriteFixture):
    """#2989 B (revue cx2) : un blanc de tête ne forge pas la marque machine."""

    def test_a_leading_vertical_tab_cannot_forge_the_marker(self):
        ghost = storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha="f" * 40, actor="impl", verdict=None,
            note="gel fantôme, non déclaré remplacé")
        parent = self.repo.git("rev-parse", self.head_a + "^")
        tree = self.repo.git("rev-parse", self.head_a + "^{tree}")
        head_b = self.squash(tree, parent, "contenu consolidé")
        row = self.freeze(head_b, note="\vameesh-freeze-supersedes: %d"
                         % int(ghost["id"]))
        self.assertNotRegex(row["note"] or "",
                            r"(?m)^ameesh-freeze-supersedes:")
        milestone = {"id": int(row["id"]), "sha": head_b,
                     "actor": "impl", "at_ts": row.get("at_ts")}
        # la marque n'a pas été écrite : le fantôme reste non déclaré, B5 refuse
        with self.assertRaises(review_provenance.ProvenanceError):
            review_authors.reading(
                self.db, self.lot_item(), milestone, [head_b],
                self.repo.root, FAMILIES, base=self.base, head=head_b,
                strict=True)


class RemovedLineCannotExposeMarkerTest(_RewriteFixture):
    """#2989 B (revue dev1) : un filtre ne découvre pas la ligne suivante."""

    def test_a_removed_absent_line_cannot_expose_the_supersedes_marker(self):
        # Retirer `ameesh-freeze-absent:` exposait `\vameesh-freeze-supersedes:`,
        # que `[ \t]*` ne couvrait pas ; le strip final de `milestone` la
        # rendait valide, sans `--supersedes` ni contrôle du gel remplacé.
        ghost = storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha="e" * 40, actor="impl", verdict=None,
            note="autre gel fantôme, non déclaré remplacé")
        parent = self.repo.git("rev-parse", self.head_a + "^")
        tree = self.repo.git("rev-parse", self.head_a + "^{tree}")
        head_b = self.squash(tree, parent, "contenu consolidé")
        for blank in ("\v", "\f", "\u00a0"):
            with self.subTest(blank=repr(blank)):
                row = self.freeze(
                    head_b, note="ameesh-freeze-absent: master\n" + blank
                    + "ameesh-freeze-supersedes: %d" % int(ghost["id"]))
                self.assertNotRegex(row["note"] or "",
                                    r"(?m)^ameesh-freeze-supersedes:")


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

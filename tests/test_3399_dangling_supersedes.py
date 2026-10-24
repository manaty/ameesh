# SPDX-License-Identifier: AGPL-3.0-only
"""#3399 — un gel devenu objet pendant après un force-push reste remplaçable.

Un force-push retire l'ancien gel de toute ref sans élaguer son objet : il
reste lisible dans l'ODB. #2989 refusait `--supersedes` sur cette seule
présence (`freeze_checks.commit_in`), et la lecture relisait ce gel : un commit
sans provenance de son ancienne plage bloquait pour toujours la désignation
des gels suivants. #3399 distingue la présence de l'objet de son accessibilité
par une ref : un objet pendant est remplaçable, un gel encore accessible par
une ref garde le refus fermé B5.

Témoin : gel A (avec un commit tiers non reçu), force-push qui laisse A
pendant, nouveau gel B sur l'histoire réécrite — la désignation de B bloquait
sur l'inconnu de A ; le gel C = B + `--supersedes A` la débloque, sans perdre
les auteurs encore accessibles.
"""
import os
from unittest import mock

from ameesh import plan_git, review_authors, storage, work

from .test_2924_history_rewrite_provenance import FAMILIES, _RewriteFixture


class _DanglingSupersededFreezeFixture(_RewriteFixture):
    """Le cas réel : l'ancien gel n'est plus dans aucune ref."""

    def setUp(self):
        super().setUp()
        # un commit tiers sans reçu, dans la plage du gel A : l'inconnu qui
        # bloque la désignation des gels suivants (#2924 B5).
        self.repo.write("src/ameesh/runner.py", "base\nlot\noperator fix\n")
        self.op = self.repo.commit("operator: fix", agent="operator")
        self.op_freeze = self.freeze(self.op)
        self.tree = self.repo.git("rev-parse", self.op + "^{tree}")
        # force-push : la branche repart de la base ; le gel A et son commit
        # tiers restent des objets pendants, lisibles mais hors de toute ref.
        self.repo.git("reset", "--hard", self.base)
        self.assertEqual(
            self.repo.git("for-each-ref", "--contains", self.op,
                          "--format=%(refname)"), "")
        # la tête réécrite reprend le même arbre, signée et reçue par impl ;
        # la branche est repointée dessus, comme après le push forcé.
        self.head_b = self.squash(self.tree, self.base, "contenu réécrit")
        self.repo.git("reset", "--hard", self.head_b)
        self.freeze_b = self.freeze(self.head_b)

    def milestone(self, row):
        return {"id": int(row["id"]), "sha": row["sha"],
                "actor": row.get("actor"), "at_ts": row.get("at_ts")}

    def read(self, row):
        commits = review_authors._commits_between(
            self.repo.root, self.base, self.head_b)
        return review_authors.reading(
            self.db, self.lot_item(), self.milestone(row), commits,
            self.repo.root, FAMILIES, base=self.base, head=self.head_b,
            strict=True)

    def supersede(self):
        return self.freeze(self.head_b, supersedes=[self.op_freeze["id"]])


class DanglingSupersededFreezeTest(_DanglingSupersededFreezeFixture):
    """Le gel pendant est remplaçable, le gel accessible ne l'est pas."""

    def test_the_old_freeze_is_present_but_not_accessible(self):
        # la condition exacte du défaut : l'objet est toujours dans l'ODB.
        self.assertEqual(self.repo.git("cat-file", "-t", self.op), "commit")
        self.assertFalse(plan_git.ref_reachable(self.repo.root, self.op))
        self.assertTrue(plan_git.ref_reachable(self.repo.root, self.head_b))

    def test_without_supersedes_the_designation_is_blocked(self):
        authors, unknown, _correctors = self.read(self.freeze_b)
        self.assertIn(self.op[:12], unknown)
        self.assertEqual({row["name"] for row in authors}, {"impl", "cx3"})

    def test_a_dangling_freeze_can_be_superseded(self):
        row = self.supersede()
        self.assertIn("ameesh-freeze-supersedes: %d" % int(self.op_freeze["id"]),
                      row["note"])

    def test_the_superseded_dangling_freeze_no_longer_blocks(self):
        row = self.supersede()
        authors, unknown, _correctors = self.read(row)
        self.assertEqual(unknown, [])
        # les auteurs du gel encore accessible par l'histoire sont conservés
        self.assertEqual({r["name"]: r["family"] for r in authors},
                         {"impl": "deepseek", "cx3": "codex"})

    def test_an_accessible_freeze_is_still_refused(self):
        # une ref recréée rend l'ancien gel accessible : le refus fermé B5
        # garde sa force, même si l'objet n'était auparavant qu'un pendant.
        self.repo.git("branch", "keep/old-freeze", self.op)
        with self.assertRaises(work.WorkError) as caught:
            self.supersede()
        self.assertIn("reachable from a ref", str(caught.exception))
        self.assertIn("proven ghost", str(caught.exception))

    def test_an_unreadable_reachability_refuses_the_replacement(self):
        with mock.patch.object(plan_git, "ref_reachable", return_value=None):
            with self.assertRaises(work.WorkError) as caught:
                self.supersede()
        self.assertIn("cannot prove", str(caught.exception))

    def test_a_superseded_freeze_still_accessible_is_still_read(self):
        # marque posée hors CLI (donnée historique ou forgée) alors que le gel
        # nommé reste accessible : la lecture ne l'efface pas (refus fermé B5).
        self.repo.git("branch", "keep/old-freeze", self.op)
        self.db.execute(
            "UPDATE work_item_milestones SET note = %s WHERE id = %s",
            ("ameesh-freeze-supersedes: %d" % int(self.op_freeze["id"]),
             int(self.freeze_b["id"])))
        _authors, unknown, _correctors = self.read(self.freeze_b)
        self.assertIn(self.op[:12], unknown)


class DanglingSupersedeWithRemoteTest(_DanglingSupersededFreezeFixture):
    """Le refus ne conclut qu'après avoir rafraîchi les refs distantes."""

    def add_remote(self, *, reachable=True):
        bare = os.path.join(self.tmp, "origin.git")
        if reachable:
            self.repo.git("init", "--bare", "-q", bare)
            self.repo.git("remote", "add", "origin", bare)
            self.repo.git("push", "-q", "origin", "HEAD:refs/heads/master")
        else:
            self.repo.git("remote", "add", "origin", bare)
        return bare

    def test_the_remote_is_refreshed_before_allowing_the_replacement(self):
        self.add_remote()
        row = self.supersede()
        self.assertIn("ameesh-freeze-supersedes: %d" % int(self.op_freeze["id"]),
                      row["note"])

    def test_an_unreachable_remote_refuses_the_replacement(self):
        self.add_remote(reachable=False)
        with self.assertRaises(work.WorkError) as caught:
            self.supersede()
        self.assertIn("cannot prove", str(caught.exception))


class SupersededGhostStillIgnoredTest(_RewriteFixture):
    """#2989 reste vrai : un gel fantôme absent (SHA erroné) est ignoré."""

    GHOST = "66a06cf09d5c2f2dfe45be5c15cf0b6ea3df8c4c"

    def test_an_absent_superseded_freeze_is_ignored(self):
        ghost = storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha=self.GHOST, actor="impl", verdict=None,
            note="gel fantôme : SHA erroné, jamais poussé")
        parent = self.repo.git("rev-parse", self.head_a + "^")
        tree = self.repo.git("rev-parse", self.head_a + "^{tree}")
        head_b = self.squash(tree, parent, "contenu consolidé")
        row = self.freeze(head_b, supersedes=[ghost["id"]])
        commits = review_authors._commits_between(self.repo.root, parent, head_b)
        _authors, unknown, _correctors = review_authors.reading(
            self.db, self.lot_item(),
            {"id": int(row["id"]), "sha": head_b, "actor": "impl",
             "at_ts": row.get("at_ts")},
            commits, self.repo.root, FAMILIES, base=parent, head=head_b,
            strict=True)
        self.assertEqual(unknown, [])

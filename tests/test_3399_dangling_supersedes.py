# SPDX-License-Identifier: AGPL-3.0-only
"""#3399 — un gel devenu objet pendant après un force-push reste remplaçable.

Un force-push retire l'ancien gel de toute ref sans élaguer son objet : il
reste lisible dans l'ODB. #2989 refusait `--supersedes` sur cette seule
présence (`freeze_checks.commit_in`), et la lecture relisait ce gel : un commit
sans provenance de son ancienne plage bloquait pour toujours la désignation
des gels suivants. #3399 distingue la présence de l'objet de son accessibilité
par une ref **locale ou distante** : un objet pendant est remplaçable, un gel
encore accessible par une ref garde le refus fermé B5.

Témoins : gel A (avec un commit tiers non reçu), force-push qui laisse A
pendant, nouveau gel B sur l'histoire réécrite — la désignation de B bloquait
sur l'inconnu de A ; le gel C = B + `--supersedes A` la débloque en gardant les
auteurs **connus** de B dont le contenu survit (correcteur #2924). Un tag
distant, une branche hors du refspec de fetch, une ref `refs/pull/N/head` ou
`refs/archive/…` qui pointe exactement A font refuser le remplacement ;
l'ascendance se prouve par un fetch exact des seules pointes heads/tags,
sinon refus fermé ; le budget de rapatriement ne compte pas les milliers de
refs/pull.
"""
import os
import subprocess
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
    """#3399 B1 : les refs distantes non rapatriées sont couvertes."""

    def add_remote(self, *, reachable=True):
        bare = os.path.join(self.tmp, "origin.git")
        if reachable:
            self.repo.git("init", "--bare", "-q", bare)
            self.repo.git("remote", "add", "origin", bare)
            self.repo.git("push", "-q", "origin", "HEAD:refs/heads/master")
        else:
            self.repo.git("remote", "add", "origin", bare)
        return bare

    def bare_git(self, bare, *args, env=None):
        proc = subprocess.run(["git", "-C", bare, *args],
                              capture_output=True, text=True, env=env)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def test_a_remote_without_the_freeze_allows_the_replacement(self):
        self.add_remote()
        row = self.supersede()
        self.assertIn("ameesh-freeze-supersedes: %d" % int(self.op_freeze["id"]),
                      row["note"])

    def test_an_unreachable_remote_refuses_the_replacement(self):
        self.add_remote(reachable=False)
        with self.assertRaises(work.WorkError) as caught:
            self.supersede()
        self.assertIn("cannot", str(caught.exception))

    def test_a_remote_tag_keeping_the_freeze_refuses(self):
        # Témoin cx1 : l'ancien gel reste accessible par un tag distant que
        # `fetch --no-tags` ne rapatrie jamais ; `ref_reachable` seul est
        # aveugle, `ls-remote` le voit et doit refuser le remplacement.
        bare = self.add_remote()
        self.repo.git("push", "-q", "origin",
                      "%s:refs/tags/retained-freeze" % self.op)
        self.assertFalse(plan_git.ref_reachable(self.repo.root, self.op))
        self.assertIn("retained-freeze", self.repo.git("ls-remote", "--tags", bare))
        with self.assertRaises(work.WorkError) as caught:
            self.supersede()
        self.assertIn("remote ref", str(caught.exception))
        self.assertIn("origin", str(caught.exception))

    def test_a_remote_branch_outside_the_fetch_refspec_refuses(self):
        # Refspec restreint : seule `master` est rapatriée ; une branche
        # distante porte un descendant de l'ancien gel. La preuve passe par
        # `ls-remote` puis par le rapatriement exact de la pointe (ascendance).
        bare = self.add_remote()
        self.repo.git("config", "remote.origin.fetch",
                      "+refs/heads/master:refs/remotes/origin/master")
        self.repo.git("push", "-q", "origin",
                      "%s:refs/heads/keep/old" % self.op)
        tree = self.repo.git("rev-parse", self.op + "^{tree}")
        child = self.bare_git(bare, "commit-tree", tree, "-p", self.op,
                              "-m", "later", env=self.env("impl"))
        self.bare_git(bare, "update-ref", "refs/heads/keep/later", child)
        self.bare_git(bare, "update-ref", "-d", "refs/heads/keep/old")
        self.repo.git("fetch", "-q", "origin")
        self.assertFalse(plan_git.ref_reachable(self.repo.root, self.op))
        self.assertIn("keep/later", self.repo.git("ls-remote", "--heads", bare))
        with self.assertRaises(work.WorkError) as caught:
            self.supersede()
        self.assertIn("remote ref", str(caught.exception))

    def test_a_remote_archive_ref_keeping_the_freeze_refuses(self):
        # #3399 B1 : l'annonce entière, pas seulement heads/tags. Une ref
        # d'archive qui pointe exactement l'ancien gel le rend accessible.
        bare = self.add_remote()
        self.repo.git("push", "-q", "origin",
                      "%s:refs/archive/retained-freeze" % self.op)
        self.assertFalse(plan_git.ref_reachable(self.repo.root, self.op))
        self.assertIn("refs/archive/retained-freeze",
                      self.repo.git("ls-remote", bare))
        with self.assertRaises(work.WorkError) as caught:
            self.supersede()
        self.assertIn("remote ref", str(caught.exception))
        self.assertIn("origin", str(caught.exception))

    def test_a_remote_pull_ref_keeping_the_freeze_refuses(self):
        # Témoin auditeur : refs/pull/N/head d'une PR fermée puis recréée.
        bare = self.add_remote()
        self.repo.git("push", "-q", "origin",
                      "%s:refs/pull/1/head" % self.op)
        self.assertFalse(plan_git.ref_reachable(self.repo.root, self.op))
        self.assertIn("refs/pull/1/head", self.repo.git("ls-remote", bare))
        with self.assertRaises(work.WorkError) as caught:
            self.supersede()
        self.assertIn("remote ref", str(caught.exception))

    def test_many_pull_refs_do_not_exhaust_the_ancestry_budget(self):
        # #3399 B2 : l'égalité exacte couvre toutes les refs, mais le budget
        # d'ascendance ne compte que heads/tags. 20 refs/pull non locales ne
        # doivent pas faire refuser la vérification : seule `keep/later`,
        # descendante de l'ancien gel, demande un rapatriement et prouve
        # l'accessibilité.
        bare = self.add_remote()
        self.repo.git("config", "remote.origin.fetch",
                      "+refs/heads/master:refs/remotes/origin/master")
        self.repo.git("push", "-q", "origin",
                      "%s:refs/heads/keep/old" % self.op)
        tree = self.repo.git("rev-parse", self.base + "^{tree}")
        for number in range(1, 21):
            tip = self.bare_git(
                bare, "commit-tree", tree, "-p", self.base,
                "-m", "pull %d" % number, env=self.env("impl"))
            self.bare_git(bare, "update-ref",
                          "refs/pull/%d/head" % number, tip)
        child = self.bare_git(bare, "commit-tree", tree, "-p", self.op,
                              "-m", "later", env=self.env("impl"))
        self.bare_git(bare, "update-ref", "refs/heads/keep/later", child)
        self.bare_git(bare, "update-ref", "-d", "refs/heads/keep/old")
        self.repo.git("fetch", "-q", "origin")
        self.assertFalse(plan_git.ref_reachable(self.repo.root, self.op))
        self.assertIn("keep/later", self.repo.git("ls-remote", "--heads", bare))
        with self.assertRaises(work.WorkError) as caught:
            self.supersede()
        text = str(caught.exception)
        self.assertIn("remote ref", text)
        self.assertNotIn("cannot", text)


class ForcePushSupersedesKeepsCorrectorTest(_RewriteFixture):
    """#3399 (auditeur) : le correcteur connu du gel pendant reste auteur.

    Témoin obligatoire de #2924 avec un force-push : le correctif codex de la
    tête A survit dans la tête réécrite, mais il ne vit que dans le gel B
    déclaré remplacé. Sans la lecture des auteurs connus du gel pendant, codex
    sort des auteurs et sa famille peut relire son propre code.
    """

    def _build_head_a(self):
        self.repo.write("src/ameesh/runner.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/runner.py", "base\nlot\n")
        self.author = self.repo.commit("impl: lot", agent="impl")
        self.receipt("impl", "deepseek", [self.author])
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.head_a = self.author
        self.freeze(self.author)                # gel A : impl seul

    def setUp(self):
        super().setUp()
        # Le correcteur d'une autre famille et un inconnu n'existent que dans
        # le gel B, laissé pendant par le force-push.
        self.repo.write("src/ameesh/runner.py",
                        "base\nlot\nif not admitted:\n"
                        "    return deferred  # cx3 fix\n")
        self.fix = self.repo.commit("cx3: correctif", agent="cx3")
        self.receipt("cx3", "codex", [self.fix])
        self.repo.write("tests/test_runner.py",
                        "def test_admission():\n"
                        "    assert admitted() is False  # cx3 witness\n")
        self.op = self.repo.commit("operator: fix", agent="operator")
        self.freeze_b = self.freeze(self.op)
        self.tree = self.repo.git("rev-parse", self.op + "^{tree}")
        self.repo.git("reset", "--hard", self.base)
        self.head_c = self.squash(self.tree, self.base, "contenu réécrit")
        self.repo.git("reset", "--hard", self.head_c)
        self.freeze_c = self.freeze(self.head_c,
                                    supersedes=[self.freeze_b["id"]])

    def test_the_known_corrector_of_the_replaced_freeze_stays_author(self):
        commits = review_authors._commits_between(
            self.repo.root, self.base, self.head_c)
        authors, unknown, correctors = review_authors.reading(
            self.db, self.lot_item(),
            {"id": int(self.freeze_c["id"]), "sha": self.head_c,
             "actor": "impl", "at_ts": self.freeze_c.get("at_ts")},
            commits, self.repo.root, FAMILIES, base=self.base,
            head=self.head_c, strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual({r["name"]: r["family"] for r in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {r["name"] for r in correctors})


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

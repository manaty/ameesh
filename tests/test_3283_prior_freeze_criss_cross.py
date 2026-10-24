# SPDX-License-Identifier: AGPL-3.0-only
"""#3283 — la plage d'un gel antérieur se lit depuis la cible, pas depuis UNE
base de fusion.

Cas réel #3186 (gels #17350/#17357) : le lot était posé sur la résolution
#3176×#3085 ; #3085 (impl-03, deepseek) est ensuite entré dans main par un
paquet. Le gel antérieur et main ont alors deux bases de fusion (le côté main
et la tête de #3085) ; `git merge-base` en rend une seule, le côté main, et la
plage `merge-base..gel` reprenait les commits #3085 déjà dans la cible. Leurs
lignes étant dans la tête (via main), impl-03 restait auteur : les trois
familles interdites, aucune désignation possible.

La plage d'un gel antérieur est `cible..gel` : exacte quel que soit le nombre
de bases. Un commit de l'autre branche **absent** de la cible reste lu, et son
auteur reste auteur tant que son contenu subsiste (#2924, inchangé).
"""
from __future__ import annotations

from ameesh import review_authors, work

from .test_2924_history_rewrite_provenance import FAMILIES, _RewriteFixture


class _CrissCrossFixture(_RewriteFixture):
    """Lot (impl, deepseek) posé sur une résolution qui fusionne la branche
    d'un autre lot (dev1, claude) ; cette branche entre ensuite dans main."""

    def dated(self, message: str, *, agent: str = "", merge: str = "") -> str:
        """Un commit daté (une minute de plus à chaque fois) : l'ordre des bases
        de fusion rendu par git est alors celui du cas réel."""
        self.clock = getattr(self, "clock", 0) + 60
        stamp = "@%d +0000" % (1792756800 + self.clock)
        env = dict(self.env(agent) if agent else self.repo.dry,
                   GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        if merge:
            self.repo.git("merge", "-q", "--no-ff", "-m", message, merge, env=env)
        else:
            self.repo.git("add", "-A")
            self.repo.git("commit", "-qm", message, env=env)
        return self.repo.git("rev-parse", "HEAD")

    def _build_head_a(self):
        self.other = work.add(self.db, title="autre lot", app="ameesh", assignee="dev1")
        self.repo.write("src/ameesh/runner.py", "base\n")
        self.repo.write("src/ameesh/other.py", "other\n")
        self.repo.write("src/ameesh/main.py", "main\n")
        self.m0 = self.dated("base")
        self.repo.git("branch", "-M", "main")
        # la branche de l'autre lot : B1 (entrera dans main), B2 (n'y entrera pas)
        self.repo.git("checkout", "-q", "-b", "other", self.m0)
        self.repo.write("src/ameesh/other.py",
                        "other\ndef other_lot():\n    return 3085  # dev1\n")
        self.b1 = self.dated("dev1: autre lot", agent="dev1")
        self.receipt("dev1", "claude", [self.b1], lot=int(self.other["id"]))
        self.repo.write("src/ameesh/extra.py",
                        "def unmerged_part():\n    return 'b2'  # dev1\n")
        self.b2 = self.dated("dev1: part non fusionnée", agent="dev1")
        self.receipt("dev1", "claude", [self.b2], lot=int(self.other["id"]))
        # main avance
        self.repo.git("checkout", "-q", "main")
        self.repo.write("src/ameesh/main.py", "main\nmain1\n")
        self.m1 = self.dated("main: avance 1")
        # le lot part d'une résolution main × B1, puis écrit son commit
        self.repo.git("checkout", "-q", "-b", "lot", self.m1)
        self.resolution = self.dated("résolution main × autre lot", agent="impl",
                                     merge=self.b1)
        self.repo.write("src/ameesh/runner.py",
                        "base\ndef lot_gate():\n    return 3186  # impl\n")
        self.lot_commit = self.dated("impl: lot", agent="impl")
        self.receipt("impl", "deepseek", [self.lot_commit])
        self.head_a = self.lot_commit
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.m1, self.lot))
        self.freeze(self.head_a)
        # main avance encore, puis le paquet fait entrer B1 (pas B2)
        self.repo.git("checkout", "-q", "main")
        self.repo.write("src/ameesh/main.py", "main\nmain1\nmain2\n")
        self.m2 = self.dated("main: avance 2")
        self.m3 = self.dated("paquet : autre lot", merge=self.b1)

    def rebased_head(self, *, carry_b2: bool = False) -> tuple[str, dict]:
        """Le lot rebasé sur main (B1 compris), regelé ; avec `carry_b2`, la
        tête reprend aussi la part de B2 que main n'a pas."""
        self.repo.git("checkout", "-q", "-B", "lot", self.m3)
        self.repo.write("src/ameesh/runner.py",
                        "base\ndef lot_gate():\n    return 3186  # impl\n")
        if carry_b2:
            self.repo.write("src/ameesh/extra.py",
                            "def unmerged_part():\n    return 'b2'  # dev1\n")
        head = self.dated("impl: lot (rebasé)", agent="impl")
        self.receipt("impl", "deepseek", [head])
        mark = self.freeze(head)
        milestone = {"id": int(mark["id"]), "sha": head, "actor": "impl",
                     "at_ts": mark.get("at_ts")}
        return head, milestone


class CrissCrossPriorFreezeTest(_CrissCrossFixture):

    def test_the_history_is_criss_crossed_like_the_real_case(self):
        bases = self.repo.git("merge-base", "--all", self.head_a, self.m3).split()
        self.assertEqual(set(bases), {self.m1, self.b1})
        # git rend le côté main : la plage lue depuis lui reprend B1
        self.assertEqual(self.repo.git("merge-base", self.head_a, self.m3), self.m1)

    def test_a_commit_already_in_the_target_is_not_an_author(self):
        head, milestone = self.rebased_head()
        authors, unknown, correctors = self.reading(milestone, [head], self.m3, head)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek"})
        self.assertEqual(correctors, [])

    def test_the_designation_reads_the_same_authors(self):
        from ameesh import review_designation_activation as act

        head, milestone = self.rebased_head()
        authors, unknown, _correctors = act.attribution_of_head(
            self.db, self.lot_item(), milestone, self.repo.root, self.m3, head,
            families=FAMILIES)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek"})

    def test_a_commit_absent_from_the_target_stays_an_author(self):
        # un second gel antérieur fusionne aussi B2, que main n'a pas reçu ;
        # la tête rebasée garde ce contenu : dev1 en reste auteur
        self.repo.git("checkout", "-q", "lot")
        self.repo.git("reset", "-q", "--hard", self.head_a)
        merged = self.dated("résolution autre lot (B2)", agent="impl", merge=self.b2)
        self.freeze(merged)
        head, milestone = self.rebased_head(carry_b2=True)
        authors, unknown, _correctors = self.reading(milestone, [head], self.m3, head)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "dev1": "claude"})


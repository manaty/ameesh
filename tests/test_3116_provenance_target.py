# SPDX-License-Identifier: AGPL-3.0-only
"""#3116 — un commit déjà dans la cible ne compte ni auteur ni tiers du lot.

Le report des gels antérieurs (#2924) relit la plage d'un gel calculée avant
un rebase : cette plage peut contenir des commits du develop déjà fusionnés,
relus sous leurs propres lots. Les tenir pour des tiers sans reçu bloquait la
désignation et la porte (#276, #3088 nexlink).

Attendu : un commit atteignable depuis la cible au moment de la lecture
(`git merge-base --is-ancestor <c> <cible>`) ne compte ni comme auteur ni
comme tiers, qu'il vienne de la plage courante ou d'un gel reporté. La
garantie #2924 reste entière hors cible : un correcteur dont la contribution
n'a pas fusionné par une autre voie garde sa provenance après une réécriture.
Un commit reporté qui n'est plus ancêtre de la tête et dont le contenu ne
survit pas (#2977) ne bloque pas non plus.
"""
from __future__ import annotations

import os

from ameesh import merge_queue, registry, review_authors, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")
SHARED = "the lot line that the target already carries\n"
FIX = "def guard():\n    return deferred  # cx fix\n"


class _TargetFixture(PgTestCase):
    """Un lot dont l'ancien gel descend d'un commit de la cible.

    La cible a avancé de deux commits de lots fusionnés — `other`, sans reçu,
    et `tgt`, avec son reçu — lus sous leurs propres lots ; l'ancien gel du lot
    descend de ces commits. Le gel courant est rejoué sur la base d'avant, à
    contenu identique : la plage reportée de l'ancien gel contient donc les
    commits de la cible.
    """

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        for name, harness in (("impl", "deepseek"), ("cx", "codex"),
                              ("other", "claude"), ("tgt", "claude"),
                              ("u", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        # -- cible : base puis un lot fusionné par `other` ------------------
        self.repo.write("base.txt", "base\n")
        self.base = self.repo.commit("base")
        self.item = work.add(self.db, title="provenance cible #3116", app="ameesh",
                             assignee="impl", branch="lot/3116-provenance")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")
        self.db.execute("UPDATE work_items SET branch_target = %s,"
                        " reviewers = ARRAY['cx'] WHERE id = %s",
                        (self.base, self.lot))
        self.repo.write("shared.txt", SHARED)
        self.target1 = self.repo.commit("other: merged target lot", agent="other")
        self.repo.write("target2.txt", "receipted target content\n")
        self.target = self.repo.commit("tgt: merged target lot with receipt",
                                       agent="tgt")
        self.receipt("tgt", "claude", [self.target])
        # -- ancien gel du lot, empilé sur le commit de la cible -------------
        self.repo.git("checkout", "-q", "-b", "lot-old", self.target)
        self.repo.write("src/lot.py", "base\nlot\n")
        self.old_lot = self.repo.commit("impl: lot", agent="impl")
        self.receipt("impl", "deepseek", [self.old_lot])
        self.repo.write("src/fix.py", FIX)
        self.old_fix = self.repo.commit("cx: guard", agent="cx")
        self.receipt("cx", "codex", [self.old_fix])
        self.old_extra()
        self.old_head = self.repo.git("rev-parse", "HEAD")
        self.old_freeze = self.freeze(self.old_head)
        # -- gel courant : rejoué sur la base, à contenu identique ----------
        self.repo.git("checkout", "-q", "-b", "lot-new", self.base)
        self.repo.write("shared.txt", SHARED)
        self.repo.write("src/lot.py", "base\nlot\n")
        self.new_lot = self.repo.commit("impl: lot", agent="impl")
        self.receipt("impl", "deepseek", [self.new_lot])
        self.repo.write("src/fix.py", FIX)
        self.new_fix = self.repo.commit("cx: guard", agent="cx")
        self.receipt("cx", "codex", [self.new_fix])
        self.new_extra()
        self.new_head = self.repo.git("rev-parse", "HEAD")
        self.new_freeze = self.freeze(self.new_head)

    def old_extra(self) -> None:
        """Commits ajoutés à l'ancien gel seulement (surcharge)."""

    def new_extra(self) -> None:
        """Commits ajoutés au gel courant seulement (surcharge)."""

    def receipt(self, actor, harness, commits, lot=None):
        for sha in commits:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
                 self.lot if lot is None else lot))

    def freeze(self, sha):
        return work.milestone(self.db, self.lot, "frozen", sha=sha,
                              actor="impl", repo=self.repo.root)

    def milestone(self, freeze):
        return {"id": int(freeze["id"]), "sha": str(freeze["sha"]),
                "actor": freeze.get("actor"), "at_ts": freeze.get("at_ts")}

    def reading(self, commits, *, target=None, strict=True):
        return review_authors.reading(
            self.db, work.get(self.db, self.lot), self.milestone(self.new_freeze),
            commits, self.repo.root, FAMILIES, strict=strict,
            base=self.base, head=self.new_head,
            target=self.target if target is None else target)

    def names(self, authors):
        return {row["name"]: row["family"] for row in authors}


class TargetCommitTest(_TargetFixture):
    """Le commit de la cible reporté par l'ancien gel n'est ni auteur ni tiers."""

    def test_the_target_commit_carried_by_the_old_freeze_is_not_a_third_party(self):
        authors, unknown, _correctors = self.reading([self.new_lot, self.new_fix])
        self.assertEqual(unknown, [])
        self.assertNotIn("other", self.names(authors))
        self.assertNotIn("tgt", self.names(authors))
        self.assertEqual(self.names(authors), {"impl": "deepseek", "cx": "codex"})

    def test_the_target_commit_in_the_current_range_is_skipped_too(self):
        # qu'il vienne de la plage courante ou d'un gel reporté (consigne #3116)
        authors, unknown, _correctors = self.reading(
            [self.target, self.new_lot, self.new_fix])
        self.assertEqual(unknown, [])
        self.assertNotIn("other", self.names(authors))
        self.assertNotIn("tgt", self.names(authors))

    def test_a_corrector_outside_the_target_keeps_its_provenance(self):
        # #2924 inchangé : cx n'a pas fusionné par la cible ; son correctif
        # subsiste à contenu identique après le rebase, il reste auteur.
        authors, unknown, _correctors = self.reading([self.new_lot, self.new_fix])
        self.assertEqual(unknown, [])
        self.assertEqual(self.names(authors).get("cx"), "codex")

    def test_the_merge_gate_reads_the_same_authors(self):
        # la porte partage la lecture : le commit de la cible ne devient pas
        # un auteur « complet » du lot par un autre chemin.
        marks = work.milestones(self.db, self.lot)
        authors, _correctors = merge_queue._attribution_on_freeze(
            self.db, marks, self.new_head, repo=self.repo.root,
            klass={"base_sha": self.base, "class": "normal"},
            item=work.get(self.db, self.lot), families=FAMILIES,
            strict=True, target=self.target)
        self.assertNotIn("tgt", self.names(authors))
        self.assertNotIn("other", self.names(authors))

    def test_the_complete_authors_read_the_same_authors(self):
        authors, _families = merge_queue._complete_authors(
            self.db, work.get(self.db, self.lot), self.new_head,
            self.repo.root, self.target,
            klass={"base_sha": self.target, "class": "normal"},
            marks=work.milestones(self.db, self.lot))
        self.assertNotIn("tgt", self.names(authors))
        self.assertNotIn("other", self.names(authors))

    def test_without_the_target_the_old_commit_still_blocks_closed(self):
        # témoin négatif : hors cible, le lecteur garde le refus fermé de
        # #2721 (le commit de `other` n'a ni reçu ni qualité d'auteur).
        authors, unknown, _correctors = self.reading(
            [self.new_lot, self.new_fix], target=self.base, strict=False)
        self.assertIn(self.target1[:12], unknown)
        # #3220 : même avec reçu, un commit de cible dont le contenu ne survit
        # pas dans la tête n'est plus promu auteur (l'ancien comportement que
        # ce témoin constatait est corrigé).
        self.assertNotIn("tgt", self.names(authors))


class RewrittenUnknownTest(_TargetFixture):
    """Un tiers hors cible dont le contenu a été réécrit ne bloque pas."""

    def old_extra(self):
        self.repo.write("dropped.txt", "a third-party line that is rewritten\n")
        self.dropped = self.repo.commit("u: third-party content", agent="u")

    def new_extra(self):
        self.repo.write("dropped.txt", "rewritten content, no surviving line\n")
        self.rewritten = self.repo.commit("impl: rewrite", agent="impl")
        self.receipt("impl", "deepseek", [self.rewritten])

    def test_the_rewritten_unknown_does_not_block(self):
        authors, unknown, _correctors = self.reading(
            [self.new_lot, self.new_fix, self.rewritten])
        self.assertEqual(unknown, [])
        self.assertNotIn("u", self.names(authors))


class SurvivingUnknownTest(_TargetFixture):
    """Un tiers hors cible dont le contenu survit bloque toujours (#2721)."""

    def old_extra(self):
        self.repo.write("kept.txt", "a third-party line that survives\n")
        self.kept = self.repo.commit("u: third-party content", agent="u")

    def new_extra(self):
        self.repo.write("kept.txt", "a third-party line that survives\n")
        self.copied = self.repo.commit("impl: keep the line", agent="impl")
        self.receipt("impl", "deepseek", [self.copied])

    def test_the_surviving_unknown_still_blocks_closed(self):
        _authors, unknown, _correctors = self.reading(
            [self.new_lot, self.new_fix, self.copied], strict=False)
        self.assertIn(self.kept[:12], unknown)


class MovedUnknownTest(_TargetFixture):
    """#3116 B1 : un inconnu déplacé (old.py → new.py) survit et bloque."""

    def old_extra(self):
        self.repo.write("old.py", "def unique_unknown_guard_3116():\n"
                                  "    return enforce_review_receipts_3116\n")
        self.moved = self.repo.commit("u: third-party content", agent="u")

    def new_extra(self):
        # old.py n'existe pas sur la branche rejouée (base) : le contenu du
        # tiers y arrive déplacé dans new.py.
        self.repo.write("new.py", "def unique_unknown_guard_3116():\n"
                                  "    return enforce_review_receipts_3116\n")
        self.copied = self.repo.commit("impl: move the guard", agent="impl")
        self.receipt("impl", "deepseek", [self.copied])

    def test_the_moved_unknown_still_blocks_closed(self):
        # B1 cx2 : le contenu du tiers subsiste, déplacé ; le déclarer réécrit
        # effaçait son doute. La preuve de réécriture doit chercher l'arbre.
        _authors, unknown, _correctors = self.reading(
            [self.new_lot, self.new_fix, self.copied], strict=False)
        self.assertIn(self.moved[:12], unknown)


class BareTargetTest(_TargetFixture):
    """#3116 B2 : une cible nue `main` se lit sur origin/main, pas sur la locale.

    La branche locale `main` porte le commit du correcteur non fusionné ; la
    cible réelle est `origin/main`. La lire sur la locale écarterait le
    correcteur de la provenance.
    """

    def setUp(self):
        super().setUp()
        self.repo.git("update-ref", "refs/remotes/origin/main", self.target)
        self.repo.git("update-ref", "refs/heads/main", self.new_fix)

    def test_target_commit_prefers_the_remote_ref(self):
        self.assertEqual(review_authors.target_commit(self.repo.root, "main"),
                         self.target)

    def test_a_qualified_remote_ref_is_read_exactly(self):
        self.assertEqual(
            review_authors.target_commit(self.repo.root,
                                         "refs/remotes/origin/main"),
            self.target)

    def test_a_bare_target_keeps_the_unmerged_corrector(self):
        authors, unknown, _correctors = self.reading(
            [self.new_lot, self.new_fix], target="main")
        self.assertEqual(unknown, [])
        self.assertEqual(self.names(authors).get("cx"), "codex")

    def test_the_merge_gate_bare_target_keeps_the_corrector(self):
        marks = work.milestones(self.db, self.lot)
        authors, _correctors = merge_queue._attribution_on_freeze(
            self.db, marks, self.new_head, repo=self.repo.root,
            klass={"base_sha": self.base, "class": "normal"},
            item=work.get(self.db, self.lot), families=FAMILIES,
            strict=True, target="main")
        self.assertEqual(self.names(authors).get("cx"), "codex")

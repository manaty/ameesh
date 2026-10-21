# SPDX-License-Identifier: AGPL-3.0-only
"""#2931 — la porte d'un paquet lit ses constituants sans `origin/<cible>`.

Le dépôt du geste `work merge-pr` est un `git init` qui ne porte que
`refs/ameesh/merge-pr/{target,head}` : ni `main` ni `origin/main` ne s'y
résolvent. `packet_reviews.need` classait chaque constituant sans lui passer la
tête de cible déjà lue : `lot_class.of_lot` retombait sur le `branch_target`
textuel (`origin/main`) et la porte refusait « no readable target branch to
diff the freeze against ». Un lot sans cible explicite n'avait alors aucune
base : la lecture des auteurs et des correcteurs était sautée, et un relecteur
pouvait compter son propre correctif de premier tour.

Les témoins ci-dessous prennent les deux cas : un membre à cible explicite,
et un membre corrigé par un relecteur sans cible explicite.
"""
from __future__ import annotations

import os

from ameesh import packet_reviews, registry, storage, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo


class PacketTargetTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name, harness in (("author", "deepseek"), ("r_codex", "codex"),
                              ("r_claude", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.tmp, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.repo = _Repo(os.path.join(self.tmp, "packet"))
        self.repo.git("checkout", "-q", "-b", "main")
        self.repo.write("src/ameesh/base.py", "base\n")
        self.base = self.repo.commit("base", agent="author")

    # -- fixtures ---------------------------------------------------------
    def receipt(self, sha, actor, harness):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, actor, "s-" + harness, "t", harness))

    def member(self, *, target=None, reviewer_fix=False):
        """Un membre gelé, puis sa composition dans `integration/paquet`."""
        self.repo.git("checkout", "-q", "-b", "lot/member")
        self.repo.write("src/ameesh/member.py", "member\n")
        self.member = self.repo.commit("member", agent="author")
        self.receipt(self.member, "author", "deepseek")
        row = work.add(self.db, title="membre", app="ameesh",
                       branch="lot/member", branch_target=target)
        self.lot = int(row["id"])
        self.db.execute("UPDATE work_items SET assignee = 'author' WHERE id = %s",
                        (self.lot,))
        self.head = self.member
        if reviewer_fix:
            self.repo.write("src/ameesh/member.py", "member\nsmall fix\n")
            self.head = self.repo.commit("r_codex: small fix", agent="r_codex")
            self.receipt(self.head, "r_codex", "codex")
            # le relecteur qui a poussé le correctif reste reconnaissable
            self.db.execute(
                "UPDATE work_items SET reviewers = ARRAY['r_codex'] WHERE id = %s",
                (self.lot,))
        self.repo.git("checkout", "-q", "main")
        self.repo.git("checkout", "-q", "-b", "integration/paquet")
        self.repo.git("merge", "-q", "--no-ff", "-m",
                      "Merge #%d — membre\n\nameesh-work: %d" % (self.lot, self.lot),
                      "lot/member")
        self.packet = self.repo.git("rev-parse", "HEAD")
        self.frozen = storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha=self.head, actor="author", verdict=None,
            note="")
        return self.lot

    def verdict(self, actor, sha=None):
        # #2928 : en classe sensible, un verdict ne compte qu'avec son reçu
        # exact (acteur, tête) ; le témoin pose le jalon comme le fait
        # `ameesh work milestone`.
        mark = storage.of(self.db).work.add_milestone(
            self.lot, "verdict", sha=sha or self.head, actor=actor,
            verdict="ok", note="")
        harness = {"author": "deepseek", "r_codex": "codex",
                   "r_claude": "claude"}.get(actor, "deepseek")
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), sha or self.head, actor,
             "s-" + harness, "t", harness))

    def need(self):
        """La lecture du véhicule, avec la cible déjà résolue (`self.base`)."""
        vehicle = {"id": 999, "app": "ameesh", "branch": "integration/paquet"}
        marks = [{"kind": "frozen", "id": int(self.frozen["id"]),
                  "sha": self.packet, "actor": "author", "at_ts": 90.0}]
        return packet_reviews.need(self.db, 999, vehicle, marks, self.repo.root,
                                   self.base, self.packet, cfg=None)

    def forget_default_target(self):
        """Comme le dépôt scratch : aucun `main`, aucun `origin/HEAD`."""
        self.repo.git("checkout", "-q", "--detach", self.packet)
        for branch in ("main", "lot/member", "integration/paquet"):
            self.repo.git("branch", "-D", branch)

    # -- témoins ----------------------------------------------------------
    def test_an_explicit_target_is_read_from_the_vehicle_base(self):
        # `origin/main` n'existe pas dans ce dépôt (pas de remote) : la porte
        # doit lire la cible déjà résolue, pas ce nom.
        self.member(target="origin/main")
        self.verdict("r_claude")
        out = self.need()
        self.assertIsNotNone(out)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["required"], 0)

    def test_a_member_without_explicit_target_still_reads_its_corrector(self):
        # Sans cible explicite ni cible par défaut, l'ancienne lecture sautait
        # les auteurs : le correctif de r_codex n'était pas reconnu et son
        # propre avis comptait. Il doit être exclu par son nom.
        self.member(reviewer_fix=True)
        self.forget_default_target()
        self.verdict("r_codex")
        out = self.need()
        self.assertIsNotNone(out)
        self.assertFalse(out["ok"], out)
        self.assertIn("constituent #%d" % self.lot, out["why"])

        # Un avis d'une autre famille reste éligible : la correction exclut
        # son auteur, pas la porte.
        self.verdict("r_claude")
        out = self.need()
        self.assertIsNotNone(out)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["required"], 0)


class BranchTargetClearTest(PgTestCase):
    """`work branch --target ""` ramène le lot à la cible par défaut."""

    def lot(self):
        row = work.add(self.db, title="cible", app="ameesh", branch="lot/cible",
                       branch_target="origin/main", actor="pilote")
        return int(row["id"])

    def test_an_empty_target_clears_the_explicit_target(self):
        lot = self.lot()
        out = self.mesh("work", "branch", str(lot), "lot/cible", "--target", "")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIsNone(work.get(self.db, lot)["branch_target"])
        self.assertIn("target cleared", out.stdout)
        # déjà sans cible : rien n'est réécrit, le geste reste réussi.
        again = self.mesh("work", "branch", str(lot), "lot/cible", "--target", "")
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_omitting_the_target_keeps_the_explicit_target(self):
        lot = self.lot()
        out = self.mesh("work", "branch", str(lot), "lot/cible")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(work.get(self.db, lot)["branch_target"], "origin/main")

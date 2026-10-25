# SPDX-License-Identifier: AGPL-3.0-only
"""#3091 — la porte du véhicule classe chaque membre depuis la base du paquet.

`packet_reviews.need` laissait `lot_class.of_lot` retomber sur la cible lue
dans le dépôt de la porte (`origin/main`), qui peut être en retard sur la base
réelle de la composition. Le diff du membre incluait alors les commits que la
cible avait gagnés depuis (dont des chemins sensibles) : un membre léger était
reclassé sensible et le paquet refusé (« 2 independent review verdict(s)
required »).

La base enregistrée par la composition (`work_packet_members.base_sha`) est
utilisée, mais elle n'est **pas** crue sur parole : elle doit être contenue
dans la cible résolue par l'appelant. Sans cette preuve, un enregistrement
`--base X` où X est un commit du véhicule ferait sortir X de toute relecture
(verdict bloquant cx1 de la relecture #3091).

Les témoins prennent :
* un membre léger dont la base de composition est la cible : il reste léger ;
* un membre léger parti d'une base plus ancienne que la composition : il reste
  jugé sur son propre delta, et la base du paquet ne reprend pas les commits
  de la cible ;
* une cible qui a avancé après la composition : la base enregistrée, plus
  ancienne, est prouvée et utilisée (les commits de la cible gagnés depuis ne
  sont ni au membre ni au véhicule) ;
* le témoin négatif de cx1 : une base enregistrée **en avant** de la cible
  n'est pas une preuve ; le commit propre du véhicule reste dans le delta et
  exige ses deux avis ;
* un membre réellement sensible demande toujours deux avis indépendants.
"""
from __future__ import annotations

import os
import types
from unittest import mock

from ameesh import lot_class, merge_queue, packet_reviews, registry, storage, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo


def _canon():
    """Un canon synthétique : les workflows sont sensibles et critiques."""
    return types.SimpleNamespace(root="fixture", sources=[], federation={
        "review_policies": {"risk_classes": {"projects": {"ameesh": {
            "default": "light",
            "rules": [
                {"paths": [".github/workflows/**"], "class": "sensitive",
                 "critical": True},
            ],
        }}}}})


class PacketMemberBaseTest(PgTestCase):
    """La base de classe du membre vient de la composition, prouvée sur la cible."""

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
        self.repo.write("README.md", "base\n")
        # La cible d'avant la composition : c'est elle que le `origin/main`
        # du checkout de la porte nomme encore.
        self.old = self.repo.commit("base", agent="author")
        self.receipt(self.old, "author", "deepseek")
        # La cible que le paquet a réellement pour base.
        self.repo.write(".github/workflows/tests.yml", "name: tests\n")
        self.base = self.repo.commit("main: tests workflow", agent="author")
        self.receipt(self.base, "author", "deepseek")
        # Une cible plus récente (postérieure à la composition) : elle porte un
        # autre chemin sensible, pour distinguer la base prouvée de la cible.
        self.repo.write(".github/workflows/deploy.yml", "name: deploy\n")
        self.ahead = self.repo.commit("main: deploy workflow", agent="author")
        self.receipt(self.ahead, "author", "deepseek")
        # Le dépôt de la porte a un `origin/main` en retard.
        self.repo.git("update-ref", "refs/remotes/origin/main", self.old)

    # -- fixtures ---------------------------------------------------------
    def receipt(self, sha, actor, harness):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, actor, "s-" + harness, "t", harness))

    def verdict(self, actor, sha):
        mark = storage.of(self.db).work.add_milestone(
            self.lot, "verdict", sha=sha, actor=actor, verdict="ok", note="")
        harness = {"author": "deepseek", "r_codex": "codex",
                   "r_claude": "claude"}.get(actor, "deepseek")
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), sha, actor,
             "s-" + harness, "t", harness))

    def member_row(self, head):
        row = work.add(self.db, title="membre", app="ameesh", branch="lot/member",
                       branch_target="main")
        lot = int(row["id"])
        self.db.execute("UPDATE work_items SET assignee = 'author' WHERE id = %s",
                        (lot,))
        storage.of(self.db).work.add_milestone(
            lot, "frozen", sha=head, actor="author", verdict=None, note="")
        return lot

    def record(self, base, head, lot, member_head):
        vehicle = work.add(self.db, title="paquet", app="ameesh",
                           branch="integration/paquet", branch_target="main")
        self.vehicle = int(vehicle["id"])
        storage.of(self.db).work.record_packet(
            self.vehicle, branch="integration/paquet", base_sha=base,
            head_sha=head,
            units=[{"work_item_id": lot, "unit_sha": member_head,
                    "frozen_sha": member_head}],
            note="packet composition recorded", actor="author")

    def compose(self, base, lot):
        self.repo.git("checkout", "-q", "-b", "integration/paquet", base)
        self.repo.git("merge", "-q", "--no-ff", "-m",
                      "Merge #%d — membre\n\nameesh-work: %d" % (lot, lot),
                      "lot/member")
        return self.repo.git("rev-parse", "HEAD")

    def member(self, *, start=None, path="src/ameesh/member.py", text="member\n"):
        """Un membre gelé, sa composition sur la base du paquet, son record."""
        self.repo.git("checkout", "-q", "-b", "lot/member", start or self.base)
        self.repo.write(path, text)
        self.head = self.repo.commit("member", agent="author")
        self.receipt(self.head, "author", "deepseek")
        self.lot = self.member_row(self.head)
        self.repo.git("checkout", "-q", "main")
        self.packet = self.compose(self.base, self.lot)
        self.record(self.base, self.packet, self.lot, self.head)
        return self.lot, self.head

    def need(self, base):
        """La porte du véhicule, avec la cible que l'appelant a résolue."""
        row = work.get(self.db, self.vehicle)
        return packet_reviews.need(
            self.db, self.vehicle, row, [], self.repo.root,
            base, self.packet, cfg=None, canons=[_canon()])

    def classify(self, base):
        """La porte, et la classe reçue pour le membre (la première lue)."""
        with mock.patch.object(merge_queue, "review_gate",
                               wraps=merge_queue.review_gate) as gate:
            out = self.need(base)
        return out, gate.call_args_list[0].kwargs["klass"]

    def own_commit_then_member(self):
        """X sensible propre au véhicule, puis un membre léger ; record --base X."""
        self.repo.git("checkout", "-q", "-b", "lot/member", self.base)
        self.repo.write("src/ameesh/member.py", "member\n")
        head = self.repo.commit("member", agent="author")
        self.receipt(head, "author", "deepseek")
        lot = self.member_row(head)
        self.repo.git("checkout", "-q", "-b", "integration/paquet", self.base)
        self.repo.write(".github/workflows/deploy.yml", "name: deploy\n")
        own = self.repo.commit("vehicule: workflow", agent="author")
        self.repo.git("merge", "-q", "--no-ff", "-m",
                      "Merge #%d — membre\n\nameesh-work: %d" % (lot, lot),
                      "lot/member")
        self.packet = self.repo.git("rev-parse", "HEAD")
        self.record(own, self.packet, lot, head)
        return own

    # -- témoins ----------------------------------------------------------
    def test_a_member_of_the_composition_base_stays_light(self):
        # La cible réelle porte un workflow sensible ; le membre, lui, n'ajoute
        # qu'un fichier léger. La classe ne compte que son propre delta.
        self.member()
        out, klass = self.classify(self.base)
        self.assertEqual(klass["class"], "light", klass)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["required"], 0)
        self.assertEqual(out["own_paths"], [])

    def test_a_member_from_an_older_base_stays_light(self):
        # Le membre est parti de la cible d'avant la composition. La base du
        # paquet ne doit pas ajouter au diff les commits de la cible gagnés
        # depuis : le membre reste jugé sur son propre delta.
        self.member(start=self.old)
        out, klass = self.classify(self.base)
        self.assertEqual(klass["class"], "light", klass)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["required"], 0)
        self.assertEqual(out["own_paths"], [])

    def test_a_recorded_base_behind_the_target_is_proven_and_used(self):
        # La cible a avancé après la composition : ses nouveaux commits ne
        # sont ni au membre ni au delta propre du véhicule, et la base
        # enregistrée (contenue dans cette cible) est la borne juste.
        self.member()
        out, klass = self.classify(self.ahead)
        self.assertEqual(klass["class"], "light", klass)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["required"], 0)
        self.assertEqual(out["own_paths"], [])

    def test_a_recorded_base_ahead_of_the_target_cannot_hide_its_commit(self):
        # Témoin cx1 : la branche du véhicule porte un commit propre sensible
        # X, puis fusionne un membre léger ; `record --base X` accepte X car
        # il est ancêtre de la tête. X ne doit pas sortir de la relecture.
        self.own_commit_then_member()
        out, klass = self.classify(self.base)
        self.assertEqual(klass["class"], "light", klass)
        self.assertIsNone(out["ok"], out)
        self.assertEqual(out["required"], 2)
        self.assertIn(".github/workflows/deploy.yml", out["own_paths"])

    def test_the_gate_must_pass_the_true_target_not_the_recorded_base(self):
        # #3534 (B4) : la classe du porteur est lue depuis le SHA de la cible
        # résolue, jamais depuis la base enregistrée X. Avec X, le commit
        # sensible propre au véhicule B..X sortirait de la relecture.
        own = self.own_commit_then_member()
        row = work.get(self.db, self.vehicle)

        def klass(base_sha):
            return lot_class.of_lot(
                self.db, self.vehicle, item=row, repo=self.repo.root,
                base="main", head=self.packet, milestones=[], canons=[_canon()],
                base_sha=base_sha)

        bypass = klass(own)
        self.assertEqual(bypass["class"], "light", bypass)
        real = klass(self.base)
        self.assertEqual(real["class"], "sensitive", real)
        self.assertIn(".github/workflows/deploy.yml",
                      [f["path"] for f in real["files"]])

    def test_a_sensitive_member_still_needs_two_independent_reviews(self):
        self.member(path=".github/workflows/deploy.yml", text="name: deploy\n")
        out, klass = self.classify(self.base)
        self.assertEqual(klass["class"], "sensitive", klass)
        self.assertFalse(out["ok"], out)
        self.assertIn("2 independent review verdict(s) required", out["why"])
        self.verdict("r_claude", self.head)
        self.assertFalse(self.need(self.base)["ok"], "one family is not enough")
        self.verdict("r_codex", self.head)
        self.assertTrue(self.need(self.base)["ok"])

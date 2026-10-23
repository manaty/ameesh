# SPDX-License-Identifier: AGPL-3.0-only
"""#3091 — la porte du véhicule classe chaque membre depuis la base du paquet.

`packet_reviews.need` laissait `lot_class.of_lot` retomber sur la cible lue
dans le dépôt de la porte (`origin/main`), qui peut être en retard sur la base
réelle de la composition. Le diff du membre incluait alors les commits que la
cible avait gagnés depuis (dont des chemins sensibles) : un membre léger était
reclassé sensible et le paquet refusé (« 2 independent review verdict(s)
required »).

Les témoins prennent les trois cas :
* un membre léger dont la base est la base du paquet, alors que le
  `origin/main` du dépôt de la porte est plus ancien, reste léger ;
* un membre léger parti d'une base plus ancienne que le paquet reste léger
  (la base du paquet n'ajoute pas au diff les commits de la cible) ;
* un membre réellement sensible demande toujours deux avis indépendants.
"""
from __future__ import annotations

import os
import types
from unittest import mock

from ameesh import merge_queue, packet_reviews, registry, storage, work

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
    """La base de classe du membre vient de la composition, pas du checkout."""

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
        # Le dépôt de la porte a un `origin/main` en retard : c'est la ref que
        # l'ancienne lecture prenait pour la base du membre.
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

    def member(self, *, start=None, path="src/ameesh/member.py", text="member\n"):
        """Un membre gelé, sa composition sur la base du paquet, son record."""
        self.repo.git("checkout", "-q", "-b", "lot/member", start or self.base)
        self.repo.write(path, text)
        self.head = self.repo.commit("member", agent="author")
        self.receipt(self.head, "author", "deepseek")
        row = work.add(self.db, title="membre", app="ameesh", branch="lot/member",
                       branch_target="main")
        self.lot = int(row["id"])
        self.db.execute("UPDATE work_items SET assignee = 'author' WHERE id = %s",
                        (self.lot,))
        storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha=self.head, actor="author", verdict=None,
            note="")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("checkout", "-q", "-b", "integration/paquet", self.base)
        self.repo.git("merge", "-q", "--no-ff", "-m",
                      "Merge #%d — membre\n\nameesh-work: %d"
                      % (self.lot, self.lot), "lot/member")
        self.packet = self.repo.git("rev-parse", "HEAD")
        vehicle = work.add(self.db, title="paquet", app="ameesh",
                           branch="integration/paquet", branch_target="main")
        self.vehicle = int(vehicle["id"])
        storage.of(self.db).work.record_packet(
            self.vehicle, branch="integration/paquet", base_sha=self.base,
            head_sha=self.packet,
            units=[{"work_item_id": self.lot, "unit_sha": self.head,
                    "frozen_sha": self.head}],
            note="packet composition recorded", actor="author")
        return self.lot, self.head

    def need(self):
        """La porte du véhicule, avec la ref de cible périmée du checkout."""
        row = work.get(self.db, self.vehicle)
        return packet_reviews.need(
            self.db, self.vehicle, row, [], self.repo.root,
            "origin/main", self.packet, cfg=None, canons=[_canon()])

    def classify(self):
        """La porte, et la classe reçue pour le membre (la première lue)."""
        with mock.patch.object(merge_queue, "review_gate",
                               wraps=merge_queue.review_gate) as gate:
            out = self.need()
        return out, gate.call_args_list[0].kwargs["klass"]

    # -- témoins ----------------------------------------------------------
    def test_a_light_member_over_a_stale_gate_base_stays_light(self):
        # La base réelle du paquet porte un workflow sensible ; le membre,
        # lui, n'ajoute qu'un fichier léger. Le `origin/main` du checkout de
        # la porte est plus ancien que la base du paquet : la classe ne doit
        # pas compter les changements que la cible a gagnés entre les deux.
        self.member()
        out, klass = self.classify()
        self.assertEqual(klass["class"], "light", klass)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["required"], 0)

    def test_a_member_from_an_older_base_stays_light(self):
        # Le membre est parti de la cible d'avant la composition. La base du
        # paquet ne doit pas ajouter au diff les commits de la cible gagnés
        # depuis : le membre reste jugé sur son propre delta.
        self.member(start=self.old)
        out, klass = self.classify()
        self.assertEqual(klass["class"], "light", klass)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["required"], 0)

    def test_a_sensitive_member_still_needs_two_independent_reviews(self):
        self.member(path=".github/workflows/deploy.yml", text="name: deploy\n")
        out, klass = self.classify()
        self.assertEqual(klass["class"], "sensitive", klass)
        self.assertFalse(out["ok"], out)
        self.assertIn("2 independent review verdict(s) required", out["why"])
        self.verdict("r_claude", self.head)
        self.assertFalse(self.need()["ok"], "one family is not enough")
        self.verdict("r_codex", self.head)
        self.assertTrue(self.need()["ok"])

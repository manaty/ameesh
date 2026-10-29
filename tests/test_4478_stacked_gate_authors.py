# SPDX-License-Identifier: AGPL-3.0-only
"""#4478 — la porte de fusion d'un lot empilé lit le parent seulement s'il est relu.

Constat (Nexlink #4476, AM13-5) : le lot est empilé sur la tête gelée de son
prérequis #4461. La désignation sait borner sa lecture au delta propre (#4343),
mais la porte de fusion recompte les commits du parent : les familles des
auteurs du parent (claude) sont alors exclues de la tête de l'enfant (codex),
et l'avis d'une famille rendue éligible est refusé comme non indépendant.

`freeze_stack.candidates` porte les parents d'empilement déclarés (`work
dependencies`) et les membres du même paquet (#3589). Une tête parente ne sort
du périmètre de l'enfant que si sa **propre porte** est satisfaite sur cette
tête exacte, comme `admitted_heads` d'un même véhicule (#3513) : un parent
jamais relu ou bloqué reste compté, et sa famille d'auteur ne peut pas relire
l'enfant. La désignation au gel garde la borne large : elle n'ouvre pas la
fusion.
"""
from __future__ import annotations

import unittest

from ameesh import merge_queue, registry, storage, work
from ameesh import work_dependencies
from ameesh.work_reviewers import designated

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture


class StackedGateTest(_ActivationFixture, PgTestCase):
    """Une tête enfant deepseek posée sur une tête parent claude gelée."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "parent-author", harness="claude",
                        host=self.cfg.host, cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                        " WHERE name = %s", ("parent-author",))
        self.parent = int(work.add(
            self.db, title="Parent empilé", app="ameesh",
            assignee="parent-author")["id"])
        # La cible du parent est le point de départ de l'empilement : sa
        # propre porte lit son delta `base..gel`, jamais le diff de l'enfant.
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.parent))
        self.parent_frozen = ""

    # -- helpers ----------------------------------------------------------
    def _receipt(self, sha: str, actor: str, harness: str, lot: int) -> None:
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, actor, "s-" + harness, "t", harness, lot))

    def _stack(self, *, dependency: bool) -> str:
        """Base → parent claude → enfant deepseek, et le lien déclaré."""
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/parent.py", "parent\n")
        self.parent_frozen = self.repo.commit("parent work", agent="parent-author")
        self._receipt(self.parent_frozen, "parent-author", "claude", self.parent)
        storage.of(self.db).work.add_milestone(
            self.parent, "frozen", sha=self.parent_frozen, actor="parent-author",
            verdict=None, note="")
        if dependency:
            work_dependencies.add(self.db, self.lot, self.parent, actor="author")
        self.repo.write("src/ameesh/mail.py", "child\n")
        self.frozen = self.repo.commit("child work", agent="author")
        self._receipt(self.frozen, "author", "deepseek", self.lot)
        return self.parent_frozen

    def _verdict_on(self, lot: int, sha: str, actor: str, harness: str,
                    verdict: str = "ok") -> dict:
        """Un avis de la tête `sha` du lot, avec le reçu exact de la porte."""
        mark = work.milestone(self.db, lot, "verdict", sha=sha, actor=actor,
                              verdict=verdict, repo=self.repo.root,
                              after_commit=[])
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, 't', %s, %s)",
            ("milestone:" + str(int(mark["id"])), sha, actor,
             "s-" + harness, harness, lot))
        return mark

    def _verdict(self, actor: str, harness: str, verdict: str = "ok") -> dict:
        return self._verdict_on(self.lot, self.frozen, actor, harness, verdict)

    def _admit_parent(self, *, blocked: bool = False) -> None:
        """La propre porte du parent : deux familles hors de l'auteur claude."""
        self._verdict_on(self.parent, self.parent_frozen, "author", "deepseek")
        self._verdict_on(self.parent, self.parent_frozen, "r2", "codex",
                         "blocked" if blocked else "ok")

    def _klass(self) -> dict:
        return {"class": "sensitive", "doubt": False,
                "base_sha": self.base, "target": self.base,
                "files": [{"path": "src/ameesh/parent.py", "class": "sensitive",
                           "rule": ["src/ameesh/parent.py"]}]}

    def _gate(self, **extra):
        item = work.get(self.db, self.lot)
        return merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.frozen,
            repo=self.repo.root, klass=self._klass(), item=item,
            families=["claude", "codex", "deepseek"], target=self.base,
            required=2, exclude_author_family=True, distinct_families=True,
            cfg=self.cfg, **extra)

    def _authors(self):
        return merge_queue._complete_authors(
            self.db, work.get(self.db, self.lot), self.frozen, self.repo.root,
            self.base, klass=self._klass(),
            marks=work.milestones(self.db, self.lot), cfg=self.cfg)

    # -- témoins ----------------------------------------------------------
    def test_the_gate_does_not_count_an_admitted_parents_authors(self):
        self._stack(dependency=True)
        self._admit_parent()
        _mark, out = self.freeze()
        self.assertEqual(out["state"], "designated", out)
        self.assertEqual(set(designated(work.get(self.db, self.lot))),
                         {"r1", "r2"})
        self._verdict("r1", "claude")
        self._verdict("r2", "codex")
        authors, _families = self._authors()
        self.assertEqual([row["name"] for row in authors], ["author"], authors)
        gate = self._gate()
        self.assertTrue(gate["ok"], gate["why"])
        self.assertEqual({row["actor"] for row in gate["accepted"]},
                         {"r1", "r2"})
        # Le feu pré-lit les auteurs complets puis les passe à la porte
        # (`authors_complete=True`) : cette lecture-là doit porter la même
        # borne, sinon la porte refuse l'avis d'une famille rendue éligible.
        pre_read = self._gate(authors=authors, authors_complete=True)
        self.assertTrue(pre_read["ok"], pre_read["why"])
        self.assertEqual({row["actor"] for row in pre_read["accepted"]},
                         {"r1", "r2"})

    def test_a_blocked_parent_cannot_land_through_the_child(self):
        # Témoin de l'auditeur : la porte du parent est bloquée. Ses commits
        # restent donc dans le périmètre de l'enfant, et sa famille d'auteur
        # ne peut pas fournir l'un des deux avis (r1 est claude).
        self._stack(dependency=True)
        self._admit_parent(blocked=True)
        _mark, out = self.freeze()
        self.assertEqual(out["state"], "designated", out)
        self._verdict("r1", "claude")
        self._verdict("r2", "codex")
        authors, _families = self._authors()
        self.assertIn("parent-author", [row["name"] for row in authors])
        gate = self._gate()
        self.assertFalse(gate["ok"], gate["why"])
        self.assertIn("claude", gate["why"])
        changed = self.repo.git("diff", "--name-only", self.base, self.frozen)
        self.assertIn("src/ameesh/parent.py", changed)

    def test_an_unreviewed_parent_stays_in_the_childs_authors(self):
        # Aucun avis sur le parent : sa porte n'est pas satisfaite, même
        # ouverte par un futur verdict de l'enfant.
        self._stack(dependency=True)
        _mark, out = self.freeze()
        self.assertEqual(out["state"], "designated", out)
        self._verdict("r1", "claude")
        self._verdict("r2", "codex")
        authors, _families = self._authors()
        self.assertIn("parent-author", [row["name"] for row in authors])
        self.assertFalse(self._gate()["ok"])

    def test_a_packet_member_is_a_frozen_parent_of_the_child(self):
        # Même empilement, mais le parent n'est lié que par le paquet : la
        # borne vient de `freeze_stack.candidates`, pas des seules arêtes
        # explicites `work dependencies`.
        self._stack(dependency=False)
        self._admit_parent()
        package = work.add(self.db, title="paquet", app="ameesh",
                           branch="integration/paquet", branch_target="main",
                           actor="mesh")["id"]
        storage.of(self.db).work.record_packet(
            package, branch="integration/paquet", base_sha=self.base,
            head_sha=self.frozen,
            units=[{"work_item_id": self.parent, "unit_sha": self.parent_frozen,
                    "frozen_sha": self.parent_frozen},
                   {"work_item_id": self.lot, "unit_sha": self.frozen,
                    "frozen_sha": self.frozen}],
            note="packet composition recorded", actor="mesh")
        _mark, out = self.freeze()
        self.assertEqual(out["state"], "designated", out)
        authors, _families = self._authors()
        self.assertEqual([row["name"] for row in authors], ["author"], authors)
        self._verdict("r1", "claude")
        self._verdict("r2", "codex")
        self.assertTrue(self._gate()["ok"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

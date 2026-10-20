# SPDX-License-Identifier: AGPL-3.0-only
"""#2812 — n'invite pas un agent que la porte écarte sur la nouvelle tête.

#2681 notifie les relecteurs dont le blocage portait sur une tête antérieure.
La notification n'écartait que l'assigné : un correcteur de premier tour
(#2739) ou un coauteur lu par `review_authors` recevait quand même l'invitation
à rendre un verdict que la porte refuserait, et consommait un tour pour rien.
La lecture est celle de la porte, sur la même plage gelée ; une lecture
impossible garde le courrier historique (aucun gel perdu).
"""
from __future__ import annotations

import os

from ameesh import registry, review_designation_activation as act, storage, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

OLD = "b" * 40


class SupersededCorrectorTest(PgTestCase):
    def receipt(self, actor, harness, commits):
        for sha in commits:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
                 self.lot))

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.ops = storage.of(self.db).work
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("src/ameesh/work.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/work.py", "base\nlot\n")
        self.author_commit = self.repo.commit("author: lot", agent="author")
        # a designated reviewer's small first-round fix (#2739): a corrector,
        # not an author of the head
        self.repo.write("src/ameesh/work.py", "base\nlot\ncx2 small fix\n")
        self.corrector_commit = self.repo.commit("cx2: small fix", agent="cx2")
        # a third-party author of the head (exact provenance receipt)
        self.repo.write("src/ameesh/other.py", "coauthored\n")
        self.coauthor_commit = self.repo.commit("dev1: coauthor", agent="dev1")
        self.frozen = self.coauthor_commit
        for name, harness in (("author", "deepseek"), ("cx2", "codex"),
                              ("cx3", "codex"), ("dev1", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                            (name,))
        self.item = work.add(self.db, title="Notification des blocages levés",
                             app="ameesh", assignee="author", source="fixture",
                             actor="fixture")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")
        self.db.execute(
            "UPDATE work_items SET branch_target = %s, reviewers = ARRAY['cx2']"
            " WHERE id = %s", (self.base, self.lot))
        self.receipt("author", "deepseek", [self.author_commit])
        self.receipt("cx2", "codex", [self.corrector_commit])
        self.receipt("dev1", "claude", [self.coauthor_commit])
        self.old = self.ops.add_milestone(self.lot, "frozen", sha=OLD,
                                          actor="author", verdict=None, note="")
        for actor in ("cx2", "cx3", "dev1"):
            self.ops.add_milestone(self.lot, "verdict", sha=OLD, actor=actor,
                                   verdict="blocked", note="")
        self.new = self.ops.add_milestone(self.lot, "frozen", sha=self.frozen,
                                          actor="author", verdict=None, note="")
        self.item = work.get(self.db, self.lot)

    def notify(self):
        threads = []
        act._notify_superseded_blocks(
            self.db, self.item, self.new, {"state": "designated", "reviewers": []},
            threads, cfg=self.cfg)
        return self.db.query(
            "SELECT recipient FROM agent_mailbox ORDER BY id")

    def test_the_corrector_and_a_coauthor_do_not_get_the_invitation(self):
        rows = self.notify()
        # cx3 is an independent blocker of the old head: it is invited.
        # cx2 (small corrector) and dev1 (coauthor) cannot be accepted on the
        # new head (#2739, review_authors): no mail, no lost turn.
        self.assertEqual([row["recipient"] for row in rows], ["cx3"])

    def test_the_guard_reads_the_same_corrector_as_the_gate(self):
        # A direct read of the same source: the corrections of the frozen
        # head name cx2 and only cx2; dev1 is an author, not a corrector.
        correctors = act.corrections_of_head(
            self.db, self.item, self.new, self.repo.root, self.base, self.frozen)
        self.assertEqual([row["name"] for row in correctors], ["cx2"])
        excluded = act._excluded_on_freeze(self.cfg, self.db, self.item, self.new)
        self.assertEqual(excluded, {"author", "cx2", "dev1"})

    def test_an_unreadable_range_keeps_the_historical_mail(self):
        # The notification never loses a freeze: if the range cannot be read,
        # it falls back to the old behaviour (invite the old blockers) rather
        # than guessing an exclusion.
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        ("c" * 40, self.lot))
        self.item = work.get(self.db, self.lot)
        rows = self.notify()
        self.assertEqual([row["recipient"] for row in rows],
                         ["cx2", "cx3", "dev1"])

# SPDX-License-Identifier: AGPL-3.0-only
"""#2681 — une seule lecture des avis et des auteurs.

Le train appelle `merge_queue.review_gate` avec les familles historiques
(`family_of`) et le compte de sa politique (`required`) ; la porte garde son
plancher. Un blocage d'une tête antérieure n'est pas lu ; la désignation et le
train partagent `review_authors`. Le relecteur qui avait bloqué une tête
antérieure est notifié au gel suivant.
"""
from __future__ import annotations

import unittest

from ameesh import merge_queue, review_designation_activation as act, storage, work

from .support import PgTestCase

HEAD = "a" * 40
OLD = "b" * 40
FAMILIES = {"author": "deepseek", "r1": "claude", "r2": "codex",
            "r3": "claude", "blocked": "claude"}


def mark(number, actor, *, sha=HEAD, verdict="ok"):
    return dict(id=number, kind="verdict", actor=actor, at_ts=float(number),
                sha=sha, verdict=verdict)


def klass(cls="sensitive"):
    return {"class": cls, "files": ({"path": "src/ameesh/x.py", "class": cls,
                                     "rule": None},)}


class GateReadingTest(unittest.TestCase):
    """La porte reste l'unique règle ; le train ne fait que lui donner ses données."""

    def gate(self, marks, *, required=None, **kw):
        return merge_queue.review_gate(
            None, marks, HEAD, klass=klass(), required=required,
            item={"assignee": "author"},
            family_of=lambda name: FAMILIES.get(str(name or "").strip().lower(), ""),
            **kw)

    def test_an_old_block_is_not_read_but_a_block_on_the_freeze_is(self):
        old = self.gate([mark(1, "blocked", sha=OLD, verdict="blocked"),
                         mark(2, "r1")], required=1)
        self.assertTrue(old["ok"], old)
        self.assertEqual(old["on_freeze"], 1)
        frozen = self.gate([mark(1, "blocked", verdict="blocked"),
                            mark(2, "r1")], required=1)
        self.assertFalse(frozen["ok"])
        self.assertIn("blocked verdict", frozen["why"])

    def test_the_policy_count_can_raise_but_never_lower_the_gate_floor(self):
        # Une classe sensible sans chemin critique demande un avis à la porte ;
        # une politique à zéro ne l'abaisse pas.
        floor = self.gate([mark(1, "r1")], required=0)
        self.assertEqual(floor["required"], 1)
        self.assertTrue(floor["ok"], floor)
        # Une politique à deux avis sur une classe normale impose deux
        # familles distinctes, comme la porte à deux avis.
        two = self.gate([mark(1, "r1"), mark(2, "r3")], required=2)
        self.assertEqual(two["required"], 2)
        self.assertFalse(two["ok"])
        self.assertEqual(len(two["accepted"]), 1)
        self.assertTrue(self.gate([mark(1, "r1"), mark(2, "r2")],
                                  required=2)["ok"])

    def test_an_invalid_requirement_is_refused_without_raising(self):
        for value in (3, -1, True, "2"):
            with self.subTest(value=value):
                out = self.gate([mark(1, "r1")], required=value)
                self.assertFalse(out["ok"])
                self.assertIn("explicit review requirement", out["error"])

    def test_an_unknown_historical_family_never_counts(self):
        out = self.gate([mark(1, "ghost")], required=1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["accepted"], [])


class SupersededBlockNotificationTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.ops = storage.of(self.db).work
        self.item = work.add(self.db, title="block notification",
                             app="notify-fixture", source="fixture", actor="fixture")
        self.lot = int(self.item["id"])
        self.old = self.ops.add_milestone(self.lot, "frozen", sha=OLD,
                                          actor="author", verdict=None, note="")
        self.ops.add_milestone(self.lot, "verdict", sha=OLD, actor="r1",
                               verdict="blocked", note="")
        self.new = self.ops.add_milestone(self.lot, "frozen", sha=HEAD,
                                          actor="author", verdict=None, note="")
        self.item = work.get(self.db, self.lot)

    def notify(self, outcome):
        threads = []
        act._notify_superseded_blocks(self.db, self.item, self.new, outcome, threads)
        return [record for record in threads]

    def mails(self, recipient="r1"):
        return self.db.query(
            "SELECT recipient, body FROM agent_mailbox WHERE recipient = %s"
            " ORDER BY id", (recipient,))

    def test_the_blocking_reviewer_is_notified_once_on_the_new_freeze(self):
        self.notify({"state": "designated", "reviewers": []})
        mails = self.mails()
        self.assertEqual([row["recipient"] for row in mails], ["r1"])
        self.assertIn("Blocage levé", mails[0]["body"])
        self.assertIn(HEAD[:12], mails[0]["body"])
        # rejouer le même gel ne renvoie pas le courrier
        self.notify({"state": "designated", "reviewers": []})
        self.assertEqual(len(self.mails()), 1)
        notes = [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s", (self.lot,))]
        self.assertTrue(any(note.startswith(act.SUPERSEDED_PREFIX) for note in notes))

    def test_a_reviewer_designated_on_the_new_freeze_is_not_mailed_twice(self):
        self.notify({"state": "designated", "reviewers": [{"name": "r1"}]})
        self.assertEqual(self.mails(), [])

    def test_a_block_on_the_new_head_is_never_called_superseded(self):
        self.ops.add_milestone(self.lot, "verdict", sha=HEAD, actor="r1",
                               verdict="blocked", note="")
        self.notify({"state": "designated", "reviewers": []})
        self.assertEqual(self.mails(), [])

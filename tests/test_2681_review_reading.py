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
FAMILIES = {"author": "deepseek", "coauthor": "claude", "r1": "claude",
            "r2": "codex", "r3": "claude", "blocked": "claude"}


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

    def test_every_recognized_author_is_excluded_from_the_verdicts(self):
        # #2681 : le train lit tous les auteurs de la tête (tiers avec reçu
        # exact, correcteur au-delà du seuil) ; la porte les reçoit et n'exclut
        # plus seulement l'assigné. Un coauteur ne relit ni lui-même ni sa
        # famille.
        authors = [{"name": "coauthor", "family": "claude"}]
        own = self.gate([mark(1, "coauthor")], required=1, authors=authors)
        self.assertFalse(own["ok"], own)
        self.assertEqual(own["accepted"], [])
        same = self.gate([mark(1, "r1")], required=1, authors=authors)
        self.assertFalse(same["ok"], same)
        self.assertEqual(same["accepted"], [])
        # required=2 : un seul avis indépendant ne suffit pas quand l'autre
        # vient d'un auteur ou de sa famille.
        two = self.gate([mark(1, "coauthor"), mark(2, "r2")], required=2,
                        authors=authors)
        self.assertFalse(two["ok"], two)
        self.assertEqual([entry["actor"] for entry in two["accepted"]], ["r2"])
        same_two = self.gate([mark(1, "r1"), mark(2, "r2")], required=2,
                             authors=authors)
        self.assertFalse(same_two["ok"], same_two)
        self.assertEqual([entry["actor"] for entry in same_two["accepted"]], ["r2"])

    def test_a_small_reviewer_fix_keeps_its_family_eligible(self):
        # #2638 keeps a small reviewer fix out of the authors, so its family
        # stays eligible; #2758 makes the exact corrector's own verdict not
        # count on the head carrying its correction. Another agent of the
        # same family can still admit it.
        authors = [{"name": "author", "family": "deepseek"}]
        own = self.gate([mark(1, "r1")], required=1, authors=authors,
                        fixers=["r1"])
        self.assertFalse(own["ok"], own)
        self.assertEqual(own["accepted"], [])
        self.assertEqual(own["corrections"], ["r1"])
        other = self.gate([mark(1, "r3")], required=1, authors=authors,
                          fixers=["r1"])
        self.assertTrue(other["ok"], other)
        self.assertEqual([entry["actor"] for entry in other["accepted"]], ["r3"])

    def test_an_unknown_coauthor_family_refuses_closed(self):
        # L'indépendance ne se suppose jamais : une famille d'auteur inconnue
        # (autres que `human:`) refuse la porte, même pour un coauteur.
        out = self.gate([mark(1, "r2")], required=1,
                        authors=[{"name": "ghost-author", "family": ""}])
        self.assertFalse(out["ok"])
        self.assertIn("no known model family", out["why"])

    def test_a_gate_without_any_known_author_still_refuses_strict(self):
        # Pas d'assigné ni d'auteurs : l'ancienne porte refusait déjà une
        # classe stricte ; `authors` ne doit pas ouvrir ce cas.
        out = merge_queue.review_gate(
            None, [mark(1, "r1")], HEAD, klass=klass(), required=1, item={},
            family_of=lambda name: FAMILIES.get(str(name or "").strip().lower(), ""))
        self.assertFalse(out["ok"])
        self.assertIn("no known model family", out["why"])


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

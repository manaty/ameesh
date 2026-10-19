# SPDX-License-Identifier: AGPL-3.0-only
"""Exact receipt shape and ordering still consumed by the train (#2681).

The review rule itself is the merge gate's (`merge_queue.review_gate`, called
by `train_history`); the per-commit coverage rule was removed with #2681.
These helpers stay shared with the admission/candidate fixtures.
"""
import dataclasses
import unittest

from ameesh.train import TrainError
from ameesh.train_review import MARK_FIELDS, Receipt, _order

HEAD, FIRST = "a" * 40, "b" * 40


def author(commit=FIRST, actor="author", harness="codex"):
    subject = "commit:" + commit
    return Receipt(subject, commit, actor, actor + "-session", actor + "-turn",
                   harness, "exact-commit-event:" + commit)


def verdict(number, actor, harness, *, commit=HEAD, status="ok"):
    mark = dict(id=number, kind="verdict", actor=actor, at_ts=float(number),
                sha=commit, verdict=status)
    proof = Receipt("milestone:%d" % number, commit, actor, actor + "-session",
                    actor + "-turn", harness, "exact-milestone-event:%d" % number,
                    {key: mark[key] for key in MARK_FIELDS})
    return mark, proof


class ReceiptShapeTest(unittest.TestCase):
    def test_known_receipt_requires_every_exact_field_and_a_known_family(self):
        valid = author()
        self.assertTrue(valid.known())
        for field, value in (("session", ""), ("turn", ""), ("record", ""),
                             ("harness", "unknown")):
            with self.subTest(field=field):
                altered = dataclasses.replace(valid, **{field: value})
                self.assertFalse(altered.known())

    def test_order_is_the_exact_verdict_identity_and_date(self):
        self.assertEqual(_order(dict(id=2, actor="r", at_ts=1.5)), (1.5, 2))
        for mark in (dict(id=0, actor="r", at_ts=1.0),
                     dict(id=2, actor="", at_ts=1.0),
                     dict(id=2, actor="r", at_ts=float("nan")),
                     dict(id="x", actor="r", at_ts=1.0)):
            with self.subTest(mark=mark), self.assertRaisesRegex(
                    TrainError, "unreadable verdict identity or date"):
                _order(mark)

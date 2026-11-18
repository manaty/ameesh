# SPDX-License-Identifier: AGPL-3.0-only
import unittest

from ameesh.pre_act_audit import correlate


ID = "1d67c20e-77aa-4f81-88bb-08b3b11444bc"
OTHER = "2d67c20e-77aa-4f81-88bb-08b3b11444bc"


def receipt(value=ID, kind="prod"):
    return {"type": "pre-act/1", "kind": kind, "pregeste_id": value}


def run(number, title):
    return {"id": number, "display_title": title}


class CorrelationTest(unittest.TestCase):
    def test_exact_match_and_reuse(self):
        runs = [run(1, "Deploy pregeste=" + ID), run(2, "Deploy pregeste=" + ID)]
        self.assertEqual(correlate(runs, [receipt()]),
                         [{"run_id": 2, "reason": "already-consumed-id"}])

    def test_missing_invalid_unknown_and_ambiguous(self):
        runs = [run(1, "Deploy"), run(2, "pregeste="),
                run(3, "pregeste=" + OTHER), run(4, "pregeste=" + ID),
                run(5, "pregeste=" + ID + " pregeste=" + OTHER)]
        self.assertEqual([x["reason"] for x in correlate(runs, [receipt(), receipt()])],
                         ["missing-or-invalid-id", "missing-or-invalid-id",
                          "unknown-or-ambiguous-id", "unknown-or-ambiguous-id",
                          "missing-or-invalid-id"])

    def test_non_prod_receipt_and_nonexact_marker_do_not_match(self):
        runs = [run(1, "pregeste=" + ID), run(2, "xpregeste=" + ID)]
        self.assertEqual([x["reason"] for x in correlate(runs, [receipt(kind="kms")])],
                         ["unknown-or-ambiguous-id", "missing-or-invalid-id"])

    def test_duplicate_api_page_does_not_consume_twice(self):
        self.assertEqual(correlate([run(1, "pregeste=" + ID)] * 2, [receipt()]), [])

# SPDX-License-Identifier: AGPL-3.0-only
"""Earlier freezes add recorded authors, never waive unknown provenance."""
import types
import unittest
from unittest import mock

from ameesh import review_authors as a, work
from . import test_3837_delivered_freezes as fixtures


class SourceFreezeAttributionTest(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.DeliveredFreezeTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.repo = fixture.repo
        self.commit, self.target = fixture.old, fixture.target
        self.head = fixture.head
        self.source = {"id": 77, "work_item_id": 10, "sha": self.commit,
                       "at_ts": 1, "kind": "frozen"}
        self.freeze = {"id": 1, "sha": self.commit, "at_ts": 2, "kind": "frozen"}
        self.item = {"id": 3837, "app": "ameesh"}
        self.port = mock.Mock()
        self.port.earlier_freezes.return_value = [self.source]
        self.port.get.return_value = {"id": 10, "app": "ameesh"}
        self.reading = ({"historic": {"claude"}},
                        {"historic": [self.commit]}, {}, [])

    def resolve(self, *, reading=None, receipt=True, fail_note=False):
        with mock.patch.object(a.storage, "of", return_value=types.SimpleNamespace(work=self.port)), \
                mock.patch.object(a, "_milestone_receipt", return_value=(
                    {"commit": self.commit} if receipt else None)), \
                mock.patch.object(a, "_prior_reading", return_value=(
                    self.reading if reading is None else reading)) as read, \
                mock.patch.object(work, "note", side_effect=OSError if fail_note else None) as note:
            result = a._attributed_from_other_freeze(
                object(), self.item, self.freeze, self.commit, self.repo.root,
                ("codex", "claude", "deepseek"), cache={}, target=self.target)
        return result, read, note

    def test_exact_source_adds_recorded_authors_and_trace(self):
        result, read, note = self.resolve()
        self.assertEqual(result, self.reading[:3])
        self.assertEqual(read.call_args.args[1]["id"], 10)
        self.assertEqual(read.call_args.args[3], [self.commit])
        self.assertTrue(read.call_args.kwargs["strict"])
        self.assertIn("attribué par le gel #77 du lot #10", note.call_args.args[2])
        self.assertTrue(note.call_args.kwargs["once"])

    def test_unreadable_other_lot_does_not_unlock(self):
        result, _, note = self.resolve(reading=({}, {}, {}, [self.commit[:12]]))
        self.assertIsNone(result)
        note.assert_not_called()

    def test_missing_family_does_not_unlock(self):
        self.assertIsNone(self.resolve(reading=({"historic": {""}},
                                               {"historic": [self.commit]}, {}, []))[0])

    def test_receipt_must_pin_source_head(self):
        self.assertIsNone(self.resolve(receipt=False)[0])

    def test_later_source_cannot_attribute_earlier_freeze(self):
        self.source["at_ts"] = 3
        result, read, _ = self.resolve()
        self.assertIsNone(result)
        read.assert_not_called()

    def test_same_lot_is_not_an_independent_source(self):
        self.source["work_item_id"] = 3837
        self.assertIsNone(self.resolve()[0])

    def test_other_project_cannot_attribute(self):
        self.port.get.return_value["app"] = "nexlink"
        self.assertIsNone(self.resolve()[0])

    def test_commit_outside_source_head_is_not_attributed(self):
        self.source["sha"] = self.target
        self.assertIsNone(self.resolve()[0])

    def test_unavailable_source_head_does_not_unlock(self):
        self.source["sha"] = "a" * 40
        self.assertIsNone(self.resolve()[0])

    def test_target_baseline_is_not_source_content(self):
        self.target = self.commit
        self.assertIsNone(self.resolve()[0])

    def test_failed_trace_does_not_unlock(self):
        self.assertIsNone(self.resolve(fail_note=True)[0])

    def test_unreadable_storage_does_not_unlock(self):
        self.port.earlier_freezes.side_effect = OSError
        self.assertIsNone(self.resolve()[0])

    def test_carried_reader_adds_family_and_removes_only_resolved_unknown(self):
        self.repo.write("copied.sql", "old_number_545();\n")
        self.head = self.repo.commit("historical contribution delivered")
        current = {"id": 2, "sha": self.head, "kind": "frozen", "at_ts": 3}
        self.port.milestones.return_value = [self.freeze, current]

        def read(_db, item, _freeze, commits, *_args, **_kwargs):
            if item["id"] == 3837:
                return {"current": {"codex"}}, {"current": [self.commit]}, {}, [self.commit[:12]]
            return self.reading

        with mock.patch.object(a.storage, "of", return_value=types.SimpleNamespace(work=self.port)), \
                mock.patch.object(a, "_milestone_receipt", return_value={"commit": self.commit}), \
                mock.patch.object(a, "_prior_reading", side_effect=read), \
                mock.patch.object(a, "_commit_receipt", return_value=None), \
                mock.patch.object(work, "note"):
            authors, unknown, _ = a._carried_from_previous(
                object(), self.item, current, [self.head], self.repo.root,
                ("codex", "claude", "deepseek"), base=self.target,
                head=self.head, target=self.target)
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"historic": "claude", "current": "codex"})
        self.assertEqual(unknown, [])


if __name__ == "__main__":
    unittest.main()

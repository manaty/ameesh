# SPDX-License-Identifier: AGPL-3.0-only
"""#4858 : la recherche du gel source ne croît plus avec les gels de l'app."""
import types
import unittest
from unittest import mock

from ameesh import review_authors as a, work
from . import test_3837_delivered_freezes as fixtures


class AttributionScanTest(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.DeliveredFreezeTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.repo = fixture.repo
        self.commit, self.target = fixture.old, fixture.target
        self.freeze = {"id": 1, "sha": self.commit, "at_ts": 2, "kind": "frozen"}
        self.item = {"id": 3837, "app": "ameesh"}
        self.port = mock.Mock()
        self.port.get.return_value = {"id": 10, "app": "ameesh"}
        self.reading = ({"historic": {"claude"}},
                        {"historic": [self.commit]}, {}, [])

    def resolve(self, sources, *, reading=None, cache=None, receipt=None):
        self.port.earlier_freezes.return_value = sources
        with mock.patch.object(a.storage, "of", return_value=types.SimpleNamespace(
                work=self.port)), \
                mock.patch.object(a, "_milestone_receipt",
                                  return_value=(receipt if receipt is not None
                                                else {"commit": self.commit})), \
                mock.patch.object(a, "_prior_reading",
                                  return_value=(self.reading if reading is None
                                                else reading)) as read, \
                mock.patch.object(a, "_is_ancestor",
                                  wraps=a._is_ancestor) as ancestor, \
                mock.patch.object(work, "note") as note:
            result = a._attributed_from_other_freeze(
                object(), self.item, self.freeze, self.commit, self.repo.root,
                ("codex", "claude", "deepseek"), cache={} if cache is None else cache,
                target=self.target)
        return result, read, note, ancestor

    def unrelated_heads(self, count=8):
        self.repo.git("checkout", "-q", "-b", "elsewhere", self.target)
        heads = []
        for index in range(count):
            self.repo.write("elsewhere", "content %d\n" % index)
            heads.append(self.repo.commit("elsewhere %d" % index))
        return heads

    def test_number_of_calls_does_not_grow_with_earlier_freezes(self):
        heads = self.unrelated_heads()
        small = [{"id": 100 + index, "work_item_id": 10 + index,
                  "sha": heads[index % len(heads)], "at_ts": 1}
                 for index in range(len(heads))]
        large = [{"id": 100 + index, "work_item_id": 10 + index,
                  "sha": heads[index % len(heads)], "at_ts": 1}
                 for index in range(8 * len(heads))]
        first = self.resolve(small)
        second = self.resolve(large)
        self.assertIsNone(first[0])
        self.assertIsNone(second[0])
        for _result, read, note, ancestor in (first, second):
            self.assertEqual(read.call_count, 0)
            self.assertEqual(ancestor.call_count, 0)
            self.assertEqual(self.port.get.call_count, 0)
            note.assert_not_called()
        self.assertLessEqual(self.port.earlier_freezes.call_count, 2)

    def test_containing_descendant_still_attributes(self):
        self.repo.git("checkout", "-q", self.commit)
        self.repo.write("later", "later\n")
        source_sha = self.repo.commit("descendant of the unknown")
        source = {"id": 77, "work_item_id": 10, "sha": source_sha,
                  "at_ts": 1, "kind": "frozen"}
        result, read, note, _ancestor = self.resolve(
            [source], receipt={"commit": source_sha})
        self.assertEqual(result, self.reading[:3])
        self.assertIn("attribué par le gel #77 du lot #10", note.call_args.args[2])
        read.assert_called_once()

    def test_bound_leaves_unknown_beyond_the_source_limit(self):
        self.repo.git("checkout", "-q", self.commit)
        self.repo.write("later", "later\n")
        source_sha = self.repo.commit("descendant of the unknown")
        sources = [{"id": 100 + index, "work_item_id": 10 + index,
                    "sha": source_sha, "at_ts": 1}
                   for index in range(a.MAX_ATTRIBUTION_SOURCES + 8)]
        result, read, note, _ancestor = self.resolve(
            sources, reading=({}, {}, {}, [self.commit[:12]]),
            receipt={"commit": source_sha})
        self.assertIsNone(result)
        self.assertEqual(read.call_count, a.MAX_ATTRIBUTION_SOURCES)
        note.assert_not_called()

    def test_source_item_and_receipt_are_read_once_per_source(self):
        self.repo.git("checkout", "-q", self.commit)
        self.repo.write("one", "one\n")
        first_commit = self.repo.commit("first unknown")
        self.repo.write("two", "two\n")
        second_commit = self.repo.commit("second unknown")
        self.repo.write("later", "later\n")
        source_sha = self.repo.commit("descendant of both")
        source = {"id": 77, "work_item_id": 10, "sha": source_sha,
                  "at_ts": 1, "kind": "frozen"}
        cache = {}
        for commit in (first_commit, second_commit):
            self.commit = commit
            self.resolve([source], reading=({}, {}, {}, [commit[:12]]),
                         cache=cache, receipt={"commit": source_sha})
        self.assertEqual(self.port.get.call_count, 1)
        self.assertEqual(self.port.earlier_freezes.call_count, 1)

    def test_unreadable_containment_leaves_the_unknown_unchanged(self):
        source = {"id": 77, "work_item_id": 10, "sha": self.target,
                  "at_ts": 1, "kind": "frozen"}
        with mock.patch.object(a, "_present_commits", return_value=None):
            result, read, note, _ancestor = self.resolve([source])
        self.assertIsNone(result)
        read.assert_not_called()
        note.assert_not_called()


if __name__ == "__main__":
    unittest.main()

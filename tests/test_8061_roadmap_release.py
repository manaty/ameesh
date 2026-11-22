# SPDX-License-Identifier: AGPL-3.0-only
import unittest

from ameesh import roadmap_release as road


class RoadmapReleaseTests(unittest.TestCase):
    def test_names_are_normalized_and_registry_failure_is_closed(self):
        self.assertIsNone(road.safe_summary('Client Foo fixes', {'client-foo'}))
        self.assertIsNone(road.safe_summary('Anything', None))
        self.assertEqual(road.safe_summary('Useful feature', {'client-foo'}), 'Useful feature')
        self.assertIsNone(road.safe_summary('Line\nbreak', set()))

    def test_patch_only_accepts_one_generated_line(self):
        line = road.render('1.2.3', '2026-11-22', [], [], 3)
        self.assertTrue(road.valid_patch('--- a/x\n+++ b/x\n@@ -1,0 +2 @@\n+' + line + '\n'))
        self.assertFalse(road.valid_patch('--- a/x\n+++ b/x\n-old\n+' + line + '\n'))
        self.assertFalse(road.valid_patch('@@ -1,0 +2 @@\n+' + line + '\n+other\n'))
        self.assertFalse(road.valid_patch('@@ -1,0 +2 @@\n+a client title\n'))
        self.assertFalse(road.valid_patch('@@ -1,0 +2 @@\n+' + line + '\n+++ hidden\n'))

    def test_insert_is_idempotent_and_preserves_manual_text(self):
        page = 'before\n' + road.BEGIN + '\n' + road.END + '\n## In progress\nmanual\n'
        line = road.render('1.2.3', '2026-11-22', ['0123_x.sql'], [], 2)
        result = road.insert(page, line)
        self.assertEqual(road.insert(result, line), result)
        self.assertTrue(result.endswith('## In progress\nmanual\n'))
        with self.assertRaises(ValueError):
            road.insert(result, road.render('1.2.3', '2026-11-23', [], [], 0))


if __name__ == '__main__':
    unittest.main()

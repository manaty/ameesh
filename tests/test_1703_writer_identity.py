# SPDX-License-Identifier: AGPL-3.0-only
"""#1703 N2: one shared identity for the target lease of a single-lot writer.

A direct merge and a packet train must never key the same target differently:
`land.lock_key` keys on `project.name`, so the declared radar project of the
repository comes first, and the fallback — the admitted lot's app, the name
the queue already carries — stays explicit. No repository key is invented.
"""
import shutil
import tempfile
import types
import unittest
from unittest import mock

from ameesh import land, merge_queue


class WriterProjectTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="ameesh-1703-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.project = types.SimpleNamespace(name="ameesh")

    def test_a_declared_project_is_the_train_identity(self):
        # the train keys the lease of this target on its project name: the
        # writer must resolve that same object, whatever the lot's app says.
        with mock.patch.object(merge_queue, "radar_project", return_value=self.project):
            got = merge_queue.writer_project(None, self.repo, "another-app")
        self.assertIs(got, self.project)
        self.assertEqual(land.lock_key(got, "origin/main"),
                         land.lock_key(self.project, "origin/main"))

    def test_without_a_declared_project_the_admitted_app_is_the_identity(self):
        with mock.patch.object(merge_queue, "radar_project", return_value=None):
            got = merge_queue.writer_project(None, self.repo, "  ameesh ")
        self.assertEqual(got.name, "ameesh")
        self.assertEqual(land.lock_key(got, "origin/main"), "ameesh:origin/main")

    def test_an_empty_app_refuses_instead_of_inventing_a_key(self):
        for app in ("", "   ", None):
            with self.subTest(app=app), \
                    mock.patch.object(merge_queue, "radar_project", return_value=None), \
                    self.assertRaisesRegex(merge_queue.QueueError, "undeclared target"):
                merge_queue.writer_project(None, self.repo, app)

    def test_the_declared_project_of_the_repository_wins_over_the_app(self):
        # real settings: the same checkout is declared under another name
        cfg = types.SimpleNamespace(radar={"projects": {"nexlink": {
            "repo": self.repo, "target": "develop", "land": "local",
            "gates": ["true"]}}})
        got = merge_queue.writer_project(cfg, self.repo, "ameesh")
        self.assertEqual(got.name, "nexlink")
        self.assertEqual(land.lock_key(got, "origin/develop"), "nexlink:origin/develop")

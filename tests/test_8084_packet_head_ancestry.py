# SPDX-License-Identifier: AGPL-3.0-only
"""A stale branch sweep must not create a false packet-head notice."""
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import lot_class, planned_waits


class PacketHeadAncestryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lot-8084-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        def git(*args):
            return subprocess.check_output(["git", "-C", self.repo, *args],
                                           stderr=subprocess.DEVNULL).decode().strip()
        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        def commit(message):
            git("commit", "--allow-empty", "-qm", message)
            return git("rev-parse", "HEAD")
        self.parent = commit("parent")
        self.source = commit("freeze")
        self.later = commit("later")
        self.carrier = dict(id=90, state="qa", branch="integration/paquet-test",
                            branch_head="a" * 40)
        self.store = mock.Mock()
        self.store.work.packet_members.return_value = [dict(
            package_id=90, work_item_id=1, head_sha="a" * 40,
            source_sha=self.source)]
        self.store.progress.lot_milestones.return_value = [dict(
            work_item_id=1, kind="frozen", sha=self.source, at_ts=1, id=1)]
        patcher = mock.patch.object(lot_class, "repo_for_freeze",
                                    return_value=(self.repo, self.source))
        patcher.start()
        self.addCleanup(patcher.stop)

    def attachment(self, head):
        moved = {}
        unit = dict(id=1, state="qa", branch_head=head)
        result = planned_waits.recorded_packet_attachments(
            self.store, [self.carrier], [unit], now=10, moved=moved, db=object())
        self.assertEqual(result, {90: {1}})
        return moved

    def test_parent_of_freeze_has_no_notice(self):
        self.assertEqual(self.attachment(self.parent), {})

    def test_later_commit_has_notice(self):
        self.assertEqual(self.attachment(self.later), {
            1: {"vehicle": 90, "frozen": self.source, "head": self.later}})

    def test_unreadable_commit_has_no_notice(self):
        self.assertEqual(self.attachment("f" * 40), {})

    def test_unreadable_repository_has_no_notice(self):
        with mock.patch.object(lot_class, "repo_for_freeze", side_effect=OSError):
            self.assertEqual(self.attachment(self.later), {})

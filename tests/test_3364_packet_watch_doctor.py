# SPDX-License-Identifier: AGPL-3.0-only
"""#3364: the packet watch runs inside `ameesh release tick`.

Cutting `ameesh-release.timer` on a host designated by `packet_watch` stops
the automatic packet merge without any other error (agents-2, 24/10 14:31Z).
`ameesh doctor` names the state and the right gesture: remove the `release`
block to remove the cutter, never disable the timer. It also names a radar
checkout that lacks a frozen head, which makes the freeze designation answer
"no repository holds the frozen head".
"""
from __future__ import annotations

import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import hostcheck


def cfg_with(block=None, host: str = "ameesh-agents-2"):
    return types.SimpleNamespace(
        host=host,
        packet_watch=block if block is not None else {
            "host": "ameesh-agents-2",
            "projects": {"ameesh": {"notify": ["composeur", "pilote"]}}})


def systemctl(state):
    def run(*args):
        assert args[:1] == ("show",), args
        return None if state is None else state + "\n"
    return run


class PacketWatchTimerTest(unittest.TestCase):
    def test_active_release_timer_says_the_watch_runs(self):
        lines = hostcheck.packet_watch_lines(cfg_with(), run=systemctl("active"))
        text = "\n".join(lines)
        self.assertIn("packet-watch", text)
        self.assertIn("ameesh-release.timer active", text)

    def test_an_inactive_timer_names_the_stopped_auto_merge_and_the_gesture(self):
        """Témoin : l'hôte packet_watch dont le tick n'est pas actif."""
        lines = hostcheck.packet_watch_lines(cfg_with(), run=systemctl("inactive"))
        text = "\n".join(lines)
        self.assertIn("warning", text)
        self.assertIn("ameesh-release.timer is not active", text)
        self.assertIn("packet watch", text)
        self.assertIn("stopped", text)
        self.assertIn("systemctl --user enable --now ameesh-release.timer", text)
        self.assertIn("remove this host's `release` block", text)
        self.assertIn("never disable the timer", text)

    def test_another_host_or_no_block_is_silent(self):
        self.assertEqual(hostcheck.packet_watch_lines(
            cfg_with(host="pc-smichea"), run=systemctl("inactive")), [])
        self.assertEqual(hostcheck.packet_watch_lines(
            types.SimpleNamespace(host="pc-smichea", packet_watch={}),
            run=systemctl("inactive")), [])

    def test_without_systemd_the_state_is_not_guessed(self):
        lines = hostcheck.packet_watch_lines(cfg_with(), run=systemctl(None))
        self.assertEqual(len(lines), 1)
        self.assertIn("systemd unavailable", lines[0])
        self.assertNotIn("stopped", lines[0])


class RadarRepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="3364-radar-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name + "/repo"
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        with open(self.repo + "/file", "w", encoding="utf-8") as stream:
            stream.write("x\n")
        subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@example.invalid",
                        "-c", "user.name=t", "add", "."], check=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@example.invalid",
                        "-c", "user.name=t", "commit", "-qm", "seed"], check=True)
        self.head = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
            text=True, check=True).stdout.strip()

    def store(self, rows, marks):
        return types.SimpleNamespace(
            work=types.SimpleNamespace(open_for_sweep=lambda limit: rows),
            progress=types.SimpleNamespace(lot_milestones=lambda ids: marks))

    def lines(self, rows, marks, repo=None):
        project = types.SimpleNamespace(name="ameesh", repo=repo or self.repo, target="main")
        settings = types.SimpleNamespace(projects={"ameesh": project})
        with mock.patch("ameesh.radar.settings_of", return_value=settings), \
                mock.patch("ameesh.storage.of", return_value=self.store(rows, marks)):
            return hostcheck.radar_repo_lines(types.SimpleNamespace(host="agents-2"), None)

    def test_a_frozen_head_absent_from_the_radar_repo_is_named(self):
        rows = [{"id": 1, "app": "ameesh"}, {"id": 2, "app": "ameesh"}]
        marks = [{"work_item_id": 1, "kind": "frozen", "sha": self.head},
                 {"work_item_id": 2, "kind": "frozen", "sha": "f" * 40}]
        lines = self.lines(rows, marks)
        text = "\n".join(lines)
        self.assertIn("warning", text)
        self.assertIn("radar repo %s lacks the frozen head" % self.repo, text)
        self.assertIn("#2 (%s)" % ("f" * 12), text)
        self.assertNotIn("#1 (", text)
        self.assertIn("git -C %s fetch origin" % self.repo, text)

    def test_every_present_head_is_silent(self):
        rows = [{"id": 1, "app": "ameesh"}]
        marks = [{"work_item_id": 1, "kind": "frozen", "sha": self.head}]
        self.assertEqual(self.lines(rows, marks), [])

    def test_a_missing_radar_checkout_is_named_once(self):
        rows = [{"id": 1, "app": "ameesh", "branch": "lot/1-x"}]
        marks = [{"work_item_id": 1, "kind": "frozen", "sha": self.head}]
        lines = self.lines(rows, marks, repo=self.tmp.name + "/absent")
        self.assertEqual(len(lines), 1)
        self.assertIn("is not a directory", lines[0])
        self.assertIn("radar.projects.ameesh.repo", lines[0])

    def test_a_lot_without_a_frozen_milestone_is_not_checked(self):
        rows = [{"id": 1, "app": "ameesh"}]
        marks = [{"work_item_id": 1, "kind": "request", "sha": ""}]
        self.assertEqual(self.lines(rows, marks), [])

    def test_no_radar_project_or_no_open_lot_is_silent(self):
        with mock.patch("ameesh.radar.settings_of",
                        return_value=types.SimpleNamespace(projects={})):
            self.assertEqual(hostcheck.radar_repo_lines(
                types.SimpleNamespace(host="agents-2"), None), [])
        self.assertEqual(self.lines([], []), [])


if __name__ == "__main__":
    unittest.main()

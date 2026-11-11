# SPDX-License-Identifier: AGPL-3.0-only
"""#6825 : le véhicule se lit dans un clone local après relève de pilote."""
import os
from types import SimpleNamespace
from unittest import mock
import unittest

from ameesh import forge_packets
from . import test_6088_forge_packets as fixtures

git = fixtures.git


class LocalVehicleTest(unittest.TestCase):
    setUp = fixtures.ForgePacketTest.setUp
    gh = fixtures.ForgePacketTest.gh
    pr = fixtures.ForgePacketTest.pr
    frozen_ms = fixtures.ForgePacketTest.frozen_ms

    def lookup(self, *, cfg=None, db=None, pr=None, frozen=None):
        return forge_packets.vehicle(
            cfg, {"id": 42, "app": "product", "assignee": "author-on-other-host"},
            self.frozen_ms(frozen or self.frozen), db=db,
            gh=self.gh([self.pr() if pr is None else pr]), cache={})

    def test_relieved_pilot_uses_local_project_by_repository_slug(self):
        # L'app du lot et la clé radar locale peuvent différer ; le slug fait
        # foi. Le cwd partagé de l'auteur appartient à l'ancien hôte.
        git(self.repo, "remote", "add", "origin", "https://github.com/o/x.git")
        cfg = SimpleNamespace(radar={"projects": {
            "local-project": {"repo": self.repo, "github": "o/x", "target": "develop"}}})
        with mock.patch("ameesh.lot_class.repository", return_value=None), \
                mock.patch("ameesh.registry.get", return_value={"cwd": self.repo + "/gone"}):
            self.assertEqual(self.lookup(cfg=cfg, db=object())["pr"], 9)

    def test_registry_checkout_survives_missing_radar_path(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/o/x.git")
        with mock.patch("ameesh.lot_class.repository", return_value=self.repo + "/gone"), \
                mock.patch("ameesh.registry.get", return_value={"cwd": self.repo}):
            self.assertEqual(self.lookup(db=object())["pr"], 9)

    def test_manual_lookup_from_own_clone_without_radar(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/o/x.git")
        with mock.patch("ameesh.lot_class.repository", return_value=None), \
                mock.patch("ameesh.fire.os.getcwd", return_value=self.repo):
            self.assertEqual(self.lookup()["pr"], 9)

    def test_wrong_repository_does_not_silence_the_alert(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/other/repository.git")
        with mock.patch("ameesh.registry.get", return_value={"cwd": self.repo}):
            self.assertIn("no local clone of o/x", self.lookup(db=object())["unreadable"])

    def test_missing_forge_head_is_distinct_from_missing_clone(self):
        result = self.lookup(pr=dict(self.pr(), headRefOid=""))
        self.assertIn("head absent from forge", result["unreadable"])
        self.assertNotIn("no local clone", result["unreadable"])

    def test_missing_frozen_commit_is_unreadable_not_absent_vehicle(self):
        result = self.lookup(frozen="f" * 40)
        self.assertIn("ancestry of frozen head", result["unreadable"])

    def test_missing_packet_commit_is_distinct_from_missing_clone(self):
        result = self.lookup(pr=dict(self.pr(), headRefOid="f" * 40))
        self.assertIn("head ffffffffffff not readable locally", result["unreadable"])

    def test_project_clone_without_packet_head_uses_registry_clone(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/o/x.git")
        stale = os.path.join(self.repo, "stale")
        git(self.repo, "clone", "--single-branch", "--branch", "develop",
            "--no-local", self.repo, stale)
        git(stale, "remote", "set-url", "origin", "https://github.com/o/x.git")
        with mock.patch("ameesh.lot_class.repository", return_value=stale), \
                mock.patch("ameesh.registry.get", return_value={"cwd": self.repo}):
            self.assertEqual(self.lookup(db=object())["pr"], 9)
            result = forge_packets.vehicle(
                None, {"id": 99, "app": "product", "assignee": "author"},
                self.frozen_ms(self.frozen), db=object(),
                gh=self.gh([self.pr()]), cache={})
            self.assertEqual(result, {})

    def test_each_pr_selects_its_own_readable_clone(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/o/x.git")
        project = os.path.join(self.repo, "project")
        git(self.repo, "clone", "--single-branch", "--branch", "develop",
            "--no-local", self.repo, project)
        git(project, "fetch", "origin", "agent/43")
        git(project, "checkout", "-q", "-b", "integration/paquet-10-x", "develop")
        git(project, "merge", "-q", "--no-ff", "FETCH_HEAD",
            "-m", "composition : #43\n\nameesh-work: 43")
        second_head = git(project, "rev-parse", "HEAD")
        git(project, "remote", "set-url", "origin", "https://github.com/o/x.git")
        with mock.patch("ameesh.lot_class.repository", return_value=project), \
                mock.patch("ameesh.registry.get", return_value={"cwd": self.repo}):
            result = forge_packets.vehicle(
                None, {"id": 43, "app": "product", "assignee": "author"},
                self.frozen_ms(self.other), db=object(),
                gh=self.gh([self.pr(), dict(self.pr(head=second_head,
                    branch="integration/paquet-10-x"), number=10)]), cache={})
        self.assertEqual(result["pr"], 10)

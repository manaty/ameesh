# SPDX-License-Identifier: AGPL-3.0-only
"""#6088 : une unité portée par une PR de paquet ouverte sans lot porteur."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import forge_packets, packet_candidates

from .test_4572_slo_candidate_cause import _SloBase


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    return subprocess.run(["git", "-C", repo, *args], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


class ForgePacketTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        repo = self.repo = self.tmp.name
        git(repo, "init", "-q", "-b", "develop")
        git(repo, "commit", "-q", "--allow-empty", "-m", "base")
        git(repo, "checkout", "-q", "-b", "agent/42")
        git(repo, "commit", "-q", "--allow-empty", "-m", "unit 42")
        self.frozen = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "agent/43", "develop")
        git(repo, "commit", "-q", "--allow-empty", "-m", "unit 43")
        self.other = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "integration/paquet-9-x", "develop")
        git(repo, "merge", "-q", "--no-ff", "agent/42",
            "-m", "composition : #42\n\nameesh-work: 42")
        self.head = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "develop")
        patches = [
            mock.patch("ameesh.work.lot_repository", return_value="o/x"),
            mock.patch("ameesh.lot_class.repository", return_value=repo),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def gh(self, rows, rc=0):
        calls = []

        def run(args):
            calls.append(args)
            return SimpleNamespace(returncode=rc, stdout=json.dumps(rows), stderr="")
        run.calls = calls
        return run

    def pr(self, head=None, branch="integration/paquet-9-x"):
        return {"number": 9, "headRefName": branch, "headRefOid": head or self.head,
                "baseRefName": "develop"}

    def frozen_ms(self, sha):
        return [{"kind": "frozen", "sha": sha, "at_ts": 1.0, "id": 1}]

    def test_unit_in_open_packet_pr_is_carried(self):
        found = forge_packets.vehicle(None, {"id": 42}, self.frozen_ms(self.frozen),
                                      gh=self.gh([self.pr()]), cache={})
        self.assertEqual(found, {"pr": 9, "branch": "integration/paquet-9-x",
                                 "head": self.head})

    def test_unit_not_cited_is_not_carried(self):
        found = forge_packets.vehicle(None, {"id": 43}, self.frozen_ms(self.other),
                                      gh=self.gh([self.pr()]), cache={})
        self.assertEqual(found, {})

    def test_cited_but_newer_freeze_not_in_head_is_not_carried(self):
        git(self.repo, "checkout", "-q", "agent/42")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "refreeze")
        newer = git(self.repo, "rev-parse", "HEAD")
        found = forge_packets.vehicle(None, {"id": 42}, self.frozen_ms(newer),
                                      gh=self.gh([self.pr()]), cache={})
        self.assertEqual(found, {})

    def test_non_packet_branch_ignored(self):
        found = forge_packets.vehicle(
            None, {"id": 42}, self.frozen_ms(self.frozen),
            gh=self.gh([self.pr(branch="agent/42")]), cache={})
        self.assertEqual(found, {})

    def test_forge_unreadable_names_the_failure(self):
        found = forge_packets.vehicle(None, {"id": 42}, self.frozen_ms(self.frozen),
                                      gh=self.gh([], rc=1), cache={})
        self.assertIn("forge unreadable", found.get("unreadable", ""))

    def test_head_absent_locally_is_not_concluded(self):
        found = forge_packets.vehicle(None, {"id": 42}, self.frozen_ms(self.frozen),
                                      gh=self.gh([self.pr(head="f" * 40)]), cache={})
        self.assertIn("not readable locally", found.get("unreadable", ""))

    def test_no_freeze_no_lookup(self):
        gh = self.gh([self.pr()])
        self.assertEqual(forge_packets.vehicle(None, {"id": 42}, [], gh=gh, cache={}), {})
        self.assertEqual(gh.calls, [])

    def test_open_pr_list_is_cached_per_repository(self):
        gh, cache = self.gh([self.pr()]), {}
        for _ in range(3):
            forge_packets.vehicle(None, {"id": 42}, self.frozen_ms(self.frozen),
                                  gh=gh, cache=cache, now=100.0)
        self.assertEqual(len(gh.calls), 1)
        forge_packets.vehicle(None, {"id": 42}, self.frozen_ms(self.frozen), gh=gh,
                              cache=cache, now=100.0 + forge_packets.TTL_S + 1)
        self.assertEqual(len(gh.calls), 2)


class PassTest(_SloBase):
    """La passe d'alertes : véhicule lu, l'unité n'escalade pas ; forge
    illisible, l'alerte part et nomme l'échec (conditions 2 et 3)."""

    READY = {"code": "", "detail": "", "owner": "", "gesture": "",
             "ready": True, "unknown": False}

    def alert_with(self, found):
        with mock.patch.object(forge_packets, "vehicle", return_value=found) as seen, \
                mock.patch.object(packet_candidates, "candidate_refusal",
                                  return_value=dict(self.READY)):
            alerts = self.alert(now=self.GO + 6 * 3600.0)
        return alerts, seen

    def test_carried_by_open_packet_pr_no_escalation(self):
        alerts, seen = self.alert_with({"pr": 9, "branch": "integration/paquet-9-x",
                                        "head": "c" * 40})
        self.assertTrue(seen.called)
        self.assertEqual(alerts, [])

    def test_unreadable_forge_keeps_alert_and_names_it(self):
        alerts, _ = self.alert_with({"unreadable": "forge unreadable (rc 1)"})
        self.assertEqual(len(alerts), 1, alerts)
        self.assertIn("open packet PRs not concluded: forge unreadable (rc 1)",
                      alerts[0]["detail"])

    def test_no_vehicle_keeps_alert_unchanged(self):
        alerts, _ = self.alert_with({})
        self.assertEqual(len(alerts), 1, alerts)
        self.assertNotIn("open packet PRs", alerts[0]["detail"])


if __name__ == "__main__":
    unittest.main()

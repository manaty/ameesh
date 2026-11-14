# SPDX-License-Identifier: AGPL-3.0-only
"""#7365: a reported staging server is a version lag, never a permanent state.

The staging sequence cannot deploy a server without root (`vm.sh` server mode
runs `ssh root@$VM`); a permanent `--absent` therefore hides a host that no
cutoff ever deploys. The release report names it and the release timer alerts
the pilot and the bureau once per release. A reported workstation is normal.
"""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from ameesh import release_cutoff, staging_deferred

SHA = "a" * 40
OLD = "b" * 40
TAG = {"version": "1.54.0", "tag": "v1.54.0", "commit": SHA, "at": 2.0}
OLD_TAG = {"version": "1.48.0", "tag": "v1.48.0", "commit": OLD, "at": 1.0}
AGENTS = ("ameesh-agents-1", "ameesh-agents-2", "ameesh-agents-3")


def attestation(sha, *, reportes=None, hotes=None, date="2026-11-14T10:00:00Z"):
    return {"schema": "ameesh-staging-attestation/1", "sha": sha, "date": date,
            "hotes": (hotes if hotes is not None
                      else {host: {"actif": sha} for host in AGENTS}
                      | ({} if reportes else {})),
            "reportes": reportes or {}}


class DeferredGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=self.tmp.name)
        self.project_ref = release_cutoff.ProjectRelease(
            name="ameesh", repo="/nowhere", target="main", remote="origin",
            report="human", github="manaty/ameesh")

    def tick(self, rows, by_commit, *, pilots=("pilot",)):
        with mock.patch.object(release_cutoff, "stored_attestations",
                               return_value=rows), \
             mock.patch.object(release_cutoff, "_tags_by_commit",
                               return_value=by_commit), \
             mock.patch.object(staging_deferred, "_registry_rows", return_value=[]), \
             mock.patch("ameesh.alert_briefs.project_pilots",
                        return_value=list(pilots)), \
             mock.patch("ameesh.mail.send") as send:
            report = staging_deferred.tick(self.cfg, object(), self.project_ref)
        return report, send

    def test_reported_server_alerts_once_per_release_and_names_the_last_proof(self):
        deferred = {"manaty-mesh-1": {"raison": "central runtime deferred; follow up separately"}}
        rows = [attestation(SHA, reportes=deferred, hotes={h: {"actif": SHA} for h in AGENTS}),
                attestation(OLD, hotes={h: {"actif": OLD} for h in AGENTS + ("manaty-mesh-1",)})]
        report, send = self.tick(rows, {SHA: TAG, OLD: OLD_TAG})
        self.assertEqual(report["result"], "alerted")
        self.assertEqual(report["tag"], "v1.54.0")
        self.assertEqual(report["hosts"], ["manaty-mesh-1"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual({call.args[2] for call in send.call_args_list},
                         {"bureau", "pilot"})
        body = send.call_args_list[0].args[3]
        self.assertIn("v1.54.0", body)
        self.assertIn("manaty-mesh-1", body)
        self.assertIn("central runtime deferred", body)
        self.assertIn("v1.48.0", body)
        self.assertIn("vm.sh installer", body)
        self.assertIn("vm.sh controler", body)
        self.assertIn(OLD[:12], body)
        # the same release does not re-alert the same recipients
        report, send = self.tick(rows, {SHA: TAG, OLD: OLD_TAG})
        self.assertEqual(report["result"], "already-sent")
        self.assertEqual(send.call_count, 0)

    def test_a_new_release_rearms_the_alert(self):
        deferred = {"manaty-mesh-1": {"raison": "deferred"}}
        rows = [attestation(SHA, reportes=deferred)]
        self.tick(rows, {SHA: TAG})
        newer = "c" * 40
        tag = {"version": "1.55.0", "tag": "v1.55.0", "commit": newer, "at": 3.0}
        report, send = self.tick([attestation(newer, reportes=deferred), *rows],
                                 {newer: tag, SHA: TAG})
        self.assertEqual(report["result"], "alerted")
        self.assertEqual(report["tag"], "v1.55.0")
        self.assertEqual(send.call_count, 2)

    def test_reported_workstation_is_not_a_version_lag(self):
        rows = [attestation(SHA, reportes={"pc-smichea": {"raison": "poste absent"}})]
        report, send = self.tick(rows, {SHA: TAG})
        self.assertEqual(report["result"], "none")
        self.assertEqual(send.call_count, 0)

    def test_no_tagged_attestation_concludes_nothing(self):
        rows = [attestation("d" * 40, reportes={"manaty-mesh-1": {"raison": "deferred"}})]
        report, send = self.tick(rows, {SHA: TAG})
        self.assertEqual(report["result"], "none")
        self.assertEqual(send.call_count, 0)

    def test_a_newer_proof_lifts_the_alert(self):
        """The other outcome: the root path is given, `--absent` removed."""
        newer = "e" * 40
        rows = [attestation(newer, hotes={h: {"actif": newer}
                                         for h in AGENTS + ("manaty-mesh-1",)}),
                attestation(SHA, reportes={"manaty-mesh-1": {"raison": "deferred"}})]
        report, send = self.tick(rows, {SHA: TAG})
        self.assertEqual(report["result"], "none")
        self.assertEqual(send.call_count, 0)

    def test_unreadable_attestations_conclude_nothing(self):
        report, send = self.tick([], {})
        self.assertEqual(report["result"], "none")
        self.assertEqual(send.call_count, 0)


class ReportBodyTest(unittest.TestCase):
    def test_release_report_names_the_reported_host_and_the_missed_tag(self):
        project = types.SimpleNamespace(name="ameesh", target="main")
        rules = types.SimpleNamespace(zone=lambda: ZoneInfo("Europe/Paris"))
        staging = {"exit": 0, "duration_s": 12.0, "log": "/tmp/journal", "summary": [],
                   "attestation": attestation(SHA, reportes={
                       "manaty-mesh-1": {"raison": "central runtime deferred"},
                       "pc-smichea": {"raison": "poste absent"}})}
        body = release_cutoff._report_body(
            project, None, result="released", version="1.54.0", tag="v1.54.0",
            release_sha=SHA, staging=staging, deviations=[], previous=None,
            cutoff_at=0.0, cutoff_label="10:00", rules=rules)
        self.assertIn("manaty-mesh-1: REPORTÉ (central runtime deferred) — non déployé "
                      "au tag v1.54.0", body)
        self.assertIn("pc-smichea: REPORTÉ (poste absent) — non déployé au tag v1.54.0 ; "
                      "mise à jour à son retour", body)

    def test_report_without_reported_host_keeps_its_shape(self):
        project = types.SimpleNamespace(name="ameesh", target="main")
        rules = types.SimpleNamespace(zone=lambda: ZoneInfo("Europe/Paris"))
        body = release_cutoff._report_body(
            project, None, result="released", version="1.54.0", tag="v1.54.0",
            release_sha=SHA, staging={"exit": 0, "duration_s": 1.0, "log": "l",
                                      "summary": [], "attestation": attestation(SHA)},
            deviations=[], previous=None, cutoff_at=0.0, cutoff_label="10:00",
            rules=rules)
        self.assertNotIn("REPORTÉ", body)


if __name__ == "__main__":
    unittest.main()

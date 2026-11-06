# SPDX-License-Identifier: AGPL-3.0-only
"""#6020: Nexlink staging handed to the bureau, two-letter versions, release-<v> tags."""
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import nexlink_release as nx, release_cutoff as rc

SHA = "a" * 40
RUNS = {"deploy-api.yml": 11, "deploy-web.yml": 12, "deploy-media-worker.yml": 13}


class VersionTests(unittest.TestCase):
    def test_letters_count_like_columns(self):
        for value, expected in (("11ar", "11as"), ("11az", "11ba"), ("11z", "11aa"),
                                ("11zz", "11aaa"), ("11v", "11w")):
            self.assertEqual(nx.next_version(value), expected)
        ordered = ["11y", "11z", "11aa", "11ar", "11az", "11ba", "11zz", "11aaa", "12a"]
        self.assertEqual(sorted(reversed(ordered), key=nx.version_key), ordered)
        for name in ("11", "11aaaa", "11A", "release-11as"):
            with self.assertRaises(rc.ReleaseError):
                nx.version_key(name)


class PublishedVersionTests(unittest.TestCase):
    """Real refs: release/11z branch, release-11ar light and release-11as annotated tags."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.remote, self.repo = root / "remote.git", root / "repo"
        subprocess.check_call(["git", "init", "-q", "--bare", str(self.remote)])
        subprocess.check_call(["git", "init", "-q", str(self.repo)])
        for key, value in (("user.name", "t"), ("user.email", "t@localhost")):
            self.git("config", key, value)
        self.git("remote", "add", "origin", str(self.remote))
        self.shas = []
        for name in ("z", "ar", "as"):
            (self.repo / "f").write_text(name)
            self.git("add", ".")
            self.git("commit", "-qm", name)
            self.shas.append(self.git("rev-parse", "HEAD").strip())
        self.git("push", "-q", "origin", self.shas[0] + ":refs/heads/release/11z")
        self.git("tag", "release-11ar", self.shas[1])
        self.git("tag", "-a", "release-11as", self.shas[2], "-m", "11as")
        self.git("push", "-q", "origin", "--tags")
        self.project = rc.ProjectRelease("nexlink", str(self.repo), target="develop",
                                         model="nexlink")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True)

    def test_tags_extend_the_branch_sequence(self):
        known = nx.branches(self.project)
        self.assertEqual(known, {"11z": self.shas[0], "11ar": self.shas[1],
                                 "11as": self.shas[2]})
        self.assertEqual(nx.choose_version(self.project), "11at")
        self.assertEqual(nx.previous_release(self.project, "fallback"), self.shas[2])
        with self.assertRaises(rc.ReleaseError):
            nx.choose_version(self.project, "11ar")


class ConfigurationTests(unittest.TestCase):
    def test_handoff_is_a_nexlink_key_without_credential(self):
        cfg = types.SimpleNamespace(humans="", radar={})
        with mock.patch.object(rc.radar, "settings_of",
                               return_value=types.SimpleNamespace(projects={})):
            project = rc.ProjectRelease.from_config(cfg, "nexlink", {
                "repo": ".", "target": "develop", "model": "nexlink",
                "staging_handoff": "bureau", "handoff_timeout_s": 600})
            self.assertEqual((project.staging_handoff, project.handoff_timeout_s),
                             ("bureau", 600.0))
            self.assertEqual(rc.child_credentials(project), frozenset())
            plain = rc.ProjectRelease.from_config(cfg, "nexlink", {
                "repo": ".", "target": "develop", "model": "nexlink"})
            self.assertEqual(rc.child_credentials(plain), {"SUPABASE_ACCESS_TOKEN"})
            for entry in ({"repo": ".", "staging_handoff": "bureau"},
                          {"repo": ".", "target": "develop", "model": "nexlink",
                           "staging_handoff": "bureau; rm"}):
                with self.assertRaises(rc.ReleaseError):
                    rc.ProjectRelease.from_config(
                        cfg, "nexlink" if "model" in entry else "ameesh", entry)


class RecordTests(unittest.TestCase):
    def test_attestation_shape_checked_before_the_event(self):
        with mock.patch("ameesh.events.record", return_value=7) as record:
            self.assertEqual(nx.record_handoff(
                object(), project="nexlink", version="11at", decision="attested",
                actor="bureau", authority="agent", sha=SHA, runs=dict(RUNS),
                migrations=nx.parse_migrations(["hub:540", "content:540"]), now=5), 7)
        args, data = record.call_args.args, record.call_args.kwargs
        self.assertEqual(args[1:], ("release.handoff", "release:nexlink:11at"))
        self.assertEqual(data["migrations"], [{"target": "content", "version": 540},
                                              {"target": "hub", "version": 540}])
        for bad in (dict(sha="abc"), dict(runs={"deploy-api.yml": 1})):
            with self.subTest(bad=bad), mock.patch("ameesh.events.record") as record:
                with self.assertRaises(rc.ReleaseError):
                    nx.record_handoff(object(), project="nexlink", version="11at",
                                      decision="attested", actor="bureau", authority="agent",
                                      now=5, **dict(dict(sha=SHA, runs=dict(RUNS)), **bad))
                record.assert_not_called()
        for texts in (["deploy-api.yml=1"], ["deploy-api-prod.yml=1", "deploy-web.yml=2",
                                             "deploy-media-worker.yml=3"]):
            with self.assertRaises(rc.ReleaseError):
                nx.parse_runs(texts)
        with self.assertRaises(rc.ReleaseError):
            nx.parse_migrations(["prod:540"])


def api_run(workflow, run_id, **change):
    row = dict(path=".github/workflows/" + workflow, head_sha=SHA, head_branch="release/11at",
               event="workflow_dispatch", status="completed", conclusion="success",
               html_url="https://example/run/%d" % run_id)
    row.update(change)
    return row


class HandoffPipelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=temp.name)
        self.db = object()
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         github="acme/nexlink", model="nexlink",
                                         staging_handoff="bureau", handoff_timeout_s=3600)
        self.pending = dict(sha=SHA, main="b" * 40, base="c" * 40, tag="release-11at",
                            version="11at", cutoff_at=100, label="11:00",
                            integration="integration/release-11at", empty=False,
                            ci_max_inflight=3, ci_parts=[{"workflow": "backend-suite.yml"}])
        rc.save_state(self.cfg, {"schema": rc.SCHEMA,
                                 "projects": {"nexlink": {"pending_nexlink": self.pending}}})
        self.slot_at = 100
        patches = [mock.patch.object(rc, "compiled_rules",
                                     return_value=(object(), types.SimpleNamespace(commit="d" * 40))),
                   mock.patch.object(rc, "cutoff_slot",
                                     side_effect=lambda rules, now: (self.slot_at, "23:00")),
                   mock.patch.object(rc, "fetch"), mock.patch.object(rc, "write_report"),
                   mock.patch.object(nx, "ensure_ref"),
                   mock.patch.object(nx, "migration_requirements", return_value={
                       "hub": [{"version": 540, "name": "540_x", "checksum": "c"}]})]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent = []
        patcher = mock.patch.object(rc, "send_report", side_effect=lambda cfg, db, project, body,
                                    report, **kw: self.sent.append((body, report, kw)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tick(self, now, events=()):
        with mock.patch.object(nx, "handoff_events", return_value=list(events)):
            return rc.execute(self.cfg, self.db, self.project, now=now)

    def slot(self):
        return rc.load_state(self.cfg)["projects"]["nexlink"]

    def handed_over(self):
        with mock.patch.object(nx, "workflow_step", return_value=True) as dispatch, \
                mock.patch.object(nx, "publish", return_value="https://example/pr/1"), \
                mock.patch.object(nx, "migration_gate") as gate, \
                mock.patch.object(nx, "migration_rows") as rows:
            report = self.run_tick(1000)
        gate.assert_not_called()
        rows.assert_not_called()
        self.assertTrue(all(not call.kwargs["staging"] for call in dispatch.call_args_list))
        return report

    def attestation(self, **change):
        data = dict(decision="attested", actor="bureau", authority="agent", sha=SHA,
                    version="11at", runs=dict(RUNS),
                    migrations=[{"target": "hub", "version": 540}])
        data.update(change)
        return data

    def test_cutoff_tags_reports_and_waits_without_token(self):
        with mock.patch.dict(os.environ, {"SUPABASE_ACCESS_TOKEN": ""}):
            report = self.handed_over()
        self.assertEqual(report["result"], "waiting-attestation")
        handoff = self.slot()["pending_nexlink"]["handoff"]
        self.assertEqual((handoff["to"], handoff["deadline"]), ("bureau", 4600))
        body, _report, kw = self.sent[-1]
        self.assertEqual(kw["also"], ("bureau",))
        self.assertIn("release-11at", body)
        self.assertIn("--migration hub:540", body)
        self.assertIn("ameesh release attest --project nexlink --version 11at", body)

    def test_valid_attestation_closes_the_cutoff(self):
        self.handed_over()
        runs = {run_id: api_run(workflow, run_id) for workflow, run_id in RUNS.items()}
        with mock.patch.object(nx, "gh", side_effect=lambda project, args: json.dumps(
                runs[int(args[1].rsplit("/", 1)[1])])):
            report = self.run_tick(2000, [(5, self.attestation())])
        self.assertEqual(report["result"], "released")
        self.assertTrue(report["staging_ok"])
        self.assertEqual(report["staging_attestation"]["event"], 5)
        self.assertNotIn("pending_nexlink", self.slot())
        self.assertEqual(self.slot()["last_cutoff"], 100)
        self.assertIn("staging attesté par bureau", self.sent[-1][0])

    def test_running_run_waits_and_wrong_attestations_are_refused_once(self):
        self.handed_over()
        with mock.patch.object(nx, "gh", return_value=json.dumps(
                api_run("deploy-api.yml", 11, status="in_progress", conclusion=None))):
            report = self.run_tick(2000, [(5, self.attestation())])
        self.assertEqual(report["result"], "waiting-attestation")
        self.assertIn("still in_progress", self.slot()["pending_nexlink"]["handoff"]["waiting"])
        wrong = [(6, self.attestation(actor="mty-amh-impl-3")),
                 (7, self.attestation(migrations=[])),
                 (8, self.attestation(sha="e" * 40))]
        before = len(self.sent)
        with mock.patch.object(nx, "gh", return_value=json.dumps(
                api_run("deploy-api.yml", 11, conclusion="failure"))):
            self.assertEqual(self.run_tick(2100, [(5, self.attestation())] + wrong)["result"],
                             "waiting-attestation")
            self.run_tick(2200, [(5, self.attestation())] + wrong)
        rejected = self.slot()["pending_nexlink"]["handoff"]["rejected"]
        self.assertEqual(sorted(rejected), ["5", "6", "7", "8"])
        self.assertIn("concluded failure", rejected["5"])
        self.assertIn("may not attest", rejected["6"])
        self.assertIn("540_x on hub not attested", rejected["7"])
        self.assertIn("not the frozen candidate", rejected["8"])
        self.assertEqual(len(self.sent) - before, 4)

    def test_missing_attestation_announced_once_at_deadline(self):
        self.handed_over()
        before = len(self.sent)
        self.run_tick(4000)
        self.assertEqual(len(self.sent), before)
        for now in (4600, 5000):
            self.assertEqual(self.run_tick(now)["result"], "waiting-attestation")
        self.assertEqual(len(self.sent), before + 1)
        body, _report, kw = self.sent[-1]
        self.assertIn("attestation de staging absente", body)
        self.assertTrue(kw["urgent"])

    def test_next_cutoff_announced_then_pilot_passes(self):
        self.handed_over()
        before = len(self.sent)
        self.slot_at = 50000
        for now in (50010, 50020):
            self.assertEqual(self.run_tick(now)["result"], "waiting-attestation")
        # Past the deadline too: each announcement once, the blocked cutoff last.
        self.assertEqual(len(self.sent), before + 2)
        self.assertIn("attestation de staging absente", self.sent[-2][0])
        self.assertIn("ameesh release pass --project nexlink --version 11at", self.sent[-1][0])
        passed = (9, dict(decision="passed", actor="mesh-design", authority="corrector",
                          version="11at"))
        refused = (8, dict(decision="passed", actor="bureau", authority="agent", version="11at"))
        with mock.patch.object(nx, "snapshot", return_value=("h", "c2", "m2", {}, None)), \
                mock.patch.object(nx, "choose_version", return_value="11au"), \
                mock.patch.object(nx, "merge_candidate", return_value="f" * 40), \
                mock.patch.object(nx, "previous_release", return_value="m2"), \
                mock.patch.object(rc, "same_tree", return_value=False), \
                mock.patch.object(rc, "due", return_value={"missed": True}), \
                mock.patch.object(nx, "ci_plan", return_value={
                    "parts": [{"workflow": "backend-suite.yml"}], "max_inflight": 3}), \
                mock.patch.object(nx, "workflow_step", return_value=False):
            report = self.run_tick(60000, [refused, passed])
        self.assertEqual((report["result"], report["version"]), ("waiting-ci", "11au"))
        slot = self.slot()
        self.assertEqual(slot["last_result"], "not-attested")
        self.assertEqual(slot["pending_nexlink"]["tag"], "release-11au")
        self.assertTrue(any("passé par mesh-design" in body for body, _r, _k in self.sent))
        self.assertTrue(any("bureau may not pass" in body for body, _r, _k in self.sent))

    def test_pass_before_the_next_cutoff_only_closes(self):
        self.handed_over()
        report = self.run_tick(2000, [(9, dict(decision="passed", actor="human:smichea",
                                               authority="corrector", version="11at"))])
        self.assertEqual(report["result"], "not-attested")
        self.assertNotIn("pending_nexlink", self.slot())


if __name__ == "__main__":
    unittest.main()

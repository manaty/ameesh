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


class StagingJobsTests(unittest.TestCase):
    def test_deploy_jobs_of_the_frozen_workflow(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        repo = Path(temp.name)
        subprocess.check_call(["git", "init", "-q", str(repo)])
        workflow = repo / ".github" / "workflows" / "deploy-web.yml"
        workflow.parent.mkdir(parents=True)
        good = """env:
  SERVICE: nexlink-web-staging
jobs:
  runner-guard:
    steps: []
  deploy:
    environment: staging
    steps:
      - with:
          service_account: '${{ vars.GCP_STAGING_DEPLOYER_SA }}'
"""
        project = rc.ProjectRelease("nexlink", str(repo), model="nexlink")

        def commit(text):
            workflow.write_text(text)
            subprocess.check_call(["git", "-C", str(repo), "add", "."])
            subprocess.check_call(["git", "-C", str(repo), "-c", "user.name=t", "-c",
                                   "user.email=t@localhost", "commit", "-qm", "w"])
            return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                           text=True).strip()
        self.assertEqual(nx.staging_jobs(project, commit(good), "deploy-web.yml"), ["deploy"])
        named = commit(good.replace("  deploy:\n", "  deploy:\n    name: Deploy ${{ x }}\n"))
        with self.assertRaisesRegex(rc.ReleaseError, "expression"):
            nx.staging_jobs(project, named, "deploy-web.yml")
        prod = commit(good.replace("environment: staging", "environment: production"))
        with self.assertRaises(rc.ReleaseError):
            nx.staging_jobs(project, prod, "deploy-web.yml")


class ConfigurationTests(unittest.TestCase):
    def test_handoff_is_a_nexlink_key_without_credential(self):
        cfg = types.SimpleNamespace(humans="", radar={})
        with mock.patch.object(rc.radar, "settings_of",
                               return_value=types.SimpleNamespace(projects={})):
            project = rc.ProjectRelease.from_config(cfg, "nexlink", {
                "repo": ".", "target": "develop", "model": "nexlink",
                "staging_handoff": "bureau"})
            self.assertEqual((project.staging_handoff, project.attestation_timeout_s),
                             ("bureau", 4 * 3600.0))
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


class NoteTests(unittest.TestCase):
    def test_attestation_round_trip_and_malformed(self):
        migrations = nx.parse_migrations(["hub:540", "content:540", "hub:540"])
        self.assertEqual(migrations, [{"target": "content", "version": 540},
                                      {"target": "hub", "version": 540}])
        text = nx.attestation_text("nexlink", "11at", sha=SHA, runs=dict(RUNS),
                                   migrations=migrations)
        kind, data = nx.read_decision("nexlink", "11at", text)
        self.assertEqual((kind, data["sha"], data["runs"]), ("attested", SHA, RUNS))
        self.assertIsNone(nx.read_decision("nexlink", "11au", text))
        self.assertEqual(nx.read_decision("nexlink", "11at",
                                          nx.skip_text("nexlink", "11at", " pas  de staging ")),
                         ("skipped", "pas de staging"))
        for bad in ('{"sha": "abc"}', "pas du json",
                    json.dumps({"sha": SHA, "runs": {"deploy-api.yml": 1}})):
            self.assertEqual(nx.read_decision(
                "nexlink", "11at", "release-attest:nexlink:11at " + bad)[0], "malformed")
        for texts in (["deploy-api.yml=1"], ["deploy-api-prod.yml=1", "deploy-web.yml=2",
                                             "deploy-media-worker.yml=3"]):
            with self.assertRaises(rc.ReleaseError):
                nx.parse_runs(texts)
        with self.assertRaises(rc.ReleaseError):
            nx.parse_migrations(["prod:540"])

    def test_reserved_prefix_refused_by_work_note(self):
        from ameesh import work
        with mock.patch.object(work, "get", return_value={"id": 1, "state": "intake"}):
            for text in (nx.attestation_text("nexlink", "11at", sha=SHA, runs=dict(RUNS),
                                             migrations=[]),
                         nx.skip_text("nexlink", "11at", "x")):
                with self.assertRaisesRegex(work.WorkError, "reserved"):
                    work.note(object(), 1, text, actor="bureau")


class AttestCommandTests(unittest.TestCase):
    def args(self, **change):
        values = dict(project="nexlink", version="11at", sha=SHA,
                      run=["%s=%d" % item for item in RUNS.items()],
                      migration=["hub:540"], skip=False, reason="", actor=None)
        values.update(change)
        return types.SimpleNamespace(**values)

    def run_attest(self, args, *, binding=None, corrector=None):
        from ameesh import release, work
        binding = binding or types.SimpleNamespace(ok=True, name="bureau", reason="")
        with mock.patch("ameesh.db.open_db"), \
                mock.patch("ameesh.identity.resolve_binding", return_value=binding), \
                mock.patch.object(work, "corrector", side_effect=corrector or (
                    lambda *a, **k: (_ for _ in ()).throw(work.WorkError("not a pilot")))), \
                mock.patch.object(nx, "write_decision", return_value=77) as write, \
                mock.patch("builtins.print"):
            return release._attest(None, args), write

    def test_attestation_author_is_the_bound_identity(self):
        status, write = self.run_attest(self.args())
        self.assertEqual(status, 0)
        project, version, kind, text, actor = write.call_args.args[1:]
        self.assertEqual((project, version, kind, actor),
                         ("nexlink", "11at", "attested", "bureau"))
        self.assertEqual(nx.read_decision("nexlink", "11at", text)[0], "attested")
        for args in (self.args(actor="bureau"), self.args(sha="abc"),
                     self.args(run=["deploy-api.yml=1"])):
            with self.subTest(args=args):
                status, write = self.run_attest(args)
                self.assertEqual(status, 1)
                write.assert_not_called()
        status, write = self.run_attest(self.args(), binding=types.SimpleNamespace(
            ok=False, name="", reason="unbound"))
        self.assertEqual(status, 1)

    def test_skip_is_a_verified_pilot_gesture(self):
        skip = self.args(sha=None, run=[], migration=[], skip=True, reason="attente levée")
        status, write = self.run_attest(skip)
        self.assertEqual(status, 1)
        write.assert_not_called()
        status, write = self.run_attest(skip, corrector=lambda *a, **k: "mesh-design")
        self.assertEqual(status, 0)
        self.assertEqual(write.call_args.args[3:6:2], ("skipped", "mesh-design"))
        no_reason = self.args(sha=None, run=[], migration=[], skip=True)
        self.assertEqual(self.run_attest(no_reason,
                                         corrector=lambda *a, **k: "mesh-design")[0], 1)


def api_run(workflow, run_id, **change):
    row = dict(path=".github/workflows/" + workflow, head_sha=SHA, head_branch="release/11at",
               event="workflow_dispatch", status="completed", conclusion="success",
               html_url="https://example/run/%d" % run_id)
    row.update(change)
    return row


def note(event_id, actor, text, *, proof=True):
    kind = "skipped" if text.startswith(nx.SKIP_PREFIX) else "attested"
    evidence = ({"schema": nx.DECISION_SCHEMA, "kind": kind, "project": "nexlink",
                 "version": "11at", "actor": actor} if proof else None)
    return {"id": event_id, "actor": actor, "note": text, "evidence": evidence}


def fake_gh(runs, jobs=None):
    """GitHub API: runs/<id> and runs/<id>/jobs; unknown id → 404."""
    jobs = jobs or {}

    def call(project, args):
        path = args[1].split("?")[0].split("/")
        run_id = int(path[path.index("runs") + 1])
        if run_id not in runs:
            raise rc.TransientReleaseError("Nexlink GitHub operation failed: HTTP 404: Not Found")
        if path[-1] == "jobs":
            return json.dumps({"jobs": jobs.get(run_id, [{"name": "deploy",
                                                          "conclusion": "success"}])})
        return json.dumps(runs[run_id])
    return call


class HandoffPipelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=temp.name)
        self.db = object()
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         github="acme/nexlink", model="nexlink",
                                         staging_handoff="bureau", attestation_timeout_s=3600)
        self.pending = dict(sha=SHA, main="b" * 40, base="c" * 40, tag="release-11at",
                            version="11at", cutoff_at=100, label="11:00",
                            integration="integration/release-11at", empty=False,
                            ci_max_inflight=3, ci_parts=[{"workflow": "backend-suite.yml"}])
        rc.save_state(self.cfg, {"schema": rc.SCHEMA,
                                 "projects": {"nexlink": {"pending_nexlink": self.pending}}})
        self.slot_at = 100
        self.notes = []
        from ameesh import work
        patches = [mock.patch.object(rc, "compiled_rules",
                                     return_value=(object(), types.SimpleNamespace(commit="d" * 40))),
                   mock.patch.object(rc, "cutoff_slot",
                                     side_effect=lambda rules, now: (self.slot_at, "23:00")),
                   mock.patch.object(rc, "fetch"), mock.patch.object(rc, "write_report"),
                   mock.patch.object(nx, "ensure_ref"),
                   mock.patch.object(nx, "open_handoff_lot", return_value=77),
                   mock.patch.object(nx, "staging_jobs", return_value=["deploy"]),
                   mock.patch.object(work, "events_noted",
                                     side_effect=lambda db, lot, prefix: list(self.notes)),
                   mock.patch.object(nx, "migration_requirements", return_value={
                       "hub": [{"version": 540, "name": "540_x", "checksum": "c"}]})]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(work, "close")
        self.close = patcher.start()
        self.addCleanup(patcher.stop)
        self.sent = []
        patcher = mock.patch.object(rc, "send_report", side_effect=lambda cfg, db, project, body,
                                    report, **kw: self.sent.append((body, report, kw)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tick(self, now):
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

    def attest(self, event_id, actor="bureau", **change):
        data = dict(sha=SHA, runs=dict(RUNS), migrations=[{"target": "hub", "version": 540}])
        data.update(change)
        self.notes.append(note(event_id, actor, nx.attestation_text("nexlink", "11at", **data)))

    def gh_runs(self, change=None, jobs=None, extra=None):
        change = change or {}
        runs = {run_id: api_run(workflow, run_id, **change.get(run_id, {}))
                for workflow, run_id in RUNS.items()}
        runs.update(extra or {})
        return mock.patch.object(nx, "gh", side_effect=fake_gh(runs, jobs))

    def test_cutoff_tags_reports_and_opens_the_attestation_lot(self):
        with mock.patch.dict(os.environ, {"SUPABASE_ACCESS_TOKEN": ""}):
            report = self.handed_over()
        self.assertEqual(report["result"], "waiting-attestation")
        handoff = self.slot()["pending_nexlink"]["handoff"]
        self.assertEqual((handoff["to"], handoff["deadline"], handoff["lot"]),
                         ("bureau", 4600, 77))
        body, _report, kw = self.sent[-1]
        self.assertEqual(kw["also"], ("bureau",))
        self.assertIn("release-11at", body)
        self.assertIn("--migration hub:540", body)
        self.assertIn("Lot d'attestation : #77", body)

    def test_valid_attestation_closes_cutoff_and_lot(self):
        self.handed_over()
        self.attest(5)
        with self.gh_runs():
            report = self.run_tick(2000)
        self.assertEqual(report["result"], "released")
        self.assertTrue(report["staging_ok"])
        self.assertEqual(report["staging_attestation"]["event"], 5)
        self.assertNotIn("pending_nexlink", self.slot())
        self.assertEqual(self.slot()["last_cutoff"], 100)
        self.assertIn("staging attesté par bureau", self.sent[-1][0])
        self.assertEqual(self.close.call_args.args[1], 77)

    def test_wrong_attestations_refused_once_each(self):
        self.handed_over()
        self.attest(6, actor="mty-amh-impl-3")
        self.attest(7, migrations=[])
        self.attest(8, sha="e" * 40)
        self.attest(9)
        self.notes.append(note(10, "bureau", "release-attest:nexlink:11at {"))
        before = len(self.sent)
        with self.gh_runs({11: dict(conclusion="failure")}):
            self.assertEqual(self.run_tick(2100)["result"], "waiting-attestation")
            self.run_tick(2200)
        rejected = self.slot()["pending_nexlink"]["handoff"]["rejected"]
        self.assertEqual(sorted(rejected), ["10", "6", "7", "8", "9"])
        self.assertIn("not the staging agent", rejected["6"])
        self.assertIn("540_x on hub not attested", rejected["7"])
        self.assertIn("not the frozen candidate", rejected["8"])
        self.assertIn("concluded failure", rejected["9"])
        self.assertEqual(len(self.sent) - before, 5)

    def test_running_attestation_never_holds_a_later_decision(self):
        """cx1 B1: [running attestation, newer valid one] and [running, skip]."""
        self.handed_over()
        self.attest(5, runs=dict(RUNS, **{"deploy-api.yml": 21}))
        gh = self.gh_runs(extra={21: api_run("deploy-api.yml", 21, status="in_progress",
                                             conclusion=None)})
        with gh:
            self.assertEqual(self.run_tick(2000)["result"], "waiting-attestation")
        self.assertIn("still in_progress", self.slot()["pending_nexlink"]["handoff"]["waiting"])
        self.attest(6)
        with gh:
            report = self.run_tick(2100)
        self.assertEqual((report["result"], report["staging_attestation"]["event"]),
                         ("released", 6))

    def test_running_attestation_then_skip(self):
        self.handed_over()
        self.attest(5)
        self.notes.append(note(6, "mesh-design", nx.skip_text("nexlink", "11at", "on passe")))
        with self.gh_runs({11: dict(status="queued", conclusion=None)}):
            report = self.run_tick(2000)
        self.assertEqual(report["result"], "staging-unattested")
        self.assertIn("passé par mesh-design (on passe)", self.sent[-1][0])

    def test_forged_notes_without_proof_are_refused(self):
        """B1 cx3: the reserved text through `work move --note` carries no proof."""
        self.handed_over()
        self.notes.append(note(5, "mty-amh-impl-3", nx.skip_text("nexlink", "11at", "x"),
                               proof=False))
        self.notes.append(note(6, "bureau", nx.attestation_text(
            "nexlink", "11at", sha=SHA, runs=dict(RUNS),
            migrations=[{"target": "hub", "version": 540}]), proof=False))
        with self.gh_runs():
            self.assertEqual(self.run_tick(2000)["result"], "waiting-attestation")
        rejected = self.slot()["pending_nexlink"]["handoff"]["rejected"]
        self.assertEqual(sorted(rejected), ["5", "6"])
        self.assertIn("without the proof", rejected["5"])

    def test_staging_workflow_and_deploy_job_are_checked(self):
        """B1 impl-03 (frozen workflow) and B2 cx3 (deploy job skipped)."""
        self.handed_over()
        self.attest(5)
        with self.gh_runs(jobs={11: [{"name": "deploy", "conclusion": "skipped"},
                                     {"name": "test", "conclusion": "success"}]}):
            self.run_tick(2000)
        self.attest(6)
        with self.gh_runs(), mock.patch.object(
                nx, "staging_jobs", side_effect=rc.ReleaseError("environment must be staging")):
            self.assertEqual(self.run_tick(2100)["result"], "waiting-attestation")
        rejected = self.slot()["pending_nexlink"]["handoff"]["rejected"]
        self.assertIn("deploy job deploy did not succeed (skipped)", rejected["5"])
        self.assertIn("is not a staging workflow", rejected["6"])

    def test_unknown_run_then_skip(self):
        """B2 impl-03: a run id GitHub does not know is refused, a skip still lands."""
        self.handed_over()
        self.attest(5, runs=dict(RUNS, **{"deploy-web.yml": 999}))
        with self.gh_runs():
            self.assertEqual(self.run_tick(2000)["result"], "waiting-attestation")
        self.assertIn("999 (deploy-web.yml) not found",
                      self.slot()["pending_nexlink"]["handoff"]["rejected"]["5"])
        self.notes.append(note(6, "mesh-design", nx.skip_text("nexlink", "11at", "run perdu")))
        with self.gh_runs():
            self.assertEqual(self.run_tick(2100)["result"], "staging-unattested")

    def test_read_failure_keeps_the_bounded_wait(self):
        """B2 impl-03: a journal (DbError, its real class) or GitHub failure
        never skips the announcements."""
        from ameesh import work
        from ameesh.db import DbError, Unavailable
        for error in (DbError("journal down"), Unavailable("journal down"),
                      rc.TransientReleaseError("journal down")):
            with self.subTest(error=type(error).__name__):
                rc.save_state(self.cfg, {"schema": rc.SCHEMA, "projects": {
                    "nexlink": {"pending_nexlink": json.loads(json.dumps(self.pending))}}})
                self.handed_over()
                before = len(self.sent)
                with mock.patch.object(work, "events_noted", side_effect=error):
                    for now in (4700, 4800):
                        self.assertEqual(self.run_tick(now)["result"], "waiting-attestation")
                bodies = [body for body, _r, _k in self.sent[before:]]
                self.assertEqual(len(bodies), 2)
                self.assertIn("lecture de l'attestation de staging en échec : journal down",
                              bodies[0])
                self.assertIn("attestation de staging absente", bodies[1])

    def test_lot_that_cannot_open_is_reported_and_retried(self):
        from ameesh import work
        from ameesh.db import DbError
        for error in (work.WorkError("bureau is not external"), DbError("base down")):
            with self.subTest(error=type(error).__name__):
                rc.save_state(self.cfg, {"schema": rc.SCHEMA, "projects": {
                    "nexlink": {"pending_nexlink": json.loads(json.dumps(self.pending))}}})
                with mock.patch.object(nx, "open_handoff_lot", side_effect=error):
                    self.assertEqual(self.handed_over()["result"], "waiting-attestation")
                self.assertIn("Lot d'attestation NON ouvert : %s" % error, self.sent[-1][0])
                self.assertEqual(self.slot()["pending_nexlink"]["handoff"]["lot_error"],
                                 str(error))
                self.notes = []
                self.attest(5)
                with self.gh_runs():
                    self.assertEqual(self.run_tick(2000)["result"], "released")

    def test_preflight_refusal_leaves_no_tag(self):
        """B1/B2 dev3: an invalid staging workflow or a git failure (error,
        timeout, OSError) is refused before publish: no tag, and the cutoff
        closes as refused-preflight instead of holding the next one."""
        import subprocess as sp
        staging, migrations = "staging_jobs", "migration_requirements"
        for target, error, expected in (
                (staging, rc.ReleaseError("Deployment environment must be literal staging"),
                 "literal staging"),
                (staging, sp.CalledProcessError(128, ["git", "show", "x:.github/workflows/deploy-api.yml"]),
                 "preflight: git show"),
                (migrations, sp.TimeoutExpired(["git", "-C", "/r", "show", "x:y.sql"], 30),
                 "timed out"),
                (migrations, OSError("no git"), "no git")):
            rc.save_state(self.cfg, {"schema": rc.SCHEMA,
                                     "projects": {"nexlink": {"pending_nexlink": dict(self.pending)}}})
            before = len(self.sent)
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(nx, target, side_effect=error), \
                    mock.patch.object(nx, "workflow_step", return_value=True), \
                    mock.patch.object(nx, "publish") as publish:
                with self.assertRaisesRegex(rc.ReleaseError, expected):
                    self.run_tick(1000)
                publish.assert_not_called()
                slot = self.slot()
                self.assertNotIn("pending_nexlink", slot)
                self.assertEqual((slot["last_result"], slot["last_cutoff"]),
                                 ("refused-preflight", 100))
                self.assertEqual(len(self.sent), before + 1)
                self.assertIn("refusée avant le tag", self.sent[-1][0])
                self.assertIn("--force", self.sent[-1][0])

    def test_next_cutoff_runs_after_a_refused_preflight(self):
        with mock.patch.object(nx, "migration_requirements",
                               side_effect=OSError("no git")), \
                mock.patch.object(nx, "workflow_step", return_value=True), \
                mock.patch.object(nx, "publish"):
            with self.assertRaises(rc.ReleaseError):
                self.run_tick(1000)
        self.assertEqual(self.run_tick(2000)["result"], "already-recorded")
        self.slot_at = 50000
        with mock.patch.object(nx, "snapshot", return_value=("h", "c2", "m2", {}, None)), \
                mock.patch.object(nx, "choose_version", return_value="11at"), \
                mock.patch.object(nx, "merge_candidate", return_value="f" * 40), \
                mock.patch.object(nx, "previous_release", return_value="m2"), \
                mock.patch.object(rc, "same_tree", return_value=False), \
                mock.patch.object(rc, "due", return_value={}), \
                mock.patch.object(nx, "ci_plan", return_value={
                    "parts": [{"workflow": "backend-suite.yml"}], "max_inflight": 3}), \
                mock.patch.object(nx, "workflow_step", return_value=False):
            report = self.run_tick(50010)
        self.assertEqual((report["result"], report["version"]), ("waiting-ci", "11at"))
        self.assertEqual(self.slot()["pending_nexlink"]["cutoff_at"], 50000)

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

    def test_next_cutoff_held_then_pilot_skips(self):
        self.handed_over()
        before = len(self.sent)
        self.slot_at = 50000
        self.assertEqual(self.run_tick(50010)["result"], "held-attestation")
        self.assertEqual(self.run_tick(50020)["result"], "waiting-attestation")
        # Past the deadline too: each announcement once, the held cutoff last.
        self.assertEqual(len(self.sent), before + 2)
        self.assertIn("attestation de staging absente", self.sent[-2][0])
        self.assertIn("ameesh release attest --project nexlink --version 11at --skip",
                      self.sent[-1][0])
        self.notes.append(note(9, "mesh-design", nx.skip_text("nexlink", "11at", "attente")))
        with mock.patch.object(nx, "snapshot", return_value=("h", "c2", "m2", {}, None)), \
                mock.patch.object(nx, "choose_version", return_value="11au"), \
                mock.patch.object(nx, "merge_candidate", return_value="f" * 40), \
                mock.patch.object(nx, "previous_release", return_value="m2"), \
                mock.patch.object(rc, "same_tree", return_value=False), \
                mock.patch.object(rc, "due", return_value={"missed": True}), \
                mock.patch.object(nx, "ci_plan", return_value={
                    "parts": [{"workflow": "backend-suite.yml"}], "max_inflight": 3}), \
                mock.patch.object(nx, "workflow_step", return_value=False):
            report = self.run_tick(60000)
        self.assertEqual((report["result"], report["version"]), ("waiting-ci", "11au"))
        slot = self.slot()
        self.assertEqual(slot["last_result"], "staging-unattested")
        self.assertEqual(slot["pending_nexlink"]["tag"], "release-11au")
        self.assertEqual(self.close.call_args.args[1], 77)

    def test_skip_before_the_next_cutoff_only_closes(self):
        self.handed_over()
        self.notes.append(note(9, "human:smichea", nx.skip_text("nexlink", "11at", "x")))
        report = self.run_tick(2000)
        self.assertEqual(report["result"], "staging-unattested")
        self.assertNotIn("pending_nexlink", self.slot())


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""#7372: the bureau's standing root right and the declared catch-up.

The owner signed decision #219407 (option a) on 2026-11-16: a standing root
right of the bureau to catch up `manaty-mesh-1` after each cutoff, until
2026-12-12, each gesture declared in the cutoff report and traced. The four
`vm.sh` steps stay manual; `ameesh release catchup` records the outcome as a
`staging.catchup` event, and the guard checks the declared commit itself.
"""
import datetime
import tempfile
import types
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from ameesh import release_cutoff, staging_deferred

SHA = "a" * 40
OLD = "b" * 40
NEW = "c" * 40
TAG = {"version": "1.57.0", "tag": "v1.57.0", "commit": SHA, "at": 2.0}
OLD_TAG = {"version": "1.56.0", "tag": "v1.56.0", "commit": OLD, "at": 1.0}
AGENTS = ("ameesh-agents-1", "ameesh-agents-2", "ameesh-agents-3")
HOST = "manaty-mesh-1"


def attestation(sha, *, reportes=None, hotes=None, date="2026-11-15T22:31:37Z"):
    return {"schema": "ameesh-staging-attestation/1", "sha": sha, "date": date,
            "hotes": (hotes if hotes is not None
                      else {host: {"actif": sha} for host in AGENTS}),
            "reportes": reportes or {}}


def project(name="ameesh", repo="/nowhere"):
    return release_cutoff.ProjectRelease(
        name=name, repo=repo, target="main", remote="origin",
        report="human", github="manaty/%s" % name)


class RightTest(unittest.TestCase):
    def test_the_standing_right_is_active_until_its_expiry_day(self):
        right = staging_deferred.right_for(project(), HOST, today="2026-11-16")
        self.assertEqual(right["decision"], 219407)
        self.assertFalse(right["expired"])
        self.assertFalse(staging_deferred.right_for(
            project(), HOST, today="2026-12-12")["expired"])
        self.assertTrue(staging_deferred.right_for(
            project(), HOST, today="2026-12-13")["expired"])

    def test_the_right_covers_only_its_project_and_host(self):
        self.assertIsNone(staging_deferred.right_for(project("nexlink"), HOST))
        self.assertIsNone(staging_deferred.right_for(project(), "pc-smichea"))
        self.assertIsNone(staging_deferred.right_for("ameesh", "ameesh-agents-1"))

    def test_the_right_accepts_a_project_name(self):
        right = staging_deferred.right_for("ameesh", HOST, today="2026-11-20")
        self.assertEqual(right["decision"], 219407)


class DeclareTest(unittest.TestCase):
    base = {"sha": SHA, "tag": "v1.57.0", "date": "2026-11-15T22:31:37Z"}

    def test_a_successful_declaration_carries_the_right_and_is_written(self):
        with mock.patch("ameesh.events.record", return_value=42) as record:
            row = staging_deferred.declare(
                object(), project(), HOST, SHA, self.base, "ok", "bureau",
                note="  quatre étapes vertes  ")
        self.assertEqual(row["event"], 42)
        self.assertEqual(row["decision"], 219407)
        self.assertEqual(row["result"], "ok")
        self.assertEqual(row["note"], "quatre étapes vertes")
        record.assert_called_once()
        args, kwargs = record.call_args
        self.assertEqual(args[1], staging_deferred.CATCHUP_EVENT)
        self.assertEqual(args[2], "staging-catchup:ameesh:manaty-mesh-1")
        self.assertEqual(kwargs["project"], "ameesh")
        self.assertEqual(kwargs["sha"], SHA)

    def test_an_expired_right_refuses_a_successful_declaration(self):
        with mock.patch.object(staging_deferred, "right_for",
                               return_value={"decision": 219407, "expired": True,
                                             "expires": "2026-12-12"}):
            with self.assertRaises(ValueError):
                staging_deferred.declare(object(), project(), HOST, SHA,
                                         self.base, "ok", "bureau")

    def test_a_failed_gesture_is_recorded_without_the_right(self):
        with mock.patch.object(staging_deferred, "right_for", return_value=None), \
             mock.patch("ameesh.events.record", return_value=43):
            row = staging_deferred.declare(object(), project(), HOST, SHA,
                                           self.base, "failed", "bureau",
                                           note="controler KO")
        self.assertEqual(row["result"], "failed")
        self.assertIsNone(row["decision"])

    def test_a_declaration_requires_a_full_commit(self):
        for sha in ("", "abc", "v1.57.0", "g" * 40, "a" * 39):
            with self.assertRaises(ValueError, msg=sha):
                staging_deferred.declare(object(), project(), HOST, sha,
                                         self.base, "ok", "bureau")


class DeclarationsTest(unittest.TestCase):
    def test_declarations_keep_the_newest_per_host_and_the_right_project(self):
        rows = [
            {"project": "ameesh", "data": {"host": HOST, "result": "failed"}},
            {"project": "nexlink", "data": {"host": HOST, "result": "ok"}},
            {"project": "ameesh", "data": {"host": HOST, "result": "ok"}},
            {"project": "ameesh", "data": {"host": "ameesh-agents-1",
                                           "result": "ok"}},
        ]
        events = mock.MagicMock()
        events.recent_type.return_value = rows
        store = mock.MagicMock(events=events)
        with mock.patch("ameesh.storage.of", return_value=store):
            out = staging_deferred.declarations(object(), project())
        self.assertEqual(out[HOST]["result"], "failed")
        self.assertEqual(out["ameesh-agents-1"]["result"], "ok")
        self.assertNotEqual(out[HOST]["result"], "ok")

    def test_an_unreadable_log_declares_nothing(self):
        with mock.patch("ameesh.storage.of", side_effect=RuntimeError("down")):
            self.assertEqual(staging_deferred.declarations(object(), project()), {})


class BaselineTest(unittest.TestCase):
    def test_baseline_is_the_tagged_attestation_that_reports_the_host(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        with mock.patch.object(release_cutoff, "stored_attestations", return_value=rows), \
             mock.patch.object(release_cutoff, "_tags_by_commit", return_value={SHA: TAG}):
            base = staging_deferred.baseline(object(), project(), HOST)
        self.assertEqual(base["sha"], SHA)
        self.assertEqual(base["tag"], "v1.57.0")
        self.assertEqual(base["raison"], "deferred")

    def test_a_proved_host_has_nothing_to_declare(self):
        rows = [attestation(SHA, hotes={**{h: {"actif": SHA} for h in AGENTS},
                                        HOST: {"actif": SHA}})]
        with mock.patch.object(release_cutoff, "stored_attestations", return_value=rows), \
             mock.patch.object(release_cutoff, "_tags_by_commit", return_value={SHA: TAG}):
            self.assertIsNone(staging_deferred.baseline(object(), project(), HOST))


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=self.tmp.name)

    def tick(self, rows, by_commit, *, declared=None, pairs=(), today=None):
        pairs = set(pairs)

        def ancestry(repo, ancestor, descendant):
            if ancestor == descendant:
                return True
            if (ancestor, descendant) in pairs:
                return True
            if (descendant, ancestor) in pairs:
                return False
            return None

        with mock.patch.object(release_cutoff, "stored_attestations",
                               return_value=rows), \
             mock.patch.object(release_cutoff, "_tags_by_commit",
                               return_value=by_commit), \
             mock.patch.object(staging_deferred, "declarations",
                               return_value=declared or {}), \
             mock.patch.object(staging_deferred, "_is_ancestor",
                               side_effect=ancestry), \
             mock.patch.object(staging_deferred, "_registry_rows", return_value=[]), \
             mock.patch("ameesh.alert_briefs.project_pilots",
                        return_value=["pilot"]), \
             mock.patch("ameesh.mail.send") as send:
            report = staging_deferred.tick(self.cfg, object(), project(), today=today)
        return report, send

    def test_a_declared_catchup_at_the_attested_tag_lifts_the_alert(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        declared = {HOST: {"result": "ok", "sha": SHA, "date": "2026-11-16T00:00:00Z",
                           "actor": "bureau"}}
        report, send = self.tick(rows, {SHA: TAG}, declared=declared)
        self.assertEqual(report["result"], "none")
        self.assertEqual(send.call_count, 0)

    def test_a_declared_catchup_at_an_ancestor_leaves_the_alert(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        declared = {HOST: {"result": "ok", "sha": OLD, "date": "2026-11-16T00:00:00Z"}}
        report, send = self.tick(rows, {SHA: TAG}, declared=declared,
                                 pairs={(OLD, SHA)})
        self.assertEqual(report["result"], "alerted")
        self.assertEqual(send.call_count, 2)

    def test_a_failed_declaration_does_not_lift_the_alert(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        declared = {HOST: {"result": "failed", "sha": SHA,
                           "date": "2026-11-16T00:00:00Z"}}
        report, send = self.tick(rows, {SHA: TAG}, declared=declared)
        self.assertEqual(report["result"], "alerted")
        self.assertEqual(send.call_count, 2)

    def test_the_alert_names_the_decision_and_its_expiry(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        report, send = self.tick(rows, {SHA: TAG}, today=datetime.date(2026, 11, 20))
        body = send.call_args_list[0].args[3]
        self.assertIn("décision #219407", body)
        self.assertIn("2026-12-12", body)
        self.assertIn("ameesh release catchup", body)

    def test_the_alert_reports_the_last_failed_gesture(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        report, send = self.tick(
            rows, {SHA: TAG},
            declared={HOST: {"result": "failed", "sha": SHA,
                             "date": "2026-11-16T00:00:00Z", "actor": "bureau"}})
        body = send.call_args_list[0].args[3]
        self.assertIn("dernier geste déclaré : commit %s (failed" % SHA[:12], body)
        self.assertIn("par bureau", body)

    def test_after_the_expiry_the_alert_asks_for_a_new_decision(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        report, send = self.tick(rows, {SHA: TAG}, today=datetime.date(2026, 12, 13))
        body = send.call_args_list[0].args[3]
        self.assertIn("droit root #219407 expiré le 2026-12-12", body)
        self.assertIn("demander une décision owner", body)

    def test_the_tick_report_carries_the_right_and_the_declaration(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        declared = {HOST: {"result": "failed", "sha": SHA,
                           "date": "2026-11-16T00:00:00Z", "actor": "bureau"}}
        report, _send = self.tick(rows, {SHA: TAG}, declared=declared)
        self.assertFalse(report["rights"][HOST]["expired"])
        self.assertEqual(report["declarations"][HOST]["result"], "failed")


class AnnotateAndReportTest(unittest.TestCase):
    def test_annotate_adds_the_declaration_and_the_right_to_the_payload(self):
        staging = {"attestation": attestation(
            SHA, reportes={HOST: {"raison": "deferred"},
                           "pc-smichea": {"raison": "poste"}})}
        declared = {HOST: {"result": "ok", "sha": SHA,
                           "date": "2026-11-16T00:00:00Z", "actor": "bureau"}}
        with mock.patch.object(staging_deferred, "declarations", return_value=declared):
            staging_deferred.annotate(object(), project(), staging)
        self.assertEqual(staging["catchups"][HOST]["result"], "ok")
        self.assertEqual(staging["rights"][HOST]["decision"], 219407)
        # the workstation is never a lag, so it has no right or declaration
        self.assertNotIn("pc-smichea", staging["catchups"])
        self.assertNotIn("pc-smichea", staging["rights"])

    def test_the_report_names_the_declared_gesture_and_the_right(self):
        rules = types.SimpleNamespace(zone=lambda: ZoneInfo("Europe/Paris"))
        staging = {
            "exit": 0, "duration_s": 12.0, "log": "/tmp/journal", "summary": [],
            "attestation": attestation(SHA, reportes={HOST: {"raison": "deferred"}}),
            "catchups": {HOST: {"result": "ok", "sha": SHA,
                                "date": "2026-11-16T00:00:00Z", "actor": "bureau"}},
            "rights": {HOST: {"decision": 219407, "expires": "2026-12-12",
                              "expired": False}},
        }
        body = release_cutoff._report_body(
            types.SimpleNamespace(name="ameesh", target="main"), None,
            result="released", version="1.57.0", tag="v1.57.0",
            release_sha=SHA, staging=staging, deviations=[], previous=None,
            cutoff_at=0.0, cutoff_label="23:00", rules=rules)
        self.assertIn("dernier geste déclaré : commit %s (ok" % SHA[:12], body)
        self.assertIn("par bureau", body)

    def test_the_report_says_when_no_gesture_was_declared(self):
        rules = types.SimpleNamespace(zone=lambda: ZoneInfo("Europe/Paris"))
        staging = {
            "exit": 0, "duration_s": 12.0, "log": "/tmp/journal", "summary": [],
            "attestation": attestation(SHA, reportes={HOST: {"raison": "deferred"}}),
            "catchups": {},
            "rights": {HOST: {"decision": 219407, "expires": "2026-12-12",
                              "expired": False}},
        }
        body = release_cutoff._report_body(
            types.SimpleNamespace(name="ameesh", target="main"), None,
            result="released", version="1.57.0", tag="v1.57.0",
            release_sha=SHA, staging=staging, deviations=[], previous=None,
            cutoff_at=0.0, cutoff_label="23:00", rules=rules)
        self.assertIn("aucun geste déclaré (droit #219407 actif jusqu'au 2026-12-12)",
                      body)


class CliTest(unittest.TestCase):
    def run_cmd(self, *, expired=True, declared=True):
        from ameesh import release
        cfg = types.SimpleNamespace()
        right = {"decision": 219407, "expires": "2026-12-12", "expired": expired}
        base = {"sha": SHA, "tag": "v1.57.0"}
        binding = types.SimpleNamespace(ok=True, name="bureau")
        row = {"schema": "ameesh-staging-catchup/1", "project": "ameesh", "host": HOST,
               "sha": SHA, "tag": "v1.57.0", "result": "ok", "actor": "bureau",
               "decision": 219407, "event": 42}
        args = types.SimpleNamespace(project="ameesh", host=HOST, sha=SHA,
                                     result="ok", actor="", note="")
        db = mock.MagicMock()
        with mock.patch.object(release_cutoff, "projects",
                               return_value={"ameesh": project()}), \
             mock.patch("ameesh.db.open_db", return_value=db), \
             mock.patch.object(staging_deferred, "baseline",
                               return_value=(base if declared else None)), \
             mock.patch.object(staging_deferred, "right_for", return_value=right), \
             mock.patch("ameesh.identity.resolve_binding", return_value=binding), \
             mock.patch.object(staging_deferred, "declare", return_value=row) as declare:
            code = release._catchup(cfg, args)
        return code, declare, db

    def test_an_active_right_declares_the_gesture(self):
        code, declare, db = self.run_cmd(expired=False)
        self.assertEqual(code, 0)
        declare.assert_called_once()
        db.close.assert_called_once()

    def test_an_expired_right_refuses_the_successful_declaration(self):
        code, declare, db = self.run_cmd(expired=True)
        self.assertEqual(code, 1)
        declare.assert_not_called()
        db.close.assert_called_once()

    def test_a_host_that_is_not_reported_has_nothing_to_declare(self):
        code, declare, _db = self.run_cmd(expired=False, declared=False)
        self.assertEqual(code, 0)
        declare.assert_not_called()


if __name__ == "__main__":
    unittest.main()

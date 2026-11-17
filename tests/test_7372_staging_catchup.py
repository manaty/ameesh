# SPDX-License-Identifier: AGPL-3.0-only
"""#7372: the bureau's standing root right and the declared catch-up.

The owner signed decision #219407 (option a) on 2026-11-16: a standing root
right of the bureau to catch up `manaty-mesh-1` after each cutoff, until
2026-12-12, each gesture declared in the cutoff report and traced. The four
`vm.sh` steps stay manual; `ameesh release catchup` records the outcome as a
`staging.catchup` event. The declaration is a trace, never a proof: only an
attestation that read the host lifts the lag alert. The right is configured
per project (`staging_rights`) and its decision is re-read on each pass.
"""
import datetime
import os
import tempfile
import types
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from ameesh import release, release_cutoff, staging_deferred
from tests import canon_staging

SHA = "a" * 40
OLD = "b" * 40
TAG = {"version": "1.57.0", "tag": "v1.57.0", "commit": SHA, "at": 2.0}
OLD_TAG = {"version": "1.56.0", "tag": "v1.56.0", "commit": OLD, "at": 1.0}
AGENTS = ("ameesh-agents-1", "ameesh-agents-2", "ameesh-agents-3")
HOST = "manaty-mesh-1"
RIGHTS = ({"gesture": "root", "beneficiary": "bureau", "host": HOST,
           "lot": 7372, "decision": 219407, "option": "a",
           "expires": "2026-12-12"},)

LABEL = ("Droit root permanent du bureau sur manaty-mesh-1 pour ce seul geste, "
         "jusqu'au 12/12 : à chaque coupure, les quatre étapes vm.sh puis "
         "retour au commit attesté, chaque geste déclaré et tracé ; "
         "révocable à tout moment.")
REFUSAL = ("Refuser le droit root sur manaty-mesh-1 jusqu'au 2026-12-12 : le "
           "report reste, aucune autorisation n'est donnée.")
OPTIONS = [
    {"key": "a", "label": LABEL},
    {"key": "b", "label": "Droit root par coupure, comme #215688 pour #7152 : "
                          "une décision owner à chaque release, avant le geste."},
    {"key": "c", "label": "Aucun droit : le report reste, l'alerte de retard de "
                          "version continue à chaque release."},
]
#: 2026-11-14: the decision's creation stamp, part of the signed answer digest.
CREATED = 1794656469.9


def attestation(sha, *, reportes=None, hotes=None, date="2026-11-15T22:31:37Z"):
    return {"schema": "ameesh-staging-attestation/1", "sha": sha, "date": date,
            "hotes": (hotes if hotes is not None
                      else {host: {"actif": sha} for host in AGENTS}),
            "reportes": reportes or {}}


def project(name="ameesh", repo="/nowhere", rights=RIGHTS):
    return release_cutoff.ProjectRelease(
        name=name, repo=repo, target="main", remote="origin",
        report="human", github="manaty/%s" % name, staging_rights=rights,
        staging_topology=canon_staging.topology())


def active(**extra):
    return {"project": "ameesh", "gesture": "root", "beneficiary": "bureau",
            "host": HOST, "lot": 7372, "decision": 219407,
            "expires": "2026-12-12", "expired": False, "active": True,
            "why": "", **extra}


def expired():
    return {"project": "ameesh", "gesture": "root", "beneficiary": "bureau",
            "host": HOST, "lot": 7372, "decision": 219407,
            "expires": "2026-12-12", "expired": True, "active": False,
            "why": "expired"}


class RightTest(unittest.TestCase):
    def test_the_configured_right_is_active_until_its_expiry_day(self):
        right = staging_deferred.right_for(project(), HOST, today="2026-11-16")
        self.assertEqual(right["decision"], 219407)
        self.assertFalse(right["expired"])
        self.assertFalse(staging_deferred.right_for(
            project(), HOST, today="2026-12-12")["expired"])
        self.assertTrue(staging_deferred.right_for(
            project(), HOST, today="2026-12-13")["expired"])

    def test_no_client_host_or_right_is_compiled(self):
        self.assertEqual(staging_deferred.RIGHTS if hasattr(
            staging_deferred, "RIGHTS") else (), ())
        self.assertIsNone(staging_deferred.right_for(project(), "pc-smichea"))
        self.assertIsNone(staging_deferred.right_for(project(rights=()), HOST))

    def view(self, **extra):
        view = {"state": "answered", "withdrawn": None, "superseded_by": None,
                "project": "ameesh", "lot": 7372, "created_ts": CREATED,
                "options": OPTIONS,
                "answer": {"option": "a", "proof": {"digest": "d" * 64}}}
        view.update(extra)
        return view

    def options(self, label=None, **extra):
        rows = [{"key": "a", "label": label if label is not None else LABEL}]
        rows[0].update(extra)
        return rows

    def active(self, view, *, today="2026-11-20"):
        with mock.patch("ameesh.decisions.get", return_value=view), \
             mock.patch("ameesh.decisions.verify_proof", return_value=(True, "")):
            return staging_deferred.active_right(object(), project(), HOST, today=today)

    def test_active_right_re_reads_the_signed_decision(self):
        view = self.view()
        with mock.patch("ameesh.decisions.get", return_value=view), \
             mock.patch("ameesh.decisions.verify_proof", return_value=(True, "")):
            right = staging_deferred.active_right(object(), project(), HOST,
                                                  today="2026-11-20")
        self.assertTrue(right["active"])

    def test_a_refused_signature_grants_nothing(self):
        view = self.view()
        with mock.patch("ameesh.decisions.get", return_value=view), \
             mock.patch("ameesh.decisions.verify_proof",
                        return_value=(False, "invalid Ed25519 signature")):
            right = staging_deferred.active_right(object(), project(), HOST,
                                                  today="2026-11-20")
        self.assertFalse(right["active"])
        self.assertIn("proof refused", right["why"])

    def test_a_decision_without_the_approved_option_grants_nothing(self):
        for view, why in (
                (self.view(answer={"option": "c",
                                   "proof": {"digest": "d" * 64}}), "option"),
                (self.view(project="nexlink"), "project"),
                (self.view(lot=9999), "lot")):
            right = self.active(view)
            self.assertFalse(right["active"], why)
            self.assertIn(why, right["why"])

    def test_the_unsigned_answer_fields_are_never_read(self):
        view = self.view(answer={"option": "a", "option_label": REFUSAL,
                                 "ts": 1893456000.0,
                                 "proof": {"digest": "d" * 64}})
        right = self.active(view)
        self.assertTrue(right["active"])
        self.assertEqual(right["expires"], "2026-12-12")

    def test_a_refusal_option_selected_or_configured_grants_nothing(self):
        selected = self.view(answer={"option": "c",
                                     "proof": {"digest": "d" * 64}})
        right = self.active(selected)
        self.assertFalse(right["active"])
        self.assertIn("option", right["why"])
        configured = ({"gesture": "root", "beneficiary": "bureau", "host": HOST,
                       "lot": 7372, "decision": 219407, "option": "c",
                       "expires": "2026-12-12"},)
        with mock.patch("ameesh.decisions.get", return_value=selected), \
             mock.patch("ameesh.decisions.verify_proof", return_value=(True, "")):
            right = staging_deferred.active_right(
                object(), project(rights=configured), HOST, today="2026-11-20")
        self.assertFalse(right["active"])
        self.assertIn("no grant", right["why"])

    def test_a_refusal_wording_in_the_signed_option_grants_nothing(self):
        view = self.view(options=self.options(REFUSAL))
        right = self.active(view)
        self.assertFalse(right["active"])
        self.assertIn("no grant", right["why"])

    def test_a_grant_for_another_host_grants_nothing(self):
        other = LABEL.replace("manaty-mesh-1", "pc-smichea")
        right = self.active(self.view(options=self.options(other)))
        self.assertFalse(right["active"])
        self.assertIn("names", right["why"])

    def test_a_structured_grant_in_the_signed_option_is_used(self):
        grant = {"gesture": "root", "beneficiary": "bureau", "host": HOST,
                 "expires": "2026-12-12"}
        view = self.view(options=self.options("", grant=grant))
        right = self.active(view)
        self.assertTrue(right["active"])

    def test_a_configuration_expiry_beyond_the_signed_one_grants_nothing(self):
        # The hostile derived fields must not buy a longer right: the signed
        # option still says 12/12 of the decision's signed creation year.
        view = self.view(answer={"option": "a", "option_label": LABEL.replace(
            "12/12", "2030-12-12"), "ts": 1893456000.0,
            "proof": {"digest": "d" * 64}})
        later = ({"gesture": "root", "beneficiary": "bureau", "host": HOST,
                  "lot": 7372, "decision": 219407, "option": "a",
                  "expires": "2027-12-12"},)
        with mock.patch("ameesh.decisions.get", return_value=view), \
             mock.patch("ameesh.decisions.verify_proof", return_value=(True, "")):
            right = staging_deferred.active_right(
                object(), project(rights=later), HOST, today="2026-11-20")
        self.assertFalse(right["active"])
        self.assertIn("exceeds the approved", right["why"])

    def test_a_withdrawn_or_superseded_decision_grants_nothing(self):
        for view in (self.view(state="withdrawn", withdrawn="why"),
                     self.view(superseded_by=2),
                     self.view(answer={"proof": {}}),
                     self.view(state="pending", answer=None)):
            with mock.patch("ameesh.decisions.get", return_value=view):
                right = staging_deferred.active_right(object(), project(), HOST,
                                                      today="2026-11-20")
            self.assertFalse(right["active"], view)

    def test_an_unreadable_decision_or_an_expired_right_grants_nothing(self):
        with mock.patch("ameesh.decisions.get", side_effect=RuntimeError("down")):
            right = staging_deferred.active_right(object(), project(), HOST,
                                                  today="2026-11-20")
        self.assertFalse(right["active"])
        with mock.patch("ameesh.decisions.get") as decision:
            right = staging_deferred.active_right(object(), project(), HOST,
                                                  today="2026-12-13")
        self.assertTrue(right["expired"])
        self.assertFalse(right["active"])
        decision.assert_not_called()


class ConfigTest(unittest.TestCase):
    def test_staging_rights_are_parsed_and_canonicalised(self):
        rights = release_cutoff._staging_rights(
            "ameesh", [{"gesture": " ROOT ", "beneficiary": "bureau",
                        "host": " manaty-mesh-1 ", "lot": 7372,
                        "decision": 219407, "option": " a ",
                        "expires": "2026-12-12"}])
        self.assertEqual(rights[0]["host"], HOST)
        self.assertEqual(rights[0]["decision"], 219407)
        self.assertEqual(rights[0]["option"], "a")
        self.assertEqual(rights[0]["gesture"], "root")
        self.assertEqual(rights[0]["beneficiary"], "bureau")
        self.assertEqual(rights[0]["lot"], 7372)

    def test_an_invalid_right_is_refused_at_configuration_time(self):
        for raw in ("x", [None], [{"host": ""}],
                    [{"gesture": "root", "beneficiary": "bureau", "host": HOST,
                      "lot": 7372, "decision": 0}],
                    [{"gesture": "root", "beneficiary": "bureau", "host": HOST,
                      "lot": 7372, "decision": 1, "expires": "12/12/2026"}],
                    [{"gesture": "root", "beneficiary": "bureau", "host": HOST,
                      "lot": 7372, "decision": 1, "expires": "2026-12-12"}],
                    [{"gesture": "", "beneficiary": "bureau", "host": HOST,
                      "lot": 7372, "decision": 1, "option": "a",
                      "expires": "2026-12-12"}],
                    [{"gesture": "root", "beneficiary": "", "host": HOST,
                      "lot": 7372, "decision": 1, "option": "a",
                      "expires": "2026-12-12"}],
                    [{"gesture": "root", "beneficiary": "bureau", "host": HOST,
                      "decision": 1, "option": "a", "expires": "2026-12-12",
                      "granted": "2026-11-16"}]):
            with self.assertRaises(release_cutoff.ReleaseError, msg=raw):
                release_cutoff._staging_rights("ameesh", raw)


class DeclareTest(unittest.TestCase):
    base = {"sha": SHA, "tag": "v1.57.0", "date": "2026-11-15T22:31:37Z"}

    def test_a_successful_declaration_carries_the_right_and_is_written(self):
        with mock.patch.object(staging_deferred, "active_right",
                               return_value=active()), \
             mock.patch("ameesh.events.record", return_value=42) as record:
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

    def test_a_withdrawn_or_expired_right_refuses_a_successful_declaration(self):
        with mock.patch.object(staging_deferred, "active_right",
                               return_value=expired()):
            with self.assertRaises(ValueError):
                staging_deferred.declare(object(), project(), HOST, SHA,
                                         self.base, "ok", "bureau")

    def test_a_failed_gesture_is_recorded_without_the_right(self):
        with mock.patch.object(staging_deferred, "active_right", return_value=None), \
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

    def test_history_read_failure_is_not_an_empty_baseline(self):
        with mock.patch("ameesh.storage.of", side_effect=RuntimeError("permission denied")):
            with self.assertRaisesRegex(release_cutoff.ReleaseError, "history unreadable.*permission denied"):
                staging_deferred.baseline(object(), project(), HOST)
            # Background/local-proof callers retain the historical fallback.
            self.assertEqual(release_cutoff.stored_attestations(object()), [])

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

    def tick(self, rows, by_commit, *, declared=None, right=active(), pairs=(),
             today=None):
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
             mock.patch.object(staging_deferred, "active_right",
                               return_value=right), \
             mock.patch.object(staging_deferred, "_is_ancestor",
                               side_effect=ancestry), \
             mock.patch.object(staging_deferred, "_registry_rows", return_value=[]), \
             mock.patch("ameesh.alert_briefs.project_pilots",
                        return_value=["pilot"]), \
             mock.patch("ameesh.mail.send") as send:
            report = staging_deferred.tick(self.cfg, object(), project(), today=today)
        return report, send

    def test_a_declaration_is_a_trace_and_does_not_lift_the_alert(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        declared = {HOST: {"result": "ok", "sha": SHA,
                           "date": "2026-11-16T00:00:00Z", "actor": "bureau"}}
        report, send = self.tick(rows, {SHA: TAG}, declared=declared)
        self.assertEqual(report["result"], "alerted")
        self.assertEqual(send.call_count, 2)
        body = send.call_args_list[0].args[3]
        self.assertIn("dernier geste déclaré (trace, non prouvé)", body)
        self.assertIn(SHA[:12], body)

    def test_a_later_attestation_that_proved_the_host_lifts_the_alert(self):
        newer = "e" * 40
        rows = [attestation(newer, hotes={h: {"actif": newer}
                                         for h in AGENTS + (HOST,)}),
                attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        report, send = self.tick(rows, {SHA: TAG}, pairs={(SHA, newer)})
        self.assertEqual(report["result"], "none")
        self.assertEqual(send.call_count, 0)

    def test_the_alert_names_the_decision_and_its_expiry(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        _report, send = self.tick(rows, {SHA: TAG})
        body = send.call_args_list[0].args[3]
        self.assertIn("décision #219407", body)
        self.assertIn("2026-12-12", body)
        self.assertIn("ameesh release catchup", body)
        self.assertIn("sans valeur de preuve", body)

    def test_after_the_expiry_the_alert_asks_for_a_new_decision(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        _report, send = self.tick(rows, {SHA: TAG}, right=expired(),
                                  today=datetime.date(2026, 12, 13))
        body = send.call_args_list[0].args[3]
        self.assertIn("droit root #219407 expiré le 2026-12-12", body)
        self.assertIn("demander une décision owner", body)

    def test_a_withdrawn_right_is_reported_as_such(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        right = active(active=False, why="decision not answered, withdrawn or superseded")
        _report, send = self.tick(rows, {SHA: TAG}, right=right)
        body = send.call_args_list[0].args[3]
        self.assertIn("retiré ou illisible", body)
        self.assertIn("demander une décision owner", body)

    def test_the_tick_report_carries_the_right_and_the_declaration(self):
        rows = [attestation(SHA, reportes={HOST: {"raison": "deferred"}})]
        declared = {HOST: {"result": "failed", "sha": SHA,
                           "date": "2026-11-16T00:00:00Z", "actor": "bureau"}}
        report, _send = self.tick(rows, {SHA: TAG}, declared=declared)
        self.assertTrue(report["rights"][HOST]["active"])
        self.assertEqual(report["declarations"][HOST]["result"], "failed")


class AnnotateAndReportTest(unittest.TestCase):
    def test_annotate_adds_the_declaration_and_the_right_to_the_payload(self):
        staging = {"attestation": attestation(
            SHA, reportes={HOST: {"raison": "deferred"},
                           "pc-smichea": {"raison": "poste"}})}
        declared = {HOST: {"result": "ok", "sha": SHA,
                           "date": "2026-11-16T00:00:00Z", "actor": "bureau"}}
        with mock.patch.object(staging_deferred, "declarations", return_value=declared), \
             mock.patch.object(staging_deferred, "active_right", return_value=active()):
            staging_deferred.annotate(object(), project(), staging)
        self.assertEqual(staging["catchups"][HOST]["result"], "ok")
        self.assertEqual(staging["rights"][HOST]["decision"], 219407)
        self.assertNotIn("pc-smichea", staging["catchups"])
        self.assertNotIn("pc-smichea", staging["rights"])

    def test_the_report_names_the_declared_gesture_and_the_right(self):
        body = self.report(
            catchups={HOST: {"result": "ok", "sha": SHA,
                             "date": "2026-11-16T00:00:00Z", "actor": "bureau"}},
            rights={HOST: active()})
        self.assertIn("dernier geste déclaré (trace, non prouvé) : commit %s"
                      % SHA[:12], body)
        self.assertIn("par bureau", body)

    def test_the_report_says_when_no_gesture_was_declared(self):
        body = self.report(catchups={}, rights={HOST: active()})
        self.assertIn("aucun geste déclaré (droit #219407 actif jusqu'au 2026-12-12)",
                      body)

    def test_the_report_says_when_the_right_was_withdrawn(self):
        body = self.report(
            catchups={},
            rights={HOST: active(active=False, why="decision superseded")})
        self.assertIn("aucun geste déclaré (droit #219407 retiré : decision superseded)",
                      body)

    def report(self, *, catchups, rights):
        rules = types.SimpleNamespace(zone=lambda: ZoneInfo("Europe/Paris"))
        staging = {
            "exit": 0, "duration_s": 12.0, "log": "/tmp/journal", "summary": [],
            "attestation": attestation(SHA, reportes={HOST: {"raison": "deferred"}}),
            "catchups": catchups, "rights": rights,
        }
        return release_cutoff._report_body(
            types.SimpleNamespace(name="ameesh", target="main"), None,
            result="released", version="1.57.0", tag="v1.57.0",
            release_sha=SHA, staging=staging, deviations=[], previous=None,
            cutoff_at=0.0, cutoff_label="23:00", rules=rules)


class ActorTest(unittest.TestCase):
    def actor(self, *, name="", ok=True, source="session", explicit="",
              bureau=True, human="human:smichea"):
        binding = types.SimpleNamespace(ok=ok, name=name, source=source,
                                        reason="unbound" if not ok else "")
        with mock.patch("ameesh.identity.resolve_binding", return_value=binding), \
             mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": ""}), \
             mock.patch("ameesh.agent_names.resolve", side_effect=lambda text, **kw: text), \
             mock.patch("ameesh.canon.load_configured", return_value=[]), \
             mock.patch("ameesh.sous_utilisation.role_orchestrators",
                        return_value={"bureau"} if bureau else set()), \
             mock.patch("ameesh.work.known_human",
                        return_value=human if human else None):
            return release._catchup_actor(types.SimpleNamespace(), object(), explicit)

    def test_the_bound_bureau_declares(self):
        self.assertEqual(self.actor(name="bureau"), "bureau")

    def test_another_agent_cannot_declare_or_act_for_the_bureau(self):
        with self.assertRaises(release_cutoff.ReleaseError):
            self.actor(name="mty-amh-impl-05", bureau=False)
        with self.assertRaises(release_cutoff.ReleaseError):
            self.actor(name="mty-amh-impl-05", explicit="bureau")

    def test_a_self_declared_name_is_not_a_bound_bureau(self):
        with self.assertRaises(release_cutoff.ReleaseError):
            self.actor(name="bureau", source="explicit")

    def test_an_unbound_session_is_a_human_terminal_only(self):
        self.assertEqual(self.actor(ok=False, source="none", explicit="human:smichea"),
                         "human:smichea")
        with self.assertRaises(release_cutoff.ReleaseError):
            self.actor(ok=False, source="none", explicit="bureau")
        with self.assertRaises(release_cutoff.ReleaseError):
            self.actor(ok=False, source="none", human=None)


class CliTest(unittest.TestCase):
    def run_cmd(self, *, right=active(), declared=True, actor="bureau"):
        cfg = types.SimpleNamespace()
        base = {"sha": SHA, "tag": "v1.57.0"}
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
             mock.patch.object(staging_deferred, "active_right", return_value=right), \
             mock.patch.object(release, "_catchup_actor", return_value=actor), \
             mock.patch.object(staging_deferred, "declare", return_value=row) as declare:
            code = release._catchup(cfg, args)
        return code, declare, db

    def test_an_active_right_declares_the_gesture(self):
        code, declare, db = self.run_cmd()
        self.assertEqual(code, 0)
        declare.assert_called_once()
        db.close.assert_called_once()

    def test_an_expired_or_withdrawn_right_refuses_the_declaration(self):
        for right in (expired(), active(active=False, why="decision superseded")):
            code, declare, db = self.run_cmd(right=right)
            self.assertEqual(code, 1)
            declare.assert_not_called()
            db.close.assert_called_once()

    def test_unreadable_history_is_an_explicit_failure(self):
        import contextlib
        import io
        error = io.StringIO()
        with mock.patch.object(release_cutoff, "stored_attestations",
                               side_effect=release_cutoff.ReleaseError(
                                   "staging attestation history unreadable: permission denied")), \
             contextlib.redirect_stderr(error):
            args = types.SimpleNamespace(project="ameesh", host=HOST, sha=SHA,
                                         result="ok", actor="", note="")
            db = mock.MagicMock()
            with mock.patch.object(release_cutoff, "projects", return_value={"ameesh": project()}), \
                 mock.patch("ameesh.db.open_db", return_value=db):
                self.assertEqual(release._catchup(types.SimpleNamespace(), args), 1)
            db.close.assert_called_once()
        self.assertIn("history unreadable", error.getvalue())

    def test_a_host_that_is_not_reported_has_nothing_to_declare(self):
        code, declare, _db = self.run_cmd(declared=False)
        self.assertEqual(code, 0)
        declare.assert_not_called()


if __name__ == "__main__":
    unittest.main()

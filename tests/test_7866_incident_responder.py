# SPDX-License-Identifier: AGPL-3.0-only
"""#7866 : routage d'incident — canon, signaux de disponibilité, réveil.

Témoins du lot : `canon check` accepte un bloc `incident:` valide et refuse les
formes invalides avec un nom de défaut ; le résolveur choisit le premier
répondant disponible puis le suivant, filtre les non-éligibles et les rôles
non déclarés, applique la fenêtre de fraîcheur de chaque signal, ne réveille
que selon la règle owner et ne journalise que le répondant, la raison et les
types de signaux — jamais un horodatage d'activité ni un historique de
présence.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import canon, incident_routing, responder

from . import test_l124_decisions as fixtures
from .support import PgTestCase, REPO


def _card(root: str, relative: str, text: str) -> None:
    path = os.path.join(root, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _member(root: str, name: str, *, roles=("project-lead",), timezone="Europe/Paris",
            eligible=True, accounts=(), extra="") -> None:
    lines = ["---", "type: Member", "title: %s" % name]
    if roles:
        lines.append("roles: [%s]" % ", ".join(roles))
    if timezone:
        lines.append("timezone: %s" % timezone)
    if eligible is not None:
        lines.append("incident_eligible: %s" % ("true" if eligible else "false"))
    if accounts:
        lines.append("local_accounts: [%s]" % ", ".join('"%s"' % a for a in accounts))
    lines += ["---", "", extra or "# %s" % name, ""]
    _card(root, "membres/%s.md" % name, "\n".join(lines))


def _application(root: str, name: str, body: str = "", *, title=None) -> None:
    text = "\n".join(["---", "type: Application", "title: %s" % (title or name),
                      "responsible: human:alice"] + (body.splitlines() if body else [])
                     + ["---", "", "# %s" % (title or name), ""])
    _card(root, "applications/%s.md" % name, text)


def _incident(*lines: str) -> str:
    return "\n".join(["incident:"] + ["  " + line for line in lines])


class _CanonFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ameesh-7866-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))

    def load(self):
        return canon.load(self.root, untrusted=True)

    def findings(self):
        return canon.validate(self.load())

    def codes(self, severity=canon.ERROR) -> set[str]:
        return {f.code for f in self.findings() if f.severity == severity}


class CanonIncidentTest(_CanonFixture):
    def test_valid_block_is_accepted_and_read(self):
        _member(self.root, "alice")
        _member(self.root, "bruno", roles=("infra",), timezone="Asia/Shanghai")
        _application(self.root, "acme", _incident(
            "severities: [p1, security]",
            "responders:",
            "  - role: project-lead",
            "  - member: human:bruno",
            "escalation:",
            "  wake: human:alice"))
        loaded = self.load()
        self.assertEqual(self.codes(), set())
        application = loaded.application("acme")
        self.assertIsNotNone(application)
        self.assertTrue(application.valid)
        self.assertEqual(application.incident.severities, ("P1", "security"))
        self.assertEqual([r.role for r in application.incident.responders],
                         ["project-lead", None])
        self.assertEqual(application.incident.escalation, "human:alice")
        serialized = application.to_dict()
        self.assertEqual(serialized["incident"]["responders"],
                         [{"role": "project-lead"}, {"member": "human:bruno"}])
        self.assertNotIn("member", serialized["incident"]["responders"][0])

    def test_card_without_incident_block_stays_valid(self):
        _member(self.root, "alice")
        _application(self.root, "plain")
        loaded = self.load()
        self.assertEqual(self.codes(), set())
        self.assertIsNone(loaded.application("plain").incident)

    def test_malformed_blocks_are_refused_with_a_named_defect(self):
        cases = {
            "mapping": ("application-incident-invalid", "incident: [x]"),
            "unknown-key": ("application-incident-key-unknown",
                            _incident("responders:", "  - role: project-lead",
                                      "escalationn:", "  wake: human:alice")),
            "severity": ("application-incident-severity-unknown",
                         _incident("severities: [P9]", "responders:",
                                   "  - role: project-lead")),
            "severities-empty": ("application-incident-severities-empty",
                                 _incident("severities: []", "responders:",
                                           "  - role: project-lead")),
            "responders-missing": ("application-incident-responders-missing",
                                   _incident("severities: [P1]")),
            "responders-empty": ("application-incident-responders-missing",
                                 _incident("responders: []")),
            "responder-both": ("application-incident-responder-invalid",
                               _incident("responders:",
                                         "  - role: project-lead",
                                         "    member: human:alice")),
            "responder-string": ("application-incident-responder-invalid",
                                 _incident("responders:", "  - project-lead")),
            "member-not-human": ("application-incident-member-invalid",
                                 _incident("responders:", "  - member: alice")),
            "member-unknown": ("application-incident-member-unknown",
                               _incident("responders:", "  - member: human:nobody")),
            "role-unknown": ("application-incident-role-unknown",
                             _incident("responders:", "  - role: ghost")),
            "escalation": ("application-incident-escalation-invalid",
                           _incident("responders:", "  - role: project-lead",
                                     "escalation:", "  wake: 123")),
            "escalation-unknown": ("application-incident-escalation-unknown",
                                   _incident("responders:", "  - role: project-lead",
                                             "escalation:", "  wake: human:nobody")),
        }
        for name, (code, body) in cases.items():
            with self.subTest(name=name):
                root = tempfile.mkdtemp(prefix="ameesh-7866-%s-" % name)
                self.addCleanup(lambda r=root: __import__("shutil").rmtree(
                    r, ignore_errors=True))
                _member(root, "alice")
                if name == "mapping":
                    _card(root, "applications/acme.md",
                          "---\ntype: Application\ntitle: acme\nincident: [x]\n---\n")
                else:
                    _application(root, "acme", body)
                findings = canon.validate(canon.load(root, untrusted=True))
                codes = {f.code for f in findings if f.severity == canon.ERROR}
                self.assertIn(code, codes, (name, sorted(codes)))
                self.assertTrue(any(f.application == "acme" for f in findings
                                    if f.code == code), name)

    def test_application_outside_its_directory_is_refused(self):
        _member(self.root, "alice")
        _card(self.root, "agents/not-an-application.md",
              "---\ntype: Application\ntitle: acme\n---\n")
        self.assertIn("application-path", self.codes())

    def test_member_timezone_and_eligibility_are_validated(self):
        _member(self.root, "alice", timezone="Mars/Olympus")
        _member(self.root, "bruno", eligible=None)
        _member(self.root, "chloe", eligible=False)
        loaded = self.load()
        codes = self.codes()
        self.assertIn("member-timezone-invalid", codes)
        alice = next(m for m in loaded.members if m.title == "alice")
        bruno = next(m for m in loaded.members if m.title == "bruno")
        chloe = next(m for m in loaded.members if m.title == "chloe")
        self.assertIsNone(alice.timezone)
        self.assertFalse(bruno.incident_eligible)
        self.assertFalse(chloe.incident_eligible)

    def test_a_broken_application_blocks_no_agent(self):
        _member(self.root, "alice")
        _application(self.root, "acme", _incident("responders:", "  - role: ghost"))
        loaded = self.load()
        findings = canon.validate(loaded)
        blocking = canon.blocking(findings)
        self.assertEqual(blocking.global_errors, [])
        self.assertTrue(any(f.code == "application-incident-role-unknown"
                            for f in findings))

    def test_application_without_a_name_keeps_the_file_stem(self):
        _member(self.root, "alice")
        _application(self.root, "acme", title="Acme Web")
        loaded = self.load()
        application = loaded.application("acme")
        self.assertEqual(application.id, "acme")
        self.assertEqual(application.title, "Acme Web")


class AvailabilityTest(unittest.TestCase):
    NOW = _dt.datetime(2026, 11, 19, 9, 0, tzinfo=_dt.timezone.utc)  # Paris 10:00

    def member(self, title="alice", timezone="Europe/Paris", eligible=True,
               accounts=("alice@pc",), roles=("project-lead",)):
        return SimpleNamespace(title=title, timezone=timezone,
                               incident_eligible=eligible,
                               local_accounts=list(accounts), roles=list(roles),
                               fiche=SimpleNamespace(data={}))

    def canon_with(self, members, responders, severities=("P1", "security")):
        root = tempfile.mkdtemp(prefix="ameesh-7866-avail-")
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        for member in members:
            _member(root, member.title, roles=member.roles, timezone=member.timezone,
                    eligible=member.incident_eligible,
                    accounts=member.local_accounts or ())
        body = "\n".join(["severities: [%s]" % ", ".join(severities), "responders:"]
                         + ["  - %s" % entry for entry in responders])
        _application(root, "acme", _incident(*body.splitlines()))
        return canon.load(root, untrusted=True), canon.load(root, untrusted=True).application("acme")

    class Signals:
        def __init__(self, attach=None, interaction=None, presence=None):
            self.attach_map = attach or {}
            self.interaction_map = interaction or {}
            self.presence_map = presence or {}

        def attach(self, member):
            return self.attach_map.get(member.title)

        def interaction(self, member, now):
            return self.interaction_map.get(member.title)

        def presence(self, member, now):
            return self.presence_map.get(member.title)

    def test_first_available_in_canon_order(self):
        alice, bruno = self.member("alice"), self.member(
            "bruno", timezone="Asia/Shanghai")
        loaded, application = self.canon_with(
            [alice, bruno], ["role: project-lead", "member: human:bruno"])
        result = responder.resolve(loaded, application, "P1",
                                   self.Signals(attach={"alice": True}), self.NOW)
        self.assertEqual(result.responder.title, "alice")
        self.assertEqual(result.reason, "attached")
        self.assertEqual(result.signals, ("attach",))

    def test_absent_signal_is_not_an_absence_and_passes_on(self):
        alice, bruno = self.member("alice"), self.member(
            "bruno", timezone="Asia/Shanghai")
        loaded, application = self.canon_with([alice, bruno], ["role: project-lead"])
        result = responder.resolve(
            loaded, application, "P1",
            self.Signals(interaction={"alice": True}), self.NOW)
        self.assertEqual(result.responder.title, "alice")
        self.assertEqual(result.reason, "recent-interaction")

    def test_present_away_presence_rules_a_human_out(self):
        alice, bruno = self.member("alice"), self.member(
            "bruno", timezone="Asia/Shanghai")
        loaded, application = self.canon_with(
            [alice, bruno], ["member: human:alice", "member: human:bruno"])
        result = responder.resolve(
            loaded, application, "P1",
            self.Signals(presence={"alice": False}, attach={"bruno": True}), self.NOW)
        self.assertEqual(result.responder.title, "bruno")
        self.assertEqual(result.reason, "attached")
        self.assertEqual(result.consulted, ("attach", "presence"))

    def test_french_member_fallback_while_the_utc8_owner_sleeps(self):
        owner = self.member("bruno", timezone="Asia/Shanghai")
        french = self.member("alice", timezone="Europe/Paris")
        loaded, application = self.canon_with(
            [owner, french], ["member: human:bruno", "member: human:alice"])
        # 18:00 UTC = 02:00 in Shanghai (outside usual hours), 19:00 in Paris
        now = _dt.datetime(2026, 11, 19, 18, 0, tzinfo=_dt.timezone.utc)
        result = responder.resolve(loaded, application, "P1", self.Signals(), now)
        self.assertEqual(result.responder.title, "alice")
        self.assertEqual(result.reason, "usual-hours")
        self.assertEqual(result.signals, ("timezone",))

    def test_member_without_timezone_is_never_reachable_by_default(self):
        erin = self.member("erin", timezone=None)
        loaded, application = self.canon_with([erin], ["member: human:erin"])
        result = responder.resolve(loaded, application, "P1", self.Signals(), self.NOW)
        self.assertIsNone(result.responder)
        self.assertEqual(result.reason, "no-responder")

    def test_not_eligible_humans_are_never_chosen(self):
        alice, ghost = self.member("alice"), self.member("ghost", eligible=False)
        loaded, application = self.canon_with(
            [alice, ghost], ["member: human:ghost", "member: human:alice"])
        result = responder.resolve(loaded, application, "P1",
                                   self.Signals(attach={"ghost": True}), self.NOW)
        self.assertEqual(result.responder.title, "alice")

    def test_a_severity_outside_the_chain_is_refused(self):
        alice = self.member("alice")
        loaded, application = self.canon_with([alice], ["member: human:alice"],
                                              severities=("P1",))
        result = responder.resolve(loaded, application, "P2", self.Signals(), self.NOW)
        self.assertIsNone(result.responder)
        self.assertEqual(result.reason, "severity-not-covered")

    def test_wake_rule(self):
        self.assertEqual(incident_routing.wake_plan("P1", None), "no-responder")
        self.assertEqual(incident_routing.wake_plan("P1", object()), "none")
        self.assertEqual(incident_routing.wake_plan("P2", None), "none")
        self.assertEqual(incident_routing.wake_plan("security", object()), "immediate")
        self.assertEqual(incident_routing.wake_plan("data-loss", None), "immediate")
        self.assertEqual(incident_routing.wake_plan("outage", object()), "immediate")
        self.assertEqual(incident_routing.wake_plan("P3", None), "none")


class WakeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.application = SimpleNamespace(
            id="acme", title="Acme",
            incident=incident_routing.IncidentPolicy(
                severities=("P1", "P2", "security"),
                responders=(incident_routing.Responder(member="human:alice"),),
                escalation="human:alice"),
            responsible="human:alice")
        self.chosen = SimpleNamespace(title="alice")
        self.nobody = responder.Resolution(application=self.application, severity="P1")

    def resolve(self, chosen=None):
        return responder.Resolution(application=self.application, severity="P1",
                                    responder=chosen)

    def test_a_chosen_responder_means_no_call_on_p1(self):
        calls = []

        def transport(*args):
            calls.append(args)
            return 200, b"{}"

        wake = responder.request_call(
            self.application, "P1", self.resolve(self.chosen), db=None,
            env={"AMEESH_INCIDENT_CALL": "1"},
            transport=transport, go_check=lambda *a: (True, ""))
        self.assertEqual(wake.plan, "none")
        self.assertEqual(wake.status, "not-needed")
        self.assertEqual(calls, [])

    def test_the_call_is_off_by_default(self):
        calls = []

        def transport(*args):
            calls.append(args)
            return 200, b"{}"

        wake = responder.request_call(
            self.application, "P1", self.resolve(), db=None, env={},
            transport=transport, go_check=lambda *a: (True, ""))
        self.assertEqual(wake.plan, "no-responder")
        self.assertEqual(wake.status, "disabled")
        self.assertEqual(wake.reason, "flag-off")
        self.assertEqual(calls, [])

    def test_no_owner_go_no_call(self):
        calls = []
        env = {"AMEESH_INCIDENT_CALL": "1"}
        wake = responder.request_call(
            self.application, "P1", self.resolve(), db=None, env=env,
            transport=lambda *a: calls.append(a) or (200, b"{}"),
            go_check=lambda *a: (False, "owner-go-required"))
        self.assertEqual(wake.status, "refused")
        self.assertEqual(wake.reason, "owner-go-required")
        self.assertEqual(calls, [])

    def test_a_signed_owner_go_requests_one_call_with_a_short_reason(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, url, headers, json.loads(body.decode("utf-8")), timeout))
            return 202, b'{"call_id": "call-1"}'

        env = {"AMEESH_INCIDENT_CALL": "1", "AMEESH_INCIDENT_CALL_GO": "42",
               "AMEESH_INCIDENT_VOICE_URL": "https://voice.example/call",
               "AMEESH_INCIDENT_VOICE_TOKEN": "secret"}
        wake = responder.request_call(
            self.application, "P1", self.resolve(), db=None, env=env,
            transport=transport, go_check=lambda db, ident, owner, app: (True, ""))
        self.assertEqual(wake.plan, "no-responder")
        self.assertEqual(wake.status, "requested")
        self.assertEqual(wake.request_id, "call-1")
        self.assertEqual(len(calls), 1)
        method, url, headers, body, _timeout = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://voice.example/call")
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(body["schema"], responder.VOICE_SCHEMA)
        self.assertEqual(body["severity"], "P1")
        self.assertLessEqual(len(body["reason"]), responder.REASON_MAX)
        self.assertNotIn("phone", json.dumps(body).lower())
        self.assertNotIn("+33", json.dumps(body))

    def test_security_wakes_even_when_a_responder_is_available(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(json.loads(body.decode("utf-8")))
            return 200, b"{}"

        env = {"AMEESH_INCIDENT_CALL": "1", "AMEESH_INCIDENT_CALL_GO": "42",
               "AMEESH_INCIDENT_VOICE_URL": "https://voice.example/call",
               "AMEESH_INCIDENT_VOICE_TOKEN": "secret"}
        resolution = responder.Resolution(application=self.application,
                                          severity="security",
                                          responder=self.chosen, reason="attached")
        wake = responder.request_call(
            self.application, "security", resolution, db=None, env=env,
            transport=transport, go_check=lambda *a: (True, ""))
        self.assertEqual(wake.plan, "immediate")
        self.assertEqual(wake.status, "requested")
        self.assertEqual(len(calls), 1)

    def test_p2_never_wakes_even_with_nobody_available(self):
        calls = []

        def transport(*args):
            calls.append(args)
            return 200, b"{}"

        wake = responder.request_call(
            self.application, "P2", self.resolve(), db=None,
            env={"AMEESH_INCIDENT_CALL": "1"}, transport=transport,
            go_check=lambda *a: (True, ""))
        self.assertEqual(wake.plan, "none")
        self.assertEqual(wake.status, "not-needed")
        self.assertEqual(calls, [])

    def test_a_configured_flag_without_endpoint_is_unconfigured(self):
        env = {"AMEESH_INCIDENT_CALL": "1", "AMEESH_INCIDENT_CALL_GO": "42"}
        wake = responder.request_call(
            self.application, "P1", self.resolve(), db=None, env=env,
            transport=lambda *a: (200, b"{}"),
            go_check=lambda *a: (True, ""))
        self.assertEqual(wake.status, "unconfigured")
        self.assertEqual(wake.reason, "voice-endpoint-missing")

    def test_voice_failure_degrades_to_failed(self):
        def transport(*args):
            raise OSError("connection refused")

        env = {"AMEESH_INCIDENT_CALL": "1", "AMEESH_INCIDENT_CALL_GO": "42",
               "AMEESH_INCIDENT_VOICE_URL": "https://voice.example/call",
               "AMEESH_INCIDENT_VOICE_TOKEN": "secret"}
        wake = responder.request_call(
            self.application, "P1", self.resolve(), db=None, env=env,
            transport=transport, go_check=lambda *a: (True, ""))
        self.assertEqual(wake.status, "failed")
        self.assertEqual(wake.reason, "voice-unreachable")


    def test_request_call_passes_the_application_to_the_go_gate(self):
        seen = []

        def go_check(db, ident, owner, application):
            seen.append((ident, owner, application))
            return False, "owner-go-unscoped"

        env = {"AMEESH_INCIDENT_CALL": "1", "AMEESH_INCIDENT_CALL_GO": "42",
               "AMEESH_INCIDENT_VOICE_URL": "https://voice.example/call",
               "AMEESH_INCIDENT_VOICE_TOKEN": "secret"}
        wake = responder.request_call(
            self.application, "P1", self.resolve(), db=None, env=env,
            transport=lambda *a: (200, b"{}"), go_check=go_check)
        self.assertEqual(wake.status, "refused")
        self.assertEqual(wake.reason, "owner-go-unscoped")
        self.assertEqual(seen, [("42", "human:alice", "acme")])


class CallGoParseTest(unittest.TestCase):
    """#7866 (B cx2): the go is one exact, scoped machine line."""

    LINE = ("incident-call/1 application=acme "
            "until=2026-11-26T00:00:00Z")

    def test_one_exact_line_is_read(self):
        rule = incident_routing.parse_call_go("Autoriser ?\n%s" % self.LINE)
        self.assertEqual(rule.application, "acme")
        self.assertEqual(rule.until, 1795651200.0)

    def test_a_question_without_the_marker_has_no_go(self):
        self.assertIsNone(incident_routing.parse_call_go("Une question sans portée"))

    def test_two_markers_are_ambiguous_and_refused(self):
        with self.assertRaises(ValueError):
            incident_routing.parse_call_go("%s\n%s" % (self.LINE, self.LINE))

    def test_a_bad_date_is_refused(self):
        with self.assertRaises(ValueError):
            incident_routing.parse_call_go(
                "incident-call/1 application=acme until=demain")


class OwnerGoTest(fixtures._Base):
    """#7866 (B cx2): the phone wake needs a positive, scoped signed go.

    A signed answer, even by the wake owner, is not enough: the signed
    question must carry one `incident-call/1` line for this application, the
    request must offer the reserved `incident-call` option with its exact
    positive label, and the signed answer must select it inside the window.
    A signed "no", a refusal option dressed as the go, or an unrelated
    decision authorizes nothing.
    """

    def setUp(self):
        super().setUp()
        # reuse the owner-key fixture without inheriting its own test methods
        self.key = fixtures.SignatureTest.owner_key(self)
        # a fixed instant: the wire is deterministic (H3, no clock dependency)
        self.now = _dt.datetime(2026, 11, 19, 18, 0, tzinfo=_dt.timezone.utc)

    def go(self, *, application="acme", until=None, options=None):
        until = until or self.now + _dt.timedelta(days=1)
        options = options or ("incident-call=%s" % incident_routing.CALL_GO_LABEL,
                              "no=Non")
        question = ("Autoriser les appels de réveil ?\n"
                    "incident-call/1 application=%s until=%s"
                    % (application, until.strftime("%Y-%m-%dT%H:%M:%SZ")))
        return self.ask(question=question, options=list(options),
                        needs_signature=True)

    def check(self, view, *, application="acme", now=None):
        return responder.verify_owner_go(
            self.db, view["id"], "human:alice", application, now=now)

    def test_a_scoped_signed_go_authorizes_the_call(self):
        view = self.go()
        self.answer(view["id"], "incident-call", key_path=self.key)
        allowed, why = self.check(view, now=self.now.timestamp())
        self.assertTrue(allowed, why)

    def test_a_signed_refusal_does_not_authorize(self):
        view = self.go()
        self.answer(view["id"], "Non", key_path=self.key)
        self.assertEqual(self.check(view, now=self.now.timestamp()),
                         (False, "owner-go-not-approved"))

    def test_a_signed_free_text_answer_does_not_authorize(self):
        view = self.go()
        self.answer(view["id"], "je préfère attendre la revue", key_path=self.key)
        self.assertEqual(self.check(view, now=self.now.timestamp()),
                         (False, "owner-go-not-approved"))

    def test_a_refusal_option_dressed_as_the_go_is_refused(self):
        # B cx2 second round: the reserved key with a "Non" label must never
        # turn a signed refusal into an authorization.
        view = self.go(options=("incident-call=Non, refuser les appels",
                                "yes=Oui"))
        self.answer(view["id"], "incident-call", key_path=self.key)
        self.assertEqual(self.check(view, now=self.now.timestamp()),
                         (False, "owner-go-option-not-positive"))

    def test_the_reserved_option_must_be_present(self):
        view = self.go(options=("call=Oui, autoriser les appels", "no=Non"))
        self.answer(view["id"], "call", key_path=self.key)
        self.assertEqual(self.check(view, now=self.now.timestamp()),
                         (False, "owner-go-option-missing"))

    def test_an_unrelated_signed_decision_does_not_authorize(self):
        view = self.ask(needs_signature=True)
        self.answer(view["id"], "a", key_path=self.key)
        self.assertEqual(self.check(view, now=self.now.timestamp()),
                         (False, "owner-go-unscoped"))

    def test_another_application_does_not_authorize(self):
        view = self.go(application="other")
        self.answer(view["id"], "incident-call", key_path=self.key)
        self.assertEqual(self.check(view, now=self.now.timestamp()),
                         (False, "owner-go-other-application"))

    def test_an_expired_window_does_not_authorize(self):
        view = self.go(until=self.now - _dt.timedelta(hours=1))
        self.answer(view["id"], "incident-call", key_path=self.key)
        self.assertEqual(self.check(view, now=self.now.timestamp()),
                         (False, "owner-go-expired"))

    def test_a_window_beyond_the_cap_does_not_authorize(self):
        view = self.go(until=self.now + _dt.timedelta(
            hours=incident_routing.CALL_GO_MAX_HOURS + 1))
        self.answer(view["id"], "incident-call", key_path=self.key)
        self.assertEqual(self.check(view, now=self.now.timestamp()),
                         (False, "owner-go-window"))

    def test_pending_and_unsigned_decisions_are_refused(self):
        view = self.go()
        self.assertEqual(self.check(view, now=self.now.timestamp()),
                         (False, "owner-go-pending"))


class JournalPrivacyTest(unittest.TestCase):
    def test_the_journal_keeps_only_the_choice_reason_and_signal_kinds(self):
        application = SimpleNamespace(id="acme", title="Acme")
        resolution = responder.Resolution(
            application=application, severity="P1",
            responder=SimpleNamespace(title="alice"), reason="attached",
            signals=("attach",), consulted=("attach", "presence"))
        wake = responder.Wake(plan="none", status="not-needed")
        with mock.patch.object(responder.events, "record",
                               return_value=11) as record:
            event_id = responder.journal_route(None, resolution, wake)
        self.assertEqual(event_id, 11)
        args, kwargs = record.call_args
        self.assertEqual(args[0], None)
        self.assertEqual(args[1], responder.EVENT)
        self.assertEqual(args[2], "application:acme")
        data = kwargs
        self.assertEqual(data["responder"], "human:alice")
        self.assertEqual(data["signals"], ["attach"])
        for forbidden in ("ts", "timestamp", "activity", "presence", "candidates",
                          "online", "last_seen", "phone"):
            self.assertNotIn(forbidden, data)
        self.assertNotIn("alice@pc", json.dumps(data))
        self.assertNotIn("Europe/Paris", json.dumps(data))

    def test_the_public_document_has_no_activity_detail(self):
        application = SimpleNamespace(id="acme", title="Acme")
        resolution = responder.Resolution(
            application=application, severity="P1",
            responder=SimpleNamespace(title="alice"), reason="attached",
            signals=("attach",), consulted=("attach",))
        document = responder.document(
            resolution, responder.Wake(plan="none", status="not-needed"),
            journal_id=3)
        self.assertEqual(document["schema"], responder.SCHEMA)
        self.assertTrue(document["ok"])
        self.assertEqual(document["signals"], ["attach"])
        text = json.dumps(document)
        for forbidden in ("ts", "activity", "presence", "online", "email", "phone"):
            self.assertNotIn(forbidden, text)


class LiveSignalsTest(unittest.TestCase):
    NOW = _dt.datetime(2026, 11, 19, 9, 0, tzinfo=_dt.timezone.utc)

    class Db:
        def __init__(self, rows=None, boom=False):
            self.rows = rows or []
            self.boom = boom
            self.sql = []

        def query(self, sql, params=()):
            self.sql.append((sql, params))
            if self.boom:
                raise RuntimeError("database unavailable")
            return self.rows

    def member(self, title="alice", accounts=("alice@pc",), timezone="Europe/Paris",
               emails=()):
        return SimpleNamespace(
            title=title, timezone=timezone, local_accounts=list(accounts),
            fiche=SimpleNamespace(data={"emails": list(emails)} if emails else {}))

    def test_attach_matches_a_live_lease_of_a_declared_local_account(self):
        db = self.Db([{"lease_owner": "attach:alice@pc"}])
        signals = responder.LiveSignals(db=db, env={})
        self.assertTrue(signals.attach(self.member()))
        self.assertIn("like 'attach:%'", db.sql[0][0])

    def test_attach_is_absent_without_a_live_lease_or_an_account(self):
        self.assertIsNone(responder.LiveSignals(
            db=self.Db([{"lease_owner": "attach:bob@pc"}]), env={}
        ).attach(self.member()))
        self.assertIsNone(responder.LiveSignals(
            db=self.Db([{"lease_owner": "attach:alice@pc"}]), env={}
        ).attach(self.member(accounts=())))

    def test_attach_degrades_when_the_database_fails(self):
        self.assertIsNone(responder.LiveSignals(
            db=self.Db(boom=True), env={}).attach(self.member()))

    def test_a_fresh_journal_interaction_is_available(self):
        epoch = self.NOW.timestamp() - 60
        signals = responder.LiveSignals(db=self.Db([
            {"source": "sent", "ts": epoch, "proof": "alice@pc",
             "host": "pc"}]), env={})
        self.assertTrue(signals.interaction(self.member(), self.NOW))

    def test_a_stale_journal_interaction_passes_to_memoria(self):
        epoch = self.NOW.timestamp() - incident_routing.INTERACTION_FRESH_S - 60
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(url)
            return 200, json.dumps({
                "schema": responder.MEMORIA_SCHEMA,
                "human": "human:alice",
                "last_interaction": "2026-11-19T08:59:00Z"}).encode("utf-8")

        env = {"AMEESH_INCIDENT_MEMORIA_URL": "https://memoria.example"}
        signals = responder.LiveSignals(db=self.Db([
            {"source": "sent", "ts": epoch, "proof": "alice@pc",
             "host": "pc"}]), env=env, transport=transport)
        self.assertTrue(signals.interaction(self.member(), self.NOW))
        self.assertEqual(len(calls), 1)

    def test_a_relayed_mail_from_another_host_is_not_the_humans_activity(self):
        epoch = self.NOW.timestamp() - 60
        signals = responder.LiveSignals(db=self.Db([
            {"source": "sent", "ts": epoch, "proof": None,
             "host": "ameesh-agents-2"}]), env={})
        self.assertIsNone(signals.journal_last(self.member()))
        self.assertIsNone(signals.interaction(self.member(), self.NOW))

    def test_a_mail_sent_from_a_declared_host_counts_without_a_proof(self):
        epoch = self.NOW.timestamp() - 60
        signals = responder.LiveSignals(db=self.Db([
            {"source": "sent", "ts": epoch, "proof": "", "host": "pc"}]), env={})
        self.assertTrue(signals.interaction(self.member(), self.NOW))

    def test_a_bureau_answer_is_never_the_owners_own_activity(self):
        # B1 de l'auditeur : une réponse « saisie par le bureau, délégation
        # §10 » ne compte pas — même si son hôte est déclaré — et la plus
        # récente réponse humaine, elle, compte.
        human = self.NOW.timestamp() - 120
        bureau = self.NOW.timestamp() - 30
        signals = responder.LiveSignals(db=self.Db([
            {"source": "answer", "ts": human, "proof": "alice@pc", "host": "pc",
             "answer_text": "a"},
            {"source": "answer", "ts": bureau, "host": "pc",
             "answer_text": "a — saisi par le bureau, délégation §10"},
            {"source": "answer", "ts": bureau, "host": "ameesh-agents-2",
             "answer_text": "a — réponse de l'owner relayée par le bureau, "
                            "source session-1"},
        ]), env={})
        last = signals.journal_last(self.member())
        self.assertIsNotNone(last)
        self.assertEqual(last.timestamp(), human)

    def test_a_decision_answer_needs_a_declared_device(self):
        epoch = self.NOW.timestamp() - 30
        signals = responder.LiveSignals(db=self.Db([
            {"source": "answer", "ts": epoch, "proof": None,
             "host": "ameesh-agents-2", "answer_text": "a"}]), env={})
        self.assertIsNone(signals.journal_last(self.member()))

    def test_missing_memoria_and_slack_degrade_cleanly(self):
        signals = responder.LiveSignals(db=self.Db([]), env={})
        self.assertIsNone(signals.interaction(self.member(), self.NOW))
        self.assertIsNone(signals.presence(self.member(), self.NOW))

    def test_slack_presence_is_read_by_user_id(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(url)
            if "getPresence" in url:
                return 200, b'{"ok": true, "presence": "active"}'
            return 500, b""

        env = {"AMEESH_INCIDENT_SLACK_TOKEN": "secret",
               "AMEESH_INCIDENT_SLACK_USERS": '{"alice": "U1"}'}
        signals = responder.LiveSignals(env=env, transport=transport)
        self.assertTrue(signals.presence(self.member(), self.NOW))
        self.assertIn("user=U1", calls[0])

    def test_slack_away_is_a_present_negative_signal(self):
        env = {"AMEESH_INCIDENT_SLACK_TOKEN": "secret",
               "AMEESH_INCIDENT_SLACK_USERS": '{"alice": "U1"}'}
        signals = responder.LiveSignals(
            env=env, transport=lambda *a: (200, b'{"ok": true, "presence": "away"}'))
        self.assertFalse(signals.presence(self.member(), self.NOW))

    def test_slack_errors_are_absence_not_away(self):
        env = {"AMEESH_INCIDENT_SLACK_TOKEN": "secret",
               "AMEESH_INCIDENT_SLACK_USERS": '{"alice": "U1"}'}
        signals = responder.LiveSignals(
            env=env, transport=lambda *a: (500, b"{}"))
        self.assertIsNone(signals.presence(self.member(), self.NOW))

    def test_presence_reads_are_kept_for_their_freshness_window(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(url)
            return 200, b'{"ok": true, "presence": "active"}'

        env = {"AMEESH_INCIDENT_SLACK_TOKEN": "secret",
               "AMEESH_INCIDENT_SLACK_USERS": '{"alice": "U1"}'}
        signals = responder.LiveSignals(env=env, transport=transport)
        self.assertTrue(signals.presence(self.member(), self.NOW))
        self.assertTrue(signals.presence(
            self.member(), self.NOW + _dt.timedelta(seconds=60)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(signals.presence(
            self.member(),
            self.NOW + _dt.timedelta(seconds=incident_routing.PRESENCE_FRESH_S + 1)))
        self.assertEqual(len(calls), 2)


class JournalAttributionPgTest(PgTestCase):
    """#7866 (B1 auditeur) : la requête réelle du journal ne retient que
    l'activité du poste de l'humain ; une réponse « saisie par le bureau » et
    un courrier relayé sous `human:<id>` ne comptent pas."""

    def member(self, accounts=("alice@pc-smichea",)):
        return SimpleNamespace(title="alice", local_accounts=list(accounts),
                               timezone="Europe/Paris",
                               fiche=SimpleNamespace(data={}))

    def add_answer(self, *, ts, text, host=None, proof=None):
        answer = {"by": "human:alice", "channel": "signed", "text": text,
                  "option": "a", "ts": ts}
        if host:
            answer["host"] = host
        if proof:
            answer["human_account"] = proof
        payload = {"decision": {"state": "answered", "answer": answer}}
        self.db.execute(
            "insert into agent_mailbox (sender, recipient, body, kind, payload, host)"
            " values (%s, %s, %s, 'request', %s::jsonb, %s)",
            ("orch", "human:alice", "question", json.dumps(payload), host))

    def test_a_bureau_answer_does_not_count_as_a_recent_interaction(self):
        human = time.time() - 120
        bureau = time.time() - 30
        self.add_answer(ts=human, text="a", host="pc-smichea",
                        proof="alice@pc-smichea")
        self.add_answer(ts=bureau, text="a — saisi par le bureau, délégation §10",
                        host="ameesh-agents-2")
        last = responder.LiveSignals(db=self.db).journal_last(self.member())
        self.assertIsNotNone(last)
        self.assertAlmostEqual(last.timestamp(), human, delta=1)

    def test_a_relayed_mail_does_not_count(self):
        self.db.execute(
            "insert into agent_mailbox (sender, recipient, body, payload, host)"
            " values ('human:alice', 'orch', 'hello', %s::jsonb, %s)",
            (json.dumps({}), "ameesh-agents-2"))
        self.assertIsNone(
            responder.LiveSignals(db=self.db).journal_last(self.member()))

    def test_mail_sent_from_the_owners_account_counts(self):
        self.db.execute(
            "insert into agent_mailbox (sender, recipient, body, payload, host)"
            " values ('human:alice', 'orch', 'hello', %s::jsonb, %s)",
            (json.dumps({"human_account": "alice@pc-smichea"}), "pc-smichea"))
        self.assertIsNotNone(
            responder.LiveSignals(db=self.db).journal_last(self.member()))


class HumanAccountProofTest(fixtures._Base):
    """#7866 (B1) : la réponse d'un humain interactif porte la preuve
    `user@host`, celle qui distingue son poste d'un relais."""

    def test_an_interactive_human_answer_carries_the_local_account_proof(self):
        from ameesh import resident_queue
        view = self.ask()
        answer = self.answer(view["id"], "a")["decision"]["answer"]
        user, host = resident_queue.local_account(self.cfg)
        self.assertEqual(answer.get("human_account"), "%s@%s" % (user, host))


class CliResponderTest(PgTestCase):
    """The end-to-end contract: a P1 picks the French member while the UTC+8
    owner is asleep; a simulation writes nothing, and the live journal event
    keeps only the choice, the reason and the signal kinds."""

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="ameesh-7866-cli-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root,
                                                            ignore_errors=True))
        _member(self.root, "alice", roles=("project-lead",),
                timezone="Europe/Paris", eligible=True)
        _member(self.root, "bruno", roles=("project-lead",),
                timezone="Asia/Shanghai", eligible=True)
        _application(self.root, "acme", _incident(
            "severities: [P1, P2, security]",
            "responders:",
            "  - member: human:bruno",
            "  - member: human:alice",
            "escalation:",
            "  wake: human:alice"))

    def env(self, **extra):
        base = dict(super().env())
        base.update(extra)
        for key in list(base):
            if key.startswith("AMEESH_INCIDENT_"):
                base.pop(key)
        base["AMEESH_CANON_UNTRUSTED"] = "1"
        return base

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env(), clear=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = responder.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_a_p1_picks_the_available_french_member_without_writing_anything(self):
        # 18:00 UTC = 02:00 Shanghai (owner asleep), 19:00 Paris (at work).
        # `--now` is a simulation: no call, no real journal event (#7866 N1).
        code, out, err = self.run_cli(
            "acme", "--severity", "P1", "--canon", self.root,
            "--now", "2026-11-19T18:00:00Z", "--json")
        self.assertEqual(code, 0, err)
        document = json.loads(out)
        self.assertEqual(document["schema"], responder.SCHEMA)
        self.assertTrue(document["ok"])
        self.assertEqual(document["responder"]["member"], "human:alice")
        self.assertEqual(document["reason"], "usual-hours")
        self.assertEqual(document["signals"], ["timezone"])
        self.assertEqual(document["wake"]["plan"], "none")
        self.assertEqual(document["wake"]["status"], "simulated")
        self.assertIsNone(document["journal"])
        rows = self.db.query(
            "select id from ameesh_events where type = %s", (responder.EVENT,))
        self.assertEqual(rows, [])

    def test_a_simulation_never_calls_even_with_the_flag_and_a_go(self):
        env = dict(self.env(), AMEESH_INCIDENT_CALL="1",
                   AMEESH_INCIDENT_CALL_GO="42",
                   AMEESH_INCIDENT_VOICE_URL="https://voice.example/call",
                   AMEESH_INCIDENT_VOICE_TOKEN="secret")
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(responder, "request_call") as call, \
                mock.patch.object(responder, "journal_route") as journal, \
                contextlib.redirect_stdout(out):
            code = responder.main(["acme", "--severity", "P1", "--canon",
                                   self.root, "--now", "2026-11-19T18:00:00Z",
                                   "--json"])
        self.assertEqual(code, 0)
        call.assert_not_called()
        journal.assert_not_called()
        self.assertIsNone(json.loads(out.getvalue())["journal"])

    def test_the_journal_event_keeps_only_the_choice_reason_and_signal_kinds(self):
        loaded = canon.load(self.root, untrusted=True)
        application = loaded.application("acme")
        now = _dt.datetime(2026, 11, 19, 18, 0, tzinfo=_dt.timezone.utc)
        resolution = responder.resolve(loaded, application, "P1",
                                       incident_routing.NoSignals(), now)
        self.assertEqual(resolution.responder.title, "alice")
        event_id = responder.journal_route(
            self.db, resolution, responder.Wake(plan="none", status="not-needed"))
        rows = self.db.query(
            "select data from ameesh_events where id = %s", (event_id,))
        self.assertEqual(len(rows), 1)
        data = rows[0]["data"]
        if isinstance(data, str):
            data = json.loads(data)
        self.assertEqual(data["responder"], "human:alice")
        self.assertEqual(data["signals"], ["timezone"])
        for forbidden in ("ts", "timestamp", "activity", "presence", "online"):
            self.assertNotIn(forbidden, data)
        self.assertNotIn("Europe/Paris", json.dumps(data))

    def test_nobody_available_on_p1_is_not_ok_and_wakes_nobody_by_default(self):
        # 02:00 UTC = 10:00 Shanghai (owner at work), 03:00 Paris (asleep):
        # reverse the chain order by serving a second application.
        _application(self.root, "owner-first", _incident(
            "severities: [P1]",
            "responders:",
            "  - member: human:bruno"))
        code, out, err = self.run_cli(
            "owner-first", "--severity", "P1", "--canon", self.root,
            "--now", "2026-11-19T18:00:00Z", "--json")
        self.assertEqual(code, 1, err)
        document = json.loads(out)
        self.assertFalse(document["ok"])
        self.assertIsNone(document["responder"])
        self.assertEqual(document["reason"], "no-responder")
        self.assertEqual(document["wake"]["plan"], "no-responder")
        self.assertEqual(document["wake"]["status"], "simulated")

    def test_an_unknown_application_is_refused_with_a_code(self):
        code, out, _err = self.run_cli(
            "nope", "--severity", "P1", "--canon", self.root, "--json")
        self.assertEqual(code, 1)
        document = json.loads(out)
        self.assertFalse(document["ok"])
        self.assertEqual(document["error"]["code"], "application-not-found")

    def test_a_severity_outside_the_chain_is_refused(self):
        _application(self.root, "p1-only", _incident(
            "severities: [P1]", "responders:", "  - member: human:alice"))
        code, out, _err = self.run_cli(
            "p1-only", "--severity", "P2", "--canon", self.root, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "severity-not-covered")


class ContractConsumerTest(unittest.TestCase):
    """The public contract, tested from the consumer side too."""

    CONSUMER = os.path.join(REPO, "examples", "fixtures", "responder-consumer",
                            "consume.py")

    def consume(self, document):
        import subprocess
        import sys
        return subprocess.run(
            [sys.executable, self.CONSUMER], input=json.dumps(document),
            capture_output=True, text=True, timeout=30)

    def test_the_consumer_accepts_a_chosen_responder(self):
        application = SimpleNamespace(id="acme", title="Acme")
        resolution = responder.Resolution(
            application=application, severity="P1",
            responder=SimpleNamespace(title="alice"), reason="attached",
            signals=("attach",))
        document = responder.document(
            resolution, responder.Wake(plan="none", status="not-needed"),
            journal_id=1)
        proc = self.consume(document)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("human:alice", proc.stdout)

    def test_the_consumer_refuses_a_document_with_nobody(self):
        application = SimpleNamespace(id="acme", title="Acme")
        resolution = responder.Resolution(
            application=application, severity="P2", reason="no-responder",
            consulted=("timezone",))
        document = responder.document(
            resolution, responder.Wake(plan="none", status="not-needed"),
            journal_id=2)
        self.assertFalse(document["ok"])
        proc = self.consume(document)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refused", proc.stderr)

    def test_producer_documents_match_the_published_schema(self):
        try:
            import jsonschema
        except ImportError:  # pragma: no cover - optional in a minimal venv
            self.skipTest("jsonschema not installed")
        path = os.path.join(REPO, "docs", "schemas",
                            "ameesh-responder-1.schema.json")
        with open(path, "r", encoding="utf-8") as handle:
            schema = json.load(handle)
        application = SimpleNamespace(id="acme", title="Acme")
        chosen = responder.document(
            responder.Resolution(
                application=application, severity="P1",
                responder=SimpleNamespace(title="alice"), reason="attached",
                signals=("attach",)),
            responder.Wake(plan="none", status="not-needed"), journal_id=1)
        jsonschema.validate(chosen, schema)
        nobody = responder.document(
            responder.Resolution(application=application, severity="P1",
                                 reason="no-responder", consulted=("timezone",)),
            responder.Wake(plan="no-responder", status="disabled", reason="flag-off"),
            journal_id=2)
        jsonschema.validate(nobody, schema)
        simulated = responder.document(
            responder.Resolution(application=application, severity="P1",
                                 reason="no-responder", consulted=("timezone",)),
            responder.Wake(plan="no-responder", status="simulated"))
        jsonschema.validate(simulated, schema)
        jsonschema.validate({"schema": responder.SCHEMA, "ok": False,
                             "error": {"code": "application-not-found",
                                       "message": "nope"}}, schema)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

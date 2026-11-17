# SPDX-License-Identifier: AGPL-3.0-only
"""#7661 : un rôle de gouvernance se lit sur sa fiche, jamais sur son nom.

Témoins : une fiche qui déclare `roles: [conception]` ou `[auditeur]` porte la
passe, quel que soit le titre de la persona ; une persona nommée `auditeur`
sans ce rôle ne l'est pas ; sans canon chargé, la projection `roles` du
registre (écrite par `canon sync`) est la déclaration ; un mesh sans ces rôles
démarre sans erreur et n'installe rien. Ce test complète la garde d'inventaire
de la tranche 1 (#7660) : il vérifie le comportement, pas la liste des noms.
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, auditor, role_timers, sous_utilisation as su


def card(title, **data):
    return SimpleNamespace(title=title, fiche=SimpleNamespace(data=data))


def canon(*agents):
    return SimpleNamespace(
        agents=list(agents), readable=True,
        owns=lambda _owner: True,
        agent=lambda name: next((a for a in agents if a.title == name), None))


def row(name, roles=None, **extra):
    value = {"name": name, "releve": "election", "host": "agents-a",
             "candidats": ["agents-a"]}
    if roles is not None:
        value["roles"] = list(roles)
    value.update(extra)
    return value


class CardRolesTest(unittest.TestCase):
    def test_the_card_declares_the_role_not_the_title(self):
        loaded = canon(card("veille", roles=["auditeur"]),
                       card("auditeur", roles=["read"]))
        self.assertEqual(su.role_holders([loaded], alert_briefs.AUDITOR_ROLES),
                         ["veille"])
        self.assertNotIn("auditeur",
                         su.role_holders([loaded], alert_briefs.AUDITOR_ROLES))

    def test_singular_role_and_synonyms(self):
        loaded = canon(card("veille", role="Auditor"),
                       card("conception", roles=["orchestrator"]))
        self.assertEqual(su.role_holders([loaded], alert_briefs.AUDITOR_ROLES),
                         ["veille"])
        self.assertEqual(su.role_holders([loaded], ("orchestrateur",)),
                         ["conception"])

    def test_registry_projection_is_read_without_a_canon(self):
        self.assertEqual(su.row_roles({"roles": ["Auditeur"]}), ["auditeur"])
        self.assertEqual(su.row_roles({"role": "conception"}), ["conception"])
        self.assertEqual(su.row_roles({"roles": "auditeur"}), ["auditeur"])
        self.assertEqual(su.row_roles({"roles": 42}), [])
        self.assertTrue(su.row_has_role({"roles": ["auditeur"]},
                                        alert_briefs.AUDITOR_ROLES))
        self.assertFalse(su.row_has_role({"name": "auditeur"}, ()))

    def test_declared_holders_fall_back_to_the_registry(self):
        listing = [row("veille", ["auditeur"]), row("auditeur", [])]
        self.assertEqual(su.declared_holders([], listing,
                                             alert_briefs.AUDITOR_ROLES),
                         ["veille"])
        loaded = canon(card("veille", roles=["auditeur"]))
        self.assertEqual(su.declared_holders([loaded], listing,
                                             alert_briefs.AUDITOR_ROLES),
                         ["veille"])


class AuditorsTest(unittest.TestCase):
    def test_auditors_ignore_the_persona_name(self):
        listing = [row("veille"), row("auditeur")]
        loaded = canon(card("veille", roles=["auditeur"]))
        self.assertEqual(alert_briefs.auditors(listing, [loaded]), ["veille"])
        self.assertEqual(alert_briefs.auditors(listing, [loaded],
                                               explicit_only=True), ["veille"])

    def test_auditors_read_the_registry_without_a_canon(self):
        listing = [row("veille", ["auditeur"]), row("auditeur")]
        self.assertEqual(alert_briefs.auditors(listing), ["veille"])

    def test_auditor_permission_follows_the_declared_role(self):
        loaded = canon(card("veille", roles=["auditeur"]))
        self.assertTrue(auditor.is_auditor(None, "veille", [loaded],
                                           agent=row("veille")))
        self.assertFalse(auditor.is_auditor(None, "auditeur", [loaded]))
        # Sans canon, la projection `roles` du registre est la déclaration.
        self.assertTrue(auditor.is_auditor(
            None, "veille", [], agent=row("veille", ["auditeur"])))
        self.assertFalse(auditor.is_auditor(
            None, "auditeur", [], agent=row("auditeur")))

    def test_bare_profile_has_no_role_critical_agent(self):
        self.assertEqual(su.critical_verdict({"name": "auditeur"}, [],
                                             configured=False),
                         (False, False))


class RoleTimersTest(unittest.TestCase):
    def settings(self):
        return SimpleNamespace(projects={"ameesh": "manaty/ameesh"},
                               interval=900)

    def plan(self, row_value, **extra):
        return role_timers.plan(
            host="agents-a", role=str(row_value["name"]), row=row_value,
            settings=self.settings(), watch_intervals=[900],
            notify_humans=["human:owner"], paquet=("", ""), **extra)

    def test_the_design_role_carries_the_observation_passes(self):
        report = self.plan(row("veille", ["conception"]))
        names = [item["name"] for item in report["timers"]]
        self.assertTrue(report["observation"])
        self.assertFalse(report["auditor"])
        self.assertIn(role_timers.PAQUET_PASS, names)
        self.assertIn(role_timers.CARTE_PASS, names)

    def test_the_auditor_role_carries_only_its_short_pass(self):
        report = self.plan(row("veille", ["auditeur"]))
        self.assertTrue(report["auditor"])
        self.assertFalse(report["observation"])
        self.assertEqual([item["name"] for item in report["timers"]],
                         ["auditeur-court"])

    def test_a_bare_profile_installs_nothing_special(self):
        report = self.plan(row("dev", []))
        names = [item["name"] for item in report["timers"]]
        self.assertFalse(report["observation"])
        self.assertFalse(report["auditor"])
        self.assertNotIn(role_timers.PAQUET_PASS, names)
        self.assertNotIn(role_timers.CARTE_PASS, names)
        self.assertNotIn("auditeur-court", names)

    def test_resolution_reads_the_card_and_refuses_ambiguity(self):
        rows = [row("veille", ["conception"]), row("dev", [])]
        with mock.patch.object(role_timers.registry, "overview",
                               return_value=rows), \
                mock.patch.object(role_timers.registry, "get",
                                  return_value=None), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(role_timers._resolve_role(object(), None),
                             ("veille", 0))
            self.assertEqual(role_timers._resolve_role(object(), "auditeur"),
                             (None, 0))
            self.assertEqual(role_timers._resolve_role(object(), "inconnu"),
                             (None, 2))

    def test_two_declared_holders_require_an_explicit_name(self):
        rows = [row("veille", ["conception"]), row("conception", ["conception"])]
        with mock.patch.object(role_timers.registry, "overview",
                               return_value=rows), \
                mock.patch.object(role_timers.registry, "get",
                                  return_value=None), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(role_timers._resolve_role(object(), None),
                             (None, 2))

    def test_bare_profile_main_exits_zero_without_a_role(self):
        db = mock.Mock()
        cfg = SimpleNamespace(host="agents-a")
        with mock.patch.object(role_timers.config_mod, "load", return_value=cfg), \
                mock.patch.object(role_timers.db_mod, "connect", return_value=db), \
                mock.patch.object(role_timers.registry, "overview", return_value=[]), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(role_timers.main(["--check", "--no-apply"]), 0)
        db.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

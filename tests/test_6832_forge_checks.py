# SPDX-License-Identifier: AGPL-3.0-only
"""#6832 : la déclaration des checks requis survit à une relève d'hôte.

Le 11/11, le pilote a échoué d'AG2 vers AG1 et `ameesh work merge-pr` a refusé
Nexlink : la protection de `develop` répond 403 de **plan** GitHub (dépôt privé
hors Pro) et la déclaration `radar.projects.nexlink.required_checks` vide
n'existait que dans la configuration d'AG2 (#5152, #211196). La déclaration est
désormais portée par le **canon du mesh**
(`extensions.ameesh.forge_checks` de `federation.yaml`), lue au commit exact
du canon appliqué : elle suit une relève avec le déploiement staging, sans
geste d'hôte, et le paquet ameesh ne nomme aucun client.

Témoins : 403 de plan + déclaration versionnée vide → la garde passe (aucun
check exigé par la forge) ; sans déclaration → refus inchangé ; 403 de droits
→ refus inchangé ; un hôte candidat d'un rôle élu sans déclaration pour un
projet dont la forge prouve le 403 de plan → doctor KO qui nomme le projet et
la cible ; déclaration présente ou protection lisible → doctor vert.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import canon as canon_mod
from ameesh import fire, forge_checks, land, role_standings_pg
from ameesh.modules.dev import merge_pr

PLAN = ("gh: Upgrade to GitHub Pro or make this repository public to enable this "
        "feature. (HTTP 403)")
RIGHTS = "gh: Resource not accessible by integration (HTTP 403)"
COMMIT = "a" * 40


def manifest(projects: str = "", *, schema: str = forge_checks.SCHEMA,
             extra: str = "") -> str:
    """Un `federation.yaml` de test avec un bloc `forge_checks`.

    `projects` est le corps YAML de `projects:` (indenté de six espaces) ;
    vide, le bloc déclare un mapping vide."""
    return ("extensions:\n"
            "  ameesh:\n"
            "    forge_checks:\n"
            "      schema: %s\n"
            "      projects:%s\n%s" % (schema, projects or " {}", extra))


#: un projet générique : aucun nom de client dans le code d'ameesh (§7)
WIDGETS = """
        widgets:
          github: acme/widgets
          required_checks: []
          why: private repository outside GitHub Pro
"""


def _fake_canon(text: str, commit: str = COMMIT):
    return types.SimpleNamespace(
        readable=True, id="acme",
        sources=[types.SimpleNamespace(mode="git", repo="/srv/canon",
                                       commit=commit, prefix="")])


def use_canon(text: str, commit: str = COMMIT):
    """Patche la source canon : le lecteur lit ce manifeste à ce commit."""
    return (
        mock.patch.object(forge_checks, "_configured_canons",
                          return_value=[_fake_canon(text, commit)]),
        mock.patch.object(canon_mod, "git_read", return_value=text.encode("utf-8")),
    )


class DetailGh:
    """Un `gh` dont toutes les lectures API rendent le même détail."""

    def __init__(self, detail: str):
        self.detail = detail
        self.calls: list = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return types.SimpleNamespace(returncode=1, stdout="", stderr=self.detail)


class ReadableGh:
    """Une protection lisible et des rulesets vides : aucune déclaration requise."""

    def __init__(self):
        self.calls: list = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        path = argv[1] if len(argv) > 1 else ""
        if path.endswith("/protection"):
            body = {"required_status_checks": {"contexts": ["tests"], "checks": []}}
        else:
            body = []
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")


class WidgetsPlanGh(ReadableGh):
    """Le projet widgets prouve le 403 de plan, le reste se lit normalement."""

    def __call__(self, argv, **kwargs):
        path = argv[1] if len(argv) > 1 else ""
        if "acme/widgets" in path:
            self.calls.append(list(argv))
            return types.SimpleNamespace(returncode=1, stdout="", stderr=PLAN)
        return super().__call__(argv, **kwargs)


class CanonTestCase(unittest.TestCase):
    def setUp(self):
        forge_checks.clear_cache()
        self.addCleanup(forge_checks.clear_cache)


class CanonSourceTest(CanonTestCase):
    """Le lecteur : le canon appliqué, au commit exact, sans valeur de client."""

    def test_the_canon_declares_widgets_empty(self):
        patches = use_canon(manifest(WIDGETS))
        with patches[0], patches[1]:
            # `()` est une déclaration, pas une absence : couvre le 403 de plan.
            self.assertEqual(forge_checks.declared("acme/widgets", object()), ())
            self.assertEqual(
                forge_checks.declared("https://github.com/ACME/Widgets.git", object()), ())
            self.assertIsNone(forge_checks.declared("acme/other", object()))
            self.assertIsNone(forge_checks.declared("", object()))

    def test_a_canon_without_the_block_declares_nothing(self):
        text = "federation: '0.1'\nid: acme\n"
        patches = use_canon(text)
        with patches[0], patches[1]:
            evidence = forge_checks.configured(object())
        self.assertIsNone(evidence.declarations)
        self.assertIn("no extensions.ameesh.forge_checks", evidence.reason)

    def test_no_configured_canon_declares_nothing(self):
        forge_checks.clear_cache()
        self.assertIsNone(forge_checks.declared("acme/widgets", object()))
        self.assertIn("no readable canon",
                      forge_checks.configured(object()).reason)

    def test_the_package_ships_no_client_declaration(self):
        beside = os.path.join(os.path.dirname(forge_checks.__file__),
                              "forge_checks.json")
        self.assertFalse(os.path.exists(beside))

    def test_the_manifest_is_read_at_the_exact_canon_commit(self):
        with tempfile.TemporaryDirectory() as repo:
            def run(*args):
                return subprocess.run(["git", "-C", repo, *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            run("init", "-q")
            run("config", "user.email", "tests@ameesh.invalid")
            run("config", "user.name", "tests")
            with open(os.path.join(repo, "federation.yaml"), "w", encoding="utf-8") as fh:
                fh.write(manifest(WIDGETS))
            run("add", "federation.yaml")
            run("commit", "-q", "-m", "canon")
            first = run("rev-parse", "HEAD")
            # un second commit retire la déclaration : le lecteur du premier
            # commit doit continuer à la voir (contenu immuable).
            with open(os.path.join(repo, "federation.yaml"), "w", encoding="utf-8") as fh:
                fh.write("federation: '0.1'\nid: acme\n")
            run("commit", "-qam", "remove")
            fake = types.SimpleNamespace(
                readable=True, id="acme",
                sources=[types.SimpleNamespace(mode="git", repo=repo, commit=first,
                                               prefix="")])
            with mock.patch.object(forge_checks, "_configured_canons", return_value=[fake]):
                self.assertEqual(forge_checks.declared("acme/widgets", object()), ())

    def test_a_present_block_is_strict(self):
        cases = {
            "unknown top-level key": manifest(WIDGETS, extra="      extra: 1\n"),
            "bad schema": manifest(WIDGETS, schema="ameesh-forge-checks/2"),
            "unknown project key": manifest(WIDGETS.replace(
                "          why:", "          extra: 1\n          why:")),
            "bad repository": manifest(WIDGETS.replace("acme/widgets", "not-a-repo")),
            "checks not a list": manifest(WIDGETS.replace(
                "required_checks: []", "required_checks: tests")),
            "empty check name": manifest(WIDGETS.replace(
                "required_checks: []", "required_checks: [' ']")),
            "duplicate check name": manifest(WIDGETS.replace(
                "required_checks: []", "required_checks: [tests, tests]")),
            "duplicate repository": manifest(WIDGETS + """
        other:
          github: ACME/WIDGETS
          required_checks: []
"""),
            "why not text": manifest(WIDGETS.replace("why: private repository outside GitHub Pro", "why: 1")),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                forge_checks.clear_cache()
                patches = use_canon(text)
                with patches[0], patches[1]:
                    with self.assertRaises(forge_checks.InventoryError):
                        forge_checks.configured(object())

    def test_an_unreadable_canon_block_refuses_the_guard(self):
        text = "extensions: [not, a, mapping]\n"
        patches = use_canon(text)
        with patches[0], patches[1]:
            with self.assertRaises(forge_checks.InventoryError):
                forge_checks.configured(object())
            with self.assertRaises(land.LandError):
                fire.versioned_declaration("acme/widgets", object())

    def test_a_canon_without_an_exact_commit_declares_nothing(self):
        fake = types.SimpleNamespace(
            readable=True, id="acme",
            sources=[types.SimpleNamespace(mode="untrusted", repo="/srv/canon",
                                           commit="", prefix="")])
        with mock.patch.object(forge_checks, "_configured_canons", return_value=[fake]):
            evidence = forge_checks.configured(object())
        self.assertIsNone(evidence.declarations)
        self.assertIn("no exact canon commit", evidence.reason)


class RequiredChecksTest(CanonTestCase):
    """La garde de fusion : la déclaration versionnée s'ajoute, jamais ne retire."""

    def test_a_versioned_empty_declaration_covers_a_plan_403(self):
        patches = use_canon(manifest(WIDGETS))
        with patches[0], patches[1]:
            self.assertEqual(
                fire.required_checks("acme/widgets", "develop", None,
                                     gh=DetailGh(PLAN), cfg=object()), ())

    def test_without_a_declaration_the_plan_403_still_refuses(self):
        patches = use_canon(manifest({}))
        with patches[0], patches[1]:
            with self.assertRaises(land.LandError):
                fire.required_checks("acme/widgets", "develop", None,
                                     gh=DetailGh(PLAN), cfg=object())

    def test_without_cfg_the_guard_stays_forge_only(self):
        with self.assertRaises(land.LandError):
            fire.required_checks("acme/widgets", "develop", None, gh=DetailGh(PLAN))

    def test_a_rights_403_is_not_covered_by_an_empty_declaration(self):
        patches = use_canon(manifest(WIDGETS))
        with patches[0], patches[1]:
            with self.assertRaises(land.LandError):
                fire.required_checks("acme/widgets", "develop", None,
                                     gh=DetailGh(RIGHTS), cfg=object())

    def test_a_versioned_declaration_adds_its_names_to_a_readable_protection(self):
        text = manifest(WIDGETS.replace("required_checks: []",
                                        "required_checks: [coverage]"))
        patches = use_canon(text)
        with patches[0], patches[1]:
            self.assertEqual(
                fire.required_checks("acme/widgets", "main", None, gh=ReadableGh(),
                                     cfg=object()),
                (("tests", None), ("coverage", None)))

    def test_the_project_declaration_still_applies_without_a_canon(self):
        project = types.SimpleNamespace(required_checks=("coverage",),
                                        required_checks_declared=True)
        self.assertEqual(
            fire.required_checks("acme/widgets", "main", project, gh=ReadableGh()),
            (("tests", None), ("coverage", None)))


class MergePrGuardTest(CanonTestCase):
    """Le geste du constat : `work merge-pr` sur l'hôte sans radar."""

    def test_the_plan_403_passes_with_the_versioned_declaration(self):
        patches = use_canon(manifest(WIDGETS))
        with patches[0], patches[1]:
            self.assertEqual(
                merge_pr._required_refusal("acme/widgets", "develop", None, [],
                                           gh=DetailGh(PLAN), cfg=object()), "")

    def test_a_rights_403_still_refuses(self):
        patches = use_canon(manifest(WIDGETS))
        with patches[0], patches[1]:
            refusal = merge_pr._required_refusal("acme/widgets", "develop", None, [],
                                                 gh=DetailGh(RIGHTS), cfg=object())
        self.assertIn("cannot be proven", refusal)
        self.assertIn("Resource not accessible", refusal)

    def test_an_undeclared_plan_403_still_refuses(self):
        patches = use_canon(manifest(WIDGETS))
        with patches[0], patches[1]:
            refusal = merge_pr._required_refusal("acme/other", "main", None, [],
                                                 gh=DetailGh(PLAN), cfg=object())
        self.assertIn("cannot be proven", refusal)


def _cfg(**radar_projects):
    return types.SimpleNamespace(
        host="ameesh-agents-1",
        github={"projects": {"widgets": {"repo": "acme/widgets",
                                          "base": ["develop"]},
                             "ameesh": "acme/ameesh"}},
        release={},
        radar={"projects": radar_projects})


class DoctorTest(CanonTestCase):
    """Le doctor : rouge sur un hôte candidat sans déclaration, pas ailleurs."""

    def _doctor(self, gh, *, cfg=None, text=None, roles=(("orchestrateur",),)):
        rows = [{"name": name} for (name,) in roles]
        text = manifest(WIDGETS) if text is None else text
        patches = use_canon(text)
        with patches[0], patches[1], \
                mock.patch.object(role_standings_pg, "elected_roles", return_value=rows):
            return forge_checks.doctor_lines(cfg or _cfg(), object(), gh=gh)

    def test_a_candidate_host_without_the_declaration_is_red(self):
        ko, lines = self._doctor(DetailGh(PLAN), text=manifest({}))
        self.assertTrue(ko)
        named = "\n".join(lines)
        self.assertIn("KO — widgets", named)
        self.assertIn("acme/widgets", named)
        self.assertIn("develop", named)
        self.assertIn("extensions.ameesh.forge_checks", named)

    def test_the_versioned_declaration_makes_the_candidate_host_green(self):
        ko, lines = self._doctor(WidgetsPlanGh())
        self.assertFalse(ko)
        self.assertIn("widgets (acme/widgets, develop): versioned declaration installed",
                      "\n".join(lines))

    def test_a_readable_protection_needs_no_declaration(self):
        ko, lines = self._doctor(ReadableGh(), text=manifest({}))
        self.assertFalse(ko)
        self.assertIn("protection readable", "\n".join(lines))

    def test_a_rights_403_is_not_conclusive_not_red(self):
        ko, lines = self._doctor(DetailGh(RIGHTS), text=manifest({}))
        self.assertFalse(ko)
        self.assertIn("rights 403", "\n".join(lines))

    def test_a_host_that_is_not_a_candidate_is_not_checked(self):
        ko, lines = self._doctor(DetailGh(PLAN), roles=())
        self.assertFalse(ko)
        self.assertIn("not a candidate host", "\n".join(lines))

    def test_a_host_only_declaration_is_a_warning_not_a_red(self):
        cfg = _cfg(widgets={"repo": "/home/ameesh/work/widgets",
                            "github": "acme/widgets", "target": "develop",
                            "required_checks": []})
        ko, lines = self._doctor(WidgetsPlanGh(), cfg=cfg, text=manifest({}))
        self.assertFalse(ko)
        self.assertIn("host-only radar declaration", "\n".join(lines))

    def test_an_unreadable_block_is_red(self):
        ko, lines = self._doctor(DetailGh(PLAN),
                                 text="extensions: [not, a, mapping]\n")
        self.assertTrue(ko)
        self.assertIn("declaration is unreadable", "\n".join(lines))

    def test_no_readable_canon_is_red(self):
        rows = [{"name": "orchestrateur"}]
        forge_checks.clear_cache()
        with mock.patch.object(role_standings_pg, "elected_roles", return_value=rows):
            ko, lines = forge_checks.doctor_lines(_cfg(), object(), gh=DetailGh(PLAN))
        self.assertTrue(ko)
        self.assertIn("canon declarations cannot be proven", "\n".join(lines))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

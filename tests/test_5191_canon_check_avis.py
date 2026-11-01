# SPDX-License-Identifier: AGPL-3.0-only
"""#5191 — `canon check` refuse un fichier non léger sans nombre d'avis.

Témoins (forme des canons 3b698a5 et 5fc7020, #5188) : une classe sensible sans
règle `train_reviews` rend une erreur qui nomme le chemin et la classe ; la
règle ajoutée, plus d'erreur. Une règle sans fichier avertit, un dépôt absent
n'est pas mesuré, et `canon.validate` (lu par `canon sync`) ne change pas.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import canon as canon_mod
from ameesh import canon_cli, canon_review_coverage as coverage

from .test_3554_canon_lowering import ENV, MECHANICS

RUNNER_SANDBOX = "src/ameesh/runner_sandbox.py"


def _federation(*, with_count=True, stale_rule=False, project_rules=True):
    classes = ("          - paths:\n              - \"%s\"\n"
               "            class: sensitive\n" % RUNNER_SANDBOX)
    if stale_rule:
        classes += ("          - paths:\n              - \"gone/**\"\n"
                    "            class: sensitive\n")
    counts = ("          - paths:\n              - \"%s\"\n"
              "            reviews: 2\n" % RUNNER_SANDBOX) if with_count else ""
    if stale_rule:
        counts += "          - paths:\n              - \"gone/**\"\n            reviews: 1\n"
    train = ("      ameesh:\n        rules:\n" + counts) if counts else \
        "      ameesh:\n        rules: []\n"
    return ("federation: \"0.1\"\nid: manaty\nreview_policies:\n"
            "  risk_classes:\n    default: normal\n    projects:\n"
            "      ameesh:\n        default: light\n        rules:\n" + classes
            + ("      omarchy:\n        default: light\n" if project_rules else "")
            + "  train_reviews:\n    schema: ameesh-train-reviews-policy/1\n"
            "    projects:\n" + train)


class CoverageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.canon_root = os.path.join(tmp.name, "canon")
        self.project = os.path.join(tmp.name, "project")
        self._repo(self.project, {RUNNER_SANDBOX: "x\n", "README.md": "x\n"})

    def _git(self, root, *args):
        proc = subprocess.run(["git", "-C", root, *args], capture_output=True,
                              text=True, env=ENV, check=True)
        return proc.stdout.strip()

    def _repo(self, root, files):
        os.makedirs(root, exist_ok=True)
        if not os.path.isdir(os.path.join(root, ".git")):
            self._git(root, "init", "-q", "-b", "main")
        for path, text in files.items():
            full = os.path.join(root, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(text)
        self._git(root, "add", "-A")
        self._git(root, "commit", "-qm", "c")
        return self._git(root, "rev-parse", "HEAD")

    def _canon(self, **options):
        commit = self._repo(self.canon_root, {
            "federation.yaml": _federation(**options),
            "processus/mecanique.yaml": MECHANICS,
        })
        return mock.Mock(sources=[canon_mod.Source(
            member="home", directory="", repo=self.canon_root, rev=commit,
            commit=commit, prefix="", mode="git")])

    def _codes(self, found, severity):
        return [f.code for f in found if f.severity == severity]

    def test_sensitive_path_without_count_is_an_error_naming_path_and_class(self):
        found = coverage.findings(None, self._canon(with_count=False),
                                  {"ameesh": self.project})
        errors = [f for f in found if f.severity == canon_mod.ERROR]
        self.assertEqual([coverage.CODE_MISSING], [f.code for f in errors])
        self.assertIn(RUNNER_SANDBOX, errors[0].message)
        self.assertIn("class sensitive", errors[0].message)

    def test_count_declared_leaves_no_error(self):
        found = coverage.findings(None, self._canon(), {"ameesh": self.project})
        self.assertEqual([], self._codes(found, canon_mod.ERROR))
        self.assertNotIn(coverage.CODE_NO_FILE, self._codes(found, canon_mod.WARNING))

    def test_rule_matching_no_file_is_a_warning(self):
        found = coverage.findings(None, self._canon(stale_rule=True),
                                  {"ameesh": self.project})
        self.assertEqual([], self._codes(found, canon_mod.ERROR))
        warned = [f for f in found if f.code == coverage.CODE_NO_FILE]
        self.assertEqual(canon_mod.WARNING, warned[0].severity)
        self.assertIn("gone/**", warned[0].message)

    def test_incomplete_project_policy_warns_instead_of_failing(self):
        found = coverage.findings(None, self._canon(), {"ameesh": self.project})
        self.assertTrue(any(f.severity == canon_mod.WARNING and "omarchy" in f.message
                            for f in found))

    def test_project_without_repository_is_not_measured(self):
        with mock.patch("os.getcwd", return_value=self.canon_root):
            found = coverage.findings(mock.Mock(radar={}, github={}),
                                      self._canon(with_count=False, project_rules=False))
        self.assertEqual([coverage.CODE_UNMEASURED], [f.code for f in found])
        self.assertEqual(canon_mod.INFO, found[0].severity)

    def test_current_clone_of_the_project_is_found_from_its_slug(self):
        self._git(self.project, "remote", "add", "origin",
                  "https://github.com/manaty/ameesh.git")
        cfg = mock.Mock(radar={}, github={"projects": {"ameesh": "manaty/ameesh"}})
        with mock.patch("os.getcwd", return_value=self.project):
            found = coverage.findings(cfg, self._canon(with_count=False, project_rules=False))
        self.assertEqual([coverage.CODE_MISSING], [f.code for f in found])

    def test_explicit_repository_never_passes_unmeasured(self):
        canon = self._canon(project_rules=False)
        for repos in ({"ameesh": os.path.join(self.project, "nope")},
                      {"inconnu": self.project}):
            with self.subTest(repos=repos), mock.patch(
                    "os.getcwd", return_value=self.canon_root):
                found = coverage.findings(mock.Mock(radar={}, github={}), canon, repos)
                self.assertIn(coverage.CODE_UNMEASURED,
                              self._codes(found, canon_mod.ERROR))

    def test_canon_without_mechanics_has_no_finding(self):
        commit = self._repo(self.canon_root, {"federation.yaml": "federation: \"0.1\"\n"})
        canon = mock.Mock(sources=[canon_mod.Source(
            member="home", directory="", repo=self.canon_root, rev=commit,
            commit=commit, prefix="", mode="git")])
        self.assertEqual([], coverage.findings(None, canon))


class CliTest(unittest.TestCase):
    """Le check sort 1 sans changer l'état d'hôte que lit `canon sync`."""

    def test_error_fails_the_check_but_not_the_host_state(self):
        finding = canon_mod.Finding(coverage.CODE_MISSING, canon_mod.ERROR, "m")
        canon = mock.Mock(readable=True, root="/c", untrusted=False, sources=[],
                          id="c", is_default=True)
        args = argparse.Namespace(host="h", json=True, project_repo=[("ameesh", "/p")])
        out = io.StringIO()
        with mock.patch.object(canon_cli.canon_mod, "validate", return_value=[]), \
                mock.patch.object(canon_cli, "_recorded", return_value=None), \
                mock.patch.object(canon_cli.placement_mod, "verdicts", return_value=[]), \
                mock.patch.object(canon_cli.canon_sync, "assess",
                                  return_value=("ok", "")) as assess, \
                mock.patch.object(coverage, "findings", return_value=[finding]) as seen, \
                contextlib.redirect_stdout(out):
            code = canon_cli._check_one(mock.Mock(host="h"), args, canon)
        self.assertEqual(1, code)
        self.assertEqual({"ameesh": "/p"}, seen.call_args.args[2])
        self.assertEqual([], assess.call_args.args[2])
        data = json.loads(out.getvalue())
        self.assertEqual("ok", data["canon_status"])
        self.assertEqual(1, data["errors"])

    def test_project_repo_option_requires_name_and_path(self):
        self.assertEqual(("a", "/p"), canon_cli._project_repo("a=/p"))
        for text in ("a", "=/p", "a="):
            with self.assertRaises(argparse.ArgumentTypeError):
                canon_cli._project_repo(text)

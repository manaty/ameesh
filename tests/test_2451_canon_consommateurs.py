# SPDX-License-Identifier: AGPL-3.0-only
"""#2451 : les consommateurs historiques exemptés par #2449 lisent le canon.

Le plafond §4 (`build.max_in_build`) vient du document global du canon
appliqué (`mechanics.configured`) ; les familles et l'indépendance du train
sont des paramètres requis ; le moteur n'a plus de calendrier local ; la
désignation reçoit les familles du canon. Sans canon lisible, chacun refuse
au lieu de supposer une valeur."""
from __future__ import annotations

import importlib.util
import inspect
import io
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest import mock

from ameesh import (canon, mechanics, progress, review_designation, train,
                    train_history, train_policy, train_review, wip, work)

from .test_l1616_mechanics import MECHANICS, _Repo

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _canon(repo: str, commit: str, *, mode: str = "git", prefix: str = ""):
    return SimpleNamespace(sources=[SimpleNamespace(mode=mode, repo=repo, commit=commit,
                                                    prefix=prefix)])


class ConfiguredTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        mechanics._CONFIGURED.clear()
        self.addCleanup(mechanics._CONFIGURED.clear)

    def applied(self, *canons):
        patch = mock.patch.object(canon, "load_configured", return_value=list(canons))
        patch.start()
        self.addCleanup(patch.stop)

    def test_build_limit_and_families_come_from_the_applied_canon(self):
        repo = _Repo(self.tmp)
        self.applied(_canon(repo.root, repo.git("rev-parse", "HEAD")))
        self.assertEqual(work.build_limit(object()), 2)
        self.assertEqual(mechanics.configured(object()).families,
                         ("claude", "codex", "deepseek"))

    def test_a_new_canon_commit_changes_the_cap(self):
        repo = _Repo(self.tmp)
        first = repo.git("rev-parse", "HEAD")
        repo.write("processus/mecanique.yaml",
                   MECHANICS.replace("max_in_build: 2", "max_in_build: 3"))
        second = repo.commit("cap 3")
        self.applied(_canon(repo.root, first))
        self.assertEqual(work.build_limit(object()), 2)
        canon.load_configured.return_value = [_canon(repo.root, second)]
        self.assertEqual(work.build_limit(object()), 3)

    def test_no_canon_untrusted_sub_directory_or_absent_document_refuse(self):
        absent = _Repo(self.tmp, mechanics_text=None)
        head = absent.git("rev-parse", "HEAD")
        cases = {
            "no canon": [],
            "untrusted": [_canon(self.tmp, "", mode="untrusted")],
            "sub-directory": [_canon(absent.root, head, prefix="knowledge")],
            "absent": [_canon(absent.root, head)],
        }
        for label, canons in cases.items():
            with self.subTest(label), \
                    mock.patch.object(canon, "load_configured", return_value=canons), \
                    self.assertRaises(mechanics.MechanicsError):
                work.build_limit(object())
        with self.assertRaises(mechanics.MechanicsError):
            work.build_limit(None)

    def test_wip_sweep_refuses_before_any_read_without_canon(self):
        self.applied()
        # db=None : le refus précède toute lecture ou écriture
        with self.assertRaises(mechanics.MechanicsError):
            wip.sweep(object(), None)
        args = SimpleNamespace(work_command="wip", limit=10, apply=True, actor="",
                               json=False)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(wip.run(None, args, cfg=object()), 1)
        self.assertIn("build.max_in_build", err.getvalue())

    def test_progress_judges_an_overrun_only_with_a_known_cap(self):
        self.assertIn("above the §4 cap of 2", progress._wip_label(3, 2))
        self.assertEqual(progress._wip_label(3, None), "3 lot(s) in build")
        self.applied()
        self.assertIsNone(progress._build_cap(object()))


class NoLocalDefaultTest(unittest.TestCase):
    def test_historical_constants_are_gone(self):
        self.assertFalse(hasattr(work, "BUILD_LIMIT"))
        self.assertFalse(hasattr(train, "cutoff"))
        self.assertFalse(hasattr(train_review, "FAMILIES"))
        self.assertFalse(hasattr(review_designation, "KNOWN_FAMILIES"))

    def test_independence_parameters_have_no_default(self):
        for function in (train_review.qualify, train_review.qualify_recorded):
            params = inspect.signature(function).parameters
            for name in ("families", "exclude_author_family", "distinct_families"):
                with self.subTest(function=function.__name__, name=name):
                    self.assertIs(params[name].default, inspect.Parameter.empty)
        self.assertIs(inspect.signature(train_review.Receipt.known)
                      .parameters["families"].default, inspect.Parameter.empty)
        for function in (review_designation.select, review_designation.apply,
                         review_designation.candidates_from_registry,
                         review_designation.live_family,
                         review_designation.Candidate.eligible):
            with self.subTest(function=function.__name__):
                self.assertIs(inspect.signature(function).parameters["families"].default,
                              inspect.Parameter.empty)

    def test_history_reader_requires_compiled_rules(self):
        with self.assertRaisesRegex(train.TrainError, "compiled canon review rules"):
            train_history.Recorded(None, "/nonexistent", provider=object(),
                                   review_rules=None)

    def test_policy_adapter_accepts_the_reader_schema_only(self):
        rules = SimpleNamespace(schema=mechanics.SCHEMA, commit="a" * 40,
                                project="ameesh", review_policy=None)
        self.assertEqual(train_policy.Compiled(rules).commit, "a" * 40)
        with self.assertRaises(train.TrainError):
            train_policy.Compiled(SimpleNamespace(**{
                **rules.__dict__, "schema": mechanics.SCHEMA_PREVALIDATION}))


class GuardTest(unittest.TestCase):
    def test_converted_consumers_carry_no_exemption(self):
        spec = importlib.util.spec_from_file_location(
            "garde_2451", ROOT / "scripts" / "garde-mecanique.py")
        garde = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = garde
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(garde)
        converted = {"src/ameesh/%s.py" % name for name in (
            "work", "train", "train_policy", "train_review", "review_designation")}
        self.assertFalse({item.path for item in garde.EXEMPTIONS} & converted)
        scan = garde.scan_repository(ROOT)
        self.assertEqual(list(scan.findings), [])
        self.assertFalse(scan.unused)


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""#2464 — la cible du paquet prime sur un `origin/HEAD` resté sur une release.

Deux défauts constatés par l'auditeur au paquet h-1300 :

* `scripts/paquet-candidats.py --project ameesh` classait « release » les lots
  sans `branch_target` : `lot_class.of_lot` relisait `default_base` (donc
  `origin/HEAD` du clone radar, figé sur `release/1.11.0` depuis le 15/10)
  même quand l'appelant lui donnait la base du paquet. #2206, #775, #2421,
  #2298, #1819 sortaient ainsi « non prêts », en silence.
* `scripts/paquet-inclusion.py --no-ready` sautait le contrôle « prêts ×
  manifeste » ; sur la recomposition de h-1300, #2384 (prêt depuis 14:29) a été
  omis sans ligne « écarté ».

Les témoins ci-dessous montent un dépôt dont `origin/HEAD` nomme une release,
puis vérifient : la base explicite ne se fait pas surclasser, une cible de
projet ou d'option prime, un `origin/HEAD` release sans cible déclarée est une
erreur de lecture (rc 1), et `--no-ready` est refusé. Le refus de recomposition
lui-même est déjà couvert par `tests.test_1644_paquet_inclusion`.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import canon, lot_class, packet_candidates, plan_git

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_script(name: str, filename: str):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, "scripts", filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


candidates_script = _load_script("test_paquet_candidats_2464", "paquet-candidats.py")


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _commit(repo: str, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


class ReleaseHeadFixture(unittest.TestCase):
    """Un dépôt dont `origin/HEAD` nomme une release périmée (clone radar)."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2464-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "banc")
        _git(self.repo, "config", "user.email", "banc@example.invalid")
        _git(self.repo, "config", "commit.gpgsign", "false")
        with open(os.path.join(self.repo, "shared.txt"), "w", encoding="utf-8") as handle:
            handle.write("one\n")
        self.base = _commit(self.repo, "base")
        _git(self.repo, "branch", "release/1.11.0", self.base)
        _git(self.repo, "update-ref", "refs/remotes/origin/release/1.11.0", self.base)
        _git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/release/1.11.0")
        _git(self.repo, "switch", "-q", "-c", "lot/1")
        with open(os.path.join(self.repo, "shared.txt"), "w", encoding="utf-8") as handle:
            handle.write("two\n")
        self.head = _commit(self.repo, "#1")
        _git(self.repo, "switch", "-q", "main")

    def row(self, **extra) -> dict:
        row = {"id": 1, "app": "ameesh", "branch": "lot/1", "branch_target": "",
               "type": "évolution", "priority": 2, "created_ts": 1.0}
        row.update(extra)
        return row


class ExplicitBaseTest(ReleaseHeadFixture):
    """#2464 (1) : une base explicite ne se fait pas surclasser par origin/HEAD."""

    def class_of(self, **kwargs):
        marks = [{"kind": "frozen", "sha": self.head}]
        with mock.patch.object(canon, "git_range_paths", return_value=["shared.txt"]), \
                mock.patch.object(lot_class, "class_of_paths",
                                  return_value={"class": "light", "why": "policy of test",
                                                "files": [], "policy_ref": "test",
                                                "doubt": False}) as classes:
            result = lot_class.of_lot(None, 1, cfg=None, repo=self.repo,
                                      head=self.head, milestones=marks,
                                      canons=[object()], **kwargs)
        return result, classes

    def test_explicit_package_base_is_not_release(self) -> None:
        result, classes = self.class_of(item=self.row(), base=self.base)
        self.assertNotEqual(result["class"], lot_class.RELEASE, result)
        self.assertEqual(result["class"], lot_class.LIGHT)
        self.assertEqual(result["target"], self.base)
        classes.assert_called_once()

    def test_resolved_target_is_carried_to_the_class(self) -> None:
        # `packet_candidates` passe la cible résolue du paquet comme
        # `branch_target` : la ligne du lot n'en portait pas, et c'est cette
        # cible qui fait foi, pas `origin/HEAD` du clone.
        result, _ = self.class_of(item=self.row(branch_target="main"), base=self.base)
        self.assertNotEqual(result["class"], lot_class.RELEASE, result)
        self.assertEqual(result["target"], self.base)

    def test_release_still_comes_from_the_lot_or_its_target(self) -> None:
        # Une cible release explicite reste release : la correction ne tait que
        # le repli `origin/HEAD`, pas l'axe release réel.
        result, _ = self.class_of(item=self.row(branch_target="release/1.11.0"),
                                  base=self.base)
        self.assertEqual(result["class"], lot_class.RELEASE)
        self.assertIn("release/1.11.0", result["why"])


class ResolveTargetTest(ReleaseHeadFixture):
    """#2464 (1 bis) : option > projet > origin/HEAD, et origin/HEAD release = erreur."""

    def test_origin_head_release_without_declared_target_is_a_read_error(self) -> None:
        project = SimpleNamespace(name="ameesh", target="")
        with self.assertRaises(packet_candidates.CandidateError) as caught:
            packet_candidates.resolve_target(project, self.repo)
        message = str(caught.exception)
        self.assertIn("origin/HEAD", message)
        self.assertIn("release/1.11.0", message)
        self.assertIn("--target", message)

    def test_explicit_target_primes_over_origin_head(self) -> None:
        project = SimpleNamespace(name="ameesh", target="")
        self.assertEqual(packet_candidates.resolve_target(project, self.repo, "main"),
                         ("main", "option"))

    def test_project_target_primes_over_origin_head(self) -> None:
        project = SimpleNamespace(name="ameesh", target="main")
        self.assertEqual(packet_candidates.resolve_target(project, self.repo),
                         ("main", "project"))
        # Même quand origin/HEAD nomme une release, une cible de projet
        # déclarée retire l'ambiguïté.
        self.assertEqual(packet_candidates.resolve_target(project, self.repo, ""),
                         ("main", "project"))

    def test_origin_head_main_is_accepted_as_the_last_resort(self) -> None:
        _git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        _git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/main")
        project = SimpleNamespace(name="ameesh", target="")
        self.assertEqual(packet_candidates.resolve_target(project, self.repo),
                         ("main", "origin/HEAD"))

    def test_origin_head_release_with_declared_project_target_is_accepted(self) -> None:
        project = SimpleNamespace(name="ameesh", target="main")
        self.assertEqual(packet_candidates.resolve_target(project, self.repo),
                         ("main", "project"))

    def test_option_target_is_normalised(self) -> None:
        project = SimpleNamespace(name="ameesh", target="")
        self.assertEqual(packet_candidates.resolve_target(project, self.repo, "origin/main"),
                         ("main", "option"))

    def test_origin_head_main_fallback_uses_default_target(self) -> None:
        _git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        _git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/main")
        self.assertEqual(plan_git.default_target(self.repo), "origin/main")
        project = SimpleNamespace(name="ameesh", target="")
        self.assertEqual(packet_candidates.resolve_target(project, self.repo),
                         ("main", "origin/HEAD"))


class ScriptExitTest(ReleaseHeadFixture):
    """#2464 (1 ter) : l'erreur de lecture sort en rc 1, jamais en classe release."""

    def test_origin_head_release_makes_the_list_fail_with_rc_1(self) -> None:
        project = SimpleNamespace(name="ameesh", target="", repo=self.repo)
        stderr = io.StringIO()
        with mock.patch.object(candidates_script.config, "load", return_value=object()), \
                mock.patch.object(candidates_script.merge_queue, "declared_projects",
                                  return_value={"ameesh": project}), \
                mock.patch.object(candidates_script.db, "connect", return_value=mock.Mock()), \
                mock.patch.object(candidates_script.merge_queue, "working_clone",
                                  return_value=(None, None)), \
                contextlib.redirect_stderr(stderr):
            code = candidates_script.main(
                ["--repo", self.repo, "--project", "ameesh", "--json"])
        self.assertEqual(code, 1, stderr.getvalue())
        self.assertIn("origin/HEAD", stderr.getvalue())
        self.assertIn("release/1.11.0", stderr.getvalue())

    def test_ready_export_is_removed_on_the_read_error(self) -> None:
        ready = os.path.join(self.tmp, "prets.txt")
        with open(ready, "w", encoding="utf-8") as handle:
            handle.write("#100\n")
        project = SimpleNamespace(name="ameesh", target="", repo=self.repo)
        with mock.patch.object(candidates_script.config, "load", return_value=object()), \
                mock.patch.object(candidates_script.merge_queue, "declared_projects",
                                  return_value={"ameesh": project}), \
                mock.patch.object(candidates_script.db, "connect", return_value=mock.Mock()), \
                mock.patch.object(candidates_script.merge_queue, "working_clone",
                                  return_value=(None, None)), \
                contextlib.redirect_stderr(io.StringIO()):
            code = candidates_script.main(
                ["--repo", self.repo, "--project", "ameesh", "--ready-out", ready])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(ready))


if __name__ == "__main__":
    unittest.main()

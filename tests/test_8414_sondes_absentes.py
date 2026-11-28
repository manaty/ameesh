# SPDX-License-Identifier: AGPL-3.0-only
"""#8414 : la sonde d'un commit absent n'est plus répétée à chaque passe.

Constat (AG1, 28/11, 01:20–01:35Z) : load 51 sur 8 vCPU, PSI CPU 96 % ; des
`git -C ~/work/mty-amh-impl-0X cat-file -e <sha>^{commit}` concurrents, chacun
à 100 % de CPU pendant ~20 s, sur des commits absents du dépôt ameesh — dont
1ecc7dff, tête d'un lot canon livré dans manaty/home. Cause : la passe de
mesure (`runner.measure_once` → `lot_measures.measure_pending/measure` →
`_repo_for`/`measure_one` → `has_commit`) re-sondait chaque dépôt de l'hôte à
chaque passe ; dans le clone partiel d'AG1 (promisor, 16 packs), un objet
absent parcourt tous les packs promisor, alors qu'un présent coûte ~0,01 s.

Témoins :
* N sondes du même absent, y compris depuis deux worktrees d'un même
  common-dir, ne font qu'un `cat-file` ; idem dans un vrai clone partiel ;
* le constat est borné (durée, nombre d'entrées) et expiré il est re-sondé ;
* un fetch réussi l'oublie, donc un commit rapatrié est vu tout de suite ;
* une passe de mesure ne re-sonde plus un lot déjà mesuré dont la fusion est
  absente (le cas 1ecc7dff) ;
* un lot dont la fusion vit dans un autre dépôt que ceux de l'hôte (marque
  `ameesh-freeze-repo`, ou dépôt de l'issue) n'est pas sondé du tout ; le même
  lot est mesuré sur un hôte qui a ce dépôt.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import lot_measures, registry, storage, work

from .support import PgTestCase


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t",
                                   GIT_AUTHOR_EMAIL="t@example.invalid",
                                   GIT_COMMITTER_NAME="t",
                                   GIT_COMMITTER_EMAIL="t@example.invalid"))
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def make_repo(root: str) -> str:
    """A one-commit repository, enough to probe commits in it."""
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "README"), "w", encoding="utf-8") as fh:
        fh.write("base\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def make_partial_clone(root: str) -> str:
    """Un vrai clone partiel (`blob:none`, sans checkout) : des blobs manquent.

    Le clone local `file://` garde le témoin hors réseau ;
    `uploadpack.allowFilter` fait servir un pack filtré par la source."""
    src = os.path.join(root, "source")
    os.makedirs(src)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@example.invalid")
    _git(src, "config", "user.name", "t")
    _git(src, "config", "uploadpack.allowFilter", "true")
    with open(os.path.join(src, "README"), "w", encoding="utf-8") as fh:
        fh.write("base\n")
    _git(src, "add", "README")
    _git(src, "commit", "-q", "-m", "base")
    dst = os.path.join(root, "partial")
    proc = subprocess.run(
        ["git", "clone", "-q", "--filter=blob:none", "--no-local",
         "--no-checkout", "file://" + src, dst], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("clone partiel : %s" % proc.stderr)
    return dst


def missing_sha(tag: int = 1) -> str:
    """A full 40-hex SHA that no repository of the test holds."""
    return "%08x" % (0x1ecc7dff + tag) + "0" * 32


def _probes():
    """(list, patcher): records every `cat-file -e` probe made by the module."""
    calls = []
    real = lot_measures._git

    def counted(repo, *args, **kwargs):
        if args[:2] == ("cat-file", "-e"):
            calls.append((repo, args[2] if len(args) > 2 else ""))
        return real(repo, *args, **kwargs)

    return calls, mock.patch.object(lot_measures, "_git", side_effect=counted)


class AbsentProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ameesh-8414-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(self.tmp.name)
        with lot_measures._ABSENT_LOCK:
            lot_measures._ABSENT.clear()
            lot_measures._COMMON_DIRS.clear()
        self.addCleanup(self._clear)

    def _clear(self):
        with lot_measures._ABSENT_LOCK:
            lot_measures._ABSENT.clear()
            lot_measures._COMMON_DIRS.clear()

    def test_absent_probe_once_for_the_worktrees_of_a_common_dir(self):
        """Le cas AG1 : quinze worktrees, un seul magasin d'objets."""
        worktree = os.path.join(self.tmp.name, "wt")
        _git(self.repo, "worktree", "add", "-q", "--detach", worktree)
        missing = missing_sha()
        calls, patched = _probes()
        with patched:
            self.assertFalse(lot_measures.has_commit(self.repo, missing))
            self.assertFalse(lot_measures.has_commit(self.repo, missing))
            self.assertFalse(lot_measures.has_commit(worktree, missing))
        self.assertEqual(len(calls), 1, calls)

    def test_absent_probe_once_in_a_partial_clone(self):
        """Le cas AG1 : un clone partiel dont un objet absent coûte cher."""
        clone = make_partial_clone(self.tmp.name)
        self.assertEqual(_git(clone, "config", "--get", "remote.origin.promisor"),
                         "true")
        blob = _git(clone, "rev-parse", "HEAD:README")
        read = subprocess.run(
            ["git", "-C", clone, "cat-file", "-e", blob],
            env=dict(os.environ, GIT_NO_LAZY_FETCH="1"), capture_output=True)
        self.assertNotEqual(read.returncode, 0,
                            "le clone doit être partiel : le blob reste absent")
        missing = missing_sha()
        calls, patched = _probes()
        with patched:
            self.assertFalse(lot_measures.has_commit(clone, missing))
            self.assertFalse(lot_measures.has_commit(clone, missing))
        self.assertEqual(len(calls), 1, calls)

    def test_absent_constat_expires(self):
        missing = missing_sha()
        calls, patched = _probes()
        with patched, mock.patch.object(lot_measures, "ABSENT_TTL_S", 0.0):
            self.assertFalse(lot_measures.has_commit(self.repo, missing))
            self.assertFalse(lot_measures.has_commit(self.repo, missing))
        self.assertEqual(len(calls), 2, calls)

    def test_absent_cache_is_bounded(self):
        calls, patched = _probes()
        with patched, mock.patch.object(lot_measures, "ABSENT_MAX_ENTRIES", 2):
            for tag in range(5):
                self.assertFalse(lot_measures.has_commit(self.repo, missing_sha(tag)))
        with lot_measures._ABSENT_LOCK:
            self.assertLessEqual(len(lot_measures._ABSENT), 2)
        self.assertEqual(len(calls), 5, calls)

    def test_forget_reprobes_after_a_fetch(self):
        """Un fetch réussi oublie le constat : l'objet peut être arrivé."""
        missing = missing_sha()
        calls, patched = _probes()
        with patched:
            self.assertFalse(lot_measures.has_commit(self.repo, missing))
            lot_measures._forget_absent(self.repo, [missing])
            self.assertFalse(lot_measures.has_commit(self.repo, missing))
        self.assertEqual(len(calls), 2, calls)

    def test_present_commit_is_never_hidden(self):
        sha = _git(self.repo, "rev-parse", "HEAD")
        calls, patched = _probes()
        with patched:
            self.assertTrue(lot_measures.has_commit(self.repo, sha))
            self.assertTrue(lot_measures.has_commit(self.repo, sha))
        self.assertEqual(len(calls), 2, calls)


class MeasureAbsentPgTest(PgTestCase):
    def test_measure_pass_probes_an_absent_merge_once(self):
        """Le lot #8378 : la fusion est dans home, aucun dépôt d'AG2 ne la
        porte ; chaque passe la re-sondait dans chaque worktree."""
        with lot_measures._ABSENT_LOCK:
            lot_measures._ABSENT.clear()
            lot_measures._COMMON_DIRS.clear()
        self.addCleanup(self._clear)
        with tempfile.TemporaryDirectory(prefix="ameesh-8414-pg-") as root:
            repo = make_repo(root)
            missing = missing_sha()
            lot = work.add(self.db, title="fusion dans un autre dépôt",
                           branch="lot/ailleurs")
            lid = int(lot["id"])
            work.move(self.db, lid, "build")
            work.close_merged(self.db, lid, sha=missing)
            registry.upsert(self.db, "solo", host=self.cfg.host, cwd=repo)
            calls, patched = _probes()
            with patched:
                first = lot_measures.measure_pending(
                    self.db, host=self.cfg.host, ci=None)
                second = lot_measures.measure_pending(
                    self.db, host=self.cfg.host, ci=None)
            self.assertEqual([r["work_item_id"] for r in first], [lid])
            self.assertEqual(second, [])
            self.assertEqual(len(calls), 1, calls)
            self.assertIsNone(storage.of(self.db).work.measure(lid)["diff_files"])
            # une mesure explicitement demandée re-sonde l'absent
            with patched:
                lot_measures.measure(self.db, [lid], repos=[repo], ci=None)
            self.assertEqual(len(calls), 2, calls)

    def _clear(self):
        with lot_measures._ABSENT_LOCK:
            lot_measures._ABSENT.clear()
            lot_measures._COMMON_DIRS.clear()


class MeasureForeignRepoPgTest(PgTestCase):
    """#8414 (2) : la fusion d'un lot peut vivre dans un autre dépôt.

    Un lot canon porte souvent une issue `manaty/ameesh` et sa fusion dans
    `manaty/home` (marque `ameesh-freeze-repo`). Sur un hôte qui n'a que des
    clones ameesh, la sonde ne peut que constater une absence : la passe
    l'écarte. Le même lot est mesuré sur un hôte qui a le dépôt nommé."""

    def setUp(self):
        super().setUp()
        with lot_measures._ABSENT_LOCK:
            lot_measures._ABSENT.clear()
            lot_measures._COMMON_DIRS.clear()
        self.addCleanup(self._clear)

    def _clear(self):
        with lot_measures._ABSENT_LOCK:
            lot_measures._ABSENT.clear()
            lot_measures._COMMON_DIRS.clear()

    def _lot(self, root, origin, marker="", issue="manaty/ameesh#6616"):
        repo = make_repo(root)
        if origin:
            _git(repo, "remote", "add", "origin", origin)
        lot = work.add(self.db, title="lot d'un autre dépôt", branch="canon/x",
                       issue_ref=issue)
        lid = int(lot["id"])
        work.move(self.db, lid, "build")
        frozen = _git(repo, "rev-parse", "HEAD")
        note = ("ameesh-freeze-repo: %s" % marker) if marker else ""
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, note, at)"
            " VALUES (%s, 'frozen', %s, 'test', %s, now() - interval '2 minutes')",
            (lid, frozen, note))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, note, at)"
            " VALUES (%s, 'merged', %s, 'test', '', now())", (lid, missing_sha()))
        registry.upsert(self.db, "solo", host=self.cfg.host, cwd=repo)
        return lid, repo

    def test_merge_outside_the_host_is_not_probed(self):
        with tempfile.TemporaryDirectory(prefix="ameesh-8414-foreign-") as root:
            lid, _repo = self._lot(root, "https://github.com/manaty/ameesh.git",
                                   marker="manaty/home")
            calls, patched = _probes()
            with patched:
                rows = lot_measures.measure_pending(
                    self.db, host=self.cfg.host, ci=None)
            self.assertEqual(rows, [])
            self.assertEqual(len(calls), 0, calls)
            self.assertIsNone(storage.of(self.db).work.measure(lid))

    def test_merge_in_the_named_repository_is_measured(self):
        with tempfile.TemporaryDirectory(prefix="ameesh-8414-home-") as root:
            lid, _repo = self._lot(root, "https://github.com/manaty/home.git",
                                   marker="manaty/home")
            calls, patched = _probes()
            with patched:
                rows = lot_measures.measure_pending(
                    self.db, host=self.cfg.host, ci=None)
            self.assertEqual([r["work_item_id"] for r in rows], [lid])
            self.assertEqual(len(calls), 1, calls)

    def test_lot_of_another_project_is_not_probed(self):
        with tempfile.TemporaryDirectory(prefix="ameesh-8414-issue-") as root:
            lid, _repo = self._lot(root, "https://github.com/manaty/ameesh.git",
                                   issue="manaty/home#42")
            calls, patched = _probes()
            with patched:
                rows = lot_measures.measure_pending(
                    self.db, host=self.cfg.host, ci=None)
            self.assertEqual(rows, [])
            self.assertEqual(len(calls), 0, calls)
            self.assertIsNone(storage.of(self.db).work.measure(lid))

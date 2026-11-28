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
  common-dir, ne font qu'un `cat-file` ;
* le constat est borné (durée, nombre d'entrées) et expiré il est re-sondé ;
* un fetch réussi l'oublie, donc un commit rapatrié est vu tout de suite ;
* une passe de mesure ne re-sonde plus un lot déjà mesuré dont la fusion est
  absente (le cas 1ecc7dff), et une mesure explicitement demandée re-sonde.
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

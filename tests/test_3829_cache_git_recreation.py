# SPDX-License-Identifier: AGPL-3.0-only
"""#3829 — un dépôt recréé au même chemin ne relit pas le cache de l'ancien.

Le cache de #3753 est indexé par (chemin réel, arguments). Le contenu d'un
objet adressé par son SHA ne change jamais, mais sa **présence** dépend du
dépôt qui est au chemin : un dépôt supprimé puis recréé au même chemin (clone,
`init`, `rm -rf` suivi d'un clone hors de `canon._git`) ne doit jamais servir
une lecture positive — `rev-parse <sha>^{commit}`, `show <sha>:<chemin>` —
faite sur l'ancien. La clé porte donc l'identité de l'instance du dépôt ; sur
un dépôt stable, le gain de #3753 reste intact (une lecture, un lancement).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import canon


def _git_env() -> dict:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("GIT_"):
            env.pop(key)
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="banc", GIT_AUTHOR_EMAIL="banc@example.invalid",
               GIT_COMMITTER_NAME="banc", GIT_COMMITTER_EMAIL="banc@example.invalid")
    return env


def _git(cwd: str, *args: str, stdin: bytes | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, env=_git_env(), input=stdin,
                          capture_output=True, check=False)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args),
                                              proc.stderr.decode("utf-8", "replace")))
    return proc.stdout.decode("utf-8", "replace").strip()


class _Repo:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        _git(root, "init", "-q")
        _git(root, "config", "user.name", "banc")
        _git(root, "config", "user.email", "banc@example.invalid")

    def write(self, path: str, text: str) -> None:
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def commit(self, message: str) -> str:
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", message)
        return _git(self.root, "rev-parse", "HEAD")


def _canon_launches(run) -> int:
    """Les lancements réellement passés par `canon._git` (préfixe propre)."""
    return sum(1 for call in run.call_args_list
               if call.args and isinstance(call.args[0], list)
               and call.args[0][:2] == ["git", "--no-pager"])


class RecreatedRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3829-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(canon._content_cache_clear)
        canon._content_cache_clear()

    def _source(self) -> str:
        source = _Repo(os.path.join(self.tmp, "source"))
        source.write("a.txt", "source\n")
        source.commit("source")
        return source.root

    def test_a_reinitialised_repository_no_longer_serves_the_old_object(self):
        repo = _Repo(os.path.join(self.tmp, "depot"))
        repo.write("a.txt", "un\n")
        first = repo.commit("un")
        canon._content_cache_clear()
        before = canon._repository_identity(repo.root)
        self.assertEqual(canon.git_commit(repo.root, first), first)
        self.assertEqual(canon._git_out(repo.root, "show", first + ":a.txt"), "un")
        self.assertEqual(canon._content_cache_stats()["entries"], 2)

        shutil.rmtree(repo.root)
        fresh = _Repo(repo.root)
        fresh.write("b.txt", "autre\n")
        fresh.commit("autre")
        self.assertNotEqual(canon._repository_identity(repo.root), before)
        # l'ancien commit n'existe pas dans le nouveau dépôt : il est absent
        self.assertIsNone(canon.git_commit(repo.root, first))
        self.assertIsNone(canon._git_out(repo.root, "show", first + ":a.txt"))

    def test_a_repository_recloned_at_the_same_path_answers_absent(self):
        repo = _Repo(os.path.join(self.tmp, "depot"))
        repo.write("a.txt", "un\n")
        first = repo.commit("un")
        canon._content_cache_clear()
        self.assertEqual(canon.git_commit(repo.root, first), first)
        shutil.rmtree(repo.root)
        _git(self.tmp, "clone", "-q", "--no-local", self._source(), repo.root)
        self.assertIsNone(canon.git_commit(repo.root, first))
        self.assertIsNone(canon._git_out(repo.root, "show", first + ":a.txt"))

    def test_clone_forgets_the_cache_of_its_destination(self):
        destination = os.path.join(self.tmp, "clone")
        old = _Repo(destination)
        old.write("a.txt", "ancien\n")
        old_sha = old.commit("ancien")
        canon._content_cache_clear()
        self.assertEqual(canon.git_commit(destination, old_sha), old_sha)
        self.assertEqual(canon._content_cache_stats()["entries"], 1)
        shutil.rmtree(destination)

        # destination relative au cwd de la commande, comme git la résout
        proc = canon._git(self.tmp, "clone", "-q", "--no-local",
                          self._source(), "clone")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(canon._content_cache_stats()["entries"], 0)
        self.assertIsNone(canon.git_commit(destination, old_sha))

    def test_init_forgets_the_cache_of_its_destination(self):
        destination = os.path.join(self.tmp, "init")
        old = _Repo(destination)
        old.write("a.txt", "ancien\n")
        old_sha = old.commit("ancien")
        canon._content_cache_clear()
        self.assertEqual(canon.git_commit(destination, old_sha), old_sha)
        self.assertEqual(canon._content_cache_stats()["entries"], 1)
        shutil.rmtree(destination)

        proc = canon._git(self.tmp, "init", "-q", "init")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(canon._content_cache_stats()["entries"], 0)
        self.assertIsNone(canon.git_commit(destination, old_sha))

    def test_a_stable_repository_keeps_its_identity_and_its_hits(self):
        repo = _Repo(os.path.join(self.tmp, "stable"))
        repo.write("a.txt", "un\n")
        sha = repo.commit("un")
        args = ("show", sha + ":a.txt")
        first_key = canon._content_cache_key(repo.root, args, None, None)
        self.assertIsNotNone(first_key)
        self.assertEqual(
            first_key, canon._content_cache_key(repo.root, args, None, None))

        with mock.patch.object(canon.subprocess, "run",
                               wraps=subprocess.run) as run:
            self.assertEqual(canon._git_out(repo.root, *args), "un")
            self.assertEqual(canon._git_out(repo.root, *args), "un")
        self.assertEqual(_canon_launches(run), 1)

    def test_a_linked_worktree_has_a_stable_identity(self):
        """Le `.git` d'un worktree est un fichier : l'identité reste stable."""
        repo = _Repo(os.path.join(self.tmp, "main"))
        repo.write("a.txt", "un\n")
        sha = repo.commit("un")
        worktree = os.path.join(self.tmp, "worktree")
        _git(repo.root, "worktree", "add", "-q", "--detach", worktree, sha)
        args = ("show", sha + ":a.txt")
        first_key = canon._content_cache_key(worktree, args, None, None)
        self.assertIsNotNone(first_key)
        self.assertEqual(
            first_key, canon._content_cache_key(worktree, args, None, None))
        with mock.patch.object(canon.subprocess, "run",
                               wraps=subprocess.run) as run:
            self.assertEqual(canon._git_out(worktree, *args), "un")
            self.assertEqual(canon._git_out(worktree, *args), "un")
        self.assertEqual(_canon_launches(run), 1)


if __name__ == "__main__":
    unittest.main()

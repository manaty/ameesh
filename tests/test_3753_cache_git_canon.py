# SPDX-License-Identifier: AGPL-3.0-only
"""#3753 — le cache des lectures git du canon reste exact.

Le cache ne retient que des objets adressés par leur SHA complet : une tête
qui bouge produit une autre clé, une ref symbolique n'est jamais cachée, et
les lectures d'état (statut, remote, refs) ne le traversent pas. Un objet
absent n'est pas mémorisé non plus : un fetch peut le rendre lisible plus
tard. Les témoins ci-dessous comptent les lancements réels de `git` (le cache
doit les faire tomber) et vérifient qu'un nouveau commit est relu. Les
exclusions de #3777 (dates relatives, mailmap, describe/decorate, fichier de
motifs) sont couvertes ici : leur sortie ne dépend pas du seul objet lu.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import canon

FEDERATION = """federation: "0.1"
id: manaty-test
root: home
members:
  - id: home
    ref: main
    bundle: .
    entrypoint: index.md
"""


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


def _fiche(title: str, responsible: str) -> str:
    return ("---\ntype: Agent\ntitle: %s\nresponsible: %s\n---\n\n# fiche\n"
            % (title, responsible))


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


class GitContentCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3753-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(canon._content_cache_clear)
        canon._content_cache_clear()
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("a.txt", "un\n")
        self.first = self.repo.commit("un")

    def test_the_same_content_read_is_served_once(self):
        with mock.patch.object(canon.subprocess, "run",
                               wraps=subprocess.run) as run:
            first = canon._git_out(self.repo.root, "show",
                                   self.first + ":a.txt")
            second = canon._git_out(self.repo.root, "show",
                                    self.first + ":a.txt")
        self.assertEqual((first, second), ("un", "un"))
        self.assertEqual(_canon_launches(run), 1)

    def test_a_new_commit_is_a_new_key_and_the_old_one_stays_readable(self):
        canon._content_cache_clear()
        self.assertEqual(canon.git_read(self.repo.root, self.first, "a.txt"),
                         b"un\n")
        self.repo.write("a.txt", "deux\n")
        second = self.repo.commit("deux")
        self.assertNotEqual(second, self.first)
        self.assertEqual(canon.git_read(self.repo.root, second, "a.txt"),
                         b"deux\n")
        # l'ancien commit est immuable : sa lecture reste celle d'alors
        self.assertEqual(canon.git_read(self.repo.root, self.first, "a.txt"),
                         b"un\n")

    def test_a_symbolic_ref_is_never_cached(self):
        with mock.patch.object(canon.subprocess, "run",
                               wraps=subprocess.run) as run:
            before = canon._git_out(self.repo.root, "show", "HEAD:a.txt")
            self.repo.write("a.txt", "deux\n")
            head = self.repo.commit("deux")
            after = canon._git_out(self.repo.root, "show", "HEAD:a.txt")
        self.assertEqual(before, "un")
        self.assertEqual(after, "deux")
        self.assertNotEqual(head, self.first)
        self.assertEqual(_canon_launches(run), 2)

    def test_state_reads_are_never_cached(self):
        with mock.patch.object(canon.subprocess, "run",
                               wraps=subprocess.run) as run:
            status_before = canon._git_out(self.repo.root, "status", "--porcelain")
            self.repo.write("b.txt", "nouveau\n")
            status_after = canon._git_out(self.repo.root, "status", "--porcelain")
            remote_before = canon._git_out(self.repo.root, "remote")
            _git(self.repo.root, "remote", "add", "origin", self.tmp)
            remote_after = canon._git_out(self.repo.root, "remote")
        self.assertEqual(status_before, "")
        self.assertIn("b.txt", status_after)
        self.assertEqual(remote_before, "")
        self.assertEqual(remote_after, "origin")
        self.assertEqual(_canon_launches(run), 4)

    def test_only_resolved_content_reads_get_a_key(self):
        def key(*args, stdin=None):
            return canon._content_cache_key(self.repo.root, args, stdin, None)

        self.assertIsNotNone(key("show", self.first + ":a.txt"))
        self.assertIsNone(key("show", "HEAD:a.txt"))
        self.assertIsNone(key("rev-parse", "HEAD"))
        for args in (("fetch", "--quiet", "origin"),
                     ("status", "--porcelain"),
                     ("remote",),
                     ("for-each-ref", "--format=%(refname)", "refs/heads/")):
            self.assertIsNone(key(*args), args)
        self.assertIsNone(key("diff", self.first))       # arbre de travail

    def test_a_format_that_reads_refs_is_not_cached(self):
        stateful = canon._content_cache_key(
            self.repo.root, ("log", "-1", "--format=%d", self.first), None, None)
        self.assertIsNone(stateful)
        content = canon._content_cache_key(
            self.repo.root, ("log", "-1", "--format=%H", self.first), None, None)
        self.assertIsNotNone(content)

    def test_a_relative_or_mailmap_format_is_not_cached(self):
        def key(*args):
            return canon._content_cache_key(self.repo.root, args, None, None)

        for placeholder in ("%ar", "%ah", "%cr", "%ch", "%aN", "%aE",
                            "%cN", "%cE", "%(describe)", "%(describe:tags)",
                            "%(decorate)"):
            self.assertIsNone(key("log", "-1", "--format=" + placeholder,
                                  self.first), placeholder)
        # noms, courriels et dates bruts sont dans l'objet : eux se cachent
        for placeholder in ("%an", "%ae", "%cn", "%ce", "%at", "%ct"):
            self.assertIsNotNone(key("log", "-1", "--format=" + placeholder,
                                     self.first), placeholder)

    def test_a_relative_date_option_is_not_cached(self):
        def key(*args):
            return canon._content_cache_key(self.repo.root, args, None, None)

        for option in (("--date=relative",), ("--date=human",),
                       ("--date", "relative"), ("--date", "human")):
            self.assertIsNone(key("log", "-1", *option, self.first), option)
            self.assertIsNone(key("show", "-s", *option, self.first), option)
        # une date absolue ne dépend pas de l'instant de lecture
        self.assertIsNotNone(key("log", "-1", "--date=iso", self.first))
        self.assertIsNotNone(key("log", "-1", "--date", "iso", self.first))

    def test_a_grep_patterns_file_is_not_cached(self):
        def key(*args, stdin=None):
            return canon._content_cache_key(self.repo.root, args, stdin, None)

        self.assertIsNone(key("grep", "-f", "pats", self.first))
        self.assertIsNone(key("grep", "--no-color", "-l", "-F", "-f", "pats",
                              self.first))
        # `-f -` lit stdin, qui fait partie de la clé
        self.assertIsNotNone(key("grep", "-f", "-", self.first, stdin=b"a\n"))

    def test_the_cache_key_includes_the_repository(self):
        other = _Repo(os.path.join(self.tmp, "autre"))
        canon._content_cache_clear()
        self.assertEqual(canon._git_out(self.repo.root, "show",
                                        self.first + ":a.txt"), "un")
        self.assertIsNone(canon._git_out(other.root, "show",
                                         self.first + ":a.txt"))
        self.assertEqual(canon._content_cache_stats()["entries"], 1)

    def test_a_search_in_a_tree_is_cached_even_without_a_match(self):
        self.repo.write("b.txt", "aiguille\n")
        second = self.repo.commit("aiguille")
        found = ("grep", "--no-color", "-l", "-I", "-F", "-f", "-", second)
        missing = ("grep", "--no-color", "-l", "-I", "-F", "-f", "-", second)
        with mock.patch.object(canon.subprocess, "run",
                               wraps=subprocess.run) as run:
            hit = canon._git(self.repo.root, *found, stdin=b"aiguille\n")
            none = canon._git(self.repo.root, *missing, stdin=b"absente\n")
            again = canon._git(self.repo.root, *found, stdin=b"aiguille\n")
        self.assertEqual(hit.returncode, 0)
        self.assertIn(b"b.txt", hit.stdout)
        self.assertEqual(none.returncode, 1)
        self.assertEqual(again.stdout, hit.stdout)
        self.assertEqual(_canon_launches(run), 2)

    def test_a_missing_batch_object_is_not_remembered(self):
        blob = _git(self.repo.root, "hash-object", "--stdin", stdin=b"ghost\n")
        request = (blob + "\n").encode()
        canon._content_cache_clear()
        missing = canon._git(self.repo.root, "cat-file", "--batch", stdin=request)
        self.assertIn(b" missing\n", missing.stdout)
        checked = canon._git(self.repo.root, "cat-file", "--batch-check",
                             stdin=request)
        self.assertIn(b" missing\n", checked.stdout)
        self.assertEqual(canon._content_cache_stats()["entries"], 0)
        _git(self.repo.root, "hash-object", "-w", "--stdin", stdin=b"ghost\n")
        present = canon._git(self.repo.root, "cat-file", "--batch", stdin=request)
        self.assertIn(b" blob 6\n", present.stdout)
        self.assertIn(b"ghost\n", present.stdout)


class CanonFreshReadTest(unittest.TestCase):
    """La lecture du canon n'est pas figée sur le premier commit lu."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3753-canon-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(canon._content_cache_clear)
        canon._content_cache_clear()
        bare = os.path.join(self.tmp, "origin.git")
        os.makedirs(bare)
        _git(bare, "init", "-q", "--bare", "-b", "main")
        self.clone = os.path.join(self.tmp, "canon")
        os.makedirs(self.clone)
        _git(self.clone, "init", "-q", "-b", "main")
        _git(self.clone, "config", "user.name", "banc")
        _git(self.clone, "config", "user.email", "banc@example.invalid")
        for path, text in (("federation.yaml", FEDERATION),
                           ("index.md", "# canon\n"),
                           ("agents/ouvrier.md",
                            _fiche("ouvrier", "human:alice"))):
            full = os.path.join(self.clone, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(text)
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "canon initial")
        _git(self.clone, "remote", "add", "origin", bare)
        _git(self.clone, "push", "-q", "origin", "main")
        _git(self.clone, "fetch", "-q", "origin")
        _git(self.clone, "remote", "set-head", "origin", "main")

    def test_a_new_canon_commit_is_read_fresh(self):
        first = canon.load(self.clone)
        self.assertTrue(first.readable, first.load_findings)
        agent = first.agent("ouvrier")
        self.assertIsNotNone(agent)
        self.assertEqual(agent.responsible, "human:alice")
        # nouveau commit du canon, poussé sur la branche lue (`origin/main`)
        with open(os.path.join(self.clone, "agents", "ouvrier.md"), "w",
                  encoding="utf-8") as handle:
            handle.write(_fiche("ouvrier", "human:bruno"))
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "responsable changé")
        _git(self.clone, "push", "-q", "origin", "main")
        _git(self.clone, "fetch", "-q", "origin")
        second = canon.load(self.clone)
        self.assertTrue(second.readable, second.load_findings)
        self.assertNotEqual(second.sources[0].commit, first.sources[0].commit)
        self.assertEqual(second.agent("ouvrier").responsible, "human:bruno")

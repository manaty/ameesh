# SPDX-License-Identifier: AGPL-3.0-only
"""#8141 : `ci_parts.extract` résout `dest` une fois — un TMPDIR de lien.

Depuis #8128, le `TMPDIR` d'une session de lot est un alias court qui est un
lien (`~/.t/<mesh>/<lot>` vers le dossier géré). `_member_path` comparait le
realpath de chaque entrée d'archive à `dest` non résolu : la première entrée
de `git archive` (`.ameesh`) était refusée « archive entry escapes the
extracted tree », et le gel ne passait plus depuis une telle session.

Témoins : extraction sous un `dest` qui est un lien, verte, écrite à la place
réelle ; `dest` sous un parent lié, créé par l'extraction, vert ; une entrée
qui sort réellement de l'arbre reste refusée (lien préexistant dans `dest`,
traversée `..`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from ameesh import ci_parts


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t",
                                   GIT_AUTHOR_EMAIL="t@example.invalid",
                                   GIT_COMMITTER_NAME="t",
                                   GIT_COMMITTER_EMAIL="t@example.invalid",
                                   GIT_CONFIG_GLOBAL=os.devnull,
                                   GIT_CONFIG_NOSYSTEM="1"))
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _write(repo: str, path: str, text: str = "x\n") -> None:
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)


class _Repo(unittest.TestCase):
    """Un dépôt de témoin par test ; la racine `.ameesh` est la première
    entrée de `git archive` sur ce dépôt, comme sur le vrai."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-8141-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _write(self.repo, ".ameesh/generated-files.json", "{}\n")
        _write(self.repo, "src/hello.py", "print('bonjour')\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        self.commit = _git(self.repo, "rev-parse", "HEAD")

    def real_dir(self, name: str = "real") -> str:
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        return path

    def link(self, target: str, name: str = "link") -> str:
        path = os.path.join(self.tmp, name)
        os.symlink(target, path)
        return path


class SymlinkedDestTest(_Repo):
    def test_a_symlinked_dest_is_extracted_green(self) -> None:
        real = self.real_dir()
        link = self.link(real)
        ci_parts.extract(self.repo, self.commit, link)
        self.assertTrue(os.path.islink(link))
        for name in (".ameesh/generated-files.json", "src/hello.py"):
            with open(os.path.join(real, name), encoding="utf-8") as handle:
                self.assertTrue(handle.read())
        self.assertEqual(os.listdir(self.tmp).count("real"), 1)

    def test_a_dest_created_under_a_linked_parent_is_resolved(self) -> None:
        parent = self.real_dir("parent")
        linked = self.link(parent, "linked")
        dest = os.path.join(linked, "nested", "tree")
        ci_parts.extract(self.repo, self.commit, dest)
        self.assertTrue(os.path.isfile(os.path.join(parent, "nested", "tree",
                                                    ".ameesh/generated-files.json")))


class EscapeStillRefusedTest(_Repo):
    def test_a_preexisting_link_in_dest_cannot_carry_an_entry_outside(self) -> None:
        real = self.real_dir()
        outside = self.real_dir("outside")
        # `src/` est dans l'archive : son realpath est `outside`, hors de dest.
        os.symlink(outside, os.path.join(real, "src"))
        link = self.link(real)
        with self.assertRaises(ci_parts.PartsError) as caught:
            ci_parts.extract(self.repo, self.commit, link)
        self.assertIn("escapes the extracted tree", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(outside, "hello.py")))

    def test_member_path_refuses_a_real_traversal_from_a_resolved_dest(self) -> None:
        dest = os.path.realpath(self.real_dir())
        for name in ("../evil", "/etc/passwd", "a/../../evil", ".", ""):
            with self.assertRaises(ci_parts.PartsError):
                ci_parts._member_path(dest, name)
        for name in (".ameesh/generated-files.json", "src/hello.py"):
            self.assertEqual(ci_parts._member_path(dest, name),
                             os.path.join(dest, *name.split("/")))


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""#1819 : les lectures au SHA qui fondent la preuve du gel ignorent `refs/replace`.

Témoins sur un vrai dépôt Git : un `git replace` local substitue un blob sans
changer le SHA ; le contrat lu, l'arbre envoyé et le pack poussé doivent rester
ceux du SHA publié."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

import ameesh.prevalidation  # noqa: F401  (preuve du chargement du fichier, plan L161f #1339)
from ameesh import offload, prevalidation as pv


class FakeRemote:
    """`put --have` ne connaît rien ; `put <snap>` indexe le pack reçu dans `mirror`."""

    def __init__(self, mirror: str):
        self.mirror = mirror

    def call(self, *words, input=None, stdin=None, capture=True, timeout=0):
        if "--have" in words:
            return subprocess.CompletedProcess(words, 0, b"", b"")
        return subprocess.run(["git", "-C", self.mirror, "index-pack", "--stdin", "--fix-thin"],
                              stdin=stdin, capture_output=True)


class NoReplaceObjects(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.join(tmp.name, "repo")
        self.mirror = os.path.join(tmp.name, "mirror.git")
        self._git(tmp.name, "init", "-q", self.repo)
        self._git(tmp.name, "init", "-q", "--bare", self.mirror)
        with open(os.path.join(self.repo, "contrat.txt"), "w", encoding="utf-8") as fh:
            fh.write("test_vrai  # publié\n")
        self._git(self.repo, "add", "contrat.txt")
        self._git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t",
                  "commit", "-qm", "un")
        self.head = self._git(self.repo, "rev-parse", "HEAD")
        self.vrai = self._git(self.repo, "rev-parse", "HEAD:contrat.txt")
        faux = self._git(self.repo, "hash-object", "-w", "--stdin", input="test_faux\n")
        self._git(self.repo, "replace", self.vrai, faux)
        # le remplacement est actif pour une lecture ordinaire
        self.assertEqual(self._git(self.repo, "cat-file", "blob", "HEAD:contrat.txt"),
                         "test_faux")

    def _git(self, repo: str, *args: str, input: str | None = None) -> str:
        proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                              input=input)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _commit_tree(self, tree: str) -> str:
        return self._git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t",
                         "commit-tree", tree, "-m", "faux")

    def test_contrat_lu_au_sha_publie(self):
        modules, why = pv.contract_modules(["contrat.txt"], self.repo, head=self.head)
        self.assertEqual((modules, why), (("test_vrai",), ""))

    def test_arbre_et_pack_envoyes_au_sha_publie(self):
        # un commit remplacé porte un autre arbre : `read-tree` le suivrait
        arbre = self._git(self.repo, "mktree",
                          input="100644 blob %s\tplan.json\n" % self.vrai)
        self._git(self.repo, "replace", self.head, self._commit_tree(arbre))
        # le plan ne remplace aucun chemin du SHA publié (l'arbre remplacé en a un)
        self.assertEqual(pv.remote_plan_path(os.path.join(self.repo, "plan.json"), self.repo,
                                             head=self.head), "plan.json")
        snap = pv.pinned_snapshot(self.repo, self.head, "plan.json", "{}\n")
        self.assertEqual(sorted(self._git(self.repo, "--no-replace-objects", "ls-tree",
                                          "--name-only", snap).split()),
                         ["contrat.txt", "plan.json"])
        self.assertEqual(self._git(self.repo, "--no-replace-objects", "rev-parse",
                                   "%s:contrat.txt" % snap), self.vrai)
        offload.push(FakeRemote(self.mirror), self.repo, "depot", snap)
        self.assertEqual(self._git(self.mirror, "cat-file", "blob", self.vrai),
                         "test_vrai  # publié")


if __name__ == "__main__":
    unittest.main()

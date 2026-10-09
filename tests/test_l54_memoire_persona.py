# SPDX-License-Identifier: AGPL-3.0-only
"""L54 (0029, 0032 §5) : mémoire de persona, dépôt git au format neutre."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import types
import unittest

from ameesh import persona_memory as pm
from ameesh.runner import AgentWorker


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class _Depot(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ameesh-l54-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bare = os.path.join(self.tmp, "memoire.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.bare], check=True)
        seed = os.path.join(self.tmp, "seed")
        subprocess.run(["git", "clone", "-q", self.bare, seed], check=True, capture_output=True)
        git(seed, "config", "user.email", "t@t")
        git(seed, "config", "user.name", "t")
        with open(os.path.join(seed, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("- [Préférences](souvenirs/preferences.md) — réponses courtes en français\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "init")
        git(seed, "push", "-q", "origin", "HEAD:main")
        self.etat = os.path.join(self.tmp, "etat")

    def ouvrir(self):
        return pm.open_copy(self.bare, self.etat, "verif-a")


class CopieTest(_Depot):
    def test_ouverture_et_designation(self):
        path = self.ouvrir()
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o700")
        texte = pm.preface(path, "verif-a", "s1")
        self.assertIn("réponses courtes en français", texte)
        self.assertIn("journal/s1.md", texte)
        self.assertIn("Jamais de secret", texte)
        self.assertEqual(self.ouvrir(), path)          # rouvrir = mettre à jour

    def test_journal_pousse(self):
        path = self.ouvrir()
        os.makedirs(os.path.join(path, "journal"))
        with open(pm.journal_path(path, "s1"), "w", encoding="utf-8") as fh:
            fh.write("- Le banc staging se lance avec scripts/fed-bench.sh\n")
        r = pm.sync(path, "fin de tour")
        self.assertEqual(r["status"], "poussee")
        self.assertIn("journal/s1.md", git(self.bare, "ls-tree", "-r", "--name-only", "main"))
        self.assertEqual(pm.sync(path, "rien")["status"], "inchangee")

    def test_secret_refuse(self):
        path = self.ouvrir()
        os.makedirs(os.path.join(path, "journal"))
        with open(pm.journal_path(path, "s1"), "w", encoding="utf-8") as fh:
            fh.write("la clé est sk-abcdefghijklmnopqrstuvwxyz0123\n")
        r = pm.sync(path, "fin de tour")
        self.assertEqual(r["status"], "secret")
        self.assertEqual(r["files"], ["journal/s1.md"])
        self.assertNotIn("journal/s1.md", git(self.bare, "ls-tree", "-r", "--name-only", "main"))

    def test_deux_sessions_paralleles(self):
        a = self.ouvrir()
        b = pm.open_copy(self.bare, os.path.join(self.tmp, "etat-b"), "verif-a")
        for path, session in ((a, "s1"), (b, "s2")):
            os.makedirs(os.path.join(path, "journal"), exist_ok=True)
            with open(pm.journal_path(path, session), "w", encoding="utf-8") as fh:
                fh.write("note de %s\n" % session)
        self.assertEqual(pm.sync(a, "a")["status"], "poussee")
        self.assertEqual(pm.sync(b, "b")["status"], "poussee")    # reprise sur le dernier commit
        noms = git(self.bare, "ls-tree", "-r", "--name-only", "main")
        self.assertIn("journal/s1.md", noms)
        self.assertIn("journal/s2.md", noms)

    def test_fermeture_efface_la_copie(self):
        path = self.ouvrir()
        self.assertEqual(pm.close(path)["status"], "effacee")
        self.assertFalse(os.path.exists(path))

    def test_depot_injoignable(self):
        with self.assertRaises(pm.MemoryError):
            pm.open_copy(os.path.join(self.tmp, "absent.git"), self.etat, "x")
        self.assertFalse(os.path.exists(pm.copy_dir(self.etat, "x")))

    def test_consigne_de_consolidation(self):
        texte = pm.consolidation_prompt("/etat/memoire/a")
        self.assertIn("TOUR DE MÉMOIRE", texte)
        self.assertIn("MEMORY.md", texte)


class ExecuteurTest(_Depot):
    def test_synchronisation_en_fin_de_tour(self):
        path = self.ouvrir()
        os.makedirs(os.path.join(path, "journal"))
        with open(pm.journal_path(path, "s1"), "w", encoding="utf-8") as fh:
            fh.write("- un fait durable\n")
        journal = []
        worker = types.SimpleNamespace(
            name="verif-a", agent={"memory_repository": self.bare},
            runner=types.SimpleNamespace(dry_run=False),
            cfg=types.SimpleNamespace(state_dir=self.etat, persona_memory={}))
        import ameesh.runner as runner_mod
        ancien = runner_mod.log
        runner_mod.log = journal.append
        self.addCleanup(setattr, runner_mod, "log", ancien)
        AgentWorker._synchronise_memoire(worker)
        for t in threading.enumerate():
            if t.name == "verif-a-memoire":
                t.join(30)
        self.assertTrue(any("poussée" in j for j in journal), journal)

    def test_sans_depot_rien(self):
        worker = types.SimpleNamespace(name="x", agent={}, runner=types.SimpleNamespace(dry_run=False),
                                       cfg=types.SimpleNamespace(state_dir=self.etat))
        AgentWorker._synchronise_memoire(worker)      # ne lève rien, ne lance rien


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""#5804 : le garde refuse qu'un test écrive dans le vrai état ameesh.

Le 27/10, un extrait de diagnostic (le même geste qu'un test, sans bac) a
appelé `read_attestation()` avec le vrai HOME : la preuve partagée a été
matérialisée dans `~/.local/state/ameesh/mise-a-jour/actif-aaaaaaaaaaaa.json`
et `staging_pending.active_hosts()` l'a lue comme un hôte actif, faisant
refuser le coupeur (#5748).

Ce module prouve les deux moitiés : le geste écrit bien dans l'état du HOME
du processus (contrôle sans garde), et `tests/state_guard.py` le refuse dès
qu'il est installé — c'est le cas de toute la suite, via `tests/__init__.py`.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from . import state_guard, support

#: Le geste du 27/10, réduit : la fixture de `test_2448_release_cutoff`
#: (date 2026-10-18T09:00:00Z, trois VM d'agents + serveur prouvés, poste
#: reporté) matérialisée par `read_attestation()` avec un faux journal.
LEAK = '''
from ameesh import release_cutoff as rc


class FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def query(self, sql, params):
        return self.rows


sha = "a" * 40
data = {"sha": sha, "date": "2026-10-18T09:00:00Z",
        "hotes": {hote: {"actif": sha} for hote in rc.STAGING_VMS},
        "reportes": {"pc-smichea": {"raison": "poste absent"}}}
rc.read_attestation(sha, FakeDb([{"data": data}]))
print(rc.attestation_path(sha))
'''

ATTESTATION = os.path.join(".local", "state", "ameesh", "mise-a-jour",
                           "actif-aaaaaaaaaaaa.json")


def run_child(code: str, home: str) -> subprocess.CompletedProcess:
    """Un Python enfant dont le HOME est un bac, comme un test le ferait."""
    env = dict(os.environ, HOME=home)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (support.SRC, support.REPO, env.get("PYTHONPATH", ""))
        if part)
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, timeout=120)


class RealStateGuardTest(unittest.TestCase):
    """Le garde est posé sur l'état du poste, pas sur les bacs des tests."""

    def test_guard_is_installed_on_the_poste_state(self):
        roots = state_guard.installed_roots()
        self.assertTrue(roots)
        self.assertEqual(roots, state_guard.default_roots())
        self.assertEqual([os.path.basename(root) for root in roots],
                         list(state_guard.PROTECTED_SUBDIRS))
        self.assertTrue(all(root.startswith(state_guard.default_state())
                            for root in roots))

    def test_write_is_refused_and_read_is_allowed(self):
        folder = state_guard.installed_roots()[0]
        path = os.path.join(folder, "actif-aaaaaaaaaaaa.json")
        for event, args, blamed in (
                ("open", (path, "w", 0), path),
                ("open", (path, "a", 0), path),
                # B1 ameesh-cx2 : un chemin bytes ne doit pas lever TypeError.
                ("open", (os.fsencode(path), "w", 0), path),
                ("open", (os.fsencode(path), None, os.O_WRONLY | os.O_CREAT), path),
                ("open", (path, None, os.O_WRONLY | os.O_CREAT), path),
                ("os.mkdir", (folder, 0o700, None), folder),
                ("os.rename", ("/tmp/ailleurs", path), path),
                ("os.remove", (path, None), path)):
            with self.subTest(event=event), \
                    self.assertRaises(state_guard.RealStateWrite) as caught:
                state_guard.check(event, args)
            self.assertIn(blamed, str(caught.exception))
        # Lecture, écriture hors de l'état réel, descripteur : permis.
        state_guard.check("open", (path, "r", 0))
        state_guard.check("open", (path, None, os.O_RDONLY))
        state_guard.check("open", (3, "w", 0))
        with tempfile.TemporaryDirectory(prefix="ameesh-5804-") as bac:
            state_guard.check("open", (os.path.join(bac, "actif-x.json"), "w", 0))
            state_guard.check("open", (os.fsencode(os.path.join(bac, "bies.json")),
                                       "w", 0))
            state_guard.check("os.mkdir", (os.path.join(bac, "neuf"), 0o700, None))
            # Le symptôme du B1, de bout en bout : une vraie écriture bytes
            # hors état protégé passe le hook (avant le correctif : TypeError).
            with open(os.fsencode(os.path.join(bac, "reel-bytes.json")),
                      "w", encoding="utf-8") as stream:
                stream.write("ok")

    def test_guarded_child_cannot_materialise_the_fixture(self):
        with tempfile.TemporaryDirectory(prefix="ameesh-5804-garde-") as tmp:
            home = os.path.join(tmp, "home")
            os.mkdir(home)
            proc = run_child("import tests\n" + LEAK, home)
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("RealStateWrite", proc.stderr)
            self.assertIn("#5804", proc.stderr)
            self.assertFalse(os.path.exists(os.path.join(home, ATTESTATION)),
                             proc.stderr)

    def test_without_the_guard_the_same_child_leaks(self):
        """Contrôle négatif : le geste est bien une écriture réelle."""
        with tempfile.TemporaryDirectory(prefix="ameesh-5804-sans-garde-") as tmp:
            home = os.path.join(tmp, "home")
            os.mkdir(home)
            proc = run_child(LEAK, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            path = os.path.join(home, ATTESTATION)
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
            self.assertEqual(data["sha"], "a" * 40)
            self.assertEqual(data["reportes"], {"pc-smichea": {"raison": "poste absent"}})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

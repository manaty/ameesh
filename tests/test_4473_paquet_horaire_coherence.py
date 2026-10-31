# SPDX-License-Identifier: AGPL-3.0-only
"""#4473 : le tic du paquet horaire et sa bibliothèque viennent du même arbre.

Le script est celui de la cible (`check_fresh`) ; l'unité le lançait avec le
Python du venv installé, si bien qu'après un retour arrière de l'hôte le script
de main importait une bibliothèque plus ancienne : `cannot import name
'packet_pause'`, « pause illisible », aucun geste. Les témoins : l'unité rendue
(et sa copie `deploy/systemd`) donne `PYTHONPATH=<clone>/src` au tic, et le
script lui-même charge sa bibliothèque de l'arbre même quand un `ameesh` plus
ancien est déjà sur `PYTHONPATH` — ou refuse le geste s'il est déjà importé.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ameesh import role_timers

ROOT = Path(__file__).resolve().parents[1]

FAKE_INIT = '__version__ = "tree"\n'
FAKE_IDENTITY = '''\
def git_identity(pilot):
    return {"GIT_AUTHOR_NAME": pilot,
            "GIT_AUTHOR_EMAIL": pilot + "@ameesh.invalid",
            "GIT_COMMITTER_NAME": pilot,
            "GIT_COMMITTER_EMAIL": pilot + "@ameesh.invalid"}
'''
FAKE_CONFIG = "def load():\n    return {}\n"
FAKE_MECHANICS = '''\
from types import SimpleNamespace


def configured(document):
    return SimpleNamespace(timezone="UTC")
'''
FAKE_DB = '''\
class _Connection:
    def close(self):
        pass


def connect(config):
    return _Connection()
'''
FAKE_PAUSE = '''\
import os
import sys

print("TREE_PACKET_PAUSE", file=sys.stderr)
print("PYTHONPATH_FIRST=" + (os.environ.get("PYTHONPATH") or "").split(os.pathsep)[0],
      file=sys.stderr)


def active(db, **kwargs):
    return None


def alert_due(db, data, **kwargs):
    print("TREE_PACKET_PAUSE_ALERT", file=sys.stderr)


def describe(data, zone=None):
    return "?"


class PauseError(ValueError):
    pass
'''


def _write_fake_tree(base: Path) -> Path:
    """Un clone minimal : scripts réels, `src/ameesh` factice et reconnaissable."""
    tree = base / "tree"
    (tree / "scripts").mkdir(parents=True)
    for name in ("paquet-horaire.py", "paquet-auto.py"):
        shutil.copyfile(ROOT / "scripts" / name, tree / "scripts" / name)
    (tree / ".git").mkdir()
    package = tree / "src" / "ameesh"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(FAKE_INIT, encoding="utf-8")
    (package / "identity.py").write_text(FAKE_IDENTITY, encoding="utf-8")
    (package / "config.py").write_text(FAKE_CONFIG, encoding="utf-8")
    (package / "mechanics.py").write_text(FAKE_MECHANICS, encoding="utf-8")
    (package / "db.py").write_text(FAKE_DB, encoding="utf-8")
    (package / "packet_pause.py").write_text(FAKE_PAUSE, encoding="utf-8")
    # #4319: the packet scripts share interpreter resolution in this module.
    shutil.copyfile(ROOT / "src" / "ameesh" / "project_python.py",
                    package / "project_python.py")
    return tree


def _load_and_main(tree: Path, script: str) -> list[str]:
    return [sys.executable, "-c", script,
            str(tree / "scripts" / "paquet-horaire.py"), str(tree)]


_LOAD = '''\
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("paquet_horaire_4473", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["paquet_horaire_4473"] = module
spec.loader.exec_module(module)
'''

RUN_TICK = _LOAD + '''
rc = module.main(["tick", "--repo", sys.argv[2], "--target", "main", "--pilot", "p"])
print("RC", rc)
'''

RUN_WITH_STALE_IMPORTED = '''\
import importlib.util
import sys

sys.path.insert(0, sys.argv[3])
import ameesh
print("STALE", ameesh.__file__)
''' + _LOAD + '''
rc = module.main(["tick", "--repo", sys.argv[2], "--target", "main", "--pilot", "p"])
print("RC", rc)
'''


class UnitTest(unittest.TestCase):
    def test_the_rendered_unit_gives_the_tree_library_to_the_tick(self):
        text = role_timers.render_paquet_service(
            role="mesh-design", bin_path="/opt/ameesh/bin/ameesh")
        self.assertIn(
            "/usr/bin/env PYTHONPATH=${AMEESH_PAQUET_REPO}/src "
            "/opt/ameesh/bin/python ${AMEESH_PAQUET_REPO}/scripts/"
            "paquet-horaire.py tick", text)
        # l'interpréteur et le script restent ceux du venv et du clone
        self.assertIn("role run mesh-design -- /usr/bin/env", text)

    def test_the_deploy_copy_carries_the_same_binding(self):
        text = (ROOT / "deploy" / "systemd" /
                "ameesh-paquet-horaire.service").read_text(encoding="utf-8")
        # la copie manuelle coupe ses ExecStart par des « \ » de fin de ligne
        flat = " ".join(text.replace("\\\n", " ").split())
        self.assertIn(
            "/usr/bin/env PYTHONPATH=${AMEESH_PAQUET_REPO}/src "
            "%h/.local/share/ameesh/venv/bin/python "
            "${AMEESH_PAQUET_REPO}/scripts/paquet-horaire.py tick", flat)


class TreeLibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="4473-coherence-")
        self.base = Path(self.tmp.name)
        self.tree = _write_fake_tree(self.base)
        self.stale = self.base / "stale"
        (self.stale / "ameesh").mkdir(parents=True)
        (self.stale / "ameesh" / "__init__.py").write_text(
            '__version__ = "stale"\n', encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, script: str) -> str:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.stale)
        env["AMEESH_PYTHON"] = sys.executable
        proc = subprocess.run(
            _load_and_main(self.tree, script), env=env, cwd=str(self.tree),
            capture_output=True, text=True, timeout=120)
        return proc.stdout + proc.stderr

    def test_a_stale_installed_library_does_not_break_the_tick(self):
        """Le témoin de l'incident : sans l'arbre, « pause illisible »."""
        out = self._run(RUN_TICK)
        self.assertIn("TREE_PACKET_PAUSE", out)
        self.assertIn("TREE_PACKET_PAUSE_ALERT", out)
        self.assertIn("PYTHONPATH_FIRST=%s" % (self.tree / "src"), out)
        self.assertIn("RC 1", out)  # le faux clone n'a pas d'origin : rien n'est fait
        self.assertIn("fetch main", out)
        self.assertNotIn("cannot import name 'packet_pause'", out)
        self.assertNotIn("pause illisible", out)

    def test_a_stale_library_already_imported_is_refused_not_mixed(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.stale)
        env["AMEESH_PYTHON"] = sys.executable
        proc = subprocess.run(
            _load_and_main(self.tree, RUN_WITH_STALE_IMPORTED) + [str(self.stale)],
            env=env, cwd=str(self.tree), capture_output=True, text=True, timeout=120)
        out = proc.stdout + proc.stderr
        self.assertIn("STALE %s" % (self.stale / "ameesh" / "__init__.py"), out)
        self.assertIn("déjà chargée ailleurs", out)
        self.assertIn("RC 1", out)
        self.assertNotIn("TREE_PACKET_PAUSE", out)


if __name__ == "__main__":
    unittest.main()

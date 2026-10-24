# SPDX-License-Identifier: AGPL-3.0-only
"""#2763 : le venv de production d'ameesh n'est jamais exposé à un tour.

Incident du 19/10/2026 à 20:03 : un `uv pip install -e` lancé depuis un
worktree de relecture a visé le venv vivant de pc-smichea (VIRTUAL_ENV et
`venv-<sha7>/bin` hérités du lanceur). Témoins :

* l'environnement d'un tour ne porte ni VIRTUAL_ENV ni `bin/` d'un venv
  d'ameesh ; `ameesh` et `agent-mail` y restent joignables (shims) ;
* un échec d'écriture des shims ne rouvre pas le PATH de production (#2772) :
  le tour garde les entrées du reste du PATH, jamais le `bin/` du venv vivant ;
* `ameesh doctor` signale une installation éditable ou un ameesh chargé hors
  du venv ;
* un venv versionné préparé par `bascule.py` refuse pip, et rien ne peut y
  être créé (uv) ; il se retire quand même ;
* la consigne figure dans chaque tour de courrier.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ameesh import adapters, mail, venv_guard  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "bascule_2763", ROOT / "deploy/mise-a-jour/bascule.py")
bascule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bascule)


def _executable(path: Path, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


class Banc(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ameesh-2763-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.share = self.base / "share"
        self.vivant = self.share / "venv-abc1234"
        self.stable = self.share / "venv"
        for venv in (self.vivant, self.stable, self.share / "venv-0ld0000"):
            for nom in ("ameesh", "agent-mail", "pip", "python3"):
                _executable(venv / "bin" / nom)
        self.autre = self.base / "outils" / "bin"
        self.autre.mkdir(parents=True)

    def en_venv(self, prefix):
        return mock.patch.multiple(sys, prefix=str(prefix), base_prefix="/usr")


class ScrubTest(Banc):
    def test_concurrent_turns_keep_the_production_bin_hidden(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading

        shims = str(self.base / "shims")
        barrier = threading.Barrier(2)
        replace = os.replace

        def concurrent_replace(src, dst):
            if Path(dst).name == "ameesh":
                barrier.wait(timeout=5)
            return replace(src, dst)

        with self.en_venv(self.vivant), \
             mock.patch.object(venv_guard.os, "replace", side_effect=concurrent_replace), \
             ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: venv_guard.scrub(self.env(), shims), range(2)))
        for env in results:
            self.assertNotIn(str(self.vivant / "bin"), env["PATH"].split(os.pathsep))
            self.assertEqual(shims, env["PATH"].split(os.pathsep)[0])

    def env(self):
        return {"VIRTUAL_ENV": str(self.vivant),
                "PATH": os.pathsep.join([str(self.vivant / "bin"), str(self.autre),
                                         str(self.stable / "bin"), "/usr/bin"])}

    def test_tour_sans_venv_de_production_et_avec_ses_points_dentree(self):
        shims = self.base / "state" / ".menage" / venv_guard.SHIM_DIR
        with self.en_venv(self.vivant):
            env = venv_guard.scrub(self.env(), str(shims))
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertEqual([str(shims), str(self.autre), "/usr/bin"],
                         env["PATH"].split(os.pathsep))
        import shutil
        for nom in venv_guard.SHIMMED:
            trouve = shutil.which(nom, path=env["PATH"])
            self.assertEqual(str(shims / nom), trouve)
            self.assertIn(str(self.vivant / "bin" / nom), Path(trouve).read_text())
        # ni pip ni python3 du venv vivant (un pip système peut rester)
        self.assertNotEqual(str(self.vivant / "bin" / "pip"),
                            shutil.which("pip", path=env["PATH"]))
        # idempotent
        with self.en_venv(self.vivant):
            again = venv_guard.scrub(dict(env), str(shims))
        self.assertEqual(env, again)

    def test_shims_visent_le_lanceur_stable_et_ne_dependent_pas_du_runner(self):
        """#3311 : deux runners de versions différentes écrivent le même shim.

        Le shim vise le lanceur stable `venv/bin/<nom>`, qui résout
        `venv-courant` à chaque appel : le dernier runner qui démarre un tour
        ne décide plus de la version des outils du tour.
        """
        self.assertEqual(bascule.LAUNCHERS, venv_guard.BASCULE_MARKER)
        (self.stable / venv_guard.BASCULE_MARKER).write_text("{}", encoding="utf-8")
        (self.share / "venv-courant").symlink_to(self.vivant.name)
        appele = self.base / "appele"
        (self.stable / "bin/ameesh").write_text(
            "#!/bin/sh\nprintf '%%s' \"$0\" > %s\n" % appele,
            encoding="utf-8")
        (self.stable / "bin/ameesh").chmod(0o755)
        shims = self.base / "state" / ".menage" / venv_guard.SHIM_DIR
        with self.en_venv(self.vivant):
            env = venv_guard.scrub(self.env(), str(shims))
        textes = {}
        for nom in venv_guard.SHIMMED:
            texte = (shims / nom).read_text(encoding="utf-8")
            textes[nom] = texte
            self.assertIn(str(self.stable / "bin" / nom), texte)
            self.assertNotIn(str(self.vivant), texte)
        # le shim exécute bien le lanceur stable
        import shutil
        trouve = shutil.which("ameesh", path=env["PATH"])
        self.assertEqual(str(shims / "ameesh"), trouve)
        subprocess.run([trouve], check=True, timeout=10)
        self.assertEqual(str(self.stable / "bin/ameesh"),
                         appele.read_text(encoding="utf-8"))
        # un second runner, autre venv versionné, écrit exactement le même shim
        autre = self.share / "venv-def5678"
        for nom in venv_guard.SHIMMED:
            _executable(autre / "bin" / nom)
        env2 = {"VIRTUAL_ENV": str(autre),
                "PATH": os.pathsep.join(
                    [str(autre / "bin"), str(self.autre), "/usr/bin"])}
        with self.en_venv(autre):
            venv_guard.scrub(env2, str(shims))
        for nom in venv_guard.SHIMMED:
            self.assertEqual(textes[nom],
                             (shims / nom).read_text(encoding="utf-8"))

    def test_lanceur_stable_absent_retombe_sur_le_venv_du_runner(self):
        """#3311 : le repli par point d'entrée garde `agent-mail` joignable."""
        (self.stable / venv_guard.BASCULE_MARKER).write_text("{}", encoding="utf-8")
        (self.stable / "bin/agent-mail").unlink()
        shims = self.base / "state" / ".menage" / venv_guard.SHIM_DIR
        with self.en_venv(self.vivant):
            venv_guard.scrub(self.env(), str(shims))
        self.assertIn(str(self.stable / "bin/ameesh"),
                      (shims / "ameesh").read_text(encoding="utf-8"))
        self.assertIn(str(self.vivant / "bin/agent-mail"),
                      (shims / "agent-mail").read_text(encoding="utf-8"))

    def test_sans_dossier_de_shims_le_path_est_seulement_nettoye(self):
        with self.en_venv(self.vivant):
            env = venv_guard.scrub(self.env())
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertEqual([str(self.autre), "/usr/bin"], env["PATH"].split(os.pathsep))

    def test_shims_impossibles_le_path_de_production_reste_ferme(self):
        """#2772 : un échec d'écriture des shims ne rouvre jamais le PATH.

        Le tour peut perdre `ameesh`/`agent-mail` s'ils ne viennent pas d'un
        autre dossier du PATH ; le `bin/` du venv vivant, lui, ne revient pas.
        """
        _executable(self.autre / "ameesh")
        with self.en_venv(self.vivant), \
             mock.patch.object(venv_guard, "install_shims", side_effect=OSError("ro")):
            with self.assertLogs(venv_guard.LOG, level="WARNING") as journal:
                env = venv_guard.scrub(self.env(), str(self.base / "ro"))
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertEqual([str(self.autre), "/usr/bin"],
                         env["PATH"].split(os.pathsep))
        self.assertTrue(any("production venv hidden" in ligne
                            for ligne in journal.output), journal.output)
        # un point d'entrée fourni par un autre dossier du PATH reste joignable
        import shutil
        self.assertEqual(str(self.autre / "ameesh"),
                         shutil.which("ameesh", path=env["PATH"]))
        # ni pip ni python3 du venv vivant ne le sont
        self.assertNotEqual(str(self.vivant / "bin" / "pip"),
                            shutil.which("pip", path=env["PATH"]))
        self.assertNotEqual(str(self.vivant / "bin" / "python3"),
                            shutil.which("python3", path=env["PATH"]))
        # idempotent : une seconde passe sur un échec persistant ne change rien
        with self.en_venv(self.vivant), \
             mock.patch.object(venv_guard, "install_shims", side_effect=OSError("ro")):
            with self.assertLogs(venv_guard.LOG, level="WARNING"):
                again = venv_guard.scrub(dict(env), str(self.base / "ro"))
        self.assertEqual(env, again)

    def test_shims_impossibles_sur_le_disque_le_path_reste_ferme(self):
        """#2772 : l'échec réel (dossier impossible) ne rouvre pas le PATH."""
        obstacle = self.base / "ro"
        obstacle.write_text("pas un dossier\n", encoding="utf-8")
        with self.en_venv(self.vivant):
            with self.assertLogs(venv_guard.LOG, level="WARNING"):
                env = venv_guard.scrub(self.env(), str(obstacle))
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertEqual([str(self.autre), "/usr/bin"],
                         env["PATH"].split(os.pathsep))

    def test_hors_venv_rien_ne_change(self):
        env = self.env()
        with mock.patch.multiple(sys, prefix="/usr", base_prefix="/usr"):
            self.assertEqual(self.env(), venv_guard.scrub(env, str(self.base / "x")))

    def test_virtual_env_dun_autre_venv_garde(self):
        env = {"VIRTUAL_ENV": str(self.base / "projet" / ".venv"), "PATH": "/usr/bin"}
        with self.en_venv(self.vivant):
            venv_guard.scrub(env)
        self.assertEqual(str(self.base / "projet" / ".venv"), env["VIRTUAL_ENV"])

    def test_runner_attach_et_resident_appellent_le_garde(self):
        runner = (ROOT / "src/ameesh/runner.py").read_text()
        self.assertEqual(2, runner.count("venv_guard.scrub(env, venv_guard.shim_dir("))
        self.assertIn("venv_guard.scrub(env)", (ROOT / "src/ameesh/resident.py").read_text())


class DoctorTest(Banc):
    def site(self, prefix):
        site = prefix / "lib" / "python3.14" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        return site

    def test_installation_editable_signalee(self):
        site = self.site(self.vivant)
        (site / "__editable__.ameesh-1.13.0.pth").write_text("/tmp/main-2696/src\n")
        info = site / "ameesh-1.13.0.dist-info"
        info.mkdir()
        (info / "direct_url.json").write_text(json.dumps(
            {"url": "file:///tmp/main-2696", "dir_info": {"editable": True}}))
        problemes = venv_guard.installation_problems(str(self.vivant))
        self.assertTrue(any("__editable__.ameesh-1.13.0.pth" in p for p in problemes))
        self.assertTrue(any("file:///tmp/main-2696" in p for p in problemes))
        # le code chargé (src/ du dépôt) n'est pas celui du venv
        self.assertTrue(any("loaded outside the venv" in p for p in problemes))

    def test_installation_deployee_saine(self):
        site = self.site(self.vivant)
        info = site / "ameesh-1.13.0.dist-info"
        info.mkdir()
        (info / "direct_url.json").write_text(json.dumps(
            {"url": "file:///home/x/src-abc1234", "dir_info": {}}))
        import ameesh
        with mock.patch.object(ameesh, "__file__", str(site / "ameesh" / "__init__.py")):
            self.assertEqual([], venv_guard.installation_problems(str(self.vivant)))

    def test_venv_doutils_sans_ameesh_installe_reste_vert(self):
        self.site(self.vivant)                 # CI : PYTHONPATH=src, rien d'installé
        self.assertEqual([], venv_guard.installation_problems(str(self.vivant)))

    def test_src_de_deploiement_autorise_et_worktree_signale(self):
        # `poste.sh` sonde le code cible depuis son instantané src-<sha>,
        # voisin du venv, avant de l'installer (B/P1 de la revue #2763)
        site = self.site(self.vivant)
        (site / "ameesh-1.13.0.dist-info").mkdir()
        import ameesh
        deploye = self.share / "src-abc1234" / "src" / "ameesh" / "__init__.py"
        deploye.parent.mkdir(parents=True)
        deploye.write_text("")
        with mock.patch.object(ameesh, "__file__", str(deploye)):
            self.assertEqual([], venv_guard.installation_problems(str(self.vivant)))
        # un worktree de relecture, même chargé par PYTHONPATH, reste signalé
        worktree = self.base / "worktree" / "src" / "ameesh" / "__init__.py"
        worktree.parent.mkdir(parents=True)
        worktree.write_text("")
        with mock.patch.object(ameesh, "__file__", str(worktree)):
            problemes = venv_guard.installation_problems(str(self.vivant))
        self.assertTrue(any("loaded outside the venv" in p for p in problemes))

    def test_doctor_rouge_sur_un_ecart(self):
        texte = (ROOT / "src/ameesh/cli.py").read_text()
        debut = texte.index("def cmd_doctor")
        corps = texte[debut:texte.index("\ndef ", debut + 1)]
        self.assertIn("venv_guard.installation_problems()", corps)


class BasculeTest(Banc):
    def test_venv_prepare_refuse_pip_et_toute_creation_puis_se_retire(self):
        venv = self.base / "pose" / "venv"            # venv réel d'avant #1969
        _executable(venv / "bin" / "python", '#!/bin/sh\n[ "$1" = "-c" ] && exit 0\n')
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (self.base / "pose" / "src-v2").mkdir()

        def creer(directory, python):
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", directory],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for nom in ("pip", "pip3"):
                _executable(Path(directory) / "bin" / nom, "#!/bin/sh\necho installe\n")
            _executable(Path(directory) / "bin" / "ameesh")

        args = SimpleNamespace(venv=str(venv), src=str(self.base / "pose" / "src-v2"),
                               version="v2", python=sys.executable)
        with mock.patch.object(bascule, "_creer_venv", side_effect=creer), \
             mock.patch.object(bascule, "_pip_install"), \
             mock.patch.object(bascule, "_controles"):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, bascule.preparer(args))
        cible = self.base / "pose" / "venv-v2"
        self.assertTrue((cible / bascule.MARKER).is_file())
        for nom in ("pip", "pip3"):
            proc = subprocess.run([str(cible / "bin" / nom), "install", "-e", "."],
                                  capture_output=True, text=True)
            self.assertEqual(1, proc.returncode)
            self.assertIn("pip est interdit", proc.stderr)
        site = next(cible.glob("lib/python3*/site-packages"))
        if os.geteuid() != 0:
            with self.assertRaises(PermissionError):
                (site / "__editable__.ameesh-1.13.0.pth").write_text("x")
            with self.assertRaises(PermissionError):
                (cible / "bin" / "ameesh").write_text("x")
        # le venv s'active toujours, et se retire malgré le scellé
        self.assertEqual(0, bascule.main(["activer", "--venv", str(venv), "--version", "v2"]))
        bascule._supprimer(str(cible))
        self.assertFalse(cible.exists())


class ConsigneTest(unittest.TestCase):
    def test_chaque_tour_de_courrier_porte_la_consigne(self):
        self.assertIn("PYTHONPATH=src", adapters.VENV_NOTE)
        self.assertIn("$TMPDIR", adapters.VENV_NOTE)
        texte = mail._assemble("mail", ["— message"], 0)
        self.assertIn(adapters.VENV_NOTE, texte)


if __name__ == "__main__":
    unittest.main()

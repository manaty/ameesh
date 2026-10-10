# SPDX-License-Identifier: AGPL-3.0-only
"""L63 : couche plateforme (`ameesh.platform`), adoption fail-closed, lint.

`PlatformTest` et `LintTest` tournent sans base : ce sont eux que la CI lance
aussi sous macOS (job `platform-macos`). Sous Linux, chacun passe deux fois : avec psutil (s'il est
installé) et par le repli /proc (psutil retiré).
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ameesh import platform

from .support import PgTestCase

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src", "ameesh")


def _variants():
    """(nom, contexte) : psutil tel quel, puis repli /proc sous Linux."""
    out = []
    if platform.has_psutil():
        out.append(("psutil", lambda: mock.patch.object(platform, "psutil", platform.psutil)))
    if platform.is_linux():
        out.append(("repli /proc", lambda: mock.patch.object(platform, "psutil", None)))
    return out


class PlatformTest(unittest.TestCase):
    def setUp(self) -> None:
        self.variants = _variants()
        if not self.variants:
            self.skipTest("ni psutil ni /proc : couche plateforme muette (NotAvailable)")

    def _child(self, code: str, *args: str) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", code, *args])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        return proc

    def _wait(self, predicate, timeout: float = 10.0) -> None:
        fin = time.monotonic() + timeout
        while not predicate():
            self.assertLess(time.monotonic(), fin, "délai dépassé")
            time.sleep(0.05)

    def test_systeme_connu(self):
        self.assertIn(platform.SYSTEM, ("linux", "macos", "windows", sys.platform))
        self.assertEqual(sum([platform.is_linux(), platform.is_macos(),
                              platform.is_windows()]), 1 if platform.SYSTEM in
                         ("linux", "macos", "windows") else 0)

    def test_ascendance(self):
        for nom, ctx in self.variants:
            with self.subTest(nom), ctx():
                chain = platform.ancestry()
                self.assertEqual(chain[0], os.getpid())
                self.assertIn(os.getppid(), chain)
                self.assertNotIn(1, chain)
                self.assertNotIn(0, chain)
                self.assertEqual(platform.ancestry(os.getppid())[0], os.getppid())

    def test_heure_de_demarrage_en_secondes_epoch(self):
        for nom, ctx in self.variants:
            with self.subTest(nom), ctx():
                debut = platform.start_time(os.getpid())
                self.assertIsInstance(debut, float)
                self.assertLessEqual(debut, time.time() + platform.process.START_TOLERANCE_S)
                self.assertGreater(debut, platform.boot_time() - 1)
                self.assertIsNone(platform.start_time(2 ** 22 + 12345))
                self.assertIsNone(platform.start_time(None))
                enfant = self._child("import time; time.sleep(60)")
                autre = platform.start_time(enfant.pid)
                self.assertGreaterEqual(autre, debut - 1)
                self.assertTrue(platform.same_start(autre, autre + 0.5))
                self.assertFalse(platform.same_start(autre, autre + 5))
                self.assertFalse(platform.same_start(autre, None))

    def test_les_deux_voies_concordent(self):
        if len(self.variants) < 2:
            self.skipTest("une seule voie disponible")
        mesures = []
        for _nom, ctx in self.variants:
            with ctx():
                mesures.append(platform.start_time(os.getpid()))
        self.assertTrue(platform.same_start(mesures[0], mesures[1]), mesures)

    @unittest.skipUnless(platform.is_linux(), "tops d'horloge : Linux")
    def test_conversion_des_tops(self):
        hertz = os.sysconf("SC_CLK_TCK")
        debut = platform.start_time(os.getpid())
        tops = round((debut - platform.boot_time()) * hertz)
        self.assertTrue(platform.same_start(platform.ticks_to_epoch(tops), debut))
        with mock.patch.object(platform, "SYSTEM", "macos"):
            with self.assertRaises(platform.NotAvailable):
                platform.ticks_to_epoch(tops)

    def test_vivant(self):
        for nom, ctx in self.variants:
            with self.subTest(nom), ctx():
                enfant = self._child("import time; time.sleep(60)")
                self.assertTrue(platform.alive(enfant.pid))
                enfant.kill()
                enfant.wait()
                self.assertFalse(platform.alive(enfant.pid))
                self.assertTrue(platform.alive(os.getpid()))

    def test_detenteurs_fichier_ouvert_et_ligne_de_commande(self):
        with tempfile.TemporaryDirectory() as tmp:
            chemin = os.path.join(tmp, "session.jsonl")
            open(chemin, "w").close()
            pret = os.path.join(tmp, "pret")
            tient = self._child(
                "import sys, time; fh = open(sys.argv[1]); open(sys.argv[2], 'w').close();"
                " time.sleep(60)", chemin, pret)
            nomme = self._child("import time; time.sleep(60)", "--resume", "sid-l63-xyz")
            self._wait(lambda: os.path.exists(pret))
            for nom, ctx in self.variants:
                with self.subTest(nom), ctx():
                    found = {h["pid"]: h for h in platform.holders(chemin, "sid-l63-xyz")}
                    self.assertEqual(found[tient.pid]["why"], "fichier ouvert")
                    self.assertIn(nomme.pid, found)
                    self.assertIn("ligne de commande", found[nomme.pid]["why"])
                    # les PID exclus (ascendance d'adopt) ne comptent pas par leur
                    # ligne de commande
                    exclus = platform.holders(chemin, "sid-l63-xyz", exclude=[nomme.pid])
                    self.assertNotIn(nomme.pid, [h["pid"] for h in exclus])
                    # personne ne tient un autre fichier
                    autre = os.path.join(tmp, "autre.jsonl")
                    open(autre, "w").close()
                    self.assertEqual(platform.holders(autre, "sid-absent"), [])

    def test_detenteurs_d_un_dossier(self):
        with tempfile.TemporaryDirectory() as tmp:
            dossier = os.path.join(tmp, "session")
            os.makedirs(dossier)
            dedans = self._child("import time; time.sleep(60)")
            enfant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                      cwd=dossier)
            self.addCleanup(lambda: (enfant.kill(), enfant.wait()))
            del dedans
            for nom, ctx in self.variants:
                with self.subTest(nom), ctx():
                    found = {h["pid"]: h for h in platform.holders(dossier)}
                    self.assertEqual(found[enfant.pid]["why"], "dossier courant")

    def test_os_muet_leve_not_available(self):
        """Ni psutil ni /proc : jamais de liste vide, jamais de None muet."""
        with mock.patch.object(platform, "psutil", None), \
                mock.patch.object(platform, "SYSTEM", "macos"):
            for appel in (lambda: platform.holders(__file__, "x"),
                          lambda: platform.ancestry(),
                          lambda: platform.start_time(os.getpid()),
                          lambda: platform.boot_time(),
                          lambda: platform.memory(),
                          lambda: platform.open_fd_count()):
                with self.assertRaises(platform.NotAvailable) as ctx:
                    appel()
                self.assertEqual(ctx.exception.system, "macos")
            # mesures non sensibles : inconnues, sans erreur
            self.assertIsNone(platform.human_tty())
            self.assertEqual(platform.power(), {"on_ac": None, "battery_percent": None})

    def test_detenteurs_illisibles_pour_soi_meme(self):
        if not platform.has_psutil():
            self.skipTest("psutil absent")

        class Refus(Exception):
            pass

        class Muet:
            def open_files(self):
                raise Refus("refusé")

        faux = mock.MagicMock()
        faux.Process.return_value = Muet()
        with mock.patch.object(platform, "psutil", faux):
            with self.assertRaises(platform.NotAvailable):
                platform.holders(__file__, "x")

    def test_mesures_de_l_hote(self):
        for nom, ctx in self.variants:
            with self.subTest(nom), ctx():
                mem = platform.memory()
                self.assertGreater(mem.get("mem_available_bytes", 1), 0)
                charge = platform.load_average()
                self.assertTrue(charge is None or charge >= 0)
                self.assertIsInstance(platform.power(), dict)
                self.assertIn(platform.mount_of(tempfile.gettempdir()) is None, (True, False))
                self.assertGreater(platform.open_fd_count(), 0)
                tty = platform.human_tty()
                self.assertTrue(tty is None or tty.startswith("/dev/"))

    def test_point_de_montage_et_alimentation_depuis_des_fichiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Une table de montage porte des chemins réels ; sur macOS le
            # dossier temporaire passe par un lien (/var -> /private/var).
            tmp = os.path.realpath(tmp)
            mounts = os.path.join(tmp, "mounts")
            with open(mounts, "w") as fh:
                fh.write("rootfs / ext4 rw 0 0\ntmpfs %s tmpfs rw 0 0\n" % tmp)
            self.assertEqual(platform.mount_of(os.path.join(tmp, "x"), mounts), (tmp, "tmpfs"))
            bat = os.path.join(tmp, "power", "BAT0")
            os.makedirs(bat)
            for nom, valeur in (("type", "Battery"), ("capacity", "42"),
                                ("status", "Discharging")):
                with open(os.path.join(bat, nom), "w") as fh:
                    fh.write(valeur)
            self.assertEqual(platform.power(os.path.join(tmp, "power")),
                             {"on_ac": False, "battery_percent": 42.0})

    def test_alimentation_psutil_hors_linux(self):
        if not platform.has_psutil():
            self.skipTest("psutil absent")
        faux = mock.MagicMock()
        faux.sensors_battery.return_value = mock.Mock(percent=73.0, power_plugged=True)
        with mock.patch.object(platform, "psutil", faux), \
                mock.patch.object(platform, "SYSTEM", "macos"):
            self.assertEqual(platform.power(), {"on_ac": True, "battery_percent": 73.0})
            faux.sensors_battery.return_value = None
            self.assertEqual(platform.power(), {"on_ac": None, "battery_percent": None})


# --------------------------------------------------------------------------
# adopt : jamais fail-open
# --------------------------------------------------------------------------

class AdoptFailClosedTest(PgTestCase):
    """Sans moyen de vérifier qu'une session est fermée, `adopt` refuse ;
    `--force` passe, mais l'avertissement va au fil (journalisé)."""

    def test_adopt_refuse_puis_force_journalise(self):
        import dataclasses
        from ameesh import registry, reprise
        from .test_l39_adoption_reprise import SID, _rollout
        comptes_dir = os.path.join(self.tmp, "comptes", "codex-1")
        os.makedirs(comptes_dir, mode=0o700)
        open(os.path.join(comptes_dir, "auth.json"), "w").close()
        os.chmod(os.path.join(comptes_dir, "auth.json"), 0o600)
        travail = os.path.join(self.tmp, "travail")
        os.makedirs(travail)
        _rollout(comptes_dir, SID, travail)
        cfg = dataclasses.replace(
            self.cfg, accounts={"codex": [{"name": "primaire", "path": comptes_dir}]})
        registry.upsert(self.db, "coord", harness="codex", host=cfg.host, mode="externe")
        muet = platform.NotAvailable("détenteurs d'un fichier", "test", "macos")
        with mock.patch.object(platform, "holders", side_effect=muet):
            with self.assertRaises(reprise.RepriseError) as ctx:
                reprise.adopt(cfg, self.db, "coord", session_id=SID, harness="codex")
            self.assertIn("impossible de vérifier", str(ctx.exception))
            self.assertEqual(registry.get(self.db, "coord")["mode"], "externe")
            avert: list[str] = []
            out = reprise.adopt(cfg, self.db, "coord", session_id=SID, harness="codex",
                                force=True, warn=avert.append)
        self.assertTrue(out["forced"])
        self.assertTrue(any("sans vérifier" in a for a in avert))
        self.assertTrue(any("sans vérifier" in n for n in out["notes"]))
        texte = []
        for racine, _d, fichiers in os.walk(self.cfg.threads_root):
            for nom in fichiers:
                with open(os.path.join(racine, nom), encoding="utf-8", errors="ignore") as fh:
                    texte.append(fh.read())
        self.assertIn("sans vérifier qu'elle est fermée", "\n".join(texte))
        self.assertEqual(registry.get(self.db, "coord")["mode"], "execute")


# --------------------------------------------------------------------------
# lint : l'OS ne se lit que dans ameesh.platform
# --------------------------------------------------------------------------

#: fichiers (relatifs à src/ameesh) encore autorisés à lire l'OS directement,
#: avec la raison. Cette liste ne peut que DIMINUER : `MAX_EXCEPTIONS` en
#: borne la taille, et une exception devenue inutile fait échouer le test.
EXCEPTIONS: dict[str, str] = {}
MAX_EXCEPTIONS = 0


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def os_accesses(source: str) -> list[str]:
    """Les accès directs à l'OS d'un source Python : `sys.platform`, import
    du module standard `platform`, chaîne contenant `/proc` (hors docstring)."""
    tree = ast.parse(source)
    skip = _docstrings(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "platform" \
                and isinstance(node.value, ast.Name) and node.value.id == "sys":
            found.append("ligne %d : sys.platform" % node.lineno)
        elif isinstance(node, ast.Import):
            if any(alias.name == "platform" for alias in node.names):
                found.append("ligne %d : import platform" % node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == "platform":
                found.append("ligne %d : from platform import" % node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in skip and "/proc" in node.value:
            found.append("ligne %d : %r" % (node.lineno, node.value[:40]))
    return found


class LintTest(unittest.TestCase):
    def _violations(self) -> dict[str, list[str]]:
        out = {}
        for racine, _dirs, fichiers in os.walk(SRC):
            rel_root = os.path.relpath(racine, SRC)
            if rel_root.split(os.sep)[0] == "platform":
                continue
            for nom in fichiers:
                if not nom.endswith(".py"):
                    continue
                chemin = os.path.join(racine, nom)
                with open(chemin, encoding="utf-8") as fh:
                    found = os_accesses(fh.read())
                if found:
                    out[os.path.relpath(chemin, SRC)] = found
        return out

    def test_l_os_ne_se_lit_que_dans_la_couche_plateforme(self):
        violations = {k: v for k, v in self._violations().items() if k not in EXCEPTIONS}
        self.assertEqual(violations, {}, "accès direct à l'OS hors de ameesh.platform : "
                                         "passez par la couche plateforme (L63)")

    def test_la_liste_d_exceptions_ne_peut_que_diminuer(self):
        self.assertLessEqual(len(EXCEPTIONS), MAX_EXCEPTIONS)
        restantes = self._violations()
        inutiles = [k for k in EXCEPTIONS if k not in restantes]
        self.assertEqual(inutiles, [], "exception devenue inutile : retirez-la et "
                                       "baissez MAX_EXCEPTIONS")

    def test_le_lint_voit_ce_qu_il_doit_voir(self):
        self.assertEqual(len(os_accesses(
            '"""doc /proc"""\nimport sys, platform\nfrom platform import system\n'
            'x = sys.platform\ny = open("/proc/self/stat")\n')), 4)
        self.assertEqual(os_accesses('from . import platform\nplatform.is_linux()\n'), [])


if __name__ == "__main__":
    unittest.main()

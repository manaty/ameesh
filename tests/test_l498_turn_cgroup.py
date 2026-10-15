# SPDX-License-Identifier: AGPL-3.0-only
"""L498 (#498) : cgroup v2 délégué par tour — primitives, trampoline, suivi.

Trois étages, sans jamais toucher un cgroup réel en dehors du témoin
explicitement autorisé (`TemoinCgroupReelTest`, qui se saute de lui-même
quand la délégation manque) :

* les primitives de `ameesh.turn_cgroup` contre une **fausse** monture cgroup
  construite dans un dossier temporaire (`root_dir=`) : lecture de
  `/proc/self/cgroup`, validation des identifiants, refus des liens,
  `cgroup.kill`, `cgroup.events`, `memory.peak`, ligne de commande ;
* le trampoline lui-même, en sous-processus hermétique : sans placement
  possible il doit refuser (125) sans exécuter l'argument ;
* l'exécuteur et le travail de fond, par mocks : READY, sonde unique de la
  racine déléguée, réconciliation des orphelins, rattachement et fermeture de
  la ressource du tour, survivants et routage du nettoyage.

Non couvert ici : le chemin de lancement réel de `AgentWorker._run_turn`
(`src/ameesh/runner.py:3961-4029`) — l'atteindre demande une base, un bail,
un harnais et tout le préambule du tour. La garantie « sans racine déléguée,
la commande n'est pas enveloppée et aucun tuyau n'est créé » y est portée par
la garde `if cgroup_path is not None` : `cgroup_path` n'est posé que sous
`root = turn_cgroup_root()` non None. Ce fichier couvre la sonde qui rend
None sans rien créer (`SonTurnCgroupRootTest`) et la ligne de commande du
trampoline (`test_commande_du_trampoline`) ; l'assertion « `turn_cgroup.
command` n'est jamais appelée quand la racine est None » demanderait le tour
complet, hors de portée d'un test unitaire.
"""
from __future__ import annotations

import contextlib
import io
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from . import support  # isole l'environnement de l'hôte (L142)
from ameesh import background, turn_cgroup
from ameesh.runner import AgentWorker, Runner

#: racine du dépôt : `cwd` et `PYTHONPATH` des sous-processus hermétiques
REPO = support.REPO


# ==========================================================================
# A. primitives pures, contre une fausse monture cgroup
# ==========================================================================

class FausseMontureTest(unittest.TestCase):
    """Base : une fausse monture cgroup et un dossier « dehors » témoin.

    Rien n'est jamais lu ni écrit sous `/sys/fs/cgroup` : `root_dir=` pointe
    la fausse monture, et le dossier extérieur sert à prouver qu'un lien
    symbolique ne fait pas sortir des chemins autorisés."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l498-monture-")
        self.base = os.path.realpath(self._tmp.name)
        self._dehors = tempfile.TemporaryDirectory(prefix="l498-dehors-")
        self.outside = os.path.realpath(self._dehors.name)
        with open(os.path.join(self.outside, "temoin"), "w", encoding="utf-8") as fh:
            fh.write("intact\n")

    def tearDown(self) -> None:
        self._dehors.cleanup()
        self._tmp.cleanup()

    def fake(self, rel: str, files: dict | None = None) -> str:
        """Crée le cgroup `rel` (chemin de monture) et ses fichiers ; rend le
        chemin relatif à la monture."""
        mount = "/" + str(rel).strip("/")
        directory = os.path.join(self.base, mount.lstrip("/"))
        os.makedirs(directory, exist_ok=True)
        for name, content in (files or {}).items():
            with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
                fh.write(content)
        return mount

    def kernel(self, mount: str) -> str:
        """Le chemin noyau (faux) d'un chemin de monture."""
        return os.path.join(self.base, mount.lstrip("/"))


class CheminsTest(FausseMontureTest):
    """A.1–A.4 : `own_cgroup`, `delegated_root`, identifiants, liens."""

    def test_own_cgroup_lit_la_ligne_unifiee(self):
        proc = Path(self.base) / "proc-cgroup"
        proc.write_text("1:name=systemd:/ignore\n0::/user.slice/x.service/runner\n",
                        encoding="utf-8")
        self.assertEqual(turn_cgroup.own_cgroup(str(proc)),
                         "/user.slice/x.service/runner")

    def test_own_cgroup_absent_ou_sans_ligne_unifiee(self):
        self.assertIsNone(turn_cgroup.own_cgroup(str(Path(self.base) / "absent")))
        legacy = Path(self.base) / "legacy"
        legacy.write_text("1:name=systemd:/x\n2:cpu:/y\n", encoding="utf-8")
        self.assertIsNone(turn_cgroup.own_cgroup(str(legacy)))
        racine = Path(self.base) / "racine"
        racine.write_text("0::\n", encoding="utf-8")
        self.assertEqual(turn_cgroup.own_cgroup(str(racine)), "/")

    def test_delegated_root_exige_le_sous_groupe(self):
        self.assertEqual(turn_cgroup.delegated_root("/user.slice/x.service/runner"),
                         "/user.slice/x.service")
        self.assertEqual(turn_cgroup.delegated_root("/a/b/runner/"), "/a/b")
        self.assertEqual(turn_cgroup.delegated_root("/runner"), "/")
        self.assertEqual(turn_cgroup.delegated_root("/a/b/autre", subgroup="autre"),
                         "/a/b")
        for own in (None, "", "a/b/runner", "/a/b", "/a/runner-x",
                    "/user.slice/x.service"):
            with self.subTest(own=own):
                self.assertIsNone(turn_cgroup.delegated_root(own))

    def test_turn_name_valide_et_refuse(self):
        self.assertEqual(turn_cgroup.turn_name("abc"), "turn-abc")
        self.assertEqual(turn_cgroup.turn_name("a" * 120), "turn-" + "a" * 120)
        for mauvais in ("", "..", "a/b", "a b", "a" * 121, "a;b", "é"):
            with self.subTest(mauvais=mauvais):
                with self.assertRaises(ValueError):
                    turn_cgroup.turn_name(mauvais)

    def test_turn_id_of_et_path_for(self):
        self.assertEqual(turn_cgroup.turn_id_of("turn-abc"), "abc")
        self.assertEqual(turn_cgroup.turn_id_of("turn-a_b-9"), "a_b-9")
        for name in ("autre", "turn-", "turn-a/b", "turn-" + "a" * 121, "turn-.."):
            with self.subTest(name=name):
                self.assertEqual(turn_cgroup.turn_id_of(name), "")
        self.assertEqual(turn_cgroup.path_for("/u", "abc"), "/u/turn-abc")
        self.assertEqual(turn_cgroup.path_for("/u/", "abc"), "/u/turn-abc")
        with self.assertRaises(ValueError):
            turn_cgroup.path_for("/u", "..")

    def test_identifiant_traversant_refuse(self):
        root = self.fake("/u", {"cgroup.procs": ""})
        with self.assertRaises(ValueError):
            turn_cgroup.path_for(root, "..")
        self.assertIsNone(turn_cgroup.create(root, "..", root_dir=self.base))
        self.assertIsNone(turn_cgroup.create(root, "../evade", root_dir=self.base))
        self.assertIsNone(turn_cgroup.create(root, "", root_dir=self.base))
        # rien n'a été créé à côté de la racine
        self.assertEqual(sorted(os.listdir(self.base)), ["u"])

    def test_lien_symbolique_ne_fait_pas_sortir_de_la_monture(self):
        root = self.fake("/u", {"cgroup.procs": ""})
        lien = "/u/turn-evil"
        os.symlink(self.outside, os.path.join(self.kernel(root), "turn-evil"))
        self.assertIsNone(turn_cgroup.create(root, "evil", root_dir=self.base))
        self.assertIsNone(turn_cgroup.populated(lien, root_dir=self.base))
        self.assertFalse(turn_cgroup.remove(lien, root_dir=self.base))
        self.assertFalse(turn_cgroup.kill(lien, timeout=0.1, root_dir=self.base))
        self.assertFalse(turn_cgroup.usable(lien, root_dir=self.base))
        # la cible du lien n'a jamais été touchée
        self.assertEqual(os.listdir(self.outside), ["temoin"])
        # une racine qui est elle-même un lien n'est jamais utilisable
        os.symlink(self.outside, os.path.join(self.base, "lien-racine"))
        self.assertIsNone(turn_cgroup.create("/lien-racine", "x", root_dir=self.base))
        self.assertFalse(turn_cgroup.usable("/lien-racine", root_dir=self.base))
        self.assertFalse(turn_cgroup.remove("/lien-racine", root_dir=self.base))
        self.assertEqual(os.listdir(self.outside), ["temoin"])

    def test_enter_ecrit_le_pid_et_relit_la_position(self):
        self.fake("/u", {"cgroup.procs": ""})
        turn = self.fake("/u/turn-a", {"cgroup.procs": ""})
        proc = Path(self.base) / "proc"
        proc.write_text("0::/u/turn-a\n", encoding="utf-8")
        self.assertTrue(turn_cgroup.enter(turn, proc=str(proc), root_dir=self.base))
        self.assertEqual((Path(self.kernel(turn)) / "cgroup.procs").read_text().strip(),
                         str(os.getpid()))
        proc.write_text("0::/u/autre\n", encoding="utf-8")
        self.assertFalse(turn_cgroup.enter(turn, proc=str(proc), root_dir=self.base))
        self.assertFalse(turn_cgroup.enter("/absent", proc=str(proc), root_dir=self.base))

    def test_root_for_runner_utilisable_ou_refuse(self):
        proc = Path(self.base) / "proc"
        proc.write_text("0::/unite/runner\n", encoding="utf-8")
        self.fake("/unite", {"cgroup.kill": "", "cgroup.events": "populated 0\n",
                             "cgroup.procs": ""})
        self.assertEqual(turn_cgroup.root_for_runner(proc=str(proc), root_dir=self.base),
                         ("/unite", ""))
        proc.write_text("0::/ailleurs\n", encoding="utf-8")
        root, raison = turn_cgroup.root_for_runner(proc=str(proc), root_dir=self.base)
        self.assertIsNone(root)
        self.assertIn("DelegateSubgroup=runner", raison)
        proc.write_text("0::/unite/runner\n", encoding="utf-8")
        (Path(self.kernel("/unite")) / "cgroup.procs").write_text("42\n", encoding="utf-8")
        root, raison = turn_cgroup.root_for_runner(proc=str(proc), root_dir=self.base)
        self.assertIsNone(root)
        self.assertIn("not usable", raison)


class FauxCgroupTest(FausseMontureTest):
    """A.5–A.8 : `usable`, `create`/`populated`/`remove`/`orphans`, `kill`,
    `memory_peak` — tout contre la fausse monture."""

    def test_usable_exige_la_racine_et_ses_fichiers(self):
        mount = self.fake("/u", {"cgroup.kill": "", "cgroup.events": "populated 0\n",
                                 "cgroup.procs": ""})
        self.assertTrue(turn_cgroup.usable(mount, root_dir=self.base))
        procs = Path(self.kernel(mount)) / "cgroup.procs"
        procs.write_text("42\n", encoding="utf-8")
        self.assertFalse(turn_cgroup.usable(mount, root_dir=self.base))
        procs.write_text("", encoding="utf-8")
        self.assertTrue(turn_cgroup.usable(mount, root_dir=self.base))
        os.unlink(os.path.join(self.kernel(mount), "cgroup.kill"))
        self.assertFalse(turn_cgroup.usable(mount, root_dir=self.base))
        self.assertFalse(turn_cgroup.usable("/absent", root_dir=self.base))

    @unittest.skipIf(os.geteuid() == 0, "root ignore le bit d'écriture")
    def test_usable_refuse_une_racine_non_inscriptible(self):
        mount = self.fake("/u", {"cgroup.kill": "", "cgroup.events": "populated 0\n",
                                 "cgroup.procs": ""})
        os.chmod(self.kernel(mount), 0o555)
        try:
            self.assertFalse(turn_cgroup.usable(mount, root_dir=self.base))
        finally:
            # rendu avant tearDown : un addCleanup passerait après le
            # `TemporaryDirectory.cleanup()` qui, lui, doit pouvoir enlever
            # le dossier (sinon FileNotFoundError sur le chmod).
            os.chmod(self.kernel(mount), 0o755)

    def test_create_refuse_existant_et_identifiant_douteux(self):
        root = self.fake("/u", {"cgroup.procs": ""})
        chemin = turn_cgroup.create(root, "abc", root_dir=self.base)
        self.assertEqual(chemin, "/u/turn-abc")
        self.assertTrue(os.path.isdir(self.kernel(chemin)))
        self.assertIsNone(turn_cgroup.create(root, "abc", root_dir=self.base))
        self.assertIsNone(turn_cgroup.create(root, "a/b", root_dir=self.base))
        self.assertIsNone(turn_cgroup.create("/absent", "abc", root_dir=self.base))

    def test_populated_lit_cgroup_events(self):
        self.fake("/u", {"cgroup.procs": ""})
        turn = self.fake("/u/turn-a", {"cgroup.events": "populated 1\nfrozen 0\n"})
        events = Path(self.kernel(turn)) / "cgroup.events"
        self.assertIs(turn_cgroup.populated(turn, root_dir=self.base), True)
        events.write_text("populated 0\n", encoding="utf-8")
        self.assertIs(turn_cgroup.populated(turn, root_dir=self.base), False)
        events.write_text("frozen 0\n", encoding="utf-8")
        self.assertIsNone(turn_cgroup.populated(turn, root_dir=self.base))
        os.unlink(events)
        self.assertIsNone(turn_cgroup.populated(turn, root_dir=self.base))
        self.assertIsNone(turn_cgroup.populated("/absent", root_dir=self.base))

    def test_remove_absent_vide_ou_peuple(self):
        self.fake("/u", {"cgroup.procs": ""})
        self.assertTrue(turn_cgroup.remove("/u/turn-absent", root_dir=self.base))
        # cgroupfs : les fichiers de contrôle ne comptent pas dans `rmdir` ;
        # une fausse monture, si. On injecte donc l'état « vide » observé
        # (`populated 0`) et le dossier reste réellement vide.
        vide = self.fake("/u/turn-vide")
        with mock.patch.object(turn_cgroup, "populated", return_value=False):
            self.assertTrue(turn_cgroup.remove(vide, root_dir=self.base))
        self.assertFalse(os.path.exists(self.kernel(vide)))
        plein = self.fake("/u/turn-plein", {"cgroup.events": "populated 1\n"})
        self.assertFalse(turn_cgroup.remove(plein, root_dir=self.base))
        self.assertTrue(os.path.isdir(self.kernel(plein)))

    def test_orphans_ne_liste_que_les_cgroups_de_tour(self):
        root = self.fake("/u", {"cgroup.procs": ""})
        for name in ("turn-b", "turn-a", "turn-bad!", "autre", "turn-" + "a" * 121):
            self.fake("/u/" + name)
        (Path(self.kernel(root)) / "turn-fichier").write_text("pas un dossier",
                                                             encoding="utf-8")
        os.symlink(self.outside, os.path.join(self.kernel(root), "turn-lien"))
        self.assertEqual(turn_cgroup.orphans(root, root_dir=self.base),
                         ["/u/turn-a", "/u/turn-b"])
        self.assertEqual(turn_cgroup.orphans("/absent", root_dir=self.base), [])

    def test_kill_absent_vide_ou_peuple(self):
        self.fake("/u", {"cgroup.procs": ""})
        self.assertTrue(turn_cgroup.kill("/u/turn-absent", timeout=0.1,
                                         root_dir=self.base))
        # même limite : `cgroup.kill` reste dans le dossier après l'écriture,
        # alors que cgroupfs l'ignore au `rmdir`. On injecte la fin (`remove`),
        # pas l'attente : `populated` est bien relu sur la fausse monture.
        vide = self.fake("/u/turn-vide", {"cgroup.events": "populated 0\n",
                                          "cgroup.kill": ""})
        with mock.patch.object(turn_cgroup, "remove", return_value=True) as enleve:
            self.assertTrue(turn_cgroup.kill(vide, timeout=0.1, root_dir=self.base))
        enleve.assert_called_once_with(vide, root_dir=self.base)
        self.assertEqual((Path(self.kernel(vide)) / "cgroup.kill").read_text(), "1")
        plein = self.fake("/u/turn-plein", {"cgroup.events": "populated 1\n",
                                            "cgroup.kill": ""})
        debut = time.monotonic()
        self.assertFalse(turn_cgroup.kill(plein, timeout=0.1, root_dir=self.base))
        # hermeticity: allow — borne de vivacité (kill rend la main), pas une mesure
        self.assertLess(time.monotonic() - debut, 2.0)
        self.assertEqual((Path(self.kernel(plein)) / "cgroup.kill").read_text(), "1")
        self.assertTrue(os.path.isdir(self.kernel(plein)))

    def test_memory_peak_et_repli_current(self):
        peak = self.fake("/u/turn-p", {"memory.peak": "12345\n",
                                       "memory.current": "1\n"})
        self.assertEqual(turn_cgroup.memory_peak(peak, root_dir=self.base), 12345)
        current = self.fake("/u/turn-c", {"memory.current": "999\n"})
        self.assertEqual(turn_cgroup.memory_peak(current, root_dir=self.base), 999)
        invalide = self.fake("/u/turn-i", {"memory.peak": "inconnu\n",
                                           "memory.current": "42\n"})
        self.assertEqual(turn_cgroup.memory_peak(invalide, root_dir=self.base), 42)
        vide = self.fake("/u/turn-v")
        self.assertIsNone(turn_cgroup.memory_peak(vide, root_dir=self.base))
        self.assertIsNone(turn_cgroup.memory_peak("/absent", root_dir=self.base))


class TrampolineTest(unittest.TestCase):
    """A.9–A.11 : ligne de commande, refus hermétiques, `main` en processus."""

    def run_module(self, args: list) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ameesh.turn_cgroup", *args],
            cwd=REPO, env=support.child_env(), capture_output=True, text=True,
            timeout=30)

    def test_commande_du_trampoline(self):
        ligne = turn_cgroup.command("/u/turn-a", 7, ["/bin/echo", "x"])
        module = str(Path(turn_cgroup.__file__).resolve())
        self.assertEqual(ligne, [sys.executable, module, "--cgroup", "/u/turn-a",
                                 "--ready-fd", "7", "--", "/bin/echo", "x"])

    def test_refus_hermetiques_sans_cgroup_valide(self):
        tmp = tempfile.mkdtemp(prefix="l498-refus-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        marqueur = os.path.join(tmp, "execute")
        argv = ["/bin/sh", "-c", "touch %s" % marqueur]
        cas = (
            ("sans --cgroup", ["--ready-fd", "1", "--"] + argv, "usage"),
            ("sans --", ["--cgroup", tmp, "--ready-fd", "1"], "usage"),
            ("hors monture", ["--cgroup", tmp, "--ready-fd", "1", "--"] + argv,
             "could not enter cgroup"),
            ("traversant", ["--cgroup", "/../../etc", "--ready-fd", "1", "--"]
             + argv, "outside"),
        )
        for nom, args, attendu in cas:
            with self.subTest(cas=nom):
                proc = self.run_module(args)
                self.assertEqual(proc.returncode, turn_cgroup.REFUSED, proc.stderr)
                self.assertIn("L498:", proc.stderr)
                self.assertIn(attendu, proc.stderr)
        # aucun cas n'a exécuté l'argument
        self.assertFalse(os.path.exists(marqueur))

    def test_main_repond_125_dans_le_processus(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            vide = turn_cgroup.main([])
            mauvais_fd = turn_cgroup.main(["--cgroup", "/u", "--ready-fd", "x",
                                           "--", "true"])
        self.assertEqual(vide, turn_cgroup.REFUSED)
        self.assertEqual(mauvais_fd, turn_cgroup.REFUSED)
        self.assertIn("usage", err.getvalue())
        self.assertIn("invalid --ready-fd", err.getvalue())


# ==========================================================================
# B. témoins sur un vrai cgroup — sautés si la délégation manque
# ==========================================================================

def _cgroup_du_temoin() -> str | None:
    """Le cgroup du processus de test, s'il est réellement utilisable comme
    parent : répertoire visible sous `/sys/fs/cgroup`, fichiers du noyau
    présents, et inscriptible par nous. None sinon : le témoin se saute."""
    own = turn_cgroup.own_cgroup()
    if not own or not own.startswith("/"):
        return None
    kernel = os.path.join(turn_cgroup.CGROUP_ROOT, own.lstrip("/"))
    try:
        if not os.path.isdir(kernel) or os.path.islink(kernel):
            return None
        for name in ("cgroup.procs", "cgroup.events"):
            if not os.path.exists(os.path.join(kernel, name)):
                return None
        if not os.access(kernel, os.W_OK | os.X_OK):
            return None
    except OSError:
        return None
    return own


#: le cgroup propre du banc, s'il autorise la création d'un enfant
TEMOIN = _cgroup_du_temoin()


@unittest.skipUnless(TEMOIN, "cgroup v2 délégué et inscriptible requis (témoin réel)")
class TemoinCgroupReelTest(unittest.TestCase):
    """B.1–B.2 : placement réel et descendant détaché (double fork + setsid).

    Seul `l498-test-<pid>`, créé ici sous le cgroup propre du banc, est
    manipulé ; tout est enlevé dans `finally`, jamais un autre cgroup."""

    #: le grand enfant écrit son pid, se détache de la session et dort
    SNIPPET = (
        "import os, sys, time\n"
        "from ameesh import turn_cgroup\n"
        "if not turn_cgroup.enter(sys.argv[1]):\n"
        "    sys.exit(3)\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    with open(sys.argv[2], 'w') as fh:\n"
        "        fh.write(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        "os._exit(0)\n"
    )

    def setUp(self) -> None:
        self.own = str(TEMOIN).rstrip("/")
        self.mount = "%s/l498-test-%d" % (self.own, os.getpid())
        self.kernel = os.path.join(turn_cgroup.CGROUP_ROOT, self.mount.lstrip("/"))
        self.assertTrue(self.mount.startswith(self.own + "/"),
                        "le témoin reste sous son propre cgroup")
        os.mkdir(self.kernel)

    def tearDown(self) -> None:
        self._nettoyer(None)

    def _nettoyer(self, pid: int | None) -> None:
        """Enlève le processus détaché et le cgroup créé par CE test."""
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            turn_cgroup.kill(self.mount, timeout=2.0)
        except Exception:  # le nettoyage ne masque jamais le verdict
            pass
        try:
            if os.path.isdir(self.kernel) and not os.listdir(self.kernel):
                os.rmdir(self.kernel)
        except OSError:
            pass

    @staticmethod
    def _lire_ligne(fd: int, timeout: float = 5.0) -> str:
        """Première ligne du descripteur, borne incluse, jamais bloquant."""
        fin = time.monotonic() + timeout
        data = b""
        while b"\n" not in data:
            reste = fin - time.monotonic()
            if reste <= 0:
                break
            prets, _, _ = select.select([fd], [], [], reste)
            if not prets:
                break
            bloc = os.read(fd, 256)
            if not bloc:
                break
            data += bloc
        return data.split(b"\n", 1)[0].decode("utf-8", "replace")

    def _attendre(self, condition, timeout: float = 5.0) -> bool:
        fin = time.monotonic() + timeout
        while time.monotonic() < fin:
            if condition():
                return True
            time.sleep(0.05)
        return False

    def test_trampoline_ready_et_placement(self):
        with tempfile.TemporaryDirectory(prefix="l498-temoin-") as tmp:
            vu = os.path.join(tmp, "vu")
            ready_r, ready_w = os.pipe()
            proc = None
            try:
                try:
                    proc = subprocess.Popen(
                        [sys.executable, "-m", "ameesh.turn_cgroup",
                         "--cgroup", self.mount, "--ready-fd", str(ready_w),
                         "--", "/bin/sh", "-c", "cat /proc/self/cgroup > %s" % vu],
                        cwd=REPO, env=support.child_env(), pass_fds=(ready_w,),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                finally:
                    os.close(ready_w)
                    ready_w = None
                ligne = self._lire_ligne(ready_r)
                _, err = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, err)
                self.assertEqual(ligne, turn_cgroup.READY)
                self.assertIn(self.mount, Path(vu).read_text(encoding="utf-8"))
                self.assertTrue(turn_cgroup.kill(self.mount, timeout=5.0))
            finally:
                if ready_w is not None:
                    os.close(ready_w)
                os.close(ready_r)
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                self._nettoyer(None)

    def test_double_fork_setsid_reste_dans_le_cgroup(self):
        with tempfile.TemporaryDirectory(prefix="l498-temoin-") as tmp:
            pidfile = os.path.join(tmp, "pid")
            erreurs = os.path.join(tmp, "err")
            pid = None
            try:
                with open(erreurs, "wb") as errfh:
                    proc = subprocess.Popen(
                        [sys.executable, "-c", self.SNIPPET, self.mount, pidfile],
                        cwd=REPO, env=support.child_env(),
                        stdout=subprocess.DEVNULL, stderr=errfh)
                    # jamais communicate() : le grand enfant détaché hérite des
                    # descripteurs et garderait les tuyaux ouverts 60 s ; le pid
                    # se lit dans le fichier, la sortie d'erreur aussi.
                    code = proc.wait(timeout=5)
                detail = Path(erreurs).read_text(encoding="utf-8", errors="replace")
                self.assertEqual(code, 0, detail)
                self.assertTrue(self._attendre(
                    lambda: os.path.exists(pidfile)
                    and os.path.getsize(pidfile) > 0), detail)
                pid = int(Path(pidfile).read_text(encoding="utf-8"))
                # le descendant détaché est invisible au relevé des pid, mais
                # le cgroup le voit : c'est la preuve de vie de L498
                self.assertTrue(self._attendre(
                    lambda: turn_cgroup.populated(self.mount) is True))
                self.assertTrue(turn_cgroup.kill(self.mount, timeout=5.0))
                self.assertTrue(self._attendre(self._disparu(pid)),
                                "grand enfant %d encore vivant après cgroup.kill" % pid)
            finally:
                self._nettoyer(pid)

    @staticmethod
    def _disparu(pid: int):
        """Le pid n'est plus vivant : disparu, ou zombie non encore moissonné
        par son nouveau parent. Un zombie reste visible de `kill(pid, 0)` —
        c'est pourtant la preuve que `cgroup.kill` l'a bien tué."""
        def sonde() -> bool:
            try:
                # hermeticity: allow — état du pid créé par ce témoin, jamais de l'hôte
                stat = Path("/proc/%d/stat" % pid).read_text(encoding="utf-8")
            except OSError:
                return True
            try:
                etat = stat.rsplit(")", 1)[1].split()[0]
            except IndexError:
                return True
            return etat == "Z"
        return sonde


# ==========================================================================
# C. exécuteur et travail de fond — mocks, aucun cgroup réel
# ==========================================================================

class ReadyTest(unittest.TestCase):
    """C.1 : lecture bornée du READY du trampoline sur le descripteur dédié."""

    @staticmethod
    def _fermer(*fds) -> None:
        for fd in fds:
            if isinstance(fd, int):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_ready_lu_sur_un_tuyau_reel(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, (turn_cgroup.READY + "\n").encode("ascii"))
            os.close(write_fd)
            write_fd = None
            self.assertEqual(AgentWorker._read_turn_cgroup_ready(read_fd),
                             turn_cgroup.READY)
        finally:
            self._fermer(read_fd, write_fd)

    def test_premiere_ligne_seulement(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, (turn_cgroup.READY + "\nsuite\n").encode("ascii"))
            self.assertEqual(AgentWorker._read_turn_cgroup_ready(read_fd),
                             turn_cgroup.READY)
        finally:
            self._fermer(read_fd, write_fd)

    def test_tuyau_ferme_sans_donnee(self):
        read_fd, write_fd = os.pipe()
        try:
            os.close(write_fd)
            write_fd = None
            self.assertEqual(AgentWorker._read_turn_cgroup_ready(read_fd), "")
        finally:
            self._fermer(read_fd, write_fd)

    def test_attente_expiree_sans_donnee(self):
        read_fd, write_fd = os.pipe()
        try:
            debut = time.monotonic()
            self.assertEqual(AgentWorker._read_turn_cgroup_ready(read_fd, timeout=0.05),
                             "")
            # hermeticity: allow — borne de vivacité de l'attente bornée, pas une mesure
            self.assertLess(time.monotonic() - debut, 2.0)
        finally:
            self._fermer(read_fd, write_fd)

    def test_ligne_incomplete_a_la_fermeture(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, turn_cgroup.READY.encode("ascii"))
            os.close(write_fd)
            write_fd = None
            self.assertEqual(AgentWorker._read_turn_cgroup_ready(read_fd), "")
        finally:
            self._fermer(read_fd, write_fd)


class SonTurnCgroupRootTest(unittest.TestCase):
    """C.2 (et garde de C.8) : la sonde de la racine, une fois, sans None levé."""

    @staticmethod
    def _runner() -> Runner:
        runner = object.__new__(Runner)
        runner._turn_cgroup_probed = False
        runner._turn_cgroup_root = None
        return runner

    def test_sonde_une_fois_puis_cache(self):
        runner = self._runner()
        with mock.patch("ameesh.runner.log"), \
                mock.patch("ameesh.turn_cgroup.root_for_runner",
                           return_value=("/user.slice/x/unit", "")) as sonde:
            self.assertEqual(runner.turn_cgroup_root(), "/user.slice/x/unit")
            self.assertEqual(runner.turn_cgroup_root(), "/user.slice/x/unit")
        self.assertEqual(sonde.call_count, 1)
        self.assertTrue(runner._turn_cgroup_probed)
        self.assertEqual(runner._turn_cgroup_root, "/user.slice/x/unit")

    def test_sonde_en_cours_ne_publie_pas_un_faux_repli(self):
        runner = self._runner()
        started, release = threading.Event(), threading.Event()
        results = []
        def probe():
            if threading.current_thread() is worker:
                started.set()
                if not release.wait(5):
                    raise RuntimeError("probe not released")
            return "/u", ""
        worker = threading.Thread(target=lambda: results.append(runner.turn_cgroup_root()))
        with mock.patch("ameesh.runner.log"), \
                mock.patch("ameesh.turn_cgroup.root_for_runner", side_effect=probe):
            worker.start()
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(runner.turn_cgroup_root(), "/u")
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, ["/u"])

    def test_sans_delegation_rend_none_sans_lever(self):
        runner = self._runner()
        with mock.patch("ameesh.runner.log"), \
                mock.patch("ameesh.turn_cgroup.root_for_runner",
                           return_value=(None, "own cgroup unknown")) as sonde:
            self.assertIsNone(runner.turn_cgroup_root())
            self.assertIsNone(runner.turn_cgroup_root())
        self.assertEqual(sonde.call_count, 1)

    def test_sans_delegation_rien_n_est_cree(self):
        runner = self._runner()
        with mock.patch("ameesh.runner.log"), \
                mock.patch("ameesh.turn_cgroup.root_for_runner",
                           return_value=(None, "own cgroup unknown")), \
                mock.patch("ameesh.turn_cgroup.create") as creation, \
                mock.patch("ameesh.turn_cgroup.command") as commande:
            self.assertIsNone(runner.turn_cgroup_root())
        creation.assert_not_called()
        commande.assert_not_called()


class ReconciliationTest(unittest.TestCase):
    """C.3 : les cgroups sans tour vivant sont tués, jamais un tour en cours."""

    @staticmethod
    def _runner(root: str | None = "/u", live=("a",)) -> Runner:
        runner = object.__new__(Runner)
        runner._turn_cgroup_probed = True
        runner._turn_cgroup_root = root
        runner._turn_cgroups_live = set(live)
        runner._turn_cgroups_lock = threading.Lock()
        runner.background = SimpleNamespace(lock=threading.Lock(), entries=[])
        return runner

    @staticmethod
    def _entry(turn_id: str, state: str = "running") -> background.Entry:
        return background.Entry(agent="agent-x", turn_id=turn_id, pgid=None,
                                ended=0.0, deadline=0.0, grace_s=0, state=state)

    def test_tue_les_orphelins_et_epargne_les_tours_vivants(self):
        runner = self._runner()
        with mock.patch("ameesh.turn_cgroup.orphans",
                        return_value=["/u/turn-a", "/u/turn-b", "/u/autre"]) as orphelins, \
                mock.patch("ameesh.turn_cgroup.kill", return_value=True) as tuer, \
                mock.patch("ameesh.runner.log"):
            self.assertEqual(runner.reconcile_turn_cgroups(), 1)
        orphelins.assert_called_once_with("/u")
        self.assertEqual(tuer.call_args_list, [mock.call("/u/turn-b")])

    def test_creation_pendant_enumeration_est_vue_vivante(self):
        runner = self._runner(live=())
        def enumerate_and_launch(root):
            with runner._turn_cgroups_lock:
                runner._turn_cgroups_live.add("new")
            return ["/u/turn-new"]
        with mock.patch("ameesh.turn_cgroup.orphans", side_effect=enumerate_and_launch), \
                mock.patch("ameesh.turn_cgroup.kill") as kill:
            self.assertEqual(runner.reconcile_turn_cgroups(), 0)
        kill.assert_not_called()

    def test_le_travail_de_fond_en_grace_est_vivant(self):
        runner = self._runner(live=())
        runner.background.entries = [self._entry("c"), self._entry("d", state="done")]
        with mock.patch("ameesh.turn_cgroup.orphans",
                        return_value=["/u/turn-c", "/u/turn-d"]), \
                mock.patch("ameesh.turn_cgroup.kill", return_value=True) as tuer, \
                mock.patch("ameesh.runner.log"):
            self.assertEqual(runner.reconcile_turn_cgroups(), 1)
        self.assertEqual(tuer.call_args_list, [mock.call("/u/turn-d")])

    def test_startup_nomme_les_orphelins_preexistants(self):
        runner = self._runner(live=())
        with mock.patch("ameesh.turn_cgroup.orphans", return_value=["/u/turn-b"]), \
                mock.patch("ameesh.turn_cgroup.kill", return_value=True), \
                mock.patch("ameesh.runner.log") as journal:
            self.assertEqual(runner.reconcile_turn_cgroups(startup=True), 1)
        messages = [appel.args[0] for appel in journal.call_args_list]
        self.assertTrue(any("pre-existing" in m for m in messages))

    def test_cgroup_qui_ne_se_vide_pas_est_signale_sans_lever(self):
        runner = self._runner(live=())
        with mock.patch("ameesh.turn_cgroup.orphans", return_value=["/u/turn-b"]), \
                mock.patch("ameesh.turn_cgroup.kill", return_value=False), \
                mock.patch("ameesh.runner.log_async") as journal:
            self.assertEqual(runner.reconcile_turn_cgroups(), 0)
        messages = [appel.args[0] for appel in journal.call_args_list]
        self.assertTrue(any("could not be emptied" in m for m in messages))

    def test_kill_en_exception_ne_leve_pas(self):
        runner = self._runner(live=())
        with mock.patch("ameesh.turn_cgroup.orphans", return_value=["/u/turn-b"]), \
                mock.patch("ameesh.turn_cgroup.kill",
                           side_effect=RuntimeError("noyau muet")), \
                mock.patch("ameesh.runner.log_async") as journal:
            self.assertEqual(runner.reconcile_turn_cgroups(), 0)
        self.assertTrue(any("not reconciled" in appel.args[0]
                            for appel in journal.call_args_list))

    def test_sans_racine_rien_n_est_balaye(self):
        runner = self._runner(root=None)
        with mock.patch("ameesh.runner.log"), \
                mock.patch("ameesh.turn_cgroup.orphans") as orphelins:
            self.assertEqual(runner.reconcile_turn_cgroups(), 0)
        orphelins.assert_not_called()

    def test_etat_du_fond_illisible_ne_tue_rien(self):
        runner = self._runner(live=())
        runner.background = SimpleNamespace(lock=None, entries=[])
        with mock.patch("ameesh.runner.log_async"), \
                mock.patch("ameesh.turn_cgroup.orphans") as orphelins:
            self.assertEqual(runner.reconcile_turn_cgroups(), 0)
        orphelins.assert_not_called()


class RessourceDuTourTest(unittest.TestCase):
    """C.4–C.5 : `attach_cgroup` au lancement, `kill` + `close_turn` à la fin."""

    @staticmethod
    def _worker(mediated: bool = False) -> AgentWorker:
        worker = object.__new__(AgentWorker)
        # `name` est une propriété de Thread : l'initialiser sans démarrer
        # aucun fil (aucun exécuteur réel n'est construit ici).
        threading.Thread.__init__(worker, name="agent-x")
        worker.db = object()
        worker.runner = SimpleNamespace(host="pc", mediated=mediated,
                                        container_runtime=lambda: None)
        worker._turn_open = False
        return worker

    # -- C.4 : rattachement -------------------------------------------------

    def test_attach_cgroup_appele_apres_open_turn(self):
        worker = self._worker()
        store = mock.Mock()
        with mock.patch("ameesh.runner.storage.of", return_value=store):
            worker._open_turn_resource("t1", 4242, cgroup="/u/turn-t1")
        ressources = store.turn_resources
        ressources.open_turn.assert_called_once_with(
            "t1", "agent-x", "pc", pgid=4242,
            label="ameesh.turn=t1,ameesh.agent=agent-x")
        ressources.attach_cgroup.assert_called_once_with("t1", "/u/turn-t1")
        self.assertTrue(worker._turn_open)

    def test_sans_cgroup_attach_non_appele(self):
        worker = self._worker()
        store = mock.Mock()
        with mock.patch("ameesh.runner.storage.of", return_value=store):
            worker._open_turn_resource("t1", 4242)
        store.turn_resources.attach_cgroup.assert_not_called()
        self.assertTrue(worker._turn_open)

    def test_executeur_medie_n_attache_pas_de_cgroup(self):
        worker = self._worker(mediated=True)
        store = mock.Mock()
        with mock.patch("ameesh.runner.storage.of", return_value=store):
            worker._open_turn_resource("t1", 4242, cgroup="/u/turn-t1")
        store.turn_resources.attach_cgroup.assert_not_called()
        self.assertTrue(worker._turn_open)

    def test_open_turn_en_echec_ne_casse_pas_le_tour(self):
        worker = self._worker()
        store = mock.Mock()
        store.turn_resources.open_turn.side_effect = RuntimeError("base indisponible")
        with mock.patch("ameesh.runner.storage.of", return_value=store), \
                mock.patch("ameesh.runner.log") as journal:
            worker._open_turn_resource("t1", 4242, cgroup="/u/turn-t1")
        self.assertFalse(worker._turn_open)
        store.turn_resources.attach_cgroup.assert_not_called()
        self.assertTrue(any("could not attach" in appel.args[0]
                            for appel in journal.call_args_list))

    def test_attach_cgroup_en_echec_laisse_la_ressource_ouverte(self):
        worker = self._worker()
        store = mock.Mock()
        store.turn_resources.attach_cgroup.side_effect = RuntimeError("colonne absente")
        with mock.patch("ameesh.runner.storage.of", return_value=store), \
                mock.patch("ameesh.runner.log") as journal:
            worker._open_turn_resource("t1", 4242, cgroup="/u/turn-t1")
        self.assertTrue(worker._turn_open)
        self.assertTrue(any("not recorded" in appel.args[0]
                            for appel in journal.call_args_list))

    # -- C.5 : fermeture ----------------------------------------------------

    def _ouvrir(self) -> AgentWorker:
        worker = self._worker()
        worker._turn_open = True
        return worker

    def test_fermeture_avec_cgroup_vide(self):
        worker = self._ouvrir()
        store = mock.Mock()
        with mock.patch("ameesh.runner.storage.of", return_value=store), \
                mock.patch("ameesh.turn_cgroup.memory_peak", return_value=4096), \
                mock.patch("ameesh.turn_cgroup.kill", return_value=True) as tuer, \
                mock.patch("ameesh.turn_processes.group_running", return_value=False), \
                mock.patch("ameesh.runner.log") as journal, \
                mock.patch("ameesh.runner.log_async"):
            worker._close_turn_resource("t1", 4242, cgroup="/u/turn-t1")
        tuer.assert_called_once_with("/u/turn-t1")
        store.turn_resources.close_turn.assert_called_once_with(
            "t1", orphan=False, containers=None)
        messages = [appel.args[0] for appel in journal.call_args_list]
        self.assertTrue(any("cgroup cleanup" in m and "emptied and removed" in m
                            and "memory peak 4096 bytes" in m for m in messages))
        self.assertFalse(worker._turn_open)

    def test_fermeture_avec_cgroup_peuple_le_signale_orphelin(self):
        worker = self._ouvrir()
        store = mock.Mock()
        with mock.patch("ameesh.runner.storage.of", return_value=store), \
                mock.patch("ameesh.turn_cgroup.memory_peak", return_value=None), \
                mock.patch("ameesh.turn_cgroup.kill", return_value=False), \
                mock.patch("ameesh.turn_processes.group_running", return_value=False), \
                mock.patch("ameesh.runner.log") as journal, \
                mock.patch("ameesh.runner.log_async"):
            worker._close_turn_resource("t1", 4242, cgroup="/u/turn-t1")
        store.turn_resources.close_turn.assert_called_once_with(
            "t1", orphan=True, containers=None)
        messages = [appel.args[0] for appel in journal.call_args_list]
        self.assertTrue(any("cgroup cleanup" in m and "still populated" in m
                            and "memory peak unknown" in m for m in messages))
        # l'orphelin signalé porte le chemin du cgroup, pas un groupe recyclé
        self.assertTrue(any(m.endswith("/u/turn-t1") for m in messages))

    def test_fermeture_sans_ligne_base_nettoie_encore_le_cgroup(self):
        worker = self._worker()
        with mock.patch("ameesh.turn_cgroup.memory_peak", return_value=None), \
                mock.patch("ameesh.turn_cgroup.kill", return_value=True) as tuer, \
                mock.patch("ameesh.turn_processes.group_running", return_value=False), \
                mock.patch("ameesh.runner.log"), \
                mock.patch("ameesh.runner.storage.of") as store:
            worker._close_turn_resource("t1", 4242, cgroup="/u/turn-t1")
        tuer.assert_called_once_with("/u/turn-t1")
        store.assert_not_called()

    def test_fermeture_sans_cgroup_ni_ligne_base_ne_fait_rien(self):
        worker = self._worker()
        with mock.patch("ameesh.turn_cgroup.kill") as tuer, \
                mock.patch("ameesh.runner.storage.of") as store:
            worker._close_turn_resource("t1", 4242)
        tuer.assert_not_called()
        store.assert_not_called()

    def test_kill_en_exception_reste_orphelin(self):
        worker = self._ouvrir()
        store = mock.Mock()
        with mock.patch("ameesh.runner.storage.of", return_value=store), \
                mock.patch("ameesh.turn_cgroup.memory_peak", return_value=None), \
                mock.patch("ameesh.turn_cgroup.kill",
                           side_effect=RuntimeError("noyau muet")), \
                mock.patch("ameesh.turn_processes.group_running", return_value=False), \
                mock.patch("ameesh.runner.log"), \
                mock.patch("ameesh.runner.log_async") as journal:
            worker._close_turn_resource("t1", 4242, cgroup="/u/turn-t1")
        store.turn_resources.close_turn.assert_called_once_with(
            "t1", orphan=True, containers=None)
        self.assertTrue(any("not killed" in appel.args[0]
                            for appel in journal.call_args_list))


class TravailDeFondTest(unittest.TestCase):
    """C.6–C.7 : le cgroup, preuve de vie puis route du nettoyage."""

    @staticmethod
    def _tracker() -> background.Tracker:
        tracker = object.__new__(background.Tracker)
        tracker.runner = SimpleNamespace(container_runtime=lambda: None)
        tracker.log = lambda message: None
        tracker.entries = []
        tracker.lock = threading.Lock()
        return tracker

    def test_cgroup_peuple_est_un_survivant(self):
        tracker = self._tracker()
        with mock.patch("ameesh.turn_cgroup.populated", return_value=True) as peuple:
            self.assertEqual(tracker.survivors("t1", None, cgroup="/u/turn-t1"),
                             ([], []))
        peuple.assert_called_once_with("/u/turn-t1")

    def test_cgroup_vide_ou_illisible_n_est_pas_un_survivant(self):
        tracker = self._tracker()
        for verdict in (False, None):
            with self.subTest(verdict=verdict), \
                    mock.patch("ameesh.turn_cgroup.populated", return_value=verdict):
                self.assertIsNone(tracker.survivors("t1", None, cgroup="/u/turn-t1"))
        with mock.patch("ameesh.turn_cgroup.populated",
                        side_effect=RuntimeError("cgroup muet")):
            self.assertIsNone(tracker.survivors("t1", None, cgroup="/u/turn-t1"))

    def test_kill_route_vers_le_cgroup(self):
        tracker = self._tracker()
        entry = background.Entry(agent="agent-x", turn_id="t1", pgid=7000,
                                 ended=0.0, deadline=0.0, grace_s=0,
                                 cgroup="/u/turn-t1")
        with mock.patch.object(tracker, "_kill_cgroup", return_value=False) as par_cgroup:
            self.assertFalse(tracker._kill(entry))
        par_cgroup.assert_called_once_with(entry)

    def test_kill_sans_cgroup_garde_le_chemin_des_pid(self):
        tracker = self._tracker()
        entry = background.Entry(agent="agent-x", turn_id="t1", pgid=None,
                                 ended=0.0, deadline=0.0, grace_s=0)
        with mock.patch.object(tracker, "_kill_cgroup") as par_cgroup:
            self.assertFalse(tracker._kill(entry))
            entry.unverified = True
            self.assertTrue(tracker._kill(entry))
        par_cgroup.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

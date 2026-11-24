# SPDX-License-Identifier: AGPL-3.0-only
"""#8128 : le `TMPDIR` d'un lot est un alias court, sur disque, hors `/tmp` et
`/dev/shm`.

#8020 a donné à chaque lot un sous-dossier temporaire sous le dossier de son
agent ; son chemin complet dépassait la marge AF_UNIX de `multiprocessing`
(`len(TMPDIR) + 40 > 103`), et `compileall -j 0` tombait sur
« AF_UNIX path too long ». L'alias `~/.t/<mesh>/l<lot>-<empreinte>` pointe le
dossier géré : quota, verrou, processus et copies Git de #8020 restent
inchangés, et le chemin court tient la socket du forkserver.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import housekeeping as menage


class _Cfg:
    def __init__(self, state_dir: str, dsn: str = ""):
        self.state_dir = state_dir
        self.host = "pc"
        self.dsn = dsn
        self.schema = "public"


class _Base(unittest.TestCase):
    # Les tests de mécanique ne dépendent pas de la longueur du chemin du
    # guest ; ceux qui éprouvent la marge AF_UNIX réelle passent par
    # `_RealBase`.
    PATCH_SHORT = True
    PATCH_MEMORY = True

    def setUp(self) -> None:
        # Le banc doit avoir un chemin court : `$TMPDIR` d'une session de lot
        # est justement le chemin long que ce lot corrige. Le préfixe et le
        # sous-dossier restent courts pour que la marge AF_UNIX soit vraie
        # sous Darwin aussi (`/Users/runner` est plus long que `/home`).
        try:
            self.base = tempfile.mkdtemp(prefix=".a8128-",
                                         dir=os.path.expanduser("~"))
        except OSError:
            self.skipTest("no writable home for a short temporary root")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = os.path.join(self.base, "h")
        os.makedirs(self.home)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Les tests de mécanique ne dépendent ni du système de fichiers ni de
        # la longueur du chemin du guest ; les tests réels passent par
        # `_RealBase`, qui n'écarte que les racines en mémoire.
        if self.PATCH_MEMORY:
            patcher = mock.patch.object(menage, "in_memory",
                                        lambda path: False)
            patcher.start()
            self.addCleanup(patcher.stop)
        if self.PATCH_SHORT:
            patcher = mock.patch.object(menage, "_short_path_ok",
                                        lambda path: True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = _Cfg(os.path.join(self.base, "state"))
        self.pol = menage.Policy()
        self.agent = "a1"
        self.agent_dir = os.path.join(self.cfg.state_dir, ".menage", "tmp", self.agent)

    def real(self, lot, agent: str | None = None) -> str:
        return os.path.join(self.cfg.state_dir, ".menage", "tmp",
                            agent or self.agent, "ameesh-lot-%s" % lot)

    def env(self, lot, agent: str | None = None) -> dict:
        return menage.turn_env(self.cfg, self.pol, agent or self.agent, {}, lot=lot)

    def alias(self, lot, agent: str | None = None) -> str:
        root = menage.lot_tmp_link_roots(self.cfg)[0]
        return os.path.join(root, menage.lot_tmp_link_name(agent or self.agent,
                                                           str(lot)))


class _RealBase(_Base):
    """Banc à marge réelle : l'alias n'est créé que si une racine du guest
    laisse la marge AF_UNIX. Le guest de prévalidation a un HOME long ou en
    tmpfs : ces tests sont alors sautés, la mécanique restant couverte par
    `_Base`."""

    PATCH_SHORT = False
    PATCH_MEMORY = False

    def require_short_root(self) -> None:
        roots = menage.lot_tmp_link_roots(self.cfg)
        candidates = [
            (root, os.path.join(root,
                                menage.lot_tmp_link_name(self.agent, "8128")))
            for root in roots
        ]
        if not any(menage._short_path_ok(path) and not menage.in_memory(root)
                   for root, path in candidates):
            self.skipTest("guest without a short on-disk root for lot TMPDIR")


class AliasCourtTest(_Base):
    def test_racine_en_memoire_est_ecartee_au_profit_de_l_etat(self):
        roots = menage.lot_tmp_link_roots(self.cfg)
        maison = os.path.abspath(self.home)

        def memoire(path):
            return os.path.abspath(path).startswith(maison)

        with mock.patch.object(menage, "in_memory", memoire):
            tmpdir = self.env(8128)["TMPDIR"]
        self.assertTrue(os.path.islink(tmpdir))
        self.assertTrue(tmpdir.startswith(os.path.abspath(roots[1]) + os.sep))
        self.assertEqual(os.path.realpath(tmpdir), self.real(8128))

    def test_sans_racine_courte_le_chemin_long_reste(self):
        with mock.patch.object(menage, "in_memory", lambda path: True):
            tmpdir = self.env(8128)["TMPDIR"]
        self.assertFalse(os.path.islink(tmpdir))
        self.assertEqual(tmpdir, self.real(8128))
        self.assertTrue(os.path.isdir(tmpdir))

    def test_une_entree_non_lien_n_est_jamais_ecrasee(self):
        home_root = menage.lot_tmp_link_roots(self.cfg)[0]
        os.makedirs(home_root, mode=0o700)
        link = self.alias(8128)
        with open(link, "w", encoding="utf-8") as fh:
            fh.write("précieux")
        env = self.env(8128)
        with open(link, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "précieux")
        # l'autre racine candidate fournit l'alias, sans toucher au fichier
        self.assertTrue(os.path.islink(env["TMPDIR"]))
        self.assertTrue(env["TMPDIR"].startswith(
            menage.lot_tmp_link_roots(self.cfg)[1] + os.sep))
        self.assertEqual(os.path.realpath(env["TMPDIR"]), self.real(8128))

    def test_deux_meshes_ont_des_racines_distinctes(self):
        autre = _Cfg(self.cfg.state_dir, dsn="postgresql://autre/base")
        self.assertNotEqual(menage.lot_tmp_link_roots(self.cfg),
                            menage.lot_tmp_link_roots(autre))

    def test_le_tour_garde_le_verrou_du_dossier_reel_et_pose_l_alias(self):
        tour = menage.Turn.begin(self.cfg, self.pol, self.agent, None,
                                 new_session=False, base_env={}, lot=8128)
        self.addCleanup(lambda: tour.lock and tour.lock.release())
        self.assertEqual(tour.tmp, self.agent_dir)
        self.assertTrue(os.path.islink(tour.env["TMPDIR"]))
        self.assertEqual(os.path.realpath(tour.env["TMPDIR"]), self.real(8128))
        # le quota de #8020 continue de compter ce qui est écrit via l'alias
        with open(os.path.join(tour.env["TMPDIR"], "quota"), "w",
                  encoding="utf-8") as fh:
            fh.write("x")
        self.assertTrue(os.path.isfile(os.path.join(self.real(8128), "quota")))


class MargeCheminTest(unittest.TestCase):
    def test_marge_af_unix_borne_portative_103_octets(self):
        # 63 + marge 40 = 103 : `sun_path` de Darwin (104 octets, NUL
        # compris) passe ; 64 la dépasse. Linux (108) accepte plus, mais la
        # borne retenue est la plus stricte des deux plateformes.
        self.assertTrue(menage._short_path_ok("x" * 63))
        self.assertFalse(menage._short_path_ok("x" * 64))


class CheminReelTest(_RealBase):
    """La marge est éprouvée sur le vrai chemin, socket AF_UNIX comprise ;
    sauté quand le guest lui-même ne peut pas offrir de racine courte."""

    def test_le_plus_long_nom_du_registre_tient_la_marge_af_unix(self):
        self.require_short_root()
        # Le plus long nom observé au registre le 2026-11-23 (25 caractères) ;
        # la correction ne doit pas dépendre de la longueur du nom.
        agent = "pilot-resident-3389-codex"
        env = self.env(8128, agent)
        tmpdir = env["TMPDIR"]
        self.assertTrue(os.path.islink(tmpdir))
        self.assertEqual(os.path.realpath(tmpdir), self.real(8128, agent))
        self.assertTrue(os.path.isdir(os.path.realpath(tmpdir)))
        self.assertLessEqual(len(os.fsencode(tmpdir)) + menage.LOT_TMP_SOCKET_MARGIN,
                             menage.AF_UNIX_PATH_MAX)
        self.assertFalse(menage.in_memory(tmpdir))
        for key in ("TMPDIR", "TMP", "TEMP", "AMEESH_TMPDIR"):
            self.assertEqual(env[key], tmpdir)
        root = menage.lot_tmp_link_roots(self.cfg)[0]
        self.assertTrue(tmpdir.startswith(root + os.sep))
        self.assertEqual(os.stat(root).st_mode & 0o077, 0)

    def test_compileall_j_0_passe_sous_le_tmpdir_court(self):
        self.require_short_root()
        env = self.env(8128)
        tmpdir = env["TMPDIR"]
        real = os.path.realpath(tmpdir)
        src = os.path.join(real, "src")
        os.makedirs(src)
        for index in range(6):
            with open(os.path.join(src, "m%02d.py" % index), "w",
                      encoding="utf-8") as fh:
                fh.write("VALUE = %d\n" % index)
        child_env = dict(os.environ, TMPDIR=tmpdir, TMP=tmpdir, TEMP=tmpdir)
        proc = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "-j", "0", src],
            env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=120)
        output = proc.stdout.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, output[-2000:])
        self.assertNotIn("AF_UNIX", output)

    def test_socket_af_unix_du_forkserver_tient_sous_l_alias(self):
        self.require_short_root()
        tmpdir = self.env(8128)["TMPDIR"]
        directory = os.path.join(tmpdir, "pymp-abcdefgh")
        os.makedirs(directory)
        path = os.path.join(directory, "listener-abcdefghijklmnop")
        self.assertLessEqual(len(os.fsencode(path)), menage.AF_UNIX_PATH_MAX)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(path)
        finally:
            sock.close()

    def test_longueur_independante_du_nom_d_agent_et_contenu_garde(self):
        self.require_short_root()
        court = self.env(10, "a")["TMPDIR"]
        long = self.env(10, "x" * 64)["TMPDIR"]
        self.assertNotEqual(court, long)
        self.assertEqual(len(os.fsencode(court)), len(os.fsencode(long)))
        self.assertLessEqual(len(os.fsencode(long)) + menage.LOT_TMP_SOCKET_MARGIN,
                             menage.AF_UNIX_PATH_MAX)
        with open(os.path.join(court, "garde"), "w", encoding="utf-8") as fh:
            fh.write("x")
        # le même tour du même lot retrouve le même alias et son contenu
        encore = self.env(10, "a")["TMPDIR"]
        self.assertEqual(encore, court)
        self.assertTrue(os.path.isfile(os.path.join(encore, "garde")))


class NettoyageAliasTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.real_tmp_holder = menage.tmp_holder
        patcher = mock.patch.object(menage, "tmp_holder", lambda *a, **k: "")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_un_alias_orphelin_est_balaye(self):
        self.env(10)
        shutil.rmtree(self.real(10))
        self.assertTrue(os.path.lexists(self.alias(10)))
        out = menage.sweep_lot_links(self.cfg)
        self.assertFalse(os.path.lexists(self.alias(10)))
        self.assertEqual([e["action"] for e in out["entries"]], ["deleted"])

    def test_l_alias_part_avec_le_dossier_du_lot(self):
        self.env(11)
        out = menage.cleanup_lot_dirs(self.cfg, self.pol, agents=[self.agent],
                                      ended_only=False)
        self.assertFalse(os.path.exists(self.real(11)))
        self.assertFalse(os.path.lexists(self.alias(11)))
        self.assertIn("short alias removed", out["entries"][0]["detail"])

    def test_essai_ne_balaye_pas_les_alias(self):
        self.env(12)
        shutil.rmtree(self.real(12))
        out = menage.cleanup_lot_dirs(self.cfg, self.pol, agents=[self.agent],
                                      ended_only=False, dry_run=True)
        self.assertTrue(os.path.lexists(self.alias(12)))
        self.assertEqual(out["dirs"], [])
        self.assertIn(self.alias(12), {p["path"] for p in out["plan"]})

    def test_un_processus_qui_nomme_l_alias_garde_le_dossier(self):
        self.env(13)
        link = self.alias(13)

        def holders(path, needle=None, allow=()):
            if needle and needle == link:
                return [{"pid": 4242, "why": "command line", "name": "test"}]
            return []

        with mock.patch.object(menage, "tmp_holder", self.real_tmp_holder), \
                mock.patch.object(menage.platform, "holders", holders):
            out = menage.cleanup_lot_dirs(self.cfg, self.pol, agents=[self.agent],
                                          ended_only=False)
        self.assertTrue(os.path.isdir(self.real(13)))
        self.assertTrue(os.path.lexists(link))
        self.assertTrue(any("held by process" in note for note in out["skipped"]))

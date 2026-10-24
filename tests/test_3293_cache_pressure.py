# SPDX-License-Identifier: AGPL-3.0-only
"""#3293 : le ménage libère les caches sous pression disque, pas seulement
au-delà du quota — et le cache npm peut partir pendant un tour dès lors
qu'aucun npm ne tourne et que rien ne le tient (les stores pnpm/yarn restent
interdits pendant un tour)."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from ameesh import menage, platform, storage

from .support import PgTestCase


class _Cfg:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.host = "pc"


def _write(path: str, size: int, age_s: float = 0.0) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"z" * size)
    if age_s:
        moment = time.time() - age_s
        os.utime(path, (moment, moment))


def _real_free(root: str) -> int:
    return shutil.disk_usage(root).free


class EvictAtLeastTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-3293-")
        self.addCleanup(shutil.rmtree, self.base)
        menage._ensure_root(self.base)   # racine gérée : l'éviction l'exige

    def _five(self):
        files = [os.path.join(self.base, "f%d" % i) for i in range(5)]
        for i, path in enumerate(files):
            _write(path, 8192, age_s=7200 - i)   # f0 le plus ancien
        return files

    def test_libere_le_deficit_meme_sous_le_quota(self):
        files = self._five()
        result = menage.evict_lru([self.base], self.base, 100_000,
                                  evict_at_least=12_000)
        self.assertEqual(result["evicted"], 16_384)
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["after"], result["before"] - 16_384)
        self.assertFalse(result["over_quota"])
        self.assertEqual([os.path.exists(p) for p in files],
                         [False, False, True, True, True])

    def test_sans_deficit_le_quota_suffit(self):
        files = self._five()
        result = menage.evict_lru([self.base], self.base, 100_000)
        self.assertEqual(result["evicted"], 0)
        self.assertTrue(all(os.path.exists(p) for p in files))

    def test_un_fichier_trop_recent_est_saute(self):
        ancien = os.path.join(self.base, "ancien")
        recent = os.path.join(self.base, "recent")
        _write(ancien, 8192, age_s=7200)
        _write(recent, 8192)
        result = menage.evict_lru([self.base], self.base, 100_000,
                                  evict_at_least=12_000)
        self.assertFalse(os.path.exists(ancien))
        self.assertTrue(os.path.exists(recent))
        self.assertEqual(result["evicted"], 8192)


class CachePressionTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("housekeeping_log", "managed_worktrees", "host_resources"):
            self.db.execute("DELETE FROM %s" % table)
        # un état par test : le cache d'un test ne doit pas être le candidat
        # le plus ancien de l'éviction du suivant
        self.cfgl = _Cfg(os.path.join(self.tmp, "state-%s" % self._testMethodName))
        self.cfgl.host = self.cfg.host

    def _cache(self, nom: str) -> str:
        """Le dossier de cache `nom`, créé par un tour (marqueur posé)."""
        menage.turn_env(self.cfgl, menage.Policy(), "a1", {})
        return os.path.join(menage.cache_root(self.cfgl, menage.Policy()), nom)

    def _sans_processus(self, noms=()):
        """Aucun processus ne tient un cache, et les gestionnaires `noms`
        tournent (titre de processus, comme `npm install …`)."""
        table = [{"pid": 10, "name": nom} for nom in noms]
        for name, answer in (
                ("process_table", lambda *a, **k: table),
                ("user_paths", lambda *a, **k: platform.UserPaths())):
            patcher = mock.patch.object(platform, name, answer)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _pression(self) -> int:
        """Une cible d'espace libre juste au-dessus de l'actuel : un déficit."""
        return _real_free(menage.cache_root(self.cfgl, menage.Policy())) + 4096

    def _rapport(self, **kw):
        return menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc",
                               actor="t", now=time.time(), **kw)

    def test_pression_libe_re_le_cache_npm_pendant_un_tour(self):
        npm = self._cache("npm")
        _write(os.path.join(npm, "_cacache", "content"), 8192, age_s=7200)
        self._sans_processus()
        rapport = self._rapport(busy=True, free_target=self._pression())
        self.assertFalse(os.path.exists(os.path.join(npm, "_cacache")))
        evicted = [e for e in rapport["entries"]
                   if e.get("kind") == "cache" and e.get("action") == "evicted"]
        self.assertEqual(len(evicted), 1)
        self.assertEqual(evicted[0]["bytes"], 8192)
        self.assertFalse(any("npm is running" in s for s in rapport["skipped"]))

    def test_npm_en_cours_garde_son_cache(self):
        npm = self._cache("npm")
        _write(os.path.join(npm, "_cacache", "content"), 8192, age_s=7200)
        self._sans_processus(noms=("npm install lod",))
        rapport = self._rapport(busy=True, free_target=self._pression())
        self.assertTrue(os.path.exists(os.path.join(npm, "_cacache", "content")))
        self.assertTrue(any("npm is running" in s for s in rapport["skipped"]))

    def test_cache_npm_tenu_par_un_processus_garde(self):
        npm = self._cache("npm")
        fichier = os.path.join(npm, "_cacache", "content")
        _write(fichier, 8192, age_s=7200)
        up = platform.UserPaths()
        up.paths[fichier] = "123 node"
        for name, answer in (("process_table", lambda *a, **k: []),
                             ("user_paths", lambda *a, **k: up)):
            patcher = mock.patch.object(platform, name, answer)
            patcher.start()
            self.addCleanup(patcher.stop)
        rapport = self._rapport(busy=True, free_target=self._pression())
        self.assertTrue(os.path.exists(fichier))
        self.assertTrue(any("cache npm kept" in s for s in rapport["skipped"]))

    def test_store_pnpm_reste_garde_pendant_un_tour_meme_sous_pression(self):
        pnpm = self._cache("pnpm")
        _write(os.path.join(pnpm, "v3", "f"), 8192, age_s=7200)
        self._sans_processus()
        rapport = self._rapport(busy=True, free_target=self._pression())
        self.assertTrue(os.path.exists(os.path.join(pnpm, "v3", "f")))
        self.assertTrue(any("turn is in progress" in s for s in rapport["skipped"]))
        # hors tour, la pression le libère aussi
        rapport = self._rapport(busy=False, free_target=self._pression())
        self.assertFalse(os.path.exists(os.path.join(pnpm, "v3", "f")))
        self.assertTrue(any(e.get("kind") == "cache" for e in rapport["entries"]))

    def test_npm_evince_au_dela_du_quota_meme_pendant_un_tour(self):
        npm = self._cache("npm")
        _write(os.path.join(npm, "_cacache", "content"), 8192, age_s=7200)
        self._sans_processus()
        pol = menage.Policy(cache_quota=10)
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                                  busy=True)
        self.assertFalse(os.path.exists(os.path.join(npm, "_cacache", "content")))
        self.assertTrue(any(e.get("kind") == "cache" for e in rapport["entries"]))

    def test_sans_pression_ni_tour_le_cache_npm_part_au_quota(self):
        npm = self._cache("npm")
        _write(os.path.join(npm, "_cacache", "content"), 8192, age_s=7200)
        self._sans_processus()
        pol = menage.Policy(cache_quota=10)
        rapport = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                                  busy=False)
        self.assertFalse(os.path.exists(os.path.join(npm, "_cacache", "content")))
        self.assertEqual(rapport["entries"][0]["kind"], "cache")

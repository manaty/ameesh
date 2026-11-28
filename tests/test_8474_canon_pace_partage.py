# SPDX-License-Identifier: AGPL-3.0-only
"""#8474 — un exécuteur à 30+ workers ne relit pas le canon à chaque `pick`.

Constat (manaty/ameesh#6681) : `canon_pace` mettait la règle en cache par
`id(cfg)` et vidait tout à 16 entrées. Un `Runner` hébergeant ~30 workers a
une configuration par worker : la 17e vidait le cache, et presque chaque
`pick()` repassait par `mechanics.configured` → `canon.load_configured`, qui
reparse toutes les fiches YAML (0,7–1,5 cœur au repos, 60–450 s en
« preparation waiting: choosing turn »). Second chemin, même ticket : chaque
`accounts.parse` relisait `harnesses.known_ids` → `scan`, et `Profile.home()`
redemandait les clés du descripteur pour chaque compte.

Témoin : canon Git de 100 fiches, 30 puis 60 configurations de worker ;
`canon.from_config` et `mechanics.load_global` sont comptés — une découverte
par TTL, pas une par `pick` ; une référence qui avance est relue au plus tard
à l'expiration du TTL, sans fuite d'un canon à l'autre ; le cache est borné
et n'est plus vidé en bloc.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import accounts, canon, config, harnesses, mechanics

BASE = """schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
build:
  max_in_build: 2
ci:
  packet: full
  incident_label: ci-full
  environment_red: does_not_block
  faulty_lot: removed_without_new_review
reviews:
  families: [claude, codex, deepseek]
  exclude_author_family: true
  distinct_families: true
merge:
  named_fire: false
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
projects: {}
"""

FEDERATION = """federation: "0.1"
id: bench
members: []
"""

ACCOUNTS = {"codex": [{"name": "primaire"}]}
THREE_HARNESSES = {
    "claude": [{"name": "primaire"}],
    "codex": [{"name": "primaire"}],
    "deepseek": [{"name": "primaire"}],
}


def _mechanics_text(multiplier: int) -> str:
    return BASE + (
        "accounts:\n"
        "  underused_loss: 0.5\n"
        "  pace_multipliers:\n"
        "    codex:\n"
        "      primaire: %d\n" % multiplier)


class _Repo:
    """Un canon Git minimal : `federation.yaml`, mécanique et N fiches."""

    def __init__(self, root: str, *, fiches: int = 100, multiplier: int = 2):
        self.root = root
        self.dry = dict(os.environ,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        os.makedirs(root, exist_ok=True)
        self.git("init", "-q")
        self.write("federation.yaml", FEDERATION)
        self.set_multiplier(multiplier)
        for index in range(fiches):
            self.write("processus/fiche-%03d.md" % index,
                       "---\ntype: Process\ntitle: fiche %d\n---\ncorps %d\n"
                       % (index, index))
        self.commit("initial")
        self.multiplier = multiplier

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", self.root, *args],
                              capture_output=True, text=True, env=self.dry)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, path: str, text: str) -> None:
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def set_multiplier(self, multiplier: int) -> None:
        self.multiplier = multiplier
        self.write("processus/mecanique.yaml", _mechanics_text(multiplier))

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")


class _PaceCase(unittest.TestCase):
    def setUp(self) -> None:
        for cache in (accounts._CANON_PACE, harnesses._KNOWN_IDS,
                      accounts._ACCOUNT_KEYS, mechanics._CONFIGURED):
            cache.clear()
            self.addCleanup(cache.clear)
        self.tmp = tempfile.mkdtemp(prefix="ameesh-8474-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def repo(self, name: str = "canon", **kwargs) -> _Repo:
        return _Repo(os.path.join(self.tmp, name), **kwargs)

    def cfg(self, repo: _Repo, accounts_raw=None) -> config.Config:
        return config.Config(canon=repo.root, canon_ref="HEAD",
                             accounts=dict(accounts_raw or ACCOUNTS))

    def clock(self, start: float = 1000.0):
        now = [start]
        patcher = mock.patch.object(accounts.time, "monotonic",
                                    side_effect=lambda: now[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        return now

    def counted(self, module, name: str):
        calls = []
        real = getattr(module, name)

        def wrapper(*args, **kwargs):
            calls.append(name)
            return real(*args, **kwargs)

        patcher = mock.patch.object(module, name, side_effect=wrapper)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls


class ThirtyWorkersTest(_PaceCase):
    """Une découverte par TTL, partagée par les workers — pas une par pick."""

    def test_30_workers_share_one_discovery_per_ttl(self):
        repo = self.repo(fiches=100, multiplier=2)
        workers = [self.cfg(repo) for _ in range(30)]
        now = self.clock()
        discoveries = self.counted(canon, "from_config")
        compilations = self.counted(mechanics, "load_global")

        for _round in range(4):
            for worker in workers:
                profiles = accounts.profiles(worker, "codex")
                self.assertEqual(profiles[0].pace_multiplier, 2.0)

        self.assertEqual(len(discoveries), 1)
        self.assertEqual(len(compilations), 1)
        self.assertEqual(len(accounts._CANON_PACE), 1)

        # une référence qui avance est relue au plus tard au TTL : une
        # découverte de plus, et le document compilé reste celui du commit
        now[0] += accounts.CANON_PACE_TTL_S + 1.0
        self.assertEqual(accounts.profiles(workers[0], "codex")[0].pace_multiplier, 2.0)
        self.assertEqual(len(discoveries), 2)
        self.assertEqual(len(compilations), 1)

    def test_60_workers_share_one_discovery_per_ttl(self):
        repo = self.repo(fiches=100, multiplier=3)
        workers = [self.cfg(repo) for _ in range(60)]
        self.clock()
        discoveries = self.counted(canon, "from_config")
        for _round in range(2):
            for worker in workers:
                self.assertEqual(
                    accounts.profiles(worker, "codex")[0].pace_multiplier, 3.0)
        self.assertEqual(len(discoveries), 1)

    def test_the_cache_is_not_empty_when_a_17th_configuration_arrives(self):
        """Le défaut d'origine : 16 entrées puis un vidage en bloc."""
        repo = self.repo(fiches=1, multiplier=2)
        workers = [self.cfg(repo) for _ in range(17)]
        now = self.clock()
        discoveries = self.counted(canon, "from_config")
        for worker in workers:
            accounts.profiles(worker, "codex")
        self.assertEqual(len(discoveries), 1)

        # une 18e configuration dans le même TTL ne coûte rien non plus
        accounts.profiles(self.cfg(repo), "codex")
        self.assertEqual(len(discoveries), 1)


class RefAndLeakTest(_PaceCase):
    """La référence avance et les canons ne se mélangent pas."""

    def test_a_new_commit_updates_the_pace_without_leaking_to_another_canon(self):
        first = self.repo("first", fiches=1, multiplier=2)
        second = self.repo("second", fiches=1, multiplier=5)
        first_cfgs = [self.cfg(first) for _ in range(10)]
        second_cfgs = [self.cfg(second) for _ in range(10)]
        now = self.clock()
        discoveries = self.counted(canon, "from_config")

        self.assertEqual(
            accounts.profiles(first_cfgs[0], "codex")[0].pace_multiplier, 2.0)
        self.assertEqual(
            accounts.profiles(second_cfgs[0], "codex")[0].pace_multiplier, 5.0)
        self.assertEqual(len(discoveries), 2)
        self.assertEqual(len(accounts._CANON_PACE), 2)

        # la référence du premier canon avance : nouvelle règle après le TTL
        now[0] += accounts.CANON_PACE_TTL_S + 1.0
        first.set_multiplier(3)
        first.commit("rythme 3")
        self.assertEqual(
            accounts.profiles(first_cfgs[-1], "codex")[0].pace_multiplier, 3.0)
        # le second canon n'a pas bougé et ne reçoit pas la valeur du premier
        self.assertEqual(
            accounts.profiles(second_cfgs[-1], "codex")[0].pace_multiplier, 5.0)
        self.assertEqual(len(discoveries), 4)

    def test_an_unreadable_canon_is_read_once_per_ttl_for_all_workers(self):
        workers = [config.Config(canon=os.path.join(self.tmp, "absent"),
                                 canon_ref="HEAD", accounts=dict(ACCOUNTS))
                   for _ in range(30)]
        now = self.clock()
        reads = []

        def failing(cfg):
            reads.append(cfg)
            raise mechanics.MechanicsError("unreadable")

        with mock.patch.object(mechanics, "configured", side_effect=failing):
            values = [accounts.canon_pace(worker) for worker in workers]
            self.assertEqual(len(reads), 1)
            self.assertTrue(all(value == (None, "unreadable") for value in values))

            # les 30 ont partagé l'erreur ; une lecture de plus après le TTL
            now[0] += accounts.CANON_PACE_TTL_S + 1.0
            accounts.canon_pace(workers[0])
            self.assertEqual(len(reads), 2)


class BoundedCacheTest(_PaceCase):
    """LRU borné : on retire les plus anciens, on ne vide plus tout."""

    def test_the_cache_evicts_the_oldest_instead_of_clearing(self):
        repos = [self.repo("canon-%d" % index, fiches=1, multiplier=index + 1)
                 for index in range(6)]
        cfgs = [self.cfg(repo) for repo in repos]
        now = self.clock()
        discoveries = self.counted(canon, "from_config")
        with mock.patch.object(accounts, "CANON_PACE_MAX", 3):
            for index, cfg in enumerate(cfgs):
                rule, error = accounts.canon_pace(cfg, now=now[0] + index)
                self.assertEqual((rule.multiplier("codex", "primaire"), error),
                                 (index + 1, ""))
            self.assertLessEqual(len(accounts._CANON_PACE), 3)
            self.assertEqual(len(discoveries), 6)

            # les trois plus récents sont encore servis sans découverte
            for index in (3, 4, 5):
                rule, _error = accounts.canon_pace(cfgs[index], now=now[0] + index)
                self.assertEqual(rule.multiplier("codex", "primaire"), index + 1)
            self.assertEqual(len(discoveries), 6)
            # le plus ancien, évincé, est relu
            accounts.canon_pace(cfgs[0], now=now[0])
            self.assertEqual(len(discoveries), 7)


class HarnessScanTest(_PaceCase):
    """Le second chemin du ticket : les descripteurs ne sont plus relus par pick."""

    def test_known_ids_is_scanned_once_per_ttl_not_once_per_pick(self):
        repo = self.repo(fiches=1, multiplier=2)
        workers = [self.cfg(repo, THREE_HARNESSES) for _ in range(30)]
        now = self.clock()
        scans = self.counted(harnesses, "scan")

        for _round in range(4):
            for worker in workers:
                self.assertEqual(len(accounts.profiles(worker, "codex")), 1)
        self.assertEqual(len(scans), 1)

        now[0] += harnesses.KNOWN_IDS_TTL_S + 1.0
        accounts.profiles(workers[0], "codex")
        self.assertEqual(len(scans), 2)

    def test_parse_scans_descriptors_once_for_three_harnesses(self):
        scans = self.counted(harnesses, "scan")
        parsed = accounts.parse(THREE_HARNESSES)
        self.assertEqual(sorted(parsed), ["claude", "codex", "deepseek"])
        self.assertEqual(len(scans), 1)


class LoaderTest(_PaceCase):
    """Lecteur C quand il est là."""

    @unittest.skipUnless(canon._yaml is not None
                         and getattr(canon._yaml, "CSafeLoader", None) is not None,
                         "PyYAML sans libyaml")
    def test_the_strict_loader_rides_the_c_fast_path(self):
        self.assertTrue(issubclass(canon._StrictLoader, canon._yaml.CSafeLoader))
        with self.assertRaises(canon.YamlError):
            canon.load_yaml("a: 1\na: 2\n", use_pyyaml=True)


class LocalConfigurationFallbackTest(_PaceCase):
    """Sans canon configuré, le cache reste par objet de configuration."""

    def test_two_bare_configurations_do_not_share_a_value(self):
        first = config.Config(accounts=dict(ACCOUNTS))
        second = config.Config(accounts=dict(ACCOUNTS))
        with mock.patch.object(mechanics, "configured",
                               side_effect=mechanics.MechanicsError("no canon")):
            self.assertEqual(accounts.canon_pace(first), (None, "no canon"))
            self.assertEqual(accounts.canon_pace(second), (None, "no canon"))
        self.assertEqual(len(accounts._CANON_PACE), 2)

    def test_a_configuration_with_a_non_iterable_canon_entries_still_answers(self):
        """Configurations factices des tests : `canon_entries` n'est pas itérable."""
        cfg = mock.Mock()
        cfg.canon_entries = object()
        with mock.patch.object(mechanics, "configured",
                               side_effect=mechanics.MechanicsError("no canon")):
            self.assertEqual(accounts.canon_pace(cfg), (None, "no canon"))
        self.assertIn(("cfg", id(cfg)), accounts._CANON_PACE)

    def test_a_reused_object_id_is_still_guarded(self):
        first = config.Config(accounts=dict(ACCOUNTS))
        second = config.Config(accounts=dict(ACCOUNTS))
        # on simule un id réutilisé : l'objet gardé n'est pas le nouveau
        accounts._CANON_PACE[("cfg", id(second))] = (first, 0.0, (None, "old"))
        with mock.patch.object(mechanics, "configured",
                               side_effect=mechanics.MechanicsError("fresh")):
            self.assertEqual(accounts.canon_pace(second), (None, "fresh"))


if __name__ == "__main__":
    unittest.main()

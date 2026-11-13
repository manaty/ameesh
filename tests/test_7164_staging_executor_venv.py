# SPDX-License-Identifier: AGPL-3.0-only
"""#7164 : le venv de l'exécuteur de staging suit le commit qu'il déploie.

Le compte dédié n'est jamais touché par `vm.sh` : sans geste opérateur, un
correctif de l'exécuteur (#7053) restait inactif. Après chaque passe
`completed`, l'exécuteur dérive le paquet scellé du commit déployé, prépare
et active son venv (avance seulement, retour si le lancement échoue), et
enregistre la version réellement lancée ; `staging_executor_stale` part quand
elle reste en retard. Témoins : la séquence des étapes sur un vrai dépôt git
(étapes simulées), la passe de l'exécuteur du #4880, l'alerte, et la
dérivation du paquet sur le banc synthétique de #5666.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import staging_executor, staging_watch
from tests import test_4880_staging_executor as fixture
from tests import test_5666_venv_staging as banc

REAL_RUN = subprocess.run


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_NOSYSTEM="1", HOME=repo)
    return REAL_RUN(["git", "-C", repo, *args], env=env, check=True,
                    capture_output=True, text=True).stdout.strip()


class Hote(unittest.TestCase):
    """Un clone dédié à deux commits et un venv de bascule sur l'ancien."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="7164-venv-")
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.repo = os.path.join(self.root, "clone")
        os.mkdir(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "old")
        self.old = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "new")
        self.new = _git(self.repo, "rev-parse", "HEAD")
        self.share = os.path.join(self.root, "share")
        os.makedirs(os.path.join(self.share, "venv"))
        os.makedirs(os.path.join(self.share, "venv-" + self.old[:7]))
        os.symlink("venv-" + self.old[:7],
                   os.path.join(self.share, staging_executor.BASCULE_CURRENT))
        self.work = os.path.join(self.root, "auto")
        os.mkdir(self.work)
        self.package = os.path.join(self.root, "paquet-operateur")
        self.options = dict(venv_update=dict(
            venv=os.path.join(self.share, "venv"), package=self.package,
            work=self.work, python="/usr/bin/python3"))
        self.steps = []
        self.failing = {}

    def step(self, argv, cwd=None):
        self.steps.append(argv)
        name = "launch" if "-m" in argv else argv[3]
        if name == "deriver":
            os.mkdir(argv[argv.index("--output") + 1])
        if name == "activer":
            link = os.path.join(self.share, staging_executor.BASCULE_CURRENT)
            os.unlink(link)
            os.symlink("venv-" + argv[argv.index("--version") + 1], link)
        if name == "retour":
            link = os.path.join(self.share, staging_executor.BASCULE_CURRENT)
            os.unlink(link)
            os.symlink("venv-" + self.old[:7], link)
        return self.failing.get(name)

    def refresh(self, sha=None):
        with mock.patch.object(staging_executor, "_venv_step", side_effect=self.step):
            return staging_executor.refresh_venv(
                self.options, self.repo, "/tree", sha or self.new)

    def names(self):
        return ["launch" if "-m" in argv else argv[3] for argv in self.steps]

    def live(self):
        return staging_executor.executor_venv(self.options)["live"]


class RefreshTest(Hote):
    def test_sealed_route_in_order_then_package_becomes_the_base(self):
        result = self.refresh()
        self.assertEqual(result, {"state": "installed", "from": self.old[:7],
                                  "to": self.new[:7]})
        self.assertEqual(self.names(), ["deriver", "produire", "preparer",
                                        "activer", "launch"])
        derive, manifest, prepare, activate, launch = self.steps
        package = os.path.join(self.work, "paquet-" + self.new[:7])
        # Derived from the operator package, from the deployed tree's tools.
        self.assertEqual(derive[2], "/tree/deploy/mise-a-jour/venv-staging.py")
        self.assertEqual(derive[derive.index("--lock") + 1],
                         os.path.join(self.package, "requirements-staging.lock"))
        self.assertIn("--scelle", manifest)
        # Never the index: the four locked options of #5711 go together.
        for option in ("--lock", "--wheelhouse", "--manifeste", "--verificateur"):
            self.assertIn(option, prepare)
        self.assertEqual(prepare[prepare.index("--lock") + 1],
                         os.path.join(package, "requirements-staging.lock"))
        self.assertEqual(activate[activate.index("--precedent") + 1], self.old[:7])
        self.assertEqual(launch[0], os.path.join(
            self.share, "venv-courant", "bin", "python"))
        self.assertEqual(self.live(), self.new[:7])
        self.assertEqual(os.path.realpath(os.path.join(self.work, "paquet-courant")),
                         os.path.realpath(package))

    def test_next_refresh_derives_from_the_last_installed_package(self):
        self.refresh()
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "next")
        later = _git(self.repo, "rev-parse", "HEAD")
        self.steps.clear()
        self.assertEqual(self.refresh(later)["state"], "installed")
        derive = self.steps[0]
        self.assertEqual(derive[derive.index("--lock") + 1], os.path.join(
            os.path.realpath(os.path.join(self.work, "paquet-" + self.new[:7])),
            "requirements-staging.lock"))
        # The base is kept until the next one replaces it; older go.
        self.assertEqual(sorted(os.listdir(self.work)), sorted([
            "paquet-courant", "paquet-" + self.new[:7], "paquet-" + later[:7]]))

    def test_current_venv_is_left_alone(self):
        self.assertEqual(self.refresh(self.old),
                         {"state": "current", "from": self.old[:7]})
        self.assertEqual(self.steps, [])

    def test_backward_move_is_refused(self):
        _git(self.repo, "checkout", "-q", "--orphan", "other")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "elsewhere")
        other = _git(self.repo, "rev-parse", "HEAD")
        result = self.refresh(other)
        self.assertEqual((result["state"], result["reason"]),
                         ("refused", "not a fast-forward from venv-" + self.old[:7]))
        self.assertEqual(self.steps, [])

    def test_failed_step_stops_before_activation(self):
        self.failing["produire"] = "venv-staging: roue absente du wheelhouse : x"
        result = self.refresh()
        self.assertEqual((result["state"], result["step"]), ("refused", "manifest"))
        self.assertIn("roue absente", result["reason"])
        self.assertEqual(self.names(), ["deriver", "produire"])
        self.assertEqual(self.live(), self.old[:7])
        self.assertFalse(os.path.lexists(os.path.join(self.work, "paquet-courant")))

    def test_launch_failure_returns_to_the_previous_venv(self):
        self.failing["launch"] = "ModuleNotFoundError: No module named 'ameesh'"
        result = self.refresh()
        self.assertEqual((result["state"], result["step"]), ("rolled back", "launch"))
        self.assertEqual(self.names()[-2:], ["launch", "retour"])
        self.assertEqual(self.live(), self.old[:7])

    def test_unconfigured_and_errors_are_never_raised(self):
        self.assertEqual(staging_executor.refresh_venv({}, self.repo, "/t", self.new),
                         {"state": "unconfigured"})
        with mock.patch.object(staging_executor, "_refresh_venv",
                               side_effect=OSError("boom")):
            result = staging_executor.refresh_venv(self.options, self.repo,
                                                   "/t", self.new)
        self.assertEqual(result["state"], "refused")

    def test_settings_require_absolute_paths(self):
        path = os.path.join(self.root, "staging.json")
        base = ('{"candidates": ["a", "b"], "key": "k", "project": "p", '
                '"role": "r", "repo": "/r", "bascule_env": "/b", "github": "o/r"')
        with open(path, "w") as fh:
            fh.write(base + ', "venv_update": {"venv": "rel", "package": "/p", '
                     '"work": "/w"}}')
        with self.assertRaises(ValueError):
            staging_executor.settings(path)
        with open(path, "w") as fh:
            fh.write(base + ', "venv_update": {"venv": "/v", "package": "/p", '
                     '"work": "/w"}}')
        self.assertEqual(staging_executor.settings(path)["venv_update"]["python"],
                         "/usr/bin/python3")


class CompletedPassTest(unittest.TestCase):
    """La passe du #4880 : le venv suit une passe completed, et elle seule."""

    def tick(self, child=None):
        case = fixture.ExecutorTest("test_attested_foreign_run_allows_next_attempt")
        if child:
            # Bound by setUp: patched before it.
            patch = mock.patch.object(fixture.ExecutorTest, "run_child", child)
            patch.start()
            self.addCleanup(patch.stop)
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.foreign, case.attested = [], []
        case.cfg.canon, case.cfg.canon_ref = "", ""
        records = []
        original = case.record

        def record(kind, key, project, data):
            records.append(dict(data))
            return original(kind, key, project, data)
        staging_executor.storage.of.return_value.events.record = record
        refreshed = dict(state="installed", **{"from": "1111111", "to": "aaaaaaa"})
        patches = [mock.patch.object(staging_executor, "refresh_venv",
                                     return_value=refreshed),
                   mock.patch.object(staging_executor, "executor_venv",
                                     return_value=dict(running="1111111",
                                                       live="aaaaaaa"))]
        with contextlib.ExitStack() as stack:
            mocks = [stack.enter_context(patch) for patch in patches]
            result = staging_executor.tick(case.db, case.cfg, case.options)
        return result, records, mocks[0]

    def test_completed_pass_records_the_outcome_after_the_terminal_record(self):
        result, records, refresh = self.tick()
        self.assertEqual(result["state"], "completed")
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[2:], ("tree", "a" * 40))
        terminal, final = records[-2:]
        self.assertEqual((terminal["state"], final["state"]), ("completed", "completed"))
        self.assertNotIn("venv_refresh", terminal)
        self.assertEqual(final["venv_refresh"]["state"], "installed")
        self.assertEqual(final["executor_venv"]["live"], "aaaaaaa")
        # The version is carried from the take of the request on.
        self.assertTrue(all("executor_venv" in data for data in records))

    def test_failed_pass_does_not_touch_the_venv(self):
        result, records, refresh = self.tick(
            lambda self, argv, env, *a, **k: (1, False))
        self.assertEqual(result["state"], "failed")
        refresh.assert_not_called()
        self.assertNotIn("venv_refresh", records[-1])


class AlertTest(unittest.TestCase):
    SHA = "a" * 40
    LATER = "b" * 40

    def findings(self, data, *, attested=(), descends=lambda a, d: True, ident=5):
        return staging_watch.executor_version_findings(
            [("mesh", dict(id=ident, data=dict(dict(
                state="completed", sha=self.SHA, notify="mesh-design"), **data)))],
            attested=attested, descends=descends)

    def test_executor_on_the_deployed_commit_is_silent(self):
        self.assertEqual(self.findings(dict(executor_venv=dict(
            running="1111111", live="aaaaaaa"))), [])

    def test_executor_older_than_7164_alerts(self):
        alert, = self.findings({})
        self.assertEqual((alert["type"], alert["reason"]),
                         ("staging_executor_stale", "mesh"))
        self.assertIn("not recorded", alert["detail"])
        self.assertEqual(alert["orchestrators"], ["mesh-design"])

    def test_refused_refresh_alerts_with_its_reason(self):
        alert, = self.findings(dict(
            executor_venv=dict(running="1111111", live="1111111"),
            venv_refresh=dict(state="refused", step="manifest",
                              reason="roue absente du wheelhouse")))
        self.assertEqual(alert["executor_venv"], "1111111")
        self.assertIn("refused at manifest: roue absente", alert["detail"])

    def test_later_staging_by_another_path_alerts(self):
        ok = dict(executor_venv=dict(running="1111111", live="aaaaaaa"))
        attested = [dict(id=9, data=dict(sha=self.LATER))]
        alert, = self.findings(ok, attested=attested)
        self.assertEqual(alert["sha"], self.LATER)
        # Its own pass's attestation (older id), or an unproved ancestry: quiet.
        self.assertEqual(self.findings(ok, attested=attested, ident=10), [])
        self.assertEqual(self.findings(ok, attested=attested,
                                       descends=lambda a, d: False), [])

    def test_newest_completed_record_is_read(self):
        rows = [dict(id=3, data=dict(state="failed")),
                dict(id=2, data=dict(state="completed", sha=self.SHA)),
                dict(id=1, data=dict(state="completed", sha="c" * 40))]
        events = mock.Mock()
        events.latest_type.return_value = [dict(object="staging-request:mesh")]
        events.recent_type.return_value = rows
        with mock.patch.object(staging_watch.storage, "of",
                               return_value=mock.Mock(events=events)):
            (key, row), = staging_watch.executor_versions(object())
        self.assertEqual((key, row["id"]), ("mesh", 2))

    def test_alert_type_is_wired(self):
        from ameesh import alert_briefs, exploitation, notify
        self.assertIn("staging_executor_stale", exploitation.ALERT_TYPES)
        self.assertIn("staging_executor_stale", alert_briefs.BRIEFED_TYPES)
        self.assertIn("staging_executor_stale", notify.TYPE_LABELS)


class DeriverTest(unittest.TestCase):
    """La dérivation sur le banc synthétique de #5666, sans réseau."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="7164-deriver-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.paquet = banc.Paquet(self.root)
        neuves = self.root / "neuves"
        neuves.mkdir(mode=0o700)
        self.nouvelle = banc._roue(
            neuves, "ameesh", "1.1", requires=("tiny-dep>=1.0",),
            scripts=("ameesh", "agent-mail"), module_name="ameesh")
        self.sortie = self.paquet.sortie / "paquet"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(self.paquet.garde())
        for patch in banc._patches(self.nouvelle):
            stack.enter_context(patch)

    def deriver(self):
        return banc.module._deriver(
            self.paquet.depot, sha=self.paquet.sha, lock_path=self.paquet.lock,
            wheelhouse=self.paquet.wheelhouse, python=self.paquet.python,
            output=self.sortie)

    def test_only_the_ameesh_wheel_changes_and_the_package_produces(self):
        resultat = self.deriver()
        self.assertEqual(resultat["wheel_sha256"], self.nouvelle["sha256"])
        lock = banc.module._lire_lock(self.sortie / "requirements-staging.lock")
        self.assertEqual(lock["records"]["ameesh"]["version"], "1.1")
        self.assertEqual(lock["records"]["ameesh"]["hashes"], (self.nouvelle["sha256"],))
        self.assertEqual(lock["records"]["tiny-dep"]["hashes"], (self.paquet.tiny["sha256"],))
        self.assertEqual(sorted(p.name for p in (self.sortie / "wheelhouse").iterdir()),
                         sorted([self.nouvelle["filename"], self.paquet.setuptools["filename"],
                                 self.paquet.tiny["filename"]]))
        # The derived package passes the production it feeds (real pip, offline).
        document = banc.module._produire(
            self.paquet.depot, sha=self.paquet.sha, tag=None,
            lock_path=self.sortie / "requirements-staging.lock",
            wheelhouse=self.sortie / "wheelhouse", python=self.paquet.python,
            output=self.paquet.sortie / "derive.json", scelle=True,
            travail=self.root / "travail")
        self.assertEqual(document["lock"]["sha256"], resultat["lock_sha256"])

    def test_existing_output_or_incoherent_base_is_refused(self):
        self.sortie.mkdir()
        with self.assertRaises(banc.module.Erreur):
            self.deriver()
        self.sortie.rmdir()
        (self.paquet.wheelhouse / self.paquet.tiny["filename"]).unlink()
        with self.assertRaises(banc.module.Erreur):
            self.deriver()
        self.assertFalse(self.sortie.exists())


if __name__ == "__main__":
    unittest.main()
